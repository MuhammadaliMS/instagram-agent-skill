#!/usr/bin/env python3
"""
keyword_dm.py - "comment CRICKET and I'll send it to you", delivered through
Instagram's own Private Replies API.

This is the one DM flow Instagram sanctions for a tool: a person comments
first, and you send ONE private reply to that comment, inside 7 days. It goes
through the official Messaging API with a token from your own Meta app. It is
not browser automation, it cannot message anyone who did not comment, and it
never sends the same person the same delivery twice.

  scan    list the comments on a post that ask for the keyword and have not
          been answered yet. Sends nothing.
  send    send the delivery to that queue. Needs IG_WRITE=1, --confirm,
          and shows the queue and the exact message first.
  watch   scan + send every N minutes until you stop it (Ctrl-C). Same gate.

The message comes from a file, so you write it, humanize it and read it
before anything goes out. {username} in it becomes the commenter's handle.

Usage
  python3 keyword_dm.py scan --media latest --keyword CRICKET
  python3 keyword_dm.py send --media 1789... --keyword CRICKET --message delivery.txt --confirm
  python3 keyword_dm.py send ... --public-reply "Sent, check your DMs" --confirm
  python3 keyword_dm.py watch --media latest --keyword CRICKET --message delivery.txt --every 10 --confirm
"""

import argparse
import datetime
import os
import re
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ig-core"))
from igcore import (GraphError, SetupError, WriteGateError, client_from_env,  # noqa: E402
                    data_path, load_json, number, require_write_enabled, save_json)

STATE = data_path("dm_state.json")
# Meta's codes for "this one person can't be messaged" (unavailable, no such
# user, outside the window). Record them and move on; retrying will not help.
# Anything else, a missing permission above all, stops the run instead, so a
# setup mistake never marks a whole queue as done.
UNREACHABLE = {551, 2018001, 2534022}


def fold(text):
    """Lower-case, strip accents and emoji: 'CRICKET!!' and 'cricket 🏏' match."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def matches(comment, keyword, loose=False):
    words = re.findall(r"[a-z0-9]+", fold(comment))
    key = fold(keyword).strip()
    if loose:
        return any(w.startswith(key) for w in words)
    return key in words


def parse_ts(ts):
    return datetime.datetime.fromisoformat(re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", ts))


def resolve_media(g, media):
    if media != "latest":
        return media
    items = g.get(f"{g.user_id}/media", fields="id,caption,timestamp", limit=1).get("data", [])
    if not items:
        raise SystemExit("no posts on this account")
    return items[0]["id"]


def queue(g, media_id, keyword, state, loose=False, now=None):
    """Comments that asked, have not been answered, and are still inside the
    private-reply window. Returns (todo, skipped_counts)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    window = datetime.timedelta(days=number("dm.private_reply_window_days", 7))
    sent = state.setdefault("sent", {})
    people = set(state.setdefault("people", {}).get(media_id, []))
    todo, skipped = [], {"answered": 0, "too old": 0, "own": 0, "hidden": 0, "no keyword": 0}
    for c in g.paginate(f"{media_id}/comments", fields="id,text,timestamp,username,from,hidden",
                        limit=50):
        who = (c.get("from") or {}).get("id") or c.get("username")
        if not matches(c.get("text", ""), keyword, loose):
            skipped["no keyword"] += 1
        elif c["id"] in sent or who in people:
            skipped["answered"] += 1
        elif str(who) == str(g.user_id):
            skipped["own"] += 1
        elif c.get("hidden"):
            skipped["hidden"] += 1
        elif c.get("timestamp") and now - parse_ts(c["timestamp"]) > window:
            skipped["too old"] += 1
        else:
            todo.append(c)
            people.add(who)          # one delivery per person, even if they comment twice
    return todo, skipped


def render_message(template, comment):
    return template.replace("{username}", comment.get("username") or "there").strip()


def check_message(text):
    limit = number("dm.message_max_bytes", 1000)
    size = len(text.encode("utf-8"))
    if size > limit:
        raise SystemExit(f"message is {size} bytes; Instagram's limit is {limit}. Cut it.")
    if "{{" in text:
        raise SystemExit("message still has a {{placeholder}} in it")


