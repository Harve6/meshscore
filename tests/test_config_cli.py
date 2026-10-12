"""Config parsing/validation, the setup wizard, plugins and the routing of a built-in + example bot. Offline."""
import asyncio
import os
import textwrap

import pytest

from meshscore import config
from meshscore.cli import build_hub, run_init
from meshscore.config import ConfigError, parse, problems, render
from meshscore.core import CFG
from meshscore.leagues import load_plugin_dir, registry
from meshscore.radio import Incoming, Transport

GOOD = textwrap.dedent('''
    [radio]
    transport = "serial"
    port = "COM3"

    [bot]
    timezone = "America/Toronto"
    admin_names = ["Dad"]

    [bots.leafs]
    type = "nhl"
    team = "TOR"
    channel = "#leafs"

    [bots.jays]
    type = "mlb"
    team = "TOR"
    tag = "JAYS"
    channel = "#jays"
''')


@pytest.fixture(autouse=True)
def fresh():
    config.reset()
    yield
    config.reset()


def load(text, tmp_path):
    p = tmp_path / "meshscore.toml"
    p.write_text(text, encoding="utf-8")
    return config.load_config(str(p))


def test_good_config_loads(tmp_path):
    assert load(GOOD, tmp_path)
    assert CFG["transport"] == "serial" and CFG["port"] == "COM3" and CFG["admin_names"] == ["Dad"]
    assert set(CFG["modules"]) == {"leafs", "jays"} and CFG["modules"]["jays"]["tag"] == "JAYS"
    assert CFG["modules"]["leafs"]["enabled"] is True
    assert problems() == []


def test_missing_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MESHCORE_CONFIG", "")
    monkeypatch.setattr(config, "user_config_path", lambda: str(tmp_path / "nope" / "x.toml"))
    assert config.load_config() is None


@pytest.mark.parametrize("text, needle", [
    ("[radio\nport=1", "Not valid TOML"),
    ('[radoi]\nport = "x"', "Unknown section"),
    ('[radio]\nprot = "COM3"', "no setting called 'prot'"),
    ('[same_air]\njitter = 3', "no setting called 'jitter'"),
])
def test_bad_files_explain_themselves(text, needle, tmp_path):
    with pytest.raises(ConfigError, match=needle):
        parse(text)


def test_problems_are_specific():
    CFG.update(transport="serial", port="", modules={"a": {"type": "nhl", "channel": "#a"},
                                                      "b": {"type": "nope", "channel": "#b"},
                                                      "c": {"type": "nfl", "team": "BUF", "channel": ""}})
    msgs = "\n".join(problems())
    assert "needs port" in msgs and "[bots.a]" in msgs and "needs team" in msgs
    assert "unknown type 'nope'" in msgs and "[bots.c] needs channel" in msgs


def test_no_bots_is_a_problem():
    CFG.update(transport="tcp", host="1.2.3.4")
    assert any("no bots configured" in p for p in problems())


def test_render_round_trips():
    text = render({"transport": "tcp", "host": "10.0.0.5", "tcp_port": 5000},
                  {"tor": {"type": "nhl", "team": "TOR", "label": "Maple Leafs", "channel": "#leafs"}}, "America/Toronto")
    parsed = parse(text)
    assert parsed["host"] == "10.0.0.5" and parsed["timezone"] == "America/Toronto"
    assert parsed["modules"]["tor"]["team"] == "TOR"


def test_wizard_writes_a_loadable_file(tmp_path, monkeypatch):
    import meshscore.teams as teams
    monkeypatch.setattr(teams, "team_list", lambda kind: [("TOR", "Toronto Maple Leafs"), ("BOS", "Boston Bruins")])
    answers = iter(["1", "TOR", "#leafs", "n", "America/Toronto"])      # league 1 = NHL, team, channel, no more bots, tz
    out = tmp_path / "meshscore.toml"
    path = run_init(str(out), ask=lambda prompt="": next(answers), say=lambda *a: None,
                    radio={"transport": "serial", "port": "COM9"}, channels=["leafs", "public"])
    assert path == str(out)
    config.load_config(str(out), strict=True)
    assert CFG["port"] == "COM9" and CFG["timezone"] == "America/Toronto"
    assert CFG["modules"]["tor"]["type"] == "nhl" and CFG["modules"]["tor"]["label"] == "Toronto Maple Leafs"


def test_wizard_refuses_to_clobber(tmp_path):
    out = tmp_path / "meshscore.toml"
    out.write_text("keep me")
    assert run_init(str(out), ask=lambda p="": "n", say=lambda *a: None, radio={"transport": "serial", "port": "x"}, channels=[]) is None
    assert out.read_text() == "keep me"


# ---------------------------------------------------------------- plugins
PLUGINS = os.path.join(os.path.dirname(__file__), "..", "examples", "plugins")


class Quiet(Transport):
    name = "quiet"
    alive = True

    def __init__(self):
        self.sent = []

    async def send_channel(self, channel, text):
        self.sent.append((channel, text))
        return True


def test_example_plugin_loads_and_answers_commands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_plugin_dir(PLUGINS) >= 1
    assert "coinflip" in registry()
    CFG.update(send_gap=0, command_cooldown=0, log_file="",
               modules={"coin": {"enabled": True, "type": "coinflip", "channel": "#coin"}})

    async def go():
        t = Quiet()
        hub = build_hub(t)
        mod = hub.modules["coin"]
        await mod.poll()                                               # one flip -> alert at the default level (max)
        assert hub.out.qsize() == 1
        w = asyncio.create_task(hub.tx_worker())
        c = asyncio.create_task(hub.cmd_worker())
        for text in ("!score", "!help", "!mode min", "!coin score"):
            await hub.on_message(Incoming(text, False, "Tester", "", 1, 5.0, "coin"))
        await asyncio.sleep(0.5)
        w.cancel(); c.cancel()
        return t.sent

    sent = [x for _, x in asyncio.run(go())]
    assert any(x.startswith("🪙 Flip #1") for x in sent)
    assert sum(x.startswith("[COIN] heads") for x in sent) == 2          # "!score" and "!coin score"
    assert any("!score" in x and x.startswith("[COIN BOT]") for x in sent)
    assert any("Alert level set to min" in x for x in sent)


def test_built_in_types_are_all_registered():
    assert {"nhl", "mlb", "nfl", "ohl", "hockeytech", "postseason", "mlb-postseason"} <= set(registry())
