"""
notula.sysaudio — computer/system audio capture, per platform.

Both backends do the same job — tap everything the machine is playing, with no
loopback driver installed — and expose the same source interface, so the recorder
picks one here and never asks which. What differs is what the OS demands in
return:

    macOS    ScreenCaptureKit, gated behind the Screen Recording permission
             (and a relaunch after granting it). Delivers 16 kHz mono directly.
    Windows  WASAPI loopback, no permission at all. Delivers the render
             endpoint's mix format, converted on the way in.

The strings below exist because the UI has to explain that difference to the
user; hardcoding either platform's wording in the HTML is what made the original
page macOS-only.
"""

from __future__ import annotations

import sys

if sys.platform == "darwin":
    from sysaudio_mac import AVAILABLE, SCKSystemAudioSource as SystemAudioSource  # noqa: F401
    BACKEND = "screencapturekit"
    NEEDS_PERMISSION = True
    DESCRIPTION = "Captures all computer audio via ScreenCaptureKit — no extra drivers."
    PERMISSION_HINT = "Needs Screen Recording permission — macOS will ask when you record."
    UNAVAILABLE_REASON = "Computer audio needs macOS 13 or newer."
elif sys.platform == "win32":
    from sysaudio_win import AVAILABLE, WasapiSystemAudioSource as SystemAudioSource  # noqa: F401
    BACKEND = "wasapi"
    NEEDS_PERMISSION = False
    DESCRIPTION = "Captures all computer audio via WASAPI loopback — no extra drivers."
    PERMISSION_HINT = ""
    UNAVAILABLE_REASON = ("Computer audio needs the PyAudioWPatch package — "
                          "run: pip install PyAudioWPatch")
else:                                                # pragma: no cover
    AVAILABLE = False
    SystemAudioSource = None
    BACKEND = None
    NEEDS_PERMISSION = False
    DESCRIPTION = ""
    PERMISSION_HINT = ""
    UNAVAILABLE_REASON = "Computer audio capture isn't supported on this platform."
