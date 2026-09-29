"""
Exercise the cloud engine without an OpenAI account: the pure parts directly,
and whole runs against a stub HTTP server on localhost that answers the way
/v1/audio/transcriptions does when streaming — the same event types, fields
and usage shapes measured against the real API. Needs ffmpeg for the
end-to-end half (the same ffmpeg the app needs); that half is skipped, not
failed, without it.
"""
import base64
import io
import json
import math
import os
import pathlib
import re
import struct
import sys
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cloud, library, pipeline          # noqa: E402

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


DIAR, PLAIN = "gpt-4o-transcribe-diarize", "gpt-transcribe"

# ---- pricing -------------------------------------------------------------------
e = cloud.estimate(3600, DIAR)
check("an hour on diarize is priced at the measured rate, not the published one",
      abs(e["usd"] - 3600 / 60 * cloud.MODELS[DIAR]["usd_per_min"]) < 1e-9
      and cloud.MODELS[DIAR]["usd_per_min"] > cloud.MODELS[DIAR]["published_usd_per_min"], e["usd"])
check("estimate text is money, not a float", e["text"] == "about $1.08", e["text"])
check("a learned rate replaces the starting one",
      abs(cloud.estimate(3600, DIAR, rate=0.02)["usd"] - 1.20) < 1e-9)
e = cloud.estimate(59.2, PLAIN)
check("billed seconds round up", e["seconds"] == 60, e["seconds"])
check("gpt-transcribe is $0.0045 a minute", abs(e["usd"] - 0.0045) < 1e-9, e["usd"])
check("exactly one model labels speakers, and it's the default",
      [m["key"] for m in cloud.available_models() if m["diarize"]] == [cloud.DEFAULT_MODEL])
menu = {m["key"]: m for m in cloud.available_models({DIAR: 0.021})}
check("the menu says where each rate comes from",
      menu[DIAR]["rate_source"] == "learned" and menu[DIAR]["usd_per_min"] == 0.021
      and menu[PLAIN]["rate_source"] == "billed", {k: v["rate_source"] for k, v in menu.items()})
check("the menu says 'measured' before there is a run to learn from",
      {m["key"]: m for m in cloud.available_models()}[DIAR]["rate_source"] == "measured")

# the real 3-minute measurement: 2,242 input + 4,923 output tokens
usd, exact = cloud.usage_cost(DIAR, {"type": "tokens", "input_tokens": 2242,
                                     "output_tokens": 4923, "total_tokens": 7165}, 180)
check("token usage is priced at the token rates", abs(usd - (2242 * 2.5 + 4923 * 10) / 1e6) < 1e-12
      and exact, usd)
usd, exact = cloud.usage_cost(PLAIN, {"type": "duration", "seconds": 60}, 60)
check("duration usage is priced per minute, exactly", abs(usd - 0.0045) < 1e-12 and exact)
usd, exact = cloud.usage_cost(DIAR, None, 120)
check("no usage falls back to length × rate, marked not exact",
      abs(usd - 2 * cloud.MODELS[DIAR]["usd_per_min"]) < 1e-12 and not exact)

check("a tiny sum is 'less than a cent'", cloud.fmt_usd(0.004) == "less than $0.01")
check("nothing is $0.00", cloud.fmt_usd(0) == "$0.00")
check("thousands get a comma", cloud.fmt_usd(1234.5) == "$1,234.50", cloud.fmt_usd(1234.5))
check("an unknown model falls back to the default",
      cloud.estimate(60, "gpt-9-imaginary")["model"] == cloud.DEFAULT_MODEL)

eta = cloud.eta_seconds(5842, DIAR) / 60
check("a 97-minute meeting takes minutes, not most of an hour", 8 < eta < 18, f"{eta:.1f} min")
check("the plain model is much quicker", cloud.eta_seconds(5842, PLAIN) < cloud.eta_seconds(5842, DIAR) / 4)
check("nothing takes no time", cloud.eta_seconds(0, DIAR) == 0)
seq = cloud.eta_seconds(5842, DIAR, parallel=False)
check("one part at a time takes much longer", seq > 2.5 * cloud.eta_seconds(5842, DIAR),
      f"{seq / 60:.1f} min")
