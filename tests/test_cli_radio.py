"""The command line (run / check / init pickers / teams / dump) and the MeshCore link, against a fake `meshcore`
library and canned HTTP. No radio, no network."""
import asyncio
import gzip
import json
import os
import sys
import types

import pytest

from meshscore import cli, config
from meshscore.core import CFG
from meshscore.radio import Incoming, LibTransport

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MESHSCORE_CONFIG", raising=False)
    config.reset()
    yield
    config.reset()
    cli._instance_lock and cli._instance_lock.close()
    cli._instance_lock = None


GOOD = '''
[radio]
transport = "serial"
port = "COM3"

[bot]
instance_port = 0

[bots.leafs]
type = "nhl"
team = "TOR"
channel = "#leafs"
'''


def write(tmp_path, text=GOOD, name="meshscore.toml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------- main() dispatch
def test_bare_command_without_config_explains_what_to_do(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "user_config_path", lambda: str(tmp_path / "none" / "x.toml"))
    assert cli.main([]) == 0
    assert "meshscore init" in capsys.readouterr().out


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0 and "meshscore" in capsys.readouterr().out


def test_run_without_a_config_file_is_a_clear_error(capsys, tmp_path):
    assert cli.main(["run", "-c", str(tmp_path / "missing.toml")]) == 2
    assert "meshscore init" in capsys.readouterr().out


def test_run_with_an_invalid_config_lists_every_problem(capsys, tmp_path):
    p = write(tmp_path, '[radio]\ntransport = "serial"\n[bots.x]\ntype = "nhl"\nchannel = "#x"\n')
    assert cli.main(["run", "-c", p]) == 2
    out = capsys.readouterr().out
    assert "needs port" in out and "needs team" in out


def test_a_typo_in_the_config_is_reported_not_ignored(capsys, tmp_path):
    p = write(tmp_path, GOOD.replace("[bot]", "[bots]\n[bot]") .replace("instance_port", "instanse_port"))
    assert cli.main(["run", "-c", p]) == 2
    assert "instanse_port" in capsys.readouterr().out


def test_second_copy_on_one_computer_is_refused(capsys, tmp_path):
    p = write(tmp_path, GOOD.replace("instance_port = 0", "instance_port = 47690"))
    assert cli.main(["check", "--offline", "-c", p]) in (0, 1)       # (check does not take the lock)
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 47690))
    try:
        assert cli.main(["run", "-c", p]) == 1
        assert "already running" in capsys.readouterr().out
    finally:
        s.close()


def test_run_starts_the_bots_and_stops_on_ctrl_c(monkeypatch, tmp_path):
    p = write(tmp_path)
    seen = {}

    async def fake_amain(transport):
        hub = cli.build_hub(transport)
        seen["bots"] = {n: type(m).__name__ for n, m in hub.modules.items()}
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "amain", fake_amain)
    assert cli.main(["run", "-c", p, "--data-dir", str(tmp_path / "data")]) == 0
    assert seen["bots"] == {"leafs": "NhlModule"}
    assert os.path.isdir(tmp_path / "data")                              # state and log go next to the data dir


def test_unknown_bot_type_is_a_config_error_not_a_crash(tmp_path):
    p = write(tmp_path, GOOD.replace('type = "nhl"', 'type = "cricket"'))
    config.load_config(p)
    with pytest.raises(config.ConfigError):
        cli.build_hub(types.SimpleNamespace(name="x", alive=True))


# ---------------------------------------------------------------- radio pickers (scripted input)
def scripted(*answers):
    it = iter(answers)
    return lambda prompt="": next(it)


def test_pick_serial_by_number_and_by_name(monkeypatch):
    ports = [types.SimpleNamespace(device="COM3", description="CP210x"), types.SimpleNamespace(device="COM7", description="USB")]
    monkeypatch.setattr(cli, "list_ports", lambda: ports)
    assert cli.pick_serial(scripted("2"), lambda *a: None) == {"transport": "serial", "port": "COM7"}
    assert cli.pick_serial(scripted("com5"), lambda *a: None) == {"transport": "serial", "port": "COM5"}
    assert cli.pick_serial(scripted("/dev/ttyUSB1"), lambda *a: None) == {"transport": "serial", "port": "/dev/ttyUSB1"}
    assert cli.pick_serial(scripted("b"), lambda *a: None) is None
    assert cli.pick_serial(scripted("q"), lambda *a: None) is False


