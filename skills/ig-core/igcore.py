#!/usr/bin/env python3
"""
igcore.py - the pieces every other skill shares: the platform numbers, the
folder your Instagram data lives in, and a small client for Meta's official
Instagram API.

Platform numbers live in platform.json next to this file. Nothing else in the
pack hard-codes a cap, a limit or a pixel. If Instagram moves one, change it
here once, or put an override in ~/.claude/instagram/platform.json so your
change survives an update of the pack.

The API client talks only to Meta's documented Graph endpoints with a token
you created yourself. It never logs in as you, never touches a browser, and
never prints the token.

Usage
  python3 igcore.py numbers             # every platform number, and when it was checked
  python3 igcore.py stale --days 120    # blocks nobody has re-checked in 120 days
  python3 igcore.py whoami              # is the API token working? (read-only)
"""

import argparse
import datetime
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SHIPPED = os.path.join(HERE, "platform.json")


def home():
    return os.path.expanduser(os.environ.get("IG_HOME", "~/.claude/instagram"))


def data_path(*parts):
    return os.path.join(home(), *parts)


# ---------------------------------------------------------------- numbers

def _merge(base, over):
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


_PLATFORM = None


def load_platform(refresh=False):
    """Shipped numbers, with the user's override file merged on top."""
    global _PLATFORM
    if _PLATFORM is not None and not refresh:
        return _PLATFORM
    with open(SHIPPED, encoding="utf-8") as fh:
        merged = json.load(fh)
    override = data_path("platform.json")
    if os.path.exists(override):
        with open(override, encoding="utf-8") as fh:
            merged = _merge(merged, json.load(fh))
    _PLATFORM = merged
    return merged


def number(path, default=None):
    """number("caption.hashtag_limit", 5) -> the live value, or the default."""
    node = load_platform()
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def stale_blocks(days, today=None):
    today = today or datetime.date.today()
    out = []
    for name, block in load_platform().items():
        if not isinstance(block, dict) or "checked" not in block:
            continue
        age = (today - datetime.date.fromisoformat(block["checked"])).days
        if age > days:
            out.append((name, block["checked"], age, block.get("source", "")))
    return out


# ---------------------------------------------------------------- credentials

def credentials():
    """IG_ACCESS_TOKEN / IG_USER_ID / IG_GRAPH_HOST from the environment, then
    from ~/.claude/instagram/credentials.env (KEY=VALUE lines).

    The token is yours, made in your own Meta app. Keep that file readable by
    you only: this warns if it is not.
    """
    found = {}
    path = data_path("credentials.env")
    if os.path.exists(path):
        mode = os.stat(path).st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            print(f"warning: {path} is readable by other users. Run: chmod 600 {path}",
                  file=sys.stderr)
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    found[key.strip()] = value.strip().strip("'\"")
    for key in ("IG_ACCESS_TOKEN", "IG_USER_ID", "IG_GRAPH_HOST", "IG_API_VERSION"):
        if os.environ.get(key):
            found[key] = os.environ[key]
    return found


class SetupError(Exception):
    pass


def client_from_env(transport=None):
    creds = credentials()
    token = creds.get("IG_ACCESS_TOKEN")
    if not token:
        raise SetupError(
            "No API token. Make one in your own Meta app (see ig-core/SKILL.md), then either\n"
            "  export IG_ACCESS_TOKEN=...   or put IG_ACCESS_TOKEN=... in "
            f"{data_path('credentials.env')} (chmod 600).")
    return Graph(token, host=creds.get("IG_GRAPH_HOST"), version=creds.get("IG_API_VERSION"),
                 user_id=creds.get("IG_USER_ID"), transport=transport)


# ---------------------------------------------------------------- the client

class GraphError(Exception):
    def __init__(self, status, payload):
        err = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
        self.status = status
        self.code = err.get("code")
        self.subcode = err.get("error_subcode")
        self.message = err.get("error_user_msg") or err.get("message") or f"HTTP {status}"
        super().__init__(f"{self.message} (HTTP {status}, code {self.code}"
                         + (f", subcode {self.subcode}" if self.subcode else "") + ")")


# Codes Meta documents as throttling. Back off and retry rather than fail.
RATE_LIMIT_CODES = {4, 17, 32, 613, 80001, 80002}


