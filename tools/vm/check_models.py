"""
Report whether the installed models are complete, and what the app would do.

    python tools\\vm\\check_models.py [models-dir]

Diagnostic. A model file that is present but truncated is the failure mode this
exists for: whisper-cli exits non-zero and every layer above just says
"whisper-cli failed".
"""
import os
import sys
from pathlib import Path

if len(sys.argv) > 1:
    os.environ["NOTULA_MODELS_DIR"] = sys.argv[1]

sys.path.insert(0, r"C:\notula" if os.name == "nt" else
                str(Path(__file__).resolve().parent.parent.parent))

import appcore   # noqa: E402
import config    # noqa: E402
import deps      # noqa: E402
import pipeline  # noqa: E402

cfg = config.load()
print("models dir:", pipeline.MODELS_DIR)
print()
print("on disk:")
if Path(pipeline.MODELS_DIR).is_dir():
    for f in sorted(Path(pipeline.MODELS_DIR).glob("*.bin*")):
        want = deps.SIZES.get(f.name)
        have = f.stat().st_size
        verdict = "complete" if deps.model_complete(f, f.name) else "INCOMPLETE"
        pct = f"  ({have / want:.0%} of {want / (1 << 20):,.0f} MB)" if want else ""
        print(f"  {verdict:11} {f.name:28} {have:>15,} bytes{pct}")
else:
    print("  (no such directory)")

print()
print("what the app would fetch:")
for s in deps.plan(cfg, live_models=True):
    print(f"  {s['key']:28} {s['bytes'] / (1 << 20):,.0f} MB")

print()
print("what the app would say:")
for t in appcore.tool_status(cfg):
    mark = "ok      " if t["ok"] else ("MISSING " if t["required"] else "absent  ")
    print(f"  {mark}{t['label']:28} {t.get('note', '')}")
