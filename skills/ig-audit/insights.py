#!/usr/bin/env python3
"""
insights.py - pull your own post metrics from Meta's official Instagram API,
read-only, and rank them the way /ig-audit says to: by how far each post beat
your own median, and by sends per reach. Not by raw views.

  pull     fetch your media and each post's insights into
           ~/.claude/instagram/insights.json (metrics keep moving for a
           couple of days, so recent posts are re-fetched every pull)
  report   rank what is in that file
  tsv      write a flat table, one post per row, for a spreadsheet

Read-only. It needs a token with instagram_business_basic (and
instagram_business_manage_insights for the metrics), made in your own Meta
app: see ig-core/SKILL.md. It reads; it never posts, likes, follows or
messages.

Usage
  python3 insights.py pull
  python3 insights.py pull --since 2026-08-01 --max 60
  python3 insights.py report
  python3 insights.py report --by sends --top 10
  python3 insights.py tsv --out posts.tsv
"""

import argparse
import datetime
import os
import re
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ig-core"))
from igcore import (GraphError, SetupError, client_from_env, data_path, load_json,  # noqa: E402
                    number, save_json)

STORE = data_path("insights.json")
LOG = data_path("log.md")
HOOKS_TSV = data_path("hooks.tsv")
REFRESH_DAYS = 7          # metrics on a post this young are still moving


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def parse_ts(ts):
    # The API returns 2026-10-01T12:00:00+0000; fromisoformat wants +00:00.
    return datetime.datetime.fromisoformat(re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", ts))


# ---------------------------------------------------------------- pull

def list_media(g, since=None, max_items=None):
    fields = number("api.media_fields")
    fallback = number("api.media_fields_fallback")
    params = {"fields": fields, "limit": 50}
    if since:
        params["since"] = int(datetime.datetime.fromisoformat(since).timestamp())
    try:
        return list(g.paginate(f"{g.user_id}/media", max_items=max_items, **params))
    except GraphError as e:
        if e.code != 100 or not fallback:
            raise
        # media_product_type is documented for Facebook Login only; on
        # Instagram Login some accounts reject it. Ask without it.
        params["fields"] = fallback
        return list(g.paginate(f"{g.user_id}/media", max_items=max_items, **params))


def is_reel(m):
    return m.get("media_product_type") == "REELS" or (
        "media_product_type" not in m and m.get("media_type") == "VIDEO")


def _value(item):
    vals = item.get("values") or [{}]
    v = vals[0].get("value")
    if v is None:
        v = (item.get("total_value") or {}).get("value")
    return v


def fetch_insights(g, media, unsupported):
    """Ask for every metric at once. If the API rejects the batch (a metric
    that does not exist for this kind of post, or was retired), ask one at a
    time and remember which ones fail so the next post skips them."""
    kind = "reel" if is_reel(media) else "feed"
    wanted = [m for m in number(f"api.{kind}_metrics", []) if m not in unsupported[kind]]
    if not wanted:
        return {}
    try:
        data = g.get(f"{media['id']}/insights", metric=",".join(wanted)).get("data", [])
        return {item["name"]: _value(item) for item in data}
    except GraphError as e:
        if e.code not in (100, 10):
            raise
    out = {}
    for metric in wanted:
        try:
            data = g.get(f"{media['id']}/insights", metric=metric).get("data", [])
            for item in data:
                out[item["name"]] = _value(item)
        except GraphError as e:
            if e.code in (100, 10):
                unsupported[kind].add(metric)
            else:
                raise
    return out


def pull(g, since=None, max_items=None, refresh_all=False, out=sys.stderr):
    store = load_json(STORE, {"media": {}})
    media = list_media(g, since, max_items)
    unsupported = {"reel": set(), "feed": set()}
    fetched = 0
    for m in media:
        old = store["media"].get(m["id"], {})
        age = (now() - parse_ts(m["timestamp"])).days if m.get("timestamp") else 0
        if old.get("metrics") and not refresh_all and age > REFRESH_DAYS \
                and old.get("fetched_age", 0) > REFRESH_DAYS:
            store["media"][m["id"]] = {**old, **m}
            continue
        metrics = fetch_insights(g, m, unsupported)
        store["media"][m["id"]] = {**old, **m, "metrics": metrics,
                                   "fetched": now().isoformat(timespec="seconds"),
                                   "fetched_age": age}
        fetched += 1
        print(f"  {m.get('timestamp', '')[:10]}  {m['id']}  {len(metrics)} metrics", file=out)
    store["pulled"] = now().isoformat(timespec="seconds")
    store["account"] = {"user_id": g.user_id}
    save_json(STORE, store)
    skipped = {k: sorted(v) for k, v in unsupported.items() if v}
    print(f"\n{len(media)} posts listed, {fetched} fetched, written to {STORE}", file=out)
    if skipped:
        print(f"metrics the API refused (check ig-core/platform.json): {skipped}", file=out)
    return store


# ---------------------------------------------------------------- hooks

LOG_RE = re.compile(r"^\s*[-*]?\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(#?\d+[^|]*)\|\s*(.+?)\s*$")


def load_hooks():
    """What was actually said in the first seconds of each post.

    In order of trust: hooks.tsv (media id or permalink, then the hook line);
    log.md lines that /ig-reel writes ("- 2026-10-31 | #5 Time Collapse |
    first line"), matched by date when only one post went up that day; and
    last, the caption's first line, which is marked as such because a
    caption is not the spoken hook.
    """
    by_key, by_date = {}, {}
    if os.path.exists(HOOKS_TSV):
        with open(HOOKS_TSV, encoding="utf-8") as fh:
            for line in fh:
                key, _, hook = line.rstrip("\n").partition("\t")
                if hook and key.lower() not in ("id", "media", "permalink"):
                    by_key[key.strip().rstrip("/")] = hook.strip()
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8") as fh:
            for line in fh:
                m = LOG_RE.match(line)
                if m:
                    by_date.setdefault(m.group(1), []).append(
                        {"formula": m.group(2).strip(), "hook": m.group(3)})
    return by_key, by_date


def hook_for(m, by_key, by_date):
    for key in (m["id"], (m.get("permalink") or "").rstrip("/")):
        if key and key in by_key:
            return by_key[key], "hooks.tsv", None
    day = (m.get("timestamp") or "")[:10]
    if len(by_date.get(day, [])) == 1:
        e = by_date[day][0]
        return e["hook"], "log.md", e["formula"]
    first = (m.get("caption") or "").strip().splitlines()
    return (first[0] if first else ""), "caption", None


# ---------------------------------------------------------------- report

def rows(store):
    by_key, by_date = load_hooks()
    out = []
    for m in store.get("media", {}).values():
        x = m.get("metrics") or {}
        views = x.get("views")
        reach = x.get("reach") or 0
        hook, source, formula = hook_for(m, by_key, by_date)
        watch = x.get("ig_reels_avg_watch_time")
        unit = number("api.watch_time_unit", "ms")
        out.append({
            "id": m["id"],
            "date": (m.get("timestamp") or "")[:10],
            "kind": "reel" if is_reel(m) else m.get("media_type", "").lower(),
            "permalink": m.get("permalink", ""),
            "views": views, "reach": reach,
            "likes": x.get("likes", m.get("like_count")),
            "comments": x.get("comments", m.get("comments_count")),
            "saves": x.get("saved"), "shares": x.get("shares"),
            "follows": x.get("follows"),
            "skip_rate": x.get("reels_skip_rate"),
            "watch_s": (watch / 1000 if unit == "ms" else watch) if watch is not None else None,
            "sends_per_1k": (1000 * x["shares"] / reach) if reach and x.get("shares") is not None else None,
            "saves_per_1k": (1000 * x["saved"] / reach) if reach and x.get("saved") is not None else None,
            "hook": hook, "hook_source": source, "formula": formula,
        })
    # The outlier multiple: each post against this account's own median.
    basis = "views" if any(r["views"] for r in out) else "reach"
    vals = [r[basis] for r in out if r[basis]]
    median = statistics.median(vals) if vals else 0
    for r in out:
        r["multiple"] = (r[basis] / median) if median and r[basis] else None
    return out, basis, median


def fmt(v, spec="{:,.0f}", none="-"):
    return none if v is None else spec.format(v)


def report(store, by="multiple", top=5, out=sys.stdout):
    data, basis, median = rows(store)
    if not data:
        print("No posts in the store. Run: python3 insights.py pull", file=out)
        return
    key = {"multiple": "multiple", "sends": "sends_per_1k", "saves": "saves_per_1k",
           "watch": "watch_s"}[by]
    ranked = sorted([r for r in data if r[key] is not None], key=lambda r: -r[key])
    dates = sorted(r["date"] for r in data if r["date"])
    print(f"\nAUDIT  ·  {len(data)} posts  ·  {dates[0]} - {dates[-1]}  ·  "
          f"median {basis} {median:,.0f}  ·  ranked by {by}", file=out)
    print("=" * 96, file=out)

    def line(r):
        hook = r["hook"] if len(r["hook"]) <= 46 else r["hook"][:43] + "..."
        mark = "" if r["hook_source"] != "caption" else " (caption)"
        return (f"  {fmt(r['multiple'], '{:5.1f}x', '    -')}  {r['date']}  {r['kind']:<8}"
                f"{fmt(r['views'], '{:>9,.0f}')} views  {fmt(r['sends_per_1k'], '{:5.1f}')} sends/1k  "
                f"{fmt(r['watch_s'], '{:4.1f}s')} watch  {fmt(r['skip_rate'], '{:4.0f}%')} skip\n"
                f"         \"{hook}\"{mark}" + (f"  [{r['formula']}]" if r["formula"] else ""))

    print(f"TOP {min(top, len(ranked))}", file=out)
    for r in ranked[:top]:
        print(line(r), file=out)
    if len(ranked) > top:
        print(f"\nBOTTOM {min(top, len(ranked) - top)}", file=out)
        for r in ranked[-top:]:
            print(line(r), file=out)

    # Hook problem or profile problem: the distinction /ig-audit is built on.
    reels = [r for r in data if r["kind"] == "reel" and r["multiple"]]
    if len(reels) >= 6:
        third = max(2, len(reels) // 3)
        srt = sorted(reels, key=lambda r: -r["multiple"])
        top_t, bot_t = srt[:third], srt[-third:]

        def avg(rs, k):
            v = [r[k] for r in rs if r[k] is not None]
            return statistics.mean(v) if v else None

        print("\nTOP THIRD vs BOTTOM THIRD (reels)", file=out)
        for k, label, spec in (("watch_s", "avg watch", "{:.1f}s"), ("skip_rate", "skip rate", "{:.0f}%"),
                               ("sends_per_1k", "sends/1k reach", "{:.1f}"),
                               ("saves_per_1k", "saves/1k reach", "{:.1f}")):
            a, b = avg(top_t, k), avg(bot_t, k)
            if a is not None and b is not None:
                print(f"  {label:<16} {spec.format(a):>8}  vs {spec.format(b):>8}", file=out)
        n_caption = sum(1 for r in reels if r["hook_source"] == "caption")
        if n_caption:
            print(f"\n  {n_caption} of {len(reels)} hooks are caption first lines, not what was said. "
                  f"Add the spoken hooks to {HOOKS_TSV} (media id <TAB> hook) before calibrating.",
                  file=out)
    print(f"\n  n={len(data)}. " + ("Enough to see a pattern." if len(data) >= 25 else
                                    "Too few posts to call a pattern. Say so."), file=out)
    print("", file=out)


def write_tsv(store, path):
    data, _, _ = rows(store)
    cols = ["id", "date", "kind", "multiple", "views", "reach", "likes", "comments", "saves",
            "shares", "follows", "sends_per_1k", "saves_per_1k", "watch_s", "skip_rate",
            "formula", "hook_source", "hook", "permalink"]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\t".join(cols) + "\n")
        for r in sorted(data, key=lambda r: r["date"]):
            fh.write("\t".join("" if r[c] is None else
                               (f"{r[c]:.3f}" if isinstance(r[c], float) else str(r[c])).replace("\t", " ")
                               for c in cols) + "\n")


def main():
    ap = argparse.ArgumentParser(description="Your own Instagram metrics, read-only.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull")
    p.add_argument("--since", help="YYYY-MM-DD")
    p.add_argument("--max", type=int, help="at most this many posts")
    p.add_argument("--refresh-all", action="store_true", help="re-fetch metrics for old posts too")
    r = sub.add_parser("report")
    r.add_argument("--by", choices=["multiple", "sends", "saves", "watch"], default="multiple")
    r.add_argument("--top", type=int, default=5)
    t = sub.add_parser("tsv")
    t.add_argument("--out", default="-")
    args = ap.parse_args()

    if args.cmd == "pull":
        try:
            pull(client_from_env(), args.since, args.max, args.refresh_all)
        except (SetupError, GraphError) as e:
            print(e, file=sys.stderr)
            sys.exit(2)
        return
    store = load_json(STORE, {"media": {}})
    if args.cmd == "report":
        report(store, args.by, args.top)
    else:
        path = "/dev/stdout" if args.out == "-" else args.out
        write_tsv(store, path)


if __name__ == "__main__":
    main()
