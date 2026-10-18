"""Mesh Word: the answer list, the scoring, and the bot (DM-only guesses, daily rollover, silent restarts)."""
import json

import pytest

from meshscore import config, core
from meshscore.core import CFG
from meshscore.dailyplay.words import ANSWERS, score, squares, valid_guess

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


def test_the_answer_list_is_clean():
    assert len(ANSWERS) >= 600 and len(set(ANSWERS)) == len(ANSWERS)
    assert all(len(w) == 5 and w.isalpha() and w.islower() and w.isascii() for w in ANSWERS), \
        [w for w in ANSWERS if not (len(w) == 5 and w.isalpha() and w.islower())]


@pytest.mark.parametrize("guess,answer,expect", [
    ("crane", "crane", "ggggg"),
    ("slate", "crane", "..g.g"),
    ("eerie", "crane", "..y.g"),          # the one E of CRANE is used by the green E; R is yellow
    ("speed", "abide", "..y.y"),          # duplicate letters: each answer letter is used once
    ("llama", "alarm", ".ggyy"),
])
def test_scoring_handles_repeated_letters(guess, answer, expect):
    assert score(guess, answer) == expect


def test_squares_and_validation():
    assert squares("gy.") == "🟩🟨⬛"
    assert valid_guess("Crane") and not any(valid_guess(x) for x in ("cran", "cranes", "cr4ne", "", "crané", None))


def test_the_daily_post_the_dm_only_guesses_and_the_recap(monkeypatch):
    set_time(monkeypatch, h=8, mi=5)

    async def go():
        async with DRig("word") as r:
            r.m.rng.seed(1)
            await r.tick()
            start = [x for x in r.t.sent if x.startswith("🔤 Mesh Word #1")]
            assert len(start) == 1 and "DM me: !guess crane" in start[0] and len(start[0].encode()) <= 130
            answer = r.m.state.data["puzzle"]["answer"]
            wrong = "crane" if answer != "crane" else "slate"
            # a guess on the channel is NOT taken
            await r.say(f"!guess {wrong}", name="Ann")
            assert "by DM" in r.t.sent[-1] and r.m.state.data["games"] == {}
            n = len(r.t.sent)
            await r.say(f"!guess {wrong}", name="Ann")
            assert len(r.t.sent) == n                                      # the pointer is not repeated straight away
            # by DM: a wrong guess, then the right one
            await r.say(f"!guess {wrong}", name="Ann", dm=True)
            assert r.last_dm().startswith("[WORD] #1 1/6") and wrong.upper() in r.last_dm()
            await r.say("!guess ab", name="Ann", dm=True)
            assert "Five letters" in r.last_dm()
            before = len(r.t.sent)
            await r.say(f"!guess {answer}", name="Ann", dm=True)
            assert "solved in 2/6" in r.t.dms[-2][1] and r.last_dm().startswith("Mesh Word #1 2/6")
            posted = r.t.sent[before:]
            assert posted == ["🔤 Ann solved Mesh Word #1 in 2/6"]            # who and how many - never a letter
            assert all(answer.upper() not in x and answer not in x for x in r.t.sent)
            await r.say("!guess crane", name="Ann", dm=True)               # already finished: shows the board
            assert "SOLVED" in r.last_dm()
            # a second player fails all six
            for w in ["slate", "crane", "pound", "wight", "fjord", "bumpy"]:
                if w == answer:
                    continue
                await r.say(f"!guess {w}", name="Bob", dm=True)
            # next morning: recap (answer revealed) + the new word
            set_time(monkeypatch, d=15, h=8, mi=5)
            r.take()
            await r.tick(86400)
            posts = r.take()
            assert posts[0].startswith(f"🔤 Mesh Word #1 was {answer.upper()}") and "1/2 solved: Ann 2" in posts[0]
            assert posts[1].startswith("🔤 Mesh Word #2")
            assert r.m.state.data["games"] == {} and r.m.state.data["history"][0]["answer"] == answer
    run(go())


def test_streaks_and_a_silent_restart(monkeypatch):
    async def go():
        set_time(monkeypatch, d=14, h=8, mi=5)
        async with DRig("word") as r:
            await r.tick()
            a1 = r.m.state.data["puzzle"]["answer"]
            await r.say(f"!guess {a1}", name="Ann", dm=True)
            set_time(monkeypatch, d=15, h=8, mi=5)
            await r.tick(86400)
            a2 = r.m.state.data["puzzle"]["answer"]
            await r.say(f"!guess {a2}", name="Ann", dm=True)
            assert r.m.state.data["stats"]["Ann"]["streak"] == 2 and r.m.state.data["stats"]["Ann"]["best"] == 2
            await r.say("!wordtop", name="Zed")
            assert "best streaks: Ann 2" in r.t.sent[-1]
        # a restart in the afternoon: nothing is posted, the game goes on
        set_time(monkeypatch, d=15, h=15, mi=0)
        async with DRig("word") as r2:
            assert r2.m.state.data["puzzle"]["no"] == 2
            await r2.tick()
            await r2.tick(3600)
            assert r2.t.sent == []
        # the bot was down at 8:00 the next day: the new word starts quietly (players find it with !word)
        set_time(monkeypatch, d=16, h=13, mi=0)
        async with DRig("word") as r3:
            await r3.tick()
            assert r3.t.sent == [] and r3.m.state.data["puzzle"]["no"] == 3
            await r3.say("!word", name="Ann", dm=True)
            assert r3.last_dm().startswith("[WORD] #3: 5 letters")
    run(go())


def test_the_web_state_never_contains_a_letter_of_todays_word(monkeypatch):
    set_time(monkeypatch, h=8, mi=5)

    async def go():
        async with DRig("word") as r:
            await r.tick()
            ans = r.m.state.data["puzzle"]["answer"]
            await r.say("!guess slate" if ans != "slate" else "!guess crane", name="Ann", dm=True)
            await r.say(f"!guess {ans}", name="Bob", dm=True)
            s = json.dumps(r.m.site_state())
            assert ans not in s and "slate" not in s.lower() or ans == "slate"
            today = {t["name"]: t for t in r.m.site_state()["today"]}
            assert today["Bob"]["tries"] == 1 and today["Bob"]["grid"] == ["🟩🟩🟩🟩🟩"]
            assert today["Ann"]["tries"] is None and "grid" not in today["Ann"]          # in progress: no grid yet
    run(go())


def test_solved_posts_are_capped_per_day(monkeypatch):
    set_time(monkeypatch, h=8, mi=5)

    async def go():
        async with DRig("word", max_announce=2) as r:
            await r.tick()
            ans = r.m.state.data["puzzle"]["answer"]
            r.take()
            for who in ("A1", "B2", "C3", "D4"):
                await r.say(f"!guess {ans}", name=who, dm=True)
            assert len([x for x in r.t.sent if "solved Mesh Word" in x]) == 2
    run(go())
