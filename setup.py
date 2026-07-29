"""
py2app build config for Notula — produces a standalone Notula.app (its own
embedded Python + pyobjc + sounddevice + numpy), so it launches as "Notula"
with its own icon rather than "Python".

    ./.venv/bin/python setup.py py2app        # standalone build -> dist/Notula.app
    ./.venv/bin/python setup.py py2app -A      # fast alias build (dev only)

WHAT'S BUNDLED vs NOT
  Bundled : the GUI app + recording/monitor (pyobjc, sounddevice, numpy, SCK).
  External: transcription still shells out to whisper-cli (Homebrew) and to the
            transcription venv's python running diarize_and_merge.py.  Those are
            machine-local, resolved by absolute path (see pipeline.py); the app
            is NOT self-contained for transcription, by design.  diarize_and_merge.py
            rides along as a *resource* (a real file) because the tx venv runs it.
"""

from setuptools import setup

APP = ["notula.py"]

DATA_FILES = [
    ("assets", ["assets/notula_ui.html"]),
    ("assets/fonts", ["assets/fonts/plex_b64.json"]),
    "diarize_and_merge.py",      # run by the tx venv python — must be a real file
]

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "assets/Notula.icns",
    # packages with data/dylibs must be copied OUT of the zip (a dylib can't be
    # dlopen'd from inside python3xx.zip). _sounddevice_data holds libportaudio.dylib.
    "packages": ["numpy", "cffi", "_sounddevice_data"],
    "includes": [
        "objc", "Foundation", "AppKit", "WebKit", "CoreMedia",
        "AVFoundation", "ScreenCaptureKit", "libdispatch", "Quartz",
        "PyObjCTools", "PyObjCTools.AppHelper",
        # our own modules (all imported by notula.py, but be explicit)
        "config", "library", "recorder", "pipeline", "permissions",
        "sysaudio", "appicon",
    ],
    "excludes": ["tkinter", "torch", "pyannote", "py2app"],
    "plist": {
        "CFBundleName": "Notula",
        "CFBundleDisplayName": "Notula",
        "CFBundleIdentifier": "id.val.notula",
        "CFBundleVersion": "1.2",
        "CFBundleShortVersionString": "1.2",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "LSMinimumSystemVersion": "13.0",
        # mandatory: touching the mic without this string makes macOS kill the app
        "NSMicrophoneUsageDescription":
            "Notula records your microphone so it can transcribe your meetings.",
        "NSHumanReadableCopyright": "MIT-licensed · Built by Virtue Digital Indonesia",
        # audio/video files Notula can open (Dock-icon drop, "Open With ▸ Notula")
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "Audio or video recording",
            "CFBundleTypeRole": "Viewer",
            "LSHandlerRank": "Alternate",
            "LSItemContentTypes": [
                "public.audio", "public.movie", "public.mpeg-4",
                "public.mpeg-4-audio", "com.apple.quicktime-movie",
                "public.mp3", "public.aac-audio", "com.microsoft.waveform-audio",
                "org.matroska.mkv", "public.avi", "public.aiff-audio",
            ],
        }],
        # Finder/Dock-launched apps get an ASCII locale that mangles non-ASCII
        # transcript text and subprocess output — force UTF-8.
        "LSEnvironment": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    },
}

setup(
    name="Notula",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
)
