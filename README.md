# Notula

Record meetings, then transcribe + diarize them into a clean, speaker-labeled
`output.txt` you can process further. A small native app for **macOS and
Windows**: an IBM Carbon UI in the system web view over a Python engine that
drives `ffmpeg`, `whisper-cli`, and `pyannote`.

One app, two shells — `appcore.py` holds all the behaviour and knows nothing
about either OS; `notula.py` wraps it in a WKWebView, `notula_win.py` in a
WebView2. Windows setup lives in [docs/windows.md](docs/windows.md).

It's the [`transcribe.sh`](../CKB—Initial/transcribe.sh) pipeline with a record
button and a library in front of it.

```
┌─ Record ──────────────────────────────────────┐
│                   0:42                          │
│  ● Microphone      ▁▃▅▇▅▃▁▂▄  ██  [Mute]        │
│  ● Computer audio  ▁▁▂▁▁▁▁▁▁  ░░  [Mute]        │
│              ●  Record   /   ■  Stop            │
├─ Meetings ─────────────────────────────────────┤
│  Weekly sync   Recorded · 0:42     [Transcribe] │
│  Standup       Transcribed ✓       [Copy][Open] │
└────────────────────────────────────────────────┘
```

## What it does

1. **Record** — captures your **microphone** and (optionally) **computer audio**
   as two independent sources straight to a 16 kHz mono WAV (whisper-native, no
   conversion step). Each source has a **live waveform + level meter** so you can
   see it's capturing, and a **Mute** button you can toggle mid-meeting. Manual
   start/stop, live timer. Multiple sources are mixed on stop.
   **Test input** previews the meters live without recording.
   **Pause** drops audio at the capture callback, so paused time never reaches
   the file and the timer freezes with it — as opposed to **Mute**, which
   records silence and keeps the timeline. (The devices stay open while paused:
   re-acquiring a mic or restarting a ScreenCaptureKit stream mid-meeting can
   fail, and a pause that can't resume is worse than one that keeps the device
   warm. The meters keep moving so you can see the input is still alive.)
2. **Import** — already have a recording (incl. OBS `.mov`/`.mp4`)? **Drop it on
   the window or the Dock icon** (or *Import audio…*, or Open With ▸ Notula). It's
   converted to 16 kHz mono with ffmpeg and added to your library, ready to
   transcribe. Video is fine — the audio track is extracted. m4a, mp3, wav, mov,
   mp4, mkv, aac, …
3. **Save** — every meeting is its own folder in your library, with the
   recording and a `meta.json`.
4. **Rename** — hit **✎** on any meeting (or double-click its name). The display
   name *and* the folder on disk both change; the `YYYY-MM-DD_HHMM_` prefix
   stays, so the library keeps sorting newest-first. You can rename **while
   recording** — type in the *Meeting name* field and press Enter, and the
   session being written follows the new name. A meeting that ffmpeg/whisper is
   currently writing into (importing, transcribing, finishing a stop) waits
   until it's done.
5. **Live transcript** *(optional)* — flip it on and text appears while you're
   still recording, roughly a second behind. **Toggle it on or off at any
   moment**, including mid-meeting; it never touches the recording, and if it
   fails the meeting is unaffected. Pick the model against the trade-off shown
   under the selector:

   | model | behind | trade-off |
   |---|---|---|
   | Small | ~0.8 s | keeps up almost word-for-word, but mangles names and loan-words |
   | **Turbo** | ~2.1 s | **recommended** — words match the final transcript, shorter choppier lines |
   | Large-v3 | ~3.1 s | same model as the final pass, slowest and heaviest on battery |

   Grey italic text is provisional and still being revised; plain text has been
   agreed by two consecutive windows and won't change again. When both audio
   sources are live, lines are labelled **You** / **Them** from the energy split
   between mic and computer audio — no diarization model involved. The rolling
   text is saved as `live.txt` next to the recording.
