"""
notula.cloud — transcription through OpenAI's audio API, as an alternative to the
local whisper-cli + pyannote pass in pipeline.py.

Why it exists: the local pass needs ~3 GB of models, a whisper build and a
torch venv, and takes real time on a laptop. Sending the audio to OpenAI costs
money instead, and gpt-4o-transcribe-diarize labels speakers on its own, so no
HuggingFace token or pyannote is involved.

What it costs, and how the estimate is made
-------------------------------------------
gpt-transcribe is billed by audio length, so its price is exact. The diarize
model is billed per token, and its published "$0.006/min" undercounts badly:
the speaker labels and timestamps it writes are output tokens at $10/M, and
real meetings measured at about $0.018 a minute (see MODELS). So the estimate
starts from that measured figure, and after every run the app learns the rate
it actually paid (appcore stores it), so the next estimate tracks how densely
this user's meetings are spoken. The figure after a run is computed from the
token counts OpenAI reports, not estimated.

Batching would not make it cheaper: OpenAI's Batch API (half price) does not
accept audio transcription at all.

How a long meeting gets through
-------------------------------
The endpoint takes one file of at most 25 MB and ~25 minutes, so the recording
is cut into parts of up to CHUNK_S, each cut nudged onto the nearest pause
(ffmpeg silencedetect) and encoded as a small mono mp3.

Every part is *streamed* (stream=true). Without that, the API sends nothing
until a part is finished — two silent minutes per five-minute part — and on a
real network such an idle connection was dropped without either side noticing,
so the app waited out its whole timeout, twice. Streamed, a finished segment
arrives every few seconds: the connection never idles, progress is real, and
a genuine stall is noticed after STALL_S instead of ten minutes.

Keeping "SPEAKER_01" the same person across parts
-------------------------------------------------
Each request labels the voices it hears A, B, C… from scratch. The API also
takes up to four reference clips (2–10 s each) with names, and voices matching
a clip come back under that name. How the clips are chosen is the one choice
the user makes per run (`parallel`), because it trades speed for consistency:

  * parallel (the default): the first part runs alone, and the longest stretch
    of each of its most talkative speakers (up to four) is sent with every
    other part, CONCURRENCY parts at a time. Fast — but a voice that first
    speaks after part one can't be handed on between parts running side by
    side, so it gets its own number in each part.

  * one at a time: each part is sent only after the one before it, with clips
    of the most talkative voices heard in *all* the parts so far. Someone who
    joins twenty minutes in is learned in their first part and keeps their
    number after that (within the API's four-voice limit). Slower, same price.

A plain model has no voices to keep consistent, so it always runs in parallel.

Nothing is paid for twice
-------------------------
Each finished part is saved in the meeting folder (CACHE_DIR) as soon as it
arrives. If a run fails partway, transcribing again sends only the missing
parts; the saved ones are reused. The folder is removed once a run completes.

Like pipeline.py this is stdlib-only (plus ffmpeg), so the app venv needs no
OpenAI SDK, and nothing here knows what OS it is on.
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import queue
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import library
import osutil
import pipeline
from pipeline import FFMPEG, PipelineError, _Mono, _noop, probe_duration

API_URL = "https://api.openai.com/v1/audio/transcriptions"
KEYS_URL = "https://platform.openai.com/api-keys"

# Prices from https://platform.openai.com/docs/pricing on PRICES_CHECKED.
#
# `usd_per_min` is what the estimate starts from before this user has a run of
# their own. For gpt-transcribe it is the billed rate. For the diarize model it
# is measured, because the published $0.006/min is several times too low: a
# 3-minute slice of a real Indonesian meeting billed 2,242 input + 4,923 output
# tokens = $0.018/min, and synthetic speech billed $0.019–0.020/min.
#
# Two entries on purpose: the newest plain model, and the only one that labels
# speakers. gpt-4o-transcribe and gpt-4o-mini-transcribe are superseded by
# gpt-transcribe, so they're out. `lang_field` is how each takes the language:
# the newer model wants a list.
PRICES_CHECKED = "2026-09-29"
MODELS = {
    "gpt-4o-transcribe-diarize": {
        "label": "GPT-4o Transcribe Diarize",
        "usd_per_min": 0.018,
        "published_usd_per_min": 0.006,
        "billing": "tokens",
        "usd_per_m_in": 2.50,
        "usd_per_m_out": 10.00,
        "diarize": True,
        "lang_field": "language",
        # processing seconds per audio second, measured: a 5-minute part takes
        # about two minutes
        "speed": 0.42,
        "note": "speaker labels from OpenAI's own diarization — the one to use for meetings",
    },
    "gpt-transcribe": {
        "label": "GPT Transcribe",
        "usd_per_min": 0.0045,
        "billing": "duration",
        "diarize": False,
        "lang_field": "languages[]",
        "speed": 0.05,
        "note": "OpenAI's newest plain transcription, about four times cheaper — no speaker labels",
    },
}
DEFAULT_MODEL = "gpt-4o-transcribe-diarize"

# Chunking. Five-minute parts, plus up to CHUNK_SLACK_S so a cut can slide onto
# a pause. Far under the ~1400 s the API accepts, ~1.2 MB each at MP3_KBPS, and
# short enough that four of them in flight keep a long meeting moving.
CHUNK_S = 300.0
CHUNK_SLACK_S = 45.0
UPLOAD_LIMIT = 25 << 20
MP3_KBPS = 32
SILENCE_DB = -35        # silencedetect threshold; speech pauses, not just digital silence
SILENCE_MIN_S = 0.6

# Speaker reference clips: what the API accepts, and how many.
REF_MIN_S, REF_MAX_S, REF_MAX_SPEAKERS = 2.0, 10.0, 4

# Network. Parts in flight at once: the measured account allows 3,000 requests
# a minute, so four is far inside it, and a 429 is retried after Retry-After.
# STALL_S is how long a stream may go without a single byte before it counts
# as dead — measured gaps between segments were under 30 s.
CONCURRENCY = 4
STALL_S = 120
RETRIES = 3
RETRY_DELAY_S = 2.0      # first retry waits this long, then doubles
RETRY_STATUS = (408, 409, 429, 500, 502, 503, 504)

CACHE_DIR = library.CLOUD_PARTS

ProgressCB = Callable[[str, float, str], None]

_RE_SIL_START = re.compile(r"silence_start:\s*([0-9.]+)")
_RE_SIL_END = re.compile(r"silence_end:\s*([0-9.]+)")


class CloudError(PipelineError):
    """Anything that stops a cloud transcription. Subclasses PipelineError so
    the app's one except-clause covers both engines."""


