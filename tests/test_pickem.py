"""Pick'em: parsing ESPN's feed, DM-only picks, locking at the start, scoring and the recap."""
import json

import pytest

from meshscore import config, core
from meshscore.core import CFG
from meshscore.dailyplay.espn import Game, parse_scoreboard
from meshscore.leagues import pickembot

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


def ev(i, start, state="pre", name="STATUS_SCHEDULED", home=("TOR", 0, None), away=("NYI", 0, None)):
    def team(ha, t):
        d = {"homeAway": ha, "team": {"abbreviation": t[0]}, "score": str(t[1])}
        if t[2] is not None:
            d["winner"] = t[2]
        return d
    return {"id": str(i), "date": start, "status": {"type": {"state": state, "name": name, "completed": state == "post"}},
            "competitions": [{"competitors": [team("home", home), team("away", away)]}]}


def test_parse_scoreboard_states_winners_and_garbage():
    data = {"events": [
        ev(1, "2026-10-14T23:00Z"),
        ev(2, "2026-10-14T23:30Z", "in", home=("BUF", 2, None), away=("BOS", 1, None)),
        ev(3, "2026-10-14T23:00Z", "post", "STATUS_FINAL", home=("CAR", 4, True), away=("CGY", 2, False)),
        ev(4, "2026-10-14T23:00Z", "post", "STATUS_FINAL", home=("A", 1, None), away=("B", 3, None)),       # no winner flag
        ev(5, "2026-10-14T23:00Z", "pre", "STATUS_POSTPONED"),
        ev(6, "2026-10-14T23:00Z", "post", "STATUS_FINAL", home=("X", 2, None), away=("Y", 2, None)),       # a tie
        {"id": "7"}, {"events": 3}, "junk"]}
    g = {x.id: x for x in parse_scoreboard(data, "nhl")}
    assert list(g) == ["1", "2", "3", "4", "5", "6"]
    assert g["1"].state == "pre" and (g["1"].home, g["1"].away) == ("TOR", "NYI") and g["1"].start > 1_700_000_000
    assert g["2"].state == "in" and (g["2"].home_score, g["2"].away_score) == (2, 1)
    assert g["3"].winner == "CAR" and g["4"].winner == "B" and g["5"].state == "void" and g["6"].state == "void"
    assert parse_scoreboard(None, "nhl") == [] and parse_scoreboard({"events": None}, "nhl") == []


def games(state1="pre", state2="pre", w1=None, w2=None):
    base = 1_800_000_000 + 40000
    return [Game("1", "nhl", base, state1, "TOR", "NYI", 3 if w1 else 0, 1 if w1 else 0, w1),
            Game("2", "nhl", base + 1800, state2, "BUF", "BOS", 0, 2 if w2 else 0, w2)]


def test_a_full_day_slate_dm_picks_lock_scoring_and_recap(monkeypatch):
    set_time(monkeypatch, d=14, h=9, mi=5)

    async def go():
        async with DRig("pickem") as r:
            r.m.fetch = lambda league, day: games()
            await r.tick()
            slate = [x for x in r.t.sent if x.startswith("🎯 Pick'em:")]
            assert len(slate) == 1 and "NYI@TOR" in slate[0] and "BOS@BUF" in slate[0] and "DM !pick TOR" in slate[0]
            assert len(slate[0].encode()) <= 130
            await r.say("!pick TOR", name="Ann")                                         # the channel: not taken
            assert "by DM" in r.t.sent[-1] and r.m.state.data["slates"][0]["picks"] == {}
            await r.say("!pick TOR", name="Ann", dm=True)
            assert "✓ TOR in NYI@TOR" in r.last_dm() and "1/2 picked" in r.last_dm()
            await r.say("!pick BOS", name="Ann", dm=True)
            await r.say("!pick NYI", name="Bob", dm=True)
            await r.say("!pick BUF", name="Bob", dm=True)
            await r.say("!pick EDM", name="Bob", dm=True)
            assert "No EDM game today" in r.last_dm() and "TOR" in r.last_dm()
            await r.say("!picks", name="Ann", dm=True)
            assert "NYI@TOR" in r.last_dm() and "TOR" in r.last_dm()
            # game 1 starts: picks lock
            r.m.fetch = lambda league, day: games("in")
            await r.tick(600)
            await r.say("!pick TOR", name="Cy", dm=True)
            assert "picks are locked" in r.last_dm()
            await r.say("!pick NYI", name="Ann", dm=True)                                # changing a pick after the start
            assert "locked" in r.last_dm()
            # both games end: TOR won, BUF lost
            r.take()
            r.m.fetch = lambda league, day: games("post", "post", "TOR", "BOS")
            await r.tick(20000)
            recap = [x for x in r.t.sent if x.startswith("🎯 Pick'em 10-14")]
            assert len(recap) == 1 and "Ann 2/2" in recap[0] and "Bob 0/2" in recap[0]
            st = r.m.state.data["stats"]
            assert st["Ann"]["season"] == 2 and st["Bob"]["season"] == 0 and st["Bob"]["picks"] == 2
            await r.tick(20000)
            assert len([x for x in r.t.sent if "Pick'em 10-14" in x]) == 1               # recapped once
            await r.say("!picktop", name="Zed")
            assert "season: Ann 2" in r.t.sent[-1]
    run(go())


