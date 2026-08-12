"""
Run the exact whisper-cli command pipeline.py builds, and show ALL of its output.

    python tools\\vm\\repro_whisper.py

Diagnostic only. The app reports whisper failures by first line, which for a
WhisperError is the useless literal "whisper-cli failed:" — this prints the part
that actually says what went wrong.
"""
import os
import subprocess
import sys
import threading
import wave
from pathlib import Path

sys.path.insert(0, r"C:\notula" if os.name == "nt" else
                str(Path(__file__).resolve().parent.parent.parent))

import config      # noqa: E402
import deps        # noqa: E402
import pipeline    # noqa: E402

cfg = config.load()
models = Path(pipeline.MODELS_DIR)

# a small model is enough to exercise the command shape
TINY = "ggml-tiny.bin"
need = []
if not (models / TINY).exists():
    need.append({"key": TINY, "label": TINY, "kind": "download", "bytes": 78 << 20})
if pipeline.find_vad_model() is None:
    need.append({"key": "ggml-silero-v6.2.0.bin", "label": "VAD", "kind": "download",
                 "bytes": 1 << 20})
if need:
    print("fetching:", [s["key"] for s in need], flush=True)
    r = deps.install(need, lambda f, m, d: None, threading.Event())
    print("fetch:", r, flush=True)

# 3 seconds of quiet noise — enough for whisper to run start to finish
wav = Path(os.environ.get("TEMP", "/tmp")) / "notula-repro.wav"
if not wav.exists():
    import struct, random
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", random.randint(-800, 800))
                               for _ in range(16000 * 3)))
print("wav:", wav, wav.stat().st_size, "bytes", flush=True)

out = Path(os.environ.get("TEMP", "/tmp")) / "notula-repro-out"
out.mkdir(exist_ok=True)

model = models / TINY
vad = pipeline.find_vad_model()
print("model:", model, model.exists())
print("vad  :", vad)
print("cli  :", pipeline.WHISPER_CLI, os.path.exists(pipeline.WHISPER_CLI))

cmd = [
    pipeline.WHISPER_CLI, "-m", str(model), "-l", cfg["lang"], "-f", str(wav),
    "--output-txt", "--output-json", "-of", str(out / "transcript"),
    "--vad", "--vad-model", str(vad), "--suppress-nst",
    "--entropy-thold", "2.6", "--logprob-thold", "-1.0",
    "--no-speech-thold", "0.6", "-mc", "0", "--print-progress",
]
print("\n--- command ---")
print(" ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd))
print("\n--- output ---", flush=True)
p = subprocess.run(cmd, capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
print(p.stdout or "")
print(p.stderr or "")
print(f"--- exit code {p.returncode} ---")
print("produced:", sorted(x.name for x in out.iterdir()) if out.exists() else "nothing")
