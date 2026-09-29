"""
Exercise AppCore against a stub host, verifying the extraction from Bridge kept
every behaviour. Runs with XDG_CONFIG_HOME redirected so the real config is never
touched, and never opens an audio device.
"""
import os
import pathlib
import sys
import tempfile
import threading

TMP = tempfile.mkdtemp(prefix="notula-test-")
# Redirect config on BOTH platforms before importing anything: osutil.config_dir
# reads XDG_CONFIG_HOME on macOS/Linux and APPDATA on Windows, and this suite
# calls config.save() repeatedly. Miss one and it overwrites the real settings.
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "cfg")
os.environ["APPDATA"] = os.path.join(TMP, "cfg")
os.environ["LOCALAPPDATA"] = os.path.join(TMP, "local")
os.environ["NOTULA_THEME"] = "dark"          # deterministic, no host theme call

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import json                              # noqa: E402
import appcore, config, library          # noqa: E402

fail = []


# run directly on a cp1252 console (Windows), a glyph in a detail string must
# not turn into a crash mid-suite
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass


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
        self.next_file = None            # what a file dialog would return
        self.next_folder = None          # what a folder dialog would return

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

    # callback-based by contract, so a host is free to run the dialog off-thread
    def pick_media_file(self, exts, callback):
        callback(self.next_file)

    def pick_folder(self, prompt, callback):
        callback(self.next_folder)

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

# ---- the cloud engine: settings, gating, and what a run leaves behind ----------
import cloud                                  # noqa: E402
check("init carries the cloud menu", "cloud" in init and init["cloud"]["models"], init.get("cloud"))
check("parallel is the default", init["settings"]["cloud_parallel"] is True)
check("init settings carry the engine", init["settings"]["engine"] == "local"
      and init["settings"]["cloud_model"] == cloud.DEFAULT_MODEL, init["settings"].get("engine"))

core.dispatch({"action": "setOpenAIKey", "value": "•" * 12})
check("masked OpenAI key ignored", core.cfg["openai_api_key"] == "")
core.dispatch({"action": "setOpenAIKey", "value": " sk-test-key "})
check("setOpenAIKey trims + persists", core.cfg["openai_api_key"] == "sk-test-key")
init_k = [a[0] for f, a in host.js_calls if f == "notulaInit"][-1]
check("the key itself never reaches the page",
      "sk-test-key" not in json.dumps(init_k) and init_k["settings"]["openai_ok"] is True)

core.dispatch({"action": "setCloudModel", "value": "gpt-9-nope"})
check("unknown cloud model rejected", core.cfg["cloud_model"] == cloud.DEFAULT_MODEL)
core.dispatch({"action": "setCloudModel", "value": "gpt-transcribe"})
check("setCloudModel", core.cfg["cloud_model"] == "gpt-transcribe")
core.dispatch({"action": "setEngine", "value": "bogus"})
check("unknown engine rejected", core.cfg["engine"] == "local")
core.dispatch({"action": "setEngine", "value": "cloud"})
check("setEngine", core.cfg["engine"] == "cloud")

# no key: the cloud engine refuses before touching the meeting
core.cfg["openai_api_key"] = ""
os.environ.pop("OPENAI_API_KEY", None)
core.dispatch({"action": "setEngine", "value": "cloud"})
check("choosing cloud without a key warns", any("OpenAI API key" in t for t in host.toasts))
cloud_mid = library.create_meeting(lib, "Cloud meeting")
library.update_meta(lib, cloud_mid, status=library.RECORDED, duration=3600)
with open(library.audio_path(lib, cloud_mid), "wb") as fh:
    fh.write(b"RIFF")
n = len(host.toasts)
core.dispatch({"action": "transcribe", "id": cloud_mid, "engine": "cloud"})
check("cloud transcribe without a key stops with a clear toast",
      core.tx_mid is None and "OpenAI API key" in host.toasts[-1], host.toasts[-1])
