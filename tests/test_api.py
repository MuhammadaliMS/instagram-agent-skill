"""insights, calibrate, publish and keyword_dm against a fake Graph API.
Nothing here touches the network."""

import datetime
import io
import json
import os
import random
import subprocess
import tempfile
import unittest

import helpers
import calibrate
import hookscore
import igcore
import insights
import keyword_dm
import publish

UID = "17841400000000000"


def iso(days_ago):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S+0000")


class Insights(unittest.TestCase):
    def tearDown(self):
        helpers.reset_home()

    def media_routes(self, reject_batch=False):
        media = {"data": [
            {"id": "m1", "media_type": "VIDEO", "media_product_type": "REELS",
             "timestamp": iso(20), "caption": "I lost $18,000 on one clause.\nmore", "permalink": "https://p/1"},
            {"id": "m2", "media_type": "VIDEO", "media_product_type": "REELS",
             "timestamp": iso(10), "caption": "Hey guys, today we talk", "permalink": "https://p/2"},
            {"id": "m3", "media_type": "IMAGE", "media_product_type": "FEED",
             "timestamp": iso(5), "caption": "A photo", "permalink": "https://p/3"},
        ]}

        def ins(method, url, headers, body):
            metric = url.split("metric=")[1].split("&")[0]
            names = metric.replace("%2C", ",").split(",")
            if reject_batch and len(names) > 1:
                return 400, {"error": {"code": 100, "message": "invalid metric"}}
            if "reels_skip_rate" in names and len(names) == 1 and reject_batch:
                return 400, {"error": {"code": 100, "message": "invalid metric"}}
            views = {"m1": 40000, "m2": 2000, "m3": 3000}[url.split("/")[4]]
            vals = {"views": views, "reach": views // 2, "shares": views // 100, "saved": 5,
                    "ig_reels_avg_watch_time": 7400, "reels_skip_rate": 40}
            return {"data": [{"name": n, "values": [{"value": vals.get(n, 1)}]} for n in names]}

        return [("GET", "/insights", ins), ("GET", f"{UID}/media", media)]

    def test_pull_and_report(self):
        g, t = helpers.graph(self.media_routes())
        store = insights.pull(g, out=io.StringIO())
        self.assertEqual(set(store["media"]), {"m1", "m2", "m3"})
        self.assertEqual(store["media"]["m1"]["metrics"]["views"], 40000)
        # Reels ask for reel metrics, feed posts for feed metrics.
        reel_call = [c for c in t.find("GET", "/insights") if "/m1/" in c["url"]][0]
        self.assertIn("ig_reels_avg_watch_time", reel_call["url"])
        feed_call = [c for c in t.find("GET", "/insights") if "/m3/" in c["url"]][0]
        self.assertNotIn("ig_reels_avg_watch_time", feed_call["url"])

        rows, basis, median = insights.rows(store)
        by = {r["id"]: r for r in rows}
        self.assertEqual(basis, "views")
        self.assertEqual(median, 3000)
        self.assertAlmostEqual(by["m1"]["multiple"], 40000 / 3000)
        self.assertAlmostEqual(by["m1"]["watch_s"], 7.4)
        self.assertEqual(by["m1"]["hook_source"], "caption")

    def test_rejected_batch_falls_back_one_metric_at_a_time(self):
        g, t = helpers.graph(self.media_routes(reject_batch=True))
        store = insights.pull(g, out=io.StringIO())
        self.assertIn("views", store["media"]["m1"]["metrics"])
        self.assertNotIn("reels_skip_rate", store["media"]["m1"]["metrics"])

    def test_hooks_come_from_tsv_then_log(self):
        with open(os.path.join(helpers.HOME, "hooks.tsv"), "w") as fh:
            fh.write("m1\tNobody tells you this costs $18,000.\n")
        day = iso(10)[:10]
        with open(os.path.join(helpers.HOME, "log.md"), "w") as fh:
            fh.write(f"- {day} | #5 Time Collapse | Proposals took five hours.\n")
        g, _ = helpers.graph(self.media_routes())
        rows, _, _ = insights.rows(insights.pull(g, out=io.StringIO()))
        by = {r["id"]: r for r in rows}
        self.assertEqual((by["m1"]["hook_source"], by["m1"]["hook"]),
                         ("hooks.tsv", "Nobody tells you this costs $18,000."))
        self.assertEqual((by["m2"]["hook_source"], by["m2"]["formula"]), ("log.md", "#5 Time Collapse"))


