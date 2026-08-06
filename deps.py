"""
notula.deps — fetch the external tools from inside the app.

Notula drives ffmpeg and whisper.cpp rather than bundling them, which on a fresh
machine leaves the user with a working window that can't transcribe. Rather than
send them to a shell script, this does the fetching in-process with progress in
the UI.

What that means differs by platform, because the upstreams differ:

    Windows   Everything is a download. ffmpeg ships as a zip, whisper.cpp
              publishes prebuilt x64 binaries per release, and the models come
              from HuggingFace. Nothing needs admin rights; it all lands in
              %LOCALAPPDATA%\\Notula.

    macOS     whisper.cpp publishes no macOS CLI build, so the binaries come
              from Homebrew — which the project already assumes and which is the
              only sane way to get a signed, notarized ffmpeg too. The models,
              which are the multi-gigabyte part, are downloaded directly.

Everything here is resumable, cancellable, and safe to re-run: a partial file is
continued with a Range request rather than restarted, and anything already
present is skipped. Downloads go to a `.part` file and are moved into place only
once complete, so an interrupted run can never leave a truncated model that
whisper would fail on with an unhelpful error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import osutil
import pipeline
import toolpaths

MODEL_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
VAD_URL = ("https://huggingface.co/ggml-org/whisper-vad/resolve/main/"
           "ggml-silero-v6.2.0.bin")
FFMPEG_WIN = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
WHISPER_API = "https://api.github.com/repos/ggml-org/whisper.cpp/releases/latest"
# Pinned so a GitHub rate limit (60/hour per IP, unauthenticated) degrades to a
# slightly older build rather than to a failure.
WHISPER_TAG = "v1.9.2"
WHISPER_ASSET = "whisper-blas-bin-x64.zip"      # BLAS: faster than plain, no GPU needed

UA = {"User-Agent": "notula-setup"}
CHUNK = 1 << 18

# Roughly how big each download is, for a progress bar that can show a total
# before any of it has started.
SIZES = {
    "ffmpeg": 80 << 20,
    "whisper": 20 << 20,
    "ggml-large-v3.bin": 2952 << 20,
    "ggml-large-v3-turbo.bin": 1549 << 20,
    "ggml-small.bin": 465 << 20,
    "ggml-silero-v6.2.0.bin": 1 << 20,
    "brew": 120 << 20,
}


class Cancelled(Exception):
    pass


# ---- download ----------------------------------------------------------------

def _download(url: str, dest: Path, on_bytes, cancel) -> None:
    """Fetch `url` to `dest`, resuming if a partial download is already there.

    The file is built as `<dest>.part` and only moved into place when complete,
    so an interruption can never leave something that looks like a finished
    model but isn't — whisper's error for a truncated .bin is inscrutable.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0

    req = urllib.request.Request(url, headers=dict(UA))
    if have:
        req.add_header("Range", f"bytes={have}-")
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if have and e.code in (416, 200):      # stale partial; start over
            part.unlink(missing_ok=True)
            have = 0
            resp = urllib.request.urlopen(
                urllib.request.Request(url, headers=dict(UA)), timeout=60)
        else:
            raise

    with resp:
        # a 200 to a Range request means the server ignored it: restart cleanly
        if have and resp.status == 200:
            part.unlink(missing_ok=True)
            have = 0
        total = int(resp.headers.get("Content-Length") or 0) + have
        mode = "ab" if have else "wb"
        with open(part, mode) as fh:
            done = have
            on_bytes(done, total)
            while True:
                if cancel.is_set():
                    raise Cancelled()
                buf = resp.read(CHUNK)
                if not buf:
                    break
                fh.write(buf)
                done += len(buf)
                on_bytes(done, total)
    part.replace(dest)


