#!/usr/bin/env python3
"""
Notula — record meetings, then transcribe + diarize them into a clean output.txt.

This file is the **macOS shell**: an NSWindow with a WKWebView in it, plus the
dozen native services the app needs (clipboard, file panels, alerts, dark-mode,
marshalling work back to the main thread). Everything the app actually *does*
lives in appcore.py, which knows nothing about AppKit — see notula_win.py for the
same shell built on WebView2.

    ./.venv/bin/python notula.py

Per the WKWebView rules every UI push is marshaled back to the main thread
(AppHelper.callAfter), because evaluateJavaScript is main-thread-only.

Config: ~/.config/notula/notula.json
"""

from __future__ import annotations

import json
import os
import signal
import sys

import objc
from Foundation import NSObject, NSTimer, NSRunLoop, NSMakeRect, NSBundle, NSURL
from PyObjCTools import AppHelper
try:
    from Foundation import NSRunLoopCommonModes
except ImportError:                                    # pragma: no cover
    NSRunLoopCommonModes = "kCFRunLoopCommonModes"
from AppKit import (
    NSApplication, NSWindow, NSMenu, NSMenuItem, NSColor, NSAlert, NSSecureTextField,
    NSOpenPanel, NSPasteboard, NSPasteboardTypeString, NSPasteboardTypeFileURL,
    NSDragOperationCopy, NSDragOperationNone,
    NSApplicationActivationPolicyRegular, NSBackingStoreBuffered,
    NSViewWidthSizable, NSViewHeightSizable,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
)
from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController

import appcore
import appicon
import osutil

HANDLER = "notula"   # must match window.webkit.messageHandlers.<name> in the HTML


# ---- resources ---------------------------------------------------------------

# the UI and its fonts are loaded identically on both platforms
resource_base = appcore.resource_base
load_html = appcore.load_html


def system_dark() -> bool:
    try:
        ap = NSApplication.sharedApplication().effectiveAppearance()
        name = ap.bestMatchFromAppearancesWithNames_(
            ["NSAppearanceNameAqua", "NSAppearanceNameDarkAqua"])
        return "Dark" in str(name)
    except Exception:
        return True


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


# ---- the macOS host ----------------------------------------------------------

class Bridge(NSObject):
    """WKWebView delegate + message handler, and appcore's Host on this platform.

    Everything here is either an ObjC callback the framework hands us, or one of
    the Host methods appcore calls when it needs something only AppKit can do.
    """

    @objc.python_method
    def setup(self):
        self.core = appcore.AppCore(self)
        self._nstimer = None
        self._ticker = None
        return self

    # ---- Host: web view ----

    @objc.python_method
    def js(self, fn, *args):
        payload = ",".join(json.dumps(a) for a in args)
        self.web.evaluateJavaScript_completionHandler_(f"{fn}({payload})", None)

    @objc.python_method
    def on_main(self, fn, *args):
        AppHelper.callAfter(fn, *args)

    @objc.python_method
    def is_dark(self):
        return system_dark()

    @objc.python_method
    def close(self):
        self.win.close()

    # ---- Host: native dialogs ----

    @objc.python_method
    def confirm(self, message, info=""):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(message)
        if info:
            alert.setInformativeText_(info)
        alert.addButtonWithTitle_("Delete")
        alert.addButtonWithTitle_("Cancel")
        return alert.runModal() == 1000    # NSAlertFirstButtonReturn

    @objc.python_method
    def alert(self, title, body):
        a = NSAlert.alloc().init()
        a.setMessageText_(title)
        if body:
            a.setInformativeText_(body)
        a.addButtonWithTitle_("OK")
        a.runModal()

    @objc.python_method
    def prompt_hf_token(self, callback):
        """Token prompt with a third button that opens the two pages you need
        first, then re-asks — the terms have to be accepted before a token works.

        Runs modally on the main thread, which is safe here: NSAlert.runModal
        keeps the run loop pumping, so the rest of the app stays alive. The
        callback is invoked before returning."""
        callback(self._run_hf_prompt())

    @objc.python_method
    def _run_hf_prompt(self):
        while True:
            alert = NSAlert.alloc().init()
            alert.setMessageText_(appcore.HF_PROMPT_TITLE)
            alert.setInformativeText_(appcore.HF_PROMPT_BODY)
            field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 24))
            field.setPlaceholderString_("hf_…")
            alert.setAccessoryView_(field)
            alert.addButtonWithTitle_("Save & enable")
            alert.addButtonWithTitle_("Open HuggingFace…")
            alert.addButtonWithTitle_("Skip")
            try:
                alert.window().setInitialFirstResponder_(field)
            except Exception:
                pass
            resp = alert.runModal()
            if resp == 1000:                      # Save & enable
                return str(field.stringValue()).strip() or None
            if resp == 1001:                      # Open HuggingFace, then re-ask
                osutil.open_url(appcore.HF_MODEL_URL)
                osutil.open_url(appcore.HF_TOKENS_URL)
                continue
            return None                           # Skip

    # Both answer via callback (see appcore.Host). Running the panel inline is
    # safe here — NSOpenPanel.runModal keeps the run loop pumping, so the app
    # stays alive — and the callback fires before returning.

    @objc.python_method
    def pick_media_file(self, exts, callback):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setTitle_("Import a recording to transcribe")
        panel.setAllowedFileTypes_(list(exts))
        callback(panel.URLs()[0].path() if panel.runModal() == 1 else None)

    @objc.python_method
    def pick_folder(self, prompt, callback):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setCanCreateDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setPrompt_(prompt)
        callback(panel.URLs()[0].path() if panel.runModal() == 1 else None)

    @objc.python_method
    def copy_text(self, text):
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)

    # ---- ObjC callbacks ----

    def webView_didFinishNavigation_(self, web, nav):     # WKNavigationDelegate
        self.core.on_page_loaded()

    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        self.core.dispatch(message.body())

    def windowWillClose_(self, note):                     # NSWindowDelegate
        self.teardown()
        AppHelper.stopEventLoop()

    # ---- heartbeat ----

    @objc.python_method
    def start_timer(self):
        # 0.1s base tick: live level meters need to be smooth; the heavier full
        # state push is throttled to ~2 Hz inside AppCore.tick().
        self._ticker = _Ticker.alloc().init().configure(self.core.tick)
        self._nstimer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, self._ticker, b"fire:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self._nstimer, NSRunLoopCommonModes)

    # ---- teardown ----

    @objc.python_method
    def teardown(self):
        if self._nstimer is not None:
            self._nstimer.invalidate()
            self._nstimer = None
        self.core.teardown()
        try:
            self.ucc.removeScriptMessageHandlerForName_(HANDLER)
        except Exception:
            pass


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
                    b.core.import_path(str(f))
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
            self._bridge.core.import_path(path)
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


# shared with the Windows build — see appcore.selftest
_selftest = appcore.selftest


if __name__ == "__main__":
    if "--install-deps" in sys.argv:
        sys.exit(appcore.install_deps_cli("--live" in sys.argv))
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        arg = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        sys.exit(_selftest(arg))
    main()
