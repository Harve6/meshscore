"""What the daily bots (word, trivia, pickem, coverage, weather) have in common."""
from __future__ import annotations

import os
import time

from .. import core
from ..chessplay.cards import CardsMixin
from ..chessplay.publish import HttpPublisher
from ..core import DIGEST_GRACE_HOURS, fit
from ..plugin import LeaguePlugin

SITE_TEMPLATE = os.path.join(os.path.dirname(__file__), "site")


class DailyModule(CardsMixin, LeaguePlugin):
    """A bot that posts something once a day and takes DM answers. Subclasses set `json_name` (its file on the shared
    web board), `board_title`, and implement `poll()` / `site_state()` / commands."""

    json_name = "daily"
    board_title = "Mesh Daily"
    link_title = "Mesh Daily"          # what the other boards call the shared page in their "Also live" line

    def __init__(self, hub, name):
        super().__init__(hub, name)
        self.clock = time.time
        c = self.cfg
        self.ack = str(c.get("ack", "dm")).lower()
        self.site_url = str(c.get("site_url", "") or "").strip()
        self.pub = HttpPublisher(str(c.get("site_api", "") or ""), str(c.get("site_token", "") or ""),
                                 float(c.get("publish_min_seconds", 5)), template_dir=SITE_TEMPLATE, label=name,
                                 state_file=f"{self.json_name}.json")
        self._hints = {}
        self._anon = 0
        self._cards_init()

    # ---------------------------------------------------------------- people
    def _voter(self, inc) -> str:
        who = (inc.sender_name or "").strip() or (inc.sender_key or "")[:8]
        if not who:
            self._anon += 1
            who = f"anon{self._anon}"
        return who

    def _feedback(self, inc, text, important=False):
        """Answer a player privately: by DM when we can; on the channel only for things that must be seen."""
        text = fit(text)
        if inc.is_dm:
            if inc.sender_key:
                self.hub.say_dm(inc.sender_key, text)
            return
        c = self.hub.t.find_contact(name=inc.sender_name) if inc.sender_name else None
        if c and self.ack != "chan":
            self.hub.say_dm(c.get("public_key", ""), text)
        elif self.ack == "chan" or (important and self.ack != "off"):
            self.hub.say_chan(self.channel, text, reply=True)

    def _dm_only(self, inc, what: str, example: str) -> list:
        """Answers sent on the channel are not taken (everyone would see them): one gentle pointer per person per 10 min."""
        who = self._voter(inc)
        now = self.clock()
        if now - self._hints.get(who, 0) < 600:
            return []
        self._hints[who] = now
        return [fit(f"[{self.tag}] {who}: send {what} to me by DM ({example}) so nobody can copy it")]

    # ---------------------------------------------------------------- time
    @staticmethod
    def today() -> str:
        return core.now().date().isoformat()

    @staticmethod
    def yesterday() -> str:
        from datetime import timedelta
        return (core.now().date() - timedelta(days=1)).isoformat()

    @staticmethod
    def in_hour(hour: int) -> bool:
        """Is it the posting hour (or the grace hour after it)? Used so a restart later in the day posts nothing."""
        return hour <= core.now().hour < hour + DIGEST_GRACE_HOURS

    @staticmethod
    def past_hour(hour: int) -> bool:
        return core.now().hour >= hour

    # ---------------------------------------------------------------- the shared board
    async def publish(self):
        if self.pub.enabled:
            import asyncio
            await asyncio.to_thread(self.pub.tick, self.site_state(), None)

    def base_state(self) -> dict:
        return {"version": 1, "updated": int(self.clock()), "title": self.board_title, **self._cards_for_site()}

    def site_state(self) -> dict:                  # pragma: no cover - overridden
        return self.base_state()
