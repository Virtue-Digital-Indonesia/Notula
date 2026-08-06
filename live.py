"""
notula.live — near-realtime transcription while the meeting is still recording.

A second, cheaper transcription tier that runs *alongside* capture and produces a
rolling preview. It never touches the recording, and the authoritative
speaker-labeled output.txt is still produced by the batch whisper + pyannote
pass at stop. If anything here fails, the meeting is unaffected.

How it works
------------
`whisper-server` is started once per recording and keeps the model resident, so
each step costs inference only (a cold `whisper-cli` would pay ~0.6-1.1 s of
model load *per chunk*). Audio is tapped off the recorder's writer thread into a
per-source ring, and every `step` seconds the last N seconds are mixed and
POSTed to the server.

Why the windows overlap, and how text gets committed
----------------------------------------------------
Whisper revises its own earlier words as more context arrives, so a naive
"transcribe the last 3 s" loop produces text that visibly rewrites itself. This
uses **LocalAgreement-2**: a segment is committed only once two consecutive
windows agree on it. Committed text never changes again; everything after it is
shown as provisional. Cost is one extra step of latency on committed text.

Why the window length barely matters
------------------------------------
Whisper's encoder always runs over a padded 30 s mel spectrogram, so a 2 s
window costs about the same as a 10 s one (measured: 0.85 s vs 0.89 s on turbo).
Window length is therefore chosen for *accuracy*; only `step` trades latency
against duty cycle. See docs/realtime-transcription.md for the measurements.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request

import numpy as np

import osutil
import pipeline
import toolpaths

SR = 16000

WHISPER_SERVER = toolpaths.WHISPER_SERVER

# The window always starts at the commit anchor and runs to "now", so no audio is
# ever skipped. MAX_WINDOW_S caps how far it can grow (bounding decode cost and
# staying well inside the encoder's 30 s); past it we commit whatever we have.
MAX_WINDOW_S = 14.0
MIN_WINDOW_S = 0.8

# Fed near-silence, Whisper invents plausible filler ("Terima kasih.", "Thank
# you.", subtitle credits). Rather than blacklist phrases per language, gate on
# the audio itself: no energy in a region means no text from that region. This
# is deliberately conservative (~-56 dBFS) so quiet speech still gets through.
SILENCE_RMS = 0.0015

# Acoustic lookback. A window that begins mid-word gives Whisper a fragment it
# can't parse, and it fills the gap with plausible filler ("Terima kasih.").
# Feeding it a little already-committed audio for context fixes that; anything
# starting inside the lookback is context only and is never committed twice.
KEEP_S = 1.2

# Model menu. `step` comes from measured compute on an M1 Pro, chosen to sit near
# a ~30% duty cycle: latency ≈ step + compute. See the R&D doc.
MODELS = {
    "small": {
        "file": "ggml-small.bin",
        "label": "Small — fastest",
        "step": 1.0,
        "compute": 0.31,
        "tradeoff": "Fastest, and the only one that keeps up almost word-for-word. "
                    "Gets the gist right but mangles names and loan-words "
                    "(it runs proper nouns together) and can clip a word at a boundary. "
                    "Good for following along — the final transcript fixes it.",
    },
    "large-v3-turbo": {
        "file": "ggml-large-v3-turbo.bin",
        "label": "Turbo — balanced",
        "step": 2.5,
        "compute": 0.89,
        "tradeoff": "Recommended. Words match the final transcript — names and "
                    "numbers come out right — at the cost of arriving a couple of "
                    "seconds later, in shorter, choppier lines.",
    },
    "large-v3": {
        "file": "ggml-large-v3.bin",
        "label": "Large-v3 — highest",
        "step": 3.5,
        "compute": 1.30,
        "tradeoff": "The same model the final transcript uses, so the live text "
                    "rarely changes afterwards — but the slowest to appear and the "
                    "heaviest on battery. Only worth it if you're reading along "
                    "closely rather than glancing.",
    },
}
DEFAULT_MODEL = "large-v3-turbo"


def model_path(key: str):
    spec = MODELS.get(key)
    return (pipeline.MODELS_DIR / spec["file"]) if spec else None


def available_models() -> list[dict]:
    """The model menu with each entry's on-disk availability, for the UI."""
    out = []
    for key, spec in MODELS.items():
        p = model_path(key)
        out.append({
            "key": key,
            "label": spec["label"],
            "tradeoff": spec["tradeoff"],
            # what you actually see: text appears greyed after roughly half a step
            # plus one inference, then firms up a step later once it's agreed twice
            "latency": f"~{spec['step'] / 2 + spec['compute']:.1f}s",
            "available": bool(p and p.exists()),
            "file": spec["file"],
        })
    return out