def _extract_find(zip_path: Path, wanted: list[str], into: Path,
                  also_dlls: bool = False) -> list[str]:
    """Unpack `zip_path` and copy out the named executables.

    Searched recursively rather than by a fixed path: whisper.cpp nests its build
    under Release\\, ffmpeg under ffmpeg-<version>-essentials_build\\bin\\, and
    both have changed shape between releases. `also_dlls` brings along every DLL
    sitting beside the binary, without which whisper.cpp's exes won't start.
    """
    into.mkdir(parents=True, exist_ok=True)
    got = []
    with tempfile.TemporaryDirectory(prefix="notula-dep-") as tmp:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        root = Path(tmp)
        for name in wanted:
            hit = next((p for p in root.rglob(name) if p.is_file()), None)
            if hit is None:
                continue
            shutil.copyfile(hit, into / name)
            got.append(name)
            if also_dlls:
                for dll in hit.parent.glob("*.dll"):
                    shutil.copyfile(dll, into / dll.name)
    return got


def _whisper_asset() -> tuple[str, str]:
    """(url, filename) of the whisper.cpp Windows build to fetch."""
    try:
        import json
        with urllib.request.urlopen(
                urllib.request.Request(WHISPER_API, headers=dict(UA)), timeout=30) as r:
            rel = json.load(r)
        for a in rel.get("assets", []):
            if a.get("name") == WHISPER_ASSET:
                return a["browser_download_url"], a["name"]
    except Exception:
        pass
    return (f"https://github.com/ggml-org/whisper.cpp/releases/download/"
            f"{WHISPER_TAG}/{WHISPER_ASSET}"), WHISPER_ASSET


# ---- what needs doing ---------------------------------------------------------

def plan(cfg, live_models: bool = False) -> list[dict]:
    """The steps needed to complete this install — only what's actually missing.

    Each step is {key, label, kind, bytes}. `kind` is what the UI needs to warn
    about: 'download' is self-contained, 'brew' shells out to Homebrew and can
    take a while with no byte count to show.
    """
    steps: list[dict] = []
    models_dir = Path(pipeline.MODELS_DIR)

    have_ffmpeg = os.path.exists(pipeline.FFMPEG) and os.path.exists(pipeline.FFPROBE)
    have_whisper = os.path.exists(pipeline.WHISPER_CLI)

    if osutil.WINDOWS:
        if not have_ffmpeg:
            steps.append({"key": "ffmpeg", "label": "ffmpeg",
                          "kind": "download", "bytes": SIZES["ffmpeg"]})
        if not have_whisper:
            steps.append({"key": "whisper", "label": "whisper.cpp",
                          "kind": "download", "bytes": SIZES["whisper"]})
    else:
        missing = [n for n, ok in (("ffmpeg", have_ffmpeg), ("whisper-cpp", have_whisper))
                   if not ok]
        if missing:
            steps.append({"key": "brew", "label": f"{', '.join(missing)} (via Homebrew)",
                          "kind": "brew", "bytes": SIZES["brew"],
                          "formulae": missing})

    wanted = [f"ggml-{cfg['model']}.bin"]
    if live_models:
        wanted += ["ggml-large-v3-turbo.bin", "ggml-small.bin"]
    for name in wanted:
        if not (models_dir / name).exists():
            steps.append({"key": name, "label": f"model {name}", "kind": "download",
                          "bytes": SIZES.get(name, 1 << 30)})
    if pipeline.find_vad_model() is None:
        steps.append({"key": "ggml-silero-v6.2.0.bin", "label": "VAD model",
                      "kind": "download", "bytes": SIZES["ggml-silero-v6.2.0.bin"]})
    return steps


def free_space(path: Path) -> int:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(path).free
    except OSError:
        return 1 << 62          # can't tell; don't block on it


# ---- doing it -----------------------------------------------------------------