class AuthError(CloudError):
    pass


class _Retryable(CloudError):
    """A failure worth another attempt at the same part."""

    def __init__(self, msg, retry_after: float = 0.0):
        super().__init__(msg)
        self.retry_after = retry_after


class _Refused(CloudError):
    """OpenAI rejected the request itself (a 4xx other than a rate limit), so
    every other part would be rejected the same way."""


class _Cancelled(CloudError):
    pass


# ---- settings helpers ---------------------------------------------------------

def api_key(cfg: dict) -> str:
    """$OPENAI_API_KEY wins, then the stored config value — same rule as the
    HuggingFace token."""
    return (os.environ.get("OPENAI_API_KEY") or cfg.get("openai_api_key") or "").strip()


def resolve_model(key) -> str:
    return key if key in MODELS else DEFAULT_MODEL


def available_models(learned: Optional[dict] = None) -> list[dict]:
    """The menu, for the UI: what each costs and whether it labels speakers.

    `learned` maps a model to the per-minute rate this user actually paid on
    earlier runs; where present it replaces the starting figure, and the page
    says which one it is showing.
    """
    learned = learned or {}
    out = []
    for k, m in MODELS.items():
        rate = learned.get(k)
        out.append({
            "key": k, "label": m["label"], "diarize": m["diarize"], "note": m["note"],
            "usd_per_min": rate if rate else m["usd_per_min"],
            "rate_source": ("learned" if rate else
                            ("billed" if m["billing"] == "duration" else "measured")),
            "published_usd_per_min": m.get("published_usd_per_min", m["usd_per_min"]),
            "speed": m["speed"],
        })
    return out


# ---- cost ---------------------------------------------------------------------

def fmt_usd(usd: float) -> str:
    """Money the way a person says it: cents, not a float."""
    if usd <= 0:
        return "$0.00"
    if usd < 0.01:
        return "less than $0.01"
    return f"${usd:,.2f}"


def estimate(duration_s: float, model: str, rate: Optional[float] = None) -> dict:
    """What `duration_s` seconds of audio should cost on `model`, at `rate`
    dollars a minute if given (a learned rate), else the model's starting one.

    Billed time is rounded up to whole seconds — the API reports usage in
    seconds — so a 59.2 s clip is priced as a minute, not 0.98 of one.
    """
    key = resolve_model(model)
    m = MODELS[key]
    per_min = rate if rate else m["usd_per_min"]
    secs = max(0, math.ceil(duration_s or 0))
    usd = secs / 60.0 * per_min
    return {
        "model": key,
        "label": m["label"],
        "seconds": secs,
        "usd_per_min": per_min,
        "usd": usd,
        "text": f"about {fmt_usd(usd)}",
        "estimated": True,
    }


