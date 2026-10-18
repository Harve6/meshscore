"""Publishing a bot's game for its web page (commit ``state.json`` [+ files] to a GitHub Pages repo).

``site_dir`` is a local clone of the Pages repo. ``SitePublisher.tick(state)`` writes ``state.json`` there and, at
most once per ``min_interval`` seconds and only when something changed, commits and pushes it. Every failure is
logged and retried later - publishing must never get in the way of the game.

Shared by the chess and Pokemon bots. ``squash=True`` (Pokemon: a new screenshot every round would otherwise bloat
the repo) amends the previous commit and force-pushes - but only ever a commit this publisher made itself.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time

log = logging.getLogger("meshscore")

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "site")
STATE_FILE = "state.json"


def _run(args, cwd, timeout=90):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


class SitePublisher:
    def __init__(self, site_dir: str = "", min_interval: float = 60.0, push: bool = True, clock=time.time, run=_run,
                 sync_page: bool = True, template_dir: str = TEMPLATE_DIR, squash: bool = False, label: str = "chess"):
        self.site_dir = os.path.abspath(site_dir) if site_dir else ""
        self.min_interval, self.push, self.clock, self.run = float(min_interval), push, clock, run
        self.sync_page = sync_page            # keep index.html in step with the version that ships with MeshScore
        self.template_dir, self.squash, self.label = template_dir, squash, label
        self.last_push = 0.0
        self.dirty = False
        self.last_text = ""
        self.failures = 0
        self.pushed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.site_dir)

    @property
    def message(self) -> str:
        return f"{self.label}: update board"

    def _stable(self, state: dict, files=None) -> str:
        """The JSON text (+ a digest of any extra files), minus fields that change every call, so an unchanged
        game never makes a commit."""
        s = {k: v for k, v in state.items() if k != "updated"}
        digest = {n: hashlib.sha1(b).hexdigest() for n, b in sorted((files or {}).items())}
        return json.dumps([s, digest], sort_keys=True, ensure_ascii=False)

    def tick(self, state: dict, files: dict | None = None) -> bool:
        """Write the state (and `files`: {name: bytes}); push if it changed and the interval allows."""
        if not self.enabled:
            return False
        try:
            stable = self._stable(state, files)
            if stable != self.last_text:
                self._write(state, files)
                self.last_text, self.dirty = stable, True
            if self.dirty and self.clock() - self.last_push >= self.min_interval * (2 ** min(self.failures, 4)):
                return self._publish()
        except Exception as e:                                   # never let publishing take the game down
            log.warning("%s site publish error: %s", self.label.capitalize(), e)
        return False

    def _write(self, state: dict, files=None):
        os.makedirs(self.site_dir, exist_ok=True)
        for name in ("index.html", ".nojekyll"):                 # the page itself: dropped in, and kept current
            dst = os.path.join(self.site_dir, name)
            src = os.path.join(self.template_dir, name)
            if not os.path.exists(src):
                continue
            if not os.path.exists(dst) or (self.sync_page and not self._same(src, dst)):
                shutil.copyfile(src, dst)
        for name, data in (files or {}).items():
            tmp = os.path.join(self.site_dir, name + ".tmp")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, os.path.join(self.site_dir, name))
        tmp = os.path.join(self.site_dir, STATE_FILE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, os.path.join(self.site_dir, STATE_FILE))

    @staticmethod
    def _same(a: str, b: str) -> bool:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()

    def _publish(self) -> bool:
        self.last_push = self.clock()
        if not self.push or not os.path.isdir(os.path.join(self.site_dir, ".git")):
            self.dirty = False              # not a git clone (or pushing is off): the file on disk is the result
            return False
        self.run(["git", "add", "-A"], self.site_dir)
        head = self.run(["git", "log", "-1", "--format=%s"], self.site_dir)
        amend = self.squash and head.returncode == 0 and head.stdout.strip() == self.message   # only our own commit
        if amend:
            r = self.run(["git", "commit", "--amend", "--no-edit", "--quiet"], self.site_dir)
        else:
            r = self.run(["git", "commit", "-m", self.message, "--quiet"], self.site_dir)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            return self._fail("commit", r)
        if amend:
            r = self.run(["git", "push", "--quiet", "--force-with-lease"], self.site_dir)
            if r.returncode != 0:
                self.run(["git", "fetch", "--quiet"], self.site_dir)
                r = self.run(["git", "push", "--quiet", "--force-with-lease"], self.site_dir)
        else:
            r = self.run(["git", "push", "--quiet"], self.site_dir)
            if r.returncode != 0:                                # someone edited the site elsewhere: rebase and retry once
                self.run(["git", "pull", "--rebase", "--autostash", "--quiet"], self.site_dir)
                r = self.run(["git", "push", "--quiet"], self.site_dir)
        if r.returncode != 0:
            return self._fail("push", r)
        self.dirty, self.failures = False, 0
        self.pushed += 1
        log.info("%s board published", self.label.capitalize())
        return True

    def _fail(self, what, r) -> bool:
        self.failures += 1
        log.warning("%s site git %s failed (retrying later): %s", self.label.capitalize(), what,
                    (r.stderr or r.stdout).strip()[:200])
        return False


class HttpPublisher:
    """Same job as SitePublisher, but the page lives on a small web service instead of a git repo: the files are
    PUT to ``<api>/api/files`` with a bearer token (see contrib/cloudflare-board). No git history, no deploy delay.
    Only files that changed since the last successful upload are sent."""

    def __init__(self, api: str = "", token: str = "", min_interval: float = 3.0, clock=time.time, session=None,
                 template_dir: str = TEMPLATE_DIR, label: str = "chess", timeout: float = 15.0, heartbeat: float = 600.0,
                 state_file: str = STATE_FILE):
        self.api = (api or "").rstrip("/")
        self.state_file = state_file             # several bots can share one board: word.json, trivia.json, ...
        self.heartbeat = float(heartbeat)       # re-send state.json this often even if nothing changed ("still alive")
        self.token, self.min_interval, self.clock = token or "", float(min_interval), clock
        self.template_dir, self.label, self.timeout = template_dir, label, timeout
        self._session = session
        self.sent: dict[str, str] = {}          # file name -> digest of what the service has
        self.last_push = 0.0
        self.last_ok = 0.0
        self.failures = 0
        self.pushed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api and self.token)

    @property
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def _put(self, payload):
        """PUT; one immediate retry on a fresh connection (an idle keep-alive connection is often reset by the far end)."""
        import requests
        headers = {"Authorization": "Bearer " + self.token}
        try:
            return self.session.put(self.api + "/api/files", json=payload, timeout=self.timeout, headers=headers)
        except requests.ConnectionError:
            try:
                self.session.close()
            except Exception:
                pass
            self._session = None
            return self.session.put(self.api + "/api/files", json=payload, timeout=self.timeout, headers=headers)

    def _page(self):
        try:
            with open(os.path.join(self.template_dir, "index.html"), "rb") as f:
                return f.read()
        except OSError:
            return None

    def tick(self, state: dict, files: dict | None = None) -> bool:
        """Upload what changed (at most once per min_interval, backing off after failures)."""
        if not self.enabled:
            return False
        try:
            body = dict(files or {})
            body[self.state_file] = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            page = self._page()
            if page is not None:
                body["index.html"] = page
            # `updated` changes every call; leave it out of the comparison so an idle game uploads nothing
            stable = {k: v for k, v in state.items() if k != "updated"}
            digests = {n: hashlib.sha1(b).hexdigest() for n, b in body.items()}
            digests[self.state_file] = hashlib.sha1(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            changed = {n: b for n, b in body.items() if self.sent.get(n) != digests[n]}
            now = self.clock()
            if not changed and self.heartbeat and self.last_ok and now - self.last_ok >= self.heartbeat:
                changed = {self.state_file: body[self.state_file]}     # nothing new, but let the page know we are alive
            if not changed or now - self.last_push < self.min_interval * (2 ** min(self.failures, 5)):
                return False
            self.last_push = now
            payload = {"files": {n: {"type": _mime(n), "b64": base64.b64encode(b).decode("ascii")} for n, b in changed.items()}}
            r = self._put(payload)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code} {r.text[:80]}")
            for n in changed:
                self.sent[n] = digests[n]
            self.failures, self.pushed, self.last_ok = 0, self.pushed + 1, now
            return True
        except Exception as e:                                   # never let publishing take the game down
            self.failures += 1
            log.warning("%s board upload failed (retrying later): %s", self.label.capitalize(), e)
            return False


def _mime(name: str) -> str:
    return {"html": "text/html; charset=utf-8", "json": "application/json", "png": "image/png", "svg": "image/svg+xml",
            "css": "text/css", "js": "text/javascript"}.get(name.rsplit(".", 1)[-1].lower(), "application/octet-stream")