check("...and leaves the meeting untouched",
      library.read_meta(lib, cloud_mid)["status"] == library.RECORDED)

# with a key: the worker is the cloud module, it's told the price up front, and
# meta.json records the engine, model and what it cost
core.cfg["openai_api_key"] = "sk-test-key"
# an earlier cloud run of this meeting stopped halfway: its first 30 minutes
# are saved, so only the rest should be priced (the suite's language is "en")
DIAR = "gpt-4o-transcribe-diarize"
cloud._PartCache(pathlib.Path(library.folder(lib, cloud_mid)), DIAR, "en").save(
    0.0, 1800.0, {"start": 0.0, "end": 1800.0, "segments": []})
check("the row reports what a retry would reuse",
      (library.describe(lib, cloud_mid).get("cloud_resume") or {}).get("done_s") == 1800.0)
calls = []
def fake_cloud(wav, out_dir, **kw):
    calls.append(kw)
    kw["progress_cb"]("upload", 0.5, "uploading…")
    out = pathlib.Path(out_dir); (out / "output.txt").write_text("# x\nhi\n")
    return {"wav": wav, "json": None, "txt": None, "merged": None, "output": out / "output.txt",
            "duration": 3600.0, "diarized": True, "warning": None, "engine": "cloud",
            "model": kw["model"], "chunks": 3, "cost_usd": 0.36, "cost_estimated": False,
            "speakers": 2}
real_cloud_tx = cloud.transcribe_meeting
cloud.transcribe_meeting = fake_cloud
# ffmpeg is checked before the upload; this suite redirects LOCALAPPDATA to a
# temp dir, so on Windows the real path resolves nowhere. Fake it, as the setup
# tests below do.
import pipeline as _pl                          # noqa: E402
_real_ffmpeg, _pl.FFMPEG = _pl.FFMPEG, sys.executable
try:
    core.dispatch({"action": "transcribe", "id": cloud_mid, "engine": "cloud",
                   "cloud_model": "gpt-4o-transcribe-diarize", "parallel": False})
    # the worker is a real thread even against the stub host; give it a moment
    import time as _time
    for _ in range(200):
        if core.tx_mid is None:
            break
        _time.sleep(0.02)
finally:
    cloud.transcribe_meeting = real_cloud_tx
    _pl.FFMPEG = _real_ffmpeg
check("the cloud module ran with the key and model",
      calls and calls[0]["key"] == "sk-test-key" and calls[0]["model"] == "gpt-4o-transcribe-diarize", calls)
check("the dialog's choice became the default",
      core.cfg["engine"] == "cloud" and core.cfg["cloud_model"] == "gpt-4o-transcribe-diarize")
start_toast = next((t for t in host.toasts if t.startswith("Sending to OpenAI")), "")
check("the estimate prices only what isn't saved, at the measured rate",
      "about $0.54" in start_toast and "remaining 30:00" in start_toast
      and "30:00 is saved" in start_toast, start_toast)
check("the start toast says how long it takes, one part at a time",
      "takes about 13 min, one part at a time" in start_toast, start_toast)
check("the dialog's one-at-a-time choice reaches the run and is remembered",
      calls[0].get("parallel") is False and core.cfg["cloud_parallel"] is False,
      (calls[0].get("parallel"), core.cfg["cloud_parallel"]))
check("the run can be cancelled", isinstance(calls[0].get("cancel"), threading.Event))
m = library.read_meta(lib, cloud_mid)
check("meta records engine, model and cost",
      m["status"] == library.TRANSCRIBED and m["engine"] == "cloud"
      and m["model"] == "gpt-4o-transcribe-diarize" and m["cost_usd"] == 0.36, m)
check("the row carries the cost for the page",
      library.describe(lib, cloud_mid)["cost_usd"] == 0.36 and library.describe(lib, cloud_mid)["seconds"] == 3600.0)
