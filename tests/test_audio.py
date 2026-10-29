"""
How recordings are stored: new ones as FLAC, older WAVs compressed to FLAC
without losing a sample, and any format a meeting folder holds still found and
transcribable. Nothing here opens an audio device.

    ./.venv/bin/python tests/test_audio.py
"""
import os
import pathlib
import sys
import tempfile
import wave

TMP = pathlib.Path(tempfile.mkdtemp(prefix="notula-audio-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np                       # noqa: E402
import cloud, library, pipeline, recorder  # noqa: E402

try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

fail = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        fail.append(name)


def write_wav(path, seconds=6.0, width=2, seed=0):
    """Something speech-shaped: bursts of tone over a noise floor, with pauses."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(16000 * seconds)) / 16000
    talk = ((t % 2.5) < 1.5).astype(float)          # 1.5 s talking, 1 s pause
    x = 0.3 * talk * np.sin(2 * np.pi * 220 * t) + 0.002 * rng.standard_normal(len(t))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(width)
        w.setframerate(16000)
        if width == 2:
            w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
        else:
            w.writeframes((np.clip(x, -1, 1) * 127 + 128).astype("u1").tobytes())
    return path


def frames(path):
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


# ---- where a meeting's recording is --------------------------------------------

lib = str(TMP / "library")
mid = library.create_meeting(lib, "Formats")
d = pathlib.Path(library.folder(lib, mid))
check("a meeting with no audio yet gets a FLAC", library.audio_path(lib, mid).endswith("audio.flac"))
(d / "audio.m4a").write_bytes(b"x")
check("a library compressed by hand is still found",
      library.audio_path(lib, mid).endswith("audio.m4a"))
check("...so the row still offers Transcribe", library.describe(lib, mid)["hasAudio"])
(d / "audio.wav").write_bytes(b"RIFF" + b"\0" * 96)
check("an older WAV is found ahead of other formats", library.audio_path(lib, mid).endswith("audio.wav"))
(d / "audio.flac").write_bytes(b"fLaC")
check("FLAC wins when both exist", library.audio_path(lib, mid).endswith("audio.flac"))
(d / "src_mic.wav").write_bytes(b"RIFF" + b"\0" * 50)
(d / "notes.wav").write_bytes(b"RIFF")
names = [os.path.basename(p) for p in library.wav_files(str(d))]
check("only the WAVs Notula wrote are offered for compression",
      names == ["audio.wav", "src_mic.wav"], names)
check("the row says how much is still WAV", library.describe(lib, mid)["wavBytes"] == 100 + 54,
      library.describe(lib, mid)["wavBytes"])
check("a missing folder has nothing to compress", library.wav_files(str(TMP / "nope")) == [])

if not os.path.exists(pipeline.FFMPEG):
    print(f"SKIP  the rest: ffmpeg not at {pipeline.FFMPEG}")
    print()
    print("FAILED:", fail if fail else "none")
    sys.exit(1 if fail else 0)

# ---- compressing a WAV -----------------------------------------------------------

src = write_wav(TMP / "audio.wav")
pcm = frames(src)
size = src.stat().st_size
freed = pipeline.compress_wav(src)
flac = TMP / "audio.flac"
check("the WAV is replaced by a FLAC", not src.exists() and flac.exists())
check("it's actually FLAC", flac.read_bytes()[:4] == b"fLaC")
check("the space it reports is what it freed", freed == size - flac.stat().st_size and freed > 0,
      f"{freed} of {size}")
back = TMP / "back.wav"
ok, err = pipeline.convert_audio(flac, back)
check("every sample survives", ok and frames(back) == pcm, err)
check("no half-written file is left behind", not list(TMP.glob(".*partial*")))

# an 8-bit WAV isn't something Notula writes; leave it alone
odd = write_wav(TMP / "odd.wav", width=1)
try:
    pipeline.compress_wav(odd)
    check("an 8-bit WAV is refused", False)
except pipeline.PipelineError as e:
    check("an 8-bit WAV is refused", "16-bit" in str(e), str(e))
check("...and kept", odd.exists() and not (TMP / "odd.flac").exists())

# the FLAC doesn't decode to the same samples: the WAV must stay
keep = write_wav(TMP / "keep.wav", seed=1)
real_md5 = pipeline._pcm_md5
seen = []
def differ(path):
    seen.append(path)
    return f"MD5={len(seen)}"
pipeline._pcm_md5 = differ
try:
    pipeline.compress_wav(keep)
    check("a FLAC that doesn't match is refused", False)
except pipeline.PipelineError as e:
    check("a FLAC that doesn't match is refused", "didn't match" in str(e), str(e))
finally:
    pipeline._pcm_md5 = real_md5
check("...the WAV is kept, the FLAC isn't",
      keep.exists() and frames(keep) and not (TMP / "keep.flac").exists()
      and not list(TMP.glob(".keep*")))

# no ffmpeg: a clear message naming it, and the WAV untouched
real_ffmpeg, pipeline.FFMPEG = pipeline.FFMPEG, str(TMP / "missing" / "ffmpeg")
try:
    pipeline.compress_wav(keep)
    check("no ffmpeg: refused", False)
except pipeline.PipelineError as e:
    check("no ffmpeg: the error names it instead of a bare WinError",
          "ffmpeg not found at" in str(e), str(e))
finally:
    pipeline.FFMPEG = real_ffmpeg
check("...and the WAV is untouched", keep.exists())

# ---- importing and transcribing ---------------------------------------------------

imp = TMP / "imported.flac"
ok, err = pipeline.convert_audio(TMP / "back.wav", imp)
check("an import is stored as FLAC", ok and imp.read_bytes()[:4] == b"fLaC", err)

with pipeline.as_wav(back) as w:
    check("a WAV is transcribed as is", w == back)
with pipeline.as_wav(flac) as w:
    decoded = w
    check("a FLAC is decoded to a WAV for whisper and pyannote",
          w.suffix == ".wav" and w.exists() and frames(w) == pcm)
check("...which is removed afterwards", not decoded.exists() and not decoded.parent.exists())
try:
    with pipeline.as_wav(TMP / "garbage.flac"):
        pass
    check("an unreadable recording fails clearly", False)
except pipeline.PipelineError as e:
    check("an unreadable recording fails clearly", "could not decode" in str(e), str(e))

# the cloud engine reads the recording with ffmpeg; FLAC must cut at the same pauses
pauses = cloud._silences(back)
check("the cloud engine finds the same pauses in a FLAC",
      len(pauses) >= 2 and cloud._silences(flac) == pauses, (pauses, cloud._silences(flac)))

# ---- the recorder's mixdown ----------------------------------------------------------

def engine(folder, kinds):
    os.makedirs(folder, exist_ok=True)
    rec = recorder.RecordingEngine([{"kind": k, "index": 0} for k in kinds], folder)
    for i, s in enumerate(rec.sources):
        write_wav(s.wav_path, seed=i)
        s.frames = 1
    return rec

final = engine(str(TMP / "rec2"), ["mic", "system"]).stop()
check("two sources are mixed straight to FLAC",
      final.endswith("audio.flac") and open(final, "rb").read(4) == b"fLaC", final)
check("...as long as the sources", abs(pipeline.probe_duration(pathlib.Path(final)) - 6.0) < 0.05)
final = engine(str(TMP / "rec1"), ["mic"]).stop()
check("one source is stored as FLAC too", final.endswith("audio.flac") and os.path.exists(final))
with pipeline.as_wav(final) as w:
    check("...with the source's exact samples", frames(w) == frames(TMP / "rec1" / "src_mic.wav"))

recorder.FFMPEG = str(TMP / "missing" / "ffmpeg")
try:
    final = engine(str(TMP / "rec0"), ["mic", "system"]).stop()
finally:
    recorder.FFMPEG = real_ffmpeg
check("no ffmpeg: the recording is still kept, as WAV",
      final.endswith("audio.wav") and os.path.exists(final)
      and not os.path.exists(str(TMP / "rec0" / "audio.flac")), final)

print()
print("FAILED:", fail if fail else "none")
print("temp dir:", TMP)
sys.exit(1 if fail else 0)
