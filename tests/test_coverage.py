"""Coverage: what the radio hears (hops, signal), without ever posting."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from meshscore import config, core
from meshscore.core import CFG
from meshscore.radio import LibTransport

from daily_rig import DRig, run


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, command_cooldown=0, log_file="", timezone="America/Toronto")
    core.init_tz()
    yield
    config.reset()


def test_counts_hops_and_signal_and_answers_coverage():
    async def go():
        async with DRig("coverage", cooldown=0) as r:
            await r.tick()
            for name, hops, snr in (("Ann", 0, 12.0), ("Ann", 0, 11.0), ("Bob", 1, 8.0), ("Cy", 2, 4.0), ("Di", 3, -2.0),
                                    ("Ed", 5, -8.0), ("Ann", 1, 9.5)):
                await r.say("hello there", name=name, hops=hops, snr=snr, channel="public")      # any channel counts
            await r.say("no sender prefix", name="", hops=1)                                    # unnamed: ignored
            w = r.m.window(24)
            assert w["nodes"] == 5 and w["msgs"] == 7 and w["hops"] == {"0": 2, "1": 2, "2": 1, "3+": 2}
            assert w["snr"]["p50"] == 8.0 and w["snr"]["n"] == 7
            await r.say("!coverage", name="Zed")
            out = r.t.sent[-1]
            assert out.startswith("[COVERAGE] 24h: 6 nodes, 8 msgs")           # Zed's own command counts: it was heard too
            assert "hops 0:2 1:3 2:1 3+:2" in out and "SNR median 8" in out and len(out.encode()) <= 130
            assert r.take() and r.m.kinds["max"] == set()                                       # it never posts on its own
    run(go())


def test_the_web_state_hides_names_unless_asked():
    async def go():
        async with DRig("coverage") as r:
            await r.say("hi", name="Ann Smith", hops=2, snr=7.0)
            await r.say("hi", name="Bob", hops=0, snr=11.0)
            await r.tick()
            st = r.m.site_state()
            s = json.dumps(st)
            assert "Ann" not in s and "Bob" not in s and {x["who"] for x in st["recent"]} == {"Node 1", "Node 2"}
            assert len(st["days"]) == 14 and st["days"][-1]["msgs"] == 2 and st["d7"]["nodes"] == 2
        async with DRig("coverage", show_names=True) as r2:
            await r2.say("hi", name="Ann Smith", hops=2, snr=7.0)
            assert r2.m.site_state()["recent"][0]["who"] == "Ann Smith"
    run(go())


def test_quiet_radio_and_old_data_are_pruned():
    async def go():
        async with DRig("coverage") as r:
            assert "has not heard anyone" in (await r.m.cmd_coverage(None, ""))[0]
            r.m.state.data["days"]["2020-01-01"] = {"n": 5, "hops": {"0": 5}, "senders": ["x"]}
            for i in range(450):
                r.m.state.data["senders"][f"n{i}"] = {"id": i, "n": 1, "first": 1, "last": i, "hops": {}, "snr": None, "last_hops": 0}
            await r.tick()
            assert "2020-01-01" not in r.m.state.data["days"] and len(r.m.state.data["senders"]) == 400
    run(go())


def test_the_radio_forwards_channels_no_bot_is_on_only_when_a_listener_exists():
    got = []

    async def on_message(inc):
        got.append((inc.channel, inc.sender_name, inc.hops, inc.snr))

    t = LibTransport()
    t.on_message, t.idx_name, t.all_names = on_message, {1: "chess"}, {0: "Public", 1: "#chess"}
    ev = lambda idx, text: SimpleNamespace(payload={"channel_idx": idx, "text": text, "path_len": 2, "SNR": 6.0})
    asyncio.run(t._on_chan(ev(1, "Ann: !ping")))
    asyncio.run(t._on_chan(ev(0, "Bob: hello")))                       # Public: nobody listens -> dropped
    assert got == [("chess", "Ann", 2, 6.0)]
    t.forward_unmapped = True
    asyncio.run(t._on_chan(ev(0, "Bob: hello")))
    asyncio.run(t._on_chan(ev(7, "Cy: who is this")))                  # an unknown slot is still dropped
    assert got[-1] == ("public", "Bob", 2, 6.0) and len(got) == 2


def test_adding_a_listener_switches_forwarding_on():
    from meshscore.cli import build_hub
    from daily_rig import Radio
    CFG["modules"] = {"cov": {"enabled": True, "type": "coverage", "channel": "#daily"}}
    t = Radio()
    build_hub(t)
    assert t.forward_unmapped is True
    CFG["modules"] = {"w": {"enabled": True, "type": "word", "channel": "#daily"}}
    t2 = Radio()
    build_hub(t2)
    assert t2.forward_unmapped is False


def test_boards_that_share_a_page_are_linked_once_and_never_to_themselves():
    from meshscore.cli import build_hub
    from daily_rig import Radio
    mk = lambda typ, **kw: {"enabled": True, "type": typ, "channel": "#c", **kw}
    CFG["modules"] = {"w": mk("word", site_url="https://d.test/"), "t": mk("trivia", site_url="https://d.test/"),
                      "chess": mk("chess", puzzle=False, site_url="https://c.test/")}
    hub = build_hub(Radio())
    assert hub.modules["w"].site_state()["links"] == [{"title": "Mesh Chess", "url": "https://c.test/"}]
    assert hub.modules["chess"].site_state()["links"] == [{"title": "Mesh Daily", "url": "https://d.test/"}]
