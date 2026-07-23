"""
notula.permissions — macOS TCC (privacy) helpers for microphone + screen audio.

Plain Python opening the CoreAudio HAL (via PortAudio/sounddevice) never triggers
the microphone permission prompt, so an unauthorized launch silently captures
nothing. Asking AVFoundation for access explicitly is what makes macOS show the
prompt and remember the grant. ScreenCaptureKit (used for computer/system audio)
gates on the separate Screen Recording permission.
"""

from __future__ import annotations

import subprocess

from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

# AVAuthorizationStatus
NOT_DETERMINED = 0
RESTRICTED = 1
DENIED = 2
AUTHORIZED = 3


def mic_status() -> int:
    return AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)


def request_mic(callback) -> None:
    """Trigger the mic prompt if undetermined. `callback(granted: bool)` is
    invoked on an arbitrary thread — marshal to the main thread for any UI."""
    AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVMediaTypeAudio, callback)


def open_privacy_pane(which: str = "Microphone") -> None:
    """Open System Settings at a Privacy pane (Microphone or ScreenCapture)."""
    url = f"x-apple.systempreferences:com.apple.preference.security?Privacy_{which}"
    subprocess.Popen(["open", url])


def screen_recording_ok() -> bool:
    """Best-effort check for Screen Recording permission (needed for SCK audio).
    CGPreflightScreenCaptureAccess returns whether we're already granted."""
    try:
        from Quartz import CGPreflightScreenCaptureAccess
        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return True   # can't tell — let SCK surface the real prompt/error


def request_screen_recording() -> None:
    try:
        from Quartz import CGRequestScreenCaptureAccess
        CGRequestScreenCaptureAccess()
    except Exception:
        pass
