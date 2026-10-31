#!/usr/bin/env python3
"""
watch.py - check a rendered Reel the way the feed will see it, before it goes up.

Everything else in this pack reads text. This reads the video file. It decodes
every frame and measures the things the other skills can only advise:

  SPEC          codec, frame rate, length, size, moov atom at the front:
                what Instagram's upload path accepts without re-encoding badly
  FIRST FRAME   is anything moving in the first half second?
  CUTS          how often the picture changes, and the longest shot
  STATIC        the longest stretch where nothing on screen moves
  SAFE ZONE     on-screen text (read with tesseract) that sits under the
                caption, the action rail or the audio strip
  HOOK CARD     is there text up at frame one?
  CAPTIONS      does the burned-in text match what the voice says at that
                moment? (needs --words, the word timings of the voice)
  SCRIPT        did the voice say the script? (needs --words and --script)
  LOOP          does the last frame hand back to the first, or the last line
                echo the hook?
  LOUDNESS      integrated LUFS and true peak
  OPENING       dead air before the first sound

These are measurements, not opinions, and each one prints its number. They
cannot tell you whether the reel is good. --gemini adds a second opinion from
a model that watches and listens to the whole file; where the two agree, act.
Where they disagree, look at the frame yourself.

Needs ffmpeg and ffprobe. tesseract is optional (no SAFE ZONE / HOOK CARD /
CAPTIONS without it). numpy is optional (faster frame maths).

Usage
  python3 watch.py reel.mp4
  python3 watch.py reel.mp4 --words vo/words.json --script script.txt
  python3 watch.py reel.mp4 --gemini
  python3 watch.py reel.mp4 --json
  python3 watch.py reel.mp4 --out reel.watch.md
"""

import argparse
import collections
import concurrent.futures
import difflib
import json
import os
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile

try:
    import numpy as np
except ImportError:          # pure Python works, just slower
    np = None


