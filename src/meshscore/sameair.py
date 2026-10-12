"""Several MeshScore bots on the same air - without any extra airtime.

Two bots following the same game would both post every goal. Instead of a coordination protocol (which would cost
airtime of its own), each bot simply *listens*:

* every channel message it hears is remembered for a few minutes;
* right before transmitting an alert it checks whether an equivalent post (same kind of event, same teams, same
  score) has already been heard from somebody else - if so it drops its own copy;
* when other bots are known on the channel, the bots form a line-up: the bot at position 0 posts straight away, the one
  at position 1 waits one *slot* (a few seconds - longer than a message takes to cross the mesh), position 2 waits two,
  and so on. Positions come from sorting the bots' node names, so every bot computes the same order without talking
  (override with ``rank``). The bot in front is heard before the others' turn comes and they stay silent; if it is
  down, the next in line simply posts after its slot - a standby with no configuration.

With a single bot on the air nothing is delayed: the line-up only applies once a peer has been heard (or declared with
``peers``).
"""
from __future__ import annotations

import json
import random
import re
import time
from collections import deque
from dataclasses import dataclass

from .core import CFG, SELF_MARKERS, log

REPLY_WINDOW = 45          # seconds: an identical command reply this recent counts as already answered
PEER_MEMORY = 7 * 86400    # a peer heard within a week is still a peer
_SCORE = re.compile(r"\b([A-Z]{2,5})\s+(\d{1,3})-(\d{1,3})\s+([A-Z]{2,5})\b")          # KIT 0-5 GUE
_FINAL = re.compile(r"\b([A-Z]{2,5})\s+(\d{1,3})\s*@\s*([A-Z]{2,5})\s+(\d{1,3})\b")    # FINAL: KIT 0 @ GUE 6
_BOT_PREFIX = re.compile(r"^\[[A-Z0-9][A-Z0-9 ]{1,15}\]")
_START = re.compile(r"puck drop|kickoff|play ball|tip-?off|first pitch", re.I)


def chan_key(name):
    return str(name).strip().lstrip("#").lower()


def kind_of(text):
    """Coarse event kind of a post: 'final', 'start', or its leading symbol (goal, intermission ...)."""
    t = text.strip()
    if re.search(r"\bFINAL\b", t):
        return "final"
    if _START.search(t):
        return "start"
    m = re.match(r"^([^\w\s\[]+)", t)
    return (m.group(1).replace("️", "") if m else "") or "text"


def score_key(text):
    """(away, home, away goals, home goals) found in a post, else None."""
    m = _SCORE.search(text)
    if m:
        return (m.group(1), m.group(4), int(m.group(2)), int(m.group(3)))
    m = _FINAL.search(text)
    if m:
        return (m.group(1), m.group(3), int(m.group(2)), int(m.group(4)))
    return None


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def letters(text):
    return re.sub(r"[^a-z]+", "", text.lower())


def looks_like_bot(text):
    t = text.strip()
    return bool(_BOT_PREFIX.match(t)) or t.startswith(SELF_MARKERS)


@dataclass
class Heard:
    ts: float
    channel: str
    sender: str
    kind: str
    key: tuple | None
    norm: str
    letters: str


class SameAir:
    def __init__(self, cfg=None, rng=None, clock=time.time, path=None):
        cfg = cfg if cfg is not None else CFG.get("same_air", {})
        self.enabled = bool(cfg.get("enabled", True))
        self.window = float(cfg.get("window_seconds", 180))
        self.slot = float(cfg.get("slot_seconds", 5))
        self.jitter = float(cfg.get("jitter_seconds", 1.5))
        self.rank_override = int(cfg.get("rank", -1))
        self.declared = {str(n) for n in (cfg.get("peers") or [])}     # node names of other bots, known from the start
        self.rng, self.clock, self.path = rng or random.Random(), clock, path
        self.heard = deque(maxlen=300)
        self.peers: dict[str, float] = {}      # bot-like sender name -> last time heard
        self.skipped = 0
        self.own_name = ""
        self._load()

    # ---------------- listening ----------------
    def hear(self, channel, text, sender=""):
        """Remember a channel message another node posted."""
        if not self.enabled or not text or (self.own_name and sender == self.own_name):
            return
        ts = self.clock()
        self.heard.append(Heard(ts, chan_key(channel), sender, kind_of(text), score_key(text), norm(text), letters(text)))
        if sender and looks_like_bot(text):
            fresh = sender not in self.peers
            self.peers[sender] = ts
            if fresh:
                log.info("Another bot is on the air: %s - same-air dedupe is active", sender)
            self._save()

    def peers_active(self):
        cutoff = self.clock() - PEER_MEMORY
        return bool(self.declared) or any(t >= cutoff for t in self.peers.values())

    # ---------------- sending ----------------
    def rank(self):
        """This bot's place in the line-up (0 = posts first): by sorted node name unless `rank` is configured."""
        if self.rank_override >= 0:
            return self.rank_override
        names = sorted(set(self.peers) | self.declared | ({self.own_name} if self.own_name else set()))
        return names.index(self.own_name) if self.own_name in names else 0

    def delay(self, is_reply=False):
        """Seconds to wait before transmitting: its slot in the line-up plus a small random tie-break, or 0 when no
        other bot is known."""
        if not self.enabled or not self.peers_active():
            return 0.0
        return self.rank() * self.slot + self.rng.uniform(0, self.jitter)

    def is_duplicate(self, channel, text, is_reply=False):
        """True when somebody else already posted the equivalent of `text` on this channel recently."""
        if not self.enabled:
            return False
        cutoff = self.clock() - (REPLY_WINDOW if is_reply else self.window)
        ch, key, kind = chan_key(channel), score_key(text), kind_of(text)
        n, lt = norm(text), letters(text)
        for h in reversed(self.heard):
            if h.ts < cutoff:
                break
            if h.channel != ch:
                continue
            if is_reply:
                if lt and h.letters == lt:
                    return True
            elif key is not None:
                if h.key == key and h.kind == kind:
                    return True
            elif h.norm == n:
                return True
        return False

    # ---------------- persistence (peers only) ----------------
    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                self.peers = {str(k): float(v) for k, v in json.load(f).get("peers", {}).items()}
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self):
        if not self.path:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"peers": self.peers}, f)
        except OSError:
            pass


class AirBudget:
    """Optional cap on alert messages per hour. Finals and game starts always go out; goals/runs are dropped only at
    the cap; period/inning results go first (at 80% of it)."""

    def __init__(self, cfg=None, clock=time.time):
        cfg = cfg if cfg is not None else CFG.get("airtime", {})
        self.limit = int(cfg.get("max_per_hour", 0) or 0)
        self.clock = clock
        self.sent = deque()
        self.dropped = 0

    def _prune(self):
        cutoff = self.clock() - 3600
        while self.sent and self.sent[0] < cutoff:
            self.sent.popleft()

    @staticmethod
    def tier(text):
        k = kind_of(text)
        if k in ("final", "start"):
            return 0
        if k in ("🚨", "⚾", "🏈", "💥", "⚠"):
            return 1
        return 2

    def allow(self, text):
        if self.limit <= 0:
            return True
        self._prune()
        used, tier = len(self.sent), self.tier(text)
        ok = tier == 0 or (tier == 1 and used < self.limit) or (tier == 2 and used < self.limit * 0.8)
        if not ok:
            self.dropped += 1
        return ok

    def record(self):
        self.sent.append(self.clock())

    def per_hour(self):
        self._prune()
        return len(self.sent)
