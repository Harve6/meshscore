"""Crowd-plays-Pokemon-Red on a MeshCore channel.

Everyone on the channel sends buttons (`!up`, `!a`, `!start` ...). The first vote of a round starts a clock (default 30
seconds); when it ends the most popular button is pressed in a headless Game Boy emulator (PyBoy), the game is saved,
and a screenshot goes to the web page. The channel only hears about things that matter - a new area, a battle, a level
up, a badge - read straight out of the game's memory. You supply your own ROM. See docs/pokemon.md.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import os
import time

from ..chessplay.cards import CardsMixin
from ..chessplay.publish import SitePublisher
from ..core import fit, is_admin, pack
from ..plugin import LeaguePlugin
from ..pokeplay import game as pg
from ..pokeplay import ram
from ..pokeplay.emulator import BUTTONS, DIRECTIONS, FakeGameBoy, GameBoy, norm_button

log = logging.getLogger("meshscore")

SITE_TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pokeplay", "site")
RECENT = 12
EVENTS = 10
MAX_EVENT_POSTS = 3


class PokemonModule(CardsMixin, LeaguePlugin):
    label = "Pokemon Red"
    kinds = {"max": {"map", "battle", "levelup", "evolve", "badge", "caught", "faint", "round"},
             "min": {"badge", "caught", "evolve"}, "off": set()}
    min_blurb = "badges, new Pokemon and evolutions only"
    bare_command = "!where"
    no_cooldown_cmds = tuple("!" + b for b in BUTTONS) + ("!u", "!d", "!l", "!r", "!sel", "!press", "!p")

    def __init__(self, hub, name):
        super().__init__(hub, name)
        self.clock = time.time
        c = self.cfg
        self.blurb = "crowd-plays Pokemon Red"
        self.window = max(5.0, float(c.get("window_seconds", 30)))
        self.steps = max(1, min(pg.MAX_STEPS, int(c.get("steps", 3))))
        self.post_every = int(c.get("post_every", 0))
        self.ack = str(c.get("ack", "dm")).lower()
        self.site_url = str(c.get("site_url", "") or "").strip()
        self.pub = SitePublisher(str(c.get("site_dir", "") or ""), float(c.get("publish_min_seconds", 20)),
                                 push=bool(c.get("site_push", True)), sync_page=bool(c.get("site_sync", True)),
                                 template_dir=SITE_TEMPLATE, squash=True, label="pokemon")
        self._lock = asyncio.Lock()
        self.png = b""
        self.problem = ""
        self._cards_init()
        d = self.state.data
        d.setdefault("rounds", 0)
        d.setdefault("round_state", pg.new_round())
        d.setdefault("last_status", None)
        d.setdefault("recent", [])
        d.setdefault("events", [])
        d.setdefault("players", {})
        self._anon = 0
        state_path = str(c.get("state_file", "") or f"{name}.state")
        self.gb = self._open(c, state_path)
        self.status = None
        if self.gb:
            self.status = ram.read_status(self.gb, self.gb.species_names)
            self.png = self.gb.png()
            if not d["last_status"] or not self.gb.resumed:
                d["last_status"] = self.status.to_dict()       # baseline: nothing that already happened is news
            self.state.save()
        rs = d["round_state"]
        if rs.get("window_end") and rs["window_end"] < self.clock():
            rs["window_end"] = self.clock() + self.window         # the clock ran out while we were down: restart it, silently
            self.state.save()

    # ---------------------------------------------------------------- the emulator
    def _open(self, c, state_path):
        rom = str(c.get("rom", "") or "").strip()
        if str(c.get("emulator", "")).lower() == "fake":        # tests
            return FakeGameBoy(state_path=state_path)
        if not rom:
            self.problem = 'no ROM configured: set rom = "C:/path/to/Pokemon Red.gb" in [bots.%s]' % self.name
        else:
            try:
                return GameBoy(rom, state_path)
            except ImportError:
                self.problem = "PyBoy is not installed: pip install pyboy pillow"
            except Exception as e:
                self.problem = f"could not start the emulator: {e}"
        log.error("Pokemon bot %s is not running: %s", self.name, self.problem)
        return None

    def _play(self, button, steps):
        """Blocking (run in a thread): press, save, look."""
        self.gb.press(button, steps)
        self.gb.save()
        return ram.read_status(self.gb, self.gb.species_names), self.gb.png()

    # ---------------------------------------------------------------- helpers
    @property
    def rnd(self) -> dict:
        return self.state.data["round_state"]

    def _voter(self, inc) -> str:
        who = (inc.sender_name or "").strip() or (inc.sender_key or "")[:8]
        if not who:
            self._anon += 1
            who = f"anon{self._anon}"
        return who

    def _player(self, who):
        return self.state.data["players"].setdefault(who, {"presses": 0, "votes": 0})

    def _feedback(self, inc, text, important=False):
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

    def _event(self, text):
        ev = self.state.data["events"]
        ev.insert(0, {"t": int(self.clock()), "text": text})
        del ev[EVENTS:]

    # ---------------------------------------------------------------- the loop
    async def poll(self):
        now = self.clock()
        if not self.gb:
            return 300
        await self._refresh_cards(now)
        rs = self.rnd
        if rs.get("window_end") and now >= rs["window_end"]:
            await self._close_round()
        if self.pub.enabled:
            await asyncio.to_thread(self.pub.tick, self.site_state(), {"screen.png": self.png})
        self.mark_ok()
        we = self.rnd.get("window_end")
        return max(0.5, min(5.0, we - self.clock() + 0.2)) if we else 3.0

    async def _close_round(self):
        d = self.state.data
        rs = self.rnd
        win = pg.winner(rs)
        voters = dict(rs["votes"])
        d["round_state"] = pg.new_round()               # votes that arrive while the game runs start the next round
        if win is None:
            self.state.save()
            return
        (button, steps), n = win
        use = (steps or self.steps) if button in DIRECTIONS else 1
        async with self._lock:
            try:
                status, png = await asyncio.to_thread(self._play, button, use)
            except Exception:
                log.exception("pokemon round failed")
                self.state.save()
                return
        old = ram.Status.from_dict(d["last_status"]) if d.get("last_status") else None
        events = ram.diff_events(old, status)
        self.status, self.png = status, png
        d["last_status"] = status.to_dict()
        d["rounds"] += 1
        d["recent"].insert(0, {"n": d["rounds"], "action": pg.label((button, steps)), "votes": n, "voters": len(voters)})
        del d["recent"][RECENT:]
        for who, (b, s, _) in voters.items():
            p = self._player(who)
            p["votes"] += 1
            if (b, s) == (button, steps):
                p["presses"] += 1
        for kind, text in events:
            self._event(text)
        self.state.save()
        for kind, text in events[:MAX_EVENT_POSTS]:
            self.alert(kind, text)
        if self.post_every and d["rounds"] % self.post_every == 0:
            self.alert("round", fit(f"🎮 #{d['rounds']} {pg.label((button, steps))} ({n}) | {ram.summary(status)}"))

    # ---------------------------------------------------------------- commands
    def extra_commands(self):
        cmds = {"!where": self.cmd_where, "!status": self.cmd_where, "!party": self.cmd_party, "!votes": self.cmd_votes,
                "!top": self.cmd_top, "!reset": self.cmd_reset, "!press": self.cmd_press, "!p": self.cmd_press}
        for word in list(BUTTONS) + ["u", "d", "l", "r", "sel"]:
            cmds["!" + word] = self._button_cmd(word)
        return cmds

    def help_text(self):
        return "!up !down !left !right !a !b !start !select (+ a number: !up 5) | !where !party !votes !top"

    def status_extra(self):
        return f" | rounds {self.state.data.get('rounds', 0)}" + (f" | DOWN: {self.problem}" if not self.gb else "")

    def _button_cmd(self, word):
        async def cmd(inc, arg):
            return await self._vote(inc, word, arg)
        return cmd

    async def cmd_press(self, inc, arg):
        if not arg:
            return [fit(f"[{self.tag}] !press <up|down|left|right|a|b|start|select> [tiles]")]
        count = inc.rest[1] if len(inc.rest) > 1 else ""
        return await self._vote(inc, arg, count)

    async def _vote(self, inc, word, count_word):
        if not self.gb:
            return [fit(f"[{self.tag}] The game is not running ({self.problem})")]
        rs = self.rnd
        count = count_word if count_word else (inc.rest[0] if inc.rest else "")
        a = pg.vote(rs, self._voter(inc), word, count)
        if a is None:
            return []
        if not rs.get("window_end"):
            rs["window_end"] = self.clock() + self.window
        self.state.save()
        for key, nvotes in pg.tally(rs):
            if key == a:
                break
        left = max(0, int(rs["window_end"] - self.clock()))
        self._feedback(inc, f"✓ {pg.label(a)}: {nvotes} of {len(rs['votes'])} votes | {left}s left")
        return []

    async def cmd_where(self, inc, arg):
        st = self.status
        if not self.gb or st is None:
            return [fit(f"[{self.tag}] The game is not running ({self.problem})")]
        url = f" | {self.site_url}" if self.site_url else ""
        return [fit(f"[{self.tag}] {ram.summary(st)} | round {self.state.data['rounds']}{url}")]

    async def cmd_party(self, inc, arg):
        st = self.status
        if not st or not st.party:
            return [f"[{self.tag}] No Pokemon yet"]
        items = [f"{m.name} L{m.level} {m.hp}/{m.max_hp}" for m in st.party]
        return pack(f"[{self.tag}]", items)

    async def cmd_votes(self, inc, arg):
        rs = self.rnd
        t = pg.tally(rs)
        if not t:
            return [fit(f"[{self.tag}] No votes yet - the first one starts a {int(self.window)}s clock")]
        left = max(0, int((rs.get("window_end") or self.clock()) - self.clock()))
        votes = ", ".join(f"{pg.label(k)} {n}" for k, n in t[:5])
        return [fit(f"[{self.tag}] votes: {votes} | {left}s left")]

    async def cmd_top(self, inc, arg):
        pl = self.state.data["players"]
        top = sorted(pl.items(), key=lambda kv: (-kv[1]["presses"], -kv[1]["votes"], kv[0]))[:4]
        if not top:
            return [f"[{self.tag}] Nobody has pressed anything yet"]
        return [fit(f"[{self.tag}] rounds {self.state.data['rounds']} | " + ", ".join(f"{n} {p['presses']}" for n, p in top))]

    async def cmd_reset(self, inc, arg):
        if not is_admin(inc):
            return [f"[{self.tag}] Only admins can reset the game."]
        if arg.lower() != "confirm":
            return [f"[{self.tag}] This restarts the game from the title screen. Send: !reset confirm"]
        path = getattr(self.gb, "state_path", "") if self.gb else ""
        try:
            if self.gb:
                self.gb.close()
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError as e:
            return [f"[{self.tag}] Could not reset: {e}"]
        self.gb = self._open(self.cfg, path)
        d = self.state.data
        d.update(rounds=0, round_state=pg.new_round(), recent=[], events=[], last_status=None)
        if self.gb:
            self.status = ram.read_status(self.gb, self.gb.species_names)
            self.png = self.gb.png()
            d["last_status"] = self.status.to_dict()
        self.state.save()
        return [fit(f"[{self.tag}] New game! Press !start")]

    # ---------------------------------------------------------------- the web page
    def site_state(self) -> dict:
        d = self.state.data
        rs = self.rnd
        top = sorted(d["players"].items(), key=lambda kv: (-kv[1]["presses"], -kv[1]["votes"], kv[0]))[:10]
        return {
            "version": 1,
            "updated": int(self.clock()),
            "round": d["rounds"],
            "window_seconds": int(self.window),
            "window_end": int(rs["window_end"]) if rs.get("window_end") else None,
            "votes": [{"action": pg.label(k), "n": n} for k, n in pg.tally(rs)],
            "status": self.status.to_dict() if self.status else None,
            "summary": ram.summary(self.status) if self.status else "",
            "recent": d["recent"],
            "events": d["events"],
            "players": [{"name": n, **p} for n, p in top],
            "default_steps": self.steps,
            **self._cards_for_site(),
        }
