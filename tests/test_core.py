import datetime
import json
import os
import unittest

import helpers
import igcore


class PlatformNumbers(unittest.TestCase):
    def tearDown(self):
        helpers.reset_home()
        igcore.load_platform(refresh=True)

    def test_shipped_numbers_load(self):
        p = igcore.load_platform(refresh=True)
        self.assertEqual(igcore.number("caption.limit"), 2200)
        self.assertEqual(igcore.number("caption.hashtag_limit"), 5)
        for block in ("caption", "speech", "reel", "api", "publishing", "dm"):
            self.assertIn("checked", p[block], block)
            datetime.date.fromisoformat(p[block]["checked"])

    def test_missing_path_returns_default(self):
        self.assertEqual(igcore.number("nope.nothing", 7), 7)
        self.assertEqual(igcore.number("caption.limit.deeper", "d"), "d")

    def test_user_override_merges_over_shipped(self):
        with open(os.path.join(helpers.HOME, "platform.json"), "w") as fh:
            json.dump({"speech": {"wpm": 190}}, fh)
        igcore.load_platform(refresh=True)
        self.assertEqual(igcore.number("speech.wpm"), 190)
        self.assertEqual(igcore.number("speech.max_beat_s"), 4.0)   # untouched sibling

    def test_override_reaches_the_scripts(self):
        with open(os.path.join(helpers.HOME, "platform.json"), "w") as fh:
            json.dump({"caption": {"hashtag_limit": 2}}, fh)
        r = helpers.run_cli("ig-caption", "caption.py", "-",
                            stdin="A caption with three tags.\n#one #two #three\n")
        self.assertIn("cap of 2", r.stdout)

    def test_stale(self):
        far = datetime.date(2030, 1, 1)
        names = [n for n, *_ in igcore.stale_blocks(30, today=far)]
        self.assertIn("caption", names)
        self.assertEqual(igcore.stale_blocks(10 ** 6), [])


class Credentials(unittest.TestCase):
    def tearDown(self):
        helpers.reset_home()
        os.environ.pop("IG_WRITE", None)

    def test_reads_credentials_file(self):
        path = os.path.join(helpers.HOME, "credentials.env")
        with open(path, "w") as fh:
            fh.write("# mine\nIG_ACCESS_TOKEN='abc'\nIG_USER_ID=42\n")
        os.chmod(path, 0o600)
        c = igcore.credentials()
        self.assertEqual(c["IG_ACCESS_TOKEN"], "abc")
        self.assertEqual(c["IG_USER_ID"], "42")

    def test_no_token_is_a_setup_error(self):
        with self.assertRaises(igcore.SetupError):
            igcore.client_from_env()

    def test_write_gate(self):
        with self.assertRaises(igcore.WriteGateError):
            igcore.require_write_enabled("Publishing")
        os.environ["IG_WRITE"] = "1"
        igcore.require_write_enabled("Publishing")


class Client(unittest.TestCase):
    def test_get_sends_bearer_and_version(self):
        g, t = helpers.graph([("GET", "/me", {"user_id": "1", "username": "x"})], user_id=None)
        self.assertEqual(g.user_id, "1")
        call = t.calls[0]
        self.assertIn("/v26.0/me", call["url"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer TEST_TOKEN")

    def test_retries_on_rate_limit_then_succeeds(self):
        state = {"n": 0}

        def flaky(*_):
            state["n"] += 1
            return (400, {"error": {"code": 4, "message": "slow down"}}) if state["n"] < 3 else {"ok": 1}

        g, t = helpers.graph([("GET", "/thing", flaky)])
        self.assertEqual(g.get("thing"), {"ok": 1})
        self.assertEqual(len(t.calls), 3)

    def test_error_is_parsed(self):
        g, _ = helpers.graph([("GET", "/bad", (400, {"error": {"code": 100, "error_subcode": 33,
                                                                "message": "Invalid"}}))])
        with self.assertRaises(igcore.GraphError) as cm:
            g.get("bad")
        self.assertEqual((cm.exception.code, cm.exception.subcode), (100, 33))

    def test_paginate_follows_next_and_caps(self):
        g, t = helpers.graph([
            ("GET", "page=2", {"data": [{"id": 3}, {"id": 4}]}),
            ("GET", "/list", {"data": [{"id": 1}, {"id": 2}],
                              "paging": {"next": "https://graph.instagram.com/v26.0/list?page=2"}}),
        ])
        self.assertEqual([x["id"] for x in g.paginate("list")], [1, 2, 3, 4])
        self.assertEqual([x["id"] for x in g.paginate("list", max_items=3)], [1, 2, 3])

    def test_post_json_body(self):
        g, t = helpers.graph([("POST", "/messages", {"message_id": "m1"})])
        g.post_json("1/messages", {"recipient": {"comment_id": "c"}})
        self.assertEqual(json.loads(t.calls[0]["body"]), {"recipient": {"comment_id": "c"}})
        self.assertEqual(t.calls[0]["headers"]["Content-Type"], "application/json")


if __name__ == "__main__":
    unittest.main()
