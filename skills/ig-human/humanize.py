#!/usr/bin/env python3
"""
humanize.py - strip the machine fingerprint out of a draft.

Three passes, in this order:

  1. INVISIBLE   delete or normalise the characters a human keyboard never
                 produces: zero-width joiners, word joiners, soft hyphens,
                 BOMs, Unicode tag characters, non-breaking and narrow spaces.
                 These survive copy-paste and are the most mechanical tell in
                 any generated text.
  2. TYPOGRAPHIC em dash -> comma, en dash -> hyphen, curly quotes -> straight,
                 ellipsis -> three dots, bullet -> hyphen.
  3. LEXICAL     replace the slop lexicon in slop.json with plain words,
                 preserving capitalisation and leaving URLs untouched.

Structural tells (rule-of-three, "not just X, it's Y", hashtag walls) are
REPORTED, never auto-rewritten - rewriting a sentence's shape needs judgement,
so that is the model's job, not a regex's.

Usage
  python3 humanize.py draft.txt
  python3 humanize.py draft.txt --report
  pbpaste | python3 humanize.py - --report
  python3 humanize.py draft.txt --json
  python3 humanize.py draft.txt -o clean.txt
  python3 humanize.py draft.txt --force      # replace everything, no review list

Some replacements would make a sentence worse, so they are not made. They are
listed under LEFT AS WRITTEN for a person (or the model) to rewrite:
  - an em dash that joins two full sentences (a comma there is a splice)
  - a lexicon word with a literal meaning: a dog harness, unlocking a phone,
    a real hack (entries marked "mode": "flag" in slop.json)
  - a word inside quotation marks, or capitalised mid-sentence (a name)
  - a deletion that would leave a two-word fragment
Two breakages a swap can cause are put right automatically: "a"/"an" before
the new word, and a word doubled by the swap.
"""

import argparse
import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
LEX = os.path.join(HERE, "slop.json")

# Links, emails, #hashtags and @handles are names, not prose. Nothing rewrites
# inside them: "#unlock" must not become "#get".
URL_RE = re.compile(r"https?://\S+|www\.\S+|\S+@\S+\.\S+|(?<![\w#@])[#@][\w.]*\w")
SENT_RE = re.compile(r"[^.!?\n]+[.!?]*")
QUOTED_RE = re.compile(r"\"[^\"\n]{1,200}\"")

# A clause that can stand on its own usually opens on one of these. An em dash
# in front of one joins two sentences, and a comma there is a splice.
CLAUSE_OPENERS = {
    "i", "we", "you", "they", "it", "it's", "that", "this", "these", "those",
    "he", "she", "there", "here", "nobody", "everyone", "everybody", "people",
    "the", "my", "our", "your", "their", "most", "some", "no", "and", "but", "so",
}


def load_lexicon(path=LEX):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _cp(spec):
    """'U+200B' -> '\\u200b';  'U+E0000-U+E007F' -> (start, end)."""
    if "-" in spec:
        a, b = spec.split("-")
        return (int(a[2:], 16), int(b[2:], 16))
    return int(spec[2:], 16)


def protect_urls(text):
    """Swap URLs for placeholders so no pass rewrites inside a link."""
    found = []

    def stash(m):
        found.append(m.group(0))
        return f"\x00URL{len(found) - 1}\x00"

    return URL_RE.sub(stash, text), found


def restore_urls(text, found):
    for i, url in enumerate(found):
        text = text.replace(f"\x00URL{i}\x00", url)
    return text


def pass_invisible(text, lex):
    """Delete or space-normalise invisible characters. Returns (text, hits)."""
    hits = []
    for entry in lex["invisible"]:
        cp = _cp(entry["cp"])
        if isinstance(cp, tuple):
            pattern = "[" + re.escape(chr(cp[0])) + "-" + re.escape(chr(cp[1])) + "]"
        else:
            pattern = re.escape(chr(cp))
        n = len(re.findall(pattern, text))
        if n:
            hits.append({"name": entry["cp"] + " " + entry["name"], "count": n,
                         "action": entry["action"]})
            text = re.sub(pattern, "" if entry["action"] == "delete" else " ", text)
    # Any remaining Cf (format) character is invisible by definition.
    stray = [c for c in text if unicodedata.category(c) == "Cf"]
    if stray:
        hits.append({"name": "other invisible format chars", "count": len(stray),
                     "action": "delete"})
        text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return text, hits


def _excerpt(text, start, end, pad=40):
    a, b = max(0, start - pad), min(len(text), end + pad)
    return ("..." if a else "") + text[a:b].replace("\n", " ").strip() + ("..." if b < len(text) else "")