def urllib_transport(method, url, headers, body, timeout=120):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Graph:
    def __init__(self, token, host=None, version=None, user_id=None, transport=None,
                 sleep=time.sleep, retries=4):
        self.token = token
        self.host = host or number("api.host", "graph.instagram.com")
        self.version = version or number("api.version", "v26.0")
        self._user_id = user_id
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.retries = retries

    def url(self, path, params=None):
        if path.startswith("https://"):
            base = path
        else:
            base = f"https://{self.host}/{self.version}/{path.lstrip('/')}"
        if params:
            base += ("&" if "?" in base else "?") + urllib.parse.urlencode(params)
        return base

    def request(self, method, path, params=None, data=None, headers=None, raw=None):
        url = self.url(path, params)
        hdrs = {"Authorization": f"Bearer {self.token}"}
        hdrs.update(headers or {})
        body = raw
        if data is not None:
            body = urllib.parse.urlencode(
                {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                 for k, v in data.items() if v is not None}).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        for attempt in range(self.retries + 1):
            status, payload = self.transport(method, url, hdrs, body)
            try:
                parsed = json.loads(payload.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                parsed = {"raw": payload[:200].decode("utf-8", "replace")}
            if status < 400 and not (isinstance(parsed, dict) and "error" in parsed):
                return parsed
            err = GraphError(status, parsed)
            throttled = status == 429 or err.code in RATE_LIMIT_CODES or status >= 500
            if throttled and attempt < self.retries:
                self.sleep(min(60, 2 ** attempt * 3))
                continue
            raise err
        raise AssertionError("unreachable")

    def get(self, path, **params):
        return self.request("GET", path, params=params)

    def post(self, path, **data):
        return self.request("POST", path, data=data)

    def post_json(self, path, body, headers=None):
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        return self.request("POST", path, raw=json.dumps(body).encode(), headers=hdrs)

    def paginate(self, path, max_items=None, **params):
        """Yield every item across pages, following paging.next. (`limit` in
        params is the API's page size; max_items caps the total.)"""
        page = self.get(path, **params)
        count = 0
        while True:
            for item in page.get("data", []):
                yield item
                count += 1
                if max_items and count >= max_items:
                    return
            nxt = page.get("paging", {}).get("next")
            if not nxt:
                return
            page = self.request("GET", nxt)

    @property
    def user_id(self):
        """Your professional account id. Asked for once, then remembered."""
        if not self._user_id:
            me = self.get("me", fields="user_id,username")
            self._user_id = str(me.get("user_id") or me["id"])
        return self._user_id


# ---------------------------------------------------------------- write gate

class WriteGateError(Exception):
    pass


def require_write_enabled(action):
    """Anything that changes your account is off until you turn it on.

    Reading insights needs a token. Posting or messaging needs the token AND
    IG_WRITE=1 in the environment (or IG_WRITE=1 in credentials.env), so a
    skill can never post just because a token happens to be lying around.
    """
    if credentials().get("IG_WRITE") != "1" and os.environ.get("IG_WRITE") != "1":
        raise WriteGateError(
            f"{action} is switched off. It changes your live account, so it needs IG_WRITE=1 "
            f"set by you (export IG_WRITE=1, or add IG_WRITE=1 to {data_path('credentials.env')}).")


# ---------------------------------------------------------------- state files

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description="Shared platform numbers and API client.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("numbers", help="print every platform number")
    st = sub.add_parser("stale", help="blocks not re-checked in N days")
    st.add_argument("--days", type=int, default=120)
    sub.add_parser("whoami", help="check the API token (read-only)")
    args = ap.parse_args()

    if args.cmd == "numbers":
        print(json.dumps(load_platform(), indent=2, ensure_ascii=False))
    elif args.cmd == "stale":
        rows = stale_blocks(args.days)
        if not rows:
            print(f"Every block was checked in the last {args.days} days.")
        for name, checked, age, source in rows:
            print(f"  {name:<11} checked {checked} ({age} days ago)\n  {'':<11} {source}")
        sys.exit(1 if rows else 0)
    elif args.cmd == "whoami":
        try:
            g = client_from_env()
            me = g.get("me", fields="user_id,username,account_type,media_count")
        except (SetupError, GraphError) as e:
            print(e, file=sys.stderr)
            sys.exit(2)
        print(f"@{me.get('username')}  id {me.get('user_id') or me.get('id')}  "
              f"{me.get('account_type', '')}  {me.get('media_count', '?')} posts  "
              f"via {g.host}/{g.version}")


if __name__ == "__main__":
    main()
