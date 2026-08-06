"""
notula.dsp — sample-rate conversion and downmix for capture sources.

macOS never needed this: ScreenCaptureKit is *told* to hand over 16 kHz mono,
and PortAudio opens CoreAudio mics at 16 kHz directly. Windows gives us neither.
WASAPI loopback can only capture in the render device's own mix format — in
practice 48 kHz stereo float32 — and a shared-mode WASAPI microphone is stuck at
whatever the endpoint is configured for. So the Windows sources convert in the
capture path, and whisper still receives the 16 kHz mono it wants.

The resampler is a windowed-sinc interpolator evaluated only at output sample
positions (a polyphase filter without materializing the upsampled signal), so it
handles arbitrary ratios — 44.1 kHz -> 16 kHz costs the same as 48 -> 16, with no
160x intermediate signal. It is stateful: `process()` carries the filter's left
context and fractional phase across blocks, so a stream chopped into blocks
resamples identically to the same stream resampled whole. Feed it one block at a
time from the audio callback and the seams are inaudible.
"""

from __future__ import annotations

import math

import numpy as np

# Kernel width, in input samples. This is what buys stopband rejection, and it
# has to be judged at the *input* rate: decimating 48 kHz to 16 kHz puts the
# cutoff at a third of Nyquist, and a Blackman-windowed sinc's transition band is
# roughly 5.5/TAPS cycles/sample — 32 taps would spread the transition over 8 kHz
# and let a 10 kHz tone through only 23 dB down, folded back into the speech band.
# 128 taps narrows it to ~2 kHz, putting everything above ~8.6 kHz in a -74 dB
# stopband, and still costs well under a millisecond per capture block.
TAPS = 128

# Band-limit slightly below the output Nyquist rather than exactly at it, so the
# transition band has somewhere to go that isn't aliasing.
_ROLLOFF = 0.95

# Filter taps depend only on an output sample's fractional position between two
# input samples, so they're precomputed once into a bank of _PHASES rows and then
# just gathered. Recomputing sinc+window per block instead costs ~10x more, all of
# it on the audio callback thread. Quantizing the phase to 1/1024 of a sample is
# 20 ns of timing jitter at 48 kHz — orders of magnitude below anything audible.
_PHASES = 1024


def rms_to_level(rms: float) -> float:
    """Map an RMS amplitude to a 0..1 meter value on a dBFS scale (-60..0 dB)."""
    if rms <= 1e-7:
        return 0.0
    db = 20.0 * math.log10(rms)
    return max(0.0, min(1.0, (db + 60.0) / 60.0))


def downmix(block: np.ndarray) -> np.ndarray:
    """(frames, channels) -> (frames,) mono float32, by average.

    Average rather than sum: summing a correlated stereo pair doubles the
    amplitude and clips a signal that was already near full scale.
    """
    a = np.asarray(block, dtype="float32")
    if a.ndim == 1:
        return a
    if a.shape[1] == 1:
        return a[:, 0]
    return a.mean(axis=1, dtype="float32")