6. **Transcribe** — a small dialog lets you set language + speaker count per
   meeting, then runs `whisper-cli` (large-v3, VAD, your flags) + `pyannote`
   speaker diarization, and writes `output.txt` — the merged, speaker-labeled
   transcript. Progress streams into the UI. If diarization can't run (no token,
   offline), you still get the plain transcript.

App-level settings (HuggingFace token, library folder) live behind the **⚙ gear**
in the header — hidden by default, since day-to-day you only touch record and
transcribe.

## Requirements

### macOS

Homebrew tools (you already have these from `transcribe.sh`):

```bash
brew install ffmpeg whisper-cpp
# models at $(brew --prefix)/share/whisper-cpp/models/:
#   ggml-large-v3.bin, ggml-silero-v6.2.0.bin        # the final transcript
#   ggml-large-v3-turbo.bin, ggml-small.bin          # optional, for live transcript
```

The live-transcript models are optional — the menu greys out whatever isn't
installed and tells you which file to fetch:

```bash
M=$(brew --prefix)/share/whisper-cpp/models
curl -L -o "$M/ggml-large-v3-turbo.bin" \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
curl -L -o "$M/ggml-small.bin" \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin
```

### Windows

Windows 10 1903+ / 11, the [WebView2
Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) (already there
on Win11), and Python 3.11+. There's no package manager to lean on, so the app
fetches ffmpeg, whisper.cpp and the models itself, into `%LOCALAPPDATA%\Notula` —
a folder it checks before `PATH`. Full walkthrough:
**[docs/windows.md](docs/windows.md)**.

No permission to grant for computer audio here, and no relaunch: WASAPI loopback
needs neither.

### Both

Two Python environments — this is deliberate:

- **App venv** (`./.venv`) — the window (`pyobjc` on macOS, `pywebview` on
  Windows) plus `sounddevice` + `numpy` for real-time capture/levels/mute.
  Created automatically by `run.command` / `run.bat`, or by hand from
  `requirements.txt` / `requirements-win.txt`.
- **Transcription venv** — `torch` / `pyannote` / `soundfile`. On macOS this is
  the existing `../.venv` (the openai-whisper project venv). Notula shells out to
  it for diarization so the heavy ML deps stay out of the GUI. Point
  `$NOTULA_TX_PYTHON` at yours if it lives elsewhere.

Every external path is resolved by `toolpaths.py` and overridable by environment
variable — `NOTULA_BIN`, `NOTULA_MODELS_DIR`, `NOTULA_WHISPER_CLI`,
`NOTULA_FFMPEG`, `NOTULA_TX_PYTHON`.

**You don't have to install those by hand.** If anything is missing, a notice in
the app offers to fetch it — ffmpeg, the whisper binaries and the models — with a
progress bar and a Stop button; downloads resume rather than restart. On Windows
everything is downloaded; on macOS the binaries go through Homebrew (whisper.cpp
publishes no macOS build) and the models are downloaded directly. Same thing
without a window:

```bash
./.venv/bin/python notula.py --install-deps        # macOS
.venv\Scripts\python notula_win.py --install-deps  REM Windows
```

## Run

```bash
./run.command        # macOS — double-click in Finder, or run from a terminal
```
```bat
run.bat              REM Windows — double-click, or run.bat --debug for a console
```

Verify the whole AI pipeline (whisper + diarize) headlessly on either platform:

```bash
./.venv/bin/python notula.py --selftest some.wav        # prints SELFTEST: OK
.venv\Scripts\python notula_win.py --selftest some.wav
```

## Build the app

### macOS — .app / DMG

```bash
./.venv/bin/pip install py2app          # once
./.venv/bin/python tools/make_icns.py   # regenerate the icon (once / on icon change)
./.venv/bin/python setup.py py2app      # -> dist/Notula.app  (launches as "Notula")
./tools/build_dmg.sh                    # -> dist/Notula-2.0.0-beta1.dmg (drag to Applications)
```

