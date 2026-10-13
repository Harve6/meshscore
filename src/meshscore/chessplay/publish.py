"""Publishing the game for the web board (option A: commit ``state.json`` to a GitHub Pages repo).

``site_dir`` is a local clone of the Pages repo. ``SitePublisher.tick(state)`` writes ``state.json`` there and, at
most once per ``min_interval`` seconds and only when something changed, commits and pushes it. Every failure is
logged and retried later - publishing must never get in the way of the game.
"""
from __future__ import annotations

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
    def __init__(self, site_dir: str = "", min_interval: float = 60.0, push: bool = True, clock=time.time, run=_run):
        self.site_dir = os.path.abspath(site_dir) if site_dir else ""
        self.min_interval, self.push, self.clock, self.run = float(min_interval), push, clock, run
        self.last_push = 0.0
        self.dirty = False
        self.last_text = ""
        self.failures = 0
        self.pushed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.site_dir)

    def _stable(self, state: dict) -> str:
        """The JSON text, minus fields that change every call (so an unchanged game never makes a commit)."""
        s = {k: v for k, v in state.items() if k != "updated"}
        return json.dumps(s, sort_keys=True, ensure_ascii=False)

    def tick(self, state: dict) -> bool:
        """Write the state; push if it changed and the interval allows. Returns True when a push happened."""
        if not self.enabled:
            return False
        try:
            stable = self._stable(state)
            if stable != self.last_text:
                self._write(state)
                self.last_text, self.dirty = stable, True
            if self.dirty and self.clock() - self.last_push >= self.min_interval * (2 ** min(self.failures, 4)):
                return self._publish()
        except Exception as e:                                   # never let publishing take the game down
            log.warning("Chess site publish error: %s", e)
        return False

    def _write(self, state: dict):
        os.makedirs(self.site_dir, exist_ok=True)
        for name in ("index.html", ".nojekyll"):                 # first run: drop the page itself in
            dst = os.path.join(self.site_dir, name)
            src = os.path.join(TEMPLATE_DIR, name)
            if not os.path.exists(dst) and os.path.exists(src):
                shutil.copyfile(src, dst)
        tmp = os.path.join(self.site_dir, STATE_FILE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, os.path.join(self.site_dir, STATE_FILE))

    def _publish(self) -> bool:
        self.last_push = self.clock()
        if not self.push or not os.path.isdir(os.path.join(self.site_dir, ".git")):
            self.dirty = False              # not a git clone (or pushing is off): the file on disk is the result
            return False
        r = self.run(["git", "add", "-A"], self.site_dir)
        r = self.run(["git", "commit", "-m", "chess: update board", "--quiet"], self.site_dir)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            return self._fail("commit", r)
        r = self.run(["git", "push", "--quiet"], self.site_dir)
        if r.returncode != 0:                                    # someone edited the site elsewhere: rebase and retry once
            self.run(["git", "pull", "--rebase", "--autostash", "--quiet"], self.site_dir)
            r = self.run(["git", "push", "--quiet"], self.site_dir)
        if r.returncode != 0:
            return self._fail("push", r)
        self.dirty, self.failures = False, 0
        self.pushed += 1
        log.info("Chess board published")
        return True

    def _fail(self, what, r) -> bool:
        self.failures += 1
        log.warning("Chess site git %s failed (retrying later): %s", what, (r.stderr or r.stdout).strip()[:200])
        return False
