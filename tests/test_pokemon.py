"""Crowd-plays-Pokemon: the memory reader, the vote rules, the bot through the hub (fake Game Boy), the squashing
publisher, and - when a ROM is available - a smoke test against the real emulator. Offline."""
import asyncio
import json
import os
import shutil
import subprocess

import pytest

from meshscore import config
from meshscore.chessplay.publish import SitePublisher
from meshscore.cli import build_hub
from meshscore.core import CFG
from meshscore.pokeplay import game as pg
from meshscore.pokeplay import ram
from meshscore.pokeplay.emulator import norm_button
from meshscore.radio import Incoming

from test_chess import Radio                         # the recording radio used by the chess tests

ROM = os.environ.get("MESHSCORE_POKEMON_ROM", "")


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, command_cooldown=15, log_file="", timezone="America/Toronto")
    yield
    config.reset()


# ================================================================== reading memory
def test_text_decoding():
    assert ram.decode_text([0x91, 0x84, 0x83, 0x50, 0x80]) == "RED"
    assert ram.decode_text([0x80, 0xA1, 0x7F, 0xF7, 0x50]) == "Ab 1"
    assert ram.decode_text([0x50]) == ""


def test_species_names_come_out_of_the_rom_table():
    rom = bytearray(0x20000)
    for i, n in enumerate(["RHYDON", "KANGASKHAN"]):
        enc = bytes(0x80 + ord(c) - 65 for c in n) + b"\x50"
        rom[ram.ROM_MONSTER_NAMES + i * 10: ram.ROM_MONSTER_NAMES + i * 10 + len(enc)] = enc.ljust(10, b"\x50")
    names = ram.species_names_from_rom(bytes(rom))
    assert names[1:3] == ["RHYDON", "KANGASKHAN"] and names[0] == "?" and len(names) == ram.MONSTER_COUNT + 1


class Mem:
    def __init__(self, cells=None):
        self.cells = cells or {}

    def read(self, addr):
        return self.cells.get(addr, 0)


def mem_with_party(mons, map_id=0x26, badges=0, battle=0):
    c = {ram.ADDR_MAP: map_id, ram.ADDR_X: 5, ram.ADDR_Y: 6, ram.ADDR_BADGES: badges, ram.ADDR_IN_BATTLE: battle,
         ram.ADDR_PARTY_COUNT: len(mons)}
    for i, (sp, nick, lvl, hp, mx) in enumerate(mons):
        base = ram.ADDR_PARTY_MONS + i * ram.MON_SIZE
        c[base] = sp
        c[base + 1], c[base + 2] = hp >> 8, hp & 255
        c[base + ram.OFF_LEVEL] = lvl
        c[base + ram.OFF_MAXHP], c[base + ram.OFF_MAXHP + 1] = mx >> 8, mx & 255
        for j, ch in enumerate(nick):
            c[ram.ADDR_PARTY_NICKS + i * ram.NICK_SIZE + j] = 0x80 + ord(ch) - 65
        c[ram.ADDR_PARTY_NICKS + i * ram.NICK_SIZE + len(nick)] = 0x50
    return Mem(c)


NAMES = ["?"] + [f"MON{i}" for i in range(1, 191)]


def test_status_reads_party_map_badges_and_money():
    m = mem_with_party([(5, "SPARKY", 12, 30, 40), (9, "ROCKY", 5, 0, 20)], map_id=0x0C, badges=0b101)
    m.cells.update({ram.ADDR_MONEY: 0x01, ram.ADDR_MONEY + 1: 0x23, ram.ADDR_MONEY + 2: 0x45,
                    ram.ADDR_DEX_OWNED: 0b111, ram.ADDR_DEX_SEEN: 0xFF})
    st = ram.read_status(m, NAMES)
    assert st.started and st.map_name == "Route 1" and st.badges == 2 and st.money == 12345
    assert st.owned == 3 and st.seen == 8
    assert [(x.name, x.species, x.level, x.hp, x.max_hp) for x in st.party] == [("SPARKY", "MON5", 12, 30, 40),
                                                                              ("ROCKY", "MON9", 5, 0, 20)]
    assert ram.Status.from_dict(json.loads(json.dumps(st.to_dict()))) == st
    assert "Route 1" in ram.summary(st) and "SPARKY L12 30/40" in ram.summary(st)