def usage_cost(model: str, usage: Optional[dict], seconds: float) -> tuple[float, bool]:
    """What one request cost, from the usage OpenAI reported with it.

    Returns (usd, exact). Token counts are priced at the model's token rates;
    a duration at its per-minute rate. With no usage at all, the part is priced
    from its length at the starting rate, and marked not exact.
    """
    m = MODELS[resolve_model(model)]
    usage = usage or {}
    if usage.get("type") == "tokens" and "usd_per_m_in" in m:
        try:
            tin = float(usage.get("input_tokens") or 0)
            tout = float(usage.get("output_tokens") or 0)
            return (tin * m["usd_per_m_in"] + tout * m["usd_per_m_out"]) / 1e6, True
        except (TypeError, ValueError):
            pass
    if usage.get("type") == "duration" and usage.get("seconds") is not None:
        try:
            return float(usage["seconds"]) / 60.0 * m["usd_per_min"], m["billing"] == "duration"
        except (TypeError, ValueError):
            pass
    return seconds / 60.0 * m["usd_per_min"], False


# ---- chunk planning -----------------------------------------------------------

def _silences(wav: Path, on_proc=None) -> list[float]:
    """Midpoints of the pauses ffmpeg hears, in seconds. Best-effort: an empty
    list just means cuts fall on the clock instead of on a pause."""
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-i", str(wav), "-af",
           f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN_S}", "-f", "null", "-"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                **osutil.popen_kwargs())
        if on_proc:
            on_proc(proc)
        err = proc.communicate(timeout=600)[1]
    except (OSError, subprocess.TimeoutExpired):
        return []
    mids, start = [], None
    for line in err.splitlines():
        m = _RE_SIL_START.search(line)
        if m:
            start = float(m.group(1))
            continue
        m = _RE_SIL_END.search(line)
        if m and start is not None:
            mids.append((start + float(m.group(1))) / 2.0)
            start = None
    return mids


def plan_chunks(duration: float, silences: list[float], chunk_s: float | None = None,
                slack: float | None = None) -> list[tuple[float, float]]:
    """Cut `duration` seconds into [start, end) spans of at most chunk_s + slack.

    Each cut is the silence nearest to its target within ±slack, or the target
    itself if the speaker never paused. Pure, so it can be tested without audio.
    """
    chunk_s = CHUNK_S if chunk_s is None else chunk_s        # read at call time,
    slack = CHUNK_SLACK_S if slack is None else slack         # so tests can shrink them
    duration = float(duration or 0)
    if duration <= chunk_s:
        return [(0.0, duration)] if duration > 0 else []
    spans, start = [], 0.0
    while duration - start > chunk_s:
        target = start + chunk_s
        near = [s for s in silences if abs(s - target) <= slack and s > start + 1.0]
        cut = min(near, key=lambda s: abs(s - target)) if near else target
        spans.append((start, cut))
        start = cut
    spans.append((start, duration))
    return spans


def runs_parallel(model: str, parallel: bool = True) -> bool:
    """Whether a run sends parts side by side. Only a diarizing model has a
    reason not to (see the module docstring); a plain one always does."""
    return bool(parallel) or not MODELS[resolve_model(model)]["diarize"]


def eta_seconds(duration_s: float, model: str, parallel: bool = True) -> float:
    """Roughly how long a run takes at the model's measured speed: in parallel,
    the first part alone (for the voices) and then the rest CONCURRENCY at a
    time; one at a time, every part in turn."""
    m = MODELS[resolve_model(model)]
    duration_s = max(0.0, float(duration_s or 0))
    if duration_s <= 0:
        return 0.0
    parts = max(1, math.ceil(duration_s / CHUNK_S))
    per_part = min(duration_s, CHUNK_S) * m["speed"] + 5.0      # + upload and setup
    conc = CONCURRENCY if runs_parallel(model, parallel) else 1
    if m["diarize"]:
        return per_part * (1 + math.ceil((parts - 1) / conc))
    return per_part * math.ceil(parts / conc)


# ---- ffmpeg helpers -----------------------------------------------------------

def _run(cmd, on_proc=None, timeout=600) -> bytes:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            **osutil.popen_kwargs())
    if on_proc:
        on_proc(proc)
    out, err = proc.communicate(timeout=timeout)
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()
        raise CloudError("ffmpeg failed: " + (tail[-1] if tail else f"exit {proc.returncode}"))
    return out


def _encode_chunk(wav: Path, start: float, end: float, dst: Path, on_proc=None) -> None:
    """One chunk as a fresh mono mp3. Encoded from the span rather than cut
    from a whole-file mp3 so it carries no duration metadata from the original —
    the gpt-4o models read that and reject the chunk as too long."""
    _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
          "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(wav),
          "-map_metadata", "-1", "-ac", "1", "-ar", "16000",
          "-c:a", "libmp3lame", "-b:a", f"{MP3_KBPS}k", str(dst)], on_proc)