check("a plain model ignores the switch",
      cloud.eta_seconds(5842, PLAIN, parallel=False) == cloud.eta_seconds(5842, PLAIN)
      and cloud.runs_parallel(PLAIN, False) and not cloud.runs_parallel(DIAR, False))

# ---- chunk planning ------------------------------------------------------------
check("a short file is one chunk", cloud.plan_chunks(200, []) == [(0.0, 200.0)])
check("an empty file is no chunks", cloud.plan_chunks(0, []) == [])
spans = cloud.plan_chunks(3000, [], chunk_s=1200, slack=90)
check("no pauses: cuts on the clock", spans == [(0.0, 1200.0), (1200.0, 2400.0), (2400.0, 3000.0)], spans)
spans = cloud.plan_chunks(3000, [400.0, 1150.0, 1230.0, 2600.0], chunk_s=1200, slack=90)
check("a cut snaps to the nearest pause", spans[0] == (0.0, 1230.0), spans)
check("a pause outside the slack is ignored", spans[1] == (1230.0, 2430.0), spans)
check("chunks cover the file with no gap",
      spans[0][0] == 0 and spans[-1][1] == 3000 and
      all(spans[i][1] == spans[i + 1][0] for i in range(len(spans) - 1)))
check("no part is longer than the API allows", cloud.CHUNK_S + cloud.CHUNK_SLACK_S < 1400)

# ---- output shaping --------------------------------------------------------------
segs = [
    {"start": 0.0, "end": 2.5, "text": "Halo semua.", "speaker": "SPEAKER_00"},
    {"start": 2.5, "end": 4.0, "text": "Lanjut ya.", "speaker": "SPEAKER_00"},
    {"start": 4.0, "end": 6.0, "text": "Siap.", "speaker": "SPEAKER_01"},
    {"start": 6.0, "end": 6.1, "text": "   ", "speaker": "SPEAKER_01"},
]
merged = cloud.merged_text(segs)
check("merged text has the pyannote layout",
      merged == "[00:00:00] SPEAKER_00:\nHalo semua.\nLanjut ya.\n\n[00:00:04] SPEAKER_01:\nSiap.\n",
      repr(merged))
wj = cloud.whisper_json(segs)
check("transcript.json is whisper-shaped", "transcription" in wj and len(wj["transcription"]) == 3)
check("offsets are milliseconds", wj["transcription"][1]["offsets"] == {"from": 2500, "to": 4000})

# ---- speakers ----------------------------------------------------------------------
sp = cloud._Speakers()
sp.begin_chunk()
a, b = sp.resolve("A"), sp.resolve("B")
check("letters become numbers in order of appearance", (a, b) == ("SPEAKER_00", "SPEAKER_01"))
sp.note_span(a, 0, 3); sp.note_span(b, 3, 4); sp.note_span(b, 4, 9)
check("the longest span per speaker is kept", sp.refs[b][0] == 5.0, sp.refs[b])
check("references go to the most talkative first", sp.known() == [b, a], sp.known())
sp.begin_chunk()
check("a known name comes back as itself", sp.resolve("SPEAKER_01") == "SPEAKER_01")
check("a letter in a later chunk is a new speaker", sp.resolve("A") == "SPEAKER_02")
for i in range(6):
    sp.note_span(sp.resolve(f"X{i}"), i * 10, i * 10 + 5)
check("references are capped at four", len(sp.known()) == 4, sp.known())

# ---- multipart / errors ----------------------------------------------------------
body, ctype = cloud._multipart([("model", "m"), ("known_speaker_names[]", "SPEAKER_00"),
                                ("known_speaker_names[]", "SPEAKER_01")],
                               [("file", "a.mp3", b"\xff\xfb\x00", "audio/mpeg")])
boundary = ctype.split("boundary=")[1]
check("multipart declares its boundary", body.count(("--" + boundary).encode()) == 5)
check("repeated names are repeated fields", body.count(b'name="known_speaker_names[]"') == 2)
check("an API error message is surfaced",
      cloud._error_message(400, b'{"error":{"message":"Audio too long"}}') == "Audio too long")

