"""
notula.version — the one place the version number lives.

It used to be written out in setup.py, installer.iss, build_dmg.sh and two
documents, which is four opportunities to ship a DMG and an EXE claiming
different versions of the same release.

Two forms, because Apple's tooling is fussy: CFBundleVersion and
CFBundleShortVersionString are documented as dot-separated integers, so the
bundle gets VERSION_SHORT while everything a human reads — DMG and installer
filenames, the UI, the changelog — gets the full VERSION.

Deliberately importable on its own: setup.py and the build scripts read it
without dragging in numpy, sounddevice or pyobjc.
"""

VERSION = "2.0.0-beta3"
VERSION_SHORT = "2.0.0"     # numeric only, for Info.plist

# 2.0 because this is the release that made Notula cross-platform: a second
# shell on WebView2, computer audio via WASAPI loopback, and all the behaviour
# moved into appcore.py so neither platform is a fork of the other.
#
# beta2 supersedes beta1, which shipped a setup script that could never repair a
# half-downloaded model: a 2 GB fragment of a 2.9 GB file counted as "installed",
# so transcription failed with nothing but "whisper-cli failed" and re-running
# setup skipped the broken file forever.
#
# beta3 adds a second transcription engine: OpenAI's gpt-4o-transcribe-diarize
# (speaker labels) and gpt-transcribe (plain) through the audio API, with the
# estimated price and time shown before the upload. Parts are streamed and run
# four at a time, and a failed run keeps its finished parts for the retry. The meetings list also remembers how
# it was left (folded, row count).
#
# Still beta: the Windows half has not yet recorded a real meeting, and the
# cloud engine has been exercised against a stub of the API, not the real one.

if __name__ == "__main__":
    import sys
    print(VERSION_SHORT if "--short" in sys.argv else VERSION)
