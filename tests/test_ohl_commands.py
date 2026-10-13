"""The OHL bot answers the same score commands as the other leagues (`!score` used to be missing here)."""
import pytest

from meshscore import config
from meshscore.cli import build_hub
from meshscore.core import CFG
from meshscore.radio import Incoming, Transport


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, command_cooldown=0, log_file="")
    yield
    config.reset()


class Quiet(Transport):
    name = "quiet"
    alive = True


def test_ohl_score_today_and_scores_all_reach_the_scoreboard():
    CFG["modules"] = {"kit": {"enabled": True, "type": "ohl", "team": "KIT", "channel": "#ohl"},
                      "tor": {"enabled": True, "type": "nhl", "team": "TOR", "channel": "#leafs"}}
    hub = build_hub(Quiet())
    for word in ("!score", "!scores", "!today", "!schedule"):
        routed = hub.route(Incoming(word, False, "Ann", "", 1, 5.0, "ohl"))
        assert routed is not None, word
        mod, cmd, _ = routed
        assert mod.name == "kit" and mod.commands[cmd] == mod.cmd_scores, word
    assert hub.route(Incoming("!score", False, "Ann", "", 1, 5.0, "leafs"))[0].name == "tor"     # unchanged