# ---- saved parts, as the library sees them ------------------------------------------
TMP = pathlib.Path(tempfile.mkdtemp(prefix="notula-cloud-"))
m_dir = TMP / "meeting"
m_dir.mkdir()
check("no saved parts reads as none", library.cloud_resume(str(m_dir)) is None)
pc = cloud._PartCache(m_dir, DIAR, "id")
pc.save(0.0, 300.0, {"start": 0.0, "end": 300.0, "segments": []})
pc.save(300.0, 612.5, {"start": 300.0, "end": 612.5, "segments": []})
info = library.cloud_resume(str(m_dir))
check("the library totals saved parts from their names",
      info and info["done_s"] == 612.5 and info["model"] == DIAR and info["lang"] == "id", info)
check("saved seconds count for the same model and language",
      cloud.saved_seconds(m_dir, DIAR, " ID ") == 612.5)
check("...and not for another model", cloud.saved_seconds(m_dir, PLAIN, "id") == 0.0)
check("...or another language", cloud.saved_seconds(m_dir, DIAR, "en") == 0.0)
check("a saved part loads back", pc.load(300.0, 612.5) == {"start": 300.0, "end": 612.5, "segments": []})
cloud._PartCache(m_dir, DIAR, "en")
check("a run in another language drops parts that don't fit it",
      library.cloud_resume(str(m_dir))["done_s"] == 0.0)

# ---- whole runs against a stub server ---------------------------------------------------
if not os.path.exists(pipeline.FFMPEG) or not os.path.exists(pipeline.FFPROBE):
    print(f"SKIP  end-to-end: ffmpeg/ffprobe not at {pipeline.FFMPEG}")
