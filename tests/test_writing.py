"""The text tools: beats, hookscore, caption, humanize, detect, swipe."""

import json
import os
import tempfile
import unittest

import helpers
import beats
import detect
import hookscore
import humanize

LEX = humanize.load_lexicon()


class Beats(unittest.TestCase):
    SCRIPT = ("Proposals used to take me five hours. || 5 HOURS -> 20 MIN\n"
              "Now they take twenty minutes with one template.\n"
              "Steal the five hour fix. || STEAL THIS\n")

    def test_cards_are_not_spoken(self):
        a = beats.analyse(self.SCRIPT)
        self.assertEqual(a["beats"][0]["text"], "Proposals used to take me five hours.")
        self.assertEqual(a["beats"][0]["card"], "5 HOURS -> 20 MIN")
        self.assertNotIn("HOURS", " ".join(b["text"] for b in a["beats"]))

    def test_spoken_numbers_count_as_concrete(self):
        a = beats.analyse(self.SCRIPT)
        self.assertGreater(a["beats"][0]["concrete"], 0)
        self.assertFalse(any("no number" in n for n in a["notes"]))

    def test_timeline_words_tile_the_duration(self):
        t = beats.timeline(beats.analyse(self.SCRIPT))
        self.assertEqual(t["format"], "ig-beats/1")
        w = t["words"]
        self.assertEqual(w[0]["start"], 0.0)
        self.assertAlmostEqual(w[-1]["end"], t["duration"], places=1)
        for a, b in zip(w, w[1:]):
            self.assertAlmostEqual(a["end"], b["start"], places=2)
        self.assertEqual({"text", "start", "end", "id"}, set(w[0]))
        self.assertEqual([c["beat"] for c in t["cards"]], [1, 3])

    def test_cli_writes_words_file(self):
        path = os.path.join(tempfile.mkdtemp(), "words.json")
        r = helpers.run_cli("ig-reel", "beats.py", "-", "--words", path, stdin=self.SCRIPT)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(path) as fh:
            self.assertEqual(json.load(fh)[0]["text"], "Proposals")


class HookScore(unittest.TestCase):
    def test_greeting_is_weak(self):
        _, score, verdict, flags = hookscore.run("Hey guys, today I wanted to talk about content strategy")
        self.assertLess(score, 50)
        self.assertTrue(flags)

    def test_concrete_cost_hook_is_strong(self):
        _, score, verdict, _ = hookscore.run("I lost $18,000 because of one missing contract.")
        self.assertGreaterEqual(score, 70)

    def test_weights_change_the_score_and_zero_weight_does_not_cap(self):
        hook = "I lost $18,000 because of one missing contract."
        _, default, _, _ = hookscore.run(hook)
        heavy = {c: 0.0 for c in hookscore.CHECKS}
        heavy["SPECIFICITY"] = 1.0
        _, weighted, _, _ = hookscore.run(hook, heavy)
        self.assertNotEqual(round(default, 1), round(weighted, 1))
        # ADDRESS (70 here) has zero weight, so only SPECIFICITY sets the floor.
        results, _, _, _ = hookscore.run(hook)
        self.assertAlmostEqual(weighted, results["SPECIFICITY"][0], places=1)

    def test_weights_file_loads_and_normalises(self):
        path = os.path.join(tempfile.mkdtemp(), "w.json")
        with open(path, "w") as fh:
            json.dump({"weights": {"LENGTH": 2, "SPECIFICITY": 2}}, fh)
        os.environ.pop("IG_HOOK_WEIGHTS", None)
        try:
            w = hookscore.load_weights(path)
        finally:
            os.environ["IG_HOOK_WEIGHTS"] = "off"
        self.assertAlmostEqual(sum(w.values()), 1.0)
        self.assertEqual(w["LENGTH"], 0.5)


class Caption(unittest.TestCase):
    def test_hashtag_cap_from_platform(self):
        r = helpers.run_cli("ig-caption", "caption.py", "-",
                            stdin="I lost $18,000 on one clause.\n#a #b #c #d #e #f\n")
        self.assertIn("cap of 5", r.stdout)