def fix_dashes(text, force=False):
    """Em dashes, and en dashes used as em dashes, one sentence at a time.

    Safe, so done:   5—10 -> 5-10;  a — paired aside — b -> a, paired aside, b;
                     a dash before a short tail -> a comma.
    Not safe, so left in place and reported: a dash that joins two clauses
    that could each stand alone. A comma there is a comma splice, which reads
    worse than the dash. The writer picks: two sentences, a colon, or a cut.
    --force restores the old behaviour (every dash becomes a comma).
    """
    review = []
    text = re.sub(r"(\d)\s*[—–]\s*(?=\d)", r"\1-", text)
    dash = r"(?:\s*—\s*|\s+–\s+)"
    if force:
        return re.sub(dash, ", ", text), review

    def per_sentence(m):
        s = m.group(0)
        parts = re.split(dash, s)
        if len(parts) == 1:
            return s
        if len(parts) == 3:                         # a paired aside
            return ", ".join(p.strip() if i else p.rstrip() for i, p in enumerate(parts))
        if len(parts) == 2:
            tail = parts[1].strip()
            first = re.sub(r"^[^\w']+", "", tail).split(" ")[0].lower() if tail else ""
            if len(tail.split()) >= 5 and first in CLAUSE_OPENERS:
                review.append({
                    "what": "em dash joining two sentences",
                    "where": _excerpt(s, len(parts[0]), len(parts[0]) + 3),
                    "why": "a comma here would be a comma splice",
                    "options": "two sentences, a colon, or cut one half",
                })
                return s
            return parts[0].rstrip() + ", " + tail
        review.append({"what": f"{len(parts) - 1} dashes in one sentence",
                       "where": _excerpt(s, 0, len(s), 0)[:120],
                       "why": "too many to repunctuate safely",
                       "options": "break it into shorter sentences"})
        return s

    return re.sub(r"[^.!?\n]+[.!?]*", per_sentence, text), review


def pass_typographic(text, lex, force=False):
    hits, review, dashes_done = [], [], False
    for entry in lex["typographic"]:
        ch = entry["from"]
        n = text.count(ch)
        if not n:
            continue
        hits.append({"name": f"{ch} {entry['name']}", "count": n, "to": entry["to"].strip() or "(space)"})
        if ch == "—":
            text, review = fix_dashes(text, force)
            dashes_done = True
        elif ch == "–":
            if not dashes_done:
                text, more = fix_dashes(text, force)
                review += more
            text = re.sub(r"(?<=\S)–(?=\S)", "-", text)     # word–word, a real en dash
        else:
            text = text.replace(ch, entry["to"])
    left = text.count("—") + len(re.findall(r"\s–\s", text))
    for h in hits:
        if h["name"].startswith(("—", "–")) and left:
            h["count"] = max(0, h["count"] - left)
    # A comma inserted before existing punctuation reads wrong.
    text = re.sub(r",\s*([,.;:!?])", r"\1", text)
    text = re.sub(r",\s*\n", "\n", text)
    hits = [h for h in hits if h["count"]]
    return text, hits, review


def _match_case(src, repl):
    if not repl:
        return repl
    if src.isupper() and len(src) > 1:
        return repl.upper()
    if src[0].isupper():
        return repl[0].upper() + repl[1:]
    return repl


def _sentence_at(text, pos):
    start = max(text.rfind(c, 0, pos) for c in ".!?\n") + 1
    ends = [i for i in (text.find(c, pos) for c in ".!?\n") if i != -1]
    return start, (min(ends) if ends else len(text))


def _why_not(text, m, entry, force):
    """Return a reason to leave this match alone, or None to replace it."""
    if force:
        return None
    if entry.get("mode") == "flag":
        lit = entry.get("literal")
        return "has a literal meaning" + (f" ({lit})" if lit else "") + " the swap would change"
    if any(q.start() < m.start() < q.end() for q in QUOTED_RE.finditer(text)):
        return "inside quotation marks: those are somebody's actual words"
    word = m.group(0)
    s0, _ = _sentence_at(text, m.start())
    at_start = not text[s0:m.start()].strip()
    if word[0].isupper() and not word.isupper() and not at_start:
        return "capitalised mid-sentence, so it is probably a name"
    if not entry["replace"]:
        s0, s1 = _sentence_at(text, m.start())
        left = len((text[s0:m.start()] + text[m.end():s1]).split())
        if 0 < left < 3:
            return "deleting it leaves a fragment"
    return None


