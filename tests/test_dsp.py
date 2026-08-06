"""Verify dsp.Resampler: correctness of the piece the Windows capture path depends on."""
import pathlib
import sys
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import dsp

fail = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        fail.append(name)


def tone(f, rate, secs, amp=0.5):
    t = np.arange(int(rate * secs)) / rate
    return (amp * np.sin(2 * np.pi * f * t)).astype("float32")


def peak_freq(x, rate):
    w = np.hanning(len(x))
    sp = np.abs(np.fft.rfft(x * w))
    return np.fft.rfftfreq(len(x), 1 / rate)[np.argmax(sp)]


# 1. output length ~ ratio
for src, dst in ((48000, 16000), (44100, 16000), (16000, 48000), (22050, 16000)):
    r = dsp.Resampler(src, dst)
    out = np.concatenate([r.process(tone(440, src, 1.0)), r.flush()])
    exp = dst
    check(f"length {src}->{dst} (flushed)", abs(len(out) - exp) <= 2, f"{len(out)} vs {exp}")

# 2. block-wise == whole-signal (the seam test that matters for a live callback)
sig = tone(1000, 48000, 0.5)
whole = dsp.Resampler(48000, 16000).process(sig)
r = dsp.Resampler(48000, 16000)
parts = [r.process(sig[i:i + 1024]) for i in range(0, len(sig), 1024)]
chunked = np.concatenate(parts)
n = min(len(whole), len(chunked))
check("blockwise == whole", np.allclose(whole[:n], chunked[:n], atol=1e-6),
      f"maxdiff={np.abs(whole[:n]-chunked[:n]).max():.2e}, n={n} vs {len(whole)}")

# irregular block sizes, like a real callback with varying frame counts
r = dsp.Resampler(48000, 16000)
rng = np.random.default_rng(0)
i, parts = 0, []
while i < len(sig):
    k = int(rng.integers(64, 2400))
    parts.append(r.process(sig[i:i + k]))
    i += k
irregular = np.concatenate(parts)
n = min(len(whole), len(irregular))
check("irregular blocks == whole", np.allclose(whole[:n], irregular[:n], atol=1e-6),
      f"maxdiff={np.abs(whole[:n]-irregular[:n]).max():.2e}")

# 3. a 1 kHz tone stays a 1 kHz tone, at the right level
out = dsp.Resampler(48000, 16000).process(tone(1000, 48000, 1.0, amp=0.5))
body = out[200:-200]
check("1kHz preserved", abs(peak_freq(body, 16000) - 1000) < 20,
      f"peak={peak_freq(body, 16000):.1f}Hz")
check("amplitude preserved", abs(np.abs(body).max() - 0.5) < 0.01,
      f"peak amp={np.abs(body).max():.4f}")

# 4. anti-aliasing: 10 kHz is above the 8 kHz output Nyquist and must be killed,
#    not folded down to a phantom 6 kHz tone
out = dsp.Resampler(48000, 16000).process(tone(10000, 48000, 1.0, amp=0.5))
body = out[400:-400]
check("10kHz rejected (no alias)", np.abs(body).max() < 0.002,
      f"residual peak={np.abs(body).max():.5f}")

# 5. speech-band content survives 44.1k -> 16k
out = dsp.Resampler(44100, 16000).process(tone(3000, 44100, 1.0, amp=0.5))
body = out[300:-300]
check("44.1k 3kHz preserved", abs(peak_freq(body, 16000) - 3000) < 25
      and abs(np.abs(body).max() - 0.5) < 0.02,
      f"peak={peak_freq(body, 16000):.1f}Hz amp={np.abs(body).max():.4f}")

# 6. DC gain is exactly unity (level meters read RMS; drift here would skew them)
out = dsp.Resampler(48000, 16000).process(np.full(48000, 0.3, dtype="float32"))
check("DC gain unity", np.allclose(out[100:-100], 0.3, atol=1e-4),
      f"mean={out[100:-100].mean():.6f}")

# 7. passthrough at matching rates is exact and allocation-free
sig16 = tone(440, 16000, 0.1)
check("passthrough identical", np.array_equal(dsp.Resampler(16000, 16000).process(sig16), sig16))

# 8. converter factory
check("converter None when already 16k mono", dsp.make_converter(16000, 1) is None)
c = dsp.make_converter(48000, 2)
stereo = np.stack([tone(500, 48000, 0.5), tone(500, 48000, 0.5)], axis=1)
out = np.concatenate([c(stereo), c.flush()])
check("stereo 48k -> mono 16k", abs(len(out) - 8000) <= 2 and abs(peak_freq(out[200:-200], 16000) - 500) < 20,
      f"n={len(out)} peak={peak_freq(out[200:-200], 16000):.1f}Hz")

# 9. tiny blocks (smaller than the kernel) must not lose or duplicate samples
r = dsp.Resampler(48000, 16000)
total = sum(len(r.process(sig[i:i + 7])) for i in range(0, len(sig), 7))
check("tiny blocks total length", abs(total - len(whole)) <= 1, f"{total} vs {len(whole)}")

# 10. throughput: this runs on the audio callback thread
import time
blk = tone(500, 48000, 0.0).astype("float32")
blk = np.stack([tone(500, 48000, 1024/48000), tone(500, 48000, 1024/48000)], axis=1)
c = dsp.make_converter(48000, 2)
t0 = time.perf_counter()
N = 2000
for _ in range(N):
    c(blk)
dt = (time.perf_counter() - t0) / N
audio_s = blk.shape[0] / 48000
check("realtime factor", dt < audio_s / 20,
      f"{dt*1e6:.0f}us per {audio_s*1000:.0f}ms block = {audio_s/dt:.0f}x realtime")

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
