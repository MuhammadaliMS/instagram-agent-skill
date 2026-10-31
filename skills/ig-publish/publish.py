#!/usr/bin/env python3
"""
publish.py - post a Reel through Meta's official Content Publishing API, with
a gate a person has to walk through at every step.

Three steps, each its own command, each one stopping:

  check    lint the file and the caption, print EXACTLY what would go up,
           and save it as a draft with a fingerprint of both. Touches nothing
           on Instagram.
  stage    upload the video into a container on Instagram. The container is
           private: nobody sees it, it expires on its own in 24 hours.
  publish  make the staged container live. Only if the file and caption
           still match the fingerprint from `check`, only with the draft's
           confirm code, and only with IG_WRITE=1 set by you.

This is the documented API for Professional accounts, with a token from your
own Meta app (ig-core/SKILL.md). It is not browser automation and it does not
log in as you. It cannot like, follow, comment or DM; /ig-dm handles the one
messaging flow Instagram allows.

Usage
  python3 publish.py check reel.mp4 --caption caption.txt
  python3 publish.py stage  <draft>                       # local file, resumable upload
  python3 publish.py stage  <draft> --video-url https://...   # or a public URL
  python3 publish.py publish <draft> --confirm <code>
  python3 publish.py list
  python3 publish.py quota
"""

import argparse
import datetime
import hashlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for sub in ("ig-core", "ig-watch", "ig-caption", "ig-human"):
    sys.path.insert(0, os.path.join(HERE, "..", sub))
from igcore import (GraphError, SetupError, WriteGateError, client_from_env,  # noqa: E402
                    data_path, load_json, number, require_write_enabled, save_json)

DRAFTS = data_path("drafts")
LOG = data_path("log.md")


def sha256(path=None, text=None):
    h = hashlib.sha256()
    if text is not None:
        h.update(text.encode("utf-8"))
    else:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def draft_path(draft_id):
    return os.path.join(DRAFTS, f"{draft_id}.json")


def load_draft(draft_id):
    path = draft_path(draft_id)
    if not os.path.exists(path):
        raise SystemExit(f"no draft {draft_id}. `publish.py list` shows the drafts.")
    return load_json(path, {})


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- check

def check(video, caption_path, cover=None, share_to_feed=True, ai_generated=False, trial=None,
          out=sys.stdout):
    import caption as caption_lint
    import watch

    with open(caption_path, encoding="utf-8") as fh:
        caption = fh.read().strip()
    problems, warnings = [], []

    p = watch.probe(video)
    spec_status, spec_detail = watch.check_spec(p, watch.moov_first(video))
    if spec_status == "FAIL":
        problems.append("file: " + spec_detail)
    elif spec_status == "WARN":
        warnings.append("file: " + spec_detail)

    lint = caption_lint.analyse(caption)
    for c in lint["checks"]:
        if c["status"] == "FAIL":
            problems.append(f"caption {c['check'].lower()}: {c['detail']}")
        elif c["status"] == "WARN":
            warnings.append(f"caption {c['check'].lower()}: {c['detail']}")
    if "{{" in caption:
        problems.append("caption still has a {{placeholder}} in it")

    try:
        import detect
        import humanize
        _, human, verdict = detect.run(caption, humanize.load_lexicon())
        if verdict != "PASS":
            warnings.append(f"caption human score {human:.0f} {verdict}: run /ig-human")
    except ImportError:
        pass                 # /ig-human not installed next to this skill

    vsha, csha = sha256(video), sha256(text=caption)
    draft_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    confirm = vsha[:4] + csha[:4]
    draft = {
        "id": draft_id, "created": now(), "kind": "REELS",
        "video": os.path.abspath(video), "video_sha256": vsha, "bytes": p["bytes"],
        "duration": p["duration"], "caption": caption, "caption_sha256": csha,
        "cover": os.path.abspath(cover) if cover else None,
        "share_to_feed": share_to_feed, "ai_generated": ai_generated, "trial": trial,
        "confirm": confirm, "problems": problems, "warnings": warnings,
        "status": "blocked" if problems else "checked",
    }
    save_json(draft_path(draft_id), draft)

    print("\nPUBLISH PREVIEW  (nothing has been sent to Instagram)", file=out)
    print("=" * 72, file=out)
    print(f"  file      {video}", file=out)
    print(f"            {p['width']}x{p['height']}, {p['duration']:.1f}s, "
          f"{p['bytes'] / 1048576:.1f} MB, sha256 {vsha[:12]}", file=out)
    print(f"  feed      {'also shown in the main grid/feed' if share_to_feed else 'Reels tab only'}",
          file=out)
    if ai_generated:
        print("  label     marked as AI-generated", file=out)
    if trial:
        print(f"  trial     trial reel, shown to non-followers first ({trial} graduation)", file=out)
    print("  caption   (exactly this, character for character)", file=out)
    print("  " + "-" * 70, file=out)
    for line in caption.splitlines() or [""]:
        print(f"  | {line}", file=out)
    print("  " + "-" * 70, file=out)
    for w in warnings:
        print(f"  WARN  {w}", file=out)
    for pr in problems:
        print(f"  FAIL  {pr}", file=out)
    print(f"\n  draft {draft_id}" + ("  BLOCKED until the FAILs are fixed" if problems else ""),
          file=out)
    if not problems:
        print(f"  confirm code {confirm}  (only give this to publish after the person says yes "
              "to this exact preview)", file=out)
        print(f"  next: python3 publish.py stage {draft_id}", file=out)
    print("", file=out)
    return draft


