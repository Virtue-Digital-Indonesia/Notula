"""
notula.permissions — microphone / system-audio privacy, per platform.

Callers use this module and never the backends directly, so the orchestration
layer can stay free of `if sys.platform` checks. The two backends expose the same
names and the same AVAuthorizationStatus-style constants; what differs is how
much they can actually do:

    macOS    a real TCC handshake — a status, a prompt, and a separate Screen
             Recording grant that computer audio depends on.
    Windows  no prompt exists for a desktop app; we can only read the consent
             toggles, and computer audio needs no permission whatsoever.
"""

from __future__ import annotations

import sys

if sys.platform == "darwin":
    from permissions_mac import (              # noqa: F401
        NOT_DETERMINED, RESTRICTED, DENIED, AUTHORIZED,
        MIC_SETTINGS_PATH, SCREEN_SETTINGS_PATH,
        mic_status, request_mic, open_privacy_pane,
        screen_recording_ok, request_screen_recording,
    )
else:
    from permissions_win import (              # noqa: F401
        NOT_DETERMINED, RESTRICTED, DENIED, AUTHORIZED,
        MIC_SETTINGS_PATH, SCREEN_SETTINGS_PATH,
        mic_status, request_mic, open_privacy_pane,
        screen_recording_ok, request_screen_recording,
    )
