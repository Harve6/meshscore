"""Polling cadence around kickoff: keep polling fast when a game's start time has passed but the feed has not flipped."""
import os
import sys
from datetime import timedelta
from types import SimpleNamespace as G

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from meshscore.core import CFG, now, scheduled_in  # noqa: E402
from meshscore.leagues.mlb import MlbModule  # noqa: E402
from meshscore.leagues.mlb_postseason import PostseasonModule  # noqa: E402
from meshscore.leagues.nfl import NflModule  # noqa: E402
from meshscore.leagues.nhl import NhlModule  # noqa: E402

MODULES = (NflModule, NhlModule, MlbModule, PostseasonModule)


def game(start_in_minutes, scheduled=True, live=False):
    return G(scheduled=scheduled, live=live, start=now() + timedelta(minutes=start_in_minutes))


def test_start_time_just_passed_keeps_the_fast_poll():
    for cls in MODULES:
        assert cls.next_delay(None, [game(-5)]) == CFG["poll_live_seconds"], cls.__name__


def test_far_off_games_still_poll_slowly():
    for cls in MODULES:
        assert cls.next_delay(None, [game(180)]) == CFG["poll_idle_seconds"], cls.__name__


def test_a_game_stuck_scheduled_for_hours_stops_the_fast_poll():
    for cls in MODULES:
        assert cls.next_delay(None, [game(-60 * 5)]) == CFG["poll_idle_seconds"], cls.__name__


def test_overdue_start_is_negative_seconds_and_started_games_are_ignored():
    secs = scheduled_in([game(-5), game(10), game(-1, scheduled=False)])
    assert len(secs) == 2 and min(secs) < 0
