"""Mesh Trivia: one multiple-choice question a day, answered by DM.

Once a day (`trivia_hour`, default 12) the bot posts the question with four choices. Players DM their letter
(`!trivia B`); they get one try, are told right or wrong, and the channel only hears the next day's recap with the
answer. Streaks and a leaderboard are on the shared web board. See docs/daily-games.md.
"""
from __future__ import annotations

import logging
import random

from ..core import fit
from ..dailyplay.base import DailyModule
from ..dailyplay.trivia_bank import QUESTIONS

log = logging.getLogger("meshscore")

HISTORY = 14
LETTERS = "ABCD"


def format_question(no: int, q: str, choices: list[str]) -> str:
    """The channel post, without the 'DM !trivia X' hint (added when it fits)."""
    return f"🧠 #{no} {q} " + " ".join(f"{LETTERS[i]}) {c}" for i, c in enumerate(choices))


class TriviaModule(DailyModule):
    label = "Mesh Trivia"
    json_name = "trivia"
    board_title = "Mesh Trivia"
    kinds = {"max": {"trivia", "recap"}, "min": {"recap"}, "off": set()}
    min_blurb = "the daily recap only"
    bare_command = "!trivia"
    no_cooldown_cmds = ("!trivia",)

    def __init__(self, hub, name):
        super().__init__(hub, name)
        c = self.cfg
        self.blurb = "daily trivia"
        self.hour = int(c.get("trivia_hour", 12))
        self.categories = {str(x).lower() for x in (c.get("categories") or [])}
        self.rng = random.Random()
        d = self.state.data
        d.setdefault("current", None)      # {"no","date","q","choices","answer","cat","q_index"}
        d.setdefault("answers", {})        # voter -> letter index
        d.setdefault("history", [])        # [{"no","date","q","answer","right","played"}]
        d.setdefault("stats", {})          # voter -> {"played","right","streak","best","last"}
        d.setdefault("used", [])           # question indexes already asked (reset when all have been)
        d.setdefault("next_no", 1)

    # ---------------------------------------------------------------- the daily loop
    async def poll(self):
        d = self.state.data
        cur = d["current"]
        today = self.today()
        if cur is None:
            self._new_question(today)
            if self.in_hour(self.hour):
                self.alert("trivia", self.post_text())
        elif cur["date"] != today and self.past_hour(self.hour):
            recap = self._finish()
            self._new_question(today)
            if self.in_hour(self.hour):
                if recap:
                    self.alert("recap", recap)
                self.alert("trivia", self.post_text())
        await self.publish()
        await self._refresh_cards(self.clock())
        self.mark_ok()
        return 60

    def _pool(self) -> list[int]:
        d = self.state.data
        idx = [i for i, q in enumerate(QUESTIONS) if not self.categories or q[0] in self.categories]
        fresh = [i for i in idx if i not in d["used"]]
        if not fresh:                                  # every question has been asked: start the cycle again
            d["used"] = [i for i in d["used"] if i not in idx]
            fresh = idx
        return fresh

    def _new_question(self, today: str):
        d = self.state.data
        i = self.rng.choice(self._pool())
        cat, q, right, wrong = QUESTIONS[i]
        choices = [right] + list(wrong)
        self.rng.shuffle(choices)
        d["current"] = {"no": d["next_no"], "date": today, "q": q, "choices": choices, "answer": choices.index(right),
                        "cat": cat, "q_index": i}
        d["next_no"] += 1
        d["used"].append(i)
        d["answers"] = {}
        self.state.save()

    def _finish(self) -> str:
        d = self.state.data
        cur = d["current"]
        right = [w for w, a in d["answers"].items() if a == cur["answer"]]
        d["history"].insert(0, {"no": cur["no"], "date": cur["date"], "q": cur["q"], "answer": cur["choices"][cur["answer"]],
                                "right": len(right), "played": len(d["answers"])})
        del d["history"][HISTORY:]
        for who, a in d["answers"].items():            # a streak survives only while you keep answering correctly
            st = d["stats"].setdefault(who, {"played": 0, "right": 0, "streak": 0, "best": 0, "last": ""})
            if a != cur["answer"]:
                st["streak"] = 0
        self.state.save()
        ans = f"{LETTERS[cur['answer']]}) {cur['choices'][cur['answer']]}"
        if not d["answers"]:
            return fit(f"🧠 #{cur['no']} answer: {ans} - nobody played")
        return fit(f"🧠 #{cur['no']} answer: {ans}. {len(right)}/{len(d['answers'])} got it")

    def post_text(self) -> str:
        cur = self.state.data["current"]
        base = format_question(cur["no"], cur["q"], cur["choices"])
        hint = " DM !trivia <A-D>"
        return fit(base + hint) if len((base + hint).encode()) <= 130 else fit(base)

    # ---------------------------------------------------------------- commands
    def extra_commands(self):
        return {"!trivia": self.cmd_trivia, "!trivtop": self.cmd_top}

    def help_text(self):
        return "!trivia <A-D> (by DM) | !trivtop"

    def status_extra(self):
        cur = self.state.data.get("current") or {}
        return f" | trivia #{cur.get('no', '-')} answers {len(self.state.data.get('answers', {}))}"

    async def cmd_trivia(self, inc, arg):
        d = self.state.data
        cur = d["current"]
        if not cur:
            return [f"[{self.tag}] No question yet - a new one comes every day at {self.hour}:00"]
        text = (" ".join(inc.rest) if inc.rest else arg).strip()
        if not text:
            q = format_question(cur["no"], cur["q"], cur["choices"])
            return [fit(f"{q} !trivia <A-D>")] if inc.is_dm else [fit(f"[{self.tag}] {q}")]
        if not inc.is_dm:
            return self._dm_only(inc, "your answer", "!trivia B")
        n = len(cur["choices"])
        if len(text) == 1 and text.upper() in LETTERS[:n]:
            idx = LETTERS.index(text.upper())
        else:                                          # the text of a choice works too ("!trivia Canadiens")
            hit = [i for i, c in enumerate(cur["choices"]) if c.lower() == text.lower()]
            if not hit:
                return [fit(f"[{self.tag}] Answer with a letter: !trivia A, B, C or D")]
            idx = hit[0]
        letter = LETTERS[idx]
        who = self._voter(inc)
        if who in d["answers"]:
            return [fit(f"[{self.tag}] You already answered #{cur['no']} ({LETTERS[d['answers'][who]]})")]
        d["answers"][who] = idx
        st = d["stats"].setdefault(who, {"played": 0, "right": 0, "streak": 0, "best": 0, "last": ""})
        st["played"] += 1
        if idx == cur["answer"]:
            st["right"] += 1
            st["streak"] = st["streak"] + 1 if st["last"] == self.yesterday() else 1
            st["best"] = max(st["best"], st["streak"])
            st["last"] = cur["date"]
            reply = f"[{self.tag}] ✓ Correct! #{cur['no']} streak {st['streak']}"
        else:
            st["streak"] = 0
            reply = f"[{self.tag}] ✗ Not {letter}. The answer is in tomorrow's post"
        self.state.save()
        self._feedback(inc, reply)
        return []

    async def cmd_top(self, inc, arg):
        stats = self.state.data["stats"]
        if not stats:
            return [f"[{self.tag}] Nobody has played yet"]
        by_streak = sorted(stats.items(), key=lambda kv: (-kv[1]["best"], -kv[1]["right"], kv[0]))[:3]
        by_right = sorted(stats.items(), key=lambda kv: (-kv[1]["right"], kv[0]))[:3]
        return [fit(f"[{self.tag}] best streaks: " + ", ".join(f"{n} {s['best']}" for n, s in by_streak) +
                    " | correct: " + ", ".join(f"{n} {s['right']}" for n, s in by_right))]

    # ---------------------------------------------------------------- the shared web board
    def site_state(self) -> dict:
        d = self.state.data
        cur = d["current"]
        top = sorted(d["stats"].items(), key=lambda kv: (-kv[1]["best"], -kv[1]["right"], kv[0]))[:10]
        return {
            **self.base_state(),
            "current": ({"no": cur["no"], "date": cur["date"], "q": cur["q"], "choices": cur["choices"], "cat": cur["cat"],
                         "answered": len(d["answers"])} if cur else None),       # the answer is never in here
            "history": d["history"][:7],
            "leaders": [{"name": n, "best": s["best"], "right": s["right"], "played": s["played"]} for n, s in top],
            "hour": self.hour,
        }
