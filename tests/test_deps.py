"""
Exercise the in-app dependency installer against the real network, using the
smallest real artifact (the ~0.8 MB VAD model) so it's a genuine end-to-end test
of download, resume, cancel and atomic placement without moving gigabytes.

Skips itself when offline. Set NOTULA_SKIP_NET=1 to skip regardless.
"""
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

TMP = tempfile.mkdtemp(prefix="notula-deps-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "cfg")
os.environ["APPDATA"] = os.path.join(TMP, "cfg")

import deps            # noqa: E402
import pipeline        # noqa: E402

fail = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        fail.append(name)


# ---- plan(): platform-correct, and offline-safe so it always runs ---------------
import config as _config          # noqa: E402
import osutil                     # noqa: E402

_cfg = _config.load()
_real = (pipeline.FFMPEG, pipeline.FFPROBE, pipeline.WHISPER_CLI, pipeline.MODELS_DIR)
pipeline.FFMPEG = pipeline.FFPROBE = pipeline.WHISPER_CLI = "/definitely/not/here"
# an empty models dir too, or this machine's real one makes the plan (correctly)
# skip the models and the test measures nothing
pipeline.MODELS_DIR = pathlib.Path(TMP) / "empty-models"
try:
    kinds = {s["kind"] for s in deps.plan(_cfg)}
    keys = [s["key"] for s in deps.plan(_cfg)]
    if osutil.WINDOWS:
        check("windows plans downloads, not brew", kinds == {"download"}, kinds)
        check("windows fetches ffmpeg + whisper",
              "ffmpeg" in keys and "whisper" in keys, keys)
    else:
        check("macos routes binaries through brew", "brew" in kinds, kinds)
        check("macos plans one brew step for both formulae",
              sum(1 for s in deps.plan(_cfg) if s["kind"] == "brew") == 1, keys)
    check("a model is always part of the plan",
          any(k.startswith("ggml-") for k in keys), keys)
    check("live models are opt-in",
          not any("turbo" in s["key"] for s in deps.plan(_cfg)) and
          any("turbo" in s["key"] for s in deps.plan(_cfg, live_models=True)))
finally:
    (pipeline.FFMPEG, pipeline.FFPROBE, pipeline.WHISPER_CLI,
     pipeline.MODELS_DIR) = _real


def online():
    if os.environ.get("NOTULA_SKIP_NET"):
        return False
    try:
        urllib.request.urlopen("https://huggingface.co", timeout=10).close()
        return True
    except Exception:
        return False


if not online():
    print("SKIP  no network (or NOTULA_SKIP_NET set) — deps tests skipped")
    sys.exit(0)

models = pathlib.Path(TMP) / "models"
pipeline.MODELS_DIR = models
VAD = "ggml-silero-v6.2.0.bin"
step = [{"key": VAD, "label": "VAD model", "kind": "download",
         "bytes": deps.SIZES[VAD]}]

# ---- a plain install -----------------------------------------------------------
seen = []
res = deps.install(step, lambda f, m, d: seen.append(f), threading.Event())
got = models / VAD
check("install reports ok", res["ok"] is True, res.get("error"))
check("the file landed", got.exists() and got.stat().st_size > 500_000,
      got.stat().st_size if got.exists() else "missing")
check("progress was reported", len(seen) > 2 and max(seen) >= 0.99, f"{len(seen)} updates")
check("no .part left behind", not (models / (VAD + ".part")).exists())
full = got.stat().st_size

# ---- already present: plan() skips it -------------------------------------------
import config                                        # noqa: E402
cfg = config.load()
check("plan skips what's present",
      not any(s["key"] == VAD for s in deps.plan(cfg)),
      [s["key"] for s in deps.plan(cfg)])

# ---- resume: a partial download continues rather than restarting -----------------
got.unlink()
part = models / (VAD + ".part")
part.write_bytes(b"\0" * 200_000)             # pretend we got 200 KB last time
res = deps.install(step, lambda f, m, d: None, threading.Event())
check("resumed download completes", res["ok"] and got.exists())
check("resumed file is the right size", got.stat().st_size == full,
      f"{got.stat().st_size} vs {full}")

# ---- a corrupt/oversized partial is discarded, not appended to -------------------
got.unlink()
part.write_bytes(b"\0" * (full + 50_000))     # longer than the real file
res = deps.install(step, lambda f, m, d: None, threading.Event())
check("bad partial is recovered from", res["ok"] and got.exists(), res.get("error"))
check("recovered file is the right size", got.stat().st_size == full,
      f"{got.stat().st_size} vs {full}")

# ---- cancel ----------------------------------------------------------------------
got.unlink()
cancel = threading.Event()
big = [{"key": "ggml-small.bin", "label": "model", "kind": "download",
        "bytes": deps.SIZES["ggml-small.bin"]}]
result = {}


def run():
    result.update(deps.install(big, lambda f, m, d: cancel.set(), cancel))


t = threading.Thread(target=run)
t.start()
t.join(60)
check("cancel stops promptly", not t.is_alive())
check("cancel is reported as cancelled", result.get("cancelled") is True, result)
check("cancel leaves no finished file", not (models / "ggml-small.bin").exists())
check("cancel keeps the partial for next time",
      (models / "ggml-small.bin.part").exists())

shutil.rmtree(TMP, ignore_errors=True)
print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
