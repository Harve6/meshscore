"""Mesh Word: a daily five-letter word game for the whole channel (Wordle style).

Once a day (`word_hour`, default 8) the bot posts one short message. Everybody gets the same word and six guesses,
guesses go **by DM** (`!guess crane`), and the channel only ever hears *who* solved it and in how many tries - never
a letter. Yesterday's word is revealed in the next morning's post. A shared web board shows today's results (colour
squares, no letters), streaks and the leaderboard. See docs/daily-games.md.
"""
from __future__ import annotations

import logging
import random

from ..core import fit, pack
from ..dailyplay.base import DailyModule
from ..dailyplay.words import ANSWERS, score, squares, valid_guess

log = logging.getLogger("meshscore")

MAX_GUESSES = 6
HISTORY = 14


class WordModule(DailyModule):
    label = "Mesh Word"
    json_name = "word"
    board_title = "Mesh Word"
    kinds = {"max": {"word", "solved", "recap"}, "min": {"recap"}, "off": set()}
    min_blurb = "the daily recap only"
    bare_command = "!word"
    no_cooldown_cmds = ("!guess", "!word")

    def __init__(self, hub, name):
        super().__init__(hub, name)
        c = self.cfg
        self.blurb = "daily word game"
        self.hour = int(c.get("word_hour", 8))
        self.max_announce = int(c.get("max_announce", 5))          # solved-posts per day (airtime)
        self.rng = random.Random()
        d = self.state.data
        d.setdefault("puzzle", None)       # {"no", "date", "answer", "announced": int}
        d.setdefault("games", {})          # voter -> {"guesses": [str], "pats": [str], "done": bool, "won": bool}
        d.setdefault("history", [])        # [{"no", "date", "answer", "solved": {name: tries}, "played": n}]
        d.setdefault("stats", {})          # voter -> {"played","won","streak","best","last_won"}
        d.setdefault("next_no", 1)

    # ---------------------------------------------------------------- the daily loop
    async def poll(self):
        d = self.state.data
        p = d["puzzle"]
        today = self.today()
        if p is None:
            self._new_puzzle(today)
            if self.in_hour(self.hour):
                self.alert("word", self.start_text())
        elif p["date"] != today and self.past_hour(self.hour):
            recap = self._finish_puzzle()
            self._new_puzzle(today)
            if self.in_hour(self.hour):                # a restart later in the day starts the new word quietly
                if recap:
                    self.alert("recap", recap)
                self.alert("word", self.start_text())
        await self.publish()
        await self._refresh_cards(self.clock())
        self.mark_ok()
        return 60

    def _pick_answer(self) -> str:
        recent = {h["answer"] for h in self.state.data["history"][:200]}
        pool = [w for w in ANSWERS if w not in recent] or ANSWERS
        return self.rng.choice(pool)

    def _new_puzzle(self, today: str):
        d = self.state.data
        d["puzzle"] = {"no": d["next_no"], "date": today, "answer": self._pick_answer(), "announced": 0}
        d["next_no"] += 1
        d["games"] = {}
        self.state.save()

    def _finish_puzzle(self) -> str:
        """Archive the finished word, update streaks, and return the recap text (revealing the answer)."""
        d = self.state.data
        p = d["puzzle"]
        solved, played = {}, 0
        for who, g in d["games"].items():
            if not g["guesses"]:
                continue
            played += 1
            st = d["stats"].setdefault(who, {"played": 0, "won": 0, "streak": 0, "best": 0, "last_won": ""})
            if g["won"]:
                solved[who] = len(g["guesses"])
            elif not g["done"]:                         # started but never finished: counts as a loss
                st["streak"] = 0
        d["history"].insert(0, {"no": p["no"], "date": p["date"], "answer": p["answer"], "solved": solved, "played": played})
        del d["history"][HISTORY:]
        self.state.save()
        who = ", ".join(f"{n} {t}" for n, t in sorted(solved.items(), key=lambda kv: (kv[1], kv[0]))[:5])
        if not played:
            return fit(f"🔤 Mesh Word #{p['no']} was {p['answer'].upper()} - nobody played")
        return fit(f"🔤 Mesh Word #{p['no']} was {p['answer'].upper()}. {len(solved)}/{played} solved" + (f": {who}" if who else ""))

    def start_text(self) -> str:
        p = self.state.data["puzzle"]
        url = f" {self.site_url}" if self.site_url else ""
        return fit(f"🔤 Mesh Word #{p['no']}: a 5-letter word, 6 tries. DM me: !guess crane{url}")

    # ---------------------------------------------------------------- commands
    def extra_commands(self):
        return {"!guess": self.cmd_guess, "!word": self.cmd_word, "!wordtop": self.cmd_top}

    def help_text(self):
        return "!guess <5 letters> (by DM) | !word your board | !wordtop"

    def status_extra(self):
        p = self.state.data.get("puzzle") or {}
        return f" | word #{p.get('no', '-')} players {len(self.state.data.get('games', {}))}"

    def _game(self, who):
        return self.state.data["games"].setdefault(who, {"guesses": [], "pats": [], "done": False, "won": False})

    async def cmd_guess(self, inc, arg):
        if not inc.is_dm:
            return self._dm_only(inc, "your guess", "!guess crane")
        d = self.state.data
        p = d["puzzle"]
        if not p:
            return [f"[{self.tag}] No word yet - a new one comes every day at {self.hour}:00"]
        text = " ".join(inc.rest) if inc.rest else arg
        if not text:
            return await self.cmd_word(inc, "")
        if not valid_guess(text):
            return [fit(f"[{self.tag}] Five letters a-z, like !guess crane")]
        who = self._voter(inc)
        g = self._game(who)
        if g["done"]:
            return await self.cmd_word(inc, "")
        guess = text.strip().lower()
        pat = score(guess, p["answer"])
        g["guesses"].append(guess)
        g["pats"].append(pat)
        n = len(g["guesses"])
        st = d["stats"].setdefault(who, {"played": 0, "won": 0, "streak": 0, "best": 0, "last_won": ""})
        if n == 1:
            st["played"] += 1
        out = []
        if guess == p["answer"]:
            g["done"], g["won"] = True, True
            st["won"] += 1
            st["streak"] = st["streak"] + 1 if st["last_won"] == self.yesterday() else 1
            st["best"] = max(st["best"], st["streak"])
            st["last_won"] = p["date"]
            out.append(fit(f"[{self.tag}] 🎉 #{p['no']} solved in {n}/{MAX_GUESSES}! Streak {st['streak']}"))
            out.append(fit(f"Mesh Word #{p['no']} {n}/{MAX_GUESSES} " + "/".join(squares(x) for x in g["pats"])))
            if p["announced"] < self.max_announce:
                p["announced"] += 1
                self.alert("solved", f"🔤 {who} solved Mesh Word #{p['no']} in {n}/{MAX_GUESSES}")
        elif n >= MAX_GUESSES:
            g["done"] = True
            st["streak"] = 0
            out.append(fit(f"[{self.tag}] #{p['no']} {squares(pat)} - out of guesses. The word is in tomorrow's post"))
        else:
            gone = sorted({c for gu, pt in zip(g["guesses"], g["pats"]) for c, k in zip(gu, pt) if k == "."} -
                          {c for gu, pt in zip(g["guesses"], g["pats"]) for c, k in zip(gu, pt) if k in "gy"})
            tail = f" | out: {' '.join(gone)}" if gone else ""
            out.append(fit(f"[{self.tag}] #{p['no']} {n}/{MAX_GUESSES} {guess.upper()} {squares(pat)}{tail}"))
        self.state.save()
        for t in out:
            self._feedback(inc, t)
        return []

    async def cmd_word(self, inc, arg):
        d = self.state.data
        p = d["puzzle"]
        if not p:
            return [f"[{self.tag}] No word yet - a new one comes every day at {self.hour}:00"]
        if not inc.is_dm:
            url = f" {self.site_url}" if self.site_url else ""
            return [fit(f"[{self.tag}] #{p['no']} - DM me !guess <5 letters> ({MAX_GUESSES} tries){url}")]
        who = self._voter(inc)
        g = d["games"].get(who)
        st = d["stats"].get(who, {})
        streak = st.get("streak", 0) if st.get("last_won") in (self.today(), self.yesterday()) else 0
        if not g or not g["guesses"]:
            return [fit(f"[{self.tag}] #{p['no']}: 5 letters, {MAX_GUESSES} tries. !guess crane | streak {streak}")]
        rows = [f"{w.upper()}{squares(pt)}" for w, pt in zip(g["guesses"], g["pats"])]
        head = f"[{self.tag}] #{p['no']} {len(g['guesses'])}/{MAX_GUESSES}" + (" SOLVED" if g["won"] else "") + f" streak {streak}:"
        return pack(head, rows, sep=" ")

    async def cmd_top(self, inc, arg):
        stats = self.state.data["stats"]
        if not stats:
            return [f"[{self.tag}] Nobody has played yet"]
        by_streak = sorted(stats.items(), key=lambda kv: (-kv[1]["best"], -kv[1]["won"], kv[0]))[:3]
        by_wins = sorted(stats.items(), key=lambda kv: (-kv[1]["won"], kv[0]))[:3]
        return [fit(f"[{self.tag}] best streaks: " + ", ".join(f"{n} {s['best']}" for n, s in by_streak) +
                    " | wins: " + ", ".join(f"{n} {s['won']}" for n, s in by_wins))]

    # ---------------------------------------------------------------- the shared web board
    def site_state(self) -> dict:
        d = self.state.data
        p = d["puzzle"]
        today = []
        for who, g in d["games"].items():
            if g["guesses"]:
                row = {"name": who, "tries": len(g["guesses"]) if g["won"] else ("X" if g["done"] else None)}
                if g["done"]:                                   # colour squares only once finished, and never letters
                    row["grid"] = [squares(x) for x in g["pats"]]
                today.append(row)
        top = sorted(d["stats"].items(), key=lambda kv: (-kv[1]["best"], -kv[1]["won"], kv[0]))[:10]
        return {
            **self.base_state(),
            "puzzle": {"no": p["no"], "date": p["date"], "guesses": MAX_GUESSES, "players": len(d["games"])} if p else None,
            "today": today,
            "history": [{"no": h["no"], "date": h["date"], "answer": h["answer"], "solved": len(h["solved"]), "played": h["played"]}
                        for h in d["history"][:7]],
            "leaders": [{"name": n, "best": s["best"], "won": s["won"], "played": s["played"]} for n, s in top],
            "hour": self.hour,
        }
