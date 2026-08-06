"""
notula.recorder — real-time capture with per-source level metering and mute.

Recording is done in-process with sounddevice (PortAudio) rather than handing a
device to ffmpeg, so the app can (a) show a live level meter per source and
(b) mute a source on the fly. Each source captures 16 kHz mono float32 and is
written to its own WAV via the stdlib `wave` module; on stop, multiple sources
are mixed down to a single whisper-ready `audio.wav` with ffmpeg's amix.

Two source kinds:
  * "mic"     — a normal input device (your microphone), captured here
  * "system"  — computer audio, captured by the platform backend in sysaudio.py
                (ScreenCaptureKit on macOS, WASAPI loopback on Windows). No
                loopback driver is involved on either platform.

A microphone doesn't always give us the 16 kHz mono we want: CoreAudio will
resample to anything, but WASAPI shared mode is pinned to whatever the endpoint
is configured for. So a source that can't be opened at 16 kHz is opened at its
own rate and converted on the way in (see dsp.py).
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import wave

import numpy as np

import dsp
import osutil
import toolpaths

# Must run before sounddevice is imported: it resolves its PortAudio DLL at
# import time, and on Windows-on-ARM it picks a name its own wheel doesn't ship.
osutil.ensure_portaudio()

import sounddevice as sd            # noqa: E402  (see above)

SR = 16000
BLOCK = 1024
FFMPEG = toolpaths.FFMPEG

# Input devices that are really loopbacks / aggregates carrying system audio.
# They're excluded from microphone auto-selection: picking "Stereo Mix" as the
# mic records the meeting's own output back into itself.
_LOOPBACK_HINTS = ("blackhole", "loopback", "aggregate", "soundflower",
                   "vb-cable", "vb cable", "ishowu", "multi-output", "existential",
                   "ladiocast", "groundcontrol",
                   # Windows
                   "stereo mix", "what u hear", "wave out", "voicemeeter",
                   "cable output", "virtual audio")


# ---- device discovery --------------------------------------------------------

def _is_loopback(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in _LOOPBACK_HINTS)


def list_input_devices() -> list[dict]:
    """Every input-capable device: [{index, name, channels, loopback}]."""
    out = []
    try:
        devices = sd.query_devices()
    except Exception:
        return out
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            out.append({
                "index": i,
                "name": d["name"],
                "channels": d["max_input_channels"],
                "loopback": _is_loopback(d["name"]),
            })
    return out


def default_mic_index():
    try:
        idx = sd.default.device[0]
        if idx is not None and idx >= 0:
            return int(idx)
    except Exception:
        pass
    devs = [d for d in list_input_devices() if not d["loopback"]]
    return devs[0]["index"] if devs else None


def detect_loopback_index():
    for d in list_input_devices():
        if d["loopback"]:
            return d["index"]
    return None


_rms_to_level = dsp.rms_to_level


# ---- one capture source ------------------------------------------------------

class Source:
    def __init__(self, kind: str, device_index: int, wav_path: str, write: bool = True):
        self.kind = kind
        self.device_index = int(device_index)
        self.wav_path = wav_path
        self.muted = False
        self.paused = False     # dropped at the callback — never reaches the WAV
        self.level = 0.0        # smoothed 0..1 for the meter
        self.frames = 0         # samples seen (drives elapsed)
        self._write = write     # False = monitor only (levels, no file)
        self.tap = None         # optional fn(kind, block) — feeds live transcription
        self._q: queue.Queue = queue.Queue(maxsize=64)
        self._stream = None
        self._writer = None
        self._wav = None
        self._run = False
        self._convert = None    # set when the device can't give us 16 kHz mono
        self._stoplock = threading.Lock()
        self.error = None

    # audio-thread callback: keep it light — meter + hand frames to the writer
    def _callback(self, indata, n, t, status):
        # the None case is the common one (a device opened at 16 kHz mono) and
        # costs nothing; otherwise this downmixes and resamples in place
        mono = self._convert(indata) if self._convert is not None else indata[:, 0]
        if not len(mono):
            return          # the resampler is still accumulating a whole output
        rms = float(np.sqrt(np.mean(mono * mono)))
        lvl = _rms_to_level(rms)
        # fast attack, slow release — reads like a VU meter
        self.level = lvl if lvl > self.level else self.level * 0.82 + lvl * 0.18
        # Paused: keep metering (the device is still open, and seeing the meter
        # move tells you it's alive) but drop the audio. Unlike Mute — which
        # writes silence and keeps the timeline — paused time is simply absent
        # from the recording, so `frames`/elapsed freeze with it.
        if self.paused:
            return
        block = np.zeros_like(mono) if self.muted else mono
        try:
            self._q.put_nowait(block.copy())
        except queue.Full:
            pass    # drop a block rather than stall the audio thread

    def _write_loop(self):
        while self._run or not self._q.empty():
            try:
                block = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._wav is not None:
                pcm = np.clip(block * 32767.0, -32768, 32767).astype("<i2")
                self._wav.writeframes(pcm.tobytes())
            self.frames += len(block)
            # Live transcription taps here, on the writer thread, deliberately —
            # never on the audio callback, where a slow consumer would cost frames.
            # Paused blocks never arrive (dropped upstream) and muted ones arrive
            # as silence, so the live tier inherits both semantics for free.
            tap = self.tap
            if tap is not None:
                try:
                    tap(self.kind, block)
                except Exception:
                    pass

    def _open_stream(self):
        """Open the device, preferring 16 kHz mono and falling back if refused.

        PortAudio over CoreAudio converts to whatever you ask for, so on macOS
        the first attempt always wins and nothing else here ever runs. WASAPI
        shared mode does not: it hands over the endpoint's own format or fails,
        and some endpoints won't do mono either. Each fallback records what it
        actually got, so the callback knows what to convert from.
        """
        try:
            info = sd.query_devices(self.device_index)
        except Exception:
            info = {}
        dev_sr = int(info.get("default_samplerate") or 0) or 48000
        dev_ch = max(1, int(info.get("max_input_channels") or 1))

        attempts = [(SR, 1)]
        if dev_sr != SR:
            attempts.append((dev_sr, 1))
        if dev_ch > 1:
            attempts.append((dev_sr, dev_ch))

        last = None
        for rate, ch in attempts:
            stream = None
            try:
                stream = sd.InputStream(
                    device=self.device_index, samplerate=rate, channels=ch,
                    dtype="float32", blocksize=BLOCK, callback=self._callback)
                # build the converter before starting: the first callback can
                # arrive the instant the stream does
                self._convert = dsp.make_converter(rate, ch, SR)
                stream.start()
                return stream
            except Exception as e:
                last = e
                self._convert = None
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
        raise last if last is not None else RuntimeError("could not open input device")

    def start(self):
        if self._write:
            self._wav = wave.open(self.wav_path, "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(SR)
        self._run = True
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()
        self._stream = self._open_stream()

    @property
    def active(self) -> bool:
        try:
            return self._stream is not None and self._stream.active
        except Exception:
            return False

    def stop(self):
        # Take the stream out of the object before closing it. Two threads can
        # reach here at once — a recording being finalized on a worker while the
        # window closing calls teardown — and closing an already-closed PortAudio
        # stream is a native crash, not a catchable exception.
        with self._stoplock:
            stream, self._stream = self._stream, None
        try:
            if stream is not None:
                stream.stop()
                stream.close()
        except Exception as e:
            self.error = str(e)
        # Drain the resampler's tail before the writer is told to finish, so the
        # track is exactly as long as the capture was. Queued first: the writer
        # exits as soon as it sees a stopped run flag and an empty queue.
        if self._convert is not None:
            try:
                tail = self._convert.flush()
                if len(tail):
                    self._q.put_nowait(tail)
            except Exception:
                pass
        self._run = False
        if self._writer is not None:
            self._writer.join(timeout=3)
        try:
            if self._wav is not None:
                self._wav.close()
        except Exception:
            pass


# ---- the recording engine (one or more sources) ------------------------------

class RecordingEngine:
    def __init__(self, specs: list[dict], folder: str, write: bool = True):
        """specs: a list of sources, order preserved. Each is either
        {'kind':'mic','index':int} (a PortAudio device) or
        {'kind':'system','system':True} (computer audio via the platform backend).
        write=False is monitor mode: live levels only, nothing written to disk."""
        self.folder = folder
        self.sources = []
        for s in specs:
            wav = os.path.join(folder, f"src_{s['kind']}.wav")
            if s.get("system") or s.get("sck"):     # 'sck' kept for older callers
                import sysaudio
                self.sources.append(sysaudio.SystemAudioSource(wav, write=write))
            else:
                self.sources.append(Source(s["kind"], s["index"], wav, write=write))
        self._started = False
        self.error = None

    def start(self):
        try:
            for src in self.sources:
                src.start()
            self._started = True
        except Exception as e:
            self.error = str(e)
            self.stop_streams()
            raise

    def wait_until_started(self, timeout: float = 6.0) -> bool:
        import time
        end = time.time() + timeout
        while time.time() < end:
            if any(src.frames > 0 for src in self.sources):
                return True
            time.sleep(0.05)
        return any(src.frames > 0 for src in self.sources)

    @property
    def running(self) -> bool:
        # Alive as long as ANY source is still capturing. A secondary source
        # dying (e.g. computer audio without Screen Recording permission) must
        # not tear down the whole recording; only all sources ending does.
        if not self._started:
            return False
        return any(src.active for src in self.sources) if self.sources else False

    @property
    def elapsed(self) -> float:
        if not self.sources:
            return 0.0
        return max(src.frames for src in self.sources) / float(SR)

    def levels(self) -> dict:
        return {src.kind: {"level": round(src.level, 4), "muted": src.muted}
                for src in self.sources}

    def set_mute(self, kind: str, muted: bool):
        for src in self.sources:
            if src.kind == kind:
                src.muted = bool(muted)

    @property
    def paused(self) -> bool:
        return bool(self.sources) and all(src.paused for src in self.sources)

    def set_paused(self, paused: bool) -> None:
        """Pause/resume every source at once.

        Paused audio is dropped in the capture callback, so it never lands on
        disk — the recording holds only the time you were actually recording,
        and the elapsed clock (driven by frames written) freezes with it. The
        streams stay open: re-acquiring a mic or restarting a ScreenCaptureKit
        stream mid-meeting can fail (device seized, permission re-check), and a
        pause that can't resume is worse than one that keeps the device warm.
        """
        paused = bool(paused)
        for src in self.sources:
            src.paused = paused

    def relocate(self, new_folder: str) -> None:
        """Point the engine at a folder that was renamed underneath it.

        Renaming a directory moves its inode, so the capture threads keep
        writing through their already-open handles without noticing. Only the
        paths we resolve *later* — the per-source WAVs we mix at stop() and the
        final audio.wav — have to be rewritten to the new location.
        """
        self.folder = new_folder
        for src in self.sources:
            src.wav_path = os.path.join(new_folder, os.path.basename(src.wav_path))

    def set_tap(self, fn) -> None:
        """Route every written block to `fn(kind, block)` (or None to detach).
        Used by the live transcriber; safe to attach/detach mid-recording."""
        for src in self.sources:
            src.tap = fn

    def get_source(self, kind: str):
        for src in self.sources:
            if src.kind == kind:
                return src
        return None

    def stop_streams(self):
        for src in self.sources:
            src.stop()
        self._started = False

    def stop(self) -> str:
        """Stop capture and produce the final mono 16 kHz audio.wav. Returns its path."""
        self.stop_streams()
        final = os.path.join(self.folder, "audio.wav")
        # only mix sources that actually captured something
        wavs = [s.wav_path for s in self.sources
                if os.path.exists(s.wav_path) and s.frames > 0]
        if not wavs:
            wavs = [s.wav_path for s in self.sources if os.path.exists(s.wav_path)]
        if not wavs:
            return final
        if len(wavs) == 1:
            shutil.copyfile(wavs[0], final)
            return final
        # mix multiple sources; amix keeps the longest, normalize=0 preserves level
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
        for w in wavs:
            cmd += ["-i", w]
        cmd += ["-filter_complex", f"amix=inputs={len(wavs)}:normalize=0:duration=longest",
                "-ar", str(SR), "-ac", "1", final]
        try:
            subprocess.run(cmd, check=True, capture_output=True,
                           **osutil.popen_kwargs(new_group=False))
        except (OSError, subprocess.CalledProcessError):
            shutil.copyfile(wavs[0], final)   # fall back to the first source
        return final