def test_pick_wifi_checks_reachability(monkeypatch):
    calls = []

    def fake_conn(addr, timeout=0):
        calls.append(addr)
        if addr[0] == "10.0.0.9":
            raise OSError("no route")
        return types.SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(cli.socket, "create_connection", fake_conn)
    assert cli.pick_wifi(scripted("10.0.0.5", ""), lambda *a: None) == {"transport": "tcp", "host": "10.0.0.5", "tcp_port": 5000}
    # unreachable: asked whether to use it anyway; "n" loops, then a good address
    assert cli.pick_wifi(scripted("10.0.0.9", "5001", "n", "10.0.0.5", "5001"), lambda *a: None)["host"] == "10.0.0.5"
    assert cli.pick_wifi(scripted("10.0.0.9", "", "y"), lambda *a: None)["host"] == "10.0.0.9"


def test_pick_ble_lists_only_mesh_radios_by_default(monkeypatch):
    rows = [("Heltec V3 abc", "AA:BB", -50), ("JBL Speaker", "CC:DD", -40), ("MeshCore-T096", "EE:FF", -70)]
    monkeypatch.setattr(cli, "scan_ble_devices", lambda: rows)
    out = []
    res = cli.pick_ble(scripted("", "1", "654321"), out.append)
    assert res == {"transport": "ble", "ble_address": "AA:BB", "ble_name": "Heltec V3 abc", "ble_pin": "654321"}
    assert not any("JBL" in str(x) for x in out)                          # the speaker is hidden until "all"


def test_pick_radio_menu_quit_and_back(monkeypatch):
    monkeypatch.setattr(cli, "list_ports", lambda: [])
    assert cli.pick_radio(scripted("q"), lambda *a: None) is None
    assert cli.pick_radio(scripted("1", "b", "q"), lambda *a: None) is None            # USB, back, quit
    monkeypatch.setattr(cli, "list_ports", lambda: [types.SimpleNamespace(device="COM4", description="x")])
    assert cli.pick_radio(scripted("1", "1"), lambda *a: None) == {"transport": "serial", "port": "COM4"}


def test_wizard_two_bots_and_priority_teams(tmp_path, monkeypatch):
    import meshscore.teams as teams
    monkeypatch.setattr(teams, "team_list", lambda kind: {"nhl": [("TOR", "Toronto")], "ohl": [("KIT", "Kitchener"), ("GUE", "Guelph")]}[kind])
    answers = scripted("1", "TOR", "#leafs", "y",          # NHL TOR, then another
                       "4", "", "#ohl", "KIT GUE", "n",     # OHL whole league (blank team), priority teams, done
                       "")                                   # timezone
    out = str(tmp_path / "meshscore.toml")
    assert cli.run_init(out, ask=answers, say=lambda *a: None, radio={"transport": "tcp", "host": "1.2.3.4"}, channels=[]) == out
    config.load_config(out, strict=True)
    assert set(CFG["modules"]) == {"tor", "ohl"} and CFG["modules"]["ohl"]["priority_teams"] == ["KIT", "GUE"]
    assert "team" not in CFG["modules"]["ohl"]


# ---------------------------------------------------------------- data tools without network
class FakeHTTP:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, params=None, timeout=None, **kw):
        r = types.SimpleNamespace(status_code=200)
        r.json = lambda: self.payload(url) if callable(self.payload) else self.payload
        return r


def test_team_lists_parse_the_real_response_shapes(monkeypatch):
    import meshscore.teams as teams
    nhl = {"standings": [{"teamAbbrev": {"default": "TOR"}, "teamName": {"default": "Toronto Maple Leafs"}},
                         {"teamAbbrev": {"default": "BOS"}, "teamName": {"default": "Boston Bruins"}}]}
    monkeypatch.setattr(teams, "session", FakeHTTP(nhl))
    assert teams.nhl() == [("BOS", "Boston Bruins"), ("TOR", "Toronto Maple Leafs")]
    monkeypatch.setattr(teams, "session", FakeHTTP({"teams": [{"abbreviation": "NYY", "name": "New York Yankees"}, {"name": "X"}]}))
    assert teams.mlb() == [("NYY", "New York Yankees")]
    espn = {"sports": [{"leagues": [{"teams": [{"team": {"abbreviation": "BUF", "displayName": "Buffalo Bills"}}]}]}]}
    monkeypatch.setattr(teams, "session", FakeHTTP(espn))
    assert teams.nfl() == [("BUF", "Buffalo Bills")]
    assert teams.team_list("curling") == []