def pass_lexical(text, lex, force=False):
    """Replace slop words and phrases. Longest first so phrases win. A match
    where the replacement would read wrong is left alone and reported."""
    hits, review = [], []
    entries = sorted(lex["phrases"] + lex["words"],
                     key=lambda e: len(e["find"]), reverse=True)
    for entry in entries:
        find = entry["find"]
        pattern = re.compile(r"\b" + re.escape(find).replace(r"\ ", r"\s+") + r"\b",
                             re.IGNORECASE)
        if not pattern.search(text):
            continue
        done = []

        def swap(m, entry=entry, done=done):
            why = _why_not(m.string, m, entry, force)
            if why:
                review.append({"what": f"\"{m.group(0)}\"",
                               "where": _excerpt(m.string, m.start(), m.end()),
                               "why": why,
                               "options": f"\"{entry['replace']}\" if that is what you mean"
                                          if entry["replace"] else "cut it if it adds nothing"})
                return m.group(0)
            done.append(1)
            return _match_case(m.group(0), entry["replace"])

        text = pattern.sub(swap, text)
        if done:
            hits.append({"find": find, "replace": entry["replace"] or "(deleted)",
                         "count": len(done), "family": entry["family"]})
    # Clean up after deletions. Deleting a whole clause leaves orphaned
    # punctuation behind ("system. ." or a line that now opens on a comma),
    # and that reads worse than the slop did.
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"(?m)^[ \t]*(?:[,.;:]+[ \t]*)+", "", text)
    text = re.sub(r"(?m)^[ \t](?=\S)", "", text)       # one space left by a deletion.
                                                      # Deeper indents are deliberate.
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r",\s*([,.;:!?])", r"\1", text)      # an em dash became a comma,
                                                      # then the clause after it went
    text = text.replace("...", "\x00ELL\x00")          # protect real ellipses
    text = re.sub(r"\.\s*\.+", ".", text)
    text = re.sub(r"([!?])\s*\.", r"\1", text)
    text = text.replace("\x00ELL\x00", "...")
    text = re.sub(r"(?m)^[ \t]+$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # An em dash that became a comma, followed by a sentence connective, leaves
    # a splice ("is important, also, it's proof"). Promote it to a full stop.
    text = re.sub(r",\s*(also|so|still|basically|in the end)\s*,\s*",
                  lambda m: ". " + m.group(1)[0].upper() + m.group(1)[1:] + ", ", text)
    return text, hits, review


VOWEL_SOUND = re.compile(r"(?i)^(?:[aeio]|u(?!ni|s[aeiu]|tens|ti)|hour|honest|honou?r|heir)")


def fix_after_swaps(original, text):
    """Two things a word swap breaks that are safe to put right mechanically:
    "an" before a word that no longer starts with a vowel sound ("an
    innovative tool" -> "a new tool"), and a word doubled by the swap ("just
    simply" -> "just just"). Only pairs the original did not already have."""
    fixes = []
    seen = {m.group(0).lower() for m in re.finditer(r"\b(?:an?)\s+[\w'-]+", original, re.I)}

    def article(m):
        art, word = m.group(1), m.group(2)
        if m.group(0).lower() in seen:
            return m.group(0)
        want = "an" if VOWEL_SOUND.match(word) else "a"
        if art.lower() == want:
            return m.group(0)
        fixes.append(f"\"{art} {word}\" -> \"{want} {word}\"")
        return (want.capitalize() if art[0].isupper() else want) + " " + word

    text = re.sub(r"\b(an?|An?)\s+([\w'-]+)", article, text)
    doubles = {m.group(0).lower() for m in re.finditer(r"\b(\w+)\s+\1\b", original, re.I)}

    def double(m):
        if m.group(0).lower() in doubles:
            return m.group(0)
        fixes.append(f"\"{m.group(0)}\" -> \"{m.group(1)}\"")
        return m.group(1)

    text = re.sub(r"\b(\w+)\s+\1\b", double, text, flags=re.I)
    return text, fixes


def scan_structures(text, lex):
    flags = []
    for s in lex["structures"]:
        try:
            pattern = re.compile(s["regex"], re.MULTILINE)
        except re.error:
            continue
        found = pattern.findall(text)
        if found:
            flags.append({"name": s["name"], "count": len(found), "fix": s["fix"]})
    # Sentence-length uniformity is structural too.
    lens = [len(s.split()) for s in SENT_RE.findall(text) if len(s.split()) > 2]
    if len(lens) >= 4:
        mean = sum(lens) / len(lens)
        var = sum((n - mean) ** 2 for n in lens) / len(lens)
        cv = (var ** 0.5) / mean if mean else 0
        if cv < 0.35:
            flags.append({
                "name": f"Uniform sentence length (variation {cv:.2f})",
                "count": len(lens),
                "fix": "Break one sentence in half. Let another run long. Machines write even.",
            })
    return flags


def restore_capitals(original, text):
    """Deleting an opener leaves the next word lower case.

    Only fix it for writers who capitalise their sentences in the first place:
    a deliberately lower-case voice is a style, not an artefact, and shouting
    over it would be exactly the kind of thing this script exists to stop.
    """
    starts = re.findall(r"(?:^|[.!?]\s+|\n)\s*([A-Za-z])", original)
    if not starts or sum(1 for c in starts if c.isupper()) * 2 < len(starts):
        return text
    return re.sub(r"(?:^|(?<=[.!?] )|(?<=[.!?]\n)|(?<=\n))\s*([a-z])",
                  lambda m: m.group(0)[:-1] + m.group(1).upper(), text)


def humanize(text, lex, force=False):
    raw_for_case = text
    text, urls = protect_urls(text)
    text, inv = pass_invisible(text, lex)
    before_swaps = text
    text, typo, review = pass_typographic(text, lex, force)
    text, lexi, more = pass_lexical(text, lex, force)
    text, repairs = fix_after_swaps(before_swaps, text)
    text = restore_capitals(raw_for_case, text)
    text = restore_urls(text, urls)
    for r in review + more:
        r["where"] = restore_urls(r["where"], urls)
    return text.strip() + "\n", {
        "invisible": inv,
        "typographic": typo,
        "lexical": lexi,
        "repairs": repairs,
        "review": review + more,
        "structures": scan_structures(text, lex),
    }


def render_report(report, out=sys.stderr):
    def head(title):
        print(f"\n{title}\n" + "-" * len(title), file=out)

    total = sum(h["count"] for h in report["invisible"]) \
        + sum(h["count"] for h in report["typographic"]) \
        + sum(h["count"] for h in report["lexical"])

    head("HUMANIZE REPORT")
    print(f"{total} machine artefacts removed, "
          f"{len(report.get('review', []))} left for you to rewrite, "
          f"{len(report['structures'])} structural tells flagged", file=out)

    if report["invisible"]:
        head("1. INVISIBLE CHARACTERS")
        for h in report["invisible"]:
            print(f"  {h['count']:>3}x  {h['name']}  -> {h['action']}", file=out)
    if report["typographic"]:
        head("2. TYPOGRAPHY")
        for h in report["typographic"]:
            print(f"  {h['count']:>3}x  {h['name']}  -> {h['to']}", file=out)
    if report["lexical"]:
        head("3. SLOP LEXICON")
        for h in report["lexical"]:
            print(f"  {h['count']:>3}x  {h['find']}  -> {h['replace']}   [{h['family']}]", file=out)
    if report["structures"]:
        head("4. STRUCTURAL TELLS  (not auto-fixed - rewrite these yourself)")
        for h in report["structures"]:
            print(f"  {h['count']:>3}x  {h['name']}\n        {h['fix']}", file=out)
    if report.get("repairs"):
        head("5. GRAMMAR PUT BACK AFTER A SWAP")
        for r in report["repairs"]:
            print(f"        {r}", file=out)
    if report.get("review"):
        head("6. LEFT AS WRITTEN  (a replacement here would read wrong - rewrite by hand)")
        for r in report["review"]:
            print(f"  {r['what']}: {r['why']}\n        {r['where']}\n        try: {r['options']}",
                  file=out)
    if not any(report.values()):
        head("CLEAN")
        print("  Nothing to strip.", file=out)
    print("", file=out)


def main():
    ap = argparse.ArgumentParser(description="Strip the machine fingerprint out of a draft.")
    ap.add_argument("input", nargs="?", default="-", help="file, or - for stdin")
    ap.add_argument("-o", "--out", help="write cleaned text here instead of stdout")
    ap.add_argument("--report", action="store_true", help="print what changed, to stderr")
    ap.add_argument("--json", action="store_true", help="emit {text, report} as JSON")
    ap.add_argument("--lexicon", default=LEX, help="path to slop.json")
    ap.add_argument("--force", action="store_true",
                    help="replace everything, even where it reads wrong (the old behaviour)")
    args = ap.parse_args()

    raw = sys.stdin.read() if args.input == "-" else open(args.input, encoding="utf-8").read()
    lex = load_lexicon(args.lexicon)
    clean, report = humanize(raw, lex, args.force)

    if args.json:
        print(json.dumps({"text": clean, "report": report}, indent=2, ensure_ascii=False))
        return
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(clean)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(clean)
    if args.report:
        render_report(report)


if __name__ == "__main__":
    main()