check("the done toast says what it cost",
      any("OpenAI cost $0.36" in t for t in host.toasts), host.toasts[-1])
check("the rate this run really cost is learned",
      core.cfg["cloud_rates"].get(DIAR) == 0.006, core.cfg["cloud_rates"])
init_r = [a[0] for f, a in host.js_calls if f == "notulaInit"][-1]
diar_menu = next(m for m in init_r["cloud"]["models"] if m["key"] == DIAR)
check("the page is told the learned rate, and that it is learned",
      diar_menu["usd_per_min"] == 0.006 and diar_menu["rate_source"] == "learned", diar_menu)
cloud._PartCache(pathlib.Path(library.folder(lib, cloud_mid)), DIAR, "en").clear()
check("the next estimate uses the learned rate",
      abs(core._estimate(cloud_mid)["usd"] - 0.36) < 1e-9, core._estimate(cloud_mid))
junk = config._sanitize(dict(config.DEFAULTS, cloud_rates={
    DIAR: "abc", "gpt-9": 0.01, "gpt-transcribe": 0.005, "x": float("nan")}))
check("learned rates are sanitized", junk["cloud_rates"] == {"gpt-transcribe": 0.005}, junk["cloud_rates"])
check("tx state is clear afterwards", core.tx_mid is None and core._tx_progress is None)
core.dispatch({"action": "setEngine", "value": "local"})

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

# ---- changing the library folder, typed and browsed ------------------------------
orig_lib = core.cfg["library"]

typed = os.path.join(TMP, "typed-library")
core.dispatch({"action": "setLibrary", "value": typed})
check("a typed path is accepted", core.cfg["library"] == typed, core.cfg["library"])
check("the folder is created", os.path.isdir(typed))
check("it persists", config.load()["library"] == typed)

browsed = os.path.join(TMP, "browsed-library")
host.next_folder = browsed
core.dispatch({"action": "pickLibrary"})
check("browsing sets it too", core.cfg["library"] == browsed, core.cfg["library"])

host.next_folder = None                       # user cancelled the dialog
core.dispatch({"action": "pickLibrary"})
check("cancelling leaves it alone", core.cfg["library"] == browsed)

core.dispatch({"action": "setLibrary", "value": "~/  "})
check("~ is expanded, whitespace trimmed",
      core.cfg["library"] == os.path.abspath(os.path.expanduser("~")), core.cfg["library"])

# a path that cannot be a directory must be refused, not saved and failed on later
blocker = os.path.join(TMP, "iam-a-file")
pathlib.Path(blocker).write_text("x")
before = core.cfg["library"]
n = len(host.toasts)
core.dispatch({"action": "setLibrary", "value": blocker})
check("an unusable path is refused", core.cfg["library"] == before, core.cfg["library"])
check("and the user is told", any("Can't use that folder" in x for x in host.toasts[n:]),
      host.toasts[-1])

core.dispatch({"action": "setLibrary", "value": ""})
check("an empty path is refused", core.cfg["library"] == before)

core.cfg["library"] = lib; config.save(core.cfg)   # restore for later checks

# ---- meetings list: timestamps and remembered view -------------------------------
mm = library.create_meeting(lib, "Timestamped")
row = next(m for m in library.list_meetings(lib) if m["id"] == mm)
check("meetings carry a sortable timestamp", isinstance(row.get("ts"), float) and row["ts"] > 0,
      row.get("ts"))
check("the id's stamp is a fallback when meta has no date",
      library.created_ts("2026-08-12_1430_x", "") > 0)
check("a junk id yields 0 rather than raising", library.created_ts("nonsense", "bad") == 0.0)

core.dispatch({"action": "setMeetingsView", "collapsed": True, "limit": 50})
check("collapse persists", config.load()["meetings_collapsed"] is True)
check("row cap persists", config.load()["meetings_limit"] == 50)
core.dispatch({"action": "setMeetingsView", "limit": 99999})
check("row cap is clamped", config.load()["meetings_limit"] == 1000,
      config.load()["meetings_limit"])
