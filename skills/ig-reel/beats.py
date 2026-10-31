#!/usr/bin/env python3
"""
beats.py - turn a Reel script into a timed beat sheet before you record it.

Estimates how long each line takes to say, stacks them into timecodes, and
flags the four things that kill a Reel in the edit: a hook that runs past the
three-second mark, a beat long enough for the viewer to leave, a run of lines
with nothing concrete in them, and a total length that does not match what you
said you were making.

The timings are an estimate from word count at a words-per-minute rate. They
are close enough to plan an edit and not a substitute for recording it. Set
your own rate with --wpm once you have timed yourself reading a script out
loud: most people land between 150 and 200, and the default here is 165.

Usage
  python3 beats.py script.txt
  python3 beats.py script.txt --target 30
  python3 beats.py script.txt --wpm 185 --target 45
  pbpaste | python3 beats.py -
  python3 beats.py script.txt --json
  python3 beats.py script.txt --timeline beats.json   # beats, cards, word timings
  python3 beats.py script.txt --words vo/words.json   # scratch timings for a renderer

On-screen cards go on the same line after " || ". They are not spoken, so
they do not change the timing:
  Proposals used to take me five hours. || 5 HOURS -> 20 MIN
"""

import argparse
import json
import os
import re
import sys


def _platform(path, default):
    """Numbers live in ../ig-core/platform.json. If this skill was copied on
    its own, the defaults below are the same numbers."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ig-core"))
        from igcore import number
        return number(path, default)
    except ImportError:
        return default


WORD_RE = re.compile(r"[A-Za-z0-9$%'’-]+")
SENT_RE = re.compile(r"[^.!?]+[.!?]*")
# Spoken scripts say their numbers out loud: "five hours" is as checkable as
# "5 hours". Same list hookscore.py uses, for the same reason.
CONCRETE_RE = re.compile(
    r"\$\s?\d|\b\d[\d,.]*\b|(?<!^)\b[A-Z][a-z]{2,}\b"
    r"|(?i:\b(?:zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen"
    r"|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million"
    r"|billion|dozen|half|twice|triple|dollars?|bucks|grand|percent)\b)", re.MULTILINE)
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "for",
    "with", "that", "this", "it", "is", "are", "was", "were", "be", "been",
    "you", "your", "i", "my", "me", "we", "our", "they", "them", "he", "she",
    "so", "just", "not", "no", "do", "did", "does", "have", "has", "had",
    "will", "can", "at", "as", "by", "from", "out", "up", "off", "one", "all",
}

HOOK_WINDOW = _platform("speech.hook_window_s", 3.0)   # seconds. Past this, the thumb has already decided.
MAX_BEAT = _platform("speech.max_beat_s", 4.0)        # seconds on one idea with no change on screen.
ABSTRACT_RUN = _platform("speech.abstract_run", 3)    # beats in a row with nothing checkable in them.
WPM = _platform("speech.wpm", 165)


def words(text):
    # "$18,000" is one word when it is spoken, so it is one word here too.
    return WORD_RE.findall(re.sub(r"(?<=\d),(?=\d)", "", text))


def pretty(token):
    """Put the thousands separator back for display."""
    return re.sub(r"(\d)(?=(\d{3})+$)", r"\1,", token)


def tc(seconds):
    m, s = divmod(seconds, 60)
    return f"{int(m)}:{s:04.1f}"


def split_beats(raw, wps):
    """One line is one beat, unless a line is too long to be one.

    A line may carry its on-screen card after " || ":
        Proposals used to take me five hours. || 5 HOURS -> 20 MIN
    The card is not spoken, so it is not timed; it rides on the beat.
    Returns [(spoken, card)].
    """
    beats = []
    for line in [l.strip() for l in raw.splitlines()]:
        if not line:
            continue
        line, _, card = line.partition("||")
        line, card = line.strip(), card.strip()
        if not line:
            continue
        if len(words(line)) / wps <= MAX_BEAT * 1.5:
            beats.append((line, card))
            continue
        # Long paragraph: break it at sentence ends so the timings mean
        # something, and let the report say it was split.
        parts = [p.strip() for p in SENT_RE.findall(line) if p.strip()]
        buf = ""
        for part in parts:
            candidate = (buf + " " + part).strip()
            if buf and len(words(candidate)) / wps > MAX_BEAT:
                beats.append((buf, card))
                buf, card = part, ""
            else:
                buf = candidate
        if buf:
            beats.append((buf, card))
    return beats


def analyse(raw, wpm=WPM, target=None):
    wps = wpm / 60.0
    beats = split_beats(raw, wps)
    if not beats:
        return None

    rows, clock = [], 0.0
    for i, (text, card) in enumerate(beats):
        n = len(words(text))
        dur = n / wps
        rows.append({
            "n": i + 1,
            "start": round(clock, 2),
            "dur": round(dur, 2),
            "words": n,
            "text": text,
            "card": card,
            "concrete": len(CONCRETE_RE.findall(text)),
            "label": "",
            "flags": [],
        })
        clock += dur
    total = clock

    # Label the structural positions a Reel is actually built around.
    for r in rows:
        if r["n"] == 1 or r["start"] + r["dur"] <= HOOK_WINDOW:
            r["label"] = "HOOK"
    rows[-1]["label"] = "CTA" if rows[-1]["label"] != "HOOK" else "HOOK/CTA"
    half = total / 2
    for r in rows:
        if not r["label"] and r["start"] <= half < r["start"] + r["dur"]:
            r["label"] = "MID"

    notes = []
    if rows[0]["dur"] > HOOK_WINDOW:
        rows[0]["flags"].append(f"hook runs {rows[0]['dur']:.1f}s, past the {HOOK_WINDOW:.0f}s mark")
        notes.append(f"Beat 1 takes {rows[0]['dur']:.1f}s to say. Cut it to "
                     f"{int(HOOK_WINDOW * wps)} words or fewer, or the hook lands after "
                     "the decision has been made.")
    if rows[0]["concrete"] == 0:
        notes.append("Beat 1 has no number and no name in it. Hooks without something "
                     "checkable are the ones that get scrolled.")

    for r in rows:
        if r["dur"] > MAX_BEAT:
            r["flags"].append(f"{r['dur']:.1f}s on one beat")
    long_beats = [r["n"] for r in rows if r["dur"] > MAX_BEAT]
    if long_beats:
        notes.append(f"Beat(s) {', '.join(map(str, long_beats))} run past {MAX_BEAT:.0f}s. "
                     "Either split the line or change what is on screen inside it. "
                     "A static frame is where people leave.")

    run, start = 0, None
    for r in rows:
        if r["concrete"] == 0:
            run += 1
            start = start if start is not None else r["n"]
            if run == ABSTRACT_RUN:
                notes.append(f"Beats {start}-{r['n']} have nothing concrete in them. "
                             "Put a number, a name or a price in one of them.")
        else:
            run, start = 0, None

    # Does the last line hand you back to the first one?
    first = {w.lower() for w in words(rows[0]["text"]) if w.lower() not in STOPWORDS}
    last = {w.lower() for w in words(rows[-1]["text"]) if w.lower() not in STOPWORDS}
    loop = sorted(pretty(w) for w in first & last)
    if loop:
        notes.append(f"Loops: the last beat repeats \"{', '.join(loop[:3])}\" from the hook. "
                     "Second watches are free reach.")
    else:
        notes.append("No loop. The last beat shares no word with the hook, so the video "
                     "ends flat. Echoing one word from beat 1 is the cheapest replay you get.")

    if target:
        delta = total - target
        if abs(delta) <= target * 0.1:
            notes.append(f"Length is on target ({total:.1f}s against {target}s).")
        elif delta > 0:
            notes.append(f"{delta:.1f}s over target. Cut about {int(delta * wps)} words.")
        else:
            notes.append(f"{-delta:.1f}s under target. Either add {int(-delta * wps)} words "
                         "or shoot it short. Short is usually right.")

    return {
        "wpm": wpm, "target": target,
        "total_seconds": round(total, 2),
        "total_words": sum(r["words"] for r in rows),
        "beats": rows,
        "notes": notes,
    }


def timeline(a):
    """The beat sheet as data an edit can be built from.

    Word timings use the same shape as `hyperframes transcribe` output
    ({text, start, end, id}), so a renderer that reads real word timings can
    lay out captions and cuts from the script before the voice exists, then
    swap in the real take without changing anything else. Inside a beat, time
    is shared out by word length: "proposals" takes longer to say than "to".
    """
    out_words, cards = [], []
    rows = a["beats"]
    for i, r in enumerate(rows):
        # Each beat ends where the next starts, so rounding never leaves a gap.
        end = rows[i + 1]["start"] if i + 1 < len(rows) else a["total_seconds"]
        toks = r["text"].split()
        spoken = [t for t in toks if words(t)]
        weights = [max(2, len("".join(words(t)))) for t in spoken]
        total = sum(weights) or 1
        clock, wl = r["start"], []
        for t, w in zip(spoken, weights):
            dur = (end - r["start"]) * w / total
            wl.append({"text": t, "start": round(clock, 3), "end": round(clock + dur, 3),
                       "id": f"w{len(out_words) + len(wl)}"})
            clock += dur
        out_words.extend(wl)
        r["end"] = round(end, 2)
        r["word_ids"] = [w["id"] for w in wl]
        if r.get("card"):
            cards.append({"text": r["card"], "start": r["start"], "end": r["end"], "beat": r["n"]})
    return {
        "format": "ig-beats/1",
        "estimated": True,
        "wpm": a["wpm"],
        "duration": a["total_seconds"],
        "beats": [{k: r[k] for k in ("n", "label", "start", "end", "text", "card", "word_ids", "flags")}
                  for r in a["beats"]],
        "cards": cards,
        "words": out_words,
    }


def render(a, out=sys.stdout):
    head = (f"BEAT SHEET  ·  {a['total_words']} words  ·  ~{a['total_seconds']:.1f}s "
            f"at {a['wpm']} wpm" + (f"  ·  target {a['target']}s" if a["target"] else ""))
    print("\n" + head, file=out)
    print("=" * max(len(head), 72), file=out)
    for r in a["beats"]:
        label = f"{r['label']:<8}" if r["label"] else " " * 8
        print(f"  {tc(r['start'])}  {r['dur']:4.1f}s  {label}{r['text']}", file=out)
        if r.get("card"):
            print(f"  {'':>6}  {'':>5}  {'':<8}[card] {r['card']}", file=out)
        for f in r["flags"]:
            print(f"  {'':>6}  {'':>5}  {'':<8}^ {f}", file=out)
    print("-" * max(len(head), 72), file=out)
    for n in a["notes"]:
        print(f"  - {n}", file=out)
    print("", file=out)


def main():
    ap = argparse.ArgumentParser(description="Time a Reel script into a beat sheet.")
    ap.add_argument("input", nargs="?", default="-", help="script file, or - for stdin")
    ap.add_argument("--wpm", type=float, default=WPM, help=f"speaking rate (default {WPM})")
    ap.add_argument("--target", type=float, help="target length in seconds")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeline", metavar="FILE",
                    help="write beats, cards and estimated word timings as JSON for an edit")
    ap.add_argument("--words", metavar="FILE",
                    help="write only the word timings, in hyperframes-transcribe shape")
    args = ap.parse_args()

    raw = sys.stdin.read() if args.input == "-" else open(args.input, encoding="utf-8").read()
    a = analyse(raw, wpm=args.wpm, target=args.target)
    if not a:
        print("empty script", file=sys.stderr)
        sys.exit(2)
    if args.timeline or args.words:
        t = timeline(a)
        for path, value in ((args.timeline, t), (args.words, t["words"])):
            if path:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(value, fh, indent=2, ensure_ascii=False)
                print(f"wrote {path}", file=sys.stderr)
    if args.json:
        print(json.dumps(a, indent=2, ensure_ascii=False))
    else:
        render(a)
    sys.exit(0)


if __name__ == "__main__":
    main()
