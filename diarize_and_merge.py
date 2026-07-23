#!/usr/bin/env python3
"""
diarize_and_merge.py — speaker diarization + transcript merge.

RUNS INSIDE THE TRANSCRIPTION VENV (the one with torch / pyannote / soundfile).
notula's pipeline invokes it as a subprocess so the heavy ML deps stay out of
the app's pyobjc venv.

It reads whisper.cpp's JSON transcript, runs pyannote speaker diarization on the
audio, assigns each transcript segment to whichever speaker overlaps it most,
and writes a merged, speaker-labeled transcript.

Progress is streamed to stdout as `@@P <fraction 0..1> <message>` lines so the
parent can drive a progress bar without importing anything. Exit codes:
    0  ok
    2  HuggingFace token missing / auth failure (gated model)
    3  model load / download failure
    1  anything else

Standalone use (outside notula):
    HF_TOKEN=hf_xxx python diarize_and_merge.py \
        --audio audio.wav --whisper-json transcript.json \
        --output transcript.merged.txt [--min-speakers N] [--max-speakers N]
"""

import argparse
import json
import os
import sys
from pathlib import Path

MODEL = "pyannote/speaker-diarization-community-1"


def emit(frac, msg, enabled):
    if enabled:
        print(f"@@P {frac:.3f} {msg}", flush=True)


def load_segments(json_path):
    """whisper.cpp JSON -> [(start_sec, end_sec, text)]. Offsets are in ms."""
    data = json.loads(Path(json_path).read_text("utf-8"))
    segs = []
    for item in data["transcription"]:
        text = item["text"].strip()
        if text:
            segs.append((item["offsets"]["from"] / 1000.0,
                         item["offsets"]["to"] / 1000.0, text))
    return segs


def load_audio(path):
    """Load the WAV as an in-memory {waveform, sample_rate} dict via soundfile,
    bypassing torchcodec entirely — torchcodec needs an exact-matching ffmpeg
    build and is broken in this venv, whereas soundfile has no ffmpeg dependency."""
    import soundfile as sf
    import torch
    waveform, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    return {"waveform": torch.from_numpy(waveform.T), "sample_rate": sample_rate}


def fmt_ts(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def assign_speaker(seg_start, seg_end, turns):
    """Pick whichever diarization turn overlaps this segment the most."""
    best_speaker, best_overlap = "UNKNOWN", 0.0
    for t_start, t_end, speaker in turns:
        overlap = max(0.0, min(seg_end, t_end) - max(seg_start, t_start))
        if overlap > best_overlap:
            best_overlap, best_speaker = overlap, speaker
    return best_speaker


def build_progress_hook(prog):
    """A minimal pyannote hook that forwards step progress to our @@P protocol.

    We deliberately do NOT subclass pyannote's ProgressHook (which draws a rich
    bar to stderr and has version-specific internals); a plain callable matching
    the hook signature is all pyannote needs, and it can't break on an upgrade.
    """
    class _Hook:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __call__(self, step_name, step_artifact, file=None,
                     total=None, completed=None):
            if not total:
                return
            frac = min(1.0, (completed or 0) / total)
            # pyannote drives the hook 0..1 once PER step; give each step its own
            # sub-band so overall progress stays monotonic (segmentation, then
            # embeddings) instead of resetting and being swallowed by the clamp.
            name = (step_name or "").lower()
            if "segment" in name:
                lo, hi = 0.15, 0.50
            elif "embed" in name:
                lo, hi = 0.50, 0.82
            else:
                lo, hi = 0.82, 0.88
            prog(lo + (hi - lo) * frac, f"diarizing… {step_name}")

    return _Hook()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--whisper-json", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--min-speakers", type=int)
    ap.add_argument("--max-speakers", type=int)
    ap.add_argument("--progress", action="store_true")
    args = ap.parse_args()

    def prog(frac, msg):
        emit(frac, msg, args.progress)

    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        print("HF_TOKEN not set", file=sys.stderr)
        sys.exit(2)

    if not Path(args.whisper_json).exists():
        print(f"missing whisper JSON: {args.whisper_json}", file=sys.stderr)
        sys.exit(1)

    import torch
    from pyannote.audio import Pipeline

    prog(0.02, "loading diarization model…")
    try:
        pipeline = Pipeline.from_pretrained(MODEL, token=token)
    except Exception as e:
        msg = str(e).lower()
        gated = any(k in msg for k in ("401", "auth", "gated", "token", "permission"))
        print(f"model load failed: {e}", file=sys.stderr)
        sys.exit(2 if gated else 3)

    prog(0.10, "loading audio…")
    audio_input = load_audio(args.audio)

    call_kwargs = {}
    if args.min_speakers:
        call_kwargs["min_speakers"] = args.min_speakers
    if args.max_speakers:
        call_kwargs["max_speakers"] = args.max_speakers

    def run(device):
        pipeline.to(torch.device(device))
        try:
            with build_progress_hook(prog) as hook:
                return pipeline(audio_input, hook=hook, **call_kwargs)
        except TypeError:
            # some pipelines don't accept a hook kwarg — run without it
            return pipeline(audio_input, **call_kwargs)

    # Prefer Metal (MPS) on Apple Silicon; fall back to CPU if an op is unsupported.
    try:
        if torch.backends.mps.is_available():
            prog(0.12, "diarizing on GPU (Metal)…")
            output = run("mps")
        else:
            prog(0.12, "diarizing on CPU…")
            output = run("cpu")
    except Exception as e:
        print(f"MPS run failed ({e}); falling back to CPU", file=sys.stderr)
        prog(0.12, "diarizing on CPU…")
        output = run("cpu")

    turns = [(turn.start, turn.end, speaker)
             for turn, speaker in output.exclusive_speaker_diarization]

    prog(0.90, "merging transcript…")
    segments = load_segments(args.whisper_json)
    lines, last_speaker = [], None
    for start, end, text in segments:
        speaker = assign_speaker(start, end, turns)
        if speaker != last_speaker:
            lines.append(f"\n[{fmt_ts(start)}] {speaker}:")
            last_speaker = speaker
        lines.append(text)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).strip() + "\n", "utf-8")

    prog(1.0, "done")
    print(f"@@DONE {len(segments)} {len(turns)}", flush=True)


if __name__ == "__main__":
    main()