def _run_brew(formulae: list[str], on_line, cancel) -> None:
    brew = shutil.which("brew") or "/opt/homebrew/bin/brew"
    if not os.path.exists(brew):
        raise RuntimeError(
            "Homebrew isn't installed, and whisper.cpp publishes no macOS build "
            "to download. Install it from https://brew.sh, then try again.")
    proc = subprocess.Popen(
        [brew, "install", *formulae], stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
        encoding="utf-8", errors="replace", **osutil.popen_kwargs())
    try:
        for line in proc.stdout:
            if cancel.is_set():
                osutil.kill_tree(proc)
                raise Cancelled()
            line = line.strip()
            if line:
                on_line(line[:120])
    finally:
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"brew install {' '.join(formulae)} failed (exit {rc})")


def install(steps: list[dict], on_progress, cancel) -> dict:
    """Run `steps`, reporting through on_progress(fraction, message, detail).

    Blocking; run it on a worker thread. Returns {ok, error, done:[keys]}.
    """
    # Steps are weighted by their size *estimate*, but progress within a step
    # uses that download's real Content-Length. Mixing the two — measuring
    # progress in bytes against an estimated total — makes the bar overshoot and
    # snap backwards whenever a guess is low (ffmpeg is ~106 MB against an 80 MB
    # estimate, so it hit 100% and then jumped back to 80%).
    total_weight = sum(s["bytes"] for s in steps) or 1
    finished_weight = 0
    done: list[str] = []
    models_dir = Path(pipeline.MODELS_DIR)
    bindir = Path(toolpaths.notula_bin())
    cur_weight = 0

    def emit(step_frac, msg, detail=""):
        frac = (finished_weight + max(0.0, min(1.0, step_frac)) * cur_weight) / total_weight
        on_progress(min(1.0, frac), msg, detail)

    def bytes_cb(label):
        def cb(d, t):
            emit(d / t if t else 0.0, f"Downloading {label}...", _human(d, t))
        return cb

    try:
        for step in steps:
            if cancel.is_set():
                raise Cancelled()
            key, label = step["key"], step["label"]
            cur_weight = step["bytes"]
            emit(0.0, f"Installing {label}...")

            if step["kind"] == "brew":
                _run_brew(step["formulae"],
                          lambda l: emit(0.5, f"Installing {label}...", l), cancel)

            elif key == "ffmpeg":
                zp = models_dir.parent / "cache" / "ffmpeg.zip"
                _download(FFMPEG_WIN, zp, bytes_cb(label), cancel)
                emit(1.0, f"Unpacking {label}...")
                got = _extract_find(zp, ["ffmpeg.exe", "ffprobe.exe"], bindir)
                if len(got) < 2:
                    raise RuntimeError("ffmpeg.exe/ffprobe.exe not found in the archive")
                zp.unlink(missing_ok=True)

            elif key == "whisper":
                url, name = _whisper_asset()
                zp = models_dir.parent / "cache" / name
                _download(url, zp, bytes_cb(label), cancel)
                emit(1.0, f"Unpacking {label}...")
                got = _extract_find(zp, ["whisper-cli.exe", "whisper-server.exe"],
                                    bindir, also_dlls=True)
                if "whisper-cli.exe" not in got:
                    raise RuntimeError("whisper-cli.exe not found in the archive")
                zp.unlink(missing_ok=True)

            else:                                    # a model
                url = VAD_URL if key.startswith("ggml-silero") else f"{MODEL_BASE}/{key}"
                _download(url, models_dir / key, bytes_cb(label), cancel)

            finished_weight += cur_weight
            cur_weight = 0
            done.append(key)
            emit(0.0, f"Installed {label}")
        on_progress(1.0, "Everything is installed", "")
        return {"ok": True, "error": None, "done": done}
    except Cancelled:
        return {"ok": False, "error": None, "cancelled": True, "done": done}
    except Exception as e:
        return {"ok": False, "error": str(e), "done": done}


def _human(done: int, total: int) -> str:
    mb = 1 << 20
    if total:
        return f"{done / mb:,.0f} / {total / mb:,.0f} MB"
    return f"{done / mb:,.0f} MB"