def test_title_screen_is_not_started():
    st = ram.read_status(Mem(), NAMES)
    assert not st.started and "not started" in ram.summary(st)


def S(**kw):
    base = dict(started=True, map_id=0x26, map_name="Red's Room", party=[ram.Mon("A", "MON1", 5, 20, 20)])
    base.update(kw)
    return ram.Status(**base)


def kinds(old, new):
    return [k for k, _ in ram.diff_events(old, new)]


def test_events_are_only_real_changes_and_never_on_first_sight():
    s = S()
    assert ram.diff_events(None, s) == []
    assert ram.diff_events(S(started=False), s) == []             # the game just began: not news
    assert ram.diff_events(s, S()) == []
    assert kinds(s, S(map_id=0x0C, map_name="Route 1")) == ["map"]
    assert kinds(s, S(badges=1)) == ["badge"]
    assert kinds(s, S(owned=1)) == ["caught"]
    assert kinds(s, S(party=[ram.Mon("A", "MON1", 6, 22, 22)])) == ["levelup"]
    assert kinds(s, S(party=[ram.Mon("A", "MON2", 16, 40, 40)])) == ["evolve"]
    assert kinds(s, S(battle=1)) == ["battle"]
    assert kinds(S(battle=1), S(battle=1)) == []
    assert kinds(s, S(party=[ram.Mon("A", "MON1", 5, 0, 20)])) == ["faint"]
    assert kinds(s, S(party=[ram.Mon("A", "MON1", 5, 20, 20), ram.Mon("B", "MON2", 3, 10, 10)])) == ["caught"]
    # entering a battle does not also announce the (battle) map; every message fits a radio packet
    for _, text in ram.diff_events(s, S(battle=2, map_id=0x40, map_name="X")):
        assert len(text.encode()) <= 130


# ================================================================== votes
def test_buttons_and_aliases():
    assert [norm_button(w) for w in ("UP", "!a", "u", "sel", "Start", "dance")] == ["up", "a", "up", "select", "start", None]


def test_votes_tally_ties_changes_and_steps():
    r = pg.new_round()
    assert pg.vote(r, "Ann", "up", "5") == ("up", 5)
    assert pg.vote(r, "Bob", "a") == ("a", 0)
    assert pg.vote(r, "Cy", "dance") is None
    assert pg.vote(r, "Di", "right", "99") == ("right", pg.MAX_STEPS)
    assert pg.vote(r, "Ed", "a", "3") == ("a", 3)                  # a count on A / B means that many taps (text boxes)
    assert pg.vote(r, "Fy", "start", "3") == ("start", 0)          # ... but not on start / select
    assert pg.vote(r, "Gus", "a", "99") == ("a", pg.MAX_TAPS)
    assert pg.tally(r)[0] == (("up", 5), 1)                        # all different: the earliest vote leads
    pg.vote(r, "Bob", "up", "5")                                    # Bob changes his mind: up x5 now leads
    assert pg.winner(r) == (("up", 5), 2)
    assert pg.label(("up", 5)) == "upx5" and pg.label(("a", 0)) == "a"
    t = pg.new_round()                                              # a tie goes to the earlier first vote
    pg.vote(t, "X", "left")
    pg.vote(t, "Y", "right")
    assert pg.winner(t)[0] == ("left", 0)


# ================================================================== the bot, through the hub
class Rig:
    def __init__(self, contacts=None, cooldown=15, script=None, **cfg):
        CFG["command_cooldown"] = cooldown
        CFG["modules"] = {"pokemon": {"enabled": True, "type": "pokemon", "channel": "#pokemon", "emulator": "fake",
                                      "window_seconds": 30, **cfg}}
        self.t = Radio(contacts)
        self.hub = build_hub(self.t)
        self.m = self.hub.modules["pokemon"]
        self.now = [1_800_000_000.0]
        self.m.clock = lambda: self.now[0]
        if script:
            self.m.gb.script = script
        self.tasks = []

    async def __aenter__(self):
        self.tasks = [asyncio.create_task(self.hub.tx_worker()), asyncio.create_task(self.hub.cmd_worker())]
        return self

    async def __aexit__(self, *a):
        for t in self.tasks:
            t.cancel()

    async def settle(self):
        for _ in range(200):
            await asyncio.sleep(0.005)
            if self.hub.cmdq.empty() and self.hub.out.empty():
                await asyncio.sleep(0.02)
                if self.hub.cmdq.empty() and self.hub.out.empty():
                    return

    async def say(self, text, name="Ann", dm=False, key=""):
        await self.hub.on_message(Incoming(text, dm, name, key or (("k" + name.lower() * 8) if dm else ""), 1, 5.0,
                                           "" if dm else "pokemon"))
        await self.settle()

    async def tick(self, advance=0):
        self.now[0] += advance
        await self.m.poll()
        await self.settle()

    def take(self):
        out, self.t.sent = self.t.sent, []
        return out


