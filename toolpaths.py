"""
notula.toolpaths — locate the external binaries and models the engine drives.

Notula deliberately doesn't bundle whisper.cpp, ffmpeg, or the torch/pyannote
stack; it drives whatever the machine already has. Where "already has" *is*
differs completely by platform:

    macOS    Homebrew, so /opt/homebrew (or /usr/local on Intel).
    Windows  there's no package manager to assume, so we look in a Notula-owned
             folder under %LOCALAPPDATA% first — that gives users a "drop the
             exes here" story that doesn't require editing PATH — then PATH,
             then the usual winget/scoop/chocolatey shim directories.

Two rules everything here follows:

  * Every lookup is overridable by environment variable. That's how a portable
    or frozen install gets pointed somewhere else without a rebuild.
  * A lookup that finds nothing still returns its best guess rather than None,
    so the error the user sees is "missing model file: C:\\...\\ggml-large-v3.bin"
    instead of a bare TypeError.

PATH is checked but never trusted alone: an app launched from Explorer or the
Dock inherits a very different PATH from one launched in a shell.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from osutil import WINDOWS, data_dir, exe

BREW = "/opt/homebrew"           # Apple Silicon Homebrew; Intel is /usr/local

# env var -> what it overrides
ENV_VARS = {
    "ffmpeg": "NOTULA_FFMPEG",
    "ffprobe": "NOTULA_FFPROBE",
    "whisper-cli": "NOTULA_WHISPER_CLI",
    "whisper-server": "NOTULA_WHISPER_SERVER",
}


def notula_bin() -> str:
    """Notula's own binary drop-box: %LOCALAPPDATA%\\Notula\\bin (or the macOS /
    Linux equivalent). Checked before PATH so a user can make Notula work by
    copying files into one folder."""
    return os.environ.get("NOTULA_BIN") or os.path.join(data_dir(), "bin")


def _search_dirs() -> list[str]:
    """Platform install roots to try after PATH, most likely first."""
    if not WINDOWS:
        return [f"{BREW}/bin", "/usr/local/bin", "/usr/bin"]
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    progs = os.environ.get("ProgramFiles") or r"C:\Program Files"
    choco = os.environ.get("ChocolateyInstall") or r"C:\ProgramData\chocolatey"
    return [
        os.path.join(local, "Microsoft", "WinGet", "Links"),   # winget shims
        os.path.join(home, "scoop", "shims"),                  # scoop
        os.path.join(choco, "bin"),                            # chocolatey
        os.path.join(progs, "ffmpeg", "bin"),
        os.path.join(progs, "whisper.cpp"),
        r"C:\ffmpeg\bin",
        r"C:\whisper.cpp",
    ]


def find_tool(name: str) -> str:
    """Absolute path to an external tool, or the best guess if it isn't installed."""
    env = os.environ.get(ENV_VARS.get(name, ""), "").strip()
    if env:
        return env                       # explicit override wins, existing or not

    candidates = [os.path.join(notula_bin(), exe(name))]
    found = shutil.which(name)
    if found:
        candidates.append(found)
    candidates += [os.path.join(d, exe(name)) for d in _search_dirs()]

    for c in candidates:
        if os.path.isfile(c):
            return c
    # nothing installed — name the place we'd most like it to be
    return candidates[1] if len(candidates) > 1 and found else candidates[0]


def find_models_dir() -> Path:
    """Where the ggml-*.bin whisper models live."""
    env = os.environ.get("NOTULA_MODELS_DIR", "").strip()
    if env:
        return Path(env)

    candidates = [Path(data_dir()) / "models"]
    if not WINDOWS:
        candidates += [Path(BREW) / "share/whisper-cpp/models",
                       Path("/usr/local/share/whisper-cpp/models")]
    # whisper.cpp's own release layout, relative to wherever the binary landed
    cli = find_tool("whisper-cli")
    if cli:
        bindir = Path(cli).parent
        candidates += [bindir / "models", bindir.parent / "share/whisper-cpp/models"]

    # A directory that actually holds models beats one that merely exists. The
    # in-app installer creates its own folder, and without this an empty new
    # folder would take precedence over a Homebrew tree with 3 GB already in it.
    for c in candidates:
        if c.is_dir() and any(c.glob("ggml-*.bin")):
            return c
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


def _has_torch(python: Path) -> bool:
    """Whether a venv's site-packages contains torch, checked on disk.

    A filesystem probe rather than running the interpreter: this is called at
    import time, and launching a python that may be loading a multi-gigabyte ML
    stack just to ask a yes/no question would cost seconds every start.
    """
    root = python.parent.parent                       # <venv>/Scripts|bin/python -> <venv>
    globs = ["Lib/site-packages/torch", "lib/python*/site-packages/torch"]
    return any(any(root.glob(g)) for g in globs)


def find_tx_python() -> str:
    """The transcription venv's python — the only place torch/pyannote live.

    It's external and machine-local by design (see pipeline.py), so this is a
    best guess that $NOTULA_TX_PYTHON is expected to override on most machines.
    """
    env = os.environ.get("NOTULA_TX_PYTHON", "").strip()
    if env:
        return env

    rel = "Scripts/python.exe" if WINDOWS else "bin/python3"
    candidates = [Path(data_dir()) / "txenv" / rel]
    try:
        # running from source: the sibling venv of the openai-whisper project
        here = Path(__file__).resolve().parent
        candidates += [here.parent / ".venv" / rel, here / ".venv" / rel]
    except OSError:
        pass

    for c in candidates:
        # A python is only the *transcription* python if torch is actually in it.
        # Without this check the app's own GUI venv wins — it sits at
        # <repo>/.venv and certainly exists — and diarization then fails at run
        # time with an import error instead of being reported as absent.
        if c.is_file() and _has_torch(c):
            return str(c)
    if not WINDOWS:
        # the original hardcoded default, kept so existing macOS installs that
        # relied on it keep working even if the relative lookup above misses
        return "/Users/macbook/Documents/openai-whisper/.venv/bin/python3"
    return str(candidates[0])


# Resolved once at import, mirroring how pipeline/recorder/live used to hold
# these as module constants. Anything that must re-resolve calls find_*().
FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
WHISPER_CLI = find_tool("whisper-cli")
WHISPER_SERVER = find_tool("whisper-server")
MODELS_DIR = find_models_dir()
TX_PYTHON = find_tx_python()