# ---------------------------------------------------------------- stage

def container_status(g, cid):
    return g.get(cid, fields="status_code,status")


def wait_for(g, cid, out=sys.stderr, sleep=time.sleep):
    poll = number("publishing.status_poll_s", 30)
    timeout = number("publishing.status_timeout_s", 600)
    waited = 0
    while True:
        st = container_status(g, cid)
        code = st.get("status_code")
        print(f"  container {cid}: {code}", file=out)
        if code in ("FINISHED", "PUBLISHED"):
            return st
        if code in ("ERROR", "EXPIRED"):
            raise SystemExit(f"Instagram could not process the video: {st.get('status') or code}")
        if waited >= timeout:
            raise SystemExit(f"still {code} after {timeout}s. Re-run `stage` later; the "
                             "container keeps processing on Instagram's side.")
        sleep(poll)
        waited += poll


def stage(g, draft, video_url=None, out=sys.stderr, sleep=time.sleep):
    require_write_enabled("Staging")
    if draft["status"] == "blocked":
        raise SystemExit("draft is blocked by FAILs from `check`. Fix them and run check again.")
    if sha256(draft["video"]) != draft["video_sha256"]:
        raise SystemExit("the video changed since `check`. Run check again so the preview is true.")
    params = {
        "media_type": "REELS",
        "caption": draft["caption"],
        "share_to_feed": "true" if draft["share_to_feed"] else "false",
    }
    if draft.get("ai_generated"):
        params["is_ai_generated"] = "true"
    if draft.get("trial"):
        params["trial_params"] = {"graduation_strategy":
                                  "SS_PERFORMANCE" if draft["trial"] == "auto" else "MANUAL"}
    if video_url:
        params["video_url"] = video_url
        res = g.post(f"{g.user_id}/media", **params)
    else:
        params["upload_type"] = "resumable"
        res = g.post(f"{g.user_id}/media", **params)
        uri = res.get("uri") or (f"https://rupload.facebook.com/ig-api-upload/"
                                 f"{g.version}/{res['id']}")
        print(f"  uploading {draft['bytes'] / 1048576:.1f} MB ...", file=out)
        with open(draft["video"], "rb") as fh:
            g.request("POST", uri, raw=fh, headers={
                "Authorization": f"OAuth {g.token}",
                "offset": "0",
                "file_size": str(draft["bytes"]),
                "Content-Length": str(draft["bytes"]),
            })
    cid = res["id"]
    draft.update({"container": cid, "staged": now(), "status": "staging"})
    save_json(draft_path(draft["id"]), draft)
    wait_for(g, cid, out, sleep)
    draft["status"] = "staged"
    save_json(draft_path(draft["id"]), draft)
    print(f"\n  staged as container {cid}. Private, expires in 24h.\n"
          f"  next, after the person says yes: python3 publish.py publish {draft['id']} "
          f"--confirm <code>", file=out)
    return draft