def _platform(path, default):
    """Numbers live in ../ig-core/platform.json. If this skill was copied on
    its own, the defaults below are the same numbers."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ig-core"))
        from igcore import number
        return number(path, default)
    except ImportError:
        return default


REF_W = _platform("reel.width", 1080)
REF_H = _platform("reel.height", 1920)
SAFE = _platform("reel.safe_zone", {"top": 230, "bottom": 480, "left": 0, "right": 230})
FIRST_WINDOW = _platform("reel.first_frame_window_s", 0.5)
MAX_BEAT = _platform("speech.max_beat_s", 4.0)
HOOK_WINDOW = _platform("speech.hook_window_s", 3.0)
LUFS = _platform("reel.loudness_lufs", -14)
PEAK = _platform("reel.true_peak_dbtp", -1.0)
PUB = {
    "min_s": _platform("publishing.reel_min_s", 3),
    "max_s": _platform("publishing.reel_max_s", 900),
    "max_mb": _platform("publishing.reel_max_mb", 300),
    "fps": _platform("publishing.fps", [23, 60]),
    "vcodecs": _platform("publishing.video_codecs", ["h264", "hevc"]),
    "acodecs": _platform("publishing.audio_codecs", ["aac"]),
    "audio_hz": _platform("publishing.audio_max_hz", 48000),
    "max_w": _platform("publishing.max_width", 1920),
    "containers": _platform("publishing.containers", ["mp4", "mov"]),
}

# Frame maths runs on tiny greyscale frames. A cut changes most pixels; a
# static frame changes almost none. Mean absolute difference, 0-255.
SMALL_W, SMALL_H = 72, 128
CUT_MIN = 18.0           # a hard cut moves the mean difference at least this far
STILL = 0.5              # below this, nothing visible moved between two frames
LOOP_MATCH = 14.0        # last frame within this of the first reads as a loop

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "for",
    "with", "that", "this", "it", "is", "are", "was", "were", "be", "been",
    "you", "your", "i", "my", "me", "we", "our", "they", "them", "he", "she",
    "so", "just", "not", "no", "do", "did", "does", "have", "has", "had",
    "will", "can", "at", "as", "by", "from", "out", "up", "off", "one", "all",
    "its", "it's", "what", "how", "why", "who", "then", "than", "into",
}
TOKEN_RE = re.compile(r"[a-z0-9']+")


def tokens(text, keep_stop=False):
    out = [t.strip("'") for t in TOKEN_RE.findall(text.lower().replace("’", "'"))]
    return [t for t in out if t and (keep_stop or (len(t) >= 3 and t not in STOPWORDS))]


def run(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def tc(seconds):
    m, s = divmod(max(0.0, seconds), 60)
    return f"{int(m)}:{s:04.1f}"


# ---------------------------------------------------------------- spec

def probe(path):
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
             "-show_format", path])
    if r.returncode:
        raise SystemExit(f"ffprobe could not read {path}: {r.stderr.strip()}")
    info = json.loads(r.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if not v:
        raise SystemExit(f"{path} has no video stream")

    def rate(s):
        num, _, den = (s or "0/1").partition("/")
        return float(num) / float(den or 1) if float(den or 1) else 0.0

    return {
        "width": int(v["width"]), "height": int(v["height"]),
        "fps": round(rate(v.get("avg_frame_rate")) or rate(v.get("r_frame_rate")), 3),
        "vcodec": v.get("codec_name"),
        "pix_fmt": v.get("pix_fmt"),
        "acodec": a.get("codec_name") if a else None,
        "audio_hz": int(a.get("sample_rate", 0)) if a else 0,
        "duration": float(info["format"].get("duration") or v.get("duration") or 0),
        "bytes": int(info["format"].get("size") or os.path.getsize(path)),
        "container": os.path.splitext(path)[1].lstrip(".").lower(),
    }


def moov_first(path):
    """Walk the top-level MP4 boxes. 'moov' before 'mdat' means the file can
    start playing (and processing) before it has fully arrived."""
    try:
        with open(path, "rb") as fh:
            while True:
                head = fh.read(8)
                if len(head) < 8:
                    return None
                size, kind = struct.unpack(">I4s", head)
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if size == 1:
                    size = struct.unpack(">Q", fh.read(8))[0]
                    fh.seek(size - 16, 1)
                elif size == 0:
                    return None
                else:
                    fh.seek(size - 8, 1)
    except OSError:
        return None


def check_spec(p, moov):
    problems = []
    if p["container"] not in PUB["containers"]:
        problems.append(f".{p['container']} is not {'/'.join(PUB['containers'])}")
    if p["vcodec"] not in PUB["vcodecs"]:
        problems.append(f"video is {p['vcodec']}, wants {'/'.join(PUB['vcodecs'])}")
    if p["pix_fmt"] and "420" not in p["pix_fmt"]:
        problems.append(f"pixel format {p['pix_fmt']}, wants 4:2:0")
    if p["acodec"] and p["acodec"] not in PUB["acodecs"]:
        problems.append(f"audio is {p['acodec']}, wants {'/'.join(PUB['acodecs'])}")
    if p["audio_hz"] > PUB["audio_hz"]:
        problems.append(f"audio at {p['audio_hz']} Hz, max {PUB['audio_hz']}")
    lo, hi = PUB["fps"]
    if not lo <= round(p["fps"]) <= hi:
        problems.append(f"{p['fps']:g} fps, wants {lo}-{hi}")
    if not PUB["min_s"] <= p["duration"] <= PUB["max_s"]:
        problems.append(f"{p['duration']:.1f}s, wants {PUB['min_s']}-{PUB['max_s']}s")
    if p["bytes"] > PUB["max_mb"] * 1024 * 1024:
        problems.append(f"{p['bytes'] / 1048576:.0f} MB, max {PUB['max_mb']} MB")
    if p["width"] > PUB["max_w"]:
        problems.append(f"{p['width']} px wide, max {PUB['max_w']}")
    if moov is False:
        problems.append("moov atom after the media (re-mux with -movflags +faststart)")
    ratio = p["width"] / p["height"]
    shape = "9:16" if abs(ratio - 9 / 16) < 0.01 else f"{p['width']}x{p['height']}, not 9:16"
    detail = (f"{p['vcodec']}/{p['acodec'] or 'no audio'} {p['container']}, {p['width']}x{p['height']} "
              f"({shape}), {p['fps']:g} fps, {p['duration']:.1f}s, {p['bytes'] / 1048576:.1f} MB"
              + (", moov first" if moov else ""))
    if problems:
        return "FAIL", detail + ". " + "; ".join(problems)
    if shape != "9:16":
        return "WARN", detail + ". Instagram will letterbox or crop anything that is not 9:16"
    return "PASS", detail


# ---------------------------------------------------------------- motion

def frame_diffs(path, fps=30.0):
    """Mean absolute difference between consecutive tiny greyscale frames,
    plus how close the last second gets to the first frame (the loop check;
    the last second, not the last frame, because many exports end on a fade)."""
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vf",
           f"scale={SMALL_W}:{SMALL_H},format=gray", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = SMALL_W * SMALL_H
    tail = collections.deque(maxlen=max(1, int(round(fps))))
    diffs, prev, first = [], None, None
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        if np is not None:
            cur = np.frombuffer(buf, dtype=np.uint8).astype(np.int16)
            if prev is not None:
                diffs.append(float(np.abs(cur - prev).mean()))
        else:
            cur = buf
            if prev is not None:
                diffs.append(sum(abs(x - y) for x, y in zip(cur, prev)) / size)
        if first is None:
            first = cur
        prev = cur
        tail.append(cur)
    proc.stdout.close()
    proc.wait()
    if first is None:
        raise SystemExit("ffmpeg decoded no frames")
    if np is not None:
        loop = min(float(np.abs(f - first).mean()) for f in tail)
    else:
        loop = min(sum(abs(x - y) for x, y in zip(f, first)) / size for f in tail)
    return diffs, loop


def find_cuts(diffs, fps):
    cuts, last = [], -1e9
    for i, d in enumerate(diffs):
        window = diffs[max(0, i - 15):i] or [0.0]
        if d >= CUT_MIN and d > 3 * statistics.median(window) + 4:
            t = (i + 1) / fps
            if t - last >= 0.25:            # a flash is one event, not two cuts
                cuts.append(round(t, 2))
                last = t
    return cuts


def longest_still(diffs, fps):
    best, run_start, best_start = 0, None, 0.0
    for i, d in enumerate(diffs + [STILL + 1]):
        if d < STILL:
            run_start = i if run_start is None else run_start
        elif run_start is not None:
            if i - run_start > best:
                best, best_start = i - run_start, run_start
            run_start = None
    return best / fps, best_start / fps


def check_motion(diffs, loop_diff, p):
    fps = p["fps"] or 30.0
    n_first = max(1, int(round(FIRST_WINDOW * fps)))
    opening = diffs[:n_first]
    motion = statistics.mean(opening) if opening else 0.0
    if motion >= 1.0:
        first = ("PASS", f"moving from frame one (motion {motion:.2f} in the first {FIRST_WINDOW:g}s)")
    elif motion >= STILL:
        first = ("WARN", f"barely moving in the first {FIRST_WINDOW:g}s (motion {motion:.2f}). "
                         "A thumb stops for movement, not for a still")
    else:
        first = ("FAIL", f"static for the first {FIRST_WINDOW:g}s (motion {motion:.2f}). "
                         "Open on movement: a push-in, a hand, an object already in motion")

    cuts = find_cuts(diffs, fps)
    bounds = [0.0] + cuts + [p["duration"]]
    shots = [b - a for a, b in zip(bounds, bounds[1:]) if b > a]
    longest = max(shots) if shots else p["duration"]
    at = bounds[shots.index(longest)] if shots else 0.0
    every = p["duration"] / (len(cuts) + 1)
    detail = (f"{len(cuts)} cuts, a new shot every {every:.1f}s on average; "
              f"longest shot {longest:.1f}s at {tc(at)}")
    cut_row = ("PASS" if every <= MAX_BEAT else "WARN",
               detail + ("" if every <= MAX_BEAT else
                         f". Average shot is past {MAX_BEAT:g}s: fine if the camera moves inside it"))

    still, still_at = longest_still(diffs, fps)
    if still > MAX_BEAT:
        static = ("WARN", f"{still:.1f}s with nothing moving at {tc(still_at)}. "
                          "That is where people leave")
    else:
        static = ("PASS", f"longest still stretch {still:.1f}s"
                  + (f" at {tc(still_at)}" if still >= 0.5 else ""))
    return first, cut_row, static, {"motion_first": round(motion, 3), "cuts": cuts,
                                    "longest_shot": round(longest, 2),
                                    "longest_still": round(still, 2),
                                    "loop_frame_diff": round(loop_diff, 2)}


# ---------------------------------------------------------------- text

def ocr_frames(path, p, fps_sample, workers=4):
    """Read the on-screen text at fps_sample frames a second. Returns
    [(t, [ {text, conf, box:(x0,y0,x1,y1) in 1080-wide coords} ])]."""
    if not shutil.which("tesseract"):
        return None
    tmp = tempfile.mkdtemp(prefix="igwatch-")
    step = 1.0 / fps_sample
    try:
        # select, not fps: the fps filter hands back the last frame of each
        # second, which would label a frame from 0:00.9 as frame one.
        r = run(["ffmpeg", "-v", "error", "-i", path, "-vf",
                 f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{step - 0.001:.3f})',"
                 f"scale={REF_W}:-2", "-fps_mode", "vfr", "-q:v", "3",
                 os.path.join(tmp, "f%05d.jpg")])
        if r.returncode:
            return None
        # The hook window gets one more look, just inside half a second.
        run(["ffmpeg", "-v", "error", "-i", path, "-ss", f"{FIRST_WINDOW * 0.9:.2f}",
             "-frames:v", "1", "-vf", f"scale={REF_W}:-2", "-q:v", "3",
             os.path.join(tmp, "h.jpg")])
        files = sorted(f for f in os.listdir(tmp) if f.endswith(".jpg"))

        def read(name):
            t = FIRST_WINDOW * 0.9 if name == "h.jpg" else (int(name[1:6]) - 1) * step
            out = run(["tesseract", os.path.join(tmp, name), "stdout", "--psm", "11", "tsv"],
                      timeout=60)
            found = []
            for line in out.stdout.splitlines()[1:]:
                cols = line.split("\t")
                if len(cols) < 12 or not cols[11].strip():
                    continue
                text, conf = cols[11].strip(), float(cols[10])
                letters = sum(ch.isalpha() for ch in text)
                if conf < 70 or letters < 2:
                    continue
                x, y, w, h = (int(c) for c in cols[6:10])
                found.append({"text": text, "conf": conf, "box": (x, y, x + w, y + h)})
            return t, found

        with concurrent.futures.ThreadPoolExecutor(workers) as pool:
            return sorted(pool.map(read, files))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def safe_box(p):
    h = REF_W * p["height"] / p["width"]
    return (SAFE["left"], SAFE["top"] * h / REF_H, REF_W - SAFE["right"],
            h - SAFE["bottom"] * h / REF_H)


def check_text(frames, p):
    if frames is None:
        skip = ("SKIP", "tesseract not installed (brew install tesseract)")
        return skip, skip, {}
    x0, y0, x1, y1 = safe_box(p)
    hits = []
    for t, words in frames:
        out = []
        for w in words:
            bx0, by0, bx1, by1 = w["box"]
            where = ("top band" if by0 < y0 else "bottom band" if by1 > y1 else
                     "right rail" if bx1 > x1 else "left edge" if bx0 < x0 else None)
            # One stray glyph in a texture is noise. A real word is 3+ letters.
            if where and len(w["text"]) >= 3:
                out.append((w["text"], where))
        if out:
            hits.append((t, out))
    if hits:
        sample = "; ".join(f"{tc(t)} {', '.join(sorted({wh for _, wh in o}))} "
                           f"(\"{' '.join(x for x, _ in o[:4])}\")" for t, o in hits[:4])
        safe = ("WARN", f"text outside the safe zone in {len(hits)} of {len(frames)} sampled "
                        f"frames: {sample}. Check each: real captions there get covered")
    else:
        safe = ("PASS", f"all text inside the safe zone across {len(frames)} sampled frames")

    at_zero = [w["text"] for t, ws in frames if t == 0 for w in ws if len(w["text"]) >= 3]
    early = [w["text"] for t, ws in frames if 0 < t <= FIRST_WINDOW for w in ws
             if len(w["text"]) >= 3]
    if at_zero:
        hook = ("PASS", "text up at frame one: \"" + " ".join(at_zero[:8]) + "\"")
    elif early:
        hook = ("WARN", f"no text at frame one; \"{' '.join(early[:8])}\" is up by "
                        f"{FIRST_WINDOW:g}s. Muted viewers decide on frame one: put the hook card there")
    else:
        hook = ("WARN", f"no readable text in the first {FIRST_WINDOW:g}s. Most people start "
                        "muted: the hook needs to be on screen at frame one")
    return safe, hook, {"outside_safe_zone": [{"t": t, "words": o} for t, o in hits]}


def load_words(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("words", [])
    return [{"text": w["text"], "start": float(w["start"]), "end": float(w["end"])} for w in data]


def check_captions(frames, spoken):
    """Text on screen at time t should share words with what is being said
    around t. Text that matches nothing the voice ever says is a label, not a
    caption, and does not count against sync."""
    if frames is None or spoken is None:
        why = "needs tesseract" if frames is None else "pass --words vo/words.json to check sync"
        return ("SKIP", why), {}
    timeline = [(t, w["start"]) for w in spoken for t in tokens(w["text"])]
    everything = {t for t, _ in timeline}
    checked, synced, offsets, late = 0, 0, [], []
    for t, words in frames:
        toks = set(tokens(" ".join(w["text"] for w in words)))
        caption_toks = toks & everything
        if not caption_toks:
            continue
        checked += 1
        near = {tok for tok, s in timeline if t - 1.5 <= s <= t + 1.0}
        if caption_toks & near:
            synced += 1
            continue
        best = min((s - t for tok, s in timeline if tok in caption_toks), key=abs)
        offsets.append(best)
        late.append((t, best, sorted(caption_toks)[:3]))
    if not checked:
        return ("SKIP", "no on-screen text matched any spoken word, so there are no captions "
                        "to sync (or tesseract could not read them)"), {}
    share = synced / checked
    detail = f"{synced}/{checked} sampled caption frames match the voice at that moment"
    if offsets:
        drift = statistics.median(offsets)
        detail += (f"; off-sync frames are a median {abs(drift):.1f}s "
                   f"{'ahead of' if drift > 0 else 'behind'} the voice, first at {tc(late[0][0])}")
    return ("PASS" if share >= 0.7 else "WARN", detail), {
        "caption_sync": round(share, 3), "offsync": late[:10]}


def check_script(spoken, script_path):
    if not script_path:
        return None, {}
    if spoken is None:
        return ("SKIP", "pass --words too, so there is something to compare the script to"), {}
    with open(script_path, encoding="utf-8") as fh:
        raw = "\n".join(line.partition("||")[0] for line in fh)
    want = tokens(raw, keep_stop=True)
    got = tokens(" ".join(w["text"] for w in spoken), keep_stop=True)
    sm = difflib.SequenceMatcher(a=want, b=got, autojunk=False)
    missing, extra = [], []
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op in ("delete", "replace"):
            missing.append(" ".join(want[a0:a1]))
        if op in ("insert", "replace"):
            extra.append(" ".join(got[b0:b1]))
    ratio = sm.ratio()
    detail = f"voice matches the script {ratio:.0%}"
    if missing:
        detail += "; not said: " + "; ".join(f"\"{m}\"" for m in missing[:3])
    if extra:
        detail += "; said but not in the script: " + "; ".join(f"\"{e}\"" for e in extra[:3])
    return ("PASS" if ratio >= 0.9 else "WARN", detail), {"script_match": round(ratio, 3)}


def check_loop(loop_diff, spoken):
    visual = loop_diff <= LOOP_MATCH
    echo = []
    if spoken:
        hook = {t for w in spoken if w["start"] < HOOK_WINDOW for t in tokens(w["text"])}
        end = spoken[-1]["end"]
        tail = {t for w in spoken if w["start"] >= end - 3.0 for t in tokens(w["text"])}
        echo = sorted(hook & tail)
    parts = [f"last second {'returns to' if visual else 'never returns to'} the opening frame "
             f"(difference {loop_diff:.1f})"]
    if spoken is not None:
        parts.append(f"last line repeats \"{', '.join(echo[:3])}\" from the hook" if echo
                     else "last line shares no word with the hook")
    if visual or echo:
        return "PASS", "; ".join(parts) + ". Replays land clean"
    return "WARN", "; ".join(parts) + ". Echo one hook word in the last line, or end on the opening frame"


# ---------------------------------------------------------------- audio

def check_audio(path, p, spoken):
    if not p["acodec"]:
        row = ("WARN", "no audio track")
        return row, row, {}
    r = run(["ffmpeg", "-nostats", "-i", path, "-filter_complex", "ebur128=peak=true",
             "-f", "null", "-"])
    lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    peak = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", r.stderr)
    measured = {}
    if lufs:
        i, tp = float(lufs[-1]), float(peak[-1]) if peak else None
        measured = {"lufs": i, "true_peak": tp}
        ok = abs(i - LUFS) <= 2 and (tp is None or tp <= PEAK + 0.3)
        loud = ("PASS" if ok else "WARN",
                f"{i:.1f} LUFS, {tp:.1f} dBTP (aim {LUFS} / {PEAK:g})" if tp is not None
                else f"{i:.1f} LUFS (aim {LUFS})")
        if not ok and i < LUFS - 2:
            loud = (loud[0], loud[1] + ". Quieter than the reels around it: they will sound "
                                       "bigger than you")
        elif tp is not None and tp > PEAK + 0.3:
            loud = (loud[0], loud[1] + ". Peaks this hot clip after Instagram re-encodes")
    else:
        loud = ("SKIP", "could not measure loudness")

    if spoken:
        onset = spoken[0]["start"]
        source = "voice"
    else:
        s = run(["ffmpeg", "-nostats", "-i", path, "-t", "5", "-af",
                 "silencedetect=noise=-40dB:d=0.2", "-f", "null", "-"]).stderr
        starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", s)]
        ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", s)]
        onset = ends[0] if starts and starts[0] <= 0.05 and ends else 0.0
        source = "sound"
    measured["onset"] = round(onset, 2)
    if onset > 0.5:
        opening = ("WARN", f"{source} starts at {onset:.2f}s. Dead air at the top reads as a "
                           "video that has not started yet")
    else:
        opening = ("PASS", f"{source} starts at {onset:.2f}s")
    return loud, opening, measured


# ---------------------------------------------------------------- gemini

GEMINI_PROMPT = """You are checking an Instagram Reel before it is posted. Watch and listen to the whole file.
Answer ONLY with one JSON object, no prose, in exactly this shape:
{"first_frame": {"moving": true, "what": ""},
 "hook": {"spoken": "", "on_screen": "", "lands_by_s": 0.0},
 "text_outside_safe_zone": [{"t": 0.0, "text": "", "where": "top|bottom|right rail"}],
 "captions_match_voice": {"ok": true, "mismatches": [{"t": 0.0, "caption": "", "spoken": ""}]},
 "static_stretches": [{"start": 0.0, "end": 0.0}],
 "loop": {"lands": true, "why": ""},
 "audio": {"voice_clear": true, "music_too_loud": false, "notes": ""},
 "biggest_problem": "",
 "fixes": [""]}