def send(g, media_id, todo, template, state, public_reply=None, out=sys.stdout,
         sleep=time.sleep):
    require_write_enabled("Sending DMs")
    interval = number("dm.send_interval_s", 5.0)
    cap = number("dm.max_per_run", 50)
    people = state.setdefault("people", {}).setdefault(media_id, [])
    sent_n = 0
    for c in todo[:cap]:
        text = render_message(template, c)
        who = (c.get("from") or {}).get("id") or c.get("username")
        record = {"media": media_id, "user": c.get("username"), "at":
                  datetime.datetime.now().isoformat(timespec="seconds")}
        try:
            res = g.post_json(f"{g.user_id}/messages",
                              {"recipient": {"comment_id": c["id"]}, "message": {"text": text}})
            record["message_id"] = res.get("message_id")
            if public_reply:
                g.post(f"{c['id']}/replies", message=public_reply)
            print(f"  sent   @{c.get('username')}: \"{c.get('text', '')[:40]}\"", file=out)
            sent_n += 1
        except GraphError as e:
            if e.code not in UNREACHABLE:
                save_json(STATE, state)
                raise
            record["error"] = str(e)
            print(f"  skip   @{c.get('username')}: {e}", file=out)
        state["sent"][c["id"]] = record
        if who not in people:
            people.append(who)
        save_json(STATE, state)     # after every send, so a crash never double-sends
        sleep(interval)
    if len(todo) > cap:
        print(f"  {len(todo) - cap} left in the queue (max {cap} per run). Run again.", file=out)
    return sent_n


def print_queue(todo, skipped, template, out=sys.stdout):
    print(f"\nKEYWORD QUEUE  ·  {len(todo)} to answer  ·  skipped: "
          + ", ".join(f"{v} {k}" for k, v in skipped.items() if v), file=out)
    print("=" * 72, file=out)
    for c in todo[:20]:
        print(f"  @{c.get('username', '?'):<22} {c.get('timestamp', '')[:16]}  "
              f"\"{c.get('text', '')[:40]}\"", file=out)
    if len(todo) > 20:
        print(f"  ... and {len(todo) - 20} more", file=out)
    if template and todo:
        print("\n  each gets exactly this (first one shown):", file=out)
        for line in render_message(template, todo[0]).splitlines():
            print(f"  | {line}", file=out)
    print("", file=out)


def main():
    ap = argparse.ArgumentParser(description="Keyword comment -> one private reply, official API.")
    ap.add_argument("cmd", choices=["scan", "send", "watch"])
    ap.add_argument("--media", required=True, help="media id, or 'latest'")
    ap.add_argument("--keyword", required=True)
    ap.add_argument("--message", help="file with the delivery message")
    ap.add_argument("--public-reply", help="also reply publicly under the comment with this")
    ap.add_argument("--loose", action="store_true", help="match words that start with the keyword")
    ap.add_argument("--every", type=float, default=10, help="watch: minutes between scans")
    ap.add_argument("--confirm", action="store_true", help="required to send")
    args = ap.parse_args()

    template = None
    if args.message:
        with open(args.message, encoding="utf-8") as fh:
            template = fh.read()
        check_message(render_message(template, {"username": "x" * 30}))
    if args.cmd != "scan" and (not template or not args.confirm):
        raise SystemExit("send/watch need --message and --confirm. Run scan first and show "
                         "the person the queue and the message.")

    try:
        g = client_from_env()
        media_id = resolve_media(g, args.media)
        while True:
            state = load_json(STATE, {"sent": {}, "people": {}})
            todo, skipped = queue(g, media_id, args.keyword, state, args.loose)
            print_queue(todo, skipped, template)
            if args.cmd == "scan":
                return
            if todo:
                n = send(g, media_id, todo, template, state, args.public_reply)
                print(f"  {n} sent.\n")
            if args.cmd == "send":
                return
            time.sleep(args.every * 60)
    except (SetupError, GraphError, WriteGateError) as e:
        print(e, file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
