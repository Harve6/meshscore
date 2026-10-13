"""The postseason 'G2 in 25m' heads-up is sent once, even across restarts."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from meshscore import config, core
from meshscore.cli import build_hub
from meshscore.core import CFG
from meshscore.radio import Transport


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, command_cooldown=0, log_file="", timezone="America/Toronto")
    core.init_tz()
    yield
    config.reset()


class Quiet(Transport):
    name = "quiet"
    alive = True


def make():
    CFG["modules"] = {"mlb": {"enabled": True, "type": "mlb-postseason", "channel": "#jays"}}
    m = build_hub(Quiet()).modules["mlb"]
    sent = []
    m.emit = lambda kind, texts, g=None, *a, **k: sent.append((kind, texts))
    m.tier_of = lambda g: "full"

    async def no_pre(g):
        return ""
    m.series_pre = no_pre
    return m, sent


def game(pk):
    return SimpleNamespace(scheduled=True, pk=pk, start=core.now() + timedelta(minutes=20), sdesc="ALCS", gtype="L",
                           sgame=2, away="CLE", home="TB", away_sp="", home_sp="")


def test_headsup_is_not_repeated_after_a_restart():
    m, sent = make()
    asyncio.run(m.check_headsup([game(7001)]))
    asyncio.run(m.check_headsup([game(7001)]))
    assert len(sent) == 1 and "G2 in" in sent[0][1][0]
    m2, sent2 = make()                                              # restart: a fresh module on the same state file
    asyncio.run(m2.check_headsup([game(7001)]))
    assert sent2 == []
    asyncio.run(m2.check_headsup([game(7002)]))                     # a different game still gets its heads-up
    assert len(sent2) == 1