The safe zone on a 9:16 frame excludes the top 12%, the bottom 25% and the right 21%: the
Instagram interface covers those. Timestamps in seconds. Be literal: report what is in the
frames and the audio, not what the video intends."""


def gemini_command():
    cmd = os.environ.get("IG_GEMINI_CMD")
    if cmd:
        return cmd
    for c in (shutil.which("gemini-video"), os.path.expanduser("~/bin/gemini-video")):
        if c and os.path.exists(c):
            return c
    return None


def parse_json_blob(text):
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError:
                    break
        start = text.find("{", start + 1)
    return None


def gemini_pass(path, timeout=420):
    """Ask Gemini to watch it. Uses a local CLI wrapper if there is one
    (IG_GEMINI_CMD, or `gemini-video` that takes <video> <question>), else the
    google-genai SDK with GEMINI_API_KEY. Returns (dict or None, note)."""
    cmd = gemini_command()
    if cmd:
        try:
            r = run([cmd, os.path.abspath(path), GEMINI_PROMPT], timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, f"{cmd} timed out after {timeout}s"
        data = parse_json_blob(r.stdout)
        return data, (f"via {os.path.basename(cmd)}" if data else
                      f"{os.path.basename(cmd)} returned no JSON: {r.stdout.strip()[:200]}")
    if os.environ.get("GEMINI_API_KEY"):
        try:
            from google import genai
        except ImportError:
            return None, "GEMINI_API_KEY is set but google-genai is not installed (pip install google-genai)"
        import time
        client = genai.Client()
        f = client.files.upload(file=path)
        while getattr(f.state, "name", str(f.state)) == "PROCESSING":
            time.sleep(3)
            f = client.files.get(name=f.name)
        model = os.environ.get("IG_GEMINI_MODEL", "gemini-2.5-flash")
        resp = client.models.generate_content(model=model, contents=[f, GEMINI_PROMPT])
        data = parse_json_blob(resp.text or "")
        return data, f"via the Gemini API ({model})" if data else "Gemini API returned no JSON"
    return None, ("no Gemini route: set IG_GEMINI_CMD to a command that takes <video> <prompt>, "
                  "put gemini-video on PATH, or set GEMINI_API_KEY")


def agreement(rows, g):
    """Where the model and the measurements agree, say so. That is the signal."""
    notes = []
    if not g:
        return notes
    ff = rows.get("FIRST FRAME", ("", ""))[0]
    moving = (g.get("first_frame") or {}).get("moving")
    if moving is not None and ff in ("PASS", "FAIL"):
        same = (ff == "PASS") == bool(moving)
        notes.append(("agree" if same else "DISAGREE") + f" on the first frame: Gemini says "
                     f"{'moving' if moving else 'static'} ({(g.get('first_frame') or {}).get('what', '')})")
    gz = g.get("text_outside_safe_zone") or []
    sz = rows.get("SAFE ZONE", ("", ""))[0]
    if sz in ("PASS", "WARN"):
        same = (sz == "WARN") == bool(gz)
        notes.append(("agree" if same else "DISAGREE") + f" on the safe zone: Gemini lists {len(gz)} "
                     "text placement(s) outside it")
    loop = (g.get("loop") or {}).get("lands")
    lp = rows.get("LOOP", ("", ""))[0]
    if loop is not None and lp:
        same = (lp == "PASS") == bool(loop)
        notes.append(("agree" if same else "DISAGREE") + f" on the loop: {(g.get('loop') or {}).get('why', '')}")
    return notes


# ---------------------------------------------------------------- report

ORDER = ["SPEC", "FIRST FRAME", "CUTS", "STATIC", "SAFE ZONE", "HOOK CARD", "CAPTIONS",
         "SCRIPT", "LOOP", "LOUDNESS", "OPENING"]


def analyse(path, words=None, script=None, ocr_fps=1.0, use_gemini=False):
    p = probe(path)
    rows, data = {}, {"file": path, "probe": p}
    rows["SPEC"] = check_spec(p, moov_first(path))
    diffs, loop_diff = frame_diffs(path, p["fps"] or 30.0)
    first, cuts, static, motion = check_motion(diffs, loop_diff, p)
    rows["FIRST FRAME"], rows["CUTS"], rows["STATIC"] = first, cuts, static
    data.update(motion)

    spoken = load_words(words) if words else None
    frames = ocr_frames(path, p, ocr_fps)
    rows["SAFE ZONE"], rows["HOOK CARD"], extra = check_text(frames, p)
    data.update(extra)
    rows["CAPTIONS"], extra = check_captions(frames, spoken)
    data.update(extra)
    row, extra = check_script(spoken, script)
    if row:
        rows["SCRIPT"] = row
    data.update(extra)
    rows["LOOP"] = check_loop(loop_diff, spoken)
    rows["LOUDNESS"], rows["OPENING"], extra = check_audio(path, p, spoken)
    data.update(extra)

    if use_gemini:
        g, note = gemini_pass(path)
        data["gemini"] = g
        data["gemini_note"] = note
        data["agreement"] = agreement(rows, g)
    data["rows"] = {k: {"status": v[0], "detail": v[1]} for k, v in rows.items()}
    data["fails"] = [k for k, v in rows.items() if v[0] == "FAIL"]
    return data


def render(a, out=sys.stdout):
    p = a["probe"]
    head = (f"WATCH  ·  {os.path.basename(a['file'])}  ·  {p['width']}x{p['height']}  ·  "
            f"{p['fps']:g} fps  ·  {p['duration']:.1f}s")
    print("\n" + head, file=out)
    print("=" * max(len(head), 72), file=out)
    for name in ORDER:
        if name in a["rows"]:
            r = a["rows"][name]
            print(f"  {r['status']:<5} {name:<12} {r['detail']}", file=out)
    if "gemini_note" in a:
        print("-" * max(len(head), 72), file=out)
        g = a.get("gemini")
        print(f"  GEMINI {a['gemini_note']}", file=out)
        for n in a.get("agreement", []):
            print(f"    {n}", file=out)
        if g:
            if g.get("biggest_problem"):
                print(f"    biggest problem: {g['biggest_problem']}", file=out)
            for fix in (g.get("fixes") or [])[:5]:
                if fix:
                    print(f"    - {fix}", file=out)
    print("", file=out)


def to_markdown(a):
    lines = [f"# Watch: {os.path.basename(a['file'])}", "",
             "| | check | result |", "|---|---|---|"]
    for name in ORDER:
        if name in a["rows"]:
            r = a["rows"][name]
            lines.append(f"| {r['status']} | {name} | {r['detail'].replace('|', '/')} |")
    if a.get("gemini"):
        lines += ["", "## Gemini", "", "```json", json.dumps(a["gemini"], indent=2), "```"]
        lines += [f"- {n}" for n in a.get("agreement", [])]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Check a rendered Reel before it goes up.")
    ap.add_argument("video")
    ap.add_argument("--words", help="word timings of the voice: [{text,start,end}] "
                                    "(hyperframes transcribe / beats.py --words shape)")
    ap.add_argument("--script", help="the script, to check the voice said it")
    ap.add_argument("--ocr-fps", type=float, default=1.0, help="frames a second to read text from")
    ap.add_argument("--gemini", action="store_true", help="add a Gemini watch-and-listen pass")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", help="also write a markdown report here")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found. brew install ffmpeg")
    a = analyse(args.video, args.words, args.script, args.ocr_fps, args.gemini)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(to_markdown(a))
    if args.json:
        print(json.dumps(a, indent=2, ensure_ascii=False, default=str))
    else:
        render(a)
    sys.exit(1 if a["fails"] else 0)


if __name__ == "__main__":
    main()
