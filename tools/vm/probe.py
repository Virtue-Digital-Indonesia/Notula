"""Audio stack probe, going through Notula's own modules (so the PortAudio
workaround in osutil.ensure_portaudio is exercised the way the app uses it)."""
import platform
import sys

sys.path.insert(0, r"C:\notula")

print("machine        =", platform.machine())
print("64-bit process =", sys.maxsize > 2 ** 32)

try:
    import recorder
    print("recorder       = OK (sounddevice imported)")
    for d in recorder.list_input_devices():
        print(f"   [{d['index']}] {d['name'][:44]:44} ch={d['channels']} loopback={d['loopback']}")
    print("default mic    =", recorder.default_mic_index())
except Exception as e:
    print("recorder       = FAILED:", type(e).__name__, e)

try:
    import sysaudio
    print("sysaudio       =", sysaudio.BACKEND, "available =", sysaudio.AVAILABLE)
    if sysaudio.AVAILABLE:
        import sysaudio_win
        import pyaudiowpatch as pa
        p = pa.PyAudio()
        lb = sysaudio_win._default_loopback(p)
        if lb:
            print("   loopback    =", lb["name"][:50])
            print("   rate        =", lb["defaultSampleRate"], " channels =", lb["maxInputChannels"])
        else:
            print("   loopback    = NONE FOUND")
        p.terminate()
except Exception as e:
    print("sysaudio       = FAILED:", type(e).__name__, e)

try:
    import permissions
    print("mic_status     =", permissions.mic_status(), "(3 = authorized)")
    print("screen ok      =", permissions.screen_recording_ok())
except Exception as e:
    print("permissions    = FAILED:", e)

try:
    import notula_win
    print("theme dark     =", notula_win.system_dark())
    print("clipboard      =", notula_win.set_clipboard("notula clipboard test"))
except Exception as e:
    print("notula_win     = FAILED:", type(e).__name__, e)
