"""Base class for writing your own league plugin in ~40 lines.

    from meshscore.plugin import LeaguePlugin

    class MyLeague(LeaguePlugin):
        label = "My League"
        kinds = {"max": {"score", "final"}, "min": {"final"}, "off": set()}   # alert kinds sent at each !mode level
        min_blurb = "finals only"

        async def poll(self):
            ...fetch your data, then:
            self.alert("score", "🏀 HOME 3-2 AWAY | Q2")
            return 30            # seconds until the next poll

        def extra_commands(self):
            return {"!score": self.cmd_score}

        async def cmd_score(self, inc, arg):
            return [f"[{self.tag}] HOME 3-2 AWAY"]

    LEAGUES = {"myleague": MyLeague}            # <- how meshscore finds it (see docs/writing-a-plugin.md)

Everything else - the bot's name/tag/channel, `!mode`, `!ping`, `!botstatus`, `!help`, per-bot state files, the error
handling around polling, message fitting, same-air dedupe and the airtime budget - is provided.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .core import CFG, HOCKEY_KINDS, ModeMixin, State, fit, pack  # noqa: F401  (re-exported for plugin authors)

log = logging.getLogger("meshscore")


class LeaguePlugin(ModeMixin):
    """Subclass this. Override `poll()` (required) and optionally `extra_commands()`."""

    label = "League"                      # shown in `meshscore check`
    kinds = HOCKEY_KINDS                  # {"max": {...}, "min": {...}, "off": set()}: which alert kinds each !mode sends
    min_blurb = "results only"            # what "min" means, shown by !mode
    arg_cmds: tuple = ()                  # commands whose argument matters for the per-command cooldown (e.g. "!team")
    no_cooldown_cmds: tuple = ()          # commands exempt from the shared per-channel cooldown (a vote: every voter counts)
    aliases: tuple = ()                   # extra command prefixes that route here ("!ps score")
    bare_command = "!help"                # what "!name" on its own answers with (the market bot: "!indices")

    def __init__(self, hub, name):
        self.hub, self.name = hub, name
        self.cfg = CFG["modules"][name]
        self.channel = self.cfg["channel"]
        self.tag = str(self.cfg.get("tag") or name).upper()
        self.blurb = f"{self.cfg.get('label') or self.label} scores"
        self.state = State(f"{name}_state.json")
        self.ok_at = 0.0                  # last successful data fetch (shown by !botstatus)
        self.commands = {"!ping": self._ping, "!botstatus": self._status, "!help": self._help, "!mode": self.cmd_mode,
                         **self.extra_commands()}

    # ---------------- what a plugin overrides ----------------
    async def poll(self) -> float:
        """Fetch data, announce changes with self.alert(), return seconds until the next poll."""
        raise NotImplementedError

    def extra_commands(self) -> dict:
        """{"!score": async fn(inc, arg) -> [reply texts]}"""
        return {}

    def help_text(self) -> str:
        return " ".join(c for c in self.commands if c not in ("!ping", "!botstatus", "!help"))

    def status_extra(self) -> str:
        return ""

    # ---------------- helpers ----------------
    def alert(self, kind, texts):
        """Post to the channel if this bot's !mode includes `kind`. `texts` is a string or a list of strings."""
        if kind in self.kinds.get(self.level(), set()):
            self.hub.say_chan(self.channel, texts)

    def mark_ok(self):
        self.ok_at = time.time()

    # ---------------- built-in commands ----------------
    async def _ping(self, inc, arg):
        return await self.hub.cmd_ping(self, inc)

    async def _status(self, inc, arg):
        return await self.hub.status(self, self.ok_at)

    async def _help(self, inc, arg):
        return [fit(f"[{self.tag} BOT] {self.help_text()} !mode !ping")]

    # ---------------- loop ----------------
    async def run(self):
        while True:
            delay = CFG["poll_idle_seconds"]
            try:
                delay = await self.poll() or delay
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s poll failed", self.name)
            await asyncio.sleep(delay)
