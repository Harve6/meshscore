"""Crowd-vs-bot chess on a MeshCore channel: everybody votes a move, the bot answers, the level climbs with each win.

How a round goes: the first `!move` of a round starts a clock (`window_minutes`, default 5); everyone who votes in that
time counts, the most popular move is played (ties: earliest first vote), then the bot replies - one channel post per
round. Players get answers by DM when the radio knows them, so the channel carries moves and results only. Daily
puzzle answers are taken by DM only, so nobody can copy them off the channel. A web board (a Cloudflare Worker,
`site_api`, or a GitHub Pages clone, `site_dir`) shows the position, the votes and the puzzle. See docs/chess.md.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time

import chess

from .. import core
from ..chessplay import puzzle as pz
from ..chessplay.cards import CardsMixin
from ..chessplay.engine import LEVELS, Opponent
from ..chessplay.game import PIECE_WORDS, CrowdGame, candidates
from ..chessplay.publish import HttpPublisher, SitePublisher
from ..core import DIGEST_GRACE_HOURS, admin_refusal, fit, fits, is_strict_admin, pack, session
from ..plugin import LeaguePlugin

log = logging.getLogger("meshscore")

MAX_TOP = 10
HISTORY = 10
STATE_VERSION = 1


def level_label(i: int) -> str:
    i = max(0, min(i, len(LEVELS) - 1))
    return f"L{i + 1} {LEVELS[i].name}"


class ChessModule(CardsMixin, LeaguePlugin):
    label = "Chess"
    kinds = {"max": {"start", "move", "final", "puzzle"}, "min": {"start", "final", "puzzle"}, "off": set()}
    min_blurb = "game results + the daily puzzle (no per-move posts)"
    bare_command = "!board"
    board_title = "Mesh Chess"
    no_cooldown_cmds = ("!move", "!m", "!puzzle")      # many people vote at once; each is limited by what a vote can do

    def __init__(self, hub, name):
        super().__init__(hub, name)
        self.clock = time.time
        c = self.cfg
        self.blurb = "crowd vs bot chess"
        self.window = max(30.0, float(c.get("window_minutes", 5)) * 60)
        self.site_url = str(c.get("site_url", "") or "").strip()
        self.ack = str(c.get("ack", "dm")).lower()
        self.puzzle_on = bool(c.get("puzzle", True))
        self.puzzle_hour = int(c.get("puzzle_hour", 9))
        self.max_level = max(0, min(int(c.get("max_level", len(LEVELS))) - 1, len(LEVELS) - 1))
        self.opponent = Opponent(str(c.get("stockfish", "") or ""), float(c.get("think_seconds", 15)))
        if str(c.get("site_api", "") or "").strip():              # a Cloudflare Worker (contrib/cloudflare-board)
            self.pub = HttpPublisher(str(c["site_api"]), str(c.get("site_token", "") or ""),
                                     float(c.get("publish_min_seconds", 4)), label="chess")
        else:                                                     # a GitHub Pages clone
            self.pub = SitePublisher(str(c.get("site_dir", "") or ""), float(c.get("publish_min_seconds", 60)),
                                     push=bool(c.get("site_push", True)), sync_page=bool(c.get("site_sync", True)))
        self.busy = False
        self._anon = 0
        self._pending = {}        # voter -> (ply, [uci...]): the choices offered after an ambiguous move
        self._dm_hint = {}        # voter -> when they were last told "puzzle answers go by DM"
        self._cards_init()
        d = self.state.data
        d.setdefault("level", max(0, min(int(c.get("start_level", 1)) - 1, self.max_level)))
        d.setdefault("next_id", 1)
        d.setdefault("record", {"crowd": 0, "bot": 0, "draw": 0})
        d.setdefault("players", {})
        d.setdefault("history", [])
        d.setdefault("game", None)
        d.setdefault("puzzle", None)
        d.setdefault("puzzle_date", "")
        g = d["game"]
        if g and not g.get("over") and g.get("window_end") and g["window_end"] < self.clock():
            g["window_end"] = self.clock() + self.window         # the clock ran out while we were down: start it over, silently
            self.state.save()

    # ---------------------------------------------------------------- helpers
    def game(self) -> CrowdGame:
        d = self.state.data
        if not d["game"] or d["game"].get("over"):
            self._new_game()
        return CrowdGame(d["game"])

    def _new_game(self, level=None):
        d = self.state.data
        if level is not None:
            d["level"] = max(0, min(level, self.max_level))
        d["game"] = CrowdGame.new(d["next_id"], d["level"], None, self.clock())
        d["next_id"] += 1
        self.state.save()

    def _voter(self, inc) -> str:
        who = (inc.sender_name or "").strip() or (inc.sender_key or "")[:8]
        if not who:
            self._anon += 1
            who = f"anon{self._anon}"
        return who

    def _url(self) -> str:
        return self.site_url

    def _player(self, who):
        return self.state.data["players"].setdefault(who, {"moves": 0, "votes": 0, "puzzles": 0})

    def _feedback(self, inc, text, important=False):
        """Answer a player without crowding the channel: DM when we can, else a channel reply only when it matters."""
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

    def _mins(self) -> str:
        m = self.window / 60
        return f"{int(m)}" if m == int(m) else f"{m:.1f}"

    # ---------------------------------------------------------------- texts
    def start_text(self, g: CrowdGame) -> str:
        url = f" {self._url()}" if self._url() else ""
        return fit(f"♟ New game #{g.data['id']}: crowd (White) vs bot {level_label(g.data['level'])}. "
                   f"Vote: !move e4 (any move){url}")

    def round_text(self, g: CrowdGame, r) -> str:
        bot = f" {r.number}...{r.bot_san}" if r.bot_san else ""
        votes = f"{r.votes} vote{'s' if r.votes != 1 else ''}"
        return fit(f"♟ {r.number}.{r.crowd_san} ({votes}){bot} | {level_label(g.data['level'])} | "
                   f"!move <move>, clock starts at 1st vote ({self._mins()}m)")

    def final_texts(self, g: CrowdGame, r, old_level, new_level, beat_top=False) -> list:
        n = (len(g.data["moves"]) + 1) // 2
        lv = LEVELS[old_level].name
        if r.winner == "crowd":
            what = f"Crowd wins by checkmate vs {lv}!"
        elif r.winner == "bot":
            what = f"Bot ({lv}) wins by checkmate."
        else:
            what = f"Draw by {r.reason} vs {lv}."
        tail = ""
        if r.winner == "crowd" and beat_top:
            tail = " Top level beaten - a legend!"
        elif r.winner == "crowd" and new_level != old_level:
            tail = f" Level up: {level_label(new_level)}."
        final = f"🏁 FINAL: {what} {n} moves.{tail}"
        nxt = f"♟ Game #{self.state.data['game']['id']}: vote !move <move>"
        both = f"{final} {nxt}"
        return [fit(both)] if fits(both) else [fit(final), fit(nxt)]

    # ---------------------------------------------------------------- the loop
    async def poll(self):
        now = self.clock()
        g = self.game()
        if not g.data.get("announced"):
            g.data["announced"] = True
            self.state.save()
            self.alert("start", self.start_text(g))
        if g.data.get("window_end") and now >= g.data["window_end"]:
            await self._close_round()
            g = self.game()
        await self._maybe_puzzle()
        await self._refresh_cards(now)
        if self.pub.enabled:
            await asyncio.to_thread(self.pub.tick, self.site_state())
        self.mark_ok()
        we = g.data.get("window_end")
        return max(1.0, min(30.0, (we - self.clock()) + 1)) if we else 15.0

    async def _close_round(self):
        d = self.state.data
        level = d["level"]
        work = copy.deepcopy(d["game"])
        cg = CrowdGame(work)
        if cg.leading_move() is None:                            # nobody voted (state edited by hand?): just reset the clock
            d["game"]["window_end"] = None
            self.state.save()
            return
        self.busy = True
        try:
            rnd = await asyncio.to_thread(cg.play_round, lambda b: self.opponent.choose(b, level))
        except Exception:
            log.exception("chess round failed; the votes stay and it will be tried again")
            d["game"]["window_end"] = self.clock() + 60
            self.state.save()
            return
        finally:
            self.busy = False
        d["game"] = cg.data
        for who in rnd.voter_ids:
            self._player(who)["votes"] += 1
        for who in rnd.chosen_by:
            self._player(who)["moves"] += 1
        if not rnd.winner:
            cg.data["window_end"] = None                         # the next round's clock starts at its first vote
            self.state.save()
            self.alert("move", self.round_text(cg, rnd))
            return
        # ---- the game ended
        rec = d["record"]
        rec[rnd.winner] = rec.get(rnd.winner, 0) + 1
        d["history"] = ([{"id": cg.data["id"], "level": level, "result": rnd.winner, "reason": rnd.reason,
                          "moves": " ".join(cg.move_list())}] + d["history"])[:HISTORY]
        beat_top = rnd.winner == "crowd" and level >= self.max_level
        if rnd.winner == "crowd" and level < self.max_level:
            d["level"] = level + 1
        self._new_game()
        g2 = CrowdGame(d["game"])
        g2.data["announced"] = True
        self.state.save()
        for t in self.final_texts(cg, rnd, level, d["level"], beat_top):
            self.alert("final", t)

    # ---------------------------------------------------------------- the daily puzzle
    async def _maybe_puzzle(self):
        if not self.puzzle_on:
            return
        d = self.state.data
        n = core.now()
        today = n.date().isoformat()
        first_ever = not d.get("puzzle")
        in_hour = self.puzzle_hour <= n.hour < self.puzzle_hour + DIGEST_GRACE_HOURS
        if d.get("puzzle_date") == today or not (in_hour or first_ever):
            return
        try:
            puz = await asyncio.to_thread(pz.fetch_daily, session, today)
        except Exception as e:
            log.warning("Daily puzzle fetch failed: %s", e)
            return
        if puz is None:
            return
        if d.get("puzzle") and d["puzzle"].get("id") == puz["id"]:
            d["puzzle_date"] = today
            self.state.save()
            return
        d["puzzle"], d["puzzle_date"] = puz, today
        self.state.save()
        if in_hour:                                              # a first-ever fetch outside the hour is quiet
            self.alert("puzzle", self.puzzle_text(puz))

    def puzzle_text(self, puz) -> str:
        url = f" {self._url()}" if self._url() else ""
        side = puz["side"].capitalize()
        return fit(f"🧩 Daily puzzle ({puz['rating']}): {side} to move. Answer by DM: !puzzle <move>{url}")

    # ---------------------------------------------------------------- commands
    def extra_commands(self):
        return {"!move": self.cmd_move, "!m": self.cmd_move, "!board": self.cmd_board, "!moves": self.cmd_moves,
                "!puzzle": self.cmd_puzzle, "!answer": self.cmd_answer, "!top": self.cmd_top,
                "!newgame": self.cmd_newgame}

    def help_text(self):
        return "!move <e4|Nf3|g1 f3> vote | !board | !moves [g1] | !top | puzzle by DM: !puzzle <move>, !answer"

    def status_extra(self):
        g = self.state.data.get("game") or {}
        return f" | game #{g.get('id', '-')} {level_label(self.state.data['level'])} votes {len(g.get('votes') or {})}"

    async def cmd_move(self, inc, arg):
        g = self.game()
        text = " ".join(inc.rest) if inc.rest else arg          # "g1 f3" and "knight e3" are several words
        if not text:
            return [fit(f"[{self.tag}] Vote a move: !move e4, !move Nf3, !move g1 f3 or !move knight e3 (!moves lists them)")]
        if self.busy:
            self._feedback(inc, "♟ The bot is thinking - vote again in a moment", important=True)
            return []
        who = self._voter(inc)
        if text.strip().isdigit():                              # the answer to a "which one?"
            pend = self._pending.get(who)
            i = int(text.strip()) - 1
            if not pend or pend[0] != len(g.data["moves"]) or not 0 <= i < len(pend[1]):
                self._feedback(inc, "✗ no choices waiting - send the move, like !move g1 f3", important=True)
                return []
            text = pend[1][i]
        status, san = g.vote(who, text)
        if status == "ambiguous":                               # two pieces can go there: offer numbered choices
            b = g.board
            opts = sorted(candidates(b, text), key=lambda m: m.from_square)[:6]       # a1..h8: the same order every time
            if len(opts) > 1:
                self._pending[who] = (len(g.data["moves"]), [m.uci() for m in opts])
                shown = " | ".join(f"{i}={b.san(m)}({chess.square_name(m.from_square)})" for i, m in enumerate(opts, 1))
                self._feedback(inc, f"Which one? Send !move N: {shown}", important=True)
                return []
        if status != "ok":
            word = {"ambiguous": "is ambiguous - say it from-to, like g1 f3",
                    "illegal": "is not legal here (!moves lists them)", "empty": "?"}.get(status, "is not possible now")
            self._feedback(inc, f"✗ {text} {word}", important=True)
            return []
        self._pending.pop(who, None)
        if not g.data.get("window_end"):
            g.data["window_end"] = self.clock() + self.window
        self.state.save()
        tally = dict(g.tally())
        left = max(0, int(g.data["window_end"] - self.clock()))
        self._feedback(inc, f"✓ {san}: {tally.get(san, 1)} vote{'s' if tally.get(san, 1) != 1 else ''}"
                            f" of {len(g.data['votes'])} | {left // 60}m{left % 60:02d}s left")
        return []

    async def cmd_board(self, inc, arg):
        g = self.game()
        b = g.board
        d = g.data
        last = ""
        if d["moves"]:
            tail = g.move_list()[-2:]
            last = " | last " + " ".join(tail)
        votes = ", ".join(f"{s} {n}" for s, n in g.tally()[:3])
        clock = ""
        if d.get("window_end"):
            left = max(0, int(d["window_end"] - self.clock()))
            clock = f" | {left // 60}m{left % 60:02d}s left"
        else:
            clock = " | clock starts at 1st vote"
        url = f" | {self._url()}" if self._url() else ""
        in_check = " CHECK" if b.is_check() else ""
        return [fit(f"[{self.tag}] #{d['id']} {level_label(d['level'])} | move {b.fullmove_number}{in_check}{last}"
                    f"{' | votes: ' + votes if votes else ''}{clock}{url}")]

    async def cmd_moves(self, inc, arg):
        """!moves = every legal move; !moves g1 = what the piece on g1 can do; !moves knight = that piece type."""
        g = self.game()
        b = g.board
        word = (arg or "").strip().lower()
        moves, label = list(b.legal_moves), "legal"
        if len(word) == 2 and word[0] in "abcdefgh" and word[1] in "12345678":
            sq = chess.parse_square(word)
            piece = b.piece_at(sq)
            moves = [m for m in moves if m.from_square == sq]
            if not moves:
                return [fit(f"[{self.tag}] Nothing of yours on {word} can move")]
            label = f"{chess.piece_name(piece.piece_type)} {word}"
        elif word in PIECE_WORDS and word != "castle":
            moves = [m for m in moves if b.piece_type_at(m.from_square) == PIECE_WORDS[word]]
            label = chess.piece_name(PIECE_WORDS[word])
            if not moves:
                return [fit(f"[{self.tag}] No {label} can move")]
        return pack(f"[{self.tag}] {label}:", sorted(b.san(m) for m in moves), sep=" ")

    async def cmd_top(self, inc, arg):
        d = self.state.data
        rec = d["record"]
        top = sorted(d["players"].items(), key=lambda kv: (-kv[1]["moves"], -kv[1]["votes"], kv[0]))[:3]
        pz_top = sorted(((n, p["puzzles"]) for n, p in d["players"].items() if p.get("puzzles")), key=lambda x: -x[1])[:2]
        text = f"[{self.tag}] crowd {rec['crowd']}W {rec['bot']}L {rec['draw']}D"
        if top and top[0][1]["moves"]:
            text += " | moves: " + ", ".join(f"{n} {p['moves']}" for n, p in top)
        if pz_top:
            text += " | puzzles: " + ", ".join(f"{n} {c}" for n, c in pz_top)
        return [fit(text)]

    def _dm_only(self, inc, what) -> list:
        """A puzzle answer sent on the channel is not checked (others would see the verdict) - point at DMs, at most
        once every 10 minutes per person."""
        who = self._voter(inc)
        now = self.clock()
        if now - self._dm_hint.get(who, 0) < 600:
            return []
        self._dm_hint[who] = now
        return [fit(f"[{self.tag}] 🧩 {who}: send {what} to me by DM (!puzzle <move>) so nobody can copy it")]

    async def cmd_puzzle(self, inc, arg):
        puz = self.state.data.get("puzzle")
        if not puz:
            return [f"[{self.tag}] No puzzle yet - one is posted every day at {self.puzzle_hour}:00"]
        who = self._voter(inc)
        arg = " ".join(inc.rest) if inc.rest else arg
        url = f" {self._url()}" if self._url() else ""
        if not arg:
            b = pz.position(puz, who)
            side = "White" if b.turn == chess.WHITE else "Black"
            if not inc.is_dm:                                  # nothing personal on the channel: it would give moves away
                return [fit(f"[{self.tag}] 🧩 {puz['rating']}: {side} to move. Answer by DM: !puzzle <move>{url}")]
            if who in puz["solved"]:
                return [fit(f"[{self.tag}] 🧩 You solved today's puzzle. New one at {self.puzzle_hour}:00")]
            prog = int(puz["progress"].get(who, 0))
            line = f" | your line: {pz.answer_text(puz, prog)}" if prog else ""
            left = pz.MAX_TRIES - int(puz["tries"].get(who, 0))
            return [fit(f"[{self.tag}] 🧩 {puz['rating']}: {side} to move{line} | {left} tries left | !puzzle <move>")]
        if not inc.is_dm:
            return self._dm_only(inc, "puzzle answers")
        res = pz.attempt(puz, who, arg)
        st = res["status"]
        if st in ("right", "solved", "wrong"):
            self.state.save()
        if st == "solved":
            self._player(who)["puzzles"] += 1
            self.state.save()
            self.alert("puzzle", fit(f"🧩 {who} solved today's puzzle! (#{res['n']})"))     # the channel hears who, not how
            return [fit(f"🧩 {res['san']} - solved! You're #{res['n']} today")]
        if st == "right":
            return [fit(f"🧩 {res['san']} ✓ They reply {res['reply_san']}. Your move: !puzzle <move>")]
        if st == "wrong":
            left = res["tries_left"]
            tail = f"{left} {'try' if left == 1 else 'tries'} left" if left else "out of tries - !answer shows it"
            return [fit(f"🧩 {res['san']} isn't it. {tail}")]
        if st == "ambiguous":
            return [f"[{self.tag}] That move is ambiguous - say it from-to, like g1 f3"]
        if st == "locked":
            return [f"[{self.tag}] Out of tries on this puzzle - !answer shows the solution"]
        if st == "done":
            return [f"[{self.tag}] You already solved today's puzzle!"]
        return [fit(f"[{self.tag}] {arg} isn't a legal move in your position (!puzzle shows who is to move)")]

    async def cmd_answer(self, inc, arg):
        puz = self.state.data.get("puzzle")
        if not puz:
            return [f"[{self.tag}] No puzzle yet"]
        if not inc.is_dm:                                      # the solution goes to whoever asks, not to everyone
            return self._dm_only(inc, "!answer")
        return pack(f"[{self.tag}] 🧩", [pz.answer_text(puz)], sep=" ")

    async def cmd_newgame(self, inc, arg):
        if not is_strict_admin(inc):
            return [fit(f"[{self.tag}] {admin_refusal('start a new game', inc)}")]
        level = None
        if arg:
            if not arg.isdigit() or not 1 <= int(arg) <= self.max_level + 1:
                return [f"[{self.tag}] !newgame [level 1-{self.max_level + 1}]"]
            level = int(arg) - 1
        self._new_game(level)
        g = self.game()
        g.data["announced"] = True
        self.state.save()
        return [self.start_text(g)]

    # ---------------------------------------------------------------- the web board
    def site_state(self) -> dict:
        d = self.state.data
        g = CrowdGame(d["game"]) if d.get("game") else self.game()
        b = g.board
        lv = d["level"]
        votes = [{"san": s, "n": n} for s, n in g.tally()]
        last = g.last_move()
        puz = d.get("puzzle")
        top = sorted(d["players"].items(), key=lambda kv: (-kv[1]["moves"], -kv[1]["votes"], kv[0]))[:MAX_TOP]
        return {
            "version": STATE_VERSION,
            "updated": int(self.clock()),
            "window_seconds": int(self.window),
            "levels": [{"name": x.name, "elo": x.elo} for x in LEVELS[: self.max_level + 1]],
            "game": {
                "id": g.data["id"], "level": lv, "level_name": LEVELS[lv].name, "elo": LEVELS[lv].elo,
                "fen": b.fen(), "turn": "white" if b.turn == chess.WHITE else "black", "check": b.is_check(),
                "moves": g.move_list(), "last": list(last) if last else None,
                "window_end": int(g.data["window_end"]) if g.data.get("window_end") else None,
                "votes": votes, "voters": len(g.data["votes"]),
            },
            "record": d["record"],
            **self._cards_for_site(),
            "players": [{"name": n, **p} for n, p in top],
            "history": d["history"],
            "puzzle": ({"id": puz["id"], "date": puz["date"], "fen": puz["fen"], "side": puz["side"],
                        "rating": puz["rating"], "solved": puz["solved"][:20]} if puz else None),
        }
