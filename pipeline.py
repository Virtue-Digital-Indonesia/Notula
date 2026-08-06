"""
notula.pipeline — turn a recorded WAV into a diarized, speaker-labeled transcript.

This module is stdlib-only so it can be imported by the app's lightweight pyobjc
venv (which has no torch/pyannote). It shells out twice:

  * whisper-cli (Homebrew) for transcription, with --print-progress parsed live;
  * the transcription venv's python running diarize_and_merge.py for pyannote
    speaker diarization (that venv is the only place torch/pyannote live).

Whisper failure is fatal (no transcript to salvage). Diarization failure
degrades gracefully to the plain transcript — output.txt is always written.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import osutil
import toolpaths

# Resolved by toolpaths, which knows where each platform keeps these. Absolute
# paths matter inside a bundled app, where PATH is stripped and which() finds
# nothing — on macOS *and* on Windows, where an app launched from Explorer
# inherits a different PATH than one launched from a shell.
MODELS_DIR = toolpaths.MODELS_DIR
WHISPER_CLI = toolpaths.WHISPER_CLI
FFPROBE = toolpaths.FFPROBE
FFMPEG = toolpaths.FFMPEG


def _resource_dir() -> Path:
    """Where our bundled data files live: Contents/Resources under py2app,
    _MEIPASS under PyInstaller, or this file's directory when running from
    source."""
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("RESOURCEPATH")
                    or getattr(sys, "_MEIPASS", "")
                    or os.path.dirname(sys.executable))
    return Path(__file__).parent


# diarize_and_merge.py must be a real on-disk file (the tx venv python runs it),
# so it ships as a py2app resource rather than a frozen module.
DIARIZE_SCRIPT = _resource_dir() / "diarize_and_merge.py"

# The transcription venv (torch/pyannote/soundfile) is external and machine-local;
# override with $NOTULA_TX_PYTHON if it lives elsewhere.
TX_PYTHON = toolpaths.TX_PYTHON

WHISPER_W = 0.70   # transcribe spans 0..0.70 of the bar; diarize 0.70..1.0

ProgressCB = Callable[[str, float, str], None]

_RE_PCT = re.compile(r"progress\s*=\s*(\d+)%")               # whisper stderr, unbuffered
_RE_SEG_END = re.compile(r"-->\s*(\d\d):(\d\d):(\d\d)\.(\d\d\d)")  # whisper stdout segment
_RE_DP = re.compile(r"^@@P\s+([0-9.]+)\s+(.*)$")             # diarize progress protocol


# ---- errors -------------------------------------------------------------------

class PipelineError(Exception):
    pass


class ModelNotFoundError(PipelineError):
    pass


class WhisperError(PipelineError):
    pass


class DiarizationError(PipelineError):
    pass


class TokenMissingError(DiarizationError):
    pass


# ---- helpers ------------------------------------------------------------------

# py2app (and PyInstaller) leak these into the environment; passing them to the
# EXTERNAL tx-venv python makes it search the frozen app's stripped stdlib and
# fail to import.
_PY_ENV_STRIP = (
    "PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "PYTHONNOUSERSITE",
    "PYTHONDONTWRITEBYTECODE", "PYTHONFRAMEWORK", "PYTHONUSERBASE",
    "__PYVENV_LAUNCHER__", "PYTHONOPTIMIZE",
)


def _clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _PY_ENV_STRIP}
    env.update(extra)
    return env


def _noop(stage: str, frac: float, msg: str) -> None:
    pass


# Whisper's VAD model. Homebrew ships one exact version, so macOS could name it
# outright; on Windows you fetch it yourself and will have whichever silero build
# was current, so accept any of them and prefer the newest.
VAD_MODEL = "ggml-silero-v6.2.0.bin"
VAD_GLOB = "ggml-silero-*.bin"


def find_vad_model() -> Optional[Path]:
    exact = MODELS_DIR / VAD_MODEL
    if exact.exists():
        return exact
    found = sorted(MODELS_DIR.glob(VAD_GLOB)) if MODELS_DIR.is_dir() else []
    return found[-1] if found else None


class _Mono:
    """Clamp the global progress fraction so it never goes backwards."""

    def __init__(self, cb: ProgressCB):
        self.cb = cb
        self.last = 0.0

    def __call__(self, stage: str, frac: float, msg: str) -> None:
        frac = min(1.0, max(self.last, max(0.0, frac)))
        self.last = frac
        self.cb(stage, frac, msg)


def convert_to_wav(src, dst) -> tuple[bool, str | None]:
    """Convert any audio/video file to a 16 kHz mono WAV (for imported recordings)."""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)]
    try:
        subprocess.run(cmd, check=True, capture_output=True,
                       **osutil.popen_kwargs(new_group=False))
        return True, None
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode("utf-8", "replace").strip()
        return False, (err.splitlines()[-1] if err else f"ffmpeg exit {e.returncode}")
    except OSError as e:
        return False, str(e)


def probe_duration(wav: Path) -> float:
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(wav)],
            capture_output=True, text=True, timeout=30,
            **osutil.popen_kwargs(new_group=False),
        ).stdout.strip()
        return float(out or 0.0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


# ---- stages -------------------------------------------------------------------

def _run_whisper(wav, prefix, model_file, vad_model, lang, duration, emit, on_proc=None) -> None:
    cmd = [
        WHISPER_CLI, "-m", str(model_file), "-l", lang, "-f", str(wav),
        "--output-txt", "--output-json", "-of", str(prefix),
        "--vad", "--vad-model", str(vad_model), "--suppress-nst",
        "--entropy-thold", "2.6", "--logprob-thold", "-1.0",
        "--no-speech-thold", "0.6", "-mc", "0",
        "--print-progress",     # not in transcribe.sh — needed for a progress bar
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            encoding="utf-8", errors="replace",   # not the ASCII locale
                            # own process group, killable on quit; and on Windows
                            # no console window flashing over the UI
                            **osutil.popen_kwargs())
    if on_proc:
        on_proc(proc)
    tail: list[str] = []
    for line in proc.stdout:
        tail.append(line)
        del tail[:-40]
        m = _RE_PCT.search(line)
        if m:
            pct = int(m.group(1))
            emit("transcribe", WHISPER_W * pct / 100.0, f"transcribing… {pct}%")
            continue
        m = _RE_SEG_END.search(line)
        if m and duration > 0:
            h, mm, s, ms = map(int, m.groups())
            secs = h * 3600 + mm * 60 + s + ms / 1000.0
            emit("transcribe", WHISPER_W * min(1.0, secs / duration), "transcribing…")
    if proc.wait() != 0:
        raise WhisperError("whisper-cli failed:\n" + "".join(tail[-20:]))


def _run_diarize(wav, json_path, merged, token, mn, mx, tx_python, emit, on_proc=None) -> None:
    if not Path(tx_python).exists():
        raise DiarizationError(f"transcription venv python not found: {tx_python}")
    env = _clean_env(HF_TOKEN=token, HF_HUB_DISABLE_PROGRESS_BARS="1",
                     PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    cmd = [str(tx_python), str(DIARIZE_SCRIPT), "--audio", str(wav),
           "--whisper-json", str(json_path), "--output", str(merged), "--progress"]
    if mn:
        cmd += ["--min-speakers", str(mn)]
    if mx:
        cmd += ["--max-speakers", str(mx)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1, env=env,
                            encoding="utf-8", errors="replace",   # child emits UTF-8
                            **osutil.popen_kwargs())
    if on_proc:
        on_proc(proc)
    tail: list[str] = []
    for line in proc.stdout:
        tail.append(line)
        del tail[:-60]
        m = _RE_DP.match(line.strip())
        if m:
            emit("diarize", WHISPER_W + (1.0 - WHISPER_W) * float(m.group(1)), m.group(2))
    rc = proc.wait()
    if rc == 2:
        raise TokenMissingError("HuggingFace auth/token failure:\n" + "".join(tail[-20:]))
    if rc != 0:
        raise DiarizationError(f"diarization failed (exit {rc}):\n" + "".join(tail[-20:]))
    if not merged.exists():
        raise DiarizationError("diarization produced no merged output")


def _write_output(output: Path, txt_path: Path, merged: Optional[Path], *,
                  meeting, lang, model, duration, min_speakers, max_speakers,
                  diar_ok, warn) -> None:
    body = (merged if merged else txt_path).read_text("utf-8")
    d = int(duration)
    h, mm, s = d // 3600, d % 3600 // 60, d % 60
    spk = f'{min_speakers or "auto"}–{max_speakers or "auto"}'
    diar = "ok" if diar_ok else ("disabled" if warn is None else f"unavailable ({warn})")
    head = [
        f"# Meeting: {meeting}",
        f"# Duration: {h:02d}:{mm:02d}:{s:02d}   Language: {lang}   Model: {model}",
        f"# Speakers: {spk}   Diarization: {diar}",
        "",
    ]
    output.write_text("\n".join(head) + body.rstrip() + "\n", "utf-8")


# ---- entry point --------------------------------------------------------------

def transcribe_meeting(wav_path, out_dir, *, lang="id", min_speakers=None,
                       max_speakers=None, model="large-v3", hf_token=None,
                       diarize=True, strict=False, tx_python=TX_PYTHON,
                       progress_cb: Optional[ProgressCB] = None, on_proc=None) -> dict:
    """Transcribe (+ diarize) a 16 kHz mono WAV. Blocks; run on a worker thread.

    progress_cb(stage, fraction, message) is invoked on the CALLING thread with a
    monotonic 0..1 fraction. on_proc(popen) is called with each spawned child so
    the caller can kill it on quit. Returns output paths + a `diarized` flag.
    """
    wav = Path(wav_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    emit = _Mono(progress_cb or _noop)

    model_file = MODELS_DIR / f"ggml-{model}.bin"
    if not model_file.exists():
        raise ModelNotFoundError(f"missing model file: {model_file}")
    vad_model = find_vad_model()
    if vad_model is None:
        raise ModelNotFoundError(
            f"missing VAD model: no {VAD_GLOB} in {MODELS_DIR}")
    if not wav.exists():
        raise PipelineError(f"recording not found: {wav}")

    prefix = out / "transcript"
    json_path = out / "transcript.json"
    txt_path = out / "transcript.txt"
    merged = out / "transcript.merged.txt"
    output = out / "output.txt"

    emit("prepare", 0.0, "reading audio…")
    duration = probe_duration(wav)

    emit("transcribe", 0.0, "starting transcription…")
    _run_whisper(wav, prefix, model_file, vad_model, lang, duration, emit, on_proc)
    if not json_path.exists():
        raise WhisperError("whisper-cli produced no JSON output")

    diar_ok, warn = False, None
    if diarize:
        token = (hf_token or os.environ.get("HF_TOKEN") or "").strip()
        if not token:
            warn = "no HuggingFace token"
            if strict:
                raise TokenMissingError(warn)
        else:
            try:
                emit("diarize", WHISPER_W, "starting diarization…")
                _run_diarize(wav, json_path, merged, token,
                             min_speakers, max_speakers, tx_python, emit, on_proc)
                diar_ok = merged.exists()
            except DiarizationError as e:
                warn = str(e).splitlines()[0]
                if strict:
                    raise

    _write_output(output, txt_path, merged if diar_ok else None,
                  meeting=out.name, lang=lang, model=model, duration=duration,
                  min_speakers=min_speakers, max_speakers=max_speakers,
                  diar_ok=diar_ok, warn=warn)
    emit("done", 1.0, "complete" if diar_ok else "complete (transcript only)")
    return {
        "wav": wav, "json": json_path, "txt": txt_path,
        "merged": merged if diar_ok else None, "output": output,
        "duration": duration, "diarized": diar_ok, "warning": warn,
    }
