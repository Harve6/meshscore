"""meshscore health / backup / run --forever: the tools for running MeshScore without help."""
import time
import zipfile
from datetime import datetime

import pytest

from meshscore import cli, config, ops


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    yield
    config.reset()


def line(t, text, level="INFO"):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") + f" {level} {text}\n"


NOW = datetime(2026, 10, 18, 12, 0, 0).timestamp()


def write_log(tmp_path, *entries):
    (tmp_path / "meshscore.log").write_text("".join(line(t, x, lv) for t, x, lv in entries), encoding="utf-8")


def levels(checks):
    return {c.name: c.level for c in checks}


def test_backup_has_settings_and_state_but_never_roms_and_keeps_the_newest(tmp_path):
    for n, body in (("meshscore.toml", "x"), ("chess_state.json", "{}"), ("pokemon-red.state", "s"),
                    ("Pokemon Red.gb", "ROM"), ("meshscore.log", "log")):
        (tmp_path / n).write_text(body)
    t = [1_800_000_000]
    paths = []
    for i in range(4):
        t[0] += 60
        paths.append(ops.make_backup(str(tmp_path), keep=3, clock=lambda: t[0]))
    names = zipfile.ZipFile(paths[-1]).namelist()
    assert sorted(names) == ["chess_state.json", "meshscore.toml", "pokemon-red.state"]
    assert ops.list_backups(str(tmp_path / "backups")) == sorted(paths[1:], reverse=True)    # the oldest was pruned


def test_restore_puts_files_back_and_keeps_the_replaced_ones(tmp_path):
    (tmp_path / "chess_state.json").write_text("old")
    zp = ops.make_backup(str(tmp_path))
    (tmp_path / "chess_state.json").write_text("broken")
    assert ops.restore_backup(zp, str(tmp_path)) == ["chess_state.json"]
    assert (tmp_path / "chess_state.json").read_text() == "old"
    assert (tmp_path / "chess_state.json.bak").read_text() == "broken"


def test_health_all_good(tmp_path):
    write_log(tmp_path, (NOW - 3000, "Connected. Channels: #chess=slot 8", "INFO"), (NOW - 60, "TX OK [#chess]: hi", "INFO"))
    ops.make_backup(str(tmp_path))
    cfg = {"modules": {"chess": {"site_url": "https://c.test/"}}}
    checks = ops.health(str(tmp_path), cfg, now=NOW, fetch=lambda u: {"updated": NOW - 300}, running=lambda: True)
    lv = levels(checks)
    assert lv["bot running"] == lv["radio link"] == lv["sending"] == lv["board chess"] == lv["backups"] == "ok"
    assert "All good" in ops.report(checks) and ops.summary_line(checks) == "all good"


def test_health_spots_the_unplugged_radio_and_the_silent_day(tmp_path):
    write_log(tmp_path,
              (NOW - 90000, "Connected. Channels: #ohl=slot 1", "INFO"),
              (NOW - 80000, "TX OK [#ohl]: a", "INFO"),
              (NOW - 70000, "TX FAIL [#ohl]: b", "INFO"),
              (NOW - 60000, "TX FAIL [#ohl]: c", "INFO"),
              (NOW - 50000, "TX FAIL [#ohl]: d", "INFO"),
              (NOW - 600, "Transport error: Cannot configure port ... 'A device attached to the system is not functioning.'", "ERROR"))
    checks = ops.health(str(tmp_path), {}, now=NOW, running=lambda: True)
    lv = levels(checks)
    assert lv["radio link"] == "fail" and lv["sending"] == "fail"
    radio = next(c for c in checks if c.name == "radio link")
    assert "unplug the radio" in radio.detail and "10 min ago" in radio.detail
    assert "radio link" in ops.summary_line(checks)


def test_health_when_the_bot_is_not_running_and_a_board_is_stale(tmp_path):
    write_log(tmp_path, (NOW - 100, "Connected. Channels: #x=slot 1", "INFO"))
    cfg = {"modules": {"pokemon": {"site_url": "https://p.test/"}, "off": {"site_url": "https://o.test/", "enabled": False}}}
    checks = ops.health(str(tmp_path), cfg, now=NOW, fetch=lambda u: {"updated": NOW - 7200}, running=lambda: False)
    lv = levels(checks)
    assert lv["bot running"] == "fail" and lv["board pokemon"] == "warn" and "board off" not in lv


def test_cli_health_exit_codes(tmp_path, monkeypatch, capsys):
    (tmp_path / "meshscore.toml").write_text('[radio]\ntransport = "serial"\nport = "COM3"\n[bots.x]\ntype = "nhl"\nteam = "TOR"\nchannel = "#l"\n')
    write_log(tmp_path, (time.time() - 100, "Connected. Channels: #l=slot 1", "INFO"), (time.time() - 50, "TX OK [#l]: x", "INFO"))
    monkeypatch.setattr(ops, "bot_running", lambda port: True)
    args = cli.build_parser().parse_args(["health", "--summary"])
    assert cli.cmd_health(args) == 0 and "all good" in capsys.readouterr().out
    monkeypatch.setattr(ops, "bot_running", lambda port: False)
    assert cli.cmd_health(cli.build_parser().parse_args(["health"])) == 1


def test_run_forever_restarts_after_an_error_and_stops_on_ctrl_c(tmp_path, monkeypatch):
    (tmp_path / "meshscore.toml").write_text('[radio]\ntransport = "serial"\nport = "COM3"\n[bot]\nlog_file = ""\n'
                                             '[bots.x]\ntype = "nhl"\nteam = "TOR"\nchannel = "#l"\n')
    calls = {"n": 0}

    async def fake_amain(transport):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "amain", fake_amain)
    monkeypatch.setattr(cli, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    args = cli.build_parser().parse_args(["run", "--forever"])
    assert cli.cmd_run(args) == 0 and calls["n"] == 2                       # crashed once, came back, then stopped
    calls["n"] = 0
    args = cli.build_parser().parse_args(["run"])
    assert cli.cmd_run(args) == 1 and calls["n"] == 1                       # without --forever an error ends it
