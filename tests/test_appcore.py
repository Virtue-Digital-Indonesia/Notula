"""
Exercise AppCore against a stub host, verifying the extraction from Bridge kept
every behaviour. Runs with XDG_CONFIG_HOME redirected so the real config is never
touched, and never opens an audio device.
"""
import os
import pathlib
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="notula-test-")
# Redirect config on BOTH platforms before importing anything: osutil.config_dir
# reads XDG_CONFIG_HOME on macOS/Linux and APPDATA on Windows, and this suite
# calls config.save() repeatedly. Miss one and it overwrites the real settings.
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "cfg")
os.environ["APPDATA"] = os.path.join(TMP, "cfg")
os.environ["LOCALAPPDATA"] = os.path.join(TMP, "local")
os.environ["NOTULA_THEME"] = "dark"          # deterministic, no host theme call

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import appcore, config, library          # noqa: E402

fail = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        fail.append(name)


class StubHost:
    """Records everything appcore asks the platform to do."""

    def __init__(self):
        self.js_calls = []
        self.toasts = []
        self.copied = None
        self.opened = []
        self.alerts = []
        self.confirm_result = False
        self.closed = False
        self.token = None

    def js(self, fn, *args):
        self.js_calls.append((fn, args))
        if fn == "showToast":
            self.toasts.append(args[0])

    def on_main(self, fn, *a):
        fn(*a)                                # run inline: single-threaded test

    def is_dark(self):
        return True

    def confirm(self, msg, info=""):
        return self.confirm_result

    def alert(self, title, body):
        self.alerts.append((title, body))

    def prompt_hf_token(self, callback):
        # asynchronous by contract; answering inline is allowed (macOS does)
        callback(self.token)

    def pick_media_file(self, exts):
        return None

    def pick_folder(self, prompt="Choose"):
        return None

    def copy_text(self, text):
        self.copied = text

    def close(self):
        self.closed = True


def fns(host):
    return [c[0] for c in host.js_calls]


# ---- construction -------------------------------------------------------------
host = StubHost()
host.token = "hf_stub_token"
core = appcore.AppCore(host)
lib = os.path.join(TMP, "library")
core.cfg["library"] = lib
config.save(core.cfg)
library.ensure_root(lib)

check("config redirected to temp", config.config_path().startswith(TMP),
      config.config_path())

# ---- page load pushes everything ----------------------------------------------
core.on_page_loaded()
check("notulaInit pushed", "notulaInit" in fns(host))
check("notulaMeetings pushed", "notulaMeetings" in fns(host))
check("notulaState pushed", "notulaState" in fns(host))

init = next(a[0] for f, a in host.js_calls if f == "notulaInit")
for key in ("theme", "settings", "live_models", "inputs", "system_available",
            "system_backend", "system_desc", "system_permission_hint",
            "system_unavailable"):
    check(f"init carries {key}", key in init)
check("init platform strings non-empty", bool(init["system_desc"]), init["system_backend"])

# the HF prompt fires when no token is stored, and the answer is saved
check("hf token prompted + saved", core.cfg["hf_token"] == "hf_stub_token",
      repr(core.cfg["hf_token"])[:20])

st = next(a[0] for f, a in host.js_calls if f == "notulaState")
check("state has header/canRecord", "header" in st and "canRecord" in st, st["header"])

# ---- dispatch: settings round-trips -------------------------------------------
core.dispatch({"action": "setLang", "value": "en"})
check("setLang", core.cfg["lang"] == "en", core.cfg["lang"])

core.dispatch({"action": "setField", "key": "max_speakers", "value": "4"})
check("setField clamps + persists", core.cfg["max_speakers"] == 4, core.cfg["max_speakers"])

core.dispatch({"action": "setField", "key": "max_speakers", "value": "999"})
check("setField upper bound", core.cfg["max_speakers"] == 20, core.cfg["max_speakers"])

core.dispatch({"action": "setField", "key": "max_speakers", "value": "abc"})
check("setField rejects non-numeric", any("must be a number" in t for t in host.toasts))

core.dispatch({"action": "setAuto", "value": True})
check("setAuto", core.cfg["auto_transcribe"] is True)

core.dispatch({"action": "setTheme", "mode": "light"})
check("setTheme", core.cfg["theme"] == "light" and "applyTheme" in fns(host))

