# R&D — live transcription while recording

Research notes behind the **Live transcript** feature. Everything below was
measured on the actual machine (M1 Pro, 10-core, 32 GB, macOS 27) against real
Notula meeting audio in Indonesian, on 2026-08-03.

> **Status: shipped.** `live.py` implements the two-tier recommendation below
> (live model of your choice + the unchanged batch pass at stop). Three things
> the prototype turned up that this research did *not* predict are recorded in
> **[What shipping it changed](#what-shipping-it-changed)** at the bottom.

---

## Verdict

**Feasible, and near-realtime is reachable: ~0.8 s behind live with `small`,
~1.9 s with `large-v3-turbo`**, both behind a persistent `whisper-server` on a
sliding window. Speaker labels stay a batch step at stop.

The narrowness is the finding. Two obvious alternatives died on contact:

| Candidate | Why it's out |
|---|---|
| **NVIDIA Parakeet TDT v3** (the fast one everybody benchmarks) | 25 **European** languages. No Indonesian. |
| **Apple `SFSpeechRecognizer`** (legacy) | `id-ID` exists but `supportsOnDeviceRecognition == False` → audio goes to Apple's servers. Dealbreaker for client calls. |
| **Apple `SpeechTranscriber`** (macOS 26+, on-device only) | 45 locales, **none Indonesian**. Verified by compiling against the framework. |

Because you record in `id`, the whole "use the fast native thing" branch is
closed. Whisper is not a default here — it's the only multilingual option that
runs locally at usable speed.

> If you ever run **English** meetings, Apple's on-device path *is* available
> (`en-US` asset already installed) and would cost almost no battery. Worth
> keeping as a per-language fast path later — not worth the complexity now.

---

## Measured baseline

Warm `whisper-server` (model resident, HTTP round-trip included), Indonesian
meeting audio:

| Window | large-v3-turbo | Implied duty cycle @ 5 s step |
|---:|---:|---:|
| 5 s | **0.83 s** | 17 % |
| 10 s | **0.87 s** | 17 % |
| 30 s | **1.91 s** | 38 % |

Cold `whisper-cli` (what Notula does today), compute excluding model load:

| Clip | large-v3 | turbo | speedup |
|---:|---:|---:|---:|
| 5 s | 1.41 s | 0.91 s | 1.5× |
| 10 s | 1.74 s | 0.94 s | 1.9× |
| 30 s | 4.41 s | 2.06 s | 2.1× |
| 120 s | 15.34 s | 6.04 s | **2.5×** |

Model load: large-v3 ≈ 1.08 s, turbo ≈ 0.56 s. Encoder pass: ≈ 810 ms (large-v3)
/ ≈ 720 ms (turbo) per window.

### The one fact that shapes the whole design

**A 2 s window and a 10 s window cost the same** — 0.85 s vs 0.89 s on turbo.
Whisper's encoder always runs over a padded 30 s mel spectrogram, so window
length is nearly free until you exceed 30 s. Consequences:

1. **Never send short chunks.** 10 s of context costs the same as 2 s and
   transcribes far better. Window length should be set by accuracy, not speed.
2. **Cost per inference is a property of the model, not the chunk.** Which means
   the only real lever on latency is *which model*, plus how often you step.

### The model ladder (Indonesian, 10 s windows, greedy, 8 threads)

Median of 4 runs, full audio context, model resident:

| model | compute | worst | Indonesian quality |
|---|---:|---:|---|
| large-v3 | 1.21–1.40 s | 1.57 s | reference |
| **turbo** | **0.85–0.89 s** | 0.89 s | matches large-v3; better punctuation |
| **small** | **0.28–0.33 s** | 0.56 s | gist correct, loan-words mangled |
| base | 0.13–0.27 s | 0.44 s | **unusable** |

What "mangled" and "unusable" mean concretely. Test audio was a private
recording, so examples here are reduced to isolated words — this repo is public:

```
             loan-word          compound noun         short phrase
turbo        disounding    ✓    penyesuaian     ✓     cuman saya gak   ✓
small        disonding     ~    penyusuaian     ~     bisa, bisa       ~
base         di song ding  ✗    senjata kemasitas ✗   bisa spetio      ✗
```

`small` loses loan-words (`promise`→`promes`, `disounding`→`disonding`) and
occasionally a pronoun, but a reader follows it. `base` invents words. It's out.

### Latency budget

`latency ≈ step + compute`, and `duty = compute / step` has to stay well under
100 % or the stream falls behind and never recovers.

| model | step | duty | worst-case latency | avg |
|---|---:|---:|---:|---:|
| turbo | 3.0 s | 29 % | 3.9 s | 2.4 s |
| turbo | 2.0 s | 44 % | 2.9 s | 1.9 s |
| turbo | 1.5 s | 58 % | 2.4 s | 1.6 s |
| **small** | **1.0 s** | **31 %** | **1.3 s** | **0.8 s** |
| small | 0.5 s | 62 % | 0.8 s | 0.6 s |

**So yes — sub-second is reachable, but only with `small`.** Turbo bottoms out
around 1.6–2 s average. This is the whole trade: one model tier buys you ~1 s of
latency and costs you loan-words.

### Two dead ends (measured, don't retry these)

- **`--audio-ctx` (`-ac`) reduction.** On 2–5 s clips it looked spectacular —
  3× faster with correct text. At realistic 10 s windows it falls apart:
  `ac=768` silently dropped most of a clip's text, and `ac=512` hit repetition
  loops (`"Apa... Apa... Apa..."`) that ran **2.4 s, worst 3.7 s — slower than
  full context.** Shrinking the audio context breaks the positional structure
  Whisper was trained with. Unshippable at any setting I found.
- **Quantization.** q5_0 and q8_0 turbo are *slower* than f16 on Metal
  (1.16 s vs 1.02 s) — the GPU kernels are f16-native and quantized weights add
  dequantization overhead. Quantization is a CPU/memory-bandwidth win, not an
  Apple-Silicon-GPU one. It does cut RAM (1.6 GB → 574 MB), which may matter
  later, but it buys no speed.

---

## Recommended architecture (for discussion)

Three tiers, each correcting the one before it — "fast draft, slow correct":

```
recorder.Source._callback ──► ring buffer (already 16 kHz mono float32)
             │
             ├─ every 1 s, last 10 s ──► small   (0.31 s) ──► provisional text, ~0.8 s behind
             │                                                 greyed / italic
             ├─ every 10 s, last 20 s ─► turbo   (0.89 s) ──► promoted text, replaces the draft
             │                                                 normal weight
             └─ at stop, whole file ───► large-v3 + pyannote ──► output.txt, speaker-labeled
```

Duty cycle: 31 % (small) + 9 % (turbo) ≈ **40 % sustained**. Both models resident
is 0.5 GB + 1.6 GB — fine at 32 GB.

The tiering is what makes `small`'s loan-word errors acceptable: nothing a user
reads stays wrong for more than ~10 s, and the file they actually keep is
produced by large-v3 as it is today. If that feels over-built for v1, **ship
tier 2 only** (turbo at a 2 s step, ~1.9 s average latency, 44 % duty) and add
the fast draft later.

At **stop**, unchanged: the existing `whisper-cli large-v3` + `pyannote` batch
pass writes the authoritative, speaker-labeled `output.txt`. The live text is a
*preview*, explicitly not the deliverable. That keeps today's quality guarantee
intact and makes the live tier failure-tolerant — if it dies mid-meeting, you
lose a convenience, not a transcript.

`recorder.py` already hands us exactly the right input: 16 kHz mono float32 in
1024-frame blocks, which is whisper's native format. No resampling, no new
capture path — the live tier is a second consumer of the existing queue.

### Free speaker attribution, no diarization model

Notula already captures **mic** and **computer audio** as two separate sources.
That is a free, perfect, zero-latency 2-way speaker split for the live pane:

- mic track → **You**
- system track → **Them**

No DIART, no streaming diarization model, no extra compute. It won't separate
three people in the same Zoom call — that's what the batch pyannote pass at stop
is for. But "You / Them" covers most of the value of live labels for the cost of
reading a flag we already have.

Cost: two inference streams instead of one (~34 % duty at 5 s step) — or run
them on alternating steps to stay near 17 %.

### The hard part: reconciliation

Overlapping windows produce overlapping text, and Whisper will revise its own
earlier words as more context arrives. Options, cheapest first:

1. **Commit-on-silence** — use the Silero VAD you already ship
   (`ggml-silero-v6.2.0.bin`) to cut windows at speech boundaries; text before a
   confirmed pause is frozen, text after it is provisional and re-rendered each
   step. Simple, and matches how people read live captions.
2. **LocalAgreement-2** — commit a token only when two consecutive windows agree
   on it. This is what `whisper_streaming` (the "Turning Whisper into Real-Time"
   paper) does; well-trodden, slightly more latency, much less flicker.
3. Naive last-window-wins — text jitters visibly. Don't.

I'd prototype (1), measure flicker, and escalate to (2) only if it's ugly.

---

## Options considered and why they lost

| Option | Verdict |
|---|---|
| **`whisper-server` + turbo, sliding window** | ✅ Recommended. Already installed via brew. Fits Notula's existing subprocess architecture exactly. Measured, works. |
| **`whisper-stream` binary** (also already installed) | Good for a same-day spike to *feel* the latency, but it owns the microphone itself — it can't consume Notula's existing capture, and it would fight over the device. Prototype tool, not shippable. |
| **WhisperKit** (CoreML → Neural Engine) | Genuinely the best-engineered streaming option on Apple Silicon, and ANE means much lower battery cost than Metal. But it's Swift, so it means a native helper process and a real build-system change. Revisit if battery drain proves unacceptable. |
| **Parakeet / parakeet-mlx** | No Indonesian. Dead. |
| **Apple Speech (either API)** | Cloud for `id`, or no `id` at all. Dead. |
| **Cloud streaming ASR** (Deepgram, AssemblyAI, pyannoteAI Live-1) | pyannoteAI's Live-1 does sub-300 ms streaming diarization over WebSocket and would be excellent — but Notula's whole premise is that client negotiations never leave the laptop. Dead on privacy, not on merit. |

---

## Side finding, independent of any of this

**`large-v3-turbo` looks like a straight upgrade for the existing batch
pipeline.** On two Indonesian meeting clips, with Notula's exact production
flags, turbo was **2.3–2.5× faster** and produced markedly better-punctuated,
sentence-level segments:

large-v3 broke the same audio into ~5-word fragments with no terminal
punctuation; turbo returned the same words as complete, punctuated sentences.
(Side-by-side omitted: private recording, public repo.)


Caveat, stated plainly: this is **n = 2 clips, no reference transcripts, so no
WER**. Word content looked comparable; what clearly differed was segmentation
and punctuation — which happens to matter a lot for the pyannote merge step,
since better sentence boundaries mean cleaner speaker-turn alignment. Worth a
proper A/B on 3–4 full meetings before switching the default.

Model is already downloaded to `$(brew --prefix)/share/whisper-cpp/models/ggml-large-v3-turbo.bin`.

---

## Open questions — the next experiments I'd run

1. **Battery and thermals.** ~40 % sustained GPU duty for a 60-minute meeting on
   battery is the biggest unknown, and the most likely reason to abandon the
   Metal path for WhisperKit/ANE (the Neural Engine is far more efficient per
   inference). Needs a `powermetrics` run over a real one-hour meeting, not a
   synthetic loop. **This is the finding most likely to change the design.**
2. **Contention with capture.** Does a Metal inference every 5 s cause dropouts
   in the ScreenCaptureKit or PortAudio callback? Notula drops a block rather
   than stall the audio thread, so the failure mode would be *silent audio loss* —
   this must be measured before shipping, not after.
3. **Real WER on Indonesian**, turbo vs large-v3, on hand-corrected references
   from 3–4 of your own meetings. Decides both the live model and possibly the
   batch default.
4. **Flicker.** How much does the live text visibly rewrite itself with
   commit-on-silence? Determines whether LocalAgreement-2 is needed.
5. **Does the live tier get its own memory budget?** turbo resident is ~1.6 GB
   for the whole meeting, on top of the app. Fine at 32 GB; worth knowing.

## Risks

- **Scope.** A live pane wants a transcript UI, scrollback, copy, and search —
  that's a bigger surface than the feature sounds. Keep v1 to a rolling
  last-N-lines pane with no interaction.
- **Trust.** If live text is visibly worse than the final `output.txt`, users
  will distrust both. The pane must be labelled as a preview, and the final pass
  must remain the deliverable.
- **A second whisper process during recording** is a new way for a recording to
  fail. It must be strictly fire-and-forget: if `whisper-server` dies, recording
  continues and nothing is lost.

---

## What shipping it changed

Three things the measurements above didn't predict, found while building `live.py`:

**1. Timestamps are load-bearing.** The first prototype passed `--no-timestamps`
to `whisper-server` to save a little work. Every window then came back as one
giant segment, the commit anchor never advanced, and the transcript repeated
itself on every step — each committed line contained most of the previous one.
Segment `start`/`end` is what advances the anchor *and* what attributes You/Them.
It stays on.

**2. Window boundaries hallucinate, and silence gating doesn't fix it.** Turbo
produced a confident `"Terima kasih."` in the middle of a pricing negotiation.
The obvious theory — Whisper inventing filler over silence — was wrong: that
region measured −18 dBFS, i.e. continuous speech. The real cause is a window
*starting mid-word*, leaving a fragment Whisper can't parse. The fix is
`KEEP_S` acoustic lookback: begin each window ~1.2 s before the commit anchor so
there's a run-up, and treat everything before the anchor as context. The
energy gate stayed anyway (it's still right, and skipping dead air is free), but
it was not the fix.

**3. Whisper's "words" are tokens, not words.** Trimming the lookback on word
timestamps split Indonesian mid-word — splitting a word across two lines mid-stem. `verbose_json`'s `words` array is really *tokens*,
and a word is often several of them; only a token carrying a leading space
starts a new word. The trim now walks forward to the next such token. This was
invisible on Turbo (whose tokens are mostly whole words) and glaring on Small —
worth remembering that a smaller model degrades tokenisation, not just accuracy.

### Where it landed

Measured on a 40 s Indonesian negotiation clip, fed at realtime pacing:

| | Small (step 1.0 s) | Turbo (step 2.5 s) |
|---|---|---|
| first text | 1.7–2.2 s | 4.8–6.3 s |
| worst step gap | 1.26 s | 2.79 s |
| committed text rewritten | never (0/40) | never (0/16) |
| content vs batch reference | gist, names mangled | near-complete, names correct |

`small` runs proper nouns together and turns `"penyesuaian kapasitas"` into
`"senjata kemasitas"`. Turbo gets both right. That is the whole trade-off, and
it's what the model picker says in the UI.

### Still open

The battery question from the R&D is **unchanged and unmeasured** — a real
one-hour `powermetrics` run on battery is still the experiment most likely to
change the design (and the strongest argument for the WhisperKit/ANE path).
