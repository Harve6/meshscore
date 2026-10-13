"""Pokemon: Red / Blue / Yellow memory layouts, the QR codes on the pages, and the daily 'which game' vote. Offline."""
import asyncio
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from meshscore import config, core
from meshscore.chessplay.cards import qr_svg
from meshscore.core import CFG
from meshscore.pokeplay import ram

from test_pokemon import Mem, Rig, run  # noqa: F401  (the harness)

ZONE = ZoneInfo("America/Toronto")


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, command_cooldown=0, log_file="", timezone="America/Toronto")
    core.init_tz()
    yield
    monkeypatch.setattr(core, "CLOCK", None)
    config.reset()


def at(monkeypatch, hour, minute=0, day=14):
    monkeypatch.setattr(core, "CLOCK", lambda: datetime(2026, 10, day, hour, minute, tzinfo=ZONE))


ROMS = {"red": "r.gb", "blue": "b.gb", "yellow": "y.gb"}


# ================================================================== layouts / ROM tables
def test_layout_follows_the_cartridge_title():
    assert ram.layout_for_title("POKEMON RED") is ram.RED_BLUE
    assert ram.layout_for_title("POKEMON BLUE") is ram.RED_BLUE
    assert ram.layout_for_title("POKEMON YELLOW") is ram.YELLOW
    assert ram.layout_for_title("") is ram.RED_BLUE


def test_yellow_is_the_same_table_one_byte_lower():
    for f in ("player_name", "party_count", "party_mons", "party_nicks", "map_id", "y", "x", "badges", "money",
              "in_battle", "dex_owned", "dex_seen"):
        assert getattr(ram.YELLOW, f) == getattr(ram.RED_BLUE, f) - 1, f
    c = {ram.YELLOW.map_id: 0x0C, ram.YELLOW.x: 4, ram.YELLOW.y: 9, ram.YELLOW.party_count: 1, ram.YELLOW.badges: 0b11,
         ram.YELLOW.party_mons: 84, ram.YELLOW.party_mons + 2: 18, ram.YELLOW.party_mons + ram.OFF_LEVEL: 5,
         ram.YELLOW.party_mons + ram.OFF_MAXHP + 1: 18}
    names = ["?"] + [f"MON{i}" for i in range(1, 191)]
    st = ram.read_status(Mem(c), names, ram.YELLOW)
    assert (st.map_name, st.badges, st.party[0].species, st.party[0].hp, st.party[0].level) == ("Route 1", 2, "MON84", 18, 5)
    wrong = ram.read_status(Mem(c), names, ram.RED_BLUE)               # the wrong layout would read nonsense
    assert wrong.map_id != 0x0C


def test_species_table_is_found_wherever_the_rom_keeps_it():
    rom = bytearray(0x100000)
    base = 0xE8000
    for i, n in enumerate(["RHYDON", "KANGASKHAN", "NIDORANM"]):
        enc = bytes(0x80 + ord(c) - 65 for c in n) + b"\x50"
        rom[base + i * 10: base + i * 10 + 10] = enc.ljust(10, b"\x50")
    assert ram.find_monster_names(bytes(rom)) == base
    assert ram.species_names_from_rom(bytes(rom))[1:4] == ["RHYDON", "KANGASKHAN", "NIDORANM"]
    assert ram.find_monster_names(bytes(0x20000)) == ram.ROM_MONSTER_NAMES      # not found: the Red/Blue default


# ================================================================== QR codes
def _decode(svg):
    zx = pytest.importorskip("zxingcpp")
    from PIL import Image
    vb = [int(float(v)) for v in re.search(r'viewBox="0 0 (\d+) (\d+)"', svg).groups()]
    size = vb[0]
    scale = 8
    img = Image.new("L", (size * scale, size * scale), 255)
    px = img.load()
    for m in re.finditer(r"M(\d+) (\d+)h(\d+)v1", re.search(r' d="([^"]+)"', svg).group(1)):
        x, y, w = map(int, m.groups())
        for yy in range(y * scale, (y + 1) * scale):
            for xx in range(x * scale, (x + w) * scale):
                px[xx, yy] = 0
    res = zxcpp_read(zx, img)
    return res


def zxcpp_read(zx, img):
    out = zx.read_barcodes(img)
    return out[0].text if out else None