def test_teams_command_prints_codes(monkeypatch, capsys):
    import meshscore.teams as teams
    monkeypatch.setattr(teams, "session", FakeHTTP({"standings": [{"teamAbbrev": {"default": "TOR"}, "teamName": {"default": "Toronto"}}]}))
    assert cli.main(["teams", "nhl"]) == 0
    assert "TOR" in capsys.readouterr().out
    assert cli.main(["teams", "curling"]) == 2


def test_check_probes_data_sources_without_the_radio(monkeypatch, capsys, tmp_path):
    """meshscore check --offline against recorded NHL data (the suites' fixtures)."""
    from meshscore import core
    blob = json.load(gzip.open(os.path.join(FIX, "phases.json.gz"), "rt", encoding="utf-8"))["entries"]
    nhl_keys = [k for k in blob if "club-schedule-season/tor/now" in k]
    assert nhl_keys, "fixture lacks the Leafs schedule"
    body = blob[nhl_keys[0]]["text"]

    class G:
        def get(self, url, params=None, timeout=None, **kw):
            ok = "club-schedule-season" in url
            return types.SimpleNamespace(status_code=200 if ok else 404, content=body.encode("utf-8"), text=body,
                                         json=lambda: json.loads(body))
    monkeypatch.setattr(core.session, "get", G().get)
    p = write(tmp_path)
    rc = cli.main(["check", "--offline", "-c", p])
    out = capsys.readouterr().out
    assert rc == 0 and "[ok]   leafs" in out and "games in the data window" in out


