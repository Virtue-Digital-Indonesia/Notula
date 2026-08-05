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
PAUSED = "paused"
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
LIVE = "live.txt"                # the rolling preview, kept as a fallback


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\- ]", "", (name or "").strip()).replace(" ", "-")
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:48] or "meeting"


def clean_name(name: str) -> str:
    """The canonical form of a meeting name: whitespace collapsed, length capped."""
    return " ".join((name or "").split())[:80]


# a meeting id is "<YYYY-MM-DD_HHMM>_<slug>" — the prefix is what sorts the library
_ID_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{4})_")


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
        "name": clean_name(name) or "Untitled meeting",
        "created": now.isoformat(timespec="seconds"),
        "device": device_name,
        "status": RECORDING,
        "duration": 0,
        "diarized": False,
        "warning": None,
    })
    return mid


def rename_meeting(root: str, mid: str, name: str) -> str:
    """Rename a meeting: update meta['name'] *and* move the folder so the saved
    session on disk carries the new name. Returns the (possibly new) id.

    The timestamp prefix is kept, so the library keeps sorting newest-first and
    the meeting's identity in time is preserved.

    This is safe to do while a recording is in flight: renaming a directory
    moves the inode, and already-open WAV handles keep writing into it. The
    caller only has to repoint the paths it will use *later* — see
    recorder.RecordingEngine.relocate().
    """
    name = clean_name(name)
    if not name:
        raise ValueError("a meeting needs a name")
    src = folder(root, mid)
    if not os.path.isdir(src):
        raise FileNotFoundError(f"no such meeting: {mid}")

    m = _ID_STAMP.match(mid)
    stamp = m.group(1) if m else datetime.now().strftime("%Y-%m-%d_%H%M")
    new_mid = base = f"{stamp}_{_slug(name)}"
    n = 2
    while True:
        dst = folder(root, new_mid)
        if not os.path.exists(dst):
            break
        try:
            # the same folder — either nothing moved, or it's a case-only change
            # that a case-insensitive volume reports as already existing
            if os.path.samefile(dst, src):
                break
        except OSError:
            pass
        new_mid, n = f"{base}-{n}", n + 1

    if new_mid != mid:
        os.rename(src, folder(root, new_mid))
    update_meta(root, new_mid, id=new_mid, name=name)
    return new_mid


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
