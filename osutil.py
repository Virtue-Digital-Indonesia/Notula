"""
notula.osutil — the handful of OS differences the engine actually cares about.

Dependency-free shims over things POSIX and Windows simply spell differently:
where config lives, how you spawn a child you can kill later, how you kill it,
and how you hand a path to the desktop. Everything else in the engine stays
platform-blind by importing from here.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import webbrowser

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"

APP_DIR_NAME = "Notula"          # %APPDATA%\Notula, %LOCALAPPDATA%\Notula


# ---- subprocesses -------------------------------------------------------------

def popen_kwargs(new_group: bool = True) -> dict:
    """Spawn flags for a child process, for Popen *and* run.

    POSIX: `start_new_session` gives the child its own process group, so
    `kill_tree` can reach anything it spawned in turn.

    Windows: `CREATE_NEW_PROCESS_GROUP` is the rough equivalent, and
    `CREATE_NO_WINDOW` is not optional — without it every ffmpeg/ffprobe/whisper
    call pops a console window over the UI, so a transcription visibly strobes.
    """
    if WINDOWS:
        flags = subprocess.CREATE_NO_WINDOW
        if new_group:
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        return {"creationflags": flags}
    return {"start_new_session": True} if new_group else {}


def kill_tree(proc, timeout: float = 3.0) -> None:
    """Terminate a child *and its descendants*, then reap it.

    Killing only the direct child is not enough: whisper-server holds a loaded
    model in RAM, and on Windows the launcher is often a shim that re-execs the
    real binary, so the process we hold a handle to is not the one using the GPU.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=timeout,
                           **popen_kwargs(new_group=False))
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=timeout)
    except Exception:
        pass


def exe(name: str) -> str:
    """Executable file name for this platform: 'ffmpeg' -> 'ffmpeg.exe'."""
    return f"{name}.exe" if WINDOWS else name


# ---- well-known folders -------------------------------------------------------

def config_dir(app: str) -> str:
    """Where this app's settings file lives.

    macOS/Linux keep the existing ~/.config/<app> (honoring $XDG_CONFIG_HOME);
    Windows uses %APPDATA%\\Notula, which is the roaming profile users expect
    settings to follow them in.
    """
    if WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return os.path.join(base, APP_DIR_NAME)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, app)


def data_dir() -> str:
    """Machine-local app data: downloaded models, a bundled ffmpeg, the tx venv.
    Deliberately *not* roaming — these are gigabytes and machine-specific."""
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, APP_DIR_NAME)
    return os.path.expanduser(f"~/Library/Application Support/{APP_DIR_NAME}") if MACOS \
        else os.path.expanduser(f"~/.local/share/{APP_DIR_NAME.lower()}")


def documents_dir() -> str:
    """The user's Documents folder.

    On Windows this is not reliably ~/Documents: it gets redirected into
    OneDrive and is localized ('Documenten', '文档'), and only the shell knows
    where it actually points.
    """
    if WINDOWS:
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            CSIDL_PERSONAL, SHGFP_TYPE_CURRENT = 5, 0
            if ctypes.windll.shell32.SHGetFolderPathW(
                    None, CSIDL_PERSONAL, None, SHGFP_TYPE_CURRENT, buf) == 0 and buf.value:
                return buf.value
        except Exception:
            pass
    return os.path.expanduser("~/Documents")


# ---- desktop integration ------------------------------------------------------

def ensure_portaudio() -> None:
    """Make a loadable PortAudio findable — call before importing sounddevice.

    sounddevice chooses its bundled DLL from `platform.machine()`, which on
    Windows reports the *hardware* architecture rather than the process's. On an
    ARM64 machine that returns 'ARM64' even for an x64 interpreter running under
    emulation, so it asks for `libportaudioarm64.dll` — a file its own wheel
    does not contain — and the import dies with "cannot load library … error
    0x7e". Every x64 Python on an ARM64 Windows box hits this, which now includes
    Snapdragon laptops and any Windows VM on Apple Silicon.

    Before that fallback, though, sounddevice tries
    `ctypes.util.find_library('portaudio')`, and on Windows that simply walks
    PATH. So publishing the x64 DLL it *did* ship under the name `portaudio.dll`
    in a directory we put on PATH is enough to steer it, without writing
    anything into site-packages.

    A no-op everywhere else, and a no-op on Windows whenever the DLL sounddevice
    is about to ask for actually exists.
    """
    if not WINDOWS:
        return
    import platform
    suffix = ("arm64" if platform.machine().lower() in ("arm64", "aarch64")
              else platform.architecture()[0])
    asio = "-asio" if "SD_ENABLE_ASIO" in os.environ else ""
    wanted = f"libportaudio{suffix}{asio}.dll"
    try:
        import _sounddevice_data
        binaries = os.path.join(next(iter(_sounddevice_data.__path__)),
                                "portaudio-binaries")
        if os.path.isfile(os.path.join(binaries, wanted)):
            return                                  # sounddevice will be fine
        have = os.path.join(binaries, f"libportaudio64bit{asio}.dll")
        if not os.path.isfile(have):
            return                                  # nothing we can substitute
        shim_dir = os.path.join(data_dir(), "bin")
        shim = os.path.join(shim_dir, "portaudio.dll")
        os.makedirs(shim_dir, exist_ok=True)
        if (not os.path.isfile(shim)
                or os.path.getsize(shim) != os.path.getsize(have)):
            import shutil
            shutil.copyfile(have, shim)
        os.environ["PATH"] = shim_dir + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass          # leave sounddevice to report its own failure


def open_path(path: str) -> None:
    """Open a file or folder with the desktop's default handler."""
    if WINDOWS:
        os.startfile(path)                                  # noqa: S606 (Windows-only)
    elif MACOS:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def open_url(url: str) -> None:
    webbrowser.open(url)
