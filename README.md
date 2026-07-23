# Notula

Record meetings, then transcribe + diarize them into a clean, speaker-labeled
`output.txt` you can process further. A small native macOS app: an IBM Carbon UI
in a WKWebView over a Python engine that drives `ffmpeg`, `whisper-cli`, and
`pyannote`.

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
2. **Import** — already have a recording? **Drag an audio/video file onto the
   window** (or click *Import audio…*). It's converted to 16 kHz mono and added
   to your library, ready to transcribe. Handles m4a, mp3, wav, mov, mp4, …
3. **Save** — every meeting is its own folder in your library, with the
   recording and a `meta.json`.
4. **Transcribe** — a small dialog lets you set language + speaker count per
   meeting, then runs `whisper-cli` (large-v3, VAD, your flags) + `pyannote`
   speaker diarization, and writes `output.txt` — the merged, speaker-labeled
   transcript. Progress streams into the UI. If diarization can't run (no token,
   offline), you still get the plain transcript.

App-level settings (HuggingFace token, library folder) live behind the **⚙ gear**
in the header — hidden by default, since day-to-day you only touch record and
transcribe.

## Requirements

Homebrew tools (you already have these from `transcribe.sh`):

```bash
brew install ffmpeg whisper-cpp
# models at $(brew --prefix)/share/whisper-cpp/models/:
#   ggml-large-v3.bin, ggml-silero-v6.2.0.bin
```

Two Python environments — this is deliberate:

- **App venv** (`./.venv`) — `pyobjc` for the window plus `sounddevice` + `numpy`
  for real-time capture/levels/mute. Created automatically by `run.command`, or:
  `python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt`
- **Transcription venv** — `torch` / `pyannote` / `soundfile`. This is the
  existing `../.venv` (the openai-whisper project venv). Notula shells out to it
  for diarization so the heavy ML deps stay out of the GUI. Override the path in
  `pipeline.py` (`TX_PYTHON`) if yours lives elsewhere.

## Run

```bash
./run.command        # double-click in Finder, or run from a terminal
```

## Build the app / DMG

```bash
./.venv/bin/pip install py2app          # once
./.venv/bin/python tools/make_icns.py   # regenerate the icon (once / on icon change)
./.venv/bin/python setup.py py2app      # -> dist/Notula.app  (launches as "Notula")
./tools/build_dmg.sh                    # -> dist/Notula-1.0.dmg (drag to Applications)
```

Verify the bundle's whole AI pipeline (whisper + diarize) without the GUI:

```bash
dist/Notula.app/Contents/MacOS/Notula --selftest some.wav   # prints SELFTEST: OK
```

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

**First launch (unsigned).** The app isn't code-signed/notarized, so Gatekeeper
will block a double-click. Right-click the app → **Open** → **Open** once (or
`xattr -dr com.apple.quarantine /Applications/Notula.app`). The first recording
prompts for **Microphone**; first computer-audio capture prompts for **Screen
Recording** (grant to *Notula* now, not your terminal).

## Output

Each meeting folder (`~/Documents/Notula/<date>_<name>/`):

| file | what |
|---|---|
| `audio.wav` | the recording (16 kHz mono) |
| `meta.json` | name, timestamps, device, status, duration |
| `transcript.json` | whisper raw output |
| `transcript.txt` | plain transcript |
| `transcript.merged.txt` | speaker-labeled transcript |
| **`output.txt`** | **the canonical file** — merged transcript + a small `#` header |

`output.txt`'s header lines start with `#`, so downstream tooling can strip them
with `grep -v '^#'`.

## Settings

Stored privately in `~/.config/notula/notula.json` (owner-only). Editable in the
app's Settings panel:

- **Language** — whisper code (`id`, `en`, …)
- **Min/Max speakers** — `0` = auto-detect
- **HuggingFace token** — needed for pyannote diarization. Also read from
  `$HF_TOKEN` if set. Without it, meetings still transcribe (plain text only).
- **Library folder** — where meeting folders are created

## The two audio sources

- **Microphone** — a PortAudio input device (pick which one). The first time you
  record, macOS prompts for **Microphone** access; without it, capture is silent.
- **Computer audio** — the other participants / any system audio, captured with
  **ScreenCaptureKit** (the same API OBS uses for desktop audio). **No loopback
  driver** (BlackHole etc.) is needed. It requires the **Screen Recording**
  ("Screen & System Audio Recording") permission — macOS asks the first time,
  and you may need to relaunch Notula once after granting it. Toggle it off if
  you only want your mic.

Each source has its own **live waveform + level meter** and **Mute** button. The
two are recorded to separate WAVs and mixed to `audio.wav` on stop.

### Permissions, in short

| Source | Permission | Prompted |
|---|---|---|
| Microphone | Privacy › Microphone | on first record (or app launch) |
| Computer audio | Privacy › Screen Recording | on first record with it enabled |

Because Notula currently runs as plain `python3`, the prompt is attributed to the
launching app (Terminal / iTerm / your IDE). Grant it there. A signed `.app`
bundle (not built yet) would prompt as "Notula" and remember it per-app.

## Microphone permission

The first recording triggers a macOS microphone prompt **for the terminal app**
that launched Notula (Terminal, iTerm, VS Code…), since it runs as plain
`python3`. Allow it there. Bundling a signed `.app` later (not done yet) makes the
prompt appear as "Notula" instead.

## Files

```
notula.py             app shell + WKWebView bridge + orchestration
recorder.py           mic capture engine (sounddevice) + per-source levels/mute/mix
sysaudio.py           computer-audio capture via ScreenCaptureKit (no loopback driver)
permissions.py        macOS mic (AVFoundation) + screen-recording (Quartz) TCC helpers
appicon.py            the Dock/menu icon (waveform tile), drawn at runtime
setup.py              py2app build config (Notula.app)
tools/make_icns.py    render assets/Notula.icns from appicon
tools/build_dmg.sh    package Notula.app into a distributable .dmg
library.py            meeting folders + meta.json
pipeline.py           whisper-cli + diarize + merge (stdlib only; shells out)
diarize_and_merge.py  pyannote diarization (runs in the transcription venv)
config.py             settings (~/.config/notula/notula.json)
assets/notula_ui.html Carbon UI (single file)
assets/fonts/         IBM Plex (base64, inlined at load)
```
