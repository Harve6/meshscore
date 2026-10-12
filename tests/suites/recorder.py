"""Record real league-API responses once, replay them offline forever.

Every league fetcher goes through `meshscore.core.session.get`, so one wrapper there is enough.

MESHSCORE_FIXTURES=
    live     (default when no fixture file exists) - talk to the real APIs, as before
    record   - talk to the real APIs AND save every response into tests/fixtures/<suite>.json.gz
    replay   - serve responses from that file; anything not recorded is an error (printed as FIXTURE MISS), and the
               bot's "now" is frozen at the moment of recording so date-dependent requests ("today +/- 8 days") match.

`replay` is the default when the fixture exists, so the suites run offline, fast and deterministically in CI.
Re-record with `MESHSCORE_FIXTURES=record python tests/suites/suite_nfl.py` (or `pytest -m record`) when a league
changes its feed, then commit the new file.
"""
import atexit
import gzip
import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures")


def _key(method, url, params):
    q = ""
    if params:
        items = params.items() if hasattr(params, "items") else params
        q = ("&" if "?" in url else "?") + urlencode(sorted((str(k), str(v)) for k, v in items))
    return f"{method} {url}{q}"


def fixture_path(suite):
    return os.path.abspath(os.path.join(FIXTURE_DIR, f"{suite}.json.gz"))


def mode_for(suite):
    explicit = os.environ.get("MESHSCORE_FIXTURES", "").lower()
    if explicit in ("live", "record", "replay"):
        return explicit
    return "replay" if os.path.exists(fixture_path(suite)) else "live"


class Recorder:
    def __init__(self, suite, session, core):
        self.suite, self.session, self.core = suite, session, core
        self.mode = mode_for(suite)
        self.path = fixture_path(suite)
        self.entries = {}
        self.misses = []
        self.recorded_at = None
        self._real_get = session.get
        if self.mode == "replay":
            with gzip.open(self.path, "rt", encoding="utf-8") as f:
                blob = json.load(f)
            self.entries, self.recorded_at = blob["entries"], blob["recorded_at"]
            frozen = datetime.fromisoformat(self.recorded_at)
            core.CLOCK = lambda: frozen
            session.get = self._replay_get
        elif self.mode == "record":
            self.recorded_at = datetime.now(timezone.utc).isoformat()    # a re-record always starts clean
            session.get = self._record_get
            atexit.register(self.save)

    # ------------------------------------------------------------ record
    def _record_get(self, url, params=None, **kw):
        res = self._real_get(url, params=params, **kw)
        try:
            text = res.content.decode("utf-8", errors="replace")
            self.entries[_key("GET", url, params)] = {"status": res.status_code, "text": text}
        except Exception:                                  # pragma: no cover
            pass
        return res

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        stamp = self.recorded_at
        with gzip.open(self.path, "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump({"recorded_at": stamp, "entries": self.entries}, f, separators=(",", ":"), sort_keys=True)
        print(f"[recorder] saved {len(self.entries)} responses -> {self.path}", file=sys.stderr)

    # ------------------------------------------------------------ replay
    def _replay_get(self, url, params=None, **kw):
        key = _key("GET", url, params)
        hit = self.entries.get(key)
        if hit is None:
            self.misses.append(key)
            print(f"FIXTURE MISS: {key}", file=sys.stderr)
            raise requests.ConnectionError(f"not in fixture: {key}")
        r = requests.Response()
        r.status_code = hit["status"]
        r._content = hit["text"].encode("utf-8")
        r.encoding = "utf-8"
        r.url = key.split(" ", 1)[1]
        r.headers["Content-Type"] = "application/json"
        return r