class Resampler:
    """Stateful arbitrary-ratio resampler, mono float32 in and out."""

    def __init__(self, src_rate: float, dst_rate: float, taps: int = TAPS):
        self.src_rate = float(src_rate)
        self.dst_rate = float(dst_rate)
        self.ratio = self.src_rate / self.dst_rate        # input samples per output
        self.passthrough = abs(self.src_rate - self.dst_rate) < 1e-6
        self._half = max(2, int(taps) // 2)
        # Anti-alias to the *lower* of the two Nyquists. Upsampling is already
        # band-limited by the source, so it needs no extra cut (and no rolloff,
        # which would throw away the top of a band we have in full).
        self._cutoff = (_ROLLOFF * self.dst_rate / self.src_rate
                        if self.dst_rate < self.src_rate else 1.0)
        self._k = np.arange(-self._half + 1, self._half + 1, dtype="float64")
        self._ki = self._k.astype(np.int64)
        self._bank = (None if self.passthrough else
                      self._kernel(np.arange(_PHASES, dtype="float64") / _PHASES))
        self.reset()

    def reset(self) -> None:
        # Prime with silence so the very first real sample already has the left
        # context the kernel needs; without it the stream would start mid-kernel.
        self._buf = np.zeros(self._half, dtype="float32")
        self._pos = float(self._half)

    def _kernel(self, frac: np.ndarray) -> np.ndarray:
        """Filter taps for each output sample's fractional phase -> (n_out, taps)."""
        x = self._k[None, :] - frac[:, None]
        h = self._cutoff * np.sinc(self._cutoff * x)
        # Blackman window over [-half, half], mapped to [0, 1]
        u = (x + self._half) / (2.0 * self._half)
        h *= 0.42 - 0.5 * np.cos(2 * np.pi * u) + 0.08 * np.cos(4 * np.pi * u)
        # Normalize each row to unity DC gain: the raw taps sum to ~1 but drift
        # with the fractional phase, which would show up as level ripple.
        s = h.sum(axis=1, keepdims=True)
        np.divide(h, s, out=h, where=s != 0)
        return h.astype("float32")

    def process(self, block: np.ndarray) -> np.ndarray:
        """Resample one block. May return fewer (or zero) samples than it takes —
        the remainder is carried until enough input has arrived."""
        block = np.asarray(block, dtype="float32")
        if self.passthrough:
            return block
        if block.ndim > 1:
            block = downmix(block)

        buf = np.concatenate((self._buf, block)) if len(self._buf) else block
        half = self._half
        # An output at position p needs input [p-half+1, p+half], so the last
        # producible output sits `half` samples inside the end of the buffer.
        limit = len(buf) - 1 - half
        n_out = 0
        if limit >= self._pos:
            n_out = int(np.floor((limit - self._pos) / self.ratio)) + 1
        if n_out <= 0:
            self._buf = buf
            return np.zeros(0, dtype="float32")

        pos = self._pos + np.arange(n_out, dtype="float64") * self.ratio
        base = np.floor(pos).astype(np.int64)
        phase = np.minimum(((pos - base) * _PHASES + 0.5).astype(np.int64), _PHASES - 1)
        h = self._bank[phase]
        idx = base[:, None] + self._ki[None, :]
        out = np.einsum("ij,ij->i", buf[idx], h, dtype="float32")

        # Keep only what the next call still needs as left context.
        nxt = self._pos + n_out * self.ratio
        keep = max(0, int(np.floor(nxt)) - half + 1)
        self._buf = buf[keep:]
        self._pos = nxt - keep
        return out

    def flush(self) -> np.ndarray:
        """Drain the tail the filter is still holding as right-context.

        `process` can't emit an output until it has seen `half` input samples
        past it, so at any instant the last ~half/ratio output samples are still
        pending — a third of a millisecond at 48->16 kHz. Streaming makes that up
        on the next block; at end of stream, call this so the resampled file is
        exactly as long as the recording was.
        """
        if self.passthrough:
            return np.zeros(0, dtype="float32")
        return self.process(np.zeros(self._half, dtype="float32"))


class Converter:
    """Downmix + resample a raw capture block to 16 kHz mono float32."""

    def __init__(self, src_rate: float, channels: int, dst_rate: float = 16000):
        self.channels = int(channels)
        self._rs = (Resampler(src_rate, dst_rate)
                    if abs(float(src_rate) - float(dst_rate)) > 1e-6 else None)

    def __call__(self, block: np.ndarray) -> np.ndarray:
        mono = downmix(block) if self.channels > 1 else np.asarray(block, dtype="float32")
        return self._rs.process(mono) if self._rs is not None else mono

    def flush(self) -> np.ndarray:
        return self._rs.flush() if self._rs is not None else np.zeros(0, dtype="float32")


def make_converter(src_rate: float, channels: int, dst_rate: float = 16000):
    """A Converter, or None when the device already delivers exactly what we want.

    Returning None for the no-op case keeps the macOS path (16 kHz mono from both
    CoreAudio and ScreenCaptureKit) free of any per-block work at all.
    """
    if int(channels) <= 1 and abs(float(src_rate) - float(dst_rate)) <= 1e-6:
        return None
    return Converter(src_rate, channels, dst_rate)