def test_team_filter_restart_silence_and_feed_errors(monkeypatch):
    set_time(monkeypatch, d=14, h=15, mi=0)

    async def go():
        async with DRig("pickem", teams=["BUF"]) as r:
            r.m.fetch = lambda league, day: games()
            await r.tick()
            assert [g["home"] for g in r.m.state.data["slates"][0]["games"].values()] == ["BUF"]
            assert r.t.sent == []                                                        # outside the posting hour: quiet

            def boom(league, day):
                raise OSError("feed down")
            r.m.fetch = boom
            await r.tick(1000)                                                           # a dead feed never raises
            assert r.m.state.data["slates"][0]["games"]
        async with DRig("pickem", teams=["BUF"]) as r2:                                  # restart: nothing re-posted
            r2.m.fetch = lambda league, day: games()
            await r2.tick()
            await r2.tick(4000)
            assert r2.t.sent == []
    run(go())


def test_no_games_a_void_game_and_the_web_state(monkeypatch):
    set_time(monkeypatch, d=14, h=9, mi=5)

    async def go():
        async with DRig("pickem") as r:
            r.m.fetch = lambda league, day: []
            await r.tick()
            assert r.t.sent == []
            await r.say("!pick TOR", name="Ann", dm=True)
            assert "No games on today's slate" in r.last_dm()
            g = games()
            g[0].state = "void"
            r.m.fetch = lambda league, day: g
            await r.tick(400)
            await r.say("!pick TOR", name="Ann", dm=True)
            assert "started" in r.last_dm() or "locked" in r.last_dm()
            await r.say("!pick BUF", name="Ann", dm=True)
            st = r.m.site_state()
            json.dumps(st)
            assert st["games"][1]["picks"] == 1 and {x["away"] for x in st["games"]} == {"NYI", "BOS"}
    run(go())


def test_clock_text_is_short():
    t = pickembot.clock_text(1_800_000_000 + 40000)
    assert t.endswith(("a", "p")) and len(t) <= 6


def test_a_team_in_two_leagues_asks_which_one(monkeypatch):
    set_time(monkeypatch, d=14, h=9, mi=5)

    async def go():
        async with DRig("pickem", leagues=["nhl", "mlb"]) as r:
            base = 1_800_000_000 + 40000
            both = [Game("1", "nhl", base, "pre", "TOR", "NYI"), Game("2", "mlb", base + 600, "pre", "TOR", "BOS")]
            r.m.fetch = lambda league, day: [g for g in both if g.league == league]
            await r.tick()
            await r.say("!pick TOR", name="Ann", dm=True)
            assert "plays twice" in r.last_dm() and "!pick nhl:TOR" in r.last_dm() and "!pick mlb:TOR" in r.last_dm()
            assert r.m.state.data["slates"][0]["picks"] == {}
            await r.say("!pick mlb:tor", name="Ann", dm=True)
            assert "✓ TOR in BOS@TOR" in r.last_dm()
            assert r.m.state.data["slates"][0]["picks"] == {"Ann": {"2": "TOR"}}
    run(go())