def test_check_reports_a_failing_source(monkeypatch, capsys, tmp_path):
    from meshscore import core
    monkeypatch.setattr(core.session, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    p = write(tmp_path)
    assert cli.main(["check", "--offline", "-c", p]) in (0, 1)


# ---------------------------------------------------------------- the MeshCore link, with a fake `meshcore` library
class Ev:
    def __init__(self, type_, payload=None):
        self.type, self.payload = type_, payload


class Types:
    CHANNEL_MSG_RECV, CONTACT_MSG_RECV, DISCONNECTED, ERROR, OK = "chan", "dm", "disc", "err", "ok"


class FakeCommands:
    def __init__(self, mc):
        self.mc = mc
        self.sent = []

    async def get_channel(self, idx):
        names = {0: "Public", 1: "#leafs", 2: "#ohl"}
        if idx in names:
            return Ev(Types.OK, {"channel_name": names[idx]})
        return Ev(Types.ERROR, {})

    async def send_chan_msg(self, idx, text):
        self.sent.append(("chan", idx, text))
        return Ev(Types.OK)

    async def send_msg(self, contact, text):
        self.sent.append(("dm", contact["public_key"], text))
        return Ev(Types.OK)

    async def get_contacts(self):
        return Ev(Types.OK)

    async def get_bat(self):
        return Ev(Types.OK, {"level": 4012})


class FakeMC:
    def __init__(self):
        self.contacts = {"abcd" * 16: {"adv_name": "Ann", "public_key": "abcd" * 16}}
        self.self_info = {"name": "MyBotNode"}
        self.commands = FakeCommands(self)
        self.subs = {}
        self.disconnected = False

    def subscribe(self, event_type, cb):
        self.subs[event_type] = cb

    async def start_auto_message_fetching(self):
        pass

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def fake_meshcore(monkeypatch):
    mc = FakeMC()
    made = {}

    class MeshCore:
        @staticmethod
        async def create_serial(port, baud):
            made["how"] = ("serial", port, baud)
            return mc

        @staticmethod
        async def create_tcp(host, port):
            made["how"] = ("tcp", host, port)
            return mc

        @staticmethod
        async def create_ble(addr, pin=None):
            made["how"] = ("ble", addr, pin)
            return mc

    mod = types.ModuleType("meshcore")
    mod.MeshCore, mod.EventType = MeshCore, Types
    monkeypatch.setitem(sys.modules, "meshcore", mod)
    return mc, made


@pytest.mark.parametrize("transport,expect", [("serial", ("serial", "COM3", 115200)), ("tcp", ("tcp", "1.2.3.4", 5000)),
                                              ("ble", ("ble", "AA:BB", "123456"))])
def test_all_three_transports_connect_the_right_way(fake_meshcore, transport, expect):
    mc, made = fake_meshcore
    CFG.update(transport=transport, port="COM3", baud=115200, host="1.2.3.4", tcp_port=5000, ble_address="AA:BB", ble_pin="123456")

    async def go():
        t = LibTransport()
        names = await t.open()
        assert names == {0: "Public", 1: "#leafs", 2: "#ohl"} and t.own_name == "MyBotNode"
        await t.stop()
    asyncio.run(go())
    assert made["how"] == expect and mc.disconnected


def test_channel_and_dm_messages_become_incoming_and_sends_go_to_the_right_slot(fake_meshcore):
    mc, _ = fake_meshcore
    CFG.update(transport="serial", port="COM3")
    got = []

    async def on_message(inc):
        got.append(inc)

    async def go():
        t = LibTransport()
        await t.start(on_message, {"leafs": {"channel": "#leafs", "channel_index": None},
                                   "nope": {"channel": "#missing", "channel_index": None}})
        assert t.alive and t.chans == {"leafs": 1}                       # a missing channel is reported, the rest still work
        await mc.subs[Types.CHANNEL_MSG_RECV](Ev(Types.CHANNEL_MSG_RECV, {"channel_idx": 1, "text": "Ann: !score", "path_len": 2, "SNR": 6.5}))
        await mc.subs[Types.CHANNEL_MSG_RECV](Ev(Types.CHANNEL_MSG_RECV, {"channel_idx": 9, "text": "x: !score"}))      # not ours
        await mc.subs[Types.CHANNEL_MSG_RECV](Ev(Types.CHANNEL_MSG_RECV, {"channel_idx": 1, "text": "no sender prefix here"}))
        await mc.subs[Types.CONTACT_MSG_RECV](Ev(Types.CONTACT_MSG_RECV, {"pubkey_prefix": "abcdabcdabcd", "text": "!next"}))
        assert await t.send_channel("#leafs", "hello") is True
        assert await t.send_channel("#unknown", "hi") is False
        assert await t.send_dm("abcdabcdabcd", "psst") is True
        assert await t.send_dm("ffffffffffff", "psst") is False
        assert "4.01V" in await t.status_extra()
        await mc.subs[Types.DISCONNECTED](Ev(Types.DISCONNECTED))
        assert t.alive is False
        await t.stop()
    asyncio.run(go())
    assert [(i.text, i.is_dm, i.sender_name, i.hops, i.snr, i.channel) for i in got] == [
        ("!score", False, "Ann", 2, 6.5, "leafs"),
        ("no sender prefix here", False, "", None, None, "leafs"),
        ("!next", True, "Ann", None, None, ""),
    ]
    assert mc.commands.sent == [("chan", 1, "hello"), ("dm", "abcd" * 16, "psst")]


def test_no_configured_channel_on_the_radio_is_fatal_with_a_helpful_message(fake_meshcore):
    CFG.update(transport="serial", port="COM3")

    async def go():
        t = LibTransport()
        with pytest.raises(RuntimeError, match="None of the configured channels"):
            await t.start(lambda inc: None, {"x": {"channel": "#missing", "channel_index": None}})
    asyncio.run(go())


def test_connect_failure_names_the_target(monkeypatch):
    class MeshCore:
        @staticmethod
        async def create_serial(port, baud):
            return None
    mod = types.ModuleType("meshcore")
    mod.MeshCore, mod.EventType = MeshCore, Types
    monkeypatch.setitem(sys.modules, "meshcore", mod)
    CFG.update(transport="serial", port="COM9")

    async def go():
        with pytest.raises(ConnectionError, match="COM9"):
            await LibTransport().open()
    asyncio.run(go())


def test_incoming_dataclass_defaults():
    i = Incoming("!x", True)
    assert i.sender_name == "" and i.rest == [] and i.channel == ""