def run(coro):
    return asyncio.run(coro)


def test_votes_start_a_clock_and_the_winning_button_is_pressed():
    async def go():
        async with Rig() as r:
            await r.tick()
            await r.say("!up", name="Ann")
            await r.say("!up 5", name="Bob")
            await r.say("!up 5", name="Cy")
            await r.say("!a", name="Di")
            assert r.m.rnd["window_end"] == r.now[0] + 30
            assert r.m.gb.presses == []                              # nothing moves before the clock runs out
            await r.tick(29)
            assert r.m.gb.presses == []
            await r.tick(2)
            assert r.m.gb.presses == [("up", 5)]
            assert r.m.state.data["rounds"] == 1 and r.m.rnd["votes"] == {}
            assert r.m.state.data["players"]["Bob"] == {"presses": 1, "votes": 1}
            assert r.m.state.data["players"]["Ann"] == {"presses": 0, "votes": 1}
    run(go())


def test_default_steps_for_the_dpad_and_one_press_for_buttons():
    async def go():
        async with Rig(steps=4) as r:
            await r.tick()
            await r.say("!left")
            await r.tick(31)
            await r.say("!a")
            await r.tick(31)
            await r.say("!press down 2")
            await r.tick(31)
            assert r.m.gb.presses == [("left", 4), ("a", 1), ("down", 2)]
    run(go())


def test_many_voters_in_the_cooldown_all_count_and_aliases_work():
    async def go():
        async with Rig(cooldown=15) as r:
            await r.tick()
            for who, w in (("A", "!u"), ("B", "!u"), ("C", "!up"), ("D", "!d")):
                await r.say(w, name=who)
            assert len(r.m.rnd["votes"]) == 4 and pg.winner(r.m.rnd)[0] == ("up", 0)
    run(go())


def test_restart_is_silent_and_resumes_the_game_and_the_round():
    base = {0xD35E: 0x26, 0xD362: 5, 0xD361: 6, 0xD163: 1, 0xD16B: 5, 0xD18C: 8, 0xD16D: 20, 0xD18E: 20}
    script = {1: base, 2: {0xD35E: 0x0C}}

    async def go():
        async with Rig(script=script) as r:
            await r.tick()
            await r.say("!up")
            await r.tick(31)                                           # the game "starts": not news
            assert r.t.sent == []
            await r.say("!up")
            await r.tick(31)
            assert any("Route 1" in x for x in r.t.sent)              # the map change was announced once
            await r.say("!a", name="Bob")
        async with Rig() as r2:                                        # restart: same state + same emulator save
            assert r2.m.gb.resumed
            await r2.tick()
            await r2.tick(5)
            assert r2.t.sent == []
            assert r2.m.status.map_id == 0x0C and len(r2.m.rnd["votes"]) == 1   # Bob's vote kept, nothing re-announced
            await r2.tick(100)
            assert r2.m.gb.presses[-1] == ("a", 1)
    run(go())


