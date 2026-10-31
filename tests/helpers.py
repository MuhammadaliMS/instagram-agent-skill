"""Shared setup for the tests: a throwaway IG_HOME (so nothing reads or
writes the real ~/.claude/instagram), every skill folder on the path, and a
fake Graph transport that records calls instead of touching the network."""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, "skills")
HOME = tempfile.mkdtemp(prefix="ig-test-home-")
os.environ["IG_HOME"] = HOME
os.environ.pop("IG_WRITE", None)
os.environ.pop("IG_ACCESS_TOKEN", None)
os.environ["IG_HOOK_WEIGHTS"] = "off"

for name in sorted(os.listdir(SKILLS)):
    path = os.path.join(SKILLS, name)
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)


def script(skill, name):
    return os.path.join(SKILLS, skill, name)


def run_cli(skill, name, *args, stdin=None, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, script(skill, name), *args], input=stdin,
                          capture_output=True, text=True, env=e)


def reset_home():
    for entry in os.listdir(HOME):
        p = os.path.join(HOME, entry)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)


class FakeTransport:
    """routes: list of (method, substring, response) checked in order. A
    response is a dict (200 JSON), a (status, dict) tuple, or a callable
    taking (method, url, headers, body). Every call is recorded."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body):
        if hasattr(body, "read"):
            body = body.read()
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        for m, needle, resp in self.routes:
            if m == method and needle in url:
                if callable(resp):
                    resp = resp(method, url, headers, body)
                status, payload = resp if isinstance(resp, tuple) else (200, resp)
                return status, json.dumps(payload).encode()
        return 404, json.dumps({"error": {"message": f"no fake route for {method} {url}",
                                          "code": 803}}).encode()

    def find(self, method, needle):
        return [c for c in self.calls if c["method"] == method and needle in c["url"]]


def graph(routes, user_id="17841400000000000"):
    from igcore import Graph
    t = FakeTransport(routes)
    return Graph("TEST_TOKEN", user_id=user_id, transport=t, sleep=lambda s: None), t


def have(tool):
    return shutil.which(tool) is not None