# ---------------------------------------------------------------- publish

def quota(g):
    data = g.get(f"{g.user_id}/content_publishing_limit", fields="quota_usage,config").get("data", [{}])
    row = data[0] if data else {}
    total = (row.get("config") or {}).get("quota_total", number("publishing.posts_per_24h", 50))
    return row.get("quota_usage", 0), total


def publish(g, draft, confirm, out=sys.stdout):
    require_write_enabled("Publishing")
    if draft.get("status") == "published":
        raise SystemExit(f"already published: {draft.get('permalink')}")
    if draft.get("status") != "staged" or not draft.get("container"):
        raise SystemExit("not staged yet. Run `stage` first.")
    if confirm != draft["confirm"]:
        raise SystemExit("confirm code does not match this draft. Show the person the preview "
                         "from `check` and use the code printed under it.")
    if sha256(draft["video"]) != draft["video_sha256"] \
            or sha256(text=draft["caption"]) != draft["caption_sha256"]:
        raise SystemExit("file or caption changed since the person approved the preview. "
                         "Run check again.")
    used, total = quota(g)
    if used >= total:
        raise SystemExit(f"publishing quota used up ({used}/{total} in the last 24h). Try later.")
    st = container_status(g, draft["container"])
    if st.get("status_code") != "FINISHED":
        raise SystemExit(f"container is {st.get('status_code')}, not FINISHED. "
                         "Re-stage if it expired.")
    res = g.post(f"{g.user_id}/media_publish", creation_id=draft["container"])
    media_id = res["id"]
    info = g.get(media_id, fields="permalink,timestamp")
    draft.update({"status": "published", "media_id": media_id, "published": now(),
                  "permalink": info.get("permalink")})
    save_json(draft_path(draft["id"]), draft)
    first = draft["caption"].splitlines()[0] if draft["caption"] else ""
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"- published {datetime.date.today().isoformat()} | {media_id} | "
                 f"{info.get('permalink', '')} | {first}\n")
    print(f"\n  LIVE  {info.get('permalink')}  (media {media_id}, quota {used + 1}/{total})\n",
          file=out)
    return draft


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description="Gated Reel publishing via the official API.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("video")
    c.add_argument("--caption", required=True, help="caption text file")
    c.add_argument("--cover", help="cover image (kept with the draft; upload needs a public URL)")
    c.add_argument("--reels-only", action="store_true", help="do not also show it in the feed")
    c.add_argument("--ai-generated", action="store_true", help="label the reel as AI-generated")
    c.add_argument("--trial", choices=["manual", "auto"],
                   help="post as a trial reel (non-followers first)")
    s = sub.add_parser("stage")
    s.add_argument("draft")
    s.add_argument("--video-url", help="public URL of the same file, instead of uploading it")
    p = sub.add_parser("publish")
    p.add_argument("draft")
    p.add_argument("--confirm", required=True, help="the confirm code printed by `check`")
    sub.add_parser("list")
    sub.add_parser("quota")
    args = ap.parse_args()

    try:
        if args.cmd == "check":
            d = check(args.video, args.caption, args.cover, not args.reels_only,
                      args.ai_generated, args.trial)
            sys.exit(1 if d["problems"] else 0)
        if args.cmd == "list":
            if not os.path.isdir(DRAFTS):
                print("no drafts")
                return
            for name in sorted(os.listdir(DRAFTS)):
                d = load_json(os.path.join(DRAFTS, name), {})
                first = (d.get("caption") or "").splitlines()[:1]
                print(f"  {d.get('id')}  {d.get('status'):<10} {os.path.basename(d.get('video', ''))}"
                      f"  \"{first[0][:50] if first else ''}\"  {d.get('permalink') or ''}")
            return
        g = client_from_env()
        if args.cmd == "quota":
            used, total = quota(g)
            print(f"{used}/{total} posts used in the last 24h")
        elif args.cmd == "stage":
            stage(g, load_draft(args.draft), args.video_url)
        elif args.cmd == "publish":
            publish(g, load_draft(args.draft), args.confirm)
    except (SetupError, GraphError, WriteGateError) as e:
        print(e, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
