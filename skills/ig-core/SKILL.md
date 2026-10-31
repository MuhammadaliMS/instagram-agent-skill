---
name: ig-core
description: >-
  The shared layer under the Instagram pack - every platform number in one
  file (caption limit, hashtag cap, safe zone, API specs) with the date each
  was last checked, and the one-time setup that connects the user's own Meta
  app to the official Instagram API for /ig-audit, /ig-publish and keyword
  DMs. Use when the user asks to "connect Instagram", "set up the API",
  "check my token", "is the hashtag cap still 5", "update the platform
  numbers", or when another ig- skill reports a missing token or a stale number.
---

# ig-core

Two things live here, and every other skill leans on both.

## 1. The platform numbers

`platform.json` holds every cap, limit and pixel the pack uses: the 2,200
character caption, the 125-character feed cut, the 5-hashtag cap, the Reel
safe zone, speaking rate, the Content Publishing specs and quota, the API
version and metric names, the private-reply window. No script hard-codes one.
Each block says when it was last checked and where it came from.

```bash
python3 igcore.py numbers             # print them all
python3 igcore.py stale --days 120    # which blocks nobody has re-checked lately
```

When Instagram moves a number:

- Change it for this user only: put the same keys in
  `~/.claude/instagram/platform.json`. It is merged over the shipped file and
  survives updates. Example: `{"speech": {"wpm": 180}}`.
- Change it for everyone: edit `platform.json` here, update `checked` and
  `source`, and send it upstream.

If a user says "Instagram changed X" and it contradicts this file, believe
Instagram, check the source, and update the file. That is the whole point of
keeping the numbers in one place.

## 2. The official API connection

Needed only for `/ig-audit` (`insights.py`), `/ig-publish` and
`keyword_dm.py`. Everything else in the pack works with no account at all.

It uses **Instagram API with Instagram Login**: the user's own Meta app, in
development mode, used only on their own Professional account. That needs no
App Review and no business verification.

Walk the user through it. They do every step themselves, in their browser.
Never ask for their Instagram or Facebook password.

1. Their Instagram account must be **Professional** (Creator or Business) and public.
2. At developers.facebook.com, **Create App**, type **Business**.
3. Add the **Instagram** product, choose **API setup with Instagram login**.
4. Under **Generate access tokens**, **Add account** and sign in to their own
   Instagram there. Grant what the skills need:
   - `instagram_business_basic`: always
   - `instagram_business_manage_insights`: `/ig-audit`
   - `instagram_business_content_publish`: `/ig-publish`
   - `instagram_business_manage_comments` and `instagram_business_manage_messages`: keyword DMs
5. Copy the generated token (long-lived, valid about 60 days) into
   `~/.claude/instagram/credentials.env`, which they create and
   `chmod 600` themselves:

   ```
   IG_ACCESS_TOKEN=...
   ```

   Or `export IG_ACCESS_TOKEN=...` in their shell. Tell them not to paste the
   token into the chat.
6. Check it, read-only:

   ```bash
   python3 igcore.py whoami
   ```

Optional keys in the same file: `IG_USER_ID` (saves one lookup),
`IG_GRAPH_HOST` (`graph.facebook.com` if they use Facebook Login instead),
`IG_API_VERSION`.

### The write switch

Reading needs only the token. Anything that changes the live account
(staging or publishing a post, sending a DM) also needs `IG_WRITE=1`, set by
the user. Never set it yourself, never add it to their files, and when a
script stops on it, explain what it unlocks and let them decide.

### When the token expires

Long-lived tokens last about 60 days. When calls start failing with code 190,
the user generates a new one in the same dashboard and replaces the line in
`credentials.env`.

## Files this pack keeps in ~/.claude/instagram/

| file | written by | read by |
| --- | --- | --- |
| `voice.md` | the user / any skill on first run | every writing skill |
| `swipe.md` | `/ig-viral` | `/ig-reel`, `/ig-plan` |
| `log.md` | `/ig-reel` on "yes", `/ig-publish` on publish | `/ig-audit` |
| `hooks.tsv` | the user (media id TAB spoken hook) | `insights.py`, `calibrate.py` |
| `insights.json` | `insights.py pull` | `insights.py report`, `calibrate.py` |
| `hook_weights.json` | `calibrate.py --write` | `hookscore.py` |
| `drafts/` | `publish.py` | `publish.py` |
| `dm_state.json` | `keyword_dm.py` | `keyword_dm.py` |
| `platform.json` | the user (optional overrides) | everything |
| `credentials.env` | the user | the API scripts |
