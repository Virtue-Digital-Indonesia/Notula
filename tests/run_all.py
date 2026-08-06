#!/usr/bin/env python3
"""
Run every suite. Plain scripts, no test framework — same spirit as the rest of
the project.

    ./.venv/bin/python tests/run_all.py          # macOS
    .venv\\Scripts\\python tests\\run_all.py       # Windows

These cover the platform-independent half of the app and the parts of the
Windows shell that don't touch Windows APIs, so they're meaningful on both. They
are *not* a substitute for actually recording something: nothing here opens an
audio device. See docs/windows.md for what still needs a real machine.
"""

import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
SUITES = ["test_dsp.py", "test_appcore.py", "test_winhost.py", "test_deps.py"]

failed = []
for name in SUITES:
    print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
    rc = subprocess.run([sys.executable, str(HERE / name)]).returncode
    if rc != 0:
        failed.append(name)

print(f"\n{'=' * 60}")
print(f"FAILED: {', '.join(failed)}" if failed else f"all {len(SUITES)} suites passed")
sys.exit(1 if failed else 0)
