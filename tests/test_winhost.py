"""
Exercise notula_win.WinHost's threading model without pywebview.

What this can prove on macOS: that every entry point (page messages, heartbeat
ticks, worker callbacks) is funnelled onto one dispatch thread, that ticks
coalesce instead of backing up, that JS payloads encode safely, and that closing
tears down on the dispatch thread rather than racing it.

What it cannot prove: pywebview/WebView2, the ctypes clipboard, winreg theme
lookup, tkinter dialogs, pyaudiowpatch. Those need a Windows machine.
"""
import os
import pathlib
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="notula-winhost-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "cfg")     # macOS/Linux
os.environ["APPDATA"] = os.path.join(TMP, "cfg")             # Windows
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import notula_win                                    # noqa: E402

fail = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        fail.append(name)


class StubWindow:
    def __init__(self):
        self.js = []
        self.destroyed = threading.Event()

    def run_js(self, code):
        self.js.append(code)

    def destroy(self):
        self.destroyed.set()


class FakeCore:
    """Stands in for AppCore, recording which thread each call arrived on."""

    def __init__(self):
        self.threads = []
        self.ticks = 0
        self.dispatched = []
        self.torn = 0
        self.tick_delay = 0.0

    def _note(self):
        self.threads.append(threading.current_thread().name)

    def tick(self):
        self._note()
        self.ticks += 1
        if self.tick_delay:
            time.sleep(self.tick_delay)

    def dispatch(self, msg):
        self._note()
        self.dispatched.append(msg)

    def on_page_loaded(self):
        self._note()

    def import_path(self, p):
        self._note()
        self.dispatched.append({"import": p})

    def teardown(self):
        self._note()
        time.sleep(0.3)                       # teardown does real work (ffmpeg mix)
        self.torn += 1

    def _toast(self, *a):
        pass


host = notula_win.WinHost()
core = FakeCore()
host.core = core
host._window = StubWindow()
host.start_threads()

# ---- everything lands on the one dispatch thread ------------------------------
host.api.post({"action": "refresh"})
host.on_main(core.dispatch, {"action": "from-worker"})
time.sleep(0.4)
check("JS message reached core.dispatch",
      {"action": "refresh"} in core.dispatched, core.dispatched)
check("worker callback reached core",
      {"action": "from-worker"} in core.dispatched)
check("all core calls on one thread", len(set(core.threads)) == 1, set(core.threads))
check("that thread is the dispatch thread",
      core.threads and core.threads[0] == "notula-ui", core.threads[:1])

# ---- heartbeat -----------------------------------------------------------------
host._loaded = True
before = core.ticks
time.sleep(0.55)
check("heartbeat ticks ~10Hz", 3 <= core.ticks - before <= 8, core.ticks - before)

# a slow tick must not build a backlog of stale ticks
core.tick_delay = 0.35
n0 = core.ticks
time.sleep(1.0)
gained = core.ticks - n0
core.tick_delay = 0.0
check("slow ticks coalesce (no backlog)", gained <= 4, f"{gained} ticks in 1.0s")
time.sleep(0.5)

# ---- JS payload encoding --------------------------------------------------------
host.js("notulaState", {"header": "Perekaman…", "n": 3, "ok": True})
code = host._window.js[-1]
check("js() builds a call", code.startswith("notulaState({"), code[:40])
check("js() escapes non-ASCII", "\\u2026" in code and "…" not in code, code[:60])
host.js("showToast", 'quote " and \\ backslash', "warn")
check("js() escapes quotes safely", host._window.js[-1].count('"') >= 2,
      host._window.js[-1][:70])

# the page-loaded hook fires once, and only once
host.on_loaded()
host.on_loaded()
time.sleep(0.3)
check("on_loaded is idempotent", core.threads.count("notula-ui") == len(core.threads))

# ---- closing: veto, tear down on the dispatch thread, then close for real -------
t0 = time.perf_counter()
first = host.on_closing()
dt = time.perf_counter() - t0
check("on_closing vetoes the first close", first is False)
check("on_closing does not block the GUI thread", dt < 0.1, f"{dt:.3f}s")
check("teardown ran exactly once", host._stop.wait(5.0) and core.torn == 1, core.torn)
check("teardown ran on the dispatch thread", set(core.threads) == {"notula-ui"},
      set(core.threads))
check("window destroyed after teardown", host._window.destroyed.wait(5.0))
check("second on_closing allows it", host.on_closing() is True and core.torn == 1)


# ---- regression: the startup token prompt must not block the dispatch thread ----
# This is what froze the first real Windows run: prompt_hf_token opened a modal
# tkinter dialog on the dispatch thread, behind the WebView2 window. The UI stayed
# up but every queued message — including the window's own close — sat behind an
# invisible dialog. It must hand the dialog to another thread and report back.
host.core = core                       # (host was torn down above; rewire it)
host._stop.clear()
host._closing = host._torn_down = False
host.start_threads()

dialog_open = threading.Event()
release = threading.Event()
host._token_dialog = lambda: (dialog_open.set(), release.wait(5), "hf_tok")[-1]

got = []
t0 = time.perf_counter()
host.prompt_hf_token(lambda tok: got.append((tok, threading.current_thread().name)))
returned_in = time.perf_counter() - t0
check("prompt_hf_token returns immediately", returned_in < 0.5, f"{returned_in:.3f}s")
check("the dialog actually opened", dialog_open.wait(5.0))

progressed = threading.Event()          # the queue must still be draining
host.on_main(progressed.set)
check("dispatch thread still alive while the dialog is up", progressed.wait(5.0))

release.set()
for _ in range(50):
    if got:
        break
    time.sleep(0.1)
check("token reported back", got and got[0][0] == "hf_tok", got)
check("callback ran on the dispatch thread", got and got[0][1] == "notula-ui", got)
host._stop.set()


# ---- regression: the Quit button must not deadlock ------------------------------
# Reproduces the real shape of the bug. pywebview's destroy() marshals to the GUI
# thread with a blocking Control.Invoke, and Form.Close() raises FormClosing
# *inside* that call — so the closing handler runs synchronously on whichever
# thread is parked in destroy(). If close() is called on the dispatch thread and
# the handler then waits on the dispatch thread, nothing can proceed.
class BlockingWindow:
    """destroy() runs the closing handler inline, as WinForms does."""

    def __init__(self):
        self.host = None
        self.destroyed = threading.Event()
        self.js = []

    def run_js(self, code):
        self.js.append(code)

    def destroy(self):
        if self.host.on_closing():          # False == the close was vetoed
            self.destroyed.set()


host2 = notula_win.WinHost()
core2 = FakeCore()
host2.core = core2
win2 = BlockingWindow()
win2.host = host2
host2._window = win2
host2.start_threads()
host2._loaded = True

# the Quit button path: JS -> dispatch thread -> core.dispatch -> host.close()
progressed = threading.Event()
host2.on_main(host2.close)
host2.on_main(progressed.set)      # queued behind close(); runs only if not blocked
check("Quit does not block the dispatch thread", progressed.wait(5.0))
check("Quit tears down and closes", win2.destroyed.wait(10.0) and core2.torn == 1,
      f"torn={core2.torn}")
check("Quit teardown stayed on the dispatch thread",
      set(core2.threads) == {"notula-ui"}, set(core2.threads))

time.sleep(0.3)
check("threads stop after close", host._stop.is_set() and host2._stop.is_set())

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
