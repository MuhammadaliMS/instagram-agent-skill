"""ig-watch on a synthetic reel whose answers are known in advance:

  0.0-0.5s  nothing moves               -> FIRST FRAME fails
  0.5-2.0s  a square slides across
  2.0s      hard cut to a new background -> one cut
  2.0-8.0s  nothing moves               -> STATIC warns (6s > 4s)
  always    "HELLO WORLD" in the bottom band -> SAFE ZONE warns, HOOK CARD passes
  audio     silent for 1s, then a tone  -> OPENING warns
"""

import json
import os
import subprocess
import tempfile
import unittest

import helpers

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None

W, H, FPS, SECONDS = 540, 960, 30, 8


def make_reel(path):
    font = ImageFont.load_default(size=56)
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-f", "lavfi", "-i",
         "aevalsrc='if(gt(t,1),0.3*sin(2*PI*440*t),0)':s=48000:d=8",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         "-movflags", "+faststart", path], stdin=subprocess.PIPE)
    for i in range(FPS * SECONDS):
        t = i / FPS
        img = Image.new("RGB", (W, H), (235, 225, 200) if t < 2 else (120, 170, 220))
        d = ImageDraw.Draw(img)
        if t < 2:
            x = 40 if t < 0.5 else 40 + (t - 0.5) * 220
            d.rectangle([x, 300, x + 140, 440], fill=(200, 60, 40))
        d.text((40, int(H * 0.86)), "HELLO WORLD", font=font, fill=(10, 10, 10))
        proc.stdin.write(img.tobytes())
    proc.stdin.close()
    proc.wait()


@unittest.skipUnless(helpers.have("ffmpeg") and Image is not None, "needs ffmpeg and Pillow")
class Watch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import watch
        cls.watch = watch
        cls.dir = tempfile.mkdtemp()
        cls.video = os.path.join(cls.dir, "reel.mp4")
        make_reel(cls.video)
        cls.words = os.path.join(cls.dir, "words.json")
        with open(cls.words, "w") as fh:
            json.dump([{"text": "hello", "start": 1.0, "end": 1.4},
                       {"text": "world", "start": 1.4, "end": 1.9},
                       {"text": "goodbye", "start": 6.5, "end": 7.0},
                       {"text": "world", "start": 7.0, "end": 7.5}], fh)
        cls.script = os.path.join(cls.dir, "script.txt")
        with open(cls.script, "w") as fh:
            fh.write("Hello world. || HELLO WORLD\nGoodbye world.\n")
        cls.a = watch.analyse(cls.video, cls.words, cls.script)
        cls.rows = cls.a["rows"]

    def status(self, name):
        return self.rows[name]["status"]

    def test_spec(self):
        self.assertEqual(self.status("SPEC"), "PASS", self.rows["SPEC"])
        self.assertIn("moov first", self.rows["SPEC"]["detail"])

    def test_static_opening_fails(self):
        self.assertEqual(self.status("FIRST FRAME"), "FAIL", self.rows["FIRST FRAME"])
        self.assertIn("FIRST FRAME", self.a["fails"])

    def test_one_cut_at_two_seconds(self):
        self.assertEqual(len(self.a["cuts"]), 1, self.a["cuts"])
        self.assertAlmostEqual(self.a["cuts"][0], 2.0, delta=0.1)

    def test_long_still_stretch_warns(self):
        self.assertEqual(self.status("STATIC"), "WARN")
        self.assertGreater(self.a["longest_still"], 5.5)

    def test_audio_onset_from_words(self):
        self.assertEqual(self.status("OPENING"), "WARN")
        self.assertEqual(self.a["onset"], 1.0)

    def test_loop_passes_on_echoed_word(self):
        self.assertEqual(self.status("LOOP"), "PASS", self.rows["LOOP"])

    def test_script_matches(self):
        self.assertEqual(self.status("SCRIPT"), "PASS", self.rows["SCRIPT"])

    @unittest.skipUnless(helpers.have("tesseract"), "needs tesseract")
    def test_text_in_bottom_band(self):
        self.assertEqual(self.status("SAFE ZONE"), "WARN", self.rows["SAFE ZONE"])
        self.assertIn("bottom band", self.rows["SAFE ZONE"]["detail"])
        self.assertEqual(self.status("HOOK CARD"), "PASS", self.rows["HOOK CARD"])

    @unittest.skipUnless(helpers.have("tesseract"), "needs tesseract")
    def test_caption_sync_is_measured(self):
        # HELLO WORLD stays up all 8s but is only said at 1-2s and "world" at 7s,
        # so some frames match and the middle ones drift.
        self.assertIn(self.status("CAPTIONS"), ("PASS", "WARN"))
        self.assertIn("caption frames match the voice", self.rows["CAPTIONS"]["detail"])

    def test_spec_rejects_bad_codec(self):
        p = dict(self.a["probe"], vcodec="vp9", fps=90.0)
        status, detail = self.watch.check_spec(p, True)
        self.assertEqual(status, "FAIL")
        self.assertIn("vp9", detail)
        self.assertIn("90 fps", detail)

    def test_cli_exit_code(self):
        r = helpers.run_cli("ig-watch", "watch.py", self.video, "--json")
        self.assertEqual(r.returncode, 1)          # FIRST FRAME fails
        self.assertEqual(json.loads(r.stdout)["rows"]["FIRST FRAME"]["status"], "FAIL")

    def test_gemini_json_is_parsed_from_noisy_output(self):
        blob = 'Sure! Here you go:\n```json\n{"first_frame": {"moving": false}, "loop": {"lands": true}}\n```'
        self.assertEqual(self.watch.parse_json_blob(blob)["first_frame"]["moving"], False)


if __name__ == "__main__":
    unittest.main()
