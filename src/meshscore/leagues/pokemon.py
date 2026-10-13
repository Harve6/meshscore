"""Crowd-plays-Pokemon on a MeshCore channel (Red, Blue and Yellow).

Everyone on the channel sends buttons (`!up`, `!a`, `!start` ...). The first vote of a round starts a clock (default 30
seconds); when it ends the most popular button is pressed in a headless Game Boy emulator (PyBoy), the game is saved,
and a screenshot goes to the web page. The channel only hears about things that matter - a new area, a battle, a level
up, a badge - read straight out of the game's memory.

With more than one ROM configured (`[bots.pokemon.roms]`) each game keeps its own save and the channel votes once a
day for which one to play (`!game blue`). You supply your own ROMs. See docs/pokemon.md.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time

from .. import core
from ..chessplay.cards import CardsMixin
from ..chessplay.publish import HttpPublisher, SitePublisher
from ..core import admin_refusal, fit, is_strict_admin, pack
from ..plugin import LeaguePlugin
from ..pokeplay import game as pg
from ..pokeplay import ram
from ..pokeplay.emulator import BUTTONS, DIRECTIONS, FakeGameBoy, GameBoy

log = logging.getLogger("meshscore")

SITE_TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pokeplay", "site")
RECENT = 12
EVENTS = 10
MAX_EVENT_POSTS = 3
BALLOT_GRACE = 600          # a ballot that ended this long ago (bot was down) is settled quietly


class PokemonModule(CardsMixin, LeaguePlugin):
    label = "Pokemon"
    kinds = {"max": {"map", "battle", "levelup", "evolve", "badge", "caught", "faint", "round", "ballot"},
             "min": {"badge", "caught", "evolve", "ballot"}, "off": set()}
    min_blurb = "badges, new Pokemon, evolutions and the daily game vote only"
    bare_command = "!where"
    no_cooldown_cmds = tuple("!" + b for b in BUTTONS) + ("!u", "!d", "!l", "!r", "!sel", "!press", "!p", "!game")

    def __init__(self, hub, name):
        super().__init__(hub, name)
        self.clock = time.time
        c = self.cfg
        self.blurb = "crowd-plays Pokemon"
        self.window = max(5.0, float(c.get("window_seconds", 30)))
        self.steps = max(1, min(pg.MAX_STEPS, int(c.get("steps", 3))))
        self.post_every = int(c.get("post_every", 0))
        self.ack = str(c.get("ack", "dm")).lower()
        self.site_url = str(c.get("site_url", "") or "").strip()
        self.vote_hour = int(c.get("vote_hour", 9))
        self.ballot_seconds = max(60.0, float(c.get("ballot_minutes", 60)) * 60)
        if str(c.get("site_api", "") or "").strip():              # a Cloudflare Worker (contrib/cloudflare-board)
            self.pub = HttpPublisher(str(c["site_api"]), str(c.get("site_token", "") or ""),
                                     float(c.get("publish_min_seconds", 4)), template_dir=SITE_TEMPLATE, label="pokemon")
        else:                                                     # a GitHub Pages clone
            self.pub = SitePublisher(str(c.get("site_dir", "") or ""), float(c.get("publish_min_seconds", 20)),
                                     push=bool(c.get("site_push", True)), sync_page=bool(c.get("site_sync", True)),
                                     template_dir=SITE_TEMPLATE, squash=True, label="pokemon")
        self._lock = asyncio.Lock()
        self.png = b""
        self.problem = ""
        self.roms = self._rom_map(c)
        self.multi = len(self.roms) > 1
        self._cards_init()
        d = self.state.data
        d.setdefault("rounds", 0)
        d.setdefault("round_state", pg.new_round())
        d.setdefault("last_status", None)
        d.setdefault("recent", [])
        d.setdefault("events", [])
        d.setdefault("players", {})
        d.setdefault("by_game", {})                   # {game: last status dict} for the games not being played
        d.setdefault("ballot", None)                  # {"date", "end", "votes": {voter: [game, seq]}, "seq"}
        d.setdefault("ballot_date", "")
        if d.get("active") not in self.roms:
            d["active"] = str(c.get("game", "") or "").lower() if str(c.get("game", "") or "").lower() in self.roms \
                else next(iter(self.roms), "")
        self._anon = 0
        self.gb = self._open(d["active"]) if self.roms else None
        self.status = None
        if self.gb:
            self.status = self._read()
            self.png = self.gb.png()
            if not d["last_status"] or not self.gb.resumed:
                d["last_status"] = self.status.to_dict()       # baseline: nothing that already happened is news
            self.state.save()
        rs = d["round_state"]
        if rs.get("window_end") and rs["window_end"] < self.clock():
            rs["window_end"] = self.clock() + self.window         # the clock ran out while we were down: restart it, silently
            self.state.save()

    # ---------------------------------------------------------------- games and the emulator
    def _rom_map(self, c) -> dict:
        roms = c.get("roms")
        if isinstance(roms, dict) and roms:
            return {str(k).lower(): str(v) for k, v in roms.items()}
        rom = str(c.get("rom", "") or "").strip()
        if rom:
            return {"game": rom}
        if str(c.get("emulator", "")).lower() == "fake":        # tests
            return {"game": "fake"}
        self.problem = 'no ROM configured: set rom = "C:/path/to/Pokemon Red.gb" in [bots.%s]' % self.name
        return {}

    def _state_path(self, game: str) -> str:
        c = self.cfg
        if not self.multi:
            return str(c.get("state_file", "") or f"{self.name}.state")
        path = f"{self.name}-{game}.state"
        legacy = f"{self.name}.state"                           # a single-ROM save from before: it was Red
        if game == "red" and not os.path.isfile(path) and os.path.isfile(legacy):
            try:
                shutil.copyfile(legacy, path)
            except OSError:
                pass
        return path

    def _open(self, game: str):
        if str(self.cfg.get("emulator", "")).lower() == "fake":
            gb = FakeGameBoy(state_path=self._state_path(game))
            if game != "game":
                gb.title = f"POKEMON {game.upper()}"
                gb.layout = ram.layout_for_title(gb.title)
            return gb
        try:
            return GameBoy(self.roms[game], self._state_path(game))
        except ImportError:
            self.problem = "PyBoy is not installed: pip install pyboy pillow"
        except Exception as e:
            self.problem = f"could not start the emulator: {e}"
        log.error("Pokemon bot %s is not running (%s): %s", self.name, game, self.problem)
        return None

    def _read(self) -> ram.Status:
        return ram.read_status(self.gb, self.gb.species_names, getattr(self.gb, "layout", ram.RED_BLUE))

    def _play(self, button, steps):
        """Blocking (run in a thread): press, save, look."""
        self.gb.press(button, steps)
        self.gb.save()
        return self._read(), self.gb.png()

    @property
    def active(self) -> str:
        return self.state.data["active"]

    def game_title(self, game: str | None = None) -> str:
        game = game or self.active
        if self.gb and game == self.active and getattr(self.gb, "title", ""):
            return self.gb.title.title()
        return f"Pokemon {game.title()}" if game != "game" else "Pokemon"

    def _match_game(self, word: str):
        w = (word or "").strip().lower()
        if w in self.roms:
            return w
        hits = [g for g in self.roms if g.startswith(w)] if w else []
        return hits[0] if len(hits) == 1 else None

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
        if self.multi:
            await self._ballot_tick(now)
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

    # ---------------------------------------------------------------- the daily game vote
    def _ballot_open(self) -> bool:
        return bool(self.state.data.get("ballot"))

    async def _ballot_tick(self, now):
        d = self.state.data
        b = d.get("ballot")
        if b:
            if now >= b["end"]:
                await self._settle_ballot(now, quiet=now - b["end"] > BALLOT_GRACE)
            return
        n = core.now()
        today = n.date().isoformat()
        if d.get("ballot_date") == today or not (self.vote_hour <= n.hour < self.vote_hour + core.DIGEST_GRACE_HOURS):
            return
        d["ballot_date"] = today
        d["ballot"] = {"date": today, "end": now + self.ballot_seconds, "votes": {}, "seq": 0}
        self.state.save()
        mins = int(self.ballot_seconds // 60)
        self.alert("ballot", fit(f"🗳 Which game today? !game {' | '.join(self.roms)} (now {self.active}). Closes in {mins}m"))

    def _ballot_tally(self):
        b = self.state.data.get("ballot") or {"votes": {}}
        counts, first = {}, {}
        for g, seq in b["votes"].values():
            counts[g] = counts.get(g, 0) + 1
            first[g] = min(first.get(g, seq), seq)
        return [(g, counts[g]) for g in sorted(counts, key=lambda g: (-counts[g], first[g]))]

    async def _settle_ballot(self, now, quiet=False):
        d = self.state.data
        tally = self._ballot_tally()
        d["ballot"] = None
        self.state.save()
        if not tally:
            if not quiet:
                self.alert("ballot", fit(f"🗳 No votes - staying on {self.game_title()}"))
            return
        game, n = tally[0]
        if game == self.active:
            if not quiet:
                self.alert("ballot", fit(f"🗳 {self.game_title()} it is ({n} vote{'s' if n != 1 else ''}) - no change"))
            return
        ok = await self._switch(game)
        if ok and not quiet:
            self.alert("ballot", fit(f"🎮 Today: {self.game_title()} ({n} vote{'s' if n != 1 else ''}). Progress is kept per game"))

    async def _switch(self, game: str) -> bool:
        """Save the running game, open another ROM with its own save. Returns False (and keeps playing) on failure."""
        d = self.state.data
        old = self.active
        async with self._lock:
            def work():
                self.gb.save()
                self.gb.close()
                return self._open(game)
            try:
                new = await asyncio.to_thread(work)
            except Exception:
                log.exception("switching game failed")
                new = None
            if new is None:                                    # could not start it: go back to what was running
                self.gb = await asyncio.to_thread(self._open, old)
                return False
            self.gb = new
        d["by_game"][old] = d.get("last_status")
        d["active"] = game
        self.status = self._read()
        self.png = self.gb.png()
        prev = d["by_game"].get(game)
        d["last_status"] = prev if (prev and self.gb.resumed) else self.status.to_dict()
        d["round_state"] = pg.new_round()
        self._event(f"🎮 Now playing {self.game_title()}")
        self.state.save()
        return True

    # ---------------------------------------------------------------- commands
    def extra_commands(self):
        cmds = {"!where": self.cmd_where, "!status": self.cmd_where, "!party": self.cmd_party, "!votes": self.cmd_votes,
                "!top": self.cmd_top, "!reset": self.cmd_reset, "!press": self.cmd_press, "!p": self.cmd_press,
                "!game": self.cmd_game, "!switch": self.cmd_switch}
        for word in list(BUTTONS) + ["u", "d", "l", "r", "sel"]:
            cmds["!" + word] = self._button_cmd(word)
        return cmds

    def help_text(self):
        extra = " | !game <name>" if self.multi else ""
        return "!up !down !left !right !a !b !start !select (+ a number: !up 5) | !where !party !votes !top" + extra

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
        nvotes = 1
        for key, n in pg.tally(rs):
            if key == a:
                nvotes = n
                break
        left = max(0, int(rs["window_end"] - self.clock()))
        self._feedback(inc, f"✓ {pg.label(a)}: {nvotes} of {len(rs['votes'])} votes | {left}s left")
        return []

    async def cmd_game(self, inc, arg):
        opts = " | ".join(self.roms)
        if not self.multi:
            return [fit(f"[{self.tag}] Playing {self.game_title()}")]
        b = self.state.data.get("ballot")
        if not arg:
            tail = f" | vote open: !game {opts}" if b else f" | vote opens daily at {self.vote_hour}:00"
            return [fit(f"[{self.tag}] Playing {self.game_title()}{tail}")]
        if not b:
            return [fit(f"[{self.tag}] No game vote open - it opens daily at {self.vote_hour}:00")]
        g = self._match_game(arg)
        if not g:
            return [fit(f"[{self.tag}] Pick one: !game {opts}")]
        b["seq"] += 1
        b["votes"][self._voter(inc)] = [g, b["seq"]]
        self.state.save()
        left = max(0, int(b["end"] - self.clock()) // 60)
        tally = ", ".join(f"{k} {n}" for k, n in self._ballot_tally())
        self._feedback(inc, f"✓ {g} | {tally} | {left}m left")
        return []

    async def cmd_switch(self, inc, arg):
        if not is_strict_admin(inc):
            return [fit(f"[{self.tag}] {admin_refusal('switch games')}")]
        if not self.multi:
            return [f"[{self.tag}] Only one ROM is configured"]
        g = self._match_game(arg)
        if not g:
            return [fit(f"[{self.tag}] !switch {' | '.join(self.roms)}")]
        if g == self.active:
            return [fit(f"[{self.tag}] Already playing {self.game_title()}")]
        ok = await self._switch(g)
        return [fit(f"[{self.tag}] " + (f"Now playing {self.game_title()}" if ok else f"Could not start {g}: {self.problem}"))]

    async def cmd_where(self, inc, arg):
        st = self.status
        if not self.gb or st is None:
            return [fit(f"[{self.tag}] The game is not running ({self.problem})")]
        url = f" | {self.site_url}" if self.site_url else ""
        which = f"{self.game_title()}: " if self.multi else ""
        return [fit(f"[{self.tag}] {which}{ram.summary(st)} | round {self.state.data['rounds']}{url}")]

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
        """!reset [game] confirm - wipe a game's save (the one being played unless another is named)."""
        if not is_strict_admin(inc):
            return [fit(f"[{self.tag}] {admin_refusal('reset a game')}")]
        words = [w.lower() for w in inc.rest]
        confirm = "confirm" in words
        named = next((self._match_game(w) for w in words if w != "confirm" and self._match_game(w)), None)
        target = named or self.active
        if not confirm:
            which = f" {target}" if self.multi else ""
            return [fit(f"[{self.tag}] This wipes {self.game_title(target)} back to the title screen. "
                        f"Send: !reset{which} confirm")]
        if target != self.active:                       # a game that is not running: just delete its save
            path = self._state_path(target)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError as e:
                return [f"[{self.tag}] Could not reset: {e}"]
            self.state.data["by_game"].pop(target, None)
            self.state.save()
            return [fit(f"[{self.tag}] {self.game_title(target)} will start fresh next time it is played")]
        path = getattr(self.gb, "state_path", "") if self.gb else ""
        try:
            if self.gb:
                self.gb.close()
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError as e:
            return [f"[{self.tag}] Could not reset: {e}"]
        self.gb = self._open(self.active)
        d = self.state.data
        d.update(rounds=0, round_state=pg.new_round(), recent=[], events=[], last_status=None)
        d["by_game"].pop(self.active, None)
        if self.gb:
            self.status = self._read()
            self.png = self.gb.png()
            d["last_status"] = self.status.to_dict()
        self.state.save()
        return [fit(f"[{self.tag}] New game! Press !start")]

    # ---------------------------------------------------------------- the web page
    def site_state(self) -> dict:
        d = self.state.data
        rs = self.rnd
        top = sorted(d["players"].items(), key=lambda kv: (-kv[1]["presses"], -kv[1]["votes"], kv[0]))[:10]
        b = d.get("ballot")
        return {
            "version": 2,
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
            "game": {"active": self.active, "title": self.game_title(),
                     "options": [{"key": k, "title": self.game_title(k)} for k in self.roms] if self.multi else [],
                     "vote_hour": self.vote_hour},
            "ballot": ({"end": int(b["end"]), "votes": [{"game": g, "n": n} for g, n in self._ballot_tally()],
                        "voters": len(b["votes"])} if b else None),
            **self._cards_for_site(),
        }
