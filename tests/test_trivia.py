"""Mesh Trivia: the question bank (every post fits a radio packet) and the bot (DM-only answers, recap, streaks)."""
import json

import pytest

from meshscore import config, core
from meshscore.core import CFG
from meshscore.dailyplay.trivia_bank import QUESTIONS
from meshscore.leagues.triviabot import format_question

from daily_rig import DRig, run, set_time


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, command_cooldown=0, log_file="", timezone="America/Toronto")
    core.init_tz()
    yield
    monkeypatch.setattr(core, "CLOCK", None)
    config.reset()


def test_the_bank_is_well_formed_and_every_question_fits_one_message():
    assert len(QUESTIONS) >= 100
    seen = set()
    for cat, q, right, wrong in QUESTIONS:
        assert len(wrong) == 3 and len({right, *wrong}) == 4, q
        assert q not in seen, q
        seen.add(q)
        assert len(format_question(99, q, [right, *wrong]).encode()) <= 130, q
    assert {c for c, *_ in QUESTIONS} >= {"hockey", "baseball", "football", "sport", "canada", "science", "radio"}


def test_daily_post_dm_answers_recap_and_streak(monkeypatch):
    set_time(monkeypatch, d=14, h=12, mi=5)

    async def go():
        async with DRig("trivia") as r:
            r.m.rng.seed(3)
            await r.tick()
            post = [x for x in r.t.sent if x.startswith("🧠 #1")]
            assert len(post) == 1 and len(post[0].encode()) <= 130
            cur = r.m.state.data["current"]
            right = "ABCD"[cur["answer"]]
            wrong = "ABCD"[(cur["answer"] + 1) % 4]
            await r.say(f"!trivia {right}", name="Ann")                    # on the channel: not taken
            assert "by DM" in r.t.sent[-1] and r.m.state.data["answers"] == {}
            await r.say(f"!trivia {right.lower()}", name="Ann", dm=True)
            assert "Correct" in r.last_dm() and "streak 1" in r.last_dm()
            await r.say(f"!trivia {wrong}", name="Ann", dm=True)
            assert "already answered" in r.last_dm()
            await r.say(f"!trivia {wrong}", name="Bob", dm=True)
            assert r.last_dm().startswith("[TRIVIA] ✗ Not") and "tomorrow" in r.last_dm()
            await r.say("!trivia", name="Ann", dm=True)                    # no answer given: shows the question
            assert "A)" in r.last_dm() and "B)" in r.last_dm()
            await r.say("!trivia Z", name="Cy", dm=True)
            assert "Answer with a letter" in r.last_dm()
            await r.say(f"!trivia {cur['choices'][cur['answer']]}", name="Cy", dm=True)   # the text of the choice works
            assert "Correct" in r.last_dm()
            s = json.dumps(r.m.site_state())
            assert cur["choices"][cur["answer"]] in s                       # the choices are public ...
            assert '"answer"' not in s and "q_index" not in s               # ... but which one is right is not
            # next day: recap with the answer, a new question, streaks
            set_time(monkeypatch, d=15, h=12, mi=5)
            r.take()
            await r.tick(86400)
            posts = r.take()
            assert posts[0].startswith("🧠 #1 answer: ") and "2/3 got it" in posts[0]
            assert posts[1].startswith("🧠 #2")
            c2 = r.m.state.data["current"]
            await r.say(f"!trivia {'ABCD'[c2['answer']]}", name="Ann", dm=True)
            assert "streak 2" in r.last_dm()
            await r.say("!trivtop", name="Zed")
            assert "best streaks: Ann 2" in r.t.sent[-1]
    run(go())


def test_restart_is_silent_and_questions_do_not_repeat_until_the_bank_is_used_up(monkeypatch):
    set_time(monkeypatch, d=14, h=15, mi=0)

    async def go():
        async with DRig("trivia") as r:
            await r.tick()
            assert r.t.sent == [] and r.m.state.data["current"]["no"] == 1      # first start outside the hour: quiet
            q1 = r.m.state.data["current"]["q_index"]
        async with DRig("trivia") as r2:
            await r2.tick()
            await r2.tick(3600)
            assert r2.t.sent == [] and r2.m.state.data["current"]["q_index"] == q1
            seen = {q1}
            for day in range(15, 15 + len(QUESTIONS) - 1):
                r2.m.state.data["current"]["date"] = "2000-01-01"
                set_time(monkeypatch, d=14, h=15, mi=0)
                r2.m._finish()
                r2.m._new_question(r2.m.today())
                seen.add(r2.m.state.data["current"]["q_index"])
            assert len(seen) == len(QUESTIONS)                                   # every question exactly once
    run(go())


def test_categories_can_be_limited(monkeypatch):
    set_time(monkeypatch, d=14, h=12, mi=5)

    async def go():
        async with DRig("trivia", categories=["hockey"]) as r:
            for _ in range(12):
                r.m._new_question("2026-10-14")
                assert r.m.state.data["current"]["cat"] == "hockey"
    run(go())
