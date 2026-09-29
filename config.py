"""
notula.config — user settings, stored as one JSON file that can never brick launch.

Config lives at ~/.config/notula/notula.json (honoring $XDG_CONFIG_HOME), or
%APPDATA%\\Notula\\notula.json on Windows. Every load starts from DEFAULTS,
overlays the file, and coerces every value back into range, so a hand-edited or
half-written file falls back to sane values instead of crashing the app. Writes
are atomic (tempfile + os.replace) and owner-only.
"""

from __future__ import annotations

import math
import os
import sys
import json
import tempfile

import osutil

APP = "notula"


# ---- defaults -----------------------------------------------------------------

def _default_library() -> str:
    # not ~/Documents: on Windows that folder is localized and often redirected
    # into OneDrive, and only the shell knows where it really is
    return os.path.join(osutil.documents_dir(), "Notula")


DEFAULTS = {
    "library": _default_library(),   # where meeting folders are created
    "mic_device": None,              # PortAudio index of the microphone (None = default)
    "system_capture": True,          # capture computer audio via ScreenCaptureKit
    "lang": "id",                    # whisper language code
    "model": "large-v3",             # ggml-<model>.bin under the whisper-cpp models dir
    "min_speakers": 0,               # 0 = auto-detect
    "max_speakers": 0,               # 0 = auto-detect
    "auto_transcribe": False,        # transcribe automatically when a recording stops
    "hf_token": "",                  # HuggingFace token for pyannote (or set $HF_TOKEN)
    "engine": "local",               # local (whisper-cli + pyannote) | cloud (OpenAI)
    "cloud_model": "gpt-4o-transcribe-diarize",   # which OpenAI model (see cloud.MODELS)
    "openai_api_key": "",            # OpenAI API key for the cloud engine (or $OPENAI_API_KEY)
    "cloud_rates": {},               # $/min each cloud model actually cost on this user's runs
    "cloud_parallel": True,          # send diarized parts side by side (fast) or one at a time
    "live_enabled": False,           # live transcript while recording (toggle any time)
    "live_model": "large-v3-turbo",  # which model the live tier uses (see live.MODELS)
    "theme": "auto",                 # auto | light | dark
    "heartbeat_s": 0.5,              # UI refresh interval
    "meetings_collapsed": False,     # meetings list folded away
    "meetings_limit": 25,            # how many rows to show at once (0 = all)
}

# numeric bounds — a corrupt value is clamped, never fatal.
_NUM_BOUNDS = {
    "min_speakers": (0, 20),
    "max_speakers": (0, 20),
    "heartbeat_s": (0.1, 5.0),
    "meetings_limit": (0, 1000),      # 0 = show everything
}
_ENUMS = {
    "theme": ("auto", "light", "dark"),
    "engine": ("local", "cloud"),
}


# ---- paths --------------------------------------------------------------------

def config_path() -> str:
    return os.path.join(osutil.config_dir(APP), f"{APP}.json")


# ---- load / sanitize / save ---------------------------------------------------

def _sanitize(cfg: dict) -> dict:
    for k, (lo, hi) in _NUM_BOUNDS.items():
        try:
            v = float(cfg.get(k))
            if not math.isfinite(v):
                raise ValueError
        except (TypeError, ValueError):
            v = float(DEFAULTS[k])
        v = min(hi, max(lo, v))
        cfg[k] = int(round(v)) if isinstance(DEFAULTS[k], int) else v

    for k, allowed in _ENUMS.items():
        if cfg.get(k) not in allowed:
            cfg[k] = DEFAULTS[k]

    # keep min ≤ max (0 = auto and is exempt) so pyannote never rejects the pair
    mn, mx = cfg.get("min_speakers"), cfg.get("max_speakers")
    if mn and mx and mn > mx:
        cfg["min_speakers"], cfg["max_speakers"] = mx, mn

    # mic_device is None (system default) or a non-negative PortAudio index
    v = cfg.get("mic_device")
    if v is not None:
        try:
            cfg["mic_device"] = max(0, int(v))
        except (TypeError, ValueError):
            cfg["mic_device"] = None

    for k in ("library", "lang", "model", "hf_token", "live_model",
              "cloud_model", "openai_api_key"):
        if not isinstance(cfg.get(k), str):
            cfg[k] = DEFAULTS[k]
    cfg["openai_api_key"] = cfg["openai_api_key"].strip()
    # an unknown cloud model (a typo, or one retired upstream) falls back to the
    # default rather than failing at upload time
    import cloud
    cfg["cloud_model"] = cloud.resolve_model(cfg["cloud_model"])
    # learned rates: known models only, and only plausible figures — a garbage
    # value here would put a garbage price in front of the user
    rates = cfg.get("cloud_rates")
    clean = {}
    if isinstance(rates, dict):
        for k, v in rates.items():
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if k in cloud.MODELS and math.isfinite(v) and 0.0001 <= v <= 1.0:
                clean[k] = round(v, 6)
    cfg["cloud_rates"] = clean
    cfg["library"] = os.path.expanduser(cfg["library"].strip() or DEFAULTS["library"])
    cfg["lang"] = (cfg["lang"].strip() or DEFAULTS["lang"])[:8]

    cfg["auto_transcribe"] = bool(cfg.get("auto_transcribe"))
    cfg["system_capture"] = bool(cfg.get("system_capture"))
    cfg["live_enabled"] = bool(cfg.get("live_enabled"))
    cfg["cloud_parallel"] = bool(cfg.get("cloud_parallel", True))
    cfg["meetings_collapsed"] = bool(cfg.get("meetings_collapsed"))
    return cfg


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("config is not a JSON object")
        cfg.update(data)
    except FileNotFoundError:
        pass
    except (ValueError, TypeError, OSError) as e:
        print(f"notula: bad config, using defaults ({e})", file=sys.stderr)
    return _sanitize(cfg)


def _atomic_write(path: str, data: bytes) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    # mkstemp gives a unique name AND creates it 0600 from the start — the file
    # can hold an HF token, so it's never briefly world-readable.
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)      # atomic; a crash mid-write leaves the old file intact
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(cfg: dict) -> None:
    try:
        payload = json.dumps(_sanitize(dict(cfg)), indent=2).encode("utf-8")
        _atomic_write(config_path(), payload)
    except OSError as e:
        print(f"notula: could not save config ({e})", file=sys.stderr)


def hf_token(cfg: dict) -> str:
    """Resolve the HuggingFace token: $HF_TOKEN wins, then the stored config value."""
    return (os.environ.get("HF_TOKEN") or cfg.get("hf_token") or "").strip()


def openai_key(cfg: dict) -> str:
    """Resolve the OpenAI key the same way: $OPENAI_API_KEY wins, then config."""
    return (os.environ.get("OPENAI_API_KEY") or cfg.get("openai_api_key") or "").strip()