class Humanize(unittest.TestCase):
    def clean(self, text, force=False):
        out, report = humanize.humanize(text, LEX, force)
        return out.strip(), report

    def test_invisible_characters_go(self):
        out, _ = self.clean("Re​ad th­is﻿ now.")
        self.assertEqual(out, "Read this now.")

    def test_safe_dashes_are_fixed(self):
        self.assertEqual(self.clean("It took 5—10 days.")[0], "It took 5-10 days.")
        self.assertEqual(self.clean("The plan — the real one — worked.")[0],
                         "The plan, the real one, worked.")
        self.assertEqual(self.clean("The answer — money.")[0], "The answer, money.")

    def test_dash_joining_two_sentences_is_left_and_reported(self):
        out, rep = self.clean("We shipped the new pricing page — it doubled signups in a week.")
        self.assertIn("—", out)
        self.assertTrue(any("two sentences" in r["what"] for r in rep["review"]))

    def test_force_restores_old_behaviour(self):
        out, rep = self.clean("We shipped the new pricing page — it doubled signups in a week.", True)
        self.assertNotIn("—", out)
        self.assertEqual(rep["review"], [])

    def test_literal_words_are_flagged_not_swapped(self):
        out, rep = self.clean("Unlocking is fine but I had to unlock my phone and buy a harness.")
        self.assertIn("unlock my phone", out)
        self.assertIn("harness", out)
        self.assertEqual({r["what"].lower() for r in rep["review"]}, {"\"unlock\"", "\"harness\""})

    def test_hashtags_handles_quotes_and_links_untouched(self):
        text = 'We leverage it. #leverage @leverage "leverage" https://x.com/leverage'
        out, _ = self.clean(text)
        self.assertTrue(out.startswith("We use it."))
        for keep in ("#leverage", "@leverage", '"leverage"', "https://x.com/leverage"):
            self.assertIn(keep, out)

    def test_article_repaired_after_swap(self):
        out, rep = self.clean("We built an innovative tool.")
        self.assertEqual(out, "We built a new tool.")
        self.assertTrue(rep["repairs"])

    def test_original_articles_left_alone(self):
        out, _ = self.clean("It is an historic day and a hour late.")
        self.assertEqual(out, "It is an historic day and a hour late.")

    def test_idempotent(self):
        once, _ = self.clean("Moreover, we leverage a robust, seamless stack — fast.")
        twice, _ = self.clean(once)
        self.assertEqual(once, twice)


class Detect(unittest.TestCase):
    def test_slop_scores_below_plain(self):
        slop = ("In today's fast-paced world, it's crucial to leverage innovative tools. "
                "Moreover, a robust strategy unlocks seamless growth. Furthermore, the "
                "landscape is ever-evolving. Ultimately, synergy is key.")
        plain = ("I lost $18,000 in March because one contract had no payment date. "
                 "Section 4 fixed it. Now invoices go out the day I deliver, not the day "
                 "a client feels ready. Two clients pushed back. Both signed anyway.")
        _, s_slop, _ = detect.run(slop, LEX)
        _, s_plain, _ = detect.run(plain, LEX)
        self.assertLess(s_slop, s_plain)


class Swipe(unittest.TestCase):
    def test_ranks_by_multiple_not_views(self):
        tsv = ("account\tfollowers\tmedian\tviews\thook\n"
               "@big\t2000000\t500000\t600000\tin this video I am going to show you my routine\n"
               "@small\t4000\t5000\t300000\tnobody tells you your first 30 reels are supposed to flop\n")
        path = os.path.join(tempfile.mkdtemp(), "c.tsv")
        with open(path, "w") as fh:
            fh.write(tsv)
        r = helpers.run_cli("ig-viral", "swipe.py", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(r.stdout.index("@small"), r.stdout.index("@big"))


if __name__ == "__main__":
    unittest.main()