@pytest.mark.parametrize("uri", [
    "meshcore://110000112233445566778899aabbccddeeff00112233445566778899aabbccddeeff2d9dce6a176a47e13ac6efc35ddc2175"
    "95418cc4beeeecadccf28d628bd9347827b1f54276292cc009fc6d08ef6269c07d52074875f60eb6c40c80369f786ea757cba10f8153706f727473626f74",
    "meshcore://channel/add?name=%23pokemon&secret=793a6a2b2bce3393c301c0a78c31aac2",
])
def test_the_qr_svg_decodes_to_exactly_the_link(uri):
    pytest.importorskip("segno")
    svg = qr_svg(uri)
    assert svg.startswith("<svg") and len(svg) < 20000
    assert _decode(svg) == uri


def test_no_qr_for_nothing():
    assert qr_svg("") == ""


# ================================================================== several games, one daily vote
def test_a_single_rom_has_no_ballot_and_game_says_so(monkeypatch):
    at(monkeypatch, 9, 5)

    async def go():
        async with Rig() as r:
            await r.tick()
            assert r.m.state.data["ballot"] is None and not r.m.multi
            await r.say("!game")
            assert "Playing" in r.t.sent[-1]
    run(go())


def test_the_ballot_opens_in_its_hour_once_a_day_and_the_winner_is_played(monkeypatch):
    at(monkeypatch, 9, 5)

    async def go():
        async with Rig(roms=ROMS) as r:
            assert r.m.multi and r.m.active == "red" and r.m.gb.title == "POKEMON RED"
            await r.tick()
            opened = [x for x in r.t.sent if x.startswith("🗳")]
            assert len(opened) == 1 and "red | blue | yellow" in opened[0] and len(opened[0].encode()) <= 130
            await r.tick(30)
            assert len([x for x in r.t.sent if x.startswith("🗳")]) == 1          # once
            await r.say("!game blue", name="Ann")
            await r.say("!game blue", name="Bob")
            await r.say("!game yellow", name="Cy")
            await r.say("!game y", name="Di")                                      # a unique prefix works
            await r.say("!game purple", name="Ed")                                 # not a game: nothing recorded
            assert [g for g, _ in r.m._ballot_tally()] == ["yellow", "blue"] or [g for g, _ in r.m._ballot_tally()] == ["blue", "yellow"]
            await r.say("!game blue", name="Di")                                   # Di changes her mind
            assert r.m._ballot_tally()[0] == ("blue", 3)
            await r.say("!up", name="Ann")
            await r.tick(31)                                                       # a round in Red before the switch
            red_presses = list(r.m.gb.presses)
            assert red_presses
            r.take()
            await r.tick(61 * 60)
            assert r.m.active == "blue" and r.m.gb.title == "POKEMON BLUE"
            assert any("Pokemon Blue" in x and "3 votes" in x for x in r.t.sent)
            assert r.m.gb.presses == []                                            # blue has its own (fresh) save
            assert r.m.state.data["ballot"] is None and r.m.state.data["round_state"]["votes"] == {}
            # back to red the next day: its save comes back
            at(monkeypatch, 9, 1, day=15)
            await r.tick(24 * 3600 - 61 * 60)
            await r.say("!game red", name="Ann")
            await r.tick(61 * 60)
            assert r.m.active == "red" and r.m.gb.presses == red_presses and r.m.gb.resumed
    run(go())


def test_ballot_no_votes_or_same_game_changes_nothing(monkeypatch):
    at(monkeypatch, 9, 5)

    async def go():
        async with Rig(roms=ROMS) as r:
            await r.tick()
            r.take()
            await r.tick(61 * 60)
            assert r.m.active == "red" and any("No votes" in x for x in r.t.sent)
            at(monkeypatch, 9, 5, day=15)
            await r.tick(24 * 3600)
            await r.say("!game red", name="Ann")
            r.take()
            await r.tick(61 * 60)
            assert r.m.active == "red" and any("no change" in x for x in r.t.sent)
    run(go())


def test_no_ballot_outside_its_hour_and_a_restart_does_not_reopen_it(monkeypatch):
    at(monkeypatch, 14, 0)

    async def go():
        async with Rig(roms=ROMS) as r:
            await r.tick()
            assert r.m.state.data["ballot"] is None and not any(x.startswith("🗳") for x in r.t.sent)
            at(monkeypatch, 9, 10, day=15)
            await r.tick()
            await r.say("!game yellow", name="Ann")
        async with Rig(roms=ROMS) as r2:                                          # restart mid-ballot, same hour
            assert r2.m.state.data["ballot"] and r2.m._ballot_tally() == [("yellow", 1)]
            await r2.tick()
            assert not any(x.startswith("🗳") for x in r2.t.sent)                 # nothing re-announced
            await r2.tick(61 * 60)
            assert r2.m.active == "yellow"
    run(go())


