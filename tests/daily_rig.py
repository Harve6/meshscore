"""A small harness for the daily bots: a hub with one bot on a recording radio, a pinned clock, DMs and channel posts."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from meshscore import core
from meshscore.cli import build_hub
from meshscore.core import CFG
from meshscore.radio import Incoming, Transport

ZONE = ZoneInfo("America/Toronto")


class Radio(Transport):
    name = "daily-test"
    alive = True

    def __init__(self, contacts=None):
        self.sent, self.dms, self.contacts = [], [], contacts or {}

    async def send_channel(self, channel, text):
        self.sent.append(text)
        return True

    async def send_dm(self, key, text):
        self.dms.append((key, text))
        return True

    def find_contact(self, name=None, key=None):
        for n, k in self.contacts.items():
            if (name and n.lower() == name.lower()) or (key and k.startswith(key[:12])):
                return {"adv_name": n, "public_key": k}
        return None


def set_time(monkeypatch, y=2026, mo=10, d=14, h=9, mi=0):
    """Pin the wall clock the bots use for 'what hour is it / which day'."""
    monkeypatch.setattr(core, "CLOCK", lambda: datetime(y, mo, d, h, mi, tzinfo=ZONE))


class DRig:
    def __init__(self, typ, channel="#daily", name=None, cooldown=0, **cfg):
        name = name or typ
        CFG["command_cooldown"] = cooldown
        CFG["modules"] = {name: {"enabled": True, "type": typ, "channel": channel, **cfg}}
        self.t = Radio()
        self.hub = build_hub(self.t)
        self.m = self.hub.modules[name]
        self.channel = channel.lstrip("#")
        self.now = [1_800_000_000.0]
        self.m.clock = lambda: self.now[0]
        self.tasks = []

    async def __aenter__(self):
        self.tasks = [asyncio.create_task(self.hub.tx_worker()), asyncio.create_task(self.hub.cmd_worker())]
        return self

    async def __aexit__(self, *a):
        for t in self.tasks:
            t.cancel()

    async def settle(self):
        for _ in range(200):
            await asyncio.sleep(0.005)
            if self.hub.cmdq.empty() and self.hub.out.empty():
                await asyncio.sleep(0.02)
                if self.hub.cmdq.empty() and self.hub.out.empty():
                    return

    async def say(self, text, name="Ann", dm=False, hops=1):
        key = ("k" + name.lower() * 8)[:32] if dm else ""
        await self.hub.on_message(Incoming(text, dm, name, key, hops, 5.0, "" if dm else self.channel))
        await self.settle()

    async def tick(self, advance=0):
        self.now[0] += advance
        r = await self.m.poll()
        await self.settle()
        return r

    def take(self):
        out, self.t.sent = self.t.sent, []
        return out

    def last_dm(self):
        return self.t.dms[-1][1] if self.t.dms else ""


def run(coro):
    return asyncio.run(coro)