def test_events_are_posted_capped_and_mode_min_keeps_only_the_big_ones():
    script = {1: {0xD35E: 0x0C, 0xD362: 5, 0xD361: 6, 0xD163: 1, 0xD16B: 5, 0xD18C: 5, 0xD16D: 20, 0xD18E: 20},
              2: {0xD18C: 6, 0xD356: 1, 0xD35E: 0x0D}}

    async def go():
        async with Rig(script=script) as r:
            await r.tick()
            await r.say("!up")
            await r.tick(31)
            r.take()
            await r.say("!up", name="Bob")
            await r.tick(31)
            out = r.take()
            assert len(out) <= 3 and any("Badge 1/8" in x for x in out) and any("L6" in x for x in out)
            assert all(len(x.encode()) <= 130 for x in out)
            r.m.state.data["mode"] = "min"
            r.m.gb.script = {3: {0xD356: 3, 0xD35E: 0x0E}}
            await r.say("!up")
            await r.tick(31)
            out = r.take()
            assert len(out) == 1 and "Badge 2/8" in out[0]
    run(go())


def test_commands_where_party_votes_top_and_reset():
    script = {1: {0xD35E: 0x0C, 0xD362: 5, 0xD361: 6, 0xD163: 1, 0xD16B: 5, 0xD18C: 8, 0xD16D: 20, 0xD18E: 20,
                  0xD2B5: 0x91, 0xD2B6: 0x84, 0xD2B7: 0x83, 0xD2B8: 0x50}}

    async def go():
        async with Rig(cooldown=0, site_url="https://x.test/p/", script=script) as r:
            await r.tick()
            await r.say("!votes")
            assert "No votes yet" in r.t.sent[-1]
            await r.say("!up", name="Ann")
            await r.say("!votes")
            assert "up 1" in r.t.sent[-1] and "30s left" in r.t.sent[-1]
            await r.tick(31)
            r.take()
            await r.say("!where")
            assert "Route 1" in r.t.sent[-1] and "RED L8 20/20" in r.t.sent[-1] and "x.test" in r.t.sent[-1]
            await r.say("!party")
            assert "RED L8 20/20" in r.t.sent[-1]
            await r.say("!top")
            assert "Ann 1" in r.t.sent[-1]
            await r.say("!pokemon where")
            assert "Route 1" in r.t.sent[-1]
            CFG["admin_names"] = ["Boss"]
            await r.say("!reset confirm", name="Ann")
            assert "Only admins" in r.t.sent[-1] and r.m.gb.presses
            await r.say("!reset", name="Boss")
            assert "!reset confirm" in r.t.sent[-1]
            await r.say("!reset confirm", name="Boss")
            assert "New game" in r.t.sent[-1] and r.m.gb.presses == [] and r.m.state.data["rounds"] == 0
    run(go())


def test_dm_ack_when_the_contact_is_known_and_nothing_on_the_channel():
    async def go():
        async with Rig(contacts={"Ann": "a" * 64}) as r:
            await r.tick()
            r.take()
            await r.say("!up 3", name="Ann")
            assert r.t.sent == [] and r.t.dms[0][1].startswith("✓ upx3: 1 of 1 votes")
            await r.say("!dance", name="Ann")                         # not a button: ignored quietly (it is not our command)
            assert r.t.sent == []
    run(go())


def test_votes_that_arrive_while_the_game_runs_start_the_next_round():
    async def go():
        async with Rig() as r:
            await r.tick()
            await r.say("!up")
            real = r.m._play
            started = asyncio.Event()

            def slow(button, steps):
                import time
                started.set()
                time.sleep(0.3)
                return real(button, steps)
            r.m._play = slow
            task = asyncio.create_task(r.tick(31))
            await asyncio.sleep(0.1)
            assert started.is_set()
            await r.say("!b", name="Late")                             # arrives mid-press
            await task
            assert r.m.gb.presses == [("up", 3)]
            assert r.m.rnd["votes"]["Late"][0] == "b" and r.m.rnd["window_end"] is not None
    run(go())


def test_missing_rom_or_emulator_never_takes_the_other_bots_down():
    async def go():
        CFG["command_cooldown"] = 0
        CFG["modules"] = {"pokemon": {"enabled": True, "type": "pokemon", "channel": "#pokemon", "rom": "C:/nope/none.gb"}}
        t = Radio()
        hub = build_hub(t)
        m = hub.modules["pokemon"]
        assert m.gb is None and "ROM not found" in m.problem
        assert await m.poll() == 300
        w = asyncio.create_task(hub.tx_worker())
        c = asyncio.create_task(hub.cmd_worker())
        await hub.on_message(Incoming("!up", False, "Ann", "", 1, 5.0, "pokemon"))
        await hub.on_message(Incoming("!where", False, "Ann", "", 1, 5.0, "pokemon"))
        await asyncio.sleep(0.3)
        w.cancel()
        c.cancel()
        assert t.sent and all("not running" in x for x in t.sent)
    run(go())


