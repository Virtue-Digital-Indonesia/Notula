# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Notula on Windows.

    .venv\\Scripts\\pyinstaller tools\\notula_win.spec     -> dist\\Notula\\Notula.exe

One-dir rather than one-file, deliberately: one-file unpacks ~100 MB to a temp
directory on every launch, and the app already shells out to external binaries
whose paths users need to see. One-dir starts fast and stays inspectable.

WHAT'S BUNDLED vs NOT — same split as the macOS build:
  Bundled : the GUI app + capture (pywebview/WebView2, sounddevice, PyAudioWPatch,
            numpy) and the UI assets.
  External: transcription still shells out to whisper-cli.exe and to the
            transcription venv's python running diarize_and_merge.py, resolved by
            toolpaths.py. diarize_and_merge.py rides along as a real file because
            that external python has to execute it.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Absolute, derived from SPECPATH (which PyInstaller injects, along with os).
# PyInstaller anchors `datas`, `scripts` and `icon` to the spec's own directory,
# but plain Python in the spec body — an os.path.exists() guard, say — resolves
# against the current working directory instead. Mixing the two silently drops
# whatever the guard was protecting. Absolute paths make the question moot.
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
ICON = os.path.join(ROOT, "assets", "Notula.ico")

datas = [
    (os.path.join(ROOT, "assets", "notula_ui.html"), "assets"),
    (os.path.join(ROOT, "assets", "fonts", "plex_b64.json"), "assets/fonts"),
    # must stay a real file on disk: the external tx-venv python runs it
    (os.path.join(ROOT, "diarize_and_merge.py"), "."),
]
# pywebview ships the WebView2 interop DLLs as package data; without these the
# window never opens.
datas += collect_data_files("webview")

hiddenimports = [
    # our own modules — imported dynamically or only on this platform, so
    # PyInstaller's static analysis doesn't always see them
    "appcore", "config", "library", "recorder", "pipeline", "dsp", "osutil",
    "toolpaths", "live", "permissions", "permissions_win",
    "sysaudio", "sysaudio_win", "pyaudiowpatch",
    # tkinter powers the confirm / alert / token dialogs
    "tkinter", "tkinter.messagebox", "tkinter.filedialog",
]
hiddenimports += collect_submodules("webview")

a = Analysis(
    [os.path.join(ROOT, "notula_win.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # the heavy ML stack lives in the separate transcription venv by design;
    # bundling it would add gigabytes for code this process never imports
    excludes=["torch", "pyannote", "matplotlib", "scipy", "PyQt5", "PySide2"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Notula",
    debug=False,
    strip=False,
    upx=False,
    # A GUI app: no console window behind it. The cost is that sys.stdout and
    # sys.stderr are None at runtime, which notula_win.py compensates for —
    # fatal errors go to a MessageBox and --selftest to a log file.
    console=False,
    icon=ICON if os.path.exists(ICON) else None,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="Notula",
)
