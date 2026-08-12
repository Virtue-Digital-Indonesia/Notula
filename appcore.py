"""
notula.appcore — everything the app does, with nothing that knows what OS it's on.

The UI is a web page either way; only the window around it differs. So this
module owns all the actual behaviour — the recording lifecycle, the live tier,
transcription, the meeting library, settings — and a per-platform *host* supplies
the dozen things that genuinely need native code (see `Host` below). notula.py is
that host on macOS (AppKit + WKWebView), notula_win.py on Windows (WebView2).

Nothing here imports AppKit, pywebview, or ctypes; nothing there decides what a
button does. That split is the whole point: the Windows port is a new window, not
a second copy of the app.

Threading contract, unchanged from the original AppKit version:
  * every method here runs on the UI thread unless marked otherwise;
  * work that blocks goes to a daemon thread and comes back via `host.on_main`;
  * `host.js` is only ever called on the UI thread, because that's the only
    thread a web view will evaluate script on.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading

import config
import deps
import library
import live
import osutil
import permissions
import pipeline
import recorder
import sysaudio
import toolpaths
import version

# the gated pyannote model diarization uses — the user must accept its terms once
HF_MODEL_URL = "https://huggingface.co/pyannote/speaker-diarization-community-1"
HF_TOKENS_URL = "https://huggingface.co/settings/tokens"

HF_PROMPT_TITLE = "Enable speaker labels (diarization)"
HF_PROMPT_BODY = (
    "To label who is speaking, Notula uses the pyannote model, which is "
    "free but gated — you must do this once:\n\n"
    "1.  Open the model page and click Agree/Accept its terms:\n"
    f"      {HF_MODEL_URL}\n"
    "2.  Create a HuggingFace access token (Read):\n"
    f"      {HF_TOKENS_URL}\n"
    "3.  Paste the token below.\n\n"
    "You can Skip — you'll still get full transcripts, just without "
    "speaker labels. Add a token later under the ⚙ gear.")

# What ffmpeg is allowed to be pointed at on import. Also a guard: AppKit hands
# the script path to application:openFiles: when the app is run from source, so
# without this a plain `python notula.py` imports notula.py as a "meeting".
MEDIA_EXTS = ("wav", "w64", "mp3", "mp2", "m4a", "m4b", "aac", "aif", "aiff",
              "flac", "ogg", "oga", "opus", "caf", "wma", "amr", "au", "mka",
              "mp4", "mov", "m4v", "webm", "mkv", "avi", "flv", "wmv", "asf",
              "mpg", "mpeg", "ts", "mts", "m2ts", "vob", "3gp", "3g2")


class Host:
    """What a platform shell must provide. Documentation, not a base class —
    the macOS host is an NSObject and can't usefully inherit from this.

    js(fn, *args)            evaluate fn(...json args...) in the web view.
                             UI thread only; must not raise.
    on_main(fn, *args)       run fn(*args) on the UI thread, from any thread.
    is_dark()                whether the desktop is currently in dark mode.
    confirm(msg, info)       modal yes/no; True if the user confirmed.
    alert(title, body)       modal message with a single dismiss button.
    prompt_hf_token(cb)      ask for the token, then call cb(token_or_None) on
                             the UI thread. MUST NOT block the caller: it fires
                             unprompted at startup. Copy: HF_PROMPT_TITLE/BODY.
    pick_media_file(exts,cb) open dialog, then cb(path_or_None) on the UI thread.
    pick_folder(prompt,cb)   folder dialog, then cb(path_or_None) on the UI thread.
                             Both MUST NOT block the caller, for the same reason
                             as prompt_hf_token: a modal that opens behind the
                             main window would otherwise freeze everything.
    copy_text(text)          put text on the system clipboard.
    close()                  close the window / quit the app.
    """


def resource_base() -> str:
    """Where assets/ lives: the py2app bundle's Resources, PyInstaller's _MEIPASS,
    or this file's directory when running from source."""
    if getattr(sys, "frozen", False):
        return (os.environ.get("RESOURCEPATH")
                or getattr(sys, "_MEIPASS", "")
                or os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def load_html() -> str:
    """The UI, with IBM Plex inlined as base64 @font-face rules.

    The fonts are embedded rather than linked because the page is loaded as a
    string with no base URL, so there is nothing relative to resolve against —
    and because it must render identically offline on both platforms.
    """
    base = resource_base()
    with open(os.path.join(base, "assets", "notula_ui.html"), encoding="utf-8") as fh:
        html = fh.read()
    faces = ""
    try:
        with open(os.path.join(base, "assets", "fonts", "plex_b64.json"), encoding="utf-8") as fh:
            for key, b64 in json.load(fh).items():
                fam, wt = key.split("|")
                faces += (f"@font-face{{font-family:'{fam}';font-style:normal;"
                          f"font-weight:{wt};font-display:swap;"
                          f"src:url(data:font/woff2;base64,{b64}) format('woff2');}}\n")
    except (OSError, ValueError):
        pass
    return html.replace("__FONTS__", faces)


def fmt_elapsed(sec) -> str:
    sec = int(sec or 0)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


log = logging.getLogger("notula")


def error_summary(err: str) -> str:
    """One useful line out of a subprocess failure.

    pipeline raises "whisper-cli failed:\\n<20 lines of its output>", so taking
    the first line — as this used to — reports the literal string "whisper-cli
    failed:" and discards everything that says why. whisper puts its actual
    complaint at the end, so pair our prefix with the last real line.
    """
    lines = [l.strip() for l in (err or "").splitlines() if l.strip()]
    if not lines:
        return "failed"
    if len(lines) == 1:
        return lines[0]
    head = lines[0].rstrip(":")
    # skip whisper's timing summary, which is noise rather than a cause
    for line in reversed(lines[1:]):
        if not line.startswith(("whisper_print_timings", "main:", "system_info")):
            return f"{head} — {line}"[:300]
    return head


def _incomplete_note(path, key) -> str:
    """'half-downloaded' reads very differently from 'missing', and it's the
    difference between the user waiting and the user re-downloading."""
    try:
        have = path.stat().st_size
    except OSError:
        return ""
    want = deps.SIZES.get(key)
    if not want or have >= want * 0.9:
        return ""
    return f"incomplete — {have / (1 << 20):,.0f} of about {want / (1 << 20):,.0f} MB"


def tool_status(cfg) -> list[dict]:
    """Every external dependency, where we looked, and whether it's there.

    One source of truth for `--selftest` and the UI's setup banner, so the two
    can never disagree about what's missing. Existence is re-checked on each
    call: the paths themselves don't move (toolpaths returns its best guess even
    for something absent), so dropping the binaries into place and hitting
    Refresh is enough to clear the banner.
    """
    model_file = pipeline.MODELS_DIR / f"ggml-{cfg['model']}.bin"
    vad = pipeline.find_vad_model()
    return [
        {"key": "ffmpeg", "label": "ffmpeg", "path": str(pipeline.FFMPEG),
         "ok": os.path.exists(pipeline.FFMPEG), "required": True,
         "for": "mixing your microphone with computer audio, and importing files"},
        {"key": "ffprobe", "label": "ffprobe", "path": str(pipeline.FFPROBE),
         "ok": os.path.exists(pipeline.FFPROBE), "required": True,
         "for": "measuring how long a recording is"},
        {"key": "whisper-cli", "label": "whisper-cli", "path": str(pipeline.WHISPER_CLI),
         "ok": os.path.exists(pipeline.WHISPER_CLI), "required": True,
         "for": "transcribing"},
        # complete, not merely present: a truncated .bin makes whisper-cli exit
        # non-zero with nothing useful to say, and never gets re-downloaded
        # because the file is there
        {"key": "model", "label": f"model ggml-{cfg['model']}.bin", "path": str(model_file),
         "ok": deps.model_complete(model_file, f"ggml-{cfg['model']}.bin"),
         "note": _incomplete_note(model_file, f"ggml-{cfg['model']}.bin"),
         "required": True, "for": "transcribing"},
        {"key": "vad", "label": "VAD model",
         "path": str(vad or (pipeline.MODELS_DIR / pipeline.VAD_MODEL)),
         "ok": vad is not None, "required": True, "for": "transcribing"},
        {"key": "whisper-server", "label": "whisper-server", "path": str(live.WHISPER_SERVER),
         "ok": os.path.exists(live.WHISPER_SERVER), "required": False,
         "for": "the live transcript"},
        {"key": "tx-python", "label": "transcription environment",
         "path": str(pipeline.TX_PYTHON), "ok": os.path.exists(str(pipeline.TX_PYTHON)),
         "required": False, "for": "speaker labels"},
    ]


def setup_hint() -> str:
    """The manual fallback, for contexts with no button to press (--selftest, the
    Details dialog). In the UI the Install button is the instruction."""
    if sys.platform == "win32":
        return ("Run:  Notula.exe --install-deps      (or tools\\setup_windows.cmd, "
                "or click Install in the app)")
    return ("Run:  notula.py --install-deps      (or: brew install ffmpeg "
            "whisper-cpp, then fetch the models)")


def setup_summary(cfg) -> dict:
    """What the UI needs to warn about a half-finished install."""
    tools = tool_status(cfg)
    missing = [t for t in tools if t["required"] and not t["ok"]]
    optional = [t for t in tools if not t["required"] and not t["ok"]]
    steps = deps.plan(cfg)
    return {
        "ok": not missing,
        "missing": [{"label": t["label"], "for": t["for"], "note": t.get("note", "")}
                    for t in missing],
        "optional": [{"label": t["label"], "for": t["for"], "note": t.get("note", "")}
                     for t in optional],
        "hint": setup_hint(),
        # what the in-app installer would fetch, so the button can say how big
        # this is before the user commits to it
        "install_bytes": sum(s["bytes"] for s in steps),
        "install_steps": [s["label"] for s in steps],
        "needs_brew": any(s["kind"] == "brew" for s in steps),
        # ffmpeg is the one whose absence costs you audio rather than just
        # blocking a later step, so the banner calls it out separately
        "loses_audio": any(t["key"] == "ffmpeg" and not t["ok"] for t in tools),
    }


def install_deps_cli(live_models: bool = False) -> int:
    """`--install-deps`: the same fetch the UI button does, with no window.

    Exists for the installer's finish page and for setting up a machine over a
    remote shell, so there's one implementation of "get the tools" rather than a
    Python one and a PowerShell one that drift.
    """
    cfg = config.load()
    steps = deps.plan(cfg, live_models)
    if not steps:
        print("Everything is already installed.")
        return 0
    total = sum(s["bytes"] for s in steps)
    print(f"Installing {len(steps)} item(s), about {total / (1 << 30):.1f} GB:")
    for s in steps:
        print(f"  - {s['label']}")
    last = [-1]

    def on_progress(frac, msg, detail):
        pct = int(frac * 100)
        if pct != last[0]:
            last[0] = pct
            print(f"[{pct:3d}%] {msg} {detail}".rstrip(), flush=True)

    import threading as _t
    result = deps.install(steps, on_progress, _t.Event())
    if result.get("ok"):
        print("Done.")
        return 0
    print(f"FAILED: {result.get('error') or 'cancelled'}")
    return 1


def selftest(wav=None) -> int:
    """Headless check of everything the GUI depends on but doesn't contain.

    With no audio file it reports where each external tool and model resolved to
    and stops — which is the fastest way to diagnose a fresh install, since
    "missing model file" is otherwise the app's only symptom for half a dozen
    different mistakes. Given a wav it goes on to run the real transcribe +
    diarize pass.

        Notula.app/Contents/MacOS/Notula --selftest [audio.wav]
        Notula.exe --selftest [audio.wav]

    Exit code 0 = everything needed is present, 1 = something is missing.
    """
    cfg = config.load()
    token = config.hf_token(cfg)

    checks = [(t["label"], t["path"], t["ok"], t["required"]) for t in tool_status(cfg)]
    checks.append(("diarize script", pipeline.DIARIZE_SCRIPT,
                   pipeline.DIARIZE_SCRIPT.exists(), False))

    print(f"SELFTEST: Notula {version.VERSION}  platform={sys.platform} frozen={getattr(sys, 'frozen', False)}")
    print(f"SELFTEST: config         {config.config_path()}")
    print(f"SELFTEST: library        {cfg['library']}")
    print(f"SELFTEST: system audio   {sysaudio.BACKEND} available={sysaudio.AVAILABLE}")
    print(f"SELFTEST: hf token set   {bool(token)}"
          f"{'' if token else '  (no speaker labels without one)'}")
    missing = []
    for label, path, ok, required in checks:
        mark = "ok " if ok else ("MISSING" if required else "absent ")
        print(f"SELFTEST: {mark:8} {label:<16} {path}")
        if not ok and required:
            missing.append(label)

    if missing:
        print(f"SELFTEST: MISSING REQUIRED: {', '.join(missing)}")
        print(f"SELFTEST: {setup_hint()}")

    if not wav:
        print("SELFTEST: environment check only — pass a wav to run the full pass")
        return 1 if missing else 0
    if not os.path.exists(wav):
        print(f"SELFTEST: no such file: {wav}")
        return 2
    if missing:
        return 1

    outdir = os.path.join(os.path.dirname(os.path.abspath(wav)), "selftest_out")
    try:
        res = pipeline.transcribe_meeting(
            wav, outdir, lang=cfg["lang"], hf_token=token,
            progress_cb=lambda s, f, m: print(f"SELFTEST: {s:<10} {f:4.0%} {m}"))
        print(f"SELFTEST: diarized={res['diarized']} warning={res['warning']}")
        print(f"SELFTEST: output={res['output']}")
        print("SELFTEST: OK")
        return 0
    except Exception as e:
        print(f"SELFTEST: FAILED: {e}")
        return 1


class AppCore:
    """The app. One instance, owned by the platform shell."""

    def __init__(self, host):
        self.host = host
        self.cfg = config.load()
        library.ensure_root(self.cfg["library"])
        self._devices = []
        self.mic_idx = None
        self.system_on = False
        self._mute = {"mic": False, "system": False}
        self.monitor = None
        self.rec = None
        self.rec_mid = None
        self.rec_name = ""
        self.rec_seq = 0        # bumped per recording; survives a mid-flight rename
        self._stopping = False
        self._pending_rename = None   # folder move deferred until the WAVs close
        self.live = None        # LiveTranscriber while it's running
        self._live_status = ""
        self._live_lines = []   # committed lines shown/saved (survives model switches)
        self._live_base = []    # lines from earlier transcriber instances
        self._live_pending = ""
        self.tx_mid = None
        self._tx_progress = None
        self._tx_proc = None
        self._tick_count = 0
        self._last_dark = None
        self._torn = False
        self._deps_thread = None      # in-app download of ffmpeg / whisper / models
        self._deps_cancel = None
        self._deps_progress = None

    # ---- Python -> JS helpers (always UI thread) ----

    def _js(self, fn, *args):
        try:
            self.host.js(fn, *args)
        except Exception:
            pass

    def _toast(self, text, kind="info"):
        self._js("showToast", text, kind)

    # ---- theme ----

    def _resolve_theme(self):
        mode = os.environ.get("NOTULA_THEME", "").lower() or self.cfg.get("theme", "auto")
        if mode not in ("auto", "light", "dark"):
            mode = "auto"
        dark = self.host.is_dark() if mode == "auto" else (mode == "dark")
        return mode, dark

    def _apply_theme(self):
        mode, dark = self._resolve_theme()
        self._last_dark = dark
        self._js("applyTheme", mode, dark)

    # ---- devices ----

    def _scan_devices(self):
        self._devices = recorder.list_input_devices()
        self._resolve_selection()
        return self._devices

    def _resolve_selection(self):
        idxs = [d["index"] for d in self._devices]
        mic = self.cfg.get("mic_device")
        self.mic_idx = mic if mic in idxs else recorder.default_mic_index()
        # computer audio comes from the platform backend (ScreenCaptureKit /
        # WASAPI loopback) — never from a loopback driver the user installed
        self.system_on = bool(self.cfg.get("system_capture")) and sysaudio.AVAILABLE

    def _device_name(self, index):
        if index is None:
            return ""
        for d in self._devices:
            if d["index"] == index:
                return d["name"]
        return f"device {index}"

    # ---- lifecycle entry points, called by the host ----

    def on_page_loaded(self):
        """The web view finished loading the UI. Push everything it needs."""
        self._scan_devices()
        self._push_init()
        self._push_meetings()
        self._push_state()
        self._startup_permissions()

    def tick(self):
        """Heartbeat, ~10 Hz, on the UI thread."""
        self._tick_count += 1
        # fast path: stream per-source levels to the meters while recording OR testing
        src = self.rec if (self.rec is not None and self.rec.running) else self.monitor
        if src is not None:
            self._js("notulaLevels", src.levels())
        # ~2 Hz: theme auto-switch + full state snapshot
        if self._tick_count % 5 == 0:
            mode, dark = self._resolve_theme()
            if mode == "auto" and dark != self._last_dark:
                self._apply_theme()
            if (self.rec is not None and not self.rec.running
                    and self.rec_mid is not None and not self._stopping):
                self._finalize_dead_recorder()
            self._push_state()

    def dispatch(self, body):
        """A message from the page. `body` is the decoded postMessage payload."""
        try:
            action = str(body["action"])
        except Exception:
            return
        try:
            self._handle(action, body)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._toast(f"Error: {e}", "err")

    # ---- permissions ----

    def _startup_permissions(self):
        """On launch, check + prompt for whatever this platform gates.

        macOS wants Microphone and (for computer audio) Screen Recording, both
        with real prompts. Windows has no prompt to raise, so `mic_status` just
        reports whether the user has switched access off and we say so.
        """
        st = permissions.mic_status()
        if st == permissions.NOT_DETERMINED:
            permissions.request_mic(
                lambda g: self.host.on_main(self._after_startup_mic, bool(g)))
        elif st in (permissions.DENIED, permissions.RESTRICTED):
            self._toast(f"Microphone is off — turn on Notula in "
                        f"{permissions.MIC_SETTINGS_PATH} so it can record you.", "warn")

        # Screen Recording powers computer-audio capture on macOS; on Windows
        # WASAPI loopback needs nothing, and screen_recording_ok() is always True.
        if self.system_on and not permissions.screen_recording_ok():
            permissions.request_screen_recording()
            self._toast(f"Allow Notula in {permissions.SCREEN_SETTINGS_PATH} to "
                        f"capture computer audio, then relaunch.", "warn")

        # HuggingFace token — needed for speaker diarization; prompt if missing.
        if not config.hf_token(self.cfg):
            self._prompt_hf_token()

    def _prompt_hf_token(self):
        """Launch-time prompt for the HuggingFace token. Skippable — transcripts
        still work without it, just with no speaker labels.

        Asynchronous by contract. This is the one dialog the app opens *by
        itself*, with no user gesture behind it, so it must never block the
        thread that dispatches UI work: a shell that runs modals on that thread
        would freeze the whole window — including its own close button — behind a
        dialog the user may not even be able to see.
        """
        try:
            self.host.prompt_hf_token(self._hf_token_result)
        except Exception:
            self._hf_token_result(None)

    def _hf_token_result(self, tok):        # UI thread
        if tok:
            self.cfg["hf_token"] = tok.strip()
            config.save(self.cfg)
            self._push_init()
            self._toast("Token saved — speaker labels enabled ✓", "ok")
        else:
            self._toast("Skipped — transcripts won't have speaker labels "
                        "until you add a HuggingFace token (⚙).", "warn")

    def _after_startup_mic(self, granted):
        if granted:
            self._toast("Microphone ready", "ok")
        else:
            self._toast(f"Microphone access denied — enable it later in "
                        f"{permissions.MIC_SETTINGS_PATH}.", "warn")

    # ---- pushes to the page ----

    def _push_init(self):
        mode, dark = self._resolve_theme()
        self._last_dark = dark
        token = config.hf_token(self.cfg)
        self._js("notulaInit", {
            "theme": {"mode": mode, "dark": dark},
            "settings": {
                "lang": self.cfg["lang"],
                "model": self.cfg["model"],
                "min_speakers": self.cfg["min_speakers"],
                "max_speakers": self.cfg["max_speakers"],
                "library": self.cfg["library"],
                "auto_transcribe": self.cfg["auto_transcribe"],
                "hf_ok": bool(token),
                "hf_token_masked": ("•" * 12) if self.cfg.get("hf_token") else "",
                "live_enabled": bool(self.cfg.get("live_enabled")),
                "live_model": self.cfg.get("live_model") or live.DEFAULT_MODEL,
            },
            "live_models": live.available_models(),
            "inputs": self._devices,
            "mic_selected": self.mic_idx,
            "system_capture": self.system_on,
            "system_available": sysaudio.AVAILABLE,
            "screen_ok": permissions.screen_recording_ok(),
            # how this platform captures computer audio, so the page can explain
            # it without hardcoding either OS's story
            "system_backend": sysaudio.BACKEND,
            "system_desc": sysaudio.DESCRIPTION,
            "system_permission_hint": sysaudio.PERMISSION_HINT,
            "system_unavailable": sysaudio.UNAVAILABLE_REASON,
            # a half-finished install is the normal first-run state on Windows,
            # so the page says so rather than letting it surface as a failure
            # halfway through a meeting
            "setup": setup_summary(self.cfg),
            "version": version.VERSION,
            "platform": "Windows" if sys.platform == "win32" else "macOS",
        })

    def _push_meetings(self):
        self._js("notulaMeetings", library.list_meetings(self.cfg["library"]))

    def _push_state(self):
        recording = self.rec is not None and self.rec.running
        paused = recording and self.rec.paused
        monitoring = self.monitor is not None
        elapsed = self.rec.elapsed if self.rec else 0.0
        sources = self._device_name(self.mic_idx) + ("  +  computer audio" if self.system_on else "")
        if recording:
            header = f"{'Paused' if paused else 'Recording'}  {fmt_elapsed(elapsed)}"
            rec_tag, rec_kind = ("Paused", "warn") if paused else ("Recording", "err")
            recmeta = ("Paused — this time is left out of the recording"
                       if paused else sources)
        elif monitoring:
            header = "Testing input…"
            rec_tag, rec_kind = "Testing", "info"
            recmeta = sources
        elif self.tx_mid:
            header = "Transcribing…"
            rec_tag, rec_kind = "Busy", "warn"
            recmeta = ""
        else:
            header = "Idle"
            rec_tag, rec_kind = "Idle", ""
            recmeta = "Ready to record" if self.mic_idx is not None else "No microphone found"
        self._js("notulaState", {
            "recording": recording,
            "paused": paused,
            "monitoring": monitoring,
            "elapsed": fmt_elapsed(elapsed),
            "canRecord": (not recording and not monitoring and not self.tx_mid and self.mic_idx is not None),
            "canTest": (not recording and not self.tx_mid and self.mic_idx is not None),
            "txId": self.tx_mid,
            "recId": self.rec_mid if recording else None,
            "recName": self.rec_name if recording else None,
            "live": {
                "on": bool(self.cfg.get("live_enabled")),
                "running": self.live is not None,
                "ready": bool(self.live is not None and self.live.ready),
                "status": self._live_status,
                "model": self.cfg.get("live_model") or live.DEFAULT_MODEL,
            },
            "progress": self._tx_progress,
            "header": header,
            "recTag": rec_tag,
            "recTagKind": rec_kind,
            "recmeta": recmeta,
        })

    # ---- JS -> Python dispatch ----

    def _handle(self, action, body):
        if action == "startRecording":
            self._start_recording(str(body.get("name") or ""))
        elif action == "stopRecording":
            self._stop_recording()
        elif action == "togglePause":
            self._toggle_pause()
        elif action == "setLive":
            self._set_live(bool(body.get("value")))
        elif action == "setLiveModel":
            self._set_live_model(str(body.get("value") or ""))
        elif action == "copyLive":
            self._copy_live()
        elif action == "transcribe":
            self._transcribe(str(body.get("id") or ""),
                             lang=body.get("lang"),
                             min_speakers=body.get("min_speakers"),
                             max_speakers=body.get("max_speakers"))
        elif action == "toggleMonitor":
            self._toggle_monitor()
        elif action == "importAudio":
            self._import_dialog()
        elif action == "copyOutput":
            self._copy_output(str(body.get("id") or ""))
        elif action == "openOutput":
            self._open_path(library.output_path(self.cfg["library"], str(body.get("id") or "")))
        elif action == "reveal":
            self._open_path(library.folder(self.cfg["library"], str(body.get("id") or "")))
        elif action == "rename":
            self._rename(str(body.get("id") or ""), str(body.get("name") or ""))
        elif action == "delete":
            self._delete(str(body.get("id") or ""))
        elif action == "pickMic":
            self._pick_mic(body.get("index"))
        elif action == "setSystemCapture":
            self._set_system_capture(bool(body.get("value")))
        elif action == "setMute":
            self._set_mute(str(body.get("kind") or ""), bool(body.get("muted")))
        elif action == "setField":
            self._set_field(str(body.get("key")), body.get("value"))
        elif action == "setLang":
            self._set_lang(str(body.get("value") or ""))
        elif action == "setToken":
            self._set_token(str(body.get("value") or ""))
        elif action == "setAuto":
            self.cfg["auto_transcribe"] = bool(body.get("value"))
            config.save(self.cfg)
        elif action == "systemHelp":
            self._system_help()
        elif action == "setupHelp":
            self._setup_help()
        elif action == "installDeps":
            self._install_deps(bool(body.get("live")))
        elif action == "cancelDeps":
            self._cancel_deps()
        elif action == "pickLibrary":
            self._pick_library()
        elif action == "setLibrary":
            self._set_library(str(body.get("value") or ""))
        elif action == "openLibrary":
            self._open_path(self.cfg["library"])
        elif action == "refresh":
            self._scan_devices()
            self._push_init()
            self._push_meetings()
            self._toast("Refreshed")
        elif action == "setTheme":
            mode = str(body.get("mode") or "auto")
            if mode in ("auto", "light", "dark"):
                self.cfg["theme"] = mode
                config.save(self.cfg)
                self._apply_theme()
        elif action == "quit":
            self.host.close()

    # ---- recording ----

    def _ensure_mic(self, on_ok):
        """Run on_ok() once microphone access is granted; otherwise guide the user.
        Triggers the OS prompt when the decision hasn't been made yet (macOS);
        on Windows there is no prompt, so the status is already final."""
        st = permissions.mic_status()
        if st == permissions.AUTHORIZED:
            on_ok()
        elif st == permissions.NOT_DETERMINED:
            self._toast("Asking for microphone access…")
            permissions.request_mic(
                lambda g: self.host.on_main(self._mic_result, bool(g), on_ok))
        else:
            self._mic_denied()

    def _mic_result(self, granted, on_ok):
        if granted:
            on_ok()
        else:
            self._mic_denied()

    def _start_recording(self, name):
        if self.rec is not None or self.tx_mid:
            return
        if self.monitor is not None:
            self._stop_monitor()
        if self.mic_idx is None:
            self._toast("No microphone available", "err")
            return
        self._ensure_mic(lambda: self._begin_recording(name))

    def _mic_denied(self):
        self._toast(f"Microphone access is off. Opening {permissions.MIC_SETTINGS_PATH} "
                    f"— turn it on for the app, then Record again.", "err")
        permissions.open_privacy_pane("Microphone")

    def _begin_recording(self, name):
        root = self.cfg["library"]
        mid = library.create_meeting(root, name, self._device_name(self.mic_idx))
        folder = library.folder(root, mid)
        specs = [{"kind": "mic", "index": self.mic_idx}]
        if self.system_on:
            if permissions.screen_recording_ok():
                specs.append({"kind": "system", "system": True})
            else:
                permissions.request_screen_recording()
                self._toast(f"Grant {permissions.SCREEN_SETTINGS_PATH}, then record "
                            f"again to capture computer audio. Microphone only for "
                            f"now.", "warn")
        engine = recorder.RecordingEngine(specs, folder)
        for kind, muted in self._mute.items():
            engine.set_mute(kind, muted)
        try:
            engine.start()
        except Exception as e:
            library.update_meta(root, mid, status=library.ERROR, warning=str(e))
            self._toast(f"Could not start recording: {e}", "err")
            self._push_meetings()
            return
        self.rec = engine
        self.rec_mid = mid
        self.rec_name = library.read_meta(root, mid).get("name") or ""
        self.rec_seq += 1
        self._live_lines, self._live_base, self._live_pending = [], [], ""
        self._pending_rename = None
        self._push_live()
        if self.cfg.get("live_enabled"):
            self._start_live()
        self._push_state()
        self._push_meetings()
        # confirm audio is actually flowing (mic permission / device busy) off the UI thread
        threading.Thread(target=self._await_start, args=(self.rec_seq,), daemon=True).start()

    def _await_start(self, seq):
        rec = self.rec                       # snapshot: UI thread may clear self.rec
        ok = rec.wait_until_started(6.0) if rec else False
        self.host.on_main(self._on_started, seq, ok)

    def _on_started(self, seq, ok):
        # a stop (or a superseding start) already in flight owns finalization.
        # keyed on the session counter, not the id — a rename changes the id.
        if self.rec_seq != seq or self.rec is None or self._stopping:
            return
        if ok:
            src = "mic + computer audio" if self.system_on else "microphone"
            self._toast(f"Recording {src}…", "ok")
        else:
            mid = self.rec_mid
            self.rec, self.rec_mid, self.rec_name = None, None, ""
            library.update_meta(self.cfg["library"], mid, status=library.ERROR,
                                warning="recording did not start")
            self._toast(f"Recording didn't start — check microphone access in "
                        f"{permissions.MIC_SETTINGS_PATH}.", "err")
            self._push_meetings()
        self._push_state()

    def _toggle_pause(self):
        """Pause/resume the running recording. Paused time is dropped at the
        capture callback, so it's absent from audio.wav and the timer freezes —
        as opposed to Mute, which records silence and keeps the timeline."""
        if self.rec is None or self.rec_mid is None or self._stopping:
            return
        if not self.rec.running:
            return
        paused = not self.rec.paused
        self.rec.set_paused(paused)
        library.update_meta(self.cfg["library"], self.rec_mid,
                            status=library.PAUSED if paused else library.RECORDING)
        self._toast("Paused — nothing is being recorded until you resume" if paused
                    else "Recording again", "warn" if paused else "ok")
        self._push_state()
        self._push_meetings()

    def _stop_recording(self):
        if self.rec is None or self.rec_mid is None or self._stopping:
            return
        self._stopping = True
        rec, mid = self.rec, self.rec_mid
        self._stop_live(mid)      # detaches the tap and writes live.txt off-thread
        self._toast("Finishing recording…")
        threading.Thread(target=self._do_stop, args=(rec, mid), daemon=True).start()

    def _do_stop(self, rec, mid):
        final = rec.stop()
        if os.path.exists(final):
            duration = pipeline.probe_duration(pipeline.Path(final)) or rec.elapsed
        else:
            duration = rec.elapsed
        self.host.on_main(self._on_stopped, mid, duration)

    def _on_stopped(self, mid, duration):
        root = self.cfg["library"]
        # The recorder state has to be cleared even if the bookkeeping write
        # fails, or the app wedges: _start_recording, _stop_recording,
        # _toggle_pause and the dead-recorder salvage all bail out early while
        # self.rec / self._stopping are still set, so a single failed meta.json
        # write would mean no further recording until a restart. The audio
        # itself is already safely on disk by this point.
        try:
            library.update_meta(root, mid, status=library.RECORDED, duration=duration)
        except OSError as e:
            self._toast(f"Recording saved, but its details couldn't be written: {e}",
                        "err")
        finally:
            self.rec, self.rec_mid, self.rec_name = None, None, ""
            self._stopping = False
            self._tx_progress = None
        # A rename asked for mid-recording that couldn't move the folder while
        # the WAVs were open. They're closed now, so finish the job.
        pending, self._pending_rename = self._pending_rename, None
        if pending:
            try:
                mid = library.rename_meeting(root, mid, pending)
            except (OSError, ValueError):
                pass          # the display name is already right; keep the folder
        self._push_state()
        self._push_meetings()
        self._toast(f"Saved · {fmt_elapsed(duration)}", "ok")
        if self.cfg.get("auto_transcribe"):
            self._transcribe(mid)

    def _finalize_dead_recorder(self):
        """A capture stream ended on its own (device unplugged/seized). Salvage
        whatever was written and settle the UI instead of wedging."""
        rec, mid = self.rec, self.rec_mid
        self._stop_live(mid)
        self.rec, self.rec_mid, self.rec_name = None, None, ""
        try:
            final = rec.stop()
            dur = pipeline.probe_duration(pipeline.Path(final)) if os.path.exists(final) else rec.elapsed
        except Exception:
            dur = 0.0
        saved = bool(dur and dur > 0.5)
        library.update_meta(self.cfg["library"], mid,
                            status=library.RECORDED if saved else library.ERROR,
                            duration=dur or 0,
                            warning=None if saved else "input device stopped")
        self._toast("Recording ended — input device stopped" if saved
                    else "Recording lost — input device stopped",
                    "warn" if saved else "err")
        self._push_meetings()

    # ---- live transcription (optional, toggleable at any time) ----

    def _start_live(self):
        """Attach the live tier to the running recording. Best-effort: any
        failure reports through _live_status_cb and leaves recording alone."""
        if self.live is not None or self.rec is None or not self.rec.running:
            return
        key = live.resolve_model(self.cfg.get("live_model") or live.DEFAULT_MODEL)
        if key != self.cfg.get("live_model"):
            self.cfg["live_model"] = key          # requested model isn't installed
            config.save(self.cfg)
        lt = live.LiveTranscriber(
            key, self.cfg["lang"],
            on_update=lambda lines, pend: self.host.on_main(self._live_update, lines, pend),
            on_status=lambda kind, msg: self.host.on_main(self._live_status_cb, kind, msg),
            # You/Them comes from the energy split between the two capture
            # sources, so it's only meaningful when both are actually running.
            attribute=bool(self.system_on),
        )
        self.live = lt
        lt.start()
        self.rec.set_tap(lt.feed)   # buffers while the model loads — no lost audio
        self._push_state()

    def _stop_live(self, mid=None):
        """Detach the live tier and persist live.txt off the UI thread."""
        lt, self.live = self.live, None
        if lt is None:
            return
        if self.rec is not None:
            try:
                self.rec.set_tap(None)
            except Exception:
                pass
        self._live_base = list(self._live_lines)   # survive a model switch
        self._live_status = ""
        # snapshot the lines for the writer: starting another recording resets
        # _live_lines on the UI thread, and this thread outlives that
        threading.Thread(target=self._finish_live,
                         args=(lt, mid, list(self._live_lines)), daemon=True).start()

    def _finish_live(self, lt, mid, lines):
        try:
            lt.stop()
        except Exception:
            pass
        if not mid:
            return
        text = "\n".join(l["text"] if not l.get("who") else f"{l['who']}: {l['text']}"
                         for l in lines if (l.get("text") or "").strip())
        if not text.strip():
            return
        try:
            with open(library.path(self.cfg["library"], mid, library.LIVE),
                      "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass

    def _live_update(self, lines, pending):        # UI thread
        self._live_lines = self._live_base + list(lines)
        self._live_pending = pending
        self._push_live()

    def _live_status_cb(self, kind, msg):          # UI thread
        self._live_status = msg or ""
        if kind == "error":
            self._toast(f"Live transcript unavailable: {msg}", "warn")
            self.live = None
        self._push_state()

    def _push_live(self):
        # its own channel: the transcript changes on the live tier's cadence, not
        # the 2 Hz UI heartbeat, and re-sending it at 2 Hz would be wasteful
        self._js("notulaLive", {
            "lines": self._live_lines[-80:],
            "pending": self._live_pending,
            "total": len(self._live_lines),
        })

    def _set_live(self, on):
        self.cfg["live_enabled"] = bool(on)
        config.save(self.cfg)
        if on:
            if self.rec is not None and self.rec.running:
                if self.live is None:
                    self._start_live()
            else:
                self._toast("Live transcript on — it starts with your next recording",
                            "info")
        else:
            if self.live is not None:
                self._stop_live(self.rec_mid)
                self._toast("Live transcript off — the recording is unaffected", "info")
        self._push_state()

    def _set_live_model(self, key):
        if key not in live.MODELS:
            return
        p = live.model_path(key)
        if not p or not p.exists():
            self._toast(f"{live.MODELS[key]['label']} isn't installed — download "
                        f"{live.MODELS[key]['file']} into the whisper models "
                        f"folder first.", "warn")
            self._push_init()
            return
        if key == self.cfg.get("live_model"):
            return
        self.cfg["live_model"] = key
        config.save(self.cfg)
        self._push_init()
        if self.live is not None:      # restart on the new model, keeping the text
            self._stop_live(None)
            self._toast(f"Switching live model to "
                        f"{live.MODELS[key]['label'].split(' —')[0]}…", "info")
            self.host.on_main(self._start_live)
        self._push_state()

    def _copy_live(self):
        text = "\n".join(l["text"] if not l.get("who") else f"{l['who']}: {l['text']}"
                         for l in self._live_lines if (l.get("text") or "").strip())
        if self._live_pending:
            text = (text + "\n" + self._live_pending).strip()
        if not text:
            self._toast("Nothing transcribed yet", "warn")
            return
        self.host.copy_text(text)
        self._toast("Live transcript copied", "ok")

    # ---- input monitor (test devices without recording) ----

    def _toggle_monitor(self):
        if self.monitor is not None:
            self._stop_monitor()
            return
        if self.rec is not None or self.tx_mid:
            return
        if self.mic_idx is None:
            self._toast("No microphone available", "err")
            return
        self._ensure_mic(self._begin_monitor)

    def _begin_monitor(self):
        if self.monitor is not None or self.rec is not None:
            return
        specs = [{"kind": "mic", "index": self.mic_idx}]
        if self.system_on and permissions.screen_recording_ok():
            specs.append({"kind": "system", "system": True})
        eng = recorder.RecordingEngine(specs, self.cfg["library"], write=False)
        for kind, muted in self._mute.items():
            eng.set_mute(kind, muted)
        try:
            eng.start()
        except Exception as e:
            self._toast(f"Couldn't open input: {e}", "err")
            return
        self.monitor = eng
        self._push_state()
        self._toast("Testing input — the meters should move as sound comes in", "ok")

    def _stop_monitor(self):
        m, self.monitor = self.monitor, None
        if m is not None:
            try:
                m.stop_streams()
            except Exception:
                pass
        self._push_state()

    # ---- import a pre-recorded file ----

    def _import_dialog(self):
        # asynchronous for the same reason as pick_folder — see _pick_library
        self.host.pick_media_file(MEDIA_EXTS, self._import_picked)

    def _import_picked(self, path):             # UI thread
        if path:
            self._import_path(path)

    def import_path(self, path):
        """Public: the host calls this for drag-and-drop and 'open with'."""
        self._import_path(path)

    def _import_path(self, path):
        if not path or not os.path.exists(path):
            self._toast("File not found", "warn")
            return
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        if ext not in MEDIA_EXTS:
            self._toast(f"“{os.path.basename(path)}” isn't an audio or video file", "warn")
            return
        if self.tx_mid or self.rec is not None:
            self._toast("Busy — finish the current task first", "warn")
            return
        name = os.path.splitext(os.path.basename(path))[0]
        root = self.cfg["library"]
        mid = library.create_meeting(root, name, "imported")
        library.update_meta(root, mid, status=library.IMPORTING)
        self._push_meetings()
        self._toast(f"Importing “{name}”…")
        threading.Thread(target=self._do_import, args=(mid, path), daemon=True).start()

    def _do_import(self, mid, path):
        wav = library.audio_path(self.cfg["library"], mid)
        ok, err = pipeline.convert_to_wav(path, wav)
        dur = pipeline.probe_duration(pipeline.Path(wav)) if ok and os.path.exists(wav) else 0.0
        self.host.on_main(self._import_done, mid, ok, dur, err)

    def _import_done(self, mid, ok, dur, err):
        if ok:
            library.update_meta(self.cfg["library"], mid,
                                status=library.RECORDED, duration=dur)
            self._toast(f"Imported · {fmt_elapsed(dur)} — ready to transcribe", "ok")
        else:
            library.update_meta(self.cfg["library"], mid,
                                status=library.ERROR, warning=err or "import failed")
            self._toast(f"Import failed: {err}", "err")
        self._push_meetings()

    # ---- transcription ----

    def _transcribe(self, mid, lang=None, min_speakers=None, max_speakers=None):
        if not mid or self.tx_mid or self.rec is not None:
            return
        # per-transcription settings from the dialog also become the saved defaults
        changed = False
        if lang:
            self.cfg["lang"] = str(lang).strip()[:8] or "id"
            changed = True
        for key, val in (("min_speakers", min_speakers), ("max_speakers", max_speakers)):
            if val is not None:
                lo, hi = config._NUM_BOUNDS[key]
                try:
                    self.cfg[key] = min(hi, max(lo, int(val)))
                    changed = True
                except (TypeError, ValueError):
                    pass
        if changed:
            config.save(self.cfg)
        root = self.cfg["library"]
        wav = library.audio_path(root, mid)
        if not os.path.exists(wav):
            self._toast("No recording found for that meeting", "err")
            return
        # Mark it busy before claiming it: if this write fails after tx_mid is
        # set, no worker exists to ever clear it and the app can neither record
        # nor transcribe again.
        try:
            library.update_meta(root, mid, status=library.TRANSCRIBING, warning=None)
        except OSError as e:
            self._toast(f"Could not start transcription: {e}", "err")
            return
        self.tx_mid = mid
        self._tx_progress = {"id": mid, "frac": 0.0, "msg": "starting…"}
        self._push_state()
        self._push_meetings()
        threading.Thread(target=self._do_transcribe, args=(mid, wav), daemon=True).start()

    def _do_transcribe(self, mid, wav):
        root = self.cfg["library"]
        out_dir = library.folder(root, mid)
        token = config.hf_token(self.cfg)

        def on_progress(stage, frac, msg):        # WORKER thread
            self.host.on_main(self._tx_progress_update, mid, frac, msg)

        try:
            result = pipeline.transcribe_meeting(
                wav, out_dir,
                lang=self.cfg["lang"],
                min_speakers=self.cfg["min_speakers"] or None,
                max_speakers=self.cfg["max_speakers"] or None,
                model=self.cfg["model"],
                hf_token=token,
                diarize=True,
                progress_cb=on_progress,
                on_proc=self._set_tx_proc,
            )
            self.host.on_main(self._tx_done, mid, result)
        except pipeline.PipelineError as e:
            self.host.on_main(self._tx_failed, mid, str(e))
        except Exception as e:                     # pragma: no cover
            self.host.on_main(self._tx_failed, mid, f"unexpected error: {e}")

    def _set_tx_proc(self, proc):
        self._tx_proc = proc     # worker thread; plain assignment so teardown can kill it

    def _tx_progress_update(self, mid, frac, msg):
        if self.tx_mid == mid:
            self._tx_progress = {"id": mid, "frac": frac, "msg": msg}

    def _tx_done(self, mid, result):
        root = self.cfg["library"]
        try:
            library.update_meta(
                root, mid, status=library.TRANSCRIBED,
                duration=result.get("duration") or 0,
                diarized=bool(result.get("diarized")),
                warning=result.get("warning"),
            )
        except OSError as e:      # see _on_stopped: never leave tx_mid stuck
            self._toast(f"Transcribed, but its details couldn't be written: {e}", "err")
        finally:
            self.tx_mid = None
            self._tx_progress = None
            self._tx_proc = None
        self._push_state()
        self._push_meetings()
        if result.get("diarized"):
            self._toast("Transcribed with speakers ✓", "ok")
        else:
            why = result.get("warning") or "no diarization"
            low = why.lower()
            if any(k in low for k in ("auth", "token", "401", "403", "gated", "permission")):
                self._toast("Transcribed, but diarization couldn't authenticate — "
                            "check your token and that you accepted the model terms "
                            "at huggingface.co/pyannote/speaker-diarization-community-1.",
                            "warn")
            else:
                self._toast(f"Transcribed (plain — {why})", "warn")

    def _tx_failed(self, mid, err):
        summary = error_summary(err)
        # The full text is the only thing that says *why* — whisper prints its
        # real complaint on the last line, and the first is our own prefix.
        log.error("transcription failed for %s:\n%s", mid, err)
        try:
            library.update_meta(self.cfg["library"], mid, status=library.ERROR,
                                warning=summary)
        except OSError:           # see _on_stopped: never leave tx_mid stuck
            pass
        finally:
            self.tx_mid = None
            self._tx_progress = None
            self._tx_proc = None
        self._push_state()
        self._push_meetings()
        self._toast(f"Transcription failed: {summary}", "err")

    # ---- meeting actions ----

    def _copy_output(self, mid):
        p = library.output_path(self.cfg["library"], mid)
        if not os.path.exists(p):
            self._toast("No output.txt yet", "warn")
            return
        try:
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            self._toast(f"Could not read output: {e}", "err")
            return
        self.host.copy_text(text)
        self._toast("Copied output.txt to clipboard", "ok")

    def _open_path(self, path):
        if not path or not os.path.exists(path):
            self._toast("Not found", "warn")
            return
        osutil.open_path(path)

    def _rename(self, mid, name):
        """Rename a meeting — its display name and the folder it's saved in.

        Renaming the in-progress recording is explicitly supported: the folder
        moves out from under the open WAV handles (which keep writing into it),
        and the engine is repointed so the mixdown at stop lands in the renamed
        session. A meeting that a *subprocess* is writing into (transcribing,
        importing) or one already being finalized can't move, so those wait.
        """
        root = self.cfg["library"]
        name = library.clean_name(name)
        if not mid:
            return
        if not name:
            self._toast("A meeting needs a name", "warn")
            self._push_meetings()
            return
        meta = library.read_meta(root, mid)
        if not meta:
            self._toast("That meeting no longer exists", "warn")
            self._push_meetings()
            return
        if name == (meta.get("name") or ""):
            self._push_meetings()          # no-op: just resync the row
            return

        recording = (mid == self.rec_mid and self.rec is not None and not self._stopping)
        if (mid == self.tx_mid or meta.get("status") == library.IMPORTING
                or (mid == self.rec_mid and not recording)):
            self._toast("That meeting is busy — rename it once it finishes.", "warn")
            self._push_meetings()
            return

        try:
            new_mid = library.rename_meeting(root, mid, name)
        except ValueError as e:
            self._toast(f"Rename failed: {e}", "err")
            self._push_meetings()
            return
        except OSError as e:
            if not recording:
                self._toast(f"Rename failed: {e}", "err")
                self._push_meetings()
                return
            # Renaming the meeting being recorded is a supported gesture, but on
            # Windows the folder can't move while its WAVs are open. Take the
            # half we can do now — the display name — and move the folder once
            # the recording stops and the handles are closed.
            try:
                library.rename_meeting(root, mid, name, move=False)
            except (OSError, ValueError) as e2:
                self._toast(f"Rename failed: {e2}", "err")
                self._push_meetings()
                return
            self._pending_rename = name
            self.rec_name = name
            self._toast(f"Renamed to “{name}” — the folder follows when you stop",
                        "ok")
            self._push_state()
            self._push_meetings()
            return

        if recording:
            self.rec.relocate(library.folder(root, new_mid))
            self.rec_mid = new_mid
            self.rec_name = name
        self._toast(f"Renamed to “{name}”" + (" — the recording follows" if recording else ""),
                    "ok")
        self._push_state()
        self._push_meetings()

    def _delete(self, mid):
        if not mid or mid in (self.rec_mid, self.tx_mid):
            self._toast("That meeting is busy", "warn")
            return
        folder = library.folder(self.cfg["library"], mid)
        if not os.path.isdir(folder):
            return
        meta = library.read_meta(self.cfg["library"], mid)
        if not self.host.confirm(f"Delete “{meta.get('name', mid)}” and its recording?",
                                 "This permanently removes the folder and cannot be undone."):
            return
        try:
            shutil.rmtree(folder)
            self._toast("Deleted")
        except OSError as e:
            self._toast(f"Delete failed: {e}", "err")
        self._push_meetings()

    # ---- settings ----

    def _pick_mic(self, index):
        try:
            self.cfg["mic_device"] = int(index)
        except (TypeError, ValueError):
            self.cfg["mic_device"] = None
        self._resolve_selection()
        config.save(self.cfg)

    def _set_system_capture(self, on):
        self.cfg["system_capture"] = bool(on)
        config.save(self.cfg)
        self._resolve_selection()
        if on and sysaudio.AVAILABLE and not permissions.screen_recording_ok():
            permissions.request_screen_recording()
            self._toast(f"Computer audio needs {permissions.SCREEN_SETTINGS_PATH} — "
                        f"grant it, then it captures on your next recording.", "info")
        self._push_state()

    def _set_mute(self, kind, muted):
        if kind not in ("mic", "system"):
            return
        self._mute[kind] = muted
        for eng in (self.rec, self.monitor):
            if eng is not None:
                eng.set_mute(kind, muted)

    def _set_field(self, key, value):
        if key not in ("min_speakers", "max_speakers"):
            return
        lo, hi = config._NUM_BOUNDS[key]
        try:
            v = int(round(float(value)))
        except (TypeError, ValueError):
            self._js("notulaField", key, self.cfg[key])
            self._toast(f"{key.replace('_', ' ')} must be a number", "warn")
            return
        v = min(hi, max(lo, v))
        self.cfg[key] = v
        config.save(self.cfg)
        self._js("notulaField", key, v)

    def _set_lang(self, value):
        value = (value or "").strip()[:8] or "id"
        self.cfg["lang"] = value
        config.save(self.cfg)

    def _set_token(self, value):
        value = (value or "").strip()
        # ignore the masked placeholder echoed back unchanged
        if value and set(value) == {"•"}:
            return
        self.cfg["hf_token"] = value
        config.save(self.cfg)
        self._push_init()
        self._toast("Token saved" if value else "Token cleared",
                    "ok" if value else "info")

    def _pick_library(self):
        """Browse for a library folder.

        Asynchronous, like every other dialog: the host puts a modal on screen,
        and blocking the dispatch thread behind one that may have opened *behind*
        the main window is how the whole UI freezes with no visible cause.
        """
        self.host.pick_folder("Use folder", self._library_picked)

    def _library_picked(self, path):            # UI thread
        if path:
            self._set_library(path)

    def _set_library(self, path):
        """Point the library at `path`, typed or browsed.

        Verified before it's committed: a path that can't be created or written
        to would otherwise be saved and then fail at the worst possible moment —
        when a recording tries to land in it.
        """
        path = os.path.expanduser((path or "").strip())
        if not path:
            self._toast("A library folder needs a path", "warn")
            self._push_init()
            return
        path = os.path.abspath(path)
        if os.path.normcase(path) == os.path.normcase(self.cfg["library"]):
            self._push_init()                   # no change; just resync the field
            return
        try:
            library.ensure_root(path)
            probe = os.path.join(path, ".notula-write-test")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("")
            os.unlink(probe)
        except OSError as e:
            self._toast(f"Can't use that folder: {e}", "err")
            self._push_init()                   # snap the field back to reality
            return
        self.cfg["library"] = path
        config.save(self.cfg)
        self._push_init()
        self._push_meetings()
        self._toast(f"Library folder is now {path} — existing meetings stay where "
                    f"they are", "ok")

    # ---- installing the external tools, from inside the app ----

    def _install_deps(self, live_models=False):
        """Download ffmpeg / whisper / models without leaving the window.

        Runs on a worker thread: this is gigabytes, and the UI has to stay
        responsive (and cancellable) throughout.
        """
        if self._deps_thread is not None and self._deps_thread.is_alive():
            return
        steps = deps.plan(self.cfg, live_models)
        if not steps:
            self._toast("Everything is already installed", "ok")
            self._push_init()
            return

        need = sum(s["bytes"] for s in steps)
        target = pipeline.Path(pipeline.MODELS_DIR)
        if deps.free_space(target) < need * 1.15:
            self._toast(f"Not enough free space — this needs about "
                        f"{need / (1 << 30):.1f} GB in {target}", "err")
            return

        self._deps_cancel = threading.Event()
        self._deps_progress = {"running": True, "frac": 0.0,
                               "msg": "Starting…", "detail": ""}
        self._push_deps()
        self._deps_thread = threading.Thread(
            target=self._do_install_deps, args=(steps,), daemon=True)
        self._deps_thread.start()

    def _do_install_deps(self, steps):           # worker thread
        def on_progress(frac, msg, detail):
            self.host.on_main(self._deps_update, frac, msg, detail)
        result = deps.install(steps, on_progress, self._deps_cancel)
        self.host.on_main(self._deps_done, result)

    def _deps_update(self, frac, msg, detail):   # UI thread
        self._deps_progress = {"running": True, "frac": frac,
                               "msg": msg, "detail": detail}
        self._push_deps()

    def _deps_done(self, result):                # UI thread
        self._deps_progress = None
        self._push_deps()
        if result.get("cancelled"):
            self._toast("Download stopped — what finished is kept, so it can "
                        "pick up where it left off", "warn")
        elif result.get("ok"):
            self._toast("Everything is installed ✓", "ok")
        else:
            self._toast(f"Install failed: {result.get('error')}", "err")
        # re-checks what's on disk, so the notice clears itself
        self._push_init()
        self._push_meetings()

    def _cancel_deps(self):
        if self._deps_cancel is not None:
            self._deps_cancel.set()
            self._toast("Stopping…", "info")

    def _push_deps(self):
        self._js("notulaSetupProgress", self._deps_progress or {"running": False})

    def _setup_help(self):
        """The full picture: every tool, where Notula looked, and what it's for."""
        rows = []
        for t in tool_status(self.cfg):
            mark = "ok      " if t["ok"] else ("MISSING " if t["required"] else "absent  ")
            rows.append(f"{mark}{t['label']}\n          {t['path']}")
        body = [
            "Notula drives ffmpeg and whisper.cpp rather than bundling them, so "
            "they have to be on this machine. Here is what it found:",
            "",
            "\n".join(rows),
            "",
            setup_hint(),
        ]
        self.host.alert("External tools", "\n".join(body))

    def _system_help(self):
        """Explain how computer audio is captured here — which is a different
        story on each platform, and on neither of them involves a loopback
        driver any more."""
        if not sysaudio.AVAILABLE:
            self.host.alert("Capturing computer audio", sysaudio.UNAVAILABLE_REASON)
            return
        body = [sysaudio.DESCRIPTION, ""]
        if sysaudio.NEEDS_PERMISSION:
            body += [
                f"It needs one permission: {permissions.SCREEN_SETTINGS_PATH}.",
                "",
                "macOS only activates that permission on a relaunch, so after "
                "granting it, quit and reopen Notula once. After that, turning "
                "Computer audio on is all it takes.",
            ]
        else:
            body += [
                "Nothing to install and no permission to grant — it taps the "
                "audio your speakers are already playing.",
                "",
                "It follows your default playback device, so if you switch "
                "output mid-meeting, start the recording again.",
            ]
        self.host.alert("Capturing computer audio", "\n".join(body))

    # ---- teardown ----

    def teardown(self):
        if self._torn:
            return
        self._torn = True
        lt, self.live = self.live, None      # don't orphan whisper-server on quit
        if lt is not None:
            try:
                lt.stop(timeout=2.0)
            except Exception:
                pass
        if self.monitor is not None:
            try:
                self.monitor.stop_streams()
            except Exception:
                pass
        if self._deps_cancel is not None:
            self._deps_cancel.set()      # a half-done download resumes next launch
        if self.rec is not None and self.rec.running:
            try:
                self.rec.stop()     # finalize the WAV so a recording isn't lost on quit
            except Exception:
                pass
        # don't orphan a running whisper/diarize on quit
        osutil.kill_tree(self._tx_proc)
        self._tx_proc = None
