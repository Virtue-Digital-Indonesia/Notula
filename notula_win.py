#!/usr/bin/env python3
"""
Notula for Windows — the same app, in a WebView2 window instead of a WKWebView.

This file is to Windows what notula.py is to macOS: a shell that owns a window
and supplies the native services appcore.py asks for (clipboard, dialogs, file
pickers, dark mode, marshalling). All the behaviour is in appcore.py and shared
verbatim with the macOS build.

    py -3 notula_win.py            (see run.bat, which sets the venv up for you)

THE UI THREAD, HERE
-------------------
AppCore is single-threaded by design: its state has no locks because on macOS
everything runs on the AppKit main thread. pywebview has no such thread to
borrow — JS calls arrive on its own worker threads — so this shell creates one
**dispatch thread** and funnels every entry point through it: messages from the
page, heartbeat ticks, and anything a background worker hands back via
`on_main`. That thread plays the part the main thread plays on macOS, and the
same no-locks invariant holds.

Native dialogs therefore also run on the dispatch thread, which is why they're
tkinter (stdlib, thread-agnostic as long as one thread owns the widgets) rather
than anything owned by the GUI thread.

WHAT'S DIFFERENT FROM macOS
---------------------------
  * Computer audio needs no permission and no relaunch (WASAPI loopback).
  * Dropping a file **onto the window** isn't wired up: WebView2 gives the page
    a File object with no filesystem path, so there's nothing to hand ffmpeg.
    Use *Import audio…*, or pass paths on the command line — which is what
    "Open with" and dropping onto the .exe do.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import traceback

import appcore
import osutil

TICK_S = 0.1          # matches the macOS NSTimer: smooth level meters
WIN_W, WIN_H = 900, 700

# How long a single queued item may occupy the dispatch thread before we say so.
# Everything the user does goes through that thread, so when it stalls the whole
# window stops responding — and the cause is invisible without this.
STALL_WARN_S = 6.0

log = logging.getLogger("notula")


def setup_logging():
    """Log to %LOCALAPPDATA%\\Notula\\notula.log.

    A windowed build has no console and run.bat launches via pythonw, so without
    a file there is nowhere for a traceback to go and a misbehaving app is
    indistinguishable from a crashed one. Set NOTULA_DEBUG=1 for per-message
    detail.
    """
    path = os.path.join(osutil.data_dir(), "notula.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=512 * 1024, backupCount=1, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if os.environ.get("NOTULA_DEBUG") else logging.INFO)
    root.addHandler(handler)
    return path


# ---- native bits, via ctypes / winreg ----------------------------------------

def system_dark() -> bool:
    """Whether the desktop is in dark mode (Windows exposes this per-app)."""
    try:
        import winreg
        key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return int(winreg.QueryValueEx(k, "AppsUseLightTheme")[0]) == 0
    except (OSError, ValueError):
        return True                       # match the macOS fallback


def set_clipboard(text: str) -> bool:
    """Put text on the clipboard so it survives this process exiting.

    Deliberately not tkinter's clipboard: Tk serves its clipboard from the
    living interpreter, so the copied text vanishes the moment the dialog's root
    is destroyed. The Win32 clipboard takes ownership of the memory instead.
    """
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # Handles are pointer-sized; without these the default c_int return type
    # truncates them on 64-bit and the clipboard silently gets garbage.
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.restype = wintypes.HGLOBAL
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE

    buf = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buf)
    if not u32.OpenClipboard(None):
        return False
    handle = None
    try:
        u32.EmptyClipboard()
        handle = k32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        ptr = k32.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, buf, size)
        k32.GlobalUnlock(handle)
        if not u32.SetClipboardData(CF_UNICODETEXT, handle):
            return False
        handle = None                     # the clipboard owns the memory now
        return True
    finally:
        if handle:
            k32.GlobalFree(handle)
        u32.CloseClipboard()


class _Tk:
    """A hidden Tk root, created per dialog and torn down after.

    Kept short-lived on purpose: a long-lived Tk root on a non-GUI thread would
    need its own mainloop pumped forever, and we only ever want modal dialogs.

    -topmost is set on the root so that dialogs parented to it inherit it. That
    is not cosmetic: a Tk dialog that opens *behind* the WebView2 window is
    invisible, and since these dialogs are modal the app simply appears frozen
    with no indication of what it is waiting for.
    """

    def __enter__(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass
        return self.root

    def __exit__(self, *exc):
        try:
            self.root.destroy()
        except Exception:
            pass
        return False


def _to_front(win):
    """Drag a Tk window in front of the WebView2 window and give it focus."""
    for attempt in (lambda: win.attributes("-topmost", True),
                    win.lift,
                    win.focus_force):
        try:
            attempt()
        except Exception:
            pass


# ---- the Windows host --------------------------------------------------------

class JsApi:
    """Exposed to the page as window.pywebview.api — one method, matching the
    single postMessage channel the macOS shell uses."""

    def __init__(self, host):
        self._host = host

    def post(self, msg):
        log.debug("js -> %s", (msg or {}).get("action"))
        self._host.on_main(self._host.core.dispatch, msg)
        return True


class WinHost:
    """appcore.Host, implemented on pywebview + Win32."""

    def __init__(self):
        self.core = None
        self.api = JsApi(self)
        self._window = None
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._tick_pending = False
        self._loaded = False
        self._closing = False
        self._torn_down = False
        self._busy = None            # (name, started) of the item being dispatched
        self._stall_logged = False

    # ---- the dispatch thread ----

    def start_threads(self):
        threading.Thread(target=self._pump, name="notula-ui", daemon=True).start()
        threading.Thread(target=self._heartbeat, name="notula-tick", daemon=True).start()

    def _pump(self):
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            fn, args = item
            name = getattr(fn, "__name__", repr(fn))
            self._busy = (name, time.monotonic())
            self._stall_logged = False
            try:
                fn(*args)
            except Exception:
                log.exception("dispatch failed: %s", name)
                traceback.print_exc()
            finally:
                self._busy = None

    def _heartbeat(self):
        while not self._stop.is_set():
            time.sleep(TICK_S)
            busy = self._busy
            if busy and not self._stall_logged and time.monotonic() - busy[1] > STALL_WARN_S:
                # Almost always a modal dialog that opened behind the main
                # window: the UI is unresponsive and the cause is off-screen.
                self._stall_logged = True
                log.warning("dispatch thread has been in %s for over %.0fs — the "
                            "UI will not respond until it returns", busy[0], STALL_WARN_S)
            # Skip a beat rather than queue up behind a slow one; a backlog of
            # ticks would keep pushing stale level meters after the fact.
            if (self._tick_pending or self.core is None or not self._loaded
                    or self._closing):
                continue
            self._tick_pending = True
            self.on_main(self._tick)

    def _tick(self):
        try:
            self.core.tick()
        finally:
            self._tick_pending = False

    # ---- Host: web view ----

    def js(self, fn, *args):
        """Push a call into the page.

        Not fire-and-forget, despite the name: pywebview's run_js marshals to
        the WebView2 GUI thread and blocks until the script has run. That's
        tolerable only because the GUI thread never waits on this one — see
        on_closing. Failures are swallowed so a wedged web view can't kill the
        pump, and nothing is sent once the window is gone.
        """
        if self._window is None or self._torn_down:
            return
        payload = ",".join(json.dumps(a) for a in args)     # ascii-escaped: encoding-proof
        code = f"{fn}({payload})"
        try:
            run = getattr(self._window, "run_js", None)
            if run is not None:
                run(code)
            else:
                self._window.evaluate_js(code)
        except Exception:
            pass

    def on_main(self, fn, *args):
        if not self._stop.is_set():
            self._q.put((fn, args))

    def is_dark(self):
        return system_dark()

    def close(self):
        """Close the window — from a thread of our own, never inline.

        pywebview's destroy() marshals to the GUI thread with a *blocking*
        Control.Invoke, and Form.Close() raises FormClosing inside that call. So
        calling it on the dispatch thread parks the dispatch thread while the GUI
        thread runs on_closing — which needs the dispatch thread. Circular wait,
        and the in-app Quit button hits it every single time.
        """
        self._destroy_async()

    def _destroy_async(self):
        threading.Thread(target=self._destroy, name="notula-close", daemon=True).start()

    def _destroy(self):
        try:
            self._window.destroy()
        except Exception:
            pass

    # ---- Host: native dialogs (all on the dispatch thread) ----

    # MessageBoxW rather than tkinter for these two: MB_TOPMOST|MB_SETFOREGROUND
    # guarantees they appear in front of the WebView2 window. A modal dialog that
    # opens behind it is invisible, and because these block the dispatch thread
    # the app would look frozen with nothing on screen to explain why.

    def _message_box(self, text, title, flags):
        import ctypes
        MB_SETFOREGROUND, MB_TOPMOST = 0x00010000, 0x00040000
        return ctypes.windll.user32.MessageBoxW(
            None, text, title, flags | MB_SETFOREGROUND | MB_TOPMOST)

    def confirm(self, message, info=""):
        MB_YESNO, MB_ICONWARNING, IDYES = 0x4, 0x30, 6
        return self._message_box(f"{message}\n\n{info}".strip(), "Notula",
                                 MB_YESNO | MB_ICONWARNING) == IDYES

    def alert(self, title, body):
        MB_OK, MB_ICONINFORMATION = 0x0, 0x40
        self._message_box(body, title, MB_OK | MB_ICONINFORMATION)

    def prompt_hf_token(self, callback):
        """Ask for the token on a thread of its own, then report back.

        Emphatically NOT on the dispatch thread. This dialog opens by itself the
        first time the app runs, and a modal dialog there blocks every queued
        message: the UI stops responding, Test Input does nothing, and the window
        cannot be closed — all while the dialog itself may be hidden behind the
        main window. That is a hard hang with no visible cause.
        """
        def worker():
            tok = None
            try:
                tok = self._token_dialog()
            except Exception:
                traceback.print_exc()
            self.on_main(callback, tok)

        threading.Thread(target=worker, name="notula-token", daemon=True).start()

    def _token_dialog(self):
        """The dialog itself, with a button that opens the two pages you need
        first. Unlike the macOS sheet it doesn't have to re-ask - it stays up
        while you go and accept the model terms. Returns the token, or None."""
        import tkinter as tk
        result = {"token": None}
        with _Tk() as root:
            dlg = tk.Toplevel(root)
            dlg.title(appcore.HF_PROMPT_TITLE)
            dlg.resizable(False, False)
            frame = tk.Frame(dlg, padx=16, pady=14)
            frame.pack(fill="both", expand=True)
            tk.Label(frame, text=appcore.HF_PROMPT_TITLE,
                     font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x")
            tk.Label(frame, text=appcore.HF_PROMPT_BODY, justify="left",
                     wraplength=470, anchor="w").pack(fill="x", pady=(8, 10))
            entry = tk.Entry(frame, show="•", width=52)
            entry.pack(fill="x")
            entry.focus_set()

            def save():
                result["token"] = entry.get().strip() or None
                dlg.destroy()

            def open_hf():
                osutil.open_url(appcore.HF_MODEL_URL)
                osutil.open_url(appcore.HF_TOKENS_URL)

            btns = tk.Frame(frame)
            btns.pack(fill="x", pady=(12, 0))
            tk.Button(btns, text="Skip", width=10, command=dlg.destroy).pack(side="right")
            tk.Button(btns, text="Open HuggingFace…", command=open_hf).pack(side="right", padx=6)
            tk.Button(btns, text="Save & enable", width=14, command=save).pack(side="right")
            dlg.bind("<Return>", lambda _e: save())
            dlg.bind("<Escape>", lambda _e: dlg.destroy())
            dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
            dlg.transient(root)
            dlg.grab_set()
            _to_front(dlg)
            root.wait_window(dlg)
        return result["token"]

    # tkinter rather than pywebview's create_file_dialog, for two reasons: it
    # runs on this thread (pywebview's marshals to the GUI thread, which we make
    # a point of never blocking), and it distinguishes failure from cancellation.
    # pywebview returns None for both, so a fallback keyed on None would pop a
    # second dialog every time the user pressed Cancel.

    def _dialog_async(self, fn, callback):
        """Run a modal on a thread of its own and report the answer back.

        Never on the dispatch thread. A Tk dialog can open behind the WebView2
        window, and a modal the user cannot see, blocking the thread that handles
        every message, is indistinguishable from a hung app — the exact failure
        the startup token prompt used to cause.
        """
        def worker():
            result = None
            try:
                result = fn()
            except Exception:
                log.exception("dialog failed")
            self.on_main(callback, result)

        threading.Thread(target=worker, name="notula-dialog", daemon=True).start()

    def pick_media_file(self, exts, callback):
        def dialog():
            from tkinter import filedialog
            with _Tk() as root:
                _to_front(root)
                return filedialog.askopenfilename(
                    parent=root, title="Import a recording to transcribe",
                    filetypes=[("Audio and video", " ".join(f"*.{e}" for e in exts)),
                               ("All files", "*.*")]) or None
        self._dialog_async(dialog, callback)

    def pick_folder(self, prompt, callback):
        def dialog():
            from tkinter import filedialog
            with _Tk() as root:
                _to_front(root)
                return filedialog.askdirectory(
                    parent=root, title=prompt, mustexist=False) or None
        self._dialog_async(dialog, callback)

    def copy_text(self, text):
        if not set_clipboard(text):
            self.core._toast("Could not write to the clipboard", "warn")

    # ---- window events (arrive on pywebview threads) ----

    def on_loaded(self):
        if self._loaded:
            return                       # in-page navigation, not a fresh start
        self._loaded = True
        log.info("page loaded")
        self.on_main(self.core.on_page_loaded)
        for path in _cli_files():
            self.on_main(self.core.import_path, path)

    def on_closing(self):
        """Finalize before the window goes away, or a recording in flight is lost.

        This fires on the GUI thread — synchronously, inside Form.Close() — so it
        must never wait on the dispatch thread: teardown touches AppCore state,
        and the no-locks invariant means only the dispatch thread may. Waiting
        here is also what deadlocks the in-app Quit button, whose destroy() call
        has the dispatch thread parked inside Control.Invoke.

        So instead of waiting: veto this close, queue teardown, and close again
        for real once it reports back. The window visibly stays up for the second
        or two ffmpeg needs to mix the sources, which is honest — that work is
        the recording being saved.
        """
        if self._torn_down:
            return True                  # second pass: everything is finished
        if not self._closing:
            log.info("closing: queueing teardown")
            self._closing = True
            self._q.put((self._finish_then_close, ()))
            # Safety net only. If the dispatch thread is wedged — the realistic
            # cause being a modal dialog nobody dismissed — the window would
            # otherwise never close and the app could only be killed.
            threading.Thread(target=self._close_watchdog, name="notula-watchdog",
                             daemon=True).start()
        return False                     # veto; _finish_then_close re-closes

    def _finish_then_close(self):        # dispatch thread
        self.js("showToast", "Finishing up…", "info")
        try:
            self.core.teardown()
        except Exception:
            traceback.print_exc()
        self._torn_down = True
        self._stop.set()
        self._destroy_async()

    def _close_watchdog(self):
        if not self._stop.wait(60.0):
            print("notula: teardown did not finish in 60s — closing anyway",
                  file=sys.stderr)
            self._torn_down = True
            self._stop.set()
            self._destroy_async()


def _cli_files():
    """Media paths passed on the command line — 'Open with', or a file dropped
    on the .exe. (Dropping on the *window* can't work; see the module docstring.)"""
    out = []
    for a in sys.argv[1:]:
        if a.startswith("-"):
            continue
        if os.path.isfile(a) and a.rsplit(".", 1)[-1].lower() in appcore.MEDIA_EXTS:
            out.append(os.path.abspath(a))
    return out


def fatal(msg: str):
    """Report a message that has to reach the user, then exit.

    A windowed build (console=False) has sys.stdout and sys.stderr set to None
    and no console attached, and run.bat launches via pythonw.exe, which is the
    same. So `sys.exit(msg)` writes the single most important thing the app can
    say — "you are missing the WebView2 runtime" — precisely nowhere. MessageBoxW
    needs no console, and no import that might itself be the thing that failed.
    """
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "Notula", 0x10)   # MB_ICONERROR
    except Exception:
        pass
    sys.exit(msg)