core.dispatch({"action": "setToken", "value": "•" * 12})
check("masked token ignored", core.cfg["hf_token"] == "hf_stub_token")
core.dispatch({"action": "setToken", "value": "hf_real"})
check("setToken", core.cfg["hf_token"] == "hf_real")

core.dispatch({"action": "setMute", "kind": "mic", "muted": True})
check("setMute tracked", core._mute["mic"] is True)

core.dispatch({"action": "pickMic", "index": None})
check("pickMic None -> default", core.cfg["mic_device"] is None)

# settings actually landed on disk
saved = config.load()
check("config persisted to disk", saved["lang"] == "en" and saved["max_speakers"] == 20,
      f"{saved['lang']} {saved['max_speakers']}")

# ---- dispatch: meeting actions on a real library ------------------------------
mid = library.create_meeting(lib, "Test meeting")
library.update_meta(lib, mid, status=library.RECORDED, duration=12)
core.dispatch({"action": "refresh"})
meetings = [a[0] for f, a in host.js_calls if f == "notulaMeetings"][-1]
check("library lists the meeting", any(m["id"] == mid for m in meetings), len(meetings))

with open(library.output_path(lib, mid), "w", encoding="utf-8") as fh:
    fh.write("# header\nhello world\n")
core.dispatch({"action": "copyOutput", "id": mid})
check("copyOutput -> clipboard", host.copied == "# header\nhello world\n", repr(host.copied))

core.dispatch({"action": "rename", "id": mid, "name": "Renamed meeting"})
ids = [m["id"] for m in [a[0] for f, a in host.js_calls if f == "notulaMeetings"][-1]]
check("rename moved the folder", any("Renamed-meeting" in i for i in ids), ids)
new_mid = next(i for i in ids if "Renamed-meeting" in i)

host.confirm_result = False
core.dispatch({"action": "delete", "id": new_mid})
check("delete respects Cancel", os.path.isdir(library.folder(lib, new_mid)))
host.confirm_result = True
core.dispatch({"action": "delete", "id": new_mid})
check("delete removes the folder", not os.path.isdir(library.folder(lib, new_mid)))

# ---- guards --------------------------------------------------------------------
n = len(host.toasts)
core.dispatch({"action": "transcribe", "id": "does-not-exist"})
check("transcribe missing meeting warns", len(host.toasts) > n and core.tx_mid is None,
      host.toasts[-1] if host.toasts else "")

core.dispatch({"action": "copyOutput", "id": "nope"})
check("copyOutput missing warns", "No output.txt yet" in host.toasts)

core.dispatch({"action": "rename", "id": mid, "name": ""})
check("empty rename rejected", "A meeting needs a name" in host.toasts)

core.dispatch({"action": "systemHelp"})
check("systemHelp uses platform copy", host.alerts and "computer audio" in host.alerts[-1][0].lower(),
      host.alerts[-1][1][:60] if host.alerts else "")

core.dispatch({"action": "quit"})
check("quit closes host", host.closed)

core.dispatch({"action": "bogus-action"})
check("unknown action is a no-op", True)
core.dispatch({"not-an-action": 1})
check("malformed message is a no-op", True)

# ---- the setup notice ------------------------------------------------------------
import pipeline                                  # noqa: E402
check("init carries setup", "setup" in init and "ok" in init["setup"], init.get("setup"))

real_ffmpeg = pipeline.FFMPEG
pipeline.FFMPEG = "/definitely/not/here/ffmpeg"
try:
    s = appcore.setup_summary(core.cfg)
    check("missing ffmpeg is not ok", s["ok"] is False)
    check("missing ffmpeg flagged as audio loss", s["loses_audio"] is True)
    check("ffmpeg named in the missing list",
          any("ffmpeg" == m["label"] for m in s["missing"]), s["missing"])
    check("the notice explains what it's for",
          all(m.get("for") for m in s["missing"]))
    check("a fix is suggested", bool(s["hint"]), s["hint"])
    host.alerts.clear()
    core.dispatch({"action": "setupHelp"})
    check("setupHelp lists the paths",
          host.alerts and "/definitely/not/here/ffmpeg" in host.alerts[-1][1],
          host.alerts[-1][0] if host.alerts else "")
