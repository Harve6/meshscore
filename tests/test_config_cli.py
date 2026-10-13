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


# ---------------------------------------------------------------- changing bots in an existing file
from meshscore import cli  # noqa: E402

COMMENTED = '''# my setup
[radio]
transport = "serial"
port = "COM3"   # the left USB port

[bots.leafs]       # the Leafs
type = "nhl"
team = "TOR"
channel = "#leafs"

# [bots.old]
# type = "nfl"

[bots.jays]
type = "mlb"
team = "TOR"
tag = "JAYS"
channel = "#jays"
'''


def test_block_finder_ignores_commented_tables():
    assert list(config.bot_blocks(COMMENTED)) == ["leafs", "jays"]


def test_add_keeps_everything_else_byte_for_byte():
    new = config.add_bot_text(COMMENTED, "mlb", {"type": "mlb-postseason", "channel": "#jays"})
    assert new.startswith(COMMENTED.rstrip("\n"))               # comments, spacing, the commented-out table: untouched
    assert parse(new)["modules"]["mlb"] == {"enabled": True, "type": "mlb-postseason", "channel": "#jays"}
    with pytest.raises(ConfigError, match="already"):
        config.add_bot_text(new, "mlb", {"type": "nfl"})


def test_remove_takes_only_that_table():
    new = config.remove_bot_text(COMMENTED, "leafs")
    assert list(config.bot_blocks(new)) == ["jays"] and "# [bots.old]" in new and "my setup" in new
    assert "TOR" in new                                         # the jays table still has its team
    with pytest.raises(ConfigError, match="No bot called"):
        config.remove_bot_text(COMMENTED, "nope")


def test_disable_and_enable_are_idempotent():
    off = config.set_enabled_text(COMMENTED, "jays", False)
    assert parse(off)["modules"]["jays"]["enabled"] is False and parse(off)["modules"]["leafs"]["enabled"] is True
    assert config.set_enabled_text(off, "jays", False).count("enabled = false") == 1
    on = config.set_enabled_text(off, "jays", True)
    assert parse(on)["modules"]["jays"]["enabled"] is True and "enabled = false" not in on


def test_save_edited_refuses_a_result_that_would_not_load(tmp_path):
    p = tmp_path / "meshscore.toml"
    p.write_text(COMMENTED, encoding="utf-8")
    bad = config.add_bot_text(COMMENTED, "x", {"type": "nhl", "channel": "#x"})             # nhl needs a team
    with pytest.raises(ConfigError, match="needs team"):
        config.save_edited(str(p), bad)
    assert p.read_text(encoding="utf-8") == COMMENTED                                       # untouched
    good = config.add_bot_text(COMMENTED, "x", {"type": "nhl", "team": "BOS", "channel": "#x"})
    config.save_edited(str(p), good)
    assert (tmp_path / "meshscore.toml.bak").read_text(encoding="utf-8") == COMMENTED       # the previous file is kept
    assert "[bots.x]" in p.read_text(encoding="utf-8")


def args(**kw):
    import argparse
    d = dict(action="list", target=None, type=None, team=None, channel=None, name=None, tag=None, label=None, config=None)
    d.update(kw)
    return argparse.Namespace(**d)


def test_bots_commands_end_to_end(tmp_path, capsys):
    p = tmp_path / "meshscore.toml"
    p.write_text(COMMENTED, encoding="utf-8")
    c = str(p)
    assert cli.cmd_bots(args(config=c)) == 0
    assert "leafs" in capsys.readouterr().out
    # the postseason bot on #jays, where a Jays team bot already is: its games are skipped there
    assert cli.cmd_bots(args(action="add", type="mlb-postseason", channel="#jays", name="mlb", config=c)) == 0
    out = capsys.readouterr().out
    assert "skip_teams" in out and "Restart" in out
    cfg = parse(p.read_text(encoding="utf-8"))["modules"]["mlb"]
    assert cfg["skip_teams"] == ["TOR"] and cfg["channel"] == "#jays"
    assert cli.cmd_bots(args(action="disable", target="mlb", config=c)) == 0
    assert parse(p.read_text(encoding="utf-8"))["modules"]["mlb"]["enabled"] is False
    assert cli.cmd_bots(args(action="remove", target="mlb", config=c)) == 0
    assert "mlb" not in parse(p.read_text(encoding="utf-8"))["modules"]
    assert cli.cmd_bots(args(action="remove", target="ghost", config=c)) == 2            # a clear refusal, file untouched
    assert cli.cmd_bots(args(action="enable", config=c)) == 2                            # name missing


