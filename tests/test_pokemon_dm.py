"""Pokemon in DM-only mode, the input log, plain-DM routing, and the contact-add link the app's scanner reads."""
import asyncio
import os
import re
from types import SimpleNamespace

import pytest

from meshscore import config, core
from meshscore.core import CFG
from meshscore.radio import LibTransport

from test_pokemon import Rig, run  # noqa: F401  (the harness)


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, command_cooldown=0, log_file="", timezone="America/Toronto")
    core.init_tz()
    yield
    config.reset()


def test_dm_only_ignores_the_channel_hints_once_and_answers_dms_by_dm():
    async def go():
        async with Rig(dm_only=True, cooldown=0, site_url="https://x.test/") as r:
            await r.tick()
            r.take()
            await r.say("!up", name="Ann")                                  # on the channel: ignored
            await r.say("!where", name="Ann")
            await r.say("!a", name="Bob")
            assert r.m.rnd["votes"] == {} and r.m.gb.presses == []
            hints = [x for x in r.t.sent if "Play by DM" in x]
            assert len(hints) == 1 and "x.test" in hints[0]                  # one hint, not one per message
            await r.say("!up 2", name="Ann", dm=True)                       # a plain DM goes to the bot that has the command
            assert r.m.rnd["votes"]["Ann"][:2] == ["up", 2] and r.t.dms[-1][1].startswith("✓ upx2")
            await r.say("!where", name="Ann", dm=True)
            assert "round" in r.t.dms[-1][1]
            assert r.m.site_state()["dm_only"] is True
    run(go())


def test_the_input_log_records_votes_and_rounds_and_log_answers_by_dm(tmp_path):
    async def go():
        async with Rig(cooldown=0) as r:
            await r.tick()
            await r.say("!up", name="Ann")
            await r.say("!up", name="Bob")
            await r.say("!a", name="Cy")
            await r.tick(31)
            text = open("pokemon-inputs.log", encoding="utf-8").read()
            assert text.count("vote ") == 3 and "Ann up" in text and "Cy a" in text
            assert re.search(r"round #1 upx3? \(2/3\) by Ann, Bob", text) or "round #1 up (2/3) by Ann, Bob" in text
            assert r.m.state.data["recent"][0]["who"] == ["Ann", "Bob"]
            await r.say("!log", name="Ann", dm=True)
            assert "#1 up (Ann,Bob)" in r.t.dms[-1][1]
            await r.say("!log", name="Ann")                                 # on the channel (not dm_only): also works
            assert "#1 up" in r.t.sent[-1]
    run(go())


def test_input_log_can_be_switched_off():
    async def go():
        async with Rig(input_log="off", cooldown=0) as r:
            await r.tick()
            await r.say("!a")
            await r.tick(31)
            assert not os.path.exists("pokemon-inputs.log")
    run(go())


def test_the_contact_link_is_the_documented_contact_add_url():
    key = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    t = LibTransport()
    t.mc = SimpleNamespace(self_info={"name": "Sports bot", "public_key": key, "adv_type": 1})
    link = asyncio.run(t.own_contact_link())
    assert link == f"meshcore://contact/add?name=Sports%20bot&public_key={key}&type=1"
    t.mc = SimpleNamespace(self_info={"name": "x"})
    assert asyncio.run(t.own_contact_link()) == ""                        # no key: nothing invented


def test_the_page_qr_is_made_from_the_contact_add_link_and_shows_the_key():
    from meshscore.chessplay.cards import qr_svg

    async def go():
        async with Rig(site_dir="site", site_push=False) as r:
            key = "ab" * 32
            r.t.uri = "meshcore://11" + key
            r.t.link = f"meshcore://contact/add?name=Sportsbot&public_key={key}&type=1"
            r.t.curi = "meshcore://channel/add?name=%23pokemon&secret=00"
            await r.tick()
            c = r.m.site_state()["contact"]
            assert c["link"] == r.t.link and c["public_key"] == key and c["uri"] == r.t.uri
            assert c["svg"] == qr_svg(r.t.link) and c["svg"] != qr_svg(r.t.uri)
    run(go())


def test_the_screen_is_never_a_blank_fade(tmp_path):
    rom = os.environ.get("MESHSCORE_POKEMON_ROM", "")
    if not os.path.isfile(rom):
        pytest.skip("no ROM")
    pytest.importorskip("pyboy")
    from meshscore.pokeplay.emulator import GameBoy
    g = GameBoy(rom, "")
    try:
        g.press("start")
        for _ in range(3):                                                 # New Game fades through white
            g.press("a")
            a = g.pb.screen.ndarray
            assert not (a == a[0, 0]).all()
    finally:
        g.close()


def test_the_page_says_who_the_radio_has_heard():
    async def go():
        async with Rig(site_dir="site", site_push=False, cooldown=0) as r:
            await r.tick()
            assert r.m.site_state()["heard"] == {"last": None, "senders_24h": 0}          # nobody yet
            await r.say("hello there", name="Ann")                                         # any channel traffic counts
            await r.say("!up", name="Bob")
            await r.say("!a", name="Bob")
            st = r.m.site_state()["heard"]
            assert st["senders_24h"] == 2 and abs(st["last"] - __import__("time").time()) < 5
            r.hub.heard["pokemon"]["Old"] = 1.0                                            # heard long ago: not counted
            assert r.m.site_state()["heard"]["senders_24h"] == 2
            await r.say("hi", name="Eve", dm=True)                                         # DMs are not channel hearing
            assert r.m.site_state()["heard"]["senders_24h"] == 2
    run(go())


def test_the_heard_field_is_stable_between_polls():
    """A time, not an age: an idle channel must not make the page state change every second (that was one upload per poll)."""

    async def go():
        async with Rig(site_dir="site", site_push=False, cooldown=0) as r:
            await r.say("hi", name="Ann")
            a = r.m.site_state()["heard"]
            await asyncio.sleep(1.2)
            assert r.m.site_state()["heard"] == a
    run(go())


def test_text_has_finished_printing_when_the_picture_is_taken():
    """Oak's intro used to show half-printed text boxes (people pressed again, or thought it was frozen)."""
    rom = os.environ.get("MESHSCORE_POKEMON_ROM", "")
    if not os.path.isfile(rom):
        pytest.skip("no ROM")
    pytest.importorskip("pyboy")
    from meshscore.pokeplay.emulator import GameBoy
    g = GameBoy(rom, "")
    try:
        g.press("start")
        g.press("a")
        moving = 0
        for _ in range(25):
            g.press("a")
            before = g._picture()
            g.pb.tick(120, False)
            g.pb.tick(1, True)
            moving += g._picture() != before                 # still changing by itself after we looked
        assert moving <= 1, f"{moving} of 25 screens were still printing"
    finally:
        g.close()