def _redirect_output_to_log():
    """Give --selftest somewhere to print in a windowed build. Returns the log
    path, or None when stdout already works (running from source)."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    path = os.path.join(osutil.data_dir(), "selftest.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path, "w", encoding="utf-8", buffering=1)
    except OSError:
        return None
    sys.stdout = fh
    sys.stderr = fh
    return path


def main():
    logpath = setup_logging()
    log.info("Notula starting: python %s, frozen=%s",
             sys.version.split()[0], getattr(sys, "frozen", False))
    try:
        import webview
    except ImportError:
        fatal("Notula needs pywebview.\n\nRun:  pip install -r requirements-win.txt")
    if logpath:
        log.info("log: %s", logpath)

    host = WinHost()
    host.core = appcore.AppCore(host)

    window = webview.create_window(
        "Notula", html=appcore.load_html(), js_api=host.api,
        width=WIN_W, height=WIN_H, min_size=(560, 520),
        background_color="#161616",
    )
    host._window = window
    window.events.loaded += host.on_loaded
    window.events.closing += host.on_closing

    def started():
        host.start_threads()

    try:
        # gui='edgechromium' is WebView2, which ships with Windows 10/11 via Edge
        webview.start(started, gui="edgechromium", private_mode=False,
                      debug=bool(os.environ.get("NOTULA_DEBUG")))
    except Exception as e:
        log.exception("webview.start failed")
        fatal(f"Notula could not open a WebView2 window.\n\n{e}\n\n"
              f"Install the Microsoft Edge WebView2 Runtime:\n"
              f"https://developer.microsoft.com/microsoft-edge/webview2/")
    finally:
        host._stop.set()
        try:
            host.core.teardown()         # backstop; idempotent
        except Exception:
            pass


if __name__ == "__main__":
    if "--install-deps" in sys.argv:
        _redirect_output_to_log()
        sys.exit(appcore.install_deps_cli("--live" in sys.argv))
    if "--selftest" in sys.argv:
        # the same headless pipeline check the macOS build has, so a Windows
        # install can be verified without a window
        log = _redirect_output_to_log()
        i = sys.argv.index("--selftest")
        rc = appcore.selftest(sys.argv[i + 1] if i + 1 < len(sys.argv) else None)
        if log:
            # frozen build: there was no console to print to, so say where it went
            try:
                sys.stdout.flush()
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None, f"Selftest finished (exit code {rc}).\n\nOutput written to:\n{log}",
                    "Notula", 0x40)                                    # MB_ICONINFORMATION
            except Exception:
                pass
        sys.exit(rc)
    main()
