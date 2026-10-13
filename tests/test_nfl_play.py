"""The NFL scoring-play lookup: ESPN's list can show the score from before the point-after."""
import asyncio
import os
import sys
from types import SimpleNamespace as G

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from meshscore.leagues import nfl  # noqa: E402


def lookup(plays, g):
    def fake(url, params=None):
        return {"scoringPlays": plays}
    old = (nfl.espn_get, nfl.NFL_PLAY_WAIT)
    nfl.espn_get, nfl.NFL_PLAY_WAIT = fake, 0
    try:
        return asyncio.run(nfl.NflModule.scoring_play(None, g, "LAR"))
    finally:
        nfl.espn_get, nfl.NFL_PLAY_WAIT = old


def play(team, away, home, text, kind="TD"):
    return {"team": {"abbreviation": team}, "awayScore": away, "homeScore": home, "text": text, "type": {"abbreviation": kind}}


G1 = G(id="1", away="BUF", home="LAR", ar=6, hr=7)


def test_the_play_is_found_when_the_list_has_caught_up():
    plays = [play("LAR", 6, 7, "Puka Nacua 3 Yd pass from Matthew Stafford (Harrison Mevis Kick)")]
    assert lookup(plays, G1)[0].startswith("Puka Nacua")


def test_a_list_that_still_shows_the_pre_kick_score_is_used_rather_than_nothing():
    plays = [play("LAR", 6, 6, "Puka Nacua 3 Yd pass from Matthew Stafford")]       # 6 on the list, 7 on the scoreboard
    assert lookup(plays, G1)[0].startswith("Puka Nacua")


def test_an_older_touchdown_is_never_described_as_the_new_one():
    plays = [play("LAR", 0, 7, "Old touchdown from the first quarter")]               # scoreboard is 14 now
    assert lookup(plays, G(id="1", away="BUF", home="LAR", ar=6, hr=14)) == (None, None)