def test_site_state_is_json_and_has_what_the_page_reads():
    async def go():
        async with Rig(site_dir="site", site_push=False) as r:
            await r.tick()
            await r.say("!left", name="Ann")
            st = r.m.site_state()
            json.dumps(st)
            for k in ("round", "window_end", "votes", "status", "recent", "events", "players", "channel", "contact"):
                assert k in st
            assert st["votes"] == [{"action": "left", "n": 1}]
            assert os.path.isfile("site/screen.png") and os.path.isfile("site/state.json")
    run(go())


def test_the_page_exists_and_reads_the_state_keys():
    from meshscore.leagues import pokemon
    html = open(os.path.join(pokemon.SITE_TEMPLATE, "index.html"), encoding="utf-8").read()
    for key in ("state.json", "screen.png", "window_end", "votes", "party", "events", "recent", "players", "channel", "contact"):
        assert key in html, key


# ================================================================== the squashing publisher
@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_squash_amends_only_its_own_commit_and_keeps_history_flat(tmp_path, monkeypatch):
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"), ("GIT_COMMITTER_NAME", "t"),
                 ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(k, v)
    remote, site = tmp_path / "remote.git", tmp_path / "site"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(site)], check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-B", "main"], cwd=site, check=True, capture_output=True)
    (site / "README.md").write_text("hi")
    subprocess.run(["git", "add", "-A"], cwd=site, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "someone's own commit"], cwd=site, check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=site, check=True, capture_output=True)
    now = [0.0]
    pub = SitePublisher(str(site), min_interval=0, clock=lambda: now[0], squash=True, label="pokemon",
                        template_dir=str(tmp_path / "none"))

    def log():
        r = subprocess.run(["git", "--git-dir", str(remote), "log", "--format=%s", "main"], capture_output=True, text=True)
        return r.stdout.split("\n")[:-1]

    now[0] = 1
    assert pub.tick({"n": 1}, {"screen.png": b"one"}) is True
    assert log() == ["pokemon: update board", "someone's own commit"]   # a foreign HEAD is never amended: new commit
    for i in range(2, 6):
        now[0] = i
        assert pub.tick({"n": i}, {"screen.png": b"x" * i}) is True
    assert log() == ["pokemon: update board", "someone's own commit"]   # then every update squashes into that one
    assert (site / "screen.png").read_bytes() == b"x" * 5
    show = subprocess.run(["git", "--git-dir", str(remote), "show", "main:screen.png"], capture_output=True)
    assert show.stdout == b"x" * 5


# ================================================================== the real emulator (needs your ROM)
@pytest.mark.skipif(not os.path.isfile(ROM), reason="no Pokemon Red ROM available (set MESHSCORE_POKEMON_ROM)")
def test_real_rom_boots_reads_memory_saves_and_resumes(tmp_path):
    pytest.importorskip("pyboy")
    pytest.importorskip("PIL")
    from meshscore.pokeplay.emulator import GameBoy
    state = str(tmp_path / "g.state")
    g = GameBoy(ROM, state)
    try:
        assert g.species_names[0xB0] == "CHARMANDER" and g.species_names[0xB1] == "SQUIRTLE" and g.species_names[0x99] == "BULBASAUR"
        assert not ram.read_status(g, g.species_names).started          # still the title screen
        g.press("start")
        for _ in range(160):
            g.press("a")
            st = ram.read_status(g, g.species_names)
            if st.started and st.map_id == 0x26:
                break
        assert st.map_id == 0x26 and st.map_name == "Red's Room" and st.money == 3000
        png = g.png()
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 200
        g.press("left", 2)
        before = (ram.read_status(g, g.species_names).x, ram.read_status(g, g.species_names).y)
        g.save()
    finally:
        g.close()
    g2 = GameBoy(ROM, state)
    try:
        assert g2.resumed
        st2 = ram.read_status(g2, g2.species_names)
        assert (st2.x, st2.y) == before and st2.map_id == 0x26
    finally:
        g2.close()