def test_adding_a_team_bot_asks_for_team_and_channel(tmp_path, monkeypatch, capsys):
    import meshscore.teams as teams
    monkeypatch.setattr(teams, "team_list", lambda kind: [("NYY", "New York Yankees"), ("TOR", "Toronto Blue Jays")])
    text = COMMENTED
    name, fields = cli.add_bot_interactive(text, args(type="mlb"), ask=(lambda it: lambda prompt="": next(it))(iter(["nyy", "yankees"])), say=lambda *a: None)
    assert name == "nyy" and fields == {"type": "mlb", "label": "New York Yankees", "team": "NYY", "channel": "#yankees"}


def test_wizard_offers_postseason_on_a_team_channel(tmp_path, monkeypatch):
    import meshscore.teams as teams
    monkeypatch.setattr(teams, "team_list", lambda kind: [("TOR", "Toronto Blue Jays"), ("NYY", "New York Yankees")])
    # league 2 = MLB team, team, channel, yes to the postseason on it, no more bots, tz
    answers = iter(["2", "TOR", "#jays", "y", "n", "America/Toronto"])
    out = tmp_path / "meshscore.toml"
    run_init(str(out), ask=lambda prompt="": next(answers), say=lambda *a: None,
             radio={"transport": "serial", "port": "COM9"}, channels=["jays"])
    config.load_config(str(out), strict=True)
    assert CFG["modules"]["tor"]["type"] == "mlb"
    ps = CFG["modules"]["mlb"]
    assert ps["type"] == "mlb-postseason" and ps["channel"] == "#jays" and ps["skip_teams"] == ["TOR"]


def test_bots_add_postseason_flag_adds_both(tmp_path, capsys):
    p = tmp_path / "meshscore.toml"
    p.write_text(COMMENTED, encoding="utf-8")
    c = str(p)
    ns = args(action="add", type="mlb", team="NYY", channel="#yankees", name="nyy", config=c)
    ns.postseason = True
    assert cli.cmd_bots(ns) == 0
    mods = parse(p.read_text(encoding="utf-8"))["modules"]
    assert mods["nyy"]["type"] == "mlb"
    assert mods["mlb"]["type"] == "mlb-postseason" and mods["mlb"]["channel"] == "#yankees" and mods["mlb"]["skip_teams"] == ["NYY"]
    nhl = args(action="add", type="nhl", team="BOS", channel="#bruins", name="bos", config=c)
    nhl.postseason = True                                           # only an MLB team bot can take it
    assert cli.cmd_bots(nhl) == 2 and "bos" not in parse(p.read_text(encoding="utf-8"))["modules"]


def test_adding_a_team_bot_makes_an_existing_postseason_bot_skip_it(tmp_path, capsys):
    p = tmp_path / "meshscore.toml"
    p.write_text('[radio]\nport = "COM3"\n\n[bots.mlb]\ntype = "mlb-postseason"\nchannel = "#jays"\n', encoding="utf-8")
    c = str(p)
    ns = args(action="add", type="mlb", team="TOR", channel="#jays", name="jays", config=c)
    ns.postseason = True                                            # already has one: no second postseason bot
    assert cli.cmd_bots(ns) == 0
    mods = parse(p.read_text(encoding="utf-8"))["modules"]
    assert mods["mlb"]["skip_teams"] == ["TOR"] and mods["jays"]["type"] == "mlb"
    assert [n for n, b in mods.items() if b.get("type") == "mlb-postseason"] == ["mlb"]
