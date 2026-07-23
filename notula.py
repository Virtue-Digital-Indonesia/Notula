#!/usr/bin/env python3
"""
Notula — record meetings, then transcribe + diarize them into a clean output.txt.

A native macOS window (WKWebView + IBM Carbon UI) over a small Python engine:
  * recorder.py  — ffmpeg captures an audio device to a 16 kHz mono WAV
  * library.py   — each meeting is a folder with the recording + transcripts
  * pipeline.py  — whisper-cli transcription + pyannote diarization + merge

Recording and transcription run on background threads; per the WKWebView rules
every UI push is marshaled back to the main thread (AppHelper.callAfter) because
evaluateJavaScript is main-thread-only.

    ./.venv/bin/python notula.py

Config: ~/.config/notula/notula.json
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading

import objc
from Foundation import NSObject, NSTimer, NSRunLoop, NSMakeRect, NSBundle, NSURL
from PyObjCTools import AppHelper
try:
    from Foundation import NSRunLoopCommonModes
except ImportError:                                    # pragma: no cover
    NSRunLoopCommonModes = "kCFRunLoopCommonModes"
from AppKit import (
    NSApplication, NSWindow, NSMenu, NSMenuItem, NSColor, NSAlert,
    NSOpenPanel, NSPasteboard, NSPasteboardTypeString, NSPasteboardTypeFileURL,
    NSDragOperationCopy, NSDragOperationNone,
    NSApplicationActivationPolicyRegular, NSBackingStoreBuffered,
    NSViewWidthSizable, NSViewHeightSizable,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
)
from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController

import config
import library
import recorder
import pipeline
import permissions
import sysaudio
import appicon

HANDLER = "notula"   # must match window.webkit.messageHandlers.<name> in the HTML


# ---- resources ---------------------------------------------------------------

def resource_base() -> str:
    if getattr(sys, "frozen", False):
        return os.environ.get("RESOURCEPATH") or os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_html() -> str:
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


def system_dark() -> bool:
    try:
        ap = NSApplication.sharedApplication().effectiveAppearance()
        name = ap.bestMatchFromAppearancesWithNames_(
            ["NSAppearanceNameAqua", "NSAppearanceNameDarkAqua"])
        return "Dark" in str(name)
    except Exception:
        return True


def fmt_elapsed(sec) -> str:
    sec = int(sec or 0)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---- heartbeat ticker --------------------------------------------------------

class _Ticker(NSObject):
    @objc.python_method
    def configure(self, cb):
        self._cb = cb
        return self

    def fire_(self, _timer):                 # ObjC selector b"fire:" — NO decorator
        try:
            self._cb()
        except Exception:
            import traceback
            traceback.print_exc()


# ---- bridge (the whole app) --------------------------------------------------

class Bridge(NSObject):

    @objc.python_method
    def setup(self):
        self.cfg = config.load()
        library.ensure_root(self.cfg["library"])
        self._devices = []
        self.mic_idx = None
        self.system_on = False
        self._mute = {"mic": False, "system": False}
        self.monitor = None
        self.rec = None
        self.rec_mid = None
        self._stopping = False
        self.tx_mid = None
        self._tx_progress = None
        self._tx_proc = None
        self._nstimer = None
        self._ticker = None
        self._tick_count = 0
        self._last_dark = None
        self._torn = False
        return self

    # ---- Python -> JS helpers (always main thread) ----

    @objc.python_method
    def _js(self, fn, *args):
        try:
            payload = ",".join(json.dumps(a) for a in args)
            self.web.evaluateJavaScript_completionHandler_(f"{fn}({payload})", None)
        except Exception:
            pass

    @objc.python_method
    def _toast(self, text, kind="info"):
        self._js("showToast", text, kind)

    # ---- theme ----

    @objc.python_method
    def _resolve_theme(self):
        mode = os.environ.get("NOTULA_THEME", "").lower() or self.cfg.get("theme", "auto")
        if mode not in ("auto", "light", "dark"):
            mode = "auto"
        dark = system_dark() if mode == "auto" else (mode == "dark")
        return mode, dark

    @objc.python_method
    def _apply_theme(self):
        mode, dark = self._resolve_theme()
        self._last_dark = dark
        self._js("applyTheme", mode, dark)

    # ---- devices ----

    @objc.python_method
    def _scan_devices(self):
        self._devices = recorder.list_input_devices()
        self._resolve_selection()
        return self._devices

    @objc.python_method
    def _resolve_selection(self):
        idxs = [d["index"] for d in self._devices]
        mic = self.cfg.get("mic_device")
        self.mic_idx = mic if mic in idxs else recorder.default_mic_index()
        # computer audio is captured via ScreenCaptureKit (no loopback driver)
        self.system_on = bool(self.cfg.get("system_capture")) and sysaudio.AVAILABLE

    @objc.python_method
    def _device_name(self, index):
        if index is None:
            return ""
        for d in self._devices:
            if d["index"] == index:
                return d["name"]
        return f"device {index}"

    # ---- initial push (once the page has loaded) ----

    def webView_didFinishNavigation_(self, web, nav):     # WKNavigationDelegate
        self._scan_devices()
        self._push_init()
        self._push_meetings()
        self._push_state()
        self._startup_permissions()

    @objc.python_method
    def _startup_permissions(self):
        """On launch, check + prompt for both permissions the app needs:
        Microphone (to record you) and Screen Recording (for computer audio)."""
        st = permissions.mic_status()
        if st == permissions.NOT_DETERMINED:
            permissions.request_mic(
                lambda g: AppHelper.callAfter(self._after_startup_mic, bool(g)))
        elif st in (permissions.DENIED, permissions.RESTRICTED):
            self._toast("Microphone is off — turn on Notula in System Settings › "
                        "Privacy & Security › Microphone so it can record you.", "warn")

        # Screen Recording powers computer-audio capture (ScreenCaptureKit).
        if self.system_on and not permissions.screen_recording_ok():
            permissions.request_screen_recording()
            self._toast("Allow Notula in System Settings › Privacy & Security › "
                        "Screen Recording to capture computer audio, then relaunch.",
                        "warn")

    @objc.python_method
    def _after_startup_mic(self, granted):
        if granted:
            self._toast("Microphone ready", "ok")
        else:
            self._toast("Microphone access denied — enable it later in System "
                        "Settings › Privacy & Security › Microphone.", "warn")

    @objc.python_method
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
            },
            "inputs": self._devices,
            "mic_selected": self.mic_idx,
            "system_capture": self.system_on,
            "system_available": sysaudio.AVAILABLE,
            "screen_ok": permissions.screen_recording_ok(),
        })

    @objc.python_method
    def _push_meetings(self):
        self._js("notulaMeetings", library.list_meetings(self.cfg["library"]))

    @objc.python_method
    def _push_state(self):
        recording = self.rec is not None and self.rec.running
        monitoring = self.monitor is not None
        elapsed = self.rec.elapsed if self.rec else 0.0
        sources = self._device_name(self.mic_idx) + ("  +  computer audio" if self.system_on else "")
        if recording:
            header = f"Recording  {fmt_elapsed(elapsed)}"
            rec_tag, rec_kind = "Recording", "err"
            recmeta = sources
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
            "monitoring": monitoring,
            "elapsed": fmt_elapsed(elapsed),
            "canRecord": (not recording and not monitoring and not self.tx_mid and self.mic_idx is not None),
            "canTest": (not recording and not self.tx_mid and self.mic_idx is not None),
            "txId": self.tx_mid,
            "progress": self._tx_progress,
            "header": header,
            "recTag": rec_tag,
            "recTagKind": rec_kind,
            "recmeta": recmeta,
        })

    # ---- heartbeat ----

    @objc.python_method
    def start_timer(self):
        # 0.1s base tick: live level meters need to be smooth; the heavier full
        # state push is throttled to ~2 Hz below.
        self._ticker = _Ticker.alloc().init().configure(self._tick)
        self._nstimer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, self._ticker, b"fire:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self._nstimer, NSRunLoopCommonModes)

    @objc.python_method
    def _tick(self):
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

    # ---- JS -> Python dispatch ----

    def userContentController_didReceiveScriptMessage_(self, ucc, message):   # handler
        body = message.body()
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

    @objc.python_method
    def _handle(self, action, body):
        if action == "startRecording":
            self._start_recording(str(body.get("name") or ""))
        elif action == "stopRecording":
            self._stop_recording()
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
        elif action == "pickLibrary":
            self._pick_library()
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
            self.win.close()

    # ---- recording ----

    @objc.python_method
    def _ensure_mic(self, on_ok):
        """Run on_ok() once microphone access is granted; otherwise guide the user.
        Triggers the macOS prompt when the decision hasn't been made yet."""
        st = permissions.mic_status()
        if st == permissions.AUTHORIZED:
            on_ok()
        elif st == permissions.NOT_DETERMINED:
            self._toast("Asking macOS for microphone access…")
            permissions.request_mic(
                lambda g: AppHelper.callAfter(self._mic_result, bool(g), on_ok))
        else:
            self._mic_denied()

    @objc.python_method
    def _mic_result(self, granted, on_ok):
        if granted:
            on_ok()
        else:
            self._mic_denied()

    @objc.python_method
    def _start_recording(self, name):
        if self.rec is not None or self.tx_mid:
            return
        if self.monitor is not None:
            self._stop_monitor()
        if self.mic_idx is None:
            self._toast("No microphone available", "err")
            return
        self._ensure_mic(lambda: self._begin_recording(name))

    @objc.python_method
    def _mic_denied(self):
        self._toast("Microphone access is off. Opening System Settings › Privacy "
                    "& Security › Microphone — turn it on for the app (or your "
                    "terminal), then Record again.", "err")
        permissions.open_privacy_pane("Microphone")

    @objc.python_method
    def _begin_recording(self, name):
        root = self.cfg["library"]
        mid = library.create_meeting(root, name, self._device_name(self.mic_idx))
        folder = library.folder(root, mid)
        specs = [{"kind": "mic", "index": self.mic_idx}]
        if self.system_on:
            if permissions.screen_recording_ok():
                specs.append({"kind": "system", "sck": True})
            else:
                permissions.request_screen_recording()
                self._toast("Grant Screen Recording (System Settings ▸ Privacy & "
                            "Security ▸ Screen Recording), then record again to "
                            "capture computer audio. Microphone only for now.", "warn")
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
        self._push_state()
        self._push_meetings()
        # confirm audio is actually flowing (mic TCC / device busy) off the main thread
        threading.Thread(target=self._await_start, args=(mid,), daemon=True).start()

    @objc.python_method
    def _await_start(self, mid):
        rec = self.rec                       # snapshot: main thread may clear self.rec
        ok = rec.wait_until_started(6.0) if rec else False
        AppHelper.callAfter(self._on_started, mid, ok)

    @objc.python_method
    def _on_started(self, mid, ok):
        # a stop (or a superseding start) already in flight owns finalization
        if self.rec_mid != mid or self._stopping:
            return
        if ok:
            src = "mic + computer audio" if self.system_on else "microphone"
            self._toast(f"Recording {src}…", "ok")
        else:
            self.rec, self.rec_mid = None, None
            library.update_meta(self.cfg["library"], mid, status=library.ERROR,
                                warning="recording did not start")
            self._toast("Recording didn't start — allow microphone access for your "
                        "terminal in System Settings › Privacy & Security › Microphone.", "err")
            self._push_meetings()
        self._push_state()

    @objc.python_method
    def _stop_recording(self):
        if self.rec is None or self.rec_mid is None or self._stopping:
            return
        self._stopping = True
        rec, mid = self.rec, self.rec_mid
        self._toast("Finishing recording…")
        threading.Thread(target=self._do_stop, args=(rec, mid), daemon=True).start()

    @objc.python_method
    def _do_stop(self, rec, mid):
        final = rec.stop()
        if os.path.exists(final):
            duration = pipeline.probe_duration(pipeline.Path(final)) or rec.elapsed
        else:
            duration = rec.elapsed
        AppHelper.callAfter(self._on_stopped, mid, duration)

    @objc.python_method
    def _on_stopped(self, mid, duration):
        root = self.cfg["library"]
        library.update_meta(root, mid, status=library.RECORDED, duration=duration)
        self.rec, self.rec_mid = None, None
        self._stopping = False
        self._tx_progress = None
        self._push_state()
        self._push_meetings()
        self._toast(f"Saved · {fmt_elapsed(duration)}", "ok")
        if self.cfg.get("auto_transcribe"):
            self._transcribe(mid)

    @objc.python_method
    def _finalize_dead_recorder(self):
        """A capture stream ended on its own (device unplugged/seized). Salvage
        whatever was written and settle the UI instead of wedging."""
        rec, mid = self.rec, self.rec_mid
        self.rec, self.rec_mid = None, None
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

    # ---- input monitor (test devices without recording) ----

    @objc.python_method
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

    @objc.python_method
    def _begin_monitor(self):
        if self.monitor is not None or self.rec is not None:
            return
        specs = [{"kind": "mic", "index": self.mic_idx}]
        if self.system_on and permissions.screen_recording_ok():
            specs.append({"kind": "system", "sck": True})
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

    @objc.python_method
    def _stop_monitor(self):
        m, self.monitor = self.monitor, None
        if m is not None:
            try:
                m.stop_streams()
            except Exception:
                pass
        self._push_state()

    # ---- import a pre-recorded file ----

    @objc.python_method
    def _import_dialog(self):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setTitle_("Import a recording to transcribe")
        panel.setAllowedFileTypes_([
            "wav", "mp3", "m4a", "aac", "aif", "aiff", "flac", "ogg", "oga",
            "opus", "caf", "wma", "mp4", "mov", "m4v", "webm", "mkv", "3gp"])
        if panel.runModal() == 1:
            self._import_path(panel.URLs()[0].path())

    @objc.python_method
    def _import_path(self, path):
        if not path or not os.path.exists(path):
            self._toast("File not found", "warn")
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

    @objc.python_method
    def _do_import(self, mid, path):
        wav = library.audio_path(self.cfg["library"], mid)
        ok, err = pipeline.convert_to_wav(path, wav)
        dur = pipeline.probe_duration(pipeline.Path(wav)) if ok and os.path.exists(wav) else 0.0
        AppHelper.callAfter(self._import_done, mid, ok, dur, err)

    @objc.python_method
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

    @objc.python_method
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
        self.tx_mid = mid
        self._tx_progress = {"id": mid, "frac": 0.0, "msg": "starting…"}
        library.update_meta(root, mid, status=library.TRANSCRIBING, warning=None)
        self._push_state()
        self._push_meetings()
        threading.Thread(target=self._do_transcribe, args=(mid, wav), daemon=True).start()

    @objc.python_method
    def _do_transcribe(self, mid, wav):
        root = self.cfg["library"]
        out_dir = library.folder(root, mid)
        token = config.hf_token(self.cfg)

        def on_progress(stage, frac, msg):        # WORKER thread
            AppHelper.callAfter(self._tx_progress_update, mid, frac, msg)

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
            AppHelper.callAfter(self._tx_done, mid, result)
        except pipeline.PipelineError as e:
            AppHelper.callAfter(self._tx_failed, mid, str(e))
        except Exception as e:                     # pragma: no cover
            AppHelper.callAfter(self._tx_failed, mid, f"unexpected error: {e}")

    @objc.python_method
    def _set_tx_proc(self, proc):
        self._tx_proc = proc     # worker thread; plain assignment so teardown can kill it

    @objc.python_method
    def _tx_progress_update(self, mid, frac, msg):
        if self.tx_mid == mid:
            self._tx_progress = {"id": mid, "frac": frac, "msg": msg}

    @objc.python_method
    def _tx_done(self, mid, result):
        root = self.cfg["library"]
        library.update_meta(
            root, mid, status=library.TRANSCRIBED,
            duration=result.get("duration") or 0,
            diarized=bool(result.get("diarized")),
            warning=result.get("warning"),
        )
        self.tx_mid = None
        self._tx_progress = None
        self._tx_proc = None
        self._push_state()
        self._push_meetings()
        if result.get("diarized"):
            self._toast("Transcribed with speakers ✓", "ok")
        else:
            why = result.get("warning") or "no diarization"
            self._toast(f"Transcribed (plain — {why})", "warn")

    @objc.python_method
    def _tx_failed(self, mid, err):
        library.update_meta(self.cfg["library"], mid, status=library.ERROR,
                            warning=err.splitlines()[0] if err else "failed")
        self.tx_mid = None
        self._tx_progress = None
        self._tx_proc = None
        self._push_state()
        self._push_meetings()
        self._toast(f"Transcription failed: {err.splitlines()[0] if err else ''}", "err")

    # ---- meeting actions ----

    @objc.python_method
    def _copy_output(self, mid):
        p = library.output_path(self.cfg["library"], mid)
        if not os.path.exists(p):
            self._toast("No output.txt yet", "warn")
            return
        try:
            text = open(p, encoding="utf-8").read()
        except OSError as e:
            self._toast(f"Could not read output: {e}", "err")
            return
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        self._toast("Copied output.txt to clipboard", "ok")

    @objc.python_method
    def _open_path(self, path):
        if not path or not os.path.exists(path):
            self._toast("Not found", "warn")
            return
        subprocess.Popen(["open", path])

    @objc.python_method
    def _delete(self, mid):
        if not mid or mid in (self.rec_mid, self.tx_mid):
            self._toast("That meeting is busy", "warn")
            return
        folder = library.folder(self.cfg["library"], mid)
        if not os.path.isdir(folder):
            return
        meta = library.read_meta(self.cfg["library"], mid)
        if not self._confirm(f"Delete “{meta.get('name', mid)}” and its recording?",
                             "This permanently removes the folder and cannot be undone."):
            return
        try:
            shutil.rmtree(folder)
            self._toast("Deleted")
        except OSError as e:
            self._toast(f"Delete failed: {e}", "err")
        self._push_meetings()

    @objc.python_method
    def _confirm(self, message, info=""):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(message)
        if info:
            alert.setInformativeText_(info)
        alert.addButtonWithTitle_("Delete")
        alert.addButtonWithTitle_("Cancel")
        return alert.runModal() == 1000    # NSAlertFirstButtonReturn

    # ---- settings ----

    @objc.python_method
    def _pick_mic(self, index):
        try:
            self.cfg["mic_device"] = int(index)
        except (TypeError, ValueError):
            self.cfg["mic_device"] = None
        self._resolve_selection()
        config.save(self.cfg)

    @objc.python_method
    def _set_system_capture(self, on):
        self.cfg["system_capture"] = bool(on)
        config.save(self.cfg)
        self._resolve_selection()
        if on and sysaudio.AVAILABLE and not permissions.screen_recording_ok():
            permissions.request_screen_recording()
            self._toast("Computer audio uses Screen Recording — grant it in System "
                        "Settings, then it captures on your next recording.", "info")
        self._push_state()

    @objc.python_method
    def _set_mute(self, kind, muted):
        if kind not in ("mic", "system"):
            return
        self._mute[kind] = muted
        for eng in (self.rec, self.monitor):
            if eng is not None:
                eng.set_mute(kind, muted)

    @objc.python_method
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

    @objc.python_method
    def _set_lang(self, value):
        value = (value or "").strip()[:8] or "id"
        self.cfg["lang"] = value
        config.save(self.cfg)

    @objc.python_method
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

    @objc.python_method
    def _pick_library(self):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setPrompt_("Use folder")
        if panel.runModal() == 1:
            path = panel.URLs()[0].path()
            self.cfg["library"] = path
            config.save(self.cfg)
            library.ensure_root(path)
            self._push_init()
            self._push_meetings()
            self._toast("Library folder set", "ok")

    @objc.python_method
    def _system_help(self):
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Capturing computer audio")
        alert.setInformativeText_(
            "macOS can't record system / other-participant audio without a "
            "loopback audio driver. To set it up:\n\n"
            "1.  Install BlackHole:   brew install blackhole-2ch\n"
            "2.  In Audio MIDI Setup, create an Aggregate Device combining your "
            "microphone + BlackHole 2ch.\n"
            "3.  Create a Multi-Output Device (BlackHole + your speakers) and set "
            "it as the system output, so meeting audio plays into BlackHole while "
            "you still hear it.\n"
            "4.  Back in Notula, choose BlackHole (or the Aggregate) as the "
            "Computer audio device.\n\n"
            "Full steps are in the README.")
        alert.addButtonWithTitle_("OK")
        alert.runModal()

    # ---- teardown ----

    @objc.python_method
    def teardown(self):
        if self._torn:
            return
        self._torn = True
        if self._nstimer is not None:
            self._nstimer.invalidate()
        if self.monitor is not None:
            try:
                self.monitor.stop_streams()
            except Exception:
                pass
        if self.rec is not None and self.rec.running:
            try:
                self.rec.stop()     # finalize the WAV so a recording isn't lost on quit
            except Exception:
                pass
        p = self._tx_proc           # don't orphan a running whisper/diarize on quit
        if p is not None and p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                try:
                    p.terminate()
                except Exception:
                    pass
        try:
            self.ucc.removeScriptMessageHandlerForName_(HANDLER)
        except Exception:
            pass

    def windowWillClose_(self, note):                 # NSWindowDelegate
        self.teardown()
        AppHelper.stopEventLoop()