else:
    wav = TMP / "audio.wav"
    SR = 16000
    # 52 s: four 10 s tones each followed by 1 s of silence, then 8 s — so 10 s
    # parts have a pause to snap onto at 10.5, 21.5, 32.5 and 43.5
    frames = io.BytesIO()
    for k in range(5):
        n_s = 10 if k < 4 else 8
        for i in range(n_s * SR):
            frames.write(struct.pack("<h", int(0.3 * 32767 * math.sin(2 * math.pi * (220 + 55 * k) * i / SR))))
        if k < 4:
            frames.write(b"\x00\x00" * SR)
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(frames.getvalue())

    LOCK = threading.Lock()
    REQ = []                 # one dict per request, in arrival order
    STATE = {"inflight": 0, "max_inflight": 0, "mode": {}, "attempts": {}}
    USAGE_TOK = {"type": "tokens", "total_tokens": 3000, "input_tokens": 1000,
                 "input_token_details": {"text_tokens": 20, "audio_tokens": 980}, "output_tokens": 2000}
    PART_COST = (1000 * 2.5 + 2000 * 10) / 1e6

    class Stub(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _sse(self, events, pause=0.02, stall_after=None, stall_s=0):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for k, ev in enumerate(events):
                if stall_after is not None and k == stall_after:
                    threading.Event().wait(stall_s)       # goes quiet mid-stream
                    return
                chunk = f"data: {json.dumps(ev)}\n\n".encode()
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
                threading.Event().wait(pause)
            end = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(end):x}\r\n".encode() + end + b"\r\n0\r\n\r\n")
            self.wfile.flush()

        def _json(self, code, obj, headers=None):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            boundary = self.headers.get("Content-Type", "").split("boundary=")[1].encode()
            fields, part_no = {}, None
            for chunk in raw.split(b"--" + boundary)[1:-1]:
                head, _, data = chunk.partition(b"\r\n\r\n")
                name = re.search(rb'name="([^"]+)"', head).group(1).decode()
                if name == "file":
                    part_no = int(re.search(rb'filename="part-(\d+)\.mp3"', head).group(1))
                else:
                    fields.setdefault(name, []).append(data[:-2].decode("utf-8", "replace"))
            with LOCK:
                STATE["inflight"] += 1
                STATE["max_inflight"] = max(STATE["max_inflight"], STATE["inflight"])
                n_try = STATE["attempts"].get(part_no, 0) + 1
                STATE["attempts"][part_no] = n_try
                rec = {"part": part_no, "fields": fields, "t0": time.monotonic(),
                       "auth": self.headers.get("Authorization"), "try": n_try}
                REQ.append(rec)
            try:
                self._answer(part_no, n_try, fields)
            finally:
                with LOCK:
                    STATE["inflight"] -= 1
                    rec["t1"] = time.monotonic()

        def _answer(self, part_no, n_try, fields):
            mode = STATE["mode"]
            if self.headers.get("Authorization") != "Bearer sk-test":
                return self._json(401, {"error": {"message": "Incorrect API key provided"}})
            if mode.get("refuse"):
                return self._json(400, {"error": {"message": "Unsupported language"}})
            if mode.get("fail_part") == part_no:
                return self._json(500, {"error": {"message": "server exploded"}})
            if mode.get("busy_part") == part_no and n_try == 1:
                return self._json(429, {"error": {"message": "slow down"}}, {"Retry-After": "0"})
            if fields.get("response_format") == ["diarized_json"]:
                known = fields.get("known_speaker_names[]", [])
                if not known:
                    segs = [("A", 0.0, 2.0, f"p{part_no} first"), ("B", 2.0, 5.5, f"p{part_no} second"),
                            ("A", 5.5, 9.0, f"p{part_no} third")]
                elif mode.get("recurring"):
                    # a guest who first speaks in part two and stays: named if
                    # the request carried their clip, a fresh letter if not
                    # (matched by name, as the API matches by voice — the
                    # clips arrive ranked by talk time, not in a fixed order)
                    guest = next((k for k in known if k not in ("SPEAKER_00", "SPEAKER_01")), "A")
                    segs = [("SPEAKER_00", 0.5, 3.0, f"p{part_no} known"), (guest, 3.0, 6.0, f"p{part_no} guest")]
                else:
                    # a known voice, then a brand-new one only this part hears
                    segs = [(known[0], 0.5, 3.0, f"p{part_no} known"), ("A", 3.0, 6.0, f"p{part_no} new")]
                events = [{"type": "transcript.text.segment", "id": f"s{k}", "speaker": w,
                           "start": s0, "end": s1, "text": " " + t} for k, (w, s0, s1, t) in enumerate(segs)]
                done = {"type": "transcript.text.done", "text": " ".join(t for *_, t in segs), "usage": USAGE_TOK}
            else:
                text = f"plain part {part_no}"
                if mode.get("nostream_part") == part_no:
                    return self._json(200, {"text": text, "usage": {"type": "duration", "seconds": 10}})
                events = [{"type": "transcript.text.delta", "delta": w + " "} for w in text.split()]
                done = {"type": "transcript.text.done", "text": text,
                        "usage": {"type": "duration", "seconds": 10}}
            stall = mode.get("stall_part") == part_no and n_try == 1
            self._sse(events + [done], pause=mode.get("pause", 0.02),
                      stall_after=1 if stall else None, stall_s=3.0)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    URL = f"http://127.0.0.1:{srv.server_port}/v1/audio/transcriptions"

    saved = (cloud.CHUNK_S, cloud.CHUNK_SLACK_S, cloud.STALL_S, cloud.RETRY_DELAY_S)
    cloud.CHUNK_S, cloud.CHUNK_SLACK_S, cloud.STALL_S, cloud.RETRY_DELAY_S = 10.0, 2.0, 1, 0.01

    def reset(**mode):
        with LOCK:
            REQ.clear()
            STATE.update(inflight=0, max_inflight=0, mode=mode, attempts={})

    def run(out, model=DIAR, lang="id", key="sk-test", progress=None, cancel=None, parallel=True):
        return cloud.transcribe_meeting(wav, out, model=model, key=key, lang=lang, url=URL,
                                        progress_cb=progress, cancel=cancel, parallel=parallel)

    def speakers_of(out):
        blocks, who = {}, None
        for line in (out / "transcript.merged.txt").read_text().splitlines():
            m = re.match(r"\[\d\d:\d\d:\d\d\] (\S+):$", line)
            if m:
                who = m.group(1)
                blocks.setdefault(who, [])
            elif line.strip() and who:
                blocks[who].append(line)
        return blocks

    try:
        # -- a diarized run: voices first, then the rest side by side ----------------
        reset(pause=0.05)
        progress = []
        out = TMP / "diar"
        res = run(out, progress=lambda s, f, m: progress.append((s, f, m)))
        parts = sorted({r["part"] for r in REQ})
        check("five parts, each sent once", parts == [1, 2, 3, 4, 5] and len(REQ) == 5, [r["part"] for r in REQ])
        first = next(r for r in REQ if r["part"] == 1)
        check("part one finishes before any other starts (voices first)",
              all(r["t0"] >= first["t1"] for r in REQ if r["part"] != 1))
        check("the rest run side by side", 2 <= STATE["max_inflight"] <= cloud.CONCURRENCY,
              STATE["max_inflight"])
        check("every request streams", all(r["fields"].get("stream") == ["true"] for r in REQ))
        f1 = first["fields"]
        check("diarize asks for diarized_json + auto chunking",
              f1["response_format"] == ["diarized_json"] and f1["chunking_strategy"] == ["auto"], f1)
        check("language is passed", f1.get("language") == ["id"])
        check("part one sends no references", "known_speaker_names[]" not in f1)
        later = [r for r in REQ if r["part"] != 1]
        check("every later part carries part one's voices, most talkative first",
              all(r["fields"].get("known_speaker_names[]") == ["SPEAKER_00", "SPEAKER_01"] for r in later),
              [r["fields"].get("known_speaker_names[]") for r in later])
        ref = later[0]["fields"]["known_speaker_references[]"][0]
        with wave.open(io.BytesIO(base64.b64decode(ref.split(",", 1)[1]))) as w:
            clip_s = w.getnframes() / w.getframerate()
        check("a reference clip is a 2–10 s wav", ref.startswith("data:audio/wav;base64,") and 2 <= clip_s <= 10,
              clip_s)

        blocks, who = {}, None
        for line in (out / "transcript.merged.txt").read_text().splitlines():
            m = re.match(r"\[(\d\d):(\d\d):(\d\d)\] (\S+):$", line)
            if m:
                who = m.group(4)
                blocks.setdefault(who, {"lines": [], "at": []})
                blocks[who]["at"].append(int(m.group(2)) * 60 + int(m.group(3)))
            elif line.strip() and who:
                blocks[who]["lines"].append(line)
        check("a known voice keeps its number in every part",
              all(f"p{k} known" in blocks["SPEAKER_00"]["lines"] for k in (2, 3, 4, 5)), blocks.get("SPEAKER_00"))
        check("a voice new to one part gets its own number there, in part order",
              [blocks.get(f"SPEAKER_{k:02d}", {}).get("lines") for k in (2, 3, 4, 5)]
              == [["p2 new"], ["p3 new"], ["p4 new"], ["p5 new"]], sorted(blocks))
        check("times are shifted onto the meeting timeline",
              blocks["SPEAKER_05"]["at"][0] >= 43, {k: v["at"] for k, v in blocks.items()})
        check("the cost is the sum of the token counts OpenAI reported",
              abs(res["cost_usd"] - 5 * PART_COST) < 1e-9 and res["cost_estimated"] is False, res["cost_usd"])
        head = (out / "output.txt").read_text().splitlines()
        check("output.txt names the engine, cost and parts",
              "# Engine: OpenAI cloud   Cost: $0.11   Parts: 5" in head, head[:5])
        check("the saved parts are cleared once the run completes", not (out / cloud.CACHE_DIR).exists())
        check("transcript.cloud.json keeps every part as returned",
              len(json.loads((out / "transcript.cloud.json").read_text())["chunks"]) == 5)
        msgs = [m for _, _, m in progress]
        check("progress goes 0 → 1 without going back",
              progress[-1][1] == 1.0 and all(progress[i][1] <= progress[i + 1][1] for i in range(len(progress) - 1)))
        check("progress says it is learning the voices, then how far along it is",
              any(m.startswith("learning the voices") for m in msgs)
              and any("parts at a time" in m and " of 0:52" in m for m in msgs), msgs[-4:])
        check("no temp files were left behind", not list(out.glob("notula-cloud-*")))

        # -- one part at a time: a late joiner keeps one number ------------------------
        reset(recurring=True, pause=0.02)
        progress = []
        out_s = TMP / "sequential"
        res = run(out_s, parallel=False, progress=lambda s, f, m: progress.append(m))
        order = [r["part"] for r in REQ]
        check("one at a time sends the parts in order", order == [1, 2, 3, 4, 5], order)
        check("...never two at once", STATE["max_inflight"] == 1
              and all(REQ[k + 1]["t0"] >= REQ[k]["t1"] for k in range(len(REQ) - 1)), STATE["max_inflight"])
        refs = [r["fields"].get("known_speaker_names[]") for r in REQ]
        check("each part carries the voices of every part before it",
              refs[1] == ["SPEAKER_00", "SPEAKER_01"]
              and refs[2] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"], refs)
        blocks = speakers_of(out_s)
        check("the guest who joined in part two keeps one number to the end",
              blocks.get("SPEAKER_02") == [f"p{k} guest" for k in (2, 3, 4, 5)], sorted(blocks))
        check("...so the meeting has three speakers, not six", res["speakers"] == 3 and res["parallel"] is False,
              res["speakers"])
        check("progress says it is going one part at a time", any("one part at a time" in m for m in progress),
              progress[-3:])

        reset(recurring=True, pause=0.02)
        res_p = run(TMP / "parallel-guest")
        check("in parallel the same guest gets a new number in each part",
              res_p["speakers"] == 6 and res_p["parallel"] is True, res_p["speakers"])

        # a part that keeps failing, one at a time: what came before is saved,
        # nothing after it is sent, and the retry picks up there
        reset(recurring=True, fail_part=4)
        out_f = TMP / "sequential-fail"
        try:
            run(out_f, parallel=False)
            check("a failing part fails a one-at-a-time run", False)
        except cloud.CloudError as e:
            check("the error says what is saved", "The 3 finished parts are saved" in str(e), str(e))
        check("nothing after the failed part is sent", 5 not in STATE["attempts"], STATE["attempts"])
        reset(recurring=True)
        run(out_f, parallel=False)
        check("the retry sends only the failed part and the ones after it",
              [r["part"] for r in REQ] == [4, 5], [r["part"] for r in REQ])
        check("...with the voices of the saved parts before it, most talkative first",
              REQ[0]["fields"].get("known_speaker_names[]") == ["SPEAKER_00", "SPEAKER_02", "SPEAKER_01"],
              REQ[0]["fields"].get("known_speaker_names[]"))

        # -- the plain model: no voices to learn, so everything runs at once -----------
        reset(nostream_part=3)
        out2 = TMP / "plain"
        res = run(out2, model=PLAIN, lang="auto")
        f = REQ[0]["fields"]
        check("plain model asks for json, streamed, with no chunking flag",
              f["response_format"] == ["json"] and f["stream"] == ["true"] and "chunking_strategy" not in f, f)
        check("'auto' language is not sent", "language" not in f and "languages[]" not in f)
        check("no part waits for another", STATE["max_inflight"] >= 2, STATE["max_inflight"])
        check("a part answered in one piece instead of a stream is still used",
              "plain part 3" in (out2 / "transcript.txt").read_text())
        check("transcript.txt has every part, in order",
              (out2 / "transcript.txt").read_text() == "\n".join(f"plain part {k}" for k in range(1, 6)) + "\n")
        check("plain result is not diarized and says so",
              res["diarized"] is False and "no speaker labels" in res["warning"])
        check("duration billing is exact", res["cost_estimated"] is False
              and abs(res["cost_usd"] - 5 * 10 / 60 * 0.0045) < 1e-9, res["cost_usd"])
        reset()
        res = run(TMP / "plain-lang", model=PLAIN, lang="id", parallel=False)
        check("gpt-transcribe takes its language as a list",
              REQ[0]["fields"].get("languages[]") == ["id"] and "language" not in REQ[0]["fields"])
        check("a plain model runs in parallel even when asked not to",
              STATE["max_inflight"] >= 2 and res["parallel"] is True, STATE["max_inflight"])

        # -- a stall and a rate limit are retried; nothing else notices -----------------
        reset(stall_part=3, busy_part=2)
        t = time.monotonic()
        res = run(TMP / "stall")
        tries = {p: STATE["attempts"][p] for p in STATE["attempts"]}
        check("a stream that goes quiet is abandoned and retried", tries.get(3) == 2, tries)
        check("a 429 is retried after Retry-After", tries.get(2) == 2, tries)
        check("...and the run still completes", res["output"].exists())
        check("a stall is noticed after STALL_S, not minutes", time.monotonic() - t < 15,
              f"{time.monotonic() - t:.1f}s")

        # -- a part that keeps failing: the rest are saved, and a retry sends only it ----
        reset(fail_part=4)
        out3 = TMP / "resume"
        try:
            run(out3)
            check("a part that always fails fails the run", False)
        except cloud.CloudError as e:
            check("the error says how many parts failed and that the rest are saved",
                  "1 of 5 parts failed" in str(e) and "saved" in str(e), str(e))
        check("the failing part was tried RETRIES times", STATE["attempts"].get(4) == cloud.RETRIES,
              STATE["attempts"])
        info = library.cloud_resume(str(out3))
        spans_ = cloud.plan_chunks(52.0, [10.5, 21.5, 32.5, 43.5])
        want = sum(e - s for k, (s, e) in enumerate(spans_) if k != 3)
        check("the four finished parts are saved", info and abs(info["done_s"] - want) < 0.05,
              (info, want))
        check("the saved time is what a retry would not pay for",
              abs(cloud.saved_seconds(out3, DIAR, "id") - want) < 0.05)
        reset()
        res = run(out3)
        check("the retry sends only the missing part", [r["part"] for r in REQ] == [4], [r["part"] for r in REQ])
        check("...with part one's voices, recovered from the saved part",
              REQ[0]["fields"].get("known_speaker_names[]") == ["SPEAKER_00", "SPEAKER_01"])
        check("...and the transcript is whole", "p5 new" in (out3 / "transcript.merged.txt").read_text()
              and "p1 first" in (out3 / "transcript.merged.txt").read_text())
        check("...and costs every part, saved ones included", abs(res["cost_usd"] - 5 * PART_COST) < 1e-9
              and res["reused"] == 4, (res["cost_usd"], res["reused"]))
        check("...and the saved parts are gone afterwards", not (out3 / cloud.CACHE_DIR).exists())

        # -- a bad key stops at once; a refused request stops the others -------------------
        reset()
        try:
            run(TMP / "badkey", key="sk-wrong")
            check("a bad key raises", False)
        except cloud.AuthError as e:
            check("a bad key raises AuthError with OpenAI's words", "Incorrect API key" in str(e), str(e))
        check("a bad key is sent once, not retried or fanned out", len(REQ) == 1, len(REQ))
        reset(refuse=True)
        try:
            run(TMP / "refused", model=PLAIN)
            check("a refused request raises", False)
        except cloud.CloudError as e:
            check("a refused request says so", "refused" in str(e) and "Unsupported language" in str(e), str(e))
        check("a refused request is not retried, and stops the other parts",
              len(REQ) <= cloud.CONCURRENCY and max(STATE["attempts"].values()) == 1, len(REQ))
        check("AuthError is a PipelineError", issubclass(cloud.AuthError, pipeline.PipelineError))

        # -- cancel (quitting the app) stops the run instead of paying for the rest -------
        reset(pause=0.3)
        stop = threading.Event()
        try:
            run(TMP / "cancel", cancel=stop,
                progress=lambda s, f, m: stop.set() if "parts at a time" in m else None)
            check("cancel raises", False)
        except cloud.CloudError as e:
            check("cancel stops the run", "cancel" in str(e), str(e))
        check("...before every part is sent", len(REQ) < 5, len(REQ))

        try:
            run(TMP / "nokey", key="")
            check("missing key raises", False)
        except cloud.AuthError:
            check("missing key raises before anything is encoded", True)
    finally:
        cloud.CHUNK_S, cloud.CHUNK_SLACK_S, cloud.STALL_S, cloud.RETRY_DELAY_S = saved
        srv.shutdown()

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
