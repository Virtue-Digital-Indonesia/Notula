# Notula on Windows

The same app as the macOS build, in a WebView2 window instead of a WKWebView.
All the behaviour is shared code (`appcore.py`); only the shell differs.

Two things are genuinely *better* here:

* **Computer audio needs no permission and no relaunch.** WASAPI loopback taps
  the render endpoint directly. There is no Screen Recording grant to hunt for,
  and nothing to install — no VB-Cable, no Voicemeeter.
* **No code signing to fight.** No Gatekeeper, no right-click-Open dance.

One thing is worse: there's no package manager to assume, so you place the
external binaries yourself. That's what most of this page is about.

---

## 1. What you need

| | |
|---|---|
| Windows | 10 version 1903+ or 11, x64 |
| [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) | preinstalled on Win11 and up-to-date Win10; installer is ~2 MB |
| [Python](https://www.python.org/downloads/windows/) 3.11+ | tick **Add python.exe to PATH** in the installer |

There are two ways in.

**Installed** — run `Notula-Setup-2.0.0-beta1.exe`. Per-user, so no admin prompt,
and it installs the WebView2 Runtime if that's missing. Then launch Notula: it
notices what else it needs and offers to download it from its own window (§2).
(Build the installer yourself with `tools\build_windows.ps1` — see §5.)

**From source** — clone the repo and:

```bat
run.bat                                REM creates .venv, installs deps, launches
tools\setup_windows.cmd                REM fetches ffmpeg + whisper.cpp + models
```

`run.bat --debug` keeps a console window open, which is worth doing the first
time: that's where whisper's stderr and any traceback appear.

Either way, check what the app can actually see:

```bat
.venv\Scripts\python notula_win.py --selftest
```

With no audio file that prints every tool and model it resolved, and where — the
fastest way to spot a misplaced binary.

The app itself says so too: a notice under the header names anything missing and
what it costs you, and Transcribe stays disabled until it's resolved. **ffmpeg is
the one to care about** — without it a recording that captures your microphone
*and* computer audio can't be mixed, so only the microphone is kept. The other
gaps merely block transcription, which is why that one reads as an error and the
rest as a warning.

## 2. The external tools

Notula drives `ffmpeg` and `whisper.cpp` rather than bundling them: they're 3+ GB
together, they update on their own schedule, and which whisper build you want
depends on whether the machine has an NVIDIA GPU.

**The easiest way is to let the app do it.** When something is missing, the
notice under the header has an **Install** button showing how much it will
download. It fetches ffmpeg, the whisper.cpp binaries and the models in the
background, with a progress bar and a Stop button, and the notice clears itself
when it's done. Interrupted downloads resume rather than restart, so stopping
costs you nothing. Headless equivalent:

```bat
Notula.exe --install-deps            REM or: .venv\Scripts\python notula_win.py --install-deps
Notula.exe --install-deps --live     REM also the live-transcript models
```

`tools\setup_windows.cmd` remains as a standalone script for setting a machine up
before the app is installed. It's also safe to re-run. Useful switches:

```bat
tools\setup_windows.cmd -Accel cuda12      REM NVIDIA GPU build (much faster)
tools\setup_windows.cmd -LiveModels        REM also fetch the live-transcript models
tools\setup_windows.cmd -TxVenv            REM also build the torch/pyannote env (§3)
```

The rest of this section is what it does, for when you'd rather do it by hand or
something went wrong. Everything goes in the folder Notula checks first:

```
%LOCALAPPDATA%\Notula\
├── bin\
│   ├── ffmpeg.exe
│   ├── ffprobe.exe
│   ├── whisper-cli.exe
│   ├── whisper-server.exe      (only for the live transcript)
│   └── *.dll                   ← whisper.cpp's DLLs, next to its .exe files
└── models\
    ├── ggml-large-v3.bin          2.9 GB   the transcript
    ├── ggml-silero-v6.2.0.bin     0.8 MB   VAD (any ggml-silero-*.bin works)
    ├── ggml-large-v3-turbo.bin    1.5 GB   optional, live transcript
    └── ggml-small.bin             465 MB   optional, live transcript
```

This folder is checked **before** `PATH` on purpose: an app launched from
Explorer inherits a different, usually smaller `PATH` than one launched from a
terminal, so "it works in cmd but not when I double-click" is otherwise a very
easy trap to fall into.

**ffmpeg** — [gyan.dev builds](https://www.gyan.dev/ffmpeg/builds/) (the
`essentials` zip is enough), or `winget install Gyan.FFmpeg`. Both `ffmpeg.exe`
and `ffprobe.exe` are needed.

**whisper.cpp** — grab a release zip from
[ggml-org/whisper.cpp/releases](https://github.com/ggml-org/whisper.cpp/releases):

| asset | when |
|---|---|
| `whisper-bin-x64.zip` | plain CPU, smallest |
| `whisper-blas-bin-x64.zip` | **default** — noticeably faster on any modern CPU |
| `whisper-cublas-12.4.0-bin-x64.zip` | NVIDIA GPU, several times faster again (~639 MB) |

Two things about the archive: everything is nested under a **`Release\`**
subfolder, and the `.exe` files will not start without the `.dll` files beside
them (`whisper.dll`, `ggml*.dll`, …). So copy the *contents* of `Release\` — at
minimum `whisper-cli.exe`, `whisper-server.exe` and every `.dll` — into `bin\`.
The other ~30 executables in there (`talk-llama`, `parakeet`, the test suite) are
unrelated demos and can be left behind.

**Models** — from the whisper.cpp model repo:

```powershell
$M = "$env:LOCALAPPDATA\Notula\models"
New-Item -ItemType Directory -Force $M | Out-Null
curl.exe -L -o "$M\ggml-large-v3.bin" `
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin
# optional, for the live transcript
curl.exe -L -o "$M\ggml-large-v3-turbo.bin" `
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
curl.exe -L -o "$M\ggml-small.bin" `
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin
```

The VAD model is in a **different** HuggingFace repo from the transcription
models — `ggml-org/whisper-vad`, not `ggerganov/whisper.cpp`:

```powershell
curl.exe -L -o "$M\ggml-silero-v6.2.0.bin" `
  https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin
```

Notula accepts **any** `ggml-silero-*.bin` and prefers the newest, so if that
version has moved on, whatever the repo currently offers will do.

Check the app agrees with you:

```bat
.venv\Scripts\python notula_win.py --selftest            REM what did it find?
.venv\Scripts\python notula_win.py --selftest some.wav   REM ...and does it work?
```

The first prints every tool and model it resolved, marking each `ok` / `MISSING`,
and exits non-zero if a required one is absent. The second goes on to run the
real transcribe + diarize pass and ends in `SELFTEST: OK`.

## 3. Speaker labels (optional)

Diarization runs `pyannote` in a **separate** Python environment, so the
multi-gigabyte torch stack stays out of the GUI process. Same design as macOS.

```bat
tools\setup_windows.cmd -TxVenv              REM or -TxVenv -CudaTorch for a GPU
```

which is equivalent to:

```bat
py -3 -m venv "%LOCALAPPDATA%\Notula\txenv"
"%LOCALAPPDATA%\Notula\txenv\Scripts\pip" install torch pyannote.audio soundfile
```

That exact path is where Notula looks by default; point `NOTULA_TX_PYTHON`
elsewhere if you already have such an environment. Note that the default PyPI
`torch` on Windows is CPU-only — `-CudaTorch` pulls the CUDA build instead, which
is the difference between diarization taking minutes and taking seconds.

You also need a HuggingFace token — the app asks for one on first launch and
explains the two steps (accept the gated model's terms, then create a Read
token). Skipping it is fine: you still get full transcripts, just without
`Speaker 1 / Speaker 2` labels.

On CPU, diarization is slow. With a CUDA torch build it isn't.

## 4. Overrides

Every path is settable, which is also how a portable install works:

| Variable | What |
|---|---|
| `NOTULA_BIN` | folder holding the .exe files |
| `NOTULA_MODELS_DIR` | folder holding `ggml-*.bin` |
| `NOTULA_FFMPEG`, `NOTULA_FFPROBE` | exact binary paths |
| `NOTULA_WHISPER_CLI`, `NOTULA_WHISPER_SERVER` | exact binary paths |
| `NOTULA_TX_PYTHON` | python.exe of the torch/pyannote env |
| `NOTULA_THEME` | `auto` \| `light` \| `dark` |
| `NOTULA_DEBUG` | open WebView2 devtools |

Settings live in `%APPDATA%\Notula\notula.json`; meetings default to
`Documents\Notula\` (resolved through the shell, so a OneDrive-redirected or
localized Documents folder is handled).

## 5. Building the .exe and the installer

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_windows.ps1
```

That installs PyInstaller if needed, builds `dist\Notula\Notula.exe`, smoke-tests
it with `--selftest`, and — if [Inno Setup 6](https://jrsoftware.org/isdl.php) is
present (`winget install JRSoftware.InnoSetup`) — packages
`dist\Notula-Setup-2.0.0-beta1.exe`. Without Inno Setup it stops after the .exe and says
so; `dist\Notula\` is complete and can just be zipped.

The two steps by hand:

```bat
.venv\Scripts\pyinstaller tools\notula_win.spec     REM -> dist\Notula\Notula.exe
iscc tools\installer.iss                            REM -> dist\Notula-Setup-2.0.0-beta1.exe
```

The spec derives its paths from `SPECPATH`, so these work from any working
directory. (The icon comes from `assets\Notula.ico`, regenerated on macOS with
`tools/make_ico.py` and committed — a Windows build never needs to make it.)

One-dir, not one-file: one-file unpacks ~100 MB to a temp directory on every
launch, and this app already points at external binaries whose paths people need
to be able to see.

**What the installer does.** Per-user (no admin prompt), Start Menu and optional
desktop shortcuts, an uninstaller, and it registers Notula as an *"Open with"*
candidate for audio/video rather than seizing the default handler for every
`.mp3` on the machine. It installs the WebView2 Runtime if that's missing, and
its finish page offers to run `setup_windows.ps1`. Uninstalling leaves your
models and settings in place, so reinstalling isn't another 3 GB.

The .exe bundles the window and the capture stack. It does **not** bundle
whisper, ffmpeg, or torch — those stay external and are found exactly as above,
so a built .exe needs the same section 2 setup as running from source.

## 6. How it differs from the macOS build

| | macOS | Windows |
|---|---|---|
| Window | WKWebView (PyObjC) | WebView2 (pywebview) |
| Computer audio | ScreenCaptureKit | WASAPI loopback (PyAudioWPatch) |
| Permission for it | Screen Recording, **+ relaunch** | none |
| Mic permission | TCC prompt | a Settings toggle; no prompt exists |
| Capture format | 16 kHz mono, native | endpoint's format, resampled in-process |
| Drop a file on the window | yes | **no** — use *Import audio…* |
| Settings | `~/.config/notula/` | `%APPDATA%\Notula\` |

**Why no drag-and-drop onto the window.** WebView2 hands the page a `File`
object with no filesystem path — by design, it's the browser security model —
and ffmpeg needs a path. Dropping onto **`Notula.exe`** works (that's a command
line), as does *Open with ▸ Notula*, and the *Import audio…* button is
unaffected.

**Why the sample-rate conversion.** ScreenCaptureKit is *told* to produce 16 kHz
mono. WASAPI loopback has no such option — it only ever produces the endpoint's
own mix format, in practice 48 kHz stereo — so `dsp.py` downmixes and resamples
every block on the way in. It's a windowed-sinc polyphase resampler with a
precomputed kernel bank: ~92 µs per 21 ms block, i.e. 4% of one core at 233×
realtime, with the first alias 94 dB down.

## 7. Troubleshooting

**Start here: there is a log.** `%LOCALAPPDATA%\Notula\notula.log`, rotating,
written whether or not there's a console. Set `NOTULA_DEBUG=1` for a line per
message from the page. It also carries a specific warning for the failure mode
that is otherwise invisible:

```
dispatch thread has been in <name> for over 6s - the UI will not respond
```

Every message from the UI is handled on one thread, so if something blocks it the
window stops responding while still looking perfectly normal. In practice that
means a modal dialog opened *behind* the main window. The log names what it's
stuck in.


**No window, or "could not open a WebView2 window"** — install the WebView2
Runtime (section 1).

**Computer audio greyed out** — `PyAudioWPatch` didn't install. Reinstall with
`.venv\Scripts\pip install -r requirements-win.txt`; the app runs fine without
it, just mic-only.

**Meters don't move** — check Settings › Privacy & security › Microphone, both
the top toggle and "Let desktop apps access your microphone". Windows never
prompts for this, it just silently records nothing.

**Computer audio is silent** — loopback follows the *default playback device*.
If you switch output devices mid-meeting, stop and start the recording again.

**"missing model file"** — run `--selftest` (section 2); it names the exact path
it wanted.

**A console window flashes on every transcription** — shouldn't happen; every
subprocess is spawned with `CREATE_NO_WINDOW`. If you see it, something is
bypassing `osutil.popen_kwargs()`.

---

## Building from macOS, via Parallels

If your Windows machine is a Parallels VM on the same Mac, you don't have to
drive it by hand. `tools/vm/run.sh` does the whole thing over `prlctl exec`:

```bash
./tools/vm/run.sh provision   # install Python + Inno Setup in the guest (once)
./tools/vm/run.sh stage       # copy the repo in, build its venv
./tools/vm/run.sh test        # run tests/run_all.py in the guest
./tools/vm/run.sh build       # -> dist\Notula-Setup-2.0.0-beta1.exe
./tools/vm/run.sh verify      # copy it back to the Mac, then install/run/uninstall it
./tools/vm/run.sh all         # all of the above
```

It needs Parallels Tools in the guest and Mac folder sharing on. Two gotchas it
already works around, in case you adapt it: `prlctl exec` runs as
`NT AUTHORITY\SYSTEM` (so `%LOCALAPPDATA%` is the system profile, not yours),
and the Mac share exposes only Desktop/Documents/Downloads — with dotfile
directories hidden entirely.

## Status: what has and hasn't been run

The port was written on macOS, then audited (7 dimensions, 26 findings raised,
22 confirmed and fixed), then actually executed on Windows 11 ARM64 under
Parallels with an x64 Python.

**Confirmed working on Windows:**

| | |
|---|---|
| `tests/run_all.py` | all three suites pass — 16 + 48 + 20 checks |
| `sounddevice` | imports; 6 input devices enumerated, default selected |
| **WASAPI loopback** | `get_default_wasapi_loopback()` returns a real endpoint at **48 kHz stereo** — exactly the format `dsp.py` converts from |
| Mic consent | reads the ConsentStore registry, reports authorized |
| Dark mode | reads `AppsUseLightTheme` |
| Clipboard | the ctypes Win32 path returns success |
| PyInstaller | builds `Notula.exe` (x64), which starts and runs `--selftest` |
| Inno Setup | builds the installer; it installs 1130 files, the installed exe runs, the uninstaller removes it cleanly |

**One defect that only running it could have found.** On Windows-on-ARM,
`platform.machine()` reports the *hardware* architecture even for an x64 process
under emulation, so sounddevice asks for `libportaudioarm64.dll` — a file its own
wheel does not ship — and the import dies. This hits any x64 Python on an ARM64
Windows box, which now includes Snapdragon laptops as well as VMs on Apple
Silicon. `osutil.ensure_portaudio()` works around it by publishing the x64 DLL
sounddevice *did* ship under the name its own `find_library('portaudio')` probe
looks for. No static review would have caught this.

**Found by the first real run**, and fixed: on a fresh install with no
HuggingFace token, the app opened its token prompt automatically — as a modal
tkinter dialog, on the dispatch thread, *behind* the WebView2 window. The UI
rendered normally and then ignored everything: Test Input did nothing, and the
window could not be closed, because every message queued behind a dialog the user
couldn't see. The prompt now runs on its own thread and reports back, the
remaining dialogs are native message boxes with `MB_TOPMOST`, and the log warns
when the dispatch thread stalls. Two regression tests cover it.

That one is worth remembering as a shape: on Windows the UI thread is *ours*, not
the OS's, so anything blocking on it takes the whole window down with it —
including its close button.

**Still not exercised**, and needing a human at the keyboard rather than a script:

1. **Actually recording.** The VM has no microphone input and nothing playing, so
   no audio has flowed through `recorder.Source` or the loopback source yet.
2. **The WASAPI stall question** — the one premise the design rests on. Play
   audio, stop for several minutes, play again, and check the computer-audio
   track still lines up with the microphone.
3. **The window.** pywebview/WebView2 has never been launched — `prlctl exec`
   runs headless as SYSTEM. Log into the VM and run `run.bat`.
4. **The transcription pipeline**, since the models aren't installed in the VM.
   Run `tools\setup_windows.cmd`, then `--selftest some.wav`.
5. **Native x64 hardware.** Everything above was ARM64 running x64 under
   emulation. The artifacts are x64 and should behave better, not worse, on real
   Intel/AMD hardware — but that is an inference, not a measurement.