def _reference_clip(wav: Path, start: float, end: float, tmp: Path, on_proc=None) -> str:
    """A speaker sample as the data URL the API wants, from the middle of a span.

    Written to a file rather than piped: a WAV streamed to a pipe carries no
    sizes in its header (ffmpeg can't seek back to fill them in), and a decoder
    that trusts the header sees a 4 GB clip."""
    length = min(REF_MAX_S, end - start)
    mid = (start + end) / 2.0
    s = max(0.0, mid - length / 2.0)
    dst = tmp / f"ref-{int(s * 1000)}.wav"
    _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
          "-ss", f"{s:.3f}", "-t", f"{length:.3f}", "-i", str(wav),
          "-map_metadata", "-1", "-ac", "1", "-ar", "16000",
          "-c:a", "pcm_s16le", str(dst)], on_proc)
    return "data:audio/wav;base64," + base64.b64encode(dst.read_bytes()).decode("ascii")


# ---- HTTP ---------------------------------------------------------------------

def _multipart(fields: list[tuple[str, str]], files: list[tuple[str, str, bytes, str]]):
    """A multipart/form-data body. Repeated field names are how the API takes
    its `known_speaker_names[]` arrays."""
    boundary = "----notula" + base64.b16encode(os.urandom(12)).decode("ascii")
    buf = io.BytesIO()
    for name, value in fields:
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                  .encode("utf-8"))
        buf.write(str(value).encode("utf-8"))
        buf.write(b"\r\n")
    for name, filename, data, ctype in files:
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                  f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode("utf-8"))
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode("utf-8"))
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def _error_message(status: int, body: bytes) -> str:
    try:
        msg = json.loads(body.decode("utf-8", "replace"))["error"]["message"]
        if msg:
            return str(msg)
    except (ValueError, KeyError, TypeError):
        pass
    text = body.decode("utf-8", "replace").strip()
    return text[:200] if text else f"HTTP {status}"


def _is_timeout(e: BaseException) -> bool:
    """A read that timed out, however urllib chose to wrap it this time."""
    if isinstance(e, (socket.timeout, TimeoutError)):
        return True
    reason = getattr(e, "reason", None)
    return isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(e).lower()


def _retry_after(headers) -> float:
    try:
        return max(0.0, min(120.0, float((headers or {}).get("Retry-After") or 0)))
    except (TypeError, ValueError):
        return 0.0


def _stream_request(key: str, body: bytes, ctype: str, url: str,
                    on_event: Callable[[dict], None], cancel: threading.Event) -> dict:
    """POST one part with stream=true and feed each server-sent event to
    on_event. Returns the final `transcript.text.done` event.

    The socket timeout is STALL_S, and on a stream it applies to every read, so
    a connection that goes quiet for that long is abandoned rather than waited
    on. Anything worth another try raises _Retryable; a bad key raises
    AuthError; a request OpenAI rejects outright raises CloudError.
    """
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": ctype,
                 "Content-Length": str(len(body)), "Accept": "text/event-stream",
                 "User-Agent": "notula"})
    try:
        resp = urllib.request.urlopen(req, timeout=STALL_S)
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        msg = _error_message(e.code, raw)
        if e.code == 401:
            raise AuthError(f"OpenAI rejected the API key — {msg}")
        if e.code in RETRY_STATUS:
            raise _Retryable(f"OpenAI returned HTTP {e.code}: {msg}", _retry_after(e.headers))
        raise _Refused(f"OpenAI refused the request (HTTP {e.code}) — {msg}")
    except (urllib.error.URLError, OSError) as e:
        if _is_timeout(e):
            raise _Retryable(f"OpenAI sent nothing for {STALL_S // 60} minutes")
        raise _Retryable(f"could not reach OpenAI — {getattr(e, 'reason', None) or e}")

    done = None
    with resp:
        if "text/event-stream" not in (resp.headers.get("Content-Type") or ""):
            # the server answered in one piece after all: treat it as the end
            try:
                data = json.loads(resp.read().decode("utf-8"))
            except (ValueError, OSError) as e:
                raise _Retryable(f"OpenAI sent an unreadable answer — {e}")
            for seg in data.get("segments") or []:
                on_event(dict(seg, type="transcript.text.segment"))
            return dict(data, type="transcript.text.done")
        try:
            for raw in resp:
                if cancel.is_set():
                    raise _Cancelled("cancelled")
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    ev = json.loads(payload)
                except ValueError:
                    raise _Retryable("OpenAI sent an unreadable event")
                kind = ev.get("type") or ""
                if kind == "error" or (not kind and "error" in ev):
                    err = ev.get("error") if isinstance(ev.get("error"), dict) else ev
                    raise _Retryable(f"OpenAI reported an error mid-transcript — "
                                     f"{err.get('message') or kind}")
                on_event(ev)
                if kind == "transcript.text.done":
                    done = ev
        except (socket.timeout, TimeoutError) as e:
            raise _Retryable(f"OpenAI went quiet for {STALL_S // 60} minutes mid-transcript") from e
        except OSError as e:
            raise _Retryable(f"the connection dropped mid-transcript — {e}") from e
    if done is None:
        raise _Retryable("the transcript stream ended before it was finished")
    return done