Verify the bundle's whole AI pipeline (whisper + diarize) without the GUI:

```bash
dist/Notula.app/Contents/MacOS/Notula --selftest some.wav   # prints SELFTEST: OK
```

### Windows — .exe / installer

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_windows.ps1
```

PyInstaller builds `dist\Notula\Notula.exe`; if Inno Setup 6 is installed it also
packages `dist\Notula-Setup-2.0.0-beta1.exe` — per-user, no admin prompt, installs the
WebView2 Runtime if missing, and offers to fetch ffmpeg/whisper/models on its
finish page. The same "bundles the window, not the transcription stack" split
applies — see [docs/windows.md](docs/windows.md).

**What the bundle contains vs needs.** `Notula.app` embeds its own Python + the
GUI/recording stack (pyobjc, sounddevice, ScreenCaptureKit, numpy). It does **not**
embed the transcription stack — that still shells out to Homebrew `whisper-cli`
and to the transcription venv's python running `diarize_and_merge.py`, resolved
by absolute path. So on **this** machine the DMG is fully functional; on a *fresh*
machine you'd also need `brew install ffmpeg whisper-cpp`, the models, and the
torch/pyannote venv (point `$NOTULA_TX_PYTHON` at it). A few build notes baked in:

- `libportaudio.dylib` is forced out of the app zip (`_sounddevice_data` in
  `packages`) — a dylib can't be `dlopen`'d from inside `python3xx.zip`.
- subprocesses run with a **cleaned env** (no `PYTHONHOME`/`PYTHONPATH` leaking
  into the external tx-venv python) and **explicit UTF-8** decoding.

**Signing.** `build_dmg.sh` signs the app with a stable identity (Developer ID
if present, else your Apple Development cert). **This matters:** macOS keys the
Screen Recording / Microphone permissions to the code signature — an *ad-hoc*
signature (py2app's default) has no stable identity, so the grant never sticks
and the app is re-prompted every launch. With a real identity, the grant
persists. It's signed but **not notarized**, so on a *different* Mac Gatekeeper
still needs a right-click → **Open** once.

**First launch.** On launch Notula prompts for **Microphone** and **Screen
Recording** (grant to *Notula*). ⚠️ **After granting Screen Recording, quit and
reopen Notula** — macOS only activates that permission on a relaunch. After that
it stops asking and computer-audio capture works. (None of this applies on
Windows: there's no signing, and loopback capture needs no permission.)

## Output

Each meeting folder (`~/Documents/Notula/<date>_<name>/`, or
`Documents\Notula\` on Windows):

| file | what |
|---|---|
| `audio.wav` | the recording (16 kHz mono) |
| `meta.json` | name, timestamps, device, status, duration |
| `transcript.json` | whisper raw output |
| `transcript.txt` | plain transcript |
| `transcript.merged.txt` | speaker-labeled transcript |
| `live.txt` | the live preview, if it was on (never the deliverable) |
| **`output.txt`** | **the canonical file** — merged transcript + a small `#` header |

`output.txt`'s header lines start with `#`, so downstream tooling can strip them
with `grep -v '^#'`.

## Settings

Stored privately in `~/.config/notula/notula.json` (owner-only), or
`%APPDATA%\Notula\notula.json`. Editable in the app's Settings panel:

- **Language** — whisper code (`id`, `en`, …)
- **Min/Max speakers** — `0` = auto-detect
- **HuggingFace token** — needed for pyannote diarization. Also read from
  `$HF_TOKEN` if set. Without it, meetings still transcribe (plain text only).
- **Live transcript** — on/off and which model; see *What it does*
- **Library folder** — where meeting folders are created

## The two audio sources

- **Microphone** — a PortAudio input device (pick which one). Opened at 16 kHz
  mono where the OS allows it (always, on CoreAudio) and otherwise at the
  device's own rate and converted in-process.
