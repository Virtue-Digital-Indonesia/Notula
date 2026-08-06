"""
notula.permissions_win — the Windows side of the privacy story, which is thin.

Windows has nothing like macOS's TCC handshake for a plain desktop app: there is
no API to *request* microphone access and no prompt to trigger. There is only a
pair of settings toggles the user may have turned off, which we can read out of
the registry so we can say something useful instead of recording silence.

Computer audio needs no permission at all here — WASAPI loopback taps the render
endpoint directly, so `screen_recording_ok()` is simply True. That's the one
place the Windows port is meaningfully nicer than the macOS original: no Screen
Recording grant, and no relaunch to make it take effect.

Also used as the fallback on any non-macOS platform.
"""

from __future__ import annotations

import os
import subprocess
import sys

# AVAuthorizationStatus values, mirrored so callers stay platform-blind
NOT_DETERMINED = 0
RESTRICTED = 1
DENIED = 2
AUTHORIZED = 3

MIC_SETTINGS_PATH = "Settings › Privacy & security › Microphone"
SCREEN_SETTINGS_PATH = ""          # nothing to grant: loopback isn't screen capture

_CONSENT = (r"SOFTWARE\Microsoft\Windows\CurrentVersion"
            r"\CapabilityAccessManager\ConsentStore\microphone")


def _consent_value(subkey: str = "") -> str | None:
    """Read a ConsentStore Value ('Allow' / 'Deny'), or None if it isn't set."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        path = _CONSENT + (("\\" + subkey) if subkey else "")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return str(winreg.QueryValueEx(k, "Value")[0])
    except OSError:
        return None


def _app_key() -> str:
    """This process's key under NonPackaged: its image path with backslashes
    replaced by '#', which is how Windows records per-desktop-app consent.

    Always sys.executable, never sys.argv[0]: Windows keys on the image path of
    the process that opened the audio endpoint. Frozen, that's Notula.exe; from
    source it's the venv's pythonw.exe — but never 'notula_win.py', which is what
    argv[0] would give and which no such key can ever exist for. (From source
    this therefore reads consent for the interpreter, which any Python app
    shares. That's genuinely what the registry says, and it's the reason this
    per-app check is advisory and the NonPackaged check below is the important
    one.)
    """
    return "NonPackaged\\" + os.path.abspath(sys.executable).replace("\\", "#")


def mic_status() -> int:
    """AUTHORIZED unless the user has explicitly turned microphone access off.

    Never NOT_DETERMINED: Windows won't prompt for a desktop app, so there is no
    undecided state to resolve — treating it as undecided would make the caller
    wait for a prompt that never comes.

    A missing key means allowed, which is why each test is for "Deny" explicitly
    rather than for absence: desktop apps often don't appear in the Settings list
    at all until they've been seen using the microphone.
    """
    if _consent_value() == "Deny":
        return DENIED           # "Let apps access your microphone" is off
    if _consent_value("NonPackaged") == "Deny":
        # "Let desktop apps access your microphone". This is the toggle that
        # actually governs a Win32 app like Notula, and the single likeliest
        # reason for a recording that comes out silent — without it we'd report
        # AUTHORIZED, start recording, and hand back an empty WAV.
        return DENIED
    if _consent_value(_app_key()) == "Deny":
        return DENIED           # this executable was denied specifically
    return AUTHORIZED


def request_mic(callback) -> None:
    """No prompt exists to raise, so report the current state and move on."""
    try:
        callback(mic_status() == AUTHORIZED)
    except Exception:
        pass


def open_privacy_pane(which: str = "Microphone") -> None:
    """Deep-link into Settings › Privacy at the relevant page."""
    page = "privacy-microphone" if which.lower().startswith("mic") else "privacy"
    try:
        os.startfile(f"ms-settings:{page}")            # noqa: S606 (Windows-only)
    except Exception:
        # CREATE_NO_WINDOW, or this fallback flashes a console over the UI
        import osutil
        subprocess.Popen(["cmd", "/c", "start", "", f"ms-settings:{page}"],
                         **osutil.popen_kwargs(new_group=False))


def screen_recording_ok() -> bool:
    """Always true: WASAPI loopback is not a screen capture and needs no grant."""
    return True


def request_screen_recording() -> None:
    pass