core.dispatch({"action": "setMeetingsView", "limit": "nonsense"})
check("junk row cap is ignored", config.load()["meetings_limit"] == 1000)
core.dispatch({"action": "setMeetingsView", "collapsed": False, "limit": 25})

init2 = None
core.dispatch({"action": "refresh"})
init2 = [a[0] for f, a in host.js_calls if f == "notulaInit"][-1]
check("init carries the remembered view",
      init2["settings"]["meetings_limit"] == 25
      and init2["settings"]["meetings_collapsed"] is False,
      init2["settings"].get("meetings_limit"))

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
import deps                                   # noqa: E402
fake_bin, fake_models = sys.executable, pathlib.Path(TMP) / "models"
fake_models.mkdir(exist_ok=True)
(fake_models / f"ggml-{core.cfg['model']}.bin").write_bytes(b"x")
(fake_models / "ggml-silero-v6.2.0.bin").write_bytes(b"x")
saved = (pipeline.FFMPEG, pipeline.FFPROBE, pipeline.WHISPER_CLI, pipeline.MODELS_DIR)
pipeline.FFMPEG = pipeline.FFPROBE = pipeline.WHISPER_CLI = fake_bin
pipeline.MODELS_DIR = fake_models
# model_complete() measures against the real download size, so a stub file is
# (correctly) "incomplete" — drop the expected size for the duration so the stub
# counts as whole. Writing 2.9 GB to prove a point is not on.
saved_sizes = dict(deps.SIZES)
deps.SIZES.pop(f"ggml-{core.cfg['model']}.bin", None)
deps.SIZES.pop("ggml-silero-v6.2.0.bin", None)
try:
    s = appcore.setup_summary(core.cfg)
    check("a complete install reports ok", s["ok"] is True, s["missing"])
    check("no audio-loss warning when ffmpeg is present", s["loses_audio"] is False)
finally:
    pipeline.FFMPEG, pipeline.FFPROBE, pipeline.WHISPER_CLI, pipeline.MODELS_DIR = saved
    deps.SIZES.clear(); deps.SIZES.update(saved_sizes)


# ---- a truncated model must read as unusable, not as installed -------------------
# This is what "whisper-cli failed" turned out to be: a 2 GB fragment of a 2.9 GB
# model sitting under the final name, so nothing re-downloaded it and whisper
# just failed to load it.
trunc = fake_models / "ggml-trunc-test.bin"
trunc.write_bytes(b"x" * 1000)
deps.SIZES["ggml-trunc-test.bin"] = 10_000
check("a truncated model is not 'complete'", deps.model_complete(trunc, "ggml-trunc-test.bin") is False)
trunc.write_bytes(b"x" * 9_600)          # 96% — within the slack
check("a whole model is 'complete'", deps.model_complete(trunc, "ggml-trunc-test.bin") is True)
deps.SIZES.pop("ggml-trunc-test.bin")
check("an unknown model falls back to non-empty", deps.model_complete(trunc, "who-knows.bin") is True)

check("whisper's real error survives, not just our prefix",
      "failed to load model" in appcore.error_summary(
          "whisper-cli failed:\nwhisper_model_load: loading model\n"
          "whisper_init_with_params_no_state: failed to load model\n"
          "whisper_print_timings:     load time = 12.00 ms"),
      appcore.error_summary("whisper-cli failed:\nx\nfailed to load model"))

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

tx_cancel = core._tx_cancel
core.teardown()
core.teardown()                                  # idempotent
check("quitting cancels a cloud run", tx_cancel is not None and tx_cancel.is_set())
check("teardown is idempotent", core._torn is True)

print()
print("FAILED:", fail if fail else "none")
print("temp dir:", TMP)
sys.exit(1 if fail else 0)
