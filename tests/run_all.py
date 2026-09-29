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
SUITES = ["test_dsp.py", "test_appcore.py", "test_winhost.py", "test_deps.py", "test_cloud.py"]
JS_SUITES = ["test_meetlist.js", "test_cloudest.js"]        # skipped when node isn't installed

# The suites print what the app shows — toasts with a gear, en dashes — and a
# Windows console is cp1252 by default, where a print of those raises instead
# of printing. UTF-8 mode for the children keeps a glyph from failing a suite.
import os
env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

failed = []
for name in SUITES:
    print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
    rc = subprocess.run([sys.executable, str(HERE / name)], env=env).returncode
    if rc != 0:
        failed.append(name)

import shutil
node = shutil.which("node")
for name in JS_SUITES:
    print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
    if not node:
        print("SKIP  node not installed")
        continue
    if subprocess.run([node, str(HERE / name)]).returncode != 0:
        failed.append(name)

total = len(SUITES) + (len(JS_SUITES) if node else 0)
print(f"\n{'=' * 60}")
print(f"FAILED: {', '.join(failed)}" if failed else f"all {total} suites passed")
sys.exit(1 if failed else 0)
