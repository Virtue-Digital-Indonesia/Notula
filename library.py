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
import time
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
CLOUD_PARTS = ".cloud-parts"     # finished cloud parts of an interrupted run (see cloud.py)


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


def _replace_with_retry(tmp: str, dst: str, attempts: int = 5) -> None:
    """os.replace, retried briefly.

    On POSIX, rename(2) simply cannot fail because someone has the destination
    open. On Windows it is MoveFileExW, which returns a sharing violation
    whenever anything holds the destination without FILE_SHARE_DELETE — a search
    indexer, a backup agent, or an antivirus scanner that opened meta.json the
    moment we created it. Those holds last milliseconds, so retrying turns a hard
    failure into a hiccup instead of a lost status update.
    """
    for i in range(attempts):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))


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
        _replace_with_retry(tmp, p)
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


def rename_meeting(root: str, mid: str, name: str, move: bool = True) -> str:
    """Rename a meeting: update meta['name'] *and* move the folder so the saved
    session on disk carries the new name. Returns the (possibly new) id.

    The timestamp prefix is kept, so the library keeps sorting newest-first and
    the meeting's identity in time is preserved.

    On POSIX this is safe to do while a recording is in flight: renaming a
    directory moves the inode, and already-open WAV handles keep writing into it.
    The caller only has to repoint the paths it will use *later* — see
    recorder.RecordingEngine.relocate().

    Windows does not work that way — it refuses to move a directory whose files
    are open — so `move=False` renames only the display name and leaves the
    folder alone. The caller repeats the rename with move=True once the handles
    are closed; see AppCore._rename.
    """
    name = clean_name(name)
    if not name:
        raise ValueError("a meeting needs a name")
    src = folder(root, mid)
    if not os.path.isdir(src):
        raise FileNotFoundError(f"no such meeting: {mid}")
    if not move:
        update_meta(root, mid, name=name)
        return mid

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


def _seconds(sec) -> float:
    try:
        v = float(sec)
        return v if v > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _fmt_duration(sec) -> str:
    sec = int(_seconds(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _fmt_created(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%b %d, %H:%M")
    except (TypeError, ValueError):
        return iso or ""


def created_ts(mid: str, iso: str) -> float:
    """Epoch seconds for the meeting, for sorting and date filtering.

    `created` is formatted for display ("Aug 12, 14:30"), which the page can't
    compare against anything. The id's own timestamp prefix is the fallback, and
    it is always present because create_meeting builds the id from it.
    """
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        pass
    m = _ID_STAMP.match(mid or "")
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d_%H%M").timestamp()
        except ValueError:
            pass
    return 0.0


def cloud_resume(meeting_dir: str) -> dict | None:
    """What an interrupted cloud run left behind: its model, language and part
    length, and how many seconds of audio are already transcribed and saved.

    Totalled from the part file names (their spans, in ms), so listing the
    library never opens them.
    """
    d = os.path.join(meeting_dir, CLOUD_PARTS)
    try:
        with open(os.path.join(d, "manifest.json"), encoding="utf-8") as fh:
            info = json.load(fh)
        names = os.listdir(d)
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    done = 0.0
    for name in names:
        m = re.match(r"^part-(\d+)-(\d+)\.json$", name)
        if m:
            done += max(0, int(m.group(2)) - int(m.group(1))) / 1000.0
    return {"model": info.get("model"), "lang": info.get("lang"),
            "chunk_s": info.get("chunk_s"), "done_s": done}


def describe(root: str, mid: str) -> dict:
    """A UI-ready summary of one meeting."""
    meta = read_meta(root, mid)
    return {
        "id": mid,
        "name": meta.get("name") or mid,
        "created": _fmt_created(meta.get("created", "")),
        "ts": created_ts(mid, meta.get("created", "")),
        "duration": _fmt_duration(meta.get("duration", 0)),
        # raw seconds too: the page prices a cloud transcription before it starts
        "seconds": _seconds(meta.get("duration", 0)),
        "status": meta.get("status", RECORDED),
        "diarized": bool(meta.get("diarized")),
        "warning": meta.get("warning"),
        "hasOutput": os.path.exists(output_path(root, mid)),
        "hasAudio": os.path.exists(audio_path(root, mid)),
        # how the last transcription was made, and what it cost if it was billed
        "engine": meta.get("engine") or "",
        "model": meta.get("model") or "",
        "cost_usd": meta.get("cost_usd"),
        # parts a failed cloud run already paid for, which a retry reuses
        "cloud_resume": cloud_resume(folder(root, mid)),
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
