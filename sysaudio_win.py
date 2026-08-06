"""
notula.sysaudio_win — capture computer/system audio via WASAPI loopback.

The Windows counterpart to the ScreenCaptureKit source, and the reason no
loopback *driver* (VB-Cable, Voicemeeter) is needed here either: WASAPI can open
a render endpoint for capture, handing back exactly what is being played. Chrome,
Zoom, Teams and everything else land in one stream.

Two things differ from the macOS source, and both are handled here so the rest of
the engine can stay ignorant of them:

  * **Format.** ScreenCaptureKit is *told* to produce 16 kHz mono. WASAPI
    loopback has no such option — it only ever produces the endpoint's own mix
    format, in practice 48 kHz stereo float32 — so every block is downmixed and
    resampled on the way in (see dsp.py).

  * **Silence.** A render endpoint that nothing is playing through can stop
    delivering buffers altogether rather than delivering zeros. Written naively,
    a meeting where nobody spoke for thirty seconds would come out with those
    thirty seconds simply missing, and everything after them shifted earlier —
    which, mixed against the microphone track, desynchronizes the whole
    recording. So the source fills detected gaps with real silence; see
    `_fill_gap`.

PyAudioWPatch supplies the loopback-capable PortAudio build (plain PyAudio and
sounddevice's PortAudio do not expose the WASAPI loopback flag).
"""

from __future__ import annotations

import threading
import time
import wave

import numpy as np

import dsp

SR = 16000
BLOCK = 1024

# How long the endpoint must deliver *nothing* before we call it a stall and
# splice in silence. This is measured as time since the previous callback, not as
# a running deficit against wall-clock, and the difference matters: a deficit
# also accumulates the ordinary drift between the audio clock and the system
# clock (~100 ppm, a third of a second per hour), so any fixed deficit threshold
# is eventually crossed by a recording that is working perfectly — splicing half
# a second of silence into the middle of a two-hour meeting and shifting
# everything after it out of step with the microphone. Time since the last
# callback has no such accumulation, so the threshold means the same thing in
# minute one and hour three.
GAP_FILL_S = 0.5

# Silence is written from a preallocated one-second buffer, in slices. A stall of
# unknown length must not turn into an allocation of unknown size on the audio
# callback thread: twenty idle minutes would otherwise be a 77 MB float32 array,
# plus two 38 MB int16 copies, built inside the capture callback.
SILENCE_CHUNK_S = 1.0

# The live transcriber keeps only a short tail, so handing it the whole gap would
# just be a large concatenate it immediately discards.
MAX_TAP_S = 15.0

AVAILABLE = True
try:
    import pyaudiowpatch as pyaudio
except Exception:                                    # pragma: no cover
    AVAILABLE = False


def _default_loopback(pa):
    """The loopback endpoint mirroring the current default speakers, or None."""
    try:
        info = pa.get_default_wasapi_loopback()
        if info:
            return info
    except Exception:
        pass
    # Older PyAudioWPatch: match the default output device to its loopback twin
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        spk = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if spk.get("isLoopbackDevice"):
            return spk
        for lb in pa.get_loopback_device_info_generator():
            if spk["name"] in lb["name"]:
                return lb
    except Exception:
        pass
    return None