class AppDelegate(NSObject):
    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True

    def application_openFiles_(self, app, files):
        # Dropping a file on the Dock icon, "Open With ▸ Notula", or `open -a
        # Notula file.mov` — import + convert each. The most reliable drag path.
        b = getattr(self, "bridge", None)
        if b is not None:
            for f in files:
                try:
                    b._import_path(str(f))
                except Exception:
                    pass
        try:
            app.replyToOpenOrPrint_(0)   # NSApplicationDelegateReplySuccess
        except Exception:
            pass

    def applicationWillTerminate_(self, note):
        # Cmd-Q / menu Quit terminate: without unwinding main()'s finally, so run
        # teardown here too (finalize the recording, kill transcription children).
        b = getattr(self, "bridge", None)
        if b is not None:
            try:
                b.teardown()
            except Exception:
                pass


class DropWebView(WKWebView):
    """WKWebView that also accepts dropped audio/video files, for drag-to-import."""

    def initWithFrame_configuration_(self, frame, conf):
        self = objc.super(DropWebView, self).initWithFrame_configuration_(frame, conf)
        if self is not None:
            self._bridge = None
            self.registerForDraggedTypes_([NSPasteboardTypeFileURL])
        return self

    @objc.python_method
    def _dropped_file(self, sender):
        pb = sender.draggingPasteboard()
        try:
            urls = pb.readObjectsForClasses_options_(
                [NSURL], {"NSPasteboardURLReadingFileURLsOnly": True})
        except Exception:
            try:
                urls = pb.readObjectsForClasses_options_([NSURL], None)
            except Exception:
                urls = None
        for u in (urls or []):
            p = u.path()
            if p and os.path.isfile(p):
                return p
        return None

    def draggingEntered_(self, sender):
        return NSDragOperationCopy if self._dropped_file(sender) else NSDragOperationNone

    def draggingUpdated_(self, sender):
        return NSDragOperationCopy if self._dropped_file(sender) else NSDragOperationNone

    def prepareForDragOperation_(self, sender):
        return bool(self._dropped_file(sender))

    def performDragOperation_(self, sender):
        path = self._dropped_file(sender)
        if path and getattr(self, "_bridge", None) is not None:
            self._bridge._import_path(path)
            return True
        return False


