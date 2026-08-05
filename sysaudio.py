"""
notula.sysaudio — capture computer/system audio via ScreenCaptureKit.

This is how OBS records "desktop audio" on macOS 13+ without any loopback driver
(BlackHole etc.): ScreenCaptureKit taps the system audio mix directly. It needs
the Screen Recording (Screen & System Audio) privacy permission.

SCK delivers audio as 16 kHz mono Float32 when configured that way (verified), so
no resampling is needed. SCKSystemAudioSource mirrors recorder.Source's interface
(start/stop/level/muted/frames/active + writes a WAV) so the RecordingEngine can
treat it like any other source and mix it into the meeting on stop.
"""

from __future__ import annotations

import math
import threading
import wave

import numpy as np

SR = 16000
AVAILABLE = True
try:
    import objc
    from Foundation import NSObject
    from ScreenCaptureKit import (
        SCShareableContent, SCStream, SCStreamConfiguration,
        SCContentFilter, SCStreamOutputTypeAudio,
    )
    from CoreMedia import (
        CMSampleBufferGetDataBuffer, CMBlockBufferGetDataLength,
        CMBlockBufferCopyDataBytes,
    )
    import libdispatch
except Exception:                                    # pragma: no cover
    AVAILABLE = False


def _rms_to_level(rms: float) -> float:
    if rms <= 1e-7:
        return 0.0
    db = 20.0 * math.log10(rms)
    return max(0.0, min(1.0, (db + 60.0) / 60.0))


if AVAILABLE:

    class _AudioOutput(NSObject):
        def initWithSource_(self, src):
            self = objc.super(_AudioOutput, self).init()
            if self is None:
                return None
            self._src = src
            return self

        # SCStreamOutput protocol
        def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, otype):
            if otype == SCStreamOutputTypeAudio:
                self._src._on_audio(sbuf)

    class _StreamDelegate(NSObject):
        def initWithSource_(self, src):
            self = objc.super(_StreamDelegate, self).init()
            if self is None:
                return None
            self._src = src
            return self

        # SCStreamDelegate protocol
        def stream_didStopWithError_(self, stream, err):
            self._src._on_stopped(err)


class SCKSystemAudioSource:
    """A computer-audio capture source backed by ScreenCaptureKit."""

    kind = "system"

    def __init__(self, wav_path: str, write: bool = True):
        self.wav_path = wav_path
        self.muted = False
        self.paused = False     # see recorder.Source: metered, but not written
        self.tap = None         # fn(kind, block) — feeds live transcription
        self.level = 0.0
        self.frames = 0
        self._write = write
        self.error = None
        self._wav = None
        self._stream = None
        self._out = None
        self._delegate = None
        self._queue = None
        self._alive = False
        self._lock = threading.Lock()

    # ---- audio-thread callback (SCK dispatch queue) ----

    def _on_audio(self, sbuf):
        try:
            bb = CMSampleBufferGetDataBuffer(sbuf)
            if bb is None:
                return
            n = CMBlockBufferGetDataLength(bb)
            res = CMBlockBufferCopyDataBytes(bb, 0, n, None)
            data = res[1] if isinstance(res, tuple) else res
            mono = np.frombuffer(bytes(data), dtype="<f4")
            if not len(mono):
                return
            rms = float(np.sqrt(np.mean(mono * mono)))
            lvl = _rms_to_level(rms)
            self.level = lvl if lvl > self.level else self.level * 0.82 + lvl * 0.18
            if self.paused:
                return          # dropped, so paused time is absent from the WAV
            block = np.zeros_like(mono) if self.muted else mono
            with self._lock:
                if self._wav is not None:
                    pcm = np.clip(block * 32767.0, -32768, 32767).astype("<i2")
                    self._wav.writeframes(pcm.tobytes())
            self.frames += len(mono)
            tap = self.tap                           # live transcription, best-effort
            if tap is not None:
                try:
                    tap(self.kind, block)
                except Exception:
                    pass
        except Exception as e:                       # never let the audio thread die
            self.error = repr(e)

    def _on_stopped(self, err):
        self._alive = False
        if err is not None:
            self.error = str(err)

    # ---- lifecycle (async, non-blocking so it never deadlocks the main loop) ----

    def start(self):
        if not AVAILABLE:
            raise RuntimeError("ScreenCaptureKit not available on this system")
        if self._write:
            self._wav = wave.open(self.wav_path, "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(SR)
        self._queue = libdispatch.dispatch_queue_create(b"id.val.notula.sck.audio", None)
        self._out = _AudioOutput.alloc().initWithSource_(self)
        self._delegate = _StreamDelegate.alloc().initWithSource_(self)
        self._alive = True    # optimistic: flips off only on error/stop
        SCShareableContent.getShareableContentWithCompletionHandler_(self._on_content)

    def _on_content(self, content, error):
        if error is not None or content is None or not content.displays():
            self.error = "Screen Recording permission needed for computer audio"
            self._alive = False
            return
        try:
            filt = SCContentFilter.alloc().initWithDisplay_excludingWindows_(
                content.displays()[0], [])
            cfg = SCStreamConfiguration.alloc().init()
            cfg.setCapturesAudio_(True)
            cfg.setExcludesCurrentProcessAudio_(True)
            cfg.setSampleRate_(SR)
            cfg.setChannelCount_(1)
            cfg.setWidth_(2)          # audio-only: keep the (unused) video tiny
            cfg.setHeight_(2)
            self._stream = SCStream.alloc().initWithFilter_configuration_delegate_(
                filt, cfg, self._delegate)
            self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
                self._out, SCStreamOutputTypeAudio, self._queue, None)
            self._stream.startCaptureWithCompletionHandler_(self._on_start_result)
        except Exception as e:
            self.error = repr(e)
            self._alive = False

    def _on_start_result(self, error):
        if error is not None:
            self.error = str(error)
            self._alive = False

    @property
    def active(self) -> bool:
        return self._alive

    def stop(self):
        if self._stream is not None:
            ev = threading.Event()
            try:
                self._stream.stopCaptureWithCompletionHandler_(lambda e: ev.set())
                ev.wait(3.0)
            except Exception:
                pass
        self._alive = False
        with self._lock:
            try:
                if self._wav is not None:
                    self._wav.close()
            except Exception:
                pass
            self._wav = None