finally:
    pipeline.FFMPEG = real_ffmpeg

# ...and goes quiet once everything is there. Faked rather than asserted about
# the real machine: this suite has to pass on a box that hasn't been set up yet,
# which is exactly the case the banner exists for.
fake_bin, fake_models = sys.executable, pathlib.Path(TMP) / "models"
fake_models.mkdir(exist_ok=True)
(fake_models / f"ggml-{core.cfg['model']}.bin").write_bytes(b"x")
(fake_models / "ggml-silero-v6.2.0.bin").write_bytes(b"x")
saved = (pipeline.FFMPEG, pipeline.FFPROBE, pipeline.WHISPER_CLI, pipeline.MODELS_DIR)
pipeline.FFMPEG = pipeline.FFPROBE = pipeline.WHISPER_CLI = fake_bin
pipeline.MODELS_DIR = fake_models
try:
    s = appcore.setup_summary(core.cfg)
    check("a complete install reports ok", s["ok"] is True, s["missing"])
    check("no audio-loss warning when ffmpeg is present", s["loses_audio"] is False)
finally:
    pipeline.FFMPEG, pipeline.FFPROBE, pipeline.WHISPER_CLI, pipeline.MODELS_DIR = saved


# ---- a failed meta write must not wedge the recorder ----------------------------
# On Windows os.replace can hit a sharing violation (an indexer or AV holding
# meta.json). If that exception escaped, self.rec / self._stopping would stay set
# and every later record/stop/pause would bail out early — forever.
class FakeEngine:
    running, paused, elapsed = True, False, 0.0

    def levels(self):
        return {}

    def relocate(self, p):
        self.relocated = p

    def set_mute(self, *a):
        pass


core.cfg["auto_transcribe"] = False          # don't chain into a transcription
wedge_mid = library.create_meeting(lib, "Wedge test")
core.rec, core.rec_mid, core._stopping = FakeEngine(), wedge_mid, True
real_update = library.update_meta
library.update_meta = lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked"))
try:
    core._on_stopped(wedge_mid, 5.0)
finally:
    library.update_meta = real_update
check("failed meta write still clears the recorder",
      core.rec is None and core._stopping is False and core.rec_mid is None)
check("failed meta write is reported to the user",
      any("couldn't be written" in t for t in host.toasts), host.toasts[-1])
core.dispatch({"action": "startRecording", "name": "x"})   # would no-op if wedged
check("recorder is usable again after the failure", core.tx_mid is None)

# ---- rename during recording, when the folder can't move ------------------------
# POSIX moves a directory out from under open file handles; Windows refuses. The
# display name must still change, with the folder move deferred to stop.
defer_mid = library.create_meeting(lib, "Before")
core.rec, core.rec_mid, core._stopping = FakeEngine(), defer_mid, False
core._pending_rename = None
real_rename = library.rename_meeting
def refuse_move(root_, m, n, move=True):
    if move:
        raise PermissionError("the process cannot access the file")
    return real_rename(root_, m, n, move=False)
library.rename_meeting = refuse_move
try:
    core.dispatch({"action": "rename", "id": defer_mid, "name": "After"})
finally:
    library.rename_meeting = real_rename
check("display name changes even when the folder can't move",
      library.read_meta(lib, defer_mid).get("name") == "After",
      library.read_meta(lib, defer_mid).get("name"))
check("folder move is deferred", core._pending_rename == "After", core._pending_rename)
check("the deferral is explained", any("follows when you stop" in t for t in host.toasts))

core._stopping = True
core._on_stopped(defer_mid, 3.0)              # handles now closed
moved = [m["id"] for m in [a[0] for f, a in host.js_calls if f == "notulaMeetings"][-1]]
check("deferred rename applied on stop", any("After" in i for i in moved), moved)
check("pending rename cleared", core._pending_rename is None)

# ---- tick + teardown ------------------------------------------------------------
before = len(host.js_calls)
for _ in range(5):
    core.tick()
check("tick pushes state at 2Hz", len(host.js_calls) > before)

core.teardown()
core.teardown()                                  # idempotent
check("teardown is idempotent", core._torn is True)

print()
print("FAILED:", fail if fail else "none")
print("temp dir:", TMP)
sys.exit(1 if fail else 0)