class Calibrate(unittest.TestCase):
    def test_spearman_and_auc(self):
        self.assertAlmostEqual(calibrate.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(calibrate.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertEqual(calibrate.auc([3, 4], [1, 2]), 1.0)
        self.assertEqual(calibrate.auc([1], [1]), 0.5)

    def test_fit_puts_weight_on_the_check_that_predicts(self):
        rng = random.Random(1)
        samples = []
        for _ in range(60):
            s = {c: rng.uniform(20, 100) for c in hookscore.CHECKS}
            samples.append((s, s["SPECIFICITY"] + rng.gauss(0, 5)))
        w, raw = calibrate.fit(samples)
        self.assertEqual(max(w, key=w.get), "SPECIFICITY")
        self.assertAlmostEqual(sum(w.values()), 1.0)
        # Shrinkage: with 60 reels and SHRINK=20, nothing drops to exactly zero.
        self.assertTrue(all(v > 0 for v in w.values()))

    def test_few_reels_shrink_toward_equal(self):
        s = [({c: float(i) for c in hookscore.CHECKS}, float(i)) for i in range(4)]
        s = [({**a, "LENGTH": 50.0}, y) for a, y in s]
        w, _ = calibrate.fit(s)
        self.assertLess(max(w.values()) - min(w.values()), 0.2)


class Publish(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not helpers.have("ffmpeg"):
            raise unittest.SkipTest("ffmpeg not installed")
        cls.dir = tempfile.mkdtemp()
        cls.video = os.path.join(cls.dir, "reel.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc=size=270x480:rate=30:duration=4", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000:duration=4", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-movflags", "+faststart",
                        cls.video], check=True)

    def setUp(self):
        self.caption = os.path.join(self.dir, "caption.txt")
        with open(self.caption, "w") as fh:
            fh.write("I lost $18,000 because of one missing clause.\n\nComment CLAUSE and I will send it.\n#freelance")

    def tearDown(self):
        helpers.reset_home()
        os.environ.pop("IG_WRITE", None)

    def routes(self):
        status = {"n": 0}

        def container(*_):
            status["n"] += 1
            return {"status_code": "IN_PROGRESS" if status["n"] < 2 else "FINISHED"}

        return [
            ("POST", "rupload.facebook.com", {"success": True}),
            ("POST", f"{UID}/media_publish", {"id": "live1"}),
            ("POST", f"{UID}/media", {"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/c1"}),
            ("GET", "content_publishing_limit", {"data": [{"quota_usage": 3, "config": {"quota_total": 50}}]}),
            ("GET", "/c1", container),
            ("GET", "/live1", {"permalink": "https://www.instagram.com/reel/abc/"}),
        ]

    def quiet(self):
        return io.StringIO()

    def test_check_writes_a_draft_and_touches_nothing(self):
        d = publish.check(self.video, self.caption, out=self.quiet())
        self.assertEqual(d["status"], "checked", d["problems"])
        self.assertEqual(len(d["confirm"]), 8)
        self.assertTrue(os.path.exists(publish.draft_path(d["id"])))

    def test_placeholder_blocks(self):
        with open(self.caption, "w") as fh:
            fh.write("I saved {{your number}} hours.")
        d = publish.check(self.video, self.caption, out=self.quiet())
        self.assertEqual(d["status"], "blocked")

    def test_full_gate(self):
        d = publish.check(self.video, self.caption, out=self.quiet())
        g, t = helpers.graph(self.routes())
        with self.assertRaises(igcore.WriteGateError):
            publish.stage(g, d, out=self.quiet(), sleep=lambda s: None)
        os.environ["IG_WRITE"] = "1"
        d = publish.stage(g, d, out=self.quiet(), sleep=lambda s: None)
        self.assertEqual(d["status"], "staged")
        up = t.find("POST", "rupload")[0]
        self.assertEqual(up["headers"]["Authorization"], "OAuth TEST_TOKEN")
        self.assertEqual(up["headers"]["file_size"], str(os.path.getsize(self.video)))
        self.assertEqual(len(up["body"]), os.path.getsize(self.video))
        self.assertFalse(t.find("POST", "media_publish"))   # staging never publishes

        with self.assertRaises(SystemExit):
            publish.publish(g, d, "wrongcode", out=self.quiet())
        d = publish.publish(g, d, d["confirm"], out=self.quiet())
        self.assertEqual(d["permalink"], "https://www.instagram.com/reel/abc/")
        with open(os.path.join(helpers.HOME, "log.md")) as fh:
            self.assertIn("live1", fh.read())

    def test_changed_caption_after_approval_is_refused(self):
        d = publish.check(self.video, self.caption, out=self.quiet())
        os.environ["IG_WRITE"] = "1"
        g, _ = helpers.graph(self.routes())
        d = publish.stage(g, d, out=self.quiet(), sleep=lambda s: None)
        d["caption"] += " edited"
        with self.assertRaises(SystemExit) as cm:
            publish.publish(g, d, d["confirm"], out=self.quiet())
        self.assertIn("changed", str(cm.exception))


class KeywordDM(unittest.TestCase):
    def tearDown(self):
        helpers.reset_home()
        os.environ.pop("IG_WRITE", None)

    def test_matching(self):
        m = keyword_dm.matches
        self.assertTrue(m("CRICKET!!", "cricket"))
        self.assertTrue(m("Cricket 🏏 please", "CRICKET"))
        self.assertTrue(m("crícket", "cricket"))
        self.assertFalse(m("crickets are loud", "cricket"))
        self.assertTrue(m("crickets are loud", "cricket", loose=True))
        self.assertFalse(m("great reel", "cricket"))

    def comments(self):
        return {"data": [
            {"id": "c1", "text": "CRICKET", "timestamp": iso(1), "username": "ana", "from": {"id": "u1"}},
            {"id": "c2", "text": "cricket pls", "timestamp": iso(1), "username": "ana", "from": {"id": "u1"}},
            {"id": "c3", "text": "Cricket", "timestamp": iso(9), "username": "old", "from": {"id": "u3"}},
            {"id": "c4", "text": "cricket", "timestamp": iso(1), "username": "me", "from": {"id": UID}},
            {"id": "c5", "text": "nice", "timestamp": iso(1), "username": "bo", "from": {"id": "u5"}},
            {"id": "c6", "text": "cricket", "timestamp": iso(1), "username": "hid", "from": {"id": "u6"}, "hidden": True},
            {"id": "c7", "text": "cricket!", "timestamp": iso(2), "username": "cy", "from": {"id": "u7"}},
        ]}

    def test_queue_rules(self):
        g, _ = helpers.graph([("GET", "/m1/comments", self.comments())])
        todo, skipped = keyword_dm.queue(g, "m1", "CRICKET", {"sent": {}, "people": {}})
        self.assertEqual([c["id"] for c in todo], ["c1", "c7"])   # one per person
        self.assertEqual(skipped["too old"], 1)
        self.assertEqual(skipped["own"], 1)
        self.assertEqual(skipped["hidden"], 1)
        self.assertEqual(skipped["no keyword"], 1)

    def test_send_records_and_never_repeats(self):
        routes = [("GET", "/m1/comments", self.comments()),
                  ("POST", f"{UID}/messages", {"recipient_id": "r", "message_id": "mid"}),
                  ("POST", "/replies", {"id": "rep"})]
        g, t = helpers.graph(routes)
        state = {"sent": {}, "people": {}}
        todo, _ = keyword_dm.queue(g, "m1", "cricket", state)
        with self.assertRaises(igcore.WriteGateError):
            keyword_dm.send(g, "m1", todo, "Hi {username}, here it is.", state, sleep=lambda s: None)
        os.environ["IG_WRITE"] = "1"
        n = keyword_dm.send(g, "m1", todo, "Hi {username}, here it is.", state,
                            public_reply="Sent!", out=io.StringIO(), sleep=lambda s: None)
        self.assertEqual(n, 2)
        body = json.loads(t.find("POST", "/messages")[0]["body"])
        self.assertEqual(body, {"recipient": {"comment_id": "c1"}, "message": {"text": "Hi ana, here it is."}})
        self.assertEqual(len(t.find("POST", "/replies")), 2)
        saved = igcore.load_json(keyword_dm.STATE, {})
        again, skipped = keyword_dm.queue(g, "m1", "cricket", saved)
        self.assertEqual(again, [])
        self.assertEqual(skipped["answered"], 3)

    def test_one_unreachable_person_skips_but_permission_error_stops(self):
        os.environ["IG_WRITE"] = "1"
        todo = self.comments()["data"][:1] + self.comments()["data"][6:7]

        def unreachable(method, url, headers, body):
            cid = json.loads(body)["recipient"]["comment_id"]
            return (400, {"error": {"code": 551, "message": "unavailable"}}) if cid == "c1" else {"message_id": "x"}

        g, _ = helpers.graph([("POST", "/messages", unreachable)])
        state = {"sent": {}, "people": {}}
        n = keyword_dm.send(g, "m1", todo, "hi", state, out=io.StringIO(), sleep=lambda s: None)
        self.assertEqual(n, 1)
        self.assertIn("error", state["sent"]["c1"])

        g, _ = helpers.graph([("POST", "/messages", (400, {"error": {"code": 10, "message": "no permission"}}))])
        state = {"sent": {}, "people": {}}
        with self.assertRaises(igcore.GraphError):
            keyword_dm.send(g, "m1", todo, "hi", state, out=io.StringIO(), sleep=lambda s: None)
        self.assertEqual(state["sent"], {})

    def test_message_size_limit(self):
        with self.assertRaises(SystemExit):
            keyword_dm.check_message("x" * 1001)
        keyword_dm.check_message("é" * 400)


if __name__ == "__main__":
    unittest.main()