- **Computer audio** — the other participants / any system audio. **No loopback
  driver** (BlackHole, VB-Cable…) is needed on either platform:

  | | how | permission |
  |---|---|---|
  | macOS | ScreenCaptureKit — the same API OBS uses for desktop audio | **Screen Recording**, plus one relaunch after granting |
  | Windows | WASAPI loopback on the default playback device | none |

  Toggle it off if you only want your mic. On Windows it follows the default
  playback device, so if you switch output mid-meeting, restart the recording.

Each source has its own **live waveform + level meter** and **Mute** button. The
two are recorded to separate WAVs and mixed to `audio.wav` on stop.

### Permissions, in short

| | macOS | Windows |
|---|---|---|
| Microphone | TCC prompt on launch and before recording | a Settings toggle — Windows never prompts, it just records silence |
| Computer audio | Privacy › Screen Recording, prompted on launch | nothing to grant |

Notula checks on startup and prompts for whatever hasn't been decided; if a
permission is off it points you at the right Settings pane. macOS only prompts
once — after that you toggle it in System Settings, and Screen Recording changes
need an app relaunch.

If you run from source rather than a bundle, macOS attributes the prompt to the
launching app (Terminal / iTerm / your IDE), so grant it there. The signed `.app`
prompts as "Notula" and remembers it per-app — which is exactly why
`build_dmg.sh` signs with a stable identity.

## Files

```
appcore.py            the whole app — every behaviour, no platform code
notula.py             macOS shell: NSWindow + WKWebView + native services
notula_win.py         Windows shell: WebView2 (pywebview) + Win32/tkinter services
assets/notula_ui.html Carbon UI (single file, both platforms)
assets/fonts/         IBM Plex (base64, inlined at load)

recorder.py           mic capture engine (sounddevice) + per-source levels/mute/mix
sysaudio.py           computer audio — dispatches to the platform backend
  sysaudio_mac.py       ScreenCaptureKit
  sysaudio_win.py       WASAPI loopback (PyAudioWPatch)
permissions.py        mic / system-audio privacy — dispatches per platform
  permissions_mac.py    AVFoundation + Quartz TCC
  permissions_win.py    consent registry + ms-settings deep links
deps.py               in-app download of ffmpeg / whisper / models, both platforms
dsp.py                downmix + windowed-sinc resampling to 16 kHz (Windows capture)
osutil.py             config dirs, subprocess flags, kill-tree, open path
toolpaths.py          finds ffmpeg / whisper / models / tx-venv per platform

library.py            meeting folders + meta.json
pipeline.py           whisper-cli + diarize + merge (stdlib only; shells out)
live.py               near-realtime transcript via whisper-server
diarize_and_merge.py  pyannote diarization (runs in the transcription venv)
config.py             settings (~/.config/notula/ or %APPDATA%\Notula\)

appicon.py            the Dock/menu icon (waveform tile), drawn at runtime
setup.py              py2app build config (Notula.app)
tools/make_icns.py    render assets/Notula.icns from appicon
tools/make_ico.py     render assets/Notula.ico (the Windows icon) from the same
tools/build_dmg.sh    package Notula.app into a distributable .dmg
tools/notula_win.spec PyInstaller build config (Notula.exe)
tools/installer.iss   Inno Setup config (Notula-Setup-2.0.0-beta1.exe)
tools/build_windows.ps1  build the .exe + installer in one command
tools/setup_windows.ps1  fetch ffmpeg / whisper.cpp / models on Windows
docs/windows.md       Windows setup, packaging, and what's still untested
tests/run_all.py      the suites — resampler, appcore, Windows shell threading
```

## Tests

```bash
./.venv/bin/python tests/run_all.py     # macOS
.venv\Scripts\python tests\run_all.py   REM Windows
```

Plain scripts, no framework. They cover the resampler, the whole `AppCore`
dispatch surface (against a stub host), and the Windows shell's threading model —
none of which needs an audio device, so they're meaningful on both platforms.
For anything that does touch hardware, `--selftest` runs the real pipeline.