# ---- menu + main -------------------------------------------------------------

def build_menu(app):
    main = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    main.addItem_(app_item)
    m = NSMenu.alloc().init()
    m.addItemWithTitle_action_keyEquivalent_("Hide Notula", b"hide:", "h")
    m.addItem_(NSMenuItem.separatorItem())
    m.addItemWithTitle_action_keyEquivalent_("Quit Notula", b"terminate:", "q")
    app_item.setSubmenu_(m)
    edit_item = NSMenuItem.alloc().init()
    main.addItem_(edit_item)
    em = NSMenu.alloc().initWithTitle_("Edit")
    for title, sel, key in (("Undo", b"undo:", "z"), ("Redo", b"redo:", "Z"),
                            ("Cut", b"cut:", "x"), ("Copy", b"copy:", "c"),
                            ("Paste", b"paste:", "v"), ("Select All", b"selectAll:", "a")):
        em.addItemWithTitle_action_keyEquivalent_(title, sel, key)
    edit_item.setSubmenu_(em)
    app.setMainMenu_(main)


def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    # present as "Notula" with a real icon instead of the generic "Python"
    try:
        info = NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = "Notula"
    except Exception:
        pass
    try:
        app.setApplicationIconImage_(appicon.make_icon())
    except Exception:
        pass
    build_menu(app)

    bridge = Bridge.alloc().init().setup()
    delegate = AppDelegate.alloc().init()
    delegate.bridge = bridge           # so applicationWillTerminate_ can reach teardown
    app.setDelegate_(delegate)
    bridge._app_delegate = delegate    # keep a strong ref

    style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
             | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 900, 700), style, NSBackingStoreBuffered, False)
    win.setTitle_("Notula")
    win.setReleasedWhenClosed_(False)
    win.setMinSize_((560, 520))
    win.setDelegate_(bridge)
    win.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(
        0.086, 0.086, 0.086, 1.0))
    bridge.win = win

    conf = WKWebViewConfiguration.alloc().init()
    ucc = WKUserContentController.alloc().init()
    ucc.addScriptMessageHandler_name_(bridge, HANDLER)
    conf.setUserContentController_(ucc)
    bridge.ucc = ucc

    web = DropWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, 900, 700), conf)
    web._bridge = bridge
    web.setNavigationDelegate_(bridge)
    web.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    try:
        web.setValue_forKey_(False, "drawsBackground")
    except Exception:
        pass
    bridge.web = web
    win.contentView().addSubview_(web)

    web.loadHTMLString_baseURL_(load_html(), None)
    bridge.start_timer()
    win.center()
    win.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)

    signal.signal(signal.SIGTERM, lambda *_: AppHelper.stopEventLoop())
    try:
        AppHelper.runEventLoop()
    finally:
        bridge.teardown()


def _selftest(wav):
    """Headless end-to-end check of the AI pipeline — run the SAME transcribe +
    diarize path the GUI uses, but with no window. Used to verify a built .app
    can still find whisper-cli, the transcription venv, the diarize script, and
    the models.  Usage:  Notula.app/Contents/MacOS/Notula --selftest audio.wav"""
    if not wav or not os.path.exists(wav):
        print("SELFTEST: pass a path to a wav file"); return 2
    cfg = config.load()
    token = config.hf_token(cfg)
    print(f"SELFTEST: frozen={getattr(sys, 'frozen', False)}")
    print(f"SELFTEST: whisper-cli   {pipeline.WHISPER_CLI}  exists={os.path.exists(pipeline.WHISPER_CLI)}")
    print(f"SELFTEST: diarize script {pipeline.DIARIZE_SCRIPT}  exists={pipeline.DIARIZE_SCRIPT.exists()}")
    print(f"SELFTEST: tx python      {pipeline.TX_PYTHON}  exists={os.path.exists(str(pipeline.TX_PYTHON))}")
    print(f"SELFTEST: hf token set   {bool(token)}")
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


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        arg = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        sys.exit(_selftest(arg))
    main()
