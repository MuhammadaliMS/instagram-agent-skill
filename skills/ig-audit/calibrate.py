#!/usr/bin/env python3
"""
calibrate.py - re-weight hookscore.py's five checks on your own reels.

hookscore.py ships with the five checks weighted equally, and its own README
says what that is worth: on a corpus of 74 real hooks it barely separated a
creator's hits from the same creator's misses (AUC 0.56). Two of the five
checks measured nothing on that corpus. That was somebody else's channels.
This measures it on yours.

For every reel in ~/.claude/instagram/insights.json that has a hook line, it
scores the hook, then asks of each check: did reels that scored higher on
this check do better, for this account? (Spearman rank correlation against
the outcome.) Checks that predicted nothing get weight near zero; checks that
predicted something get more. The weights are shrunk toward equal, harder
when there are fewer reels, so twelve posts cannot talk it into anything.

It then reports the honest number: leave-one-out AUC, top third against
bottom third, for the equal weights and for yours. Each reel is scored with
weights fitted on the others, so a reel never grades itself. If yours do not
beat equal, it says so and does not write the file.

Outcome, by default, is log(views / your median views): the outlier multiple.
Others: sends (shares per reach), watch (average watch time), skip (skip rate,
lower is better).

Usage
  python3 calibrate.py                    # report only
  python3 calibrate.py --write            # save weights if they beat equal
  python3 calibrate.py --outcome watch
  python3 calibrate.py --reset            # delete your weights, back to equal
"""

import argparse
import datetime
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ig-core"))
sys.path.insert(0, os.path.join(HERE, "..", "ig-reel"))
from igcore import load_json, save_json  # noqa: E402
import hookscore  # noqa: E402
import insights   # noqa: E402

MIN_REELS = 15           # fewer than this and the weights are noise
SHRINK = 20              # pseudo-reels of "equal weights" mixed into the fit


def ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(xs, ys):
    if len(xs) < 3:
        return 0.0
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    return cov / math.sqrt(vx * vy) if vx and vy else 0.0


def auc(pos, neg):
    """Probability a random top-third reel outscores a random bottom-third one."""
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def fit(samples):
    """samples: [(check_scores dict, outcome)]. Returns weights summing to 1."""
    n = len(samples)
    raw = {}
    for c in hookscore.CHECKS:
        raw[c] = max(0.0, spearman([s[c] for s, _ in samples], [o for _, o in samples]))
    total = sum(raw.values())
    equal = 1.0 / len(hookscore.CHECKS)
    trust = n / (n + SHRINK)
    if not total:
        return {c: equal for c in hookscore.CHECKS}, raw
    return {c: trust * raw[c] / total + (1 - trust) * equal for c in hookscore.CHECKS}, raw


def outcome_of(r, kind):
    if kind == "multiple":
        return math.log(r["multiple"]) if r["multiple"] else None
    if kind == "sends":
        return r["sends_per_1k"]
    if kind == "watch":
        return r["watch_s"]
    if kind == "skip":
        return -r["skip_rate"] if r["skip_rate"] is not None else None
    raise ValueError(kind)


def collect(store, kind, allow_captions=False):
    data, _, _ = insights.rows(store)
    out = []
    for r in data:
        if r["kind"] != "reel" or not r["hook"]:
            continue
        if r["hook_source"] == "caption" and not allow_captions:
            continue
        y = outcome_of(r, kind)
        if y is None:
            continue
        checks, _, _, _ = hookscore.run(r["hook"])
        out.append(({c: checks[c][0] for c in hookscore.CHECKS}, y, r))
    return out


def overall(scores, weights):
    floor_checks = [c for c in hookscore.CHECKS if weights is None or weights[c] >= 0.05]
    floor = min(scores[c] for c in floor_checks)
    if weights is None:
        centre = sum(scores.values()) / len(scores)
    else:
        centre = sum(scores[c] * weights[c] for c in hookscore.CHECKS)
    return centre * 0.6 + floor * 0.4


def evaluate(samples):
    """Leave-one-out: each reel scored by weights fitted without it."""
    ys = sorted(y for _, y, _ in samples)
    third = max(2, len(samples) // 3)
    hi, lo = ys[-third], ys[third - 1]
    default_pos, default_neg, cal_pos, cal_neg = [], [], [], []
    for i, (scores, y, _) in enumerate(samples):
        rest = [(s, o) for j, (s, o, _) in enumerate(samples) if j != i]
        w, _ = fit(rest)
        d, c = overall(scores, None), overall(scores, w)
        if y >= hi:
            default_pos.append(d)
            cal_pos.append(c)
        elif y <= lo:
            default_neg.append(d)
            cal_neg.append(c)
    return auc(default_pos, default_neg), auc(cal_pos, cal_neg)


def main():
    ap = argparse.ArgumentParser(description="Fit hookscore weights to your own reels.")
    ap.add_argument("--outcome", choices=["multiple", "sends", "watch", "skip"], default="multiple")
    ap.add_argument("--write", action="store_true", help="save the weights if they beat equal")
    ap.add_argument("--force", action="store_true", help="save even with few reels or no gain")
    ap.add_argument("--allow-captions", action="store_true",
                    help="use caption first lines where no spoken hook is recorded")
    ap.add_argument("--reset", action="store_true", help="delete saved weights")
    args = ap.parse_args()

    path = hookscore.WEIGHTS_FILE
    if args.reset:
        if os.path.exists(path):
            os.remove(path)
            print(f"removed {path}; hookscore.py is back to equal weights")
        return

    store = load_json(insights.STORE, {"media": {}})
    samples = collect(store, args.outcome, args.allow_captions)
    n = len(samples)
    print(f"\nCALIBRATE  ·  {n} reels with a hook and a {args.outcome} outcome")
    print("=" * 72)
    if n < 6:
        print(f"  Not enough to fit anything. Need {MIN_REELS}+ reels with spoken hooks recorded\n"
              f"  in {insights.HOOKS_TSV} or log.md. Run insights.py pull first.\n")
        sys.exit(1)

    weights, raw = fit([(s, y) for s, y, _ in samples])
    print("  check          rank corr.   weight   (equal is 0.20)")
    for c in hookscore.CHECKS:
        print(f"  {c:<13} {raw[c]:>+10.2f}   {weights[c]:>6.2f}")
    a_default, a_cal = evaluate(samples)
    print("-" * 72)
    print(f"  leave-one-out AUC, top third vs bottom third:  equal {a_default:.2f}   "
          f"yours {a_cal:.2f}   (0.50 is a coin flip)")
    better = a_cal is not None and a_default is not None and a_cal > a_default + 0.02
    if n < MIN_REELS:
        print(f"\n  {n} reels is under {MIN_REELS}. Treat every number above as noise.")
    if not better:
        print("\n  Your weights do not beat equal weights on reels they did not see. "
              "Nothing that reads the hook text predicts your results better than the default. "
              "That is a real finding: the difference is in the video, not the words.")
    if args.write:
        if (better and n >= MIN_REELS) or args.force:
            save_json(path, {
                "weights": weights, "raw_correlation": raw, "n": n,
                "outcome": args.outcome, "auc_equal": a_default, "auc_calibrated": a_cal,
                "fitted": datetime.date.today().isoformat(),
            })
            print(f"\n  wrote {path}. hookscore.py now uses these weights "
                  "(--default-weights to compare).")
        else:
            print("\n  Not written. (--force to write anyway.)")
    print("")


if __name__ == "__main__":
    main()