def resolve_model(key: str) -> str:
    """The requested model if it's installed, else the first one that is."""
    if key in MODELS and (p := model_path(key)) and p.exists():
        return key
    for k in (DEFAULT_MODEL, *MODELS):
        if (p := model_path(k)) and p.exists():
            return k
    return key


# ---- audio plumbing ----------------------------------------------------------

class _Ring:
    """Mono float32 tail buffer. Only ever asked for "the last N seconds", which
    is what makes two independently-started sources trivially alignable: both
    tails end *now*, whenever each one happened to begin."""

    def __init__(self, cap_s: float = MAX_WINDOW_S + 8.0):
        self._cap = int(cap_s * SR)
        self._buf = np.zeros(0, dtype="float32")
        self._lock = threading.Lock()
        self.total = 0                      # samples ever seen (this source's clock)

    def add(self, block: np.ndarray) -> None:
        with self._lock:
            self._buf = np.concatenate((self._buf, block))
            if len(self._buf) > self._cap:
                self._buf = self._buf[-self._cap:]
            self.total += len(block)

    def tail(self, n: int) -> np.ndarray:
        with self._lock:
            return self._buf[-n:].copy() if n > 0 else np.zeros(0, dtype="float32")


def _wav_bytes(pcm: np.ndarray) -> bytes:
    data = np.clip(pcm * 32767.0, -32768, 32767).astype("<i2").tobytes()
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, SR, SR * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _norm(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def _trim_to_fresh(seg: dict, keep_s: float):
    """Keep only the part of a segment that lies past the lookback boundary.

    Whisper hands back per-word timestamps, so a segment straddling the boundary
    can be cut exactly: the words before it were committed on an earlier window
    (dropping the whole segment would lose the new half; keeping it would repeat
    the old half). Returns None if nothing new is left.
    """
    words = seg.get("words") or []
    if not words:
        return seg if float(seg.get("start") or 0.0) >= keep_s - 0.15 else None
    idx = next((i for i, w in enumerate(words)
                if float(w.get("start") or 0.0) >= keep_s - 0.05), None)
    if idx is None:
        return None
    # Whisper's "words" are really tokens, and a word can be several of them
    # ("mis" + "alnya"). Only a token carrying a leading space starts a new word,
    # so walk forward to one — otherwise the cut lands mid-word.
    while idx > 0 and idx < len(words) and not str(words[idx].get("word") or "").startswith(" "):
        idx += 1
    kept = words[idx:]
    if not kept:
        return None
    text = "".join(str(w.get("word") or "") for w in kept)
    if not text.strip():
        return None
    return {"text": text,
            "start": float(kept[0].get("start") or 0.0),
            "end": float(kept[-1].get("end") or seg.get("end") or 0.0),
            "words": kept}


# ---- the live transcriber ----------------------------------------------------

class LiveTranscriber:
    """Owns a whisper-server, a worker thread, and the committed/pending text.

    Everything is best-effort: any failure sets `self.error`, reports through
    `on_status`, and stops the tier without disturbing the recording.
    """

    def __init__(self, model_key: str, lang: str, on_update, on_status=None,
                 attribute: bool = False):
        self.model_key = resolve_model(model_key)
        self.spec = MODELS.get(self.model_key, MODELS[DEFAULT_MODEL])
        self.lang = lang or "id"
        self._on_update = on_update          # (lines, provisional) -> None
        self._on_status = on_status or (lambda *_: None)
        self._attribute = attribute          # label You/Them from source energy

        self.error = None
        self.ready = False
        self._proc = None
        self._port = None
        self._thread = None
        self._run = False

        self._rings: dict[str, _Ring] = {}
        self._rings_lock = threading.Lock()
        self._anchor = 0                     # master-clock sample where pending audio starts
        self._committed: list[dict] = []     # [{text, who}]
        self._pending = ""                   # provisional tail, not yet agreed twice
        self._prev_pending: list[str] = []   # last run's uncommitted segment texts

    # ---- audio in (called from the recorder's writer thread) ----

    def feed(self, kind: str, block: np.ndarray) -> None:
        if not self._run:
            return
        with self._rings_lock:
            ring = self._rings.get(kind)
            if ring is None:
                ring = self._rings[kind] = _Ring()
        ring.add(block)

    @property
    def _master(self):
        """Mic drives the clock — a recording always has one, and it starts first."""
        with self._rings_lock:
            return self._rings.get("mic") or next(iter(self._rings.values()), None)

    # ---- lifecycle ----

    def start(self) -> None:
        mp = model_path(self.model_key)
        if not mp or not mp.exists():
            self.error = f"model {self.spec['file']} is not installed"
            self._on_status("error", self.error)
            return
        if not os.path.exists(WHISPER_SERVER):
            # Name the path rather than a package manager: this is shared code,
            # and on Windows there is no brew and the user placed the binary
            # themselves, so where we looked is the only useful thing to say.
            self.error = f"whisper-server not found at {WHISPER_SERVER}"
            self._on_status("error", self.error)
            return
        self._run = True
        self._on_status("starting", f"loading {self.spec['label'].split(' —')[0]}…")
        self._thread = threading.Thread(target=self._main, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 4.0) -> None:
        self._run = False
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._kill_server()
        self.ready = False

    def _kill_server(self) -> None:
        p, self._proc = self._proc, None
        osutil.kill_tree(p)

    # ---- worker ----

    def _main(self) -> None:
        try:
            if not self._spawn_server():
                return
            self.ready = True
            self._on_status("ready", f"live · {self.spec['label'].split(' —')[0]}")
            step = float(self.spec["step"])
            nxt = time.monotonic()
            while self._run:
                nxt += step
                sleep = nxt - time.monotonic()
                if sleep > 0:
                    time.sleep(sleep)
                else:
                    nxt = time.monotonic()      # fell behind; don't spiral
                if not self._run:
                    break
                try:
                    self._step()
                except Exception as e:          # one bad step must not kill the tier
                    self.error = str(e)
            self._flush()
        except Exception as e:                  # pragma: no cover
            self.error = str(e)
            self._on_status("error", str(e))
        finally:
            self._kill_server()

    def _spawn_server(self) -> bool:
        self._port = _free_port()
        # timestamps must stay ON: segment start/end is what advances the commit
        # anchor and attributes speakers. Without them every window collapses to
        # one giant segment and the transcript repeats itself.
        cmd = [WHISPER_SERVER, "-m", str(model_path(self.model_key)),
               "-l", self.lang, "--host", "127.0.0.1", "--port", str(self._port),
               "-t", "8"]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=pipeline._clean_env(), **osutil.popen_kwargs())
        except OSError as e:
            self.error = f"could not start whisper-server: {e}"
            self._on_status("error", self.error)
            return False
        # readiness: the port accepts a connection once the model is resident
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and self._run:
            if self._proc.poll() is not None:
                self.error = "whisper-server exited during startup"
                self._on_status("error", self.error)
                return False
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.25)
        if self._run:
            self.error = "whisper-server did not become ready"
            self._on_status("error", self.error)
        return False

    # ---- one transcription step ----

    def _window(self):
        """(mixed audio, per-source tails, master index now, lookback seconds).

        The window covers [anchor - KEEP_S, now]: uncommitted audio, plus a short
        run-up for context. Everything before `keep_s` is context only.
        """
        master = self._master
        if master is None:
            return None, {}, 0, 0.0
        now = master.total
        # never start after the anchor (that would skip untranscribed speech)
        fresh = max(0, min(now - self._anchor, int(MAX_WINDOW_S * SR)))
        if fresh < int(MIN_WINDOW_S * SR):
            return None, {}, now, 0.0
        keep = min(int(KEEP_S * SR), self._anchor)
        want = min(fresh + keep, int(MAX_WINDOW_S * SR))
        with self._rings_lock:
            rings = dict(self._rings)
        tails = {k: r.tail(want) for k, r in rings.items()}
        n = max((len(t) for t in tails.values()), default=0)
        if n < int(MIN_WINDOW_S * SR):
            return None, {}, now, 0.0
        mix = np.zeros(n, dtype="float32")
        for t in tails.values():
            if len(t):
                mix[n - len(t):] += t          # end-aligned: both tails finish "now"
        np.clip(mix, -1.0, 1.0, out=mix)
        return mix, tails, now, max(0.0, (n - fresh) / float(SR))

    def _step(self) -> None:
        mix, tails, now, keep_s = self._window()
        if mix is None:
            return
        win_len = len(mix)
        if float(np.sqrt(np.mean(mix * mix))) < SILENCE_RMS:
            # dead air: drop it instead of paying for inference that would only
            # hallucinate. The anchor still advances, so nothing backs up.
            self._anchor = now
            self._prev_pending = []
            return
        segs = self._infer(mix)
        if segs is None:
            return
        # Strip the lookback: those words are context and were already committed.
        # A segment that straddles the boundary is trimmed word-by-word rather
        # than dropped, so we neither duplicate nor lose the half that is new.
        if keep_s > 0:
            segs = [t for s in segs if (t := _trim_to_fresh(s, keep_s)) is not None]
        segs = [s for s in segs if self._seg_rms(s, mix, win_len) >= SILENCE_RMS]
        if not segs:
            # Nothing new to commit yet. Hold the anchor so the audio gets another
            # pass with more context — only give up once the window is maxed out.
            if (now - self._anchor) > int(MAX_WINDOW_S * SR):
                self._anchor = now
            self._prev_pending = []
            self._pending = ""
            self._emit()
            return
        texts = [_norm(s["text"]) for s in segs]

        # LocalAgreement-2: commit the prefix this run and the last one agree on.
        k = 0
        while k < len(texts) and k < len(self._prev_pending) and texts[k] == self._prev_pending[k]:
            k += 1
        forced = (now - self._anchor) > int(MAX_WINDOW_S * SR)
        if forced:
            k = len(texts)                     # window is maxed out — take everything

        if k:
            for s in segs[:k]:
                t = (s["text"] or "").strip()
                if not t:
                    continue
                # Overlapping windows sometimes re-emit the tail of what was just
                # committed ("…so it is cheaper." then "cheaper."). Suffix match, not
                # substring, so a genuine short repeat ("betul", "ya") survives.
                nt = _norm(t)
                tail = _norm(" ".join(c["text"] for c in self._committed[-3:]))
                if nt and len(nt) >= 3 and tail.endswith(nt):
                    continue
                self._committed.append({
                    "text": t,
                    "who": self._attribute_segment(s, tails, win_len),
                })
            # Advance only to the end of what we actually committed — anything
            # after it is speech we haven't transcribed yet and must not skip.
            end_s = float(segs[k - 1].get("end") or 0.0)
            new_anchor = now - win_len + int(end_s * SR)
            if new_anchor <= self._anchor:      # missing/zero timestamps
                new_anchor = self._anchor + int(float(self.spec["step"]) * SR)
            self._anchor = min(now, max(self._anchor + 1, new_anchor))
            self._prev_pending = texts[k:]
        else:
            self._prev_pending = texts

        self._pending = " ".join((s["text"] or "").strip() for s in segs[k:]).strip()
        self._emit()

    @staticmethod
    def _seg_range(seg, win_len):
        a = int(max(0.0, float(seg.get("start") or 0.0)) * SR)
        b = int(max(0.0, float(seg.get("end") or 0.0)) * SR)
        if b <= a:
            b = a + int(0.2 * SR)
        return max(0, min(a, win_len)), max(0, min(b, win_len))

    def _seg_rms(self, seg, mix, win_len) -> float:
        try:
            a, b = self._seg_range(seg, win_len)
            s = mix[a:b]
            return float(np.sqrt(np.mean(s * s))) if len(s) else 0.0
        except Exception:
            return 1.0      # can't tell — keep the segment rather than lose speech

    def _attribute_segment(self, seg, tails, win_len):
        """Who spoke, from the energy split between the two capture sources — the
        mic is you, computer audio is everyone else. Free: no diarization model,
        no extra inference. Only meaningful when both sources are live, since a
        mic alone also picks up the speakers."""
        if not self._attribute or len(tails) < 2:
            return None
        mic, sysau = tails.get("mic"), tails.get("system")
        if mic is None or sysau is None or not len(mic) or not len(sysau):
            return None
        try:
            a, b = self._seg_range(seg, win_len)
            def rms(t):
                # tails are end-aligned to the window, so index into their tail end
                off = win_len - len(t)
                lo, hi = max(0, a - off), max(0, min(len(t), b - off))
                s = t[lo:hi]
                return float(np.sqrt(np.mean(s * s))) if len(s) else 0.0
            m, s = rms(mic), rms(sysau)
            if m > s * 2.5 and m > 1e-4:
                return "You"
            if s > m * 2.5 and s > 1e-4:
                return "Them"
        except Exception:
            pass
        return None

    def _infer(self, pcm: np.ndarray):
        boundary = "----notula-live-boundary"
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"w.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode(),
            _wav_bytes(pcm), b"\r\n",
        ]
        for key, val in (("response_format", "verbose_json"), ("language", self.lang),
                         ("temperature", "0"), ("no_context", "true")):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                         f"name=\"{key}\"\r\n\r\n{val}\r\n".encode())
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/inference", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.error = str(e)
            return None
        segs = data.get("segments")
        if isinstance(segs, list) and segs:
            return [{"text": s.get("text", ""), "start": s.get("start", 0.0),
                     "end": s.get("end", 0.0), "words": s.get("words") or []}
                    for s in segs if (s.get("text") or "").strip()]
        text = (data.get("text") or "").strip()
        return [{"text": text, "start": 0.0, "end": len(pcm) / SR}] if text else []

    # ---- output ----

    def _flush(self) -> None:
        """Turn whatever is still provisional into committed text, at stop."""
        if self._pending:
            self._committed.append({"text": self._pending, "who": None})
            self._pending = ""
            self._emit()

    def _emit(self) -> None:
        try:
            self._on_update(list(self._committed), self._pending)
        except Exception:
            pass

    def transcript_text(self) -> str:
        lines = []
        for c in self._committed:
            t = (c["text"] or "").strip()
            if not t:
                continue
            lines.append(f"{c['who']}: {t}" if c.get("who") else t)
        return "\n".join(lines)