def test_game_command_outside_a_ballot_and_admin_switch(monkeypatch):
    at(monkeypatch, 14, 0)

    async def go():
        async with Rig(roms=ROMS, cooldown=0) as r:
            await r.tick()
            await r.say("!game")
            assert "vote opens daily at 9:00" in r.t.sent[-1] and "Pokemon Red" in r.t.sent[-1]
            await r.say("!game blue", name="Ann")
            assert "No game vote open" in r.t.sent[-1]
            CFG["admin_names"] = ["Boss"]
            await r.say("!switch blue", name="Ann")
            assert "Only admins" in r.t.sent[-1] and r.m.active == "red"
            await r.say("!switch blue", name="Boss")
            assert "Now playing Pokemon Blue" in r.t.sent[-1] and r.m.active == "blue"
            await r.say("!where")
            assert "Pokemon Blue:" in r.t.sent[-1]
    run(go())


def test_games_do_not_announce_each_others_progress():
    script = {1: {0xD35E: 0x26, 0xD362: 5, 0xD361: 6, 0xD163: 1, 0xD16B: 5, 0xD18C: 8, 0xD16D: 20, 0xD18E: 20}}

    async def go():
        async with Rig(roms=ROMS, script=script) as r:
            await r.tick()
            await r.say("!up")
            await r.tick(31)
            r.take()
            await r.say("!switch blue", name="Ann")                                # blue is at a different place entirely
            await r.tick(5)
            await r.say("!left", name="Ann")
            await r.tick(31)
            assert not [x for x in r.t.sent if x.startswith(("📍", "⬆", "⚔"))]
    run(go())


def test_a_single_rom_save_is_picked_up_as_red_when_more_roms_are_added(tmp_path):
    (tmp_path / "pokemon.state").write_text(json.dumps({"ram": {"54110": 1}, "presses": [["a", 1], ["start", 1]]}))

    async def go():
        async with Rig(roms=ROMS) as r:
            assert r.m.gb.resumed and r.m.gb.presses == [("a", 1), ("start", 1)]
            assert os.path.isfile("pokemon-red.state") and os.path.isfile("pokemon.state")
    run(go())


def test_the_page_state_describes_the_games_and_the_vote(monkeypatch):
    at(monkeypatch, 9, 5)

    async def go():
        async with Rig(roms=ROMS) as r:
            await r.tick()
            await r.say("!game blue", name="Ann")
            st = r.m.site_state()
            json.dumps(st)
            assert st["game"]["active"] == "red" and [o["key"] for o in st["game"]["options"]] == ["red", "blue", "yellow"]
            assert st["ballot"]["votes"] == [{"game": "blue", "n": 1}] and st["ballot"]["voters"] == 1
    run(go())


def test_admin_commands_are_off_until_admins_are_configured_and_reset_can_name_a_game(monkeypatch, tmp_path):
    at(monkeypatch, 14, 0)

    async def go():
        async with Rig(roms=ROMS, cooldown=0) as r:
            await r.tick()
            await r.say("!up", name="Ann")
            await r.tick(31)
            await r.say("!reset confirm", name="Ann")
            assert "Admin commands are off" in r.t.sent[-1] and r.m.gb.presses
            await r.say("!switch blue", name="Ann")
            assert "Admin commands are off" in r.t.sent[-1] and r.m.active == "red"
            CFG["admin_keys"] = ["ab12"]                                          # an authenticated (DM) admin only
            await r.say("!reset confirm", name="Ann")                             # a name on the channel is not enough
            assert "Only admins can reset a game" in r.t.sent[-1] and r.m.gb.presses
            await r.say("!pokemon reset", name="Boss", dm=True, key="ab12" + "00" * 14)
            assert "!reset red confirm" in r.t.dms[-1][1]
            await r.say("!pokemon switch yellow", name="Boss", dm=True, key="ab12" + "00" * 14)
            assert r.m.active == "yellow" and "Now playing Pokemon Yellow" in r.t.dms[-1][1]
            await r.say("!pokemon reset red confirm", name="Boss", dm=True, key="ab12" + "00" * 14)   # not the running game
            assert "will start fresh" in r.t.dms[-1][1] and not os.path.isfile("pokemon-red.state")
            assert r.m.active == "yellow"
            await r.say("!pokemon reset confirm", name="Boss", dm=True, key="ab12" + "00" * 14)       # the running game
            assert "New game" in r.t.dms[-1][1] and r.m.state.data["rounds"] == 0
    run(go())
