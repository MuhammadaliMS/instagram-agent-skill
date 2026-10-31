---
name: ig-publish
description: >-
  Post a finished Reel to Instagram through Meta's official Content Publishing
  API, behind a three-step gate - check (lint the file and caption, print the
  exact preview), stage (private upload), publish (only with the confirm code
  after the user approves that exact preview). Use when the user says "post
  this", "publish the reel", "put it up", "schedule this reel", or has a
  rendered reel and a final caption and wants it live without opening the app.
---

# ig-publish

The rest of the pack ends with a copy-ready block and the user posting it by
hand. This is the optional last step for users who want it: the documented
Content Publishing API for Professional accounts, with a token from their own
Meta app. It is not browser automation, it never logs in as anyone, and it
can only publish. It cannot like, follow or comment.

It is **off** until the user sets it up (`/ig-core`) **and** sets `IG_WRITE=1`
themselves. Never set that variable, never write it into a file, and never
tell the user it is fine to leave it on.

```bash
python3 publish.py check reel.mp4 --caption caption.txt     # preview, touches nothing
python3 publish.py stage <draft>                            # private upload
python3 publish.py publish <draft> --confirm <code>         # live
python3 publish.py list                                     # drafts and what happened to them
python3 publish.py quota                                    # posts left in the last 24h
```

## The gate, step by step

**1. Before check.** The reel has been through `/ig-watch` with no FAIL and
the caption through `/ig-caption` and `/ig-human`. If not, do those first.

**2. check.** Runs the upload spec on the file (codec, fps, length, size,
moov atom), lints the caption (length, hashtag cap, `{{placeholders}}`), scores
it with the humanizer, and prints the preview: the file, its fingerprint, feed
or Reels-only, labels, and the caption character for character. It saves a
draft with a SHA-256 of the file and the caption and prints a **confirm code**.

Show the user that preview in full. Then ask, in one line, whether to post
exactly that. Do not paraphrase the caption and do not trim it.

**3. stage.** Only after the user says yes. Uploads the file into a container
(resumable upload of the local file, or `--video-url` for a public URL of the
same file) and waits for Instagram to finish processing. The container is
private and expires by itself in 24 hours. If the file changed since `check`,
it refuses.

**4. publish.** Uses the confirm code from step 2. It refuses if the file or
caption changed since the user approved them, if the container is not
FINISHED, or if the 24-hour quota is used up. Then it posts, prints the
permalink, and appends the post to `~/.claude/instagram/log.md` so `/ig-audit`
can find it later.

A yes in this conversation covers one draft. A new caption, a re-render or a
second reel is a new preview and a new yes.

## Options to raise with the user, not decide for them

- `--reels-only` keeps it off the main grid.
- `--ai-generated` applies Instagram's AI label. If the reel uses generated
  visuals or a synthetic voice, say so and let the user choose.
- `--trial manual|auto` posts it as a trial reel, shown to non-followers first.
  `auto` lets Instagram graduate it on performance. Meta added `trial_params`
  recently; check the field against the current docs before relying on it.

## When it fails

- `IG_WRITE` not set: tell the user what it is for and stop. They set it.
- Resumable upload rejected: some apps on Instagram Login only accept
  `video_url`. Ask the user for a public URL of the same file (their own
  storage) and stage with `--video-url`.
- `ERROR` while processing: the file broke a spec rule the probe missed.
  Re-export with `-c:v libx264 -pix_fmt yuv420p -c:a aac -ar 48000 -movflags +faststart`
  and check again.
- Quota used up: 50 API posts per rolling 24 hours at the time of writing; the
  script reads the live number.

## Never

- Never publish without a yes from the user to the exact preview `check` printed.
- Never reuse a confirm code across drafts, or pass one the user has not seen.
- Never set `IG_WRITE`, never ask for the user's Instagram password, and never
  paste their access token into chat or into a file other than
  `~/.claude/instagram/credentials.env`.
