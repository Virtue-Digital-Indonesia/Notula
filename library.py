"""
notula.library — the on-disk meeting library.

Each meeting is a folder under the library root:

    <library>/2026-07-23_1430_weekly-sync/
        audio.wav               the recording (16 kHz mono)
        meta.json               name, timestamps, device, status, duration…
        transcript.json         whisper raw (after transcription)
        transcript.txt          plain transcript
        transcript.merged.txt   speaker-labeled transcript
        output.txt              the canonical file you process further

The folder name is the meeting id. meta.json is the source of truth for
everything the UI shows; it's written atomically so a crash can't corrupt it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime

# status values
RECORDING = "recording"
IMPORTING = "importing"
RECORDED = "recorded"
TRANSCRIBING = "transcribing"
TRANSCRIBED = "transcribed"
ERROR = "error"

AUDIO = "audio.wav"
META = "meta.json"
OUTPUT = "output.txt"
TRANSCRIPT_JSON = "transcript.json"
TRANSCRIPT_TXT = "transcript.txt"
TRANSCRIPT_MERGED = "transcript.merged.txt"


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\- ]", "", (name or "").strip()).replace(" ", "-")
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:48] or "meeting"


def ensure_root(root: str) -> str:
    os.makedirs(root, exist_ok=True)
    return root


# ---- paths --------------------------------------------------------------------

def folder(root: str, mid: str) -> str:
    return os.path.join(root, mid)


def path(root: str, mid: str, name: str) -> str:
    return os.path.join(root, mid, name)


def audio_path(root: str, mid: str) -> str:
    return path(root, mid, AUDIO)


def output_path(root: str, mid: str) -> str:
    return path(root, mid, OUTPUT)


# ---- meta read / write --------------------------------------------------------

def _meta_file(root: str, mid: str) -> str:
    return path(root, mid, META)


def read_meta(root: str, mid: str) -> dict:
    try:
        with open(_meta_file(root, mid), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def write_meta(root: str, mid: str, meta: dict) -> None:
    p = _meta_file(root, mid)
    d = os.path.dirname(p)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=META + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_meta(root: str, mid: str, **fields) -> dict:
    meta = read_meta(root, mid)
    meta.update(fields)
    write_meta(root, mid, meta)
    return meta


# ---- create / list ------------------------------------------------------------

def create_meeting(root: str, name: str, device_name: str = "") -> str:
    """Create a new meeting folder and return its id."""
    ensure_root(root)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H%M")
    mid = f"{stamp}_{_slug(name)}"
    # avoid a collision if two meetings start in the same minute
    base, n = mid, 2
    while os.path.exists(folder(root, mid)):
        mid = f"{base}-{n}"
        n += 1
    os.makedirs(folder(root, mid), exist_ok=True)
    write_meta(root, mid, {
        "id": mid,
        "name": name.strip() or "Untitled meeting",
        "created": now.isoformat(timespec="seconds"),
        "device": device_name,
        "status": RECORDING,
        "duration": 0,
        "diarized": False,
        "warning": None,
    })
    return mid


def _fmt_duration(sec) -> str:
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        sec = 0
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _fmt_created(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%b %d, %H:%M")
    except (TypeError, ValueError):
        return iso or ""


def describe(root: str, mid: str) -> dict:
    """A UI-ready summary of one meeting."""
    meta = read_meta(root, mid)
    return {
        "id": mid,
        "name": meta.get("name") or mid,
        "created": _fmt_created(meta.get("created", "")),
        "duration": _fmt_duration(meta.get("duration", 0)),
        "status": meta.get("status", RECORDED),
        "diarized": bool(meta.get("diarized")),
        "warning": meta.get("warning"),
        "hasOutput": os.path.exists(output_path(root, mid)),
        "hasAudio": os.path.exists(audio_path(root, mid)),
    }


def list_meetings(root: str) -> list[dict]:
    """All meetings, newest first."""
    if not os.path.isdir(root):
        return []
    ids = []
    for entry in os.scandir(root):
        if entry.is_dir() and os.path.exists(os.path.join(entry.path, META)):
            ids.append(entry.name)
    ids.sort(reverse=True)   # ids start with a sortable timestamp
    return [describe(root, mid) for mid in ids]
