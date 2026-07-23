"""
notula.recorder — real-time capture with per-source level metering and mute.

Recording is done in-process with sounddevice (PortAudio) rather than handing a
device to ffmpeg, so the app can (a) show a live level meter per source and
(b) mute a source on the fly. Each source captures 16 kHz mono float32 and is
written to its own WAV via the stdlib `wave` module; on stop, multiple sources
are mixed down to a single whisper-ready `audio.wav` with ffmpeg's amix.

Two source kinds:
  * "mic"     — a normal input device (your microphone)
  * "system"  — a loopback device (e.g. BlackHole) that carries computer audio.
                macOS can't capture system output without such a driver, so this
                source is only available if a loopback device is present.
"""

from __future__ import annotations

import math
import os
import queue
import shutil
import subprocess
import threading
import wave

import numpy as np
import sounddevice as sd

SR = 16000
BLOCK = 1024
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

# input devices that are really loopbacks / aggregates carrying system audio
_LOOPBACK_HINTS = ("blackhole", "loopback", "aggregate", "soundflower",
                   "vb-cable", "vb cable", "ishowu", "multi-output", "existential",
                   "ladiocast", "groundcontrol")


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


def _rms_to_level(rms: float) -> float:
    """Map an RMS amplitude to a 0..1 meter value on a dBFS scale (-60..0 dB)."""
    if rms <= 1e-7:
        return 0.0
    db = 20.0 * math.log10(rms)
    return max(0.0, min(1.0, (db + 60.0) / 60.0))


# ---- one capture source ------------------------------------------------------

class Source:
    def __init__(self, kind: str, device_index: int, wav_path: str, write: bool = True):
        self.kind = kind
        self.device_index = int(device_index)
        self.wav_path = wav_path
        self.muted = False
        self.level = 0.0        # smoothed 0..1 for the meter
        self.frames = 0         # samples seen (drives elapsed)
        self._write = write     # False = monitor only (levels, no file)
        self._q: queue.Queue = queue.Queue(maxsize=64)
        self._stream = None
        self._writer = None
        self._wav = None
        self._run = False
        self.error = None

    # audio-thread callback: keep it light — meter + hand frames to the writer
    def _callback(self, indata, n, t, status):
        mono = indata[:, 0]
        rms = float(np.sqrt(np.mean(mono * mono))) if len(mono) else 0.0
        lvl = _rms_to_level(rms)
        # fast attack, slow release — reads like a VU meter
        self.level = lvl if lvl > self.level else self.level * 0.82 + lvl * 0.18
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

    def start(self):
        if self._write:
            self._wav = wave.open(self.wav_path, "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(SR)
        self._run = True
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()
        self._stream = sd.InputStream(
            device=self.device_index, samplerate=SR, channels=1,
            dtype="float32", blocksize=BLOCK, callback=self._callback)
        self._stream.start()

    @property
    def active(self) -> bool:
        try:
            return self._stream is not None and self._stream.active
        except Exception:
            return False

    def stop(self):
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception as e:
            self.error = str(e)
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
        {'kind':'system','sck':True} (computer audio via ScreenCaptureKit).
        write=False is monitor mode: live levels only, nothing written to disk."""
        self.folder = folder
        self.sources = []
        for s in specs:
            wav = os.path.join(folder, f"src_{s['kind']}.wav")
            if s.get("sck"):
                import sysaudio
                self.sources.append(sysaudio.SCKSystemAudioSource(wav, write=write))
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
            subprocess.run(cmd, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            shutil.copyfile(wavs[0], final)   # fall back to the first source
        return final