# ---- saved parts (so a retry never pays twice) ----------------------------------

class _PartCache:
    """Finished parts, saved in the meeting folder as they arrive.

    A manifest pins the model, language and part length the saved parts were
    made with; if any differs, the old parts don't fit this run and are dropped.
    Part files are named by their span in milliseconds, which is also how
    library.describe totals what's already done without reading them.
    """

    def __init__(self, out: Path, model: str, lang: str):
        self.dir = out / CACHE_DIR
        self.meta = {"model": model, "lang": lang, "chunk_s": CHUNK_S}
        try:
            have = json.loads((self.dir / "manifest.json").read_text("utf-8"))
        except (OSError, ValueError):
            have = None
        if have != self.meta:
            self.clear()
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "manifest.json").write_text(json.dumps(self.meta), "utf-8")
        self._lock = threading.Lock()

    @staticmethod
    def name(start: float, end: float) -> str:
        return f"part-{int(round(start * 1000)):012d}-{int(round(end * 1000)):012d}.json"

    def load(self, start: float, end: float) -> Optional[dict]:
        try:
            data = json.loads((self.dir / self.name(start, end)).read_text("utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def save(self, start: float, end: float, result: dict) -> None:
        with self._lock:
            dst = self.dir / self.name(start, end)
            tmp = dst.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(result, ensure_ascii=False), "utf-8")
                os.replace(tmp, dst)
            except OSError:
                pass            # losing the cache costs money on a retry, not the run

    def clear(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def saved_seconds(out_dir, model: str, lang: str) -> float:
    """How much of this meeting is already transcribed and saved for `model` in
    `lang` — what a retry would not send again."""
    info = library.cloud_resume(str(out_dir))
    if not info or info.get("model") != model or info.get("lang") != (lang or "").strip().lower():
        return 0.0
    if info.get("chunk_s") != CHUNK_S:
        return 0.0
    return float(info.get("done_s") or 0.0)


# ---- speakers -----------------------------------------------------------------

class _Speakers:
    """Maps the labels one chunk returns onto stable SPEAKER_NN names.

    Known names (ones we sent a reference clip for) come back verbatim. Anything
    else is a fresh letter local to that chunk, and gets the next free number.
    """

    def __init__(self):
        self.names: list[str] = []          # in order of first appearance
        self.refs: dict[str, tuple[float, float, float]] = {}   # name -> (len, start, end)
        self.talk: dict[str, float] = {}    # name -> seconds spoken
        self._local: dict[str, str] = {}

    def begin_chunk(self):
        self._local = {}

    def resolve(self, raw: str) -> str:
        raw = (raw or "").strip() or "?"
        if raw in self.names:
            return raw
        if raw not in self._local:
            name = f"SPEAKER_{len(self.names):02d}"
            self.names.append(name)
            self._local[raw] = name
        return self._local[raw]

    def note_span(self, name: str, start: float, end: float):
        """Count talk time, and remember each speaker's longest stretch for
        the reference clip."""
        length = end - start
        self.talk[name] = self.talk.get(name, 0.0) + max(0.0, length)
        if length < REF_MIN_S:
            return
        have = self.refs.get(name)
        if have is None or length > have[0]:
            self.refs[name] = (length, start, end)

    def known(self) -> list[str]:
        """Who gets a reference clip: the most talkative speakers with a usable
        stretch, at most four (the API's limit)."""
        ranked = sorted((n for n in self.names if n in self.refs),
                        key=lambda n: -self.talk.get(n, 0.0))
        return ranked[:REF_MAX_SPEAKERS]


# ---- output -------------------------------------------------------------------

def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_ts_ms(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    s, ms = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _clock(seconds: float) -> str:
    s = int(round(max(0.0, seconds)))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def merged_text(segments: list[dict]) -> str:
    """The speaker-labeled transcript, in the same shape diarize_and_merge.py
    writes, so anything reading transcript.merged.txt sees no difference."""
    lines, last = [], None
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        who = seg.get("speaker") or "UNKNOWN"
        if who != last:
            lines.append(f"\n[{_fmt_ts(seg['start'])}] {who}:")
            last = who
        lines.append(text)
    return "\n".join(lines).strip() + "\n"


def whisper_json(segments: list[dict]) -> dict:
    """transcript.json in whisper.cpp's layout (offsets in ms), so tooling built
    on the local pass keeps working on a cloud transcript."""
    items = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        item = {
            "timestamps": {"from": _fmt_ts_ms(seg["start"]), "to": _fmt_ts_ms(seg["end"])},
            "offsets": {"from": int(round(seg["start"] * 1000)),
                        "to": int(round(seg["end"] * 1000))},
            "text": " " + text,
        }
        if seg.get("speaker"):
            item["speaker"] = seg["speaker"]
        items.append(item)
    return {"transcription": items}


# ---- one run --------------------------------------------------------------------

class _Run:
    """The state of one transcription: the parts, who is working on which, and
    the progress label. Workers call in from their own threads, so everything
    that touches shared state goes through `lock`."""

    def __init__(self, wav, spans, model, key, lang, url, emit, cancel, tmp):
        self.wav, self.spans, self.model, self.key = wav, spans, model, key
        self.spec = MODELS[model]
        self.lang, self.url, self.emit, self.cancel, self.tmp = lang, url, emit, cancel, tmp
        self.duration = spans[-1][1] if spans else 0.0
        self.covered = [0.0] * len(spans)
        self.results: list[Optional[dict]] = [None] * len(spans)
        self.refs: list[tuple[str, str]] = []           # (name, data url)
        self.phase = ""
        self.lock = threading.Lock()

    # progress ----------------------------------------------------------------

    def progress(self, i: Optional[int] = None, upto: Optional[float] = None, note: str = ""):
        with self.lock:
            if i is not None and upto is not None:
                span = self.spans[i][1] - self.spans[i][0]
                self.covered[i] = max(self.covered[i], min(span, upto))
            done = sum(self.covered)
            frac = 0.08 + 0.88 * (done / self.duration if self.duration else 0.0)
            if self.phase == "voices":
                span = self.spans[0][1] - self.spans[0][0]
                msg = (f"learning the voices from the first {_clock(span)}… "
                       f"{_clock(self.covered[0])} of {_clock(span)}")
            else:
                par = min(CONCURRENCY, len(self.spans))
                msg = f"transcribing with OpenAI · {_clock(done)} of {_clock(self.duration)}"
                if self.phase == "sequential" and len(self.spans) > 1:
                    msg += " · one part at a time"
                elif par > 1:
                    msg += f" · {par} parts at a time"
            self.emit("transcribe", frac, msg + (f" · {note}" if note else ""))

    # one part ------------------------------------------------------------------

    def fields(self, refs: list[tuple[str, str]]) -> list[tuple[str, str]]:
        f = [("model", self.model),
             ("response_format", "diarized_json" if self.spec["diarize"] else "json"),
             ("stream", "true")]
        if self.lang and self.lang != "auto":
            f.append((self.spec.get("lang_field", "language"), self.lang))
        if self.spec["diarize"]:
            f.append(("chunking_strategy", "auto"))
            for name, url in refs:
                f.append(("known_speaker_names[]", name))
                f.append(("known_speaker_references[]", url))
        return f

    def run_part(self, i: int) -> dict:
        """Send part i until it succeeds or RETRIES run out. Returns the part
        as saved: its raw segments (times relative to the part), its text and
        its usage."""
        start, end = self.spans[i]
        mp3 = Path(self.tmp) / f"part-{i + 1:03d}.mp3"
        data = mp3.read_bytes()
        body, ctype = _multipart(self.fields(self.refs if i else []),
                                 [("file", mp3.name, data, "audio/mpeg")])
        last: Optional[_Retryable] = None
        for attempt in range(RETRIES):
            if self.cancel.is_set():
                raise _Cancelled("cancelled")
            if attempt:
                self.progress(note=f"part {i + 1} retrying ({last})")
                delay = RETRY_DELAY_S * 2 ** (attempt - 1)
                if self.cancel.wait(max(delay, last.retry_after if last else 0.0)):
                    raise _Cancelled("cancelled")
            segs: list[dict] = []
            deltas: list[str] = []

            def on_event(ev, i=i, segs=segs, deltas=deltas):
                kind = ev.get("type")
                if kind == "transcript.text.segment":
                    segs.append({k: ev.get(k) for k in ("speaker", "start", "end", "text")})
                    try:
                        self.progress(i, float(ev.get("end") or 0.0))
                    except (TypeError, ValueError):
                        pass
                elif kind == "transcript.text.delta":
                    deltas.append(ev.get("delta") or "")

            try:
                done = _stream_request(self.key, body, ctype, self.url, on_event, self.cancel)
            except _Retryable as e:
                last = e
                with self.lock:
                    self.covered[i] = 0.0          # that attempt's progress is void
                continue
            result = {"start": start, "end": end, "segments": segs,
                      "text": done.get("text") or "".join(deltas),
                      "usage": done.get("usage")}
            self.progress(i, end - start)
            return result
        raise CloudError(f"part {i + 1} ({_clock(start)}–{_clock(end)}) failed "
                         f"{RETRIES} times — {last}")

    # the voices ----------------------------------------------------------------

    def learn_voices(self, parts: list[dict]) -> None:
        """Reference clips of the most talkative speakers heard in `parts`
        (finished parts, in meeting order), named the way the final transcript
        will name them — resolution is the same in-order pass as the assembly.
        A clip already cut for the same stretch is reused, not re-encoded."""
        sp = _Speakers()
        for part in parts:
            sp.begin_chunk()
            base = part["start"]
            for seg in part.get("segments") or []:
                try:
                    s0 = base + float(seg.get("start") or 0.0)
                    s1 = base + float(seg.get("end") or s0)
                except (TypeError, ValueError):
                    continue
                sp.note_span(sp.resolve(seg.get("speaker")), s0, s1)
        clips = getattr(self, "_clips", {})
        refs = []
        for name in sp.known():
            _, s0, s1 = sp.refs[name]
            if (s0, s1) not in clips:
                clips[(s0, s1)] = _reference_clip(self.wav, s0, s1, Path(self.tmp))
            refs.append((name, clips[(s0, s1)]))
        self._clips = clips
        self.refs = refs


# ---- entry point --------------------------------------------------------------

def transcribe_meeting(wav_path, out_dir, *, model=DEFAULT_MODEL, key: str = "",
                       lang="id", progress_cb: Optional[ProgressCB] = None,
                       on_proc=None, url: str = API_URL,
                       cancel: Optional[threading.Event] = None,
                       parallel: bool = True) -> dict:
    """Transcribe a WAV through OpenAI. Blocks; run on a worker thread.

    Same contract as pipeline.transcribe_meeting — progress_cb(stage, frac, msg)
    with a monotonic fraction, on_proc(popen) for each ffmpeg it spawns — and
    the same output files, plus transcript.cloud.json (every part as OpenAI
    returned it) and a `cost_usd` in the result. progress_cb may be called from
    several threads; the app's on_main hop makes that safe. Setting `cancel`
    stops the run at the next event. `parallel=False` sends a diarizing
    model's parts one at a time, for consistent speakers (module docstring).
    """
    wav = Path(wav_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    mono = _Mono(progress_cb or _noop)

    def emit(stage, frac, msg):
        with lock:
            mono(stage, frac, msg)

    cancel = cancel or threading.Event()
    model = resolve_model(model)
    spec = MODELS[model]
    key = (key or "").strip()
    if not key:
        raise AuthError("no OpenAI API key")
    if not wav.exists():
        raise PipelineError(f"recording not found: {wav}")
    if not os.path.exists(FFMPEG):
        raise CloudError(f"ffmpeg not found at {FFMPEG} — needed to prepare the upload")

    emit("prepare", 0.0, "reading audio…")
    duration = probe_duration(wav)
    if duration <= 0:
        raise CloudError("could not read the recording's length")
    lang = (lang or "").strip().lower()
    spans = plan_chunks(duration, _silences(wav, on_proc) if duration > CHUNK_S else [])
    n = len(spans)
    cache = _PartCache(out, model, lang)

    with tempfile.TemporaryDirectory(prefix="notula-cloud-", dir=str(out)) as tmp:
        run = _Run(wav, spans, model, key, lang, url, emit, cancel, tmp)
        for i, (s0, s1) in enumerate(spans):
            got = cache.load(s0, s1)
            if got is not None:
                run.results[i] = got
                run.covered[i] = s1 - s0
        reused = sum(1 for r in run.results if r is not None)

        todo = [i for i in range(n) if run.results[i] is None]
        for k, i in enumerate(todo):
            if cancel.is_set():
                raise _Cancelled("cancelled")
            emit("encode", 0.02 + 0.06 * k / max(1, len(todo)),
                 f"preparing audio… {k + 1} of {len(todo)}"
                 + (f" ({reused} of {n} parts already done)" if reused else ""))
            s0, s1 = spans[i]
            mp3 = Path(tmp) / f"part-{i + 1:03d}.mp3"
            _encode_chunk(wav, s0, s1, mp3, on_proc)
            if mp3.stat().st_size > UPLOAD_LIMIT:
                raise CloudError(f"part {i + 1} is {mp3.stat().st_size >> 20} MB, over "
                                 f"the API's 25 MB limit")

        parallel = runs_parallel(model, parallel)

        # one at a time: each part carries the voices of every part before it
        if not parallel:
            run.phase = "sequential"
            run.progress()
            for i in range(n):
                if run.results[i] is not None:
                    continue
                run.learn_voices(run.results[:i])
                try:
                    run.results[i] = run.run_part(i)
                except (AuthError, _Refused, _Cancelled):
                    raise
                except CloudError as e:
                    done = sum(1 for r in run.results if r is not None)
                    raise CloudError(
                        f"{e}. The {done} finished parts are saved — transcribe again "
                        f"and only the missing ones are sent.") from e
                cache.save(*spans[i], run.results[i])

        # the voices: part one alone, so every other part can be told who's who
        if parallel and spec["diarize"] and n > 1:
            if run.results[0] is None:
                run.phase = "voices"
                run.progress()
                run.results[0] = run.run_part(0)
                cache.save(*spans[0], run.results[0])
            run.learn_voices(run.results[:1])
        if parallel:
            run.phase = "rest"
            run.progress()

        # the rest, CONCURRENCY at a time, on daemon threads so a quit mid-run
        # never waits on a socket
        work: queue.Queue = queue.Queue()
        for i in range(n):
            if run.results[i] is None:
                work.put(i)
        failures: list[tuple[int, CloudError]] = []
        fatal: list[BaseException] = []

        def worker():
            while not cancel.is_set():
                try:
                    i = work.get_nowait()
                except queue.Empty:
                    return
                try:
                    res = run.run_part(i)
                    run.results[i] = res
                    cache.save(*spans[i], res)
                except _Cancelled:
                    return
                except (AuthError, _Refused) as e:
                    # a bad key or a refused request fails every part the same
                    # way, so stop the others instead of paying for them
                    fatal.append(e)
                    cancel.set()
                    return
                except CloudError as e:
                    failures.append((i, e))
                except Exception as e:                     # pragma: no cover
                    failures.append((i, CloudError(f"part {i + 1}: {e}")))

        threads = [threading.Thread(target=worker, daemon=True, name=f"notula-cloud-{k}")
                   for k in range(min(CONCURRENCY, max(1, work.qsize())))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if fatal:
            raise fatal[0] if isinstance(fatal[0], CloudError) else CloudError(str(fatal[0]))
        if cancel.is_set():
            raise _Cancelled("cancelled")
        if failures:
            failures.sort()
            done = sum(1 for r in run.results if r is not None)
            raise CloudError(
                f"{len(failures)} of {n} parts failed ({failures[0][1]}). The other {done} "
                f"are saved — transcribe again and only the missing ones are sent.")

    # assemble, in order, with speaker names stable across parts
    emit("write", 0.96, "writing transcript…")
    speakers = _Speakers()
    segments: list[dict] = []
    cost, exact = 0.0, True
    for part in run.results:
        start, end = part["start"], part["end"]
        c, ok = usage_cost(model, part.get("usage"), end - start)
        cost += c
        exact = exact and ok
        speakers.begin_chunk()
        got = part.get("segments") or []
        if got:
            for seg in got:
                try:
                    s0 = start + float(seg.get("start") or 0.0)
                    s1 = start + float(seg.get("end") or s0)
                except (TypeError, ValueError):
                    continue
                item = {"start": s0, "end": max(s0, s1), "text": seg.get("text") or ""}
                if spec["diarize"]:
                    item["speaker"] = speakers.resolve(seg.get("speaker"))
                segments.append(item)
        else:
            # no segments (the plain model): one block for the part
            segments.append({"start": start, "end": end, "text": part.get("text") or ""})

    if not any((s.get("text") or "").strip() for s in segments):
        raise CloudError("OpenAI returned an empty transcript")

    diarized = spec["diarize"] and any(s.get("speaker") for s in segments)
    txt_path = out / "transcript.txt"
    json_path = out / "transcript.json"
    cloud_path = out / "transcript.cloud.json"
    merged = out / "transcript.merged.txt"
    output = out / "output.txt"

    plain = "\n".join((s.get("text") or "").strip() for s in segments
                      if (s.get("text") or "").strip())
    txt_path.write_text(plain + "\n", "utf-8")
    json_path.write_text(json.dumps(whisper_json(segments), indent=2, ensure_ascii=False),
                         "utf-8")
    cloud_path.write_text(json.dumps({"model": model, "language": lang, "chunks": run.results},
                                     indent=2, ensure_ascii=False), "utf-8")
    if diarized:
        merged.write_text(merged_text(segments), "utf-8")
    elif merged.exists():
        merged.unlink()       # a stale one from an earlier local pass would mislead

    cost_line = (f"# Engine: OpenAI cloud   Cost: {fmt_usd(cost)}"
                 f"{'' if exact else ' (estimated)'}   Parts: {n}")
    pipeline._write_output(
        output, txt_path, merged if diarized else None,
        meeting=out.name, lang=lang or "auto", model=f"{model} (OpenAI)",
        duration=duration, min_speakers=None, max_speakers=None,
        diar_ok=diarized, warn=None,
        extra=[cost_line])
    cache.clear()             # everything is in transcript.cloud.json now
    emit("done", 1.0, "complete" if diarized else "complete (transcript only)")
    return {
        "wav": wav, "json": json_path, "txt": txt_path,
        "merged": merged if diarized else None, "output": output,
        "duration": duration, "diarized": diarized,
        "warning": None if spec["diarize"] else "no speaker labels with this model",
        "engine": "cloud", "model": model, "chunks": n, "reused": reused,
        "parallel": parallel,
        "cost_usd": cost, "cost_estimated": not exact,
        "speakers": len(speakers.names),
    }