class WasapiSystemAudioSource:
    """A computer-audio capture source backed by WASAPI loopback.

    Mirrors recorder.Source / sysaudio_mac.SCKSystemAudioSource (start/stop/
    level/muted/paused/frames/active + writes a WAV) so RecordingEngine can treat
    it like any other source.
    """

    kind = "system"

    def __init__(self, wav_path: str, write: bool = True):
        self.wav_path = wav_path
        self.muted = False
        self.tap = None          # fn(kind, block) — feeds live transcription
        self.level = 0.0
        self.frames = 0
        self.error = None
        self._write = write
        self._wav = None
        self._pa = None
        self._stream = None
        self._convert = None
        self._channels = 2
        self._alive = False
        self._lock = threading.Lock()          # guards the WAV; held by the callback
        self._stoplock = threading.Lock()      # guards teardown; never held by it
        self._paused = False
        self._last_cb = None                   # monotonic time of the last buffer
        self._block_s = BLOCK / float(SR)      # nominal gap between callbacks
        self._sil_pcm = b""                    # preallocated silence, see _fill_gap
        self._sil_f32 = None

    # ---- pause is a property here: resuming has to re-arm the stall clock, and
    # ---- the source can only know that if it's told the moment the state flips.

    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        value = bool(value)
        if value == self._paused:
            return
        self._paused = value
        if not value:
            # A stretch we spent paused is not a stall — nothing was supposed to
            # be recorded — so start counting again from here.
            self._last_cb = time.monotonic()

    # ---- audio-thread callback (PortAudio) ----

    def _fill_gap(self) -> int:
        """Write silence for a stretch where the endpoint delivered nothing.

        Returns how many frames were written. Runs under self._lock on the audio
        callback thread, so it writes from a preallocated buffer in one-second
        slices: the length of a stall is not bounded by anything, and neither
        allocating nor zero-filling it is work this thread can afford.
        """
        last = self._last_cb
        if last is None:
            return 0
        stalled = time.monotonic() - last - self._block_s
        if stalled < GAP_FILL_S:
            return 0
        n = int(stalled * SR)
        if self._wav is not None:
            chunk = int(SILENCE_CHUNK_S * SR)
            left = n
            while left > 0:
                take = min(left, chunk)
                self._wav.writeframes(self._sil_pcm[: take * 2])   # 2 bytes/frame
                left -= take
        self.frames += n
        return n

    def _callback(self, in_data, frame_count, time_info, status):
        try:
            raw = np.frombuffer(in_data, dtype="<f4")
            if self._channels > 1:
                raw = raw.reshape(-1, self._channels)
            mono = self._convert(raw) if self._convert is not None else raw
            if len(mono):
                rms = float(np.sqrt(np.mean(mono * mono)))
                lvl = dsp.rms_to_level(rms)
                self.level = lvl if lvl > self.level else self.level * 0.82 + lvl * 0.18
            if self._paused or not len(mono):
                # A delivered buffer proves the endpoint is alive even when we
                # drop it, so the stall clock is re-armed either way.
                self._last_cb = time.monotonic()
                return (None, pyaudio.paContinue)   # paused time never lands on disk
            block = np.zeros_like(mono) if self.muted else mono
            padded = 0
            with self._lock:
                padded = self._fill_gap()
                if self._wav is not None:
                    pcm = np.clip(block * 32767.0, -32768, 32767).astype("<i2")
                    self._wav.writeframes(pcm.tobytes())
            self._last_cb = time.monotonic()
            self.frames += len(block)
            tap = self.tap                # live transcription, best-effort, no lock
            if tap is not None:
                try:
                    if padded:
                        tap(self.kind, self._sil_f32[: min(padded, int(MAX_TAP_S * SR))])
                    tap(self.kind, block)
                except Exception:
                    pass
        except Exception as e:                       # never let the audio thread die
            self.error = repr(e)
        return (None, pyaudio.paContinue)

    # ---- lifecycle ----

    def start(self):
        if not AVAILABLE:
            raise RuntimeError("pyaudiowpatch is not installed — computer audio "
                               "needs it for WASAPI loopback capture")
        # Beyond this point a failure is reported, not raised: computer audio is
        # the secondary source, and losing it must never take the microphone (and
        # with it the whole meeting) down. Same contract as the macOS source.
        try:
            self._pa = pyaudio.PyAudio()
            dev = _default_loopback(self._pa)
            if dev is None:
                raise RuntimeError("no WASAPI loopback device — is an audio "
                                   "output device present and enabled?")
            self._channels = max(1, int(dev.get("maxInputChannels") or 2))
            rate = int(dev.get("defaultSampleRate") or 48000)
            self._convert = dsp.make_converter(rate, self._channels, SR)
            self._block_s = BLOCK / float(rate)
            chunk = int(SILENCE_CHUNK_S * SR)
            self._sil_pcm = np.zeros(chunk, dtype="<i2").tobytes()
            self._sil_f32 = np.zeros(int(MAX_TAP_S * SR), dtype="float32")
            if self._write:
                self._wav = wave.open(self.wav_path, "wb")
                self._wav.setnchannels(1)
                self._wav.setsampwidth(2)
                self._wav.setframerate(SR)
            self._stream = self._pa.open(
                format=pyaudio.paFloat32, channels=self._channels, rate=rate,
                input=True, input_device_index=int(dev["index"]),
                frames_per_buffer=BLOCK, stream_callback=self._callback, start=False)
            self._alive = True
            # Arm the stall clock as late as possible: opening the stream takes
            # time, and that is startup latency, not a gap in the recording.
            self._last_cb = time.monotonic()
            self._stream.start_stream()
        except Exception as e:
            self.error = str(e)
            self._alive = False
            self._teardown()

    @property
    def active(self) -> bool:
        if not self._alive:
            return False
        try:
            return self._stream is not None and self._stream.is_active()
        except Exception:
            return False

    def _teardown(self):
        """Close the stream exactly once, even if two threads ask at the same time.

        AppCore can reach here twice: `_stop_recording` finalizes on a worker
        thread while `teardown()` (window closing) still sees `rec.running` and
        stops it again. PyAudio's Stream.close() has no guard of its own, and a
        second Pa_CloseStream on a freed handle is a native crash rather than a
        Python exception — so the handles are taken out of the object under a
        lock first, and only the winner touches them.
        """
        with self._stoplock:
            stream, self._stream = self._stream, None
            pa, self._pa = self._pa, None
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass

    def stop(self):
        self._alive = False
        self._teardown()
        with self._lock:
            try:
                if self._wav is not None:
                    # drain the resampler's tail so the track is exactly as long
                    # as the capture was
                    if self._convert is not None:
                        tail = self._convert.flush()
                        if len(tail):
                            pcm = np.clip(tail * 32767.0, -32768, 32767).astype("<i2")
                            self._wav.writeframes(pcm.tobytes())
                            self.frames += len(tail)
                    self._wav.close()
            except Exception:
                pass
            self._wav = None
