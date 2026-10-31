---
name: ig-watch
description: >-
  Check a rendered Reel file before it goes up - motion in the first frame,
  how often the picture changes, text under the Instagram interface, captions
  out of sync with the voice, the loop, loudness, and whether the file will
  upload cleanly - by measuring the actual video, with an optional Gemini
  watch-and-listen pass. Use when the user has an exported .mp4/.mov and says
  "check this reel", "is this ready to post", "watch my video", "review the
  render", or right after any reel render finishes and before /ig-publish.
---

# ig-watch

Every other skill in this pack reads text. This one reads the video.

`/ig-reel` tells you to open on motion, change the frame every beat, keep text
out of the bottom of the frame and loop the last line. Advice is not
evidence: this decodes every frame and measures each of those, so the answer
is a number, not a feeling.

```bash
python3 watch.py reel.mp4                                         # the measurements
python3 watch.py reel.mp4 --words vo/words.json --script script.txt
python3 watch.py reel.mp4 --gemini                                # plus a model that watches it
python3 watch.py reel.mp4 --out reel.watch.md                     # keep the report
```

Needs `ffmpeg`. `tesseract` (`brew install tesseract`) turns on the text
checks. Runs in a few seconds for a 60-second reel without `--gemini`.

## What it measures

| check | how | fails when |
| --- | --- | --- |
| SPEC | ffprobe + the MP4 box order | codec, fps, length, size or width is outside what the Content Publishing API takes; moov atom at the end |
| FIRST FRAME | mean pixel change over the first 0.5s | nothing moves (FAIL) or barely moves (WARN) |
| CUTS | jumps in frame difference | the average shot runs past 4s |
| STATIC | longest run of near-identical frames | anything sits still past 4s |
| SAFE ZONE | tesseract word boxes against the overlay margins in `platform.json` | a word sits in the top band, the bottom band or the right rail |
| HOOK CARD | text in the frame at 0:00 | no text at frame one |
| CAPTIONS | on-screen words against the voice's word timings | under 70% of caption frames match what is being said; reports the drift |
| SCRIPT | the voice's words against the script | the take dropped or added lines |
| LOOP | last frame against the first; last line against the hook | neither hands back to the start |
| LOUDNESS | ffmpeg ebur128 | far from -14 LUFS, or true peak above -1 dBTP |
| OPENING | first word, or first sound | dead air past 0.5s |

`--words` takes `[{text, start, end}]`: the output of `hyperframes transcribe`,
or the estimate `beats.py --words` writes from the script before the voice
exists. Text that matches nothing the voice ever says is treated as a label on
the art, not a caption, and does not count against sync.

## The Gemini pass

`--gemini` sends the file to a model that watches and listens, and asks for a
fixed JSON shape: first frame, hook spoken and on screen, text outside the safe
zone, caption mismatches, still stretches, the loop, the audio, the biggest
problem and the fixes. It uses, in order:

1. `IG_GEMINI_CMD`, any command that takes `<video> <prompt>` and prints the answer;
2. `gemini-video` on the PATH or in `~/bin`;
3. the Gemini API with `GEMINI_API_KEY` and `pip install google-genai`.

The report then says where the model and the measurements **agree** and where
they **disagree**. Model reviews vary from run to run. Act on agreement; where
they disagree, pull the frame and look (`ffmpeg -i reel.mp4 -ss 12.0 -frames:v 1 f.png`).

## How to use the result

1. Run it on every export, before `/ig-caption` and long before `/ig-publish`.
2. Fix FAILs. They are the file failing a hard rule (the upload spec, a static
   opening). Fix in the source, re-render, re-run. Do not hand-wave a FAIL.
3. Read every WARN with the frame open. A SAFE ZONE warning on a deliberate
   top caption may be fine at 11% of the height; the same warning on the
   bottom band is not, because the caption covers it.
4. Show the user the table, then the two or three fixes that matter most. Do
   not paste the whole JSON.

## Rules

- **Measure, then judge.** Quote the number from the report when you name a problem.
- **Never call a reel good because it passed.** Passing means nothing broke. It
  says nothing about whether the idea travels.
- **The safe zone and loudness targets live in `ig-core/platform.json`.** If
  Instagram moves its interface, change them there, not here.
- This skill reads a local file. It uploads nothing except to Gemini when
  `--gemini` is asked for, and it never posts.
