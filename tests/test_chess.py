"""The crowd-vs-bot chess bot: engine, rules, voting, restarts, the daily puzzle and the web-board publisher. Offline."""
import asyncio
import json
import os
import random
import shutil
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import chess
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from meshscore import config, core
from meshscore.chessplay import puzzle as pz
from meshscore.chessplay.engine import LEVELS, Opponent, PyEngine
from meshscore.chessplay.game import CrowdGame, outcome_of, parse_move
from meshscore.chessplay.publish import SitePublisher
from meshscore.cli import build_hub
from meshscore.core import CFG
from meshscore.radio import Incoming, Transport

HERE = os.path.dirname(__file__)
LICHESS = json.load(open(os.path.join(HERE, "fixtures", "lichess_daily.json"), encoding="utf-8"))


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, command_cooldown=15, log_file="", timezone="America/Toronto")
    core.init_tz()
    yield
    config.reset()


# ================================================================== parsing
@pytest.mark.parametrize("text,uci", [
    ("e4", "e2e4"), ("E4", "e2e4"), ("e2e4", "e2e4"), ("e2-e4", "e2e4"), ("Nf3", "g1f3"), ("nf3", "g1f3"),
    ("g1f3", "g1f3"), ("1.e4", "e2e4"), ("e4!", "e2e4"), ("  e4 ", "e2e4"),
])
def test_parse_move_accepts_the_ways_people_type(text, uci):
    mv, err = parse_move(chess.Board(), text)
    assert err is None and mv.uci() == uci


@pytest.mark.parametrize("text,err", [("Nf6", "illegal"), ("e5", "illegal"), ("O-O", "illegal"), ("zzz", "illegal"),
                                      ("", "empty"), ("  ", "empty")])
def test_parse_move_rejects(text, err):
    assert parse_move(chess.Board(), text) == (None, err)


def test_castling_promotion_and_ambiguity():
    b = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    for t in ("O-O", "0-0", "o-o", "e1g1"):
        assert parse_move(b, t)[0].uci() == "e1g1"
    assert parse_move(b, "O-O-O")[0].uci() == "e1c1"
    b = chess.Board("8/4P3/8/8/8/8/8/k6K w - - 0 1")
    for t in ("e8=Q", "e8q", "e7e8q", "e7e8"):
        assert parse_move(b, t)[0].uci() == "e7e8q"
    assert parse_move(b, "e8=N")[0].uci() == "e7e8n"
    assert parse_move(chess.Board("4k3/8/8/8/8/8/8/1N1K1N2 w - - 0 1"), "Nd2") == (None, "ambiguous")


# ================================================================== engine
@pytest.mark.parametrize("fen,san", [
    ("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1", "Ra8#"),                          # back-rank mate
    ("r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4", "Qxf7#"),
    ("rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 2", "Qh4#"),  # fool's mate
])
def test_engine_takes_mate_in_one(fen, san):
    b = chess.Board(fen)
    for lv in LEVELS[3:6]:                           # the levels that never throw a move away
        mv = PyEngine(random.Random(3)).best_move(b, lv, 10)
        assert b.san(mv) == san, lv.name


def test_engine_finds_mate_in_two_at_depth_three():
    b = chess.Board("kbK5/pp6/1P6/8/8/8/8/R7 w - - 0 1")           # 1.Ra6! bxa6 2.b7#
    mv = PyEngine(random.Random(1)).best_move(b, LEVELS[4], 15)
    assert b.san(mv) == "Ra6"


def test_engine_does_not_leave_its_queen_en_prise_to_a_pawn():
    b = chess.Board("rnb1kbnr/pppppppp/8/q7/1P6/8/P1PPPPPP/RNBQKBNR b KQkq - 0 1")   # b4 pawn attacks the queen
    for lv in LEVELS[3:6]:
        c = b.copy()
        c.push(PyEngine(random.Random(5)).best_move(b, lv, 10))
        assert not any(c.piece_type_at(m.to_square) == chess.QUEEN for m in c.legal_moves if c.is_capture(m)), lv.name


def test_engine_leaves_the_callers_board_alone_and_honours_the_clock():
    import time
    b = chess.Board("r2q1rk1/ppp2ppp/2n1bn2/3pp3/1bB1P3/2NP1N2/PPP1QPPP/R1B2RK1 w - - 0 8")
    fen, stack = b.fen(), list(b.move_stack)
    t = time.time()
    mv = PyEngine(random.Random(2)).best_move(b, LEVELS[7], 0.4)
    assert time.time() - t < 3
    assert mv in b.legal_moves and b.fen() == fen and list(b.move_stack) == stack


def test_engine_always_returns_a_legal_move_from_the_start_position():
    for i, lv in enumerate(LEVELS[:5]):
        b = chess.Board()
        assert PyEngine(random.Random(i)).best_move(b, lv, 5) in b.legal_moves


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(0, 10 ** 6), plies=st.integers(0, 70), lvl=st.integers(0, 4))
def test_engine_legal_in_random_positions(seed, plies, lvl):
    rng = random.Random(seed)
    b = chess.Board()
    for _ in range(plies):
        moves = list(b.legal_moves)
        if not moves or b.is_game_over():
            break
        b.push(rng.choice(moves))
    if b.is_game_over():
        return
    mv = PyEngine(rng).best_move(b, LEVELS[lvl], 0.3)
    assert mv in b.legal_moves


def test_opponent_falls_back_when_stockfish_is_missing():
    op = Opponent(stockfish_path="C:/definitely/not/here/stockfish.exe", think_seconds=2)
    b = chess.Board()
    assert op.choose(b, 4) in b.legal_moves                         # level 5 would use Stockfish; the file is not there
    assert op.choose(b, 99) in b.legal_moves                        # out-of-range level clamps


# ================================================================== the game rules
def fresh_game(level=0):
    return CrowdGame(CrowdGame.new(1, level, None, 0))


class Scripted:
    """Stands in for the engine: plays the given UCI moves in order (first legal move if they run out / are illegal)."""
    def __init__(self, *ucis):
        self.ucis = list(ucis)
        self.calls = 0

    def choose(self, board, level):
        self.calls += 1
        while self.ucis:
            m = chess.Move.from_uci(self.ucis.pop(0))
            if m in board.legal_moves:
                return m
        return next(iter(board.legal_moves))


def test_tally_ties_go_to_the_earliest_first_vote_and_votes_can_change():
    g = fresh_game()
    g.vote("A", "e4")
    g.vote("B", "d4")
    g.vote("C", "d4")
    assert g.tally() == [("d4", 2), ("e4", 1)]
    g.vote("B", "e4")                                 # B changes their mind: e4 has 2 (first seen earliest), d4 has 1
    assert g.tally() == [("e4", 2), ("d4", 1)]
    g.vote("C", "e4")
    g.vote("A", "d4")
    assert g.tally()[0][1] == 2
    g2 = fresh_game()
    g2.vote("A", "e4")
    g2.vote("B", "d4")                                # 1-1: the earlier vote wins
    assert g2.tally()[0] == ("e4", 1)


def test_illegal_votes_are_not_recorded():
    g = fresh_game()
    assert g.vote("A", "Ke2")[0] == "illegal"
    assert g.data["votes"] == {}
    assert g.leading_move() is None
    assert g.play_round(lambda b: None) is None


def test_crowd_checkmate_ends_the_game_without_a_bot_reply():
    g = fresh_game()
    bot = Scripted("f7f6", "g7g5")
    for mv in ("e4", "d4", "Qh5"):
        assert g.vote("A", mv)[0] == "ok"
        r = g.play_round(lambda b: bot.choose(b, 0))
    assert (r.winner, r.reason, r.bot_san, r.crowd_san) == ("crowd", "checkmate", None, "Qh5#")
    assert g.over and g.data["result"] == "crowd"
    assert g.vote("A", "e5")[0] == "over"


def test_bot_checkmate_is_a_crowd_loss():
    g = fresh_game()
    bot = Scripted("e7e5", "d8h4")
    for mv in ("f3", "g4"):
        g.vote("A", mv)
        r = g.play_round(lambda b: bot.choose(b, 0))
    assert (r.winner, r.reason, r.bot_san) == ("bot", "checkmate", "Qh4#")


@pytest.mark.parametrize("fen,expect", [
    ("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1", ("draw", "stalemate")),
    ("8/8/8/4k3/8/8/3K4/8 w - - 0 1", ("draw", "insufficient material")),
    ("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 99 60", None),
    ("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 100 60", ("draw", "50-move rule")),
    ("R5k1/5ppp/8/8/8/8/5PPP/6K1 b - - 0 1", ("crowd", "checkmate")),      # black is the one mated
])
def test_outcomes(fen, expect):
    assert outcome_of(chess.Board(fen)) == expect


def test_threefold_repetition_is_a_draw():
    g = fresh_game()
    bot = Scripted("g8f6", "f6g8", "g8f6", "f6g8", "g8f6", "f6g8")
    last = None
    for mv in ("Nf3", "Ng1", "Nf3", "Ng1", "Nf3", "Ng1"):
        if g.over:
            break
        g.vote("A", mv)
        last = g.play_round(lambda b: bot.choose(b, 0))
    assert g.over and last.winner == "draw" and last.reason == "repetition"


def test_a_round_does_not_touch_the_game_until_it_succeeds():
    g = fresh_game()
    g.vote("A", "e4")

    def boom(board):
        raise RuntimeError("engine died")
    import copy
    work = copy.deepcopy(g.data)
    with pytest.raises(RuntimeError):
        CrowdGame(work).play_round(boom)
    assert g.data["moves"] == [] and g.data["votes"]                # the real game is untouched: votes remain


# ================================================================== the bot, through the hub
class Radio(Transport):
    name = "chess-test"
    alive = True

    def __init__(self, contacts=None):
        self.sent, self.dms = [], []
        self.contacts = contacts or {}

    async def send_channel(self, channel, text):
        self.sent.append(text)
        return True

    async def send_dm(self, key, text):
        self.dms.append((key, text))
        return True

    async def own_contact_uri(self):
        return getattr(self, "uri", "")

    async def channel_uri(self, channel):
        return getattr(self, "curi", "")

    async def own_contact_link(self):
        return getattr(self, "link", "")

    def find_contact(self, name=None, key=None):
        for n, k in self.contacts.items():
            if (name and n.lower() == name.lower()) or (key and k.startswith(key[:12])):
                return {"adv_name": n, "public_key": k}
        return None


class Rig:
    def __init__(self, contacts=None, opponent=None, cooldown=0, **cfg):
        CFG["command_cooldown"] = cooldown
        CFG["modules"] = {"chess": {"enabled": True, "type": "chess", "channel": "#chess", "puzzle": False,
                                    "window_minutes": 5, **cfg}}
        self.t = Radio(contacts)
        self.hub = build_hub(self.t)
        self.m = self.hub.modules["chess"]
        self.now = [1_800_000_000.0]
        self.m.clock = lambda: self.now[0]
        if opponent is not None:
            self.m.opponent = opponent
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
        await self.hub.on_message(Incoming(text, dm, name, key or ("k" + name.lower() * 8 if dm else ""), 1, 5.0,
                                           "" if dm else "chess"))
        await self.settle()

    async def tick(self, advance=0):
        self.now[0] += advance
        await self.m.poll()
        await self.settle()

    @property
    def game(self):
        return self.m.game()

    def take(self):
        out, self.t.sent = self.t.sent, []
        return out


def run(coro):
    return asyncio.run(coro)


def test_start_is_announced_once_and_a_restart_is_silent():
    async def go():
        async with Rig() as r:
            await r.tick()
            assert len(r.t.sent) == 1 and r.t.sent[0].startswith("♟ New game #1")
            await r.tick(60)
            assert len(r.t.sent) == 1                                   # nothing more while nobody votes
        async with Rig() as r2:                                         # restart, same state file
            await r2.tick()
            await r2.tick(3600)
            assert r2.t.sent == [] and r2.game.data["id"] == 1
    run(go())


def test_a_round_one_post_many_voters_no_cooldown_loss():
    async def go():
        async with Rig(opponent=Scripted("e7e5"), cooldown=15) as r:
            await r.tick()
            r.take()
            for who in ("Ann", "Bob", "Cy", "Di", "Ed"):               # all inside the 15 s command cooldown
                await r.say("!move e4" if who != "Ed" else "!move d4", name=who)
            await r.say("!move Ke2", name="Fay")                        # illegal: never counted
            assert len(r.game.data["votes"]) == 5
            assert r.game.tally() == [("e4", 4), ("d4", 1)]
            before = list(r.t.sent)
            await r.tick(299)
            assert r.t.sent == before                                   # the clock has not run out yet
            await r.tick(5)
            posts = [x for x in r.t.sent if x.startswith("♟ 1.")]
            assert len(posts) == 1 and "1.e4 (4 votes) 1...e5" in posts[0]
            assert len(posts[0].encode()) <= 130
            assert r.game.data["window_end"] is None and r.game.data["votes"] == {}
            assert r.m.state.data["players"]["Ann"] == {"moves": 1, "votes": 1, "puzzles": 0}
            assert r.m.state.data["players"]["Ed"] == {"moves": 0, "votes": 1, "puzzles": 0}
    run(go())


def test_the_clock_starts_at_the_first_vote_and_silence_costs_nothing():
    async def go():
        async with Rig(opponent=Scripted()) as r:
            await r.tick()
            r.take()
            await r.tick(86400)                                         # a whole quiet day
            assert r.t.sent == [] and r.game.data["window_end"] is None
            await r.say("!move d4", name="Ann")
            end = r.game.data["window_end"]
            assert end == r.now[0] + 300
            await r.tick(100)
            await r.say("!move e4", name="Bob")
            assert r.game.data["window_end"] == end                     # later votes do not move the clock
    run(go())


def test_restart_mid_round_keeps_the_votes_and_posts_nothing():
    async def go():
        async with Rig(opponent=Scripted()) as r:
            await r.tick()
            await r.say("!move e4", name="Ann")
            await r.say("!move e4", name="Bob")
            t = r.now[0]
        async with Rig(opponent=Scripted("e7e5")) as r2:
            r2.now[0] = t + 10
            await r2.tick()
            assert r2.t.sent == [] and len(r2.game.data["votes"]) == 2    # nothing re-announced, votes kept
            await r2.tick(300)
            assert [x for x in r2.t.sent if "1.e4 (2 votes)" in x]
    run(go())


def test_expired_clock_in_state_is_restarted_at_boot():
    async def go():
        async with Rig(opponent=Scripted()) as r:
            await r.tick()
            await r.say("!move e4", name="Ann")
            t0 = r.now[0]
        old = json.load(open("chess_state.json", encoding="utf-8"))
        assert old["game"]["window_end"] == t0 + 300
        later = t0 + 7200
        CFG["modules"] = {"chess": {"enabled": True, "type": "chess", "channel": "#chess", "puzzle": False}}
        import time as _t
        real = _t.time
        _t.time = lambda: later                                          # the module's default clock is time.time
        try:
            hub = build_hub(Radio())
        finally:
            _t.time = real
        g = hub.modules["chess"].state.data["game"]
        assert g["window_end"] == later + 300 and g["votes"]            # fresh clock, same votes
    run(go())


def test_crowd_win_levels_up_and_announces_the_final_and_next_game():
    async def go():
        async with Rig(opponent=Scripted("f7f6", "g7g5")) as r:
            await r.tick()
            for mv in ("e4", "d4", "Qh5"):
                await r.say(f"!move {mv}", name="Ann")
                await r.tick(301)
            finals = [x for x in r.t.sent if "FINAL" in x]
            assert len(finals) == 1 and "Crowd wins" in finals[0] and "Level up: L2 Novice" in finals[0]
            assert all(len(x.encode()) <= 130 for x in r.t.sent)
            d = r.m.state.data
            assert d["level"] == 1 and d["record"] == {"crowd": 1, "bot": 0, "draw": 0}
            assert d["game"]["id"] == 2 and d["game"]["moves"] == [] and d["game"]["announced"]
            assert d["history"][0]["result"] == "crowd" and d["history"][0]["moves"].startswith("1.e4 f6 2.d4 g5 3.Qh5#")
    run(go())


def test_crowd_loss_stays_on_the_same_level():
    async def go():
        async with Rig(opponent=Scripted("e7e5", "d8h4"), start_level=3) as r:
            await r.tick()
            for mv in ("f3", "g4"):
                await r.say(f"!move {mv}", name="Ann")
                await r.tick(301)
            assert any("Bot (Casual) wins" in x for x in r.t.sent)
            d = r.m.state.data
            assert d["level"] == 2 and d["record"]["bot"] == 1          # start_level 3 -> index 2; unchanged
    run(go())


def test_beating_the_top_level_does_not_overflow():
    async def go():
        async with Rig(opponent=Scripted("f7f6", "g7g5"), max_level=2, start_level=2) as r:
            await r.tick()
            for mv in ("e4", "d4", "Qh5"):
                await r.say(f"!move {mv}")
                await r.tick(301)
            assert r.m.state.data["level"] == 1                          # index 1 = level 2 = the cap
            assert any("legend" in x for x in r.t.sent)
    run(go())


def test_mode_off_is_silent_but_the_game_goes_on():
    async def go():
        async with Rig(opponent=Scripted("e7e5")) as r:
            r.m.state.data["mode"] = "off"
            await r.tick()
            await r.say("!move e4", name="Ann")
            await r.tick(301)
            assert r.t.sent == [] and r.game.data["moves"] == ["e2e4", "e7e5"]
    run(go())


def test_mode_min_skips_the_per_move_posts_but_not_the_result():
    async def go():
        async with Rig(opponent=Scripted("f7f6", "g7g5")) as r:
            r.m.state.data["mode"] = "min"
            await r.tick()
            r.take()
            for mv in ("e4", "d4", "Qh5"):
                await r.say(f"!move {mv}")
                await r.tick(301)
            out = r.take()
            assert len(out) == 1 and "FINAL" in out[0]
    run(go())


def test_feedback_goes_by_dm_when_the_contact_is_known_else_only_errors_use_the_channel():
    async def go():
        async with Rig(contacts={"Ann": "a" * 64}, opponent=Scripted()) as r:
            await r.tick()
            r.take()
            await r.say("!move e4", name="Ann")                          # known: DM ack, channel stays quiet
            assert r.t.sent == [] and len(r.t.dms) == 1 and r.t.dms[0][1].startswith("✓ e4: 1 vote of 1")
            await r.say("!move d4", name="Stranger")                     # unknown contact: valid vote is silent
            assert r.t.sent == [] and len(r.t.dms) == 1
            await r.say("!move Qh9", name="Stranger")                    # an error is worth a short channel reply
            assert len(r.t.sent) == 1 and "not legal" in r.t.sent[0]
    run(go())


def test_ack_chan_and_ack_off():
    async def go():
        async with Rig(ack="chan", opponent=Scripted()) as r:
            await r.tick()
            r.take()
            await r.say("!move e4")
            assert len(r.t.sent) == 1 and r.t.sent[0].startswith("✓ e4")
        async with Rig(ack="off", opponent=Scripted()) as r2:
            await r2.tick()
            r2.take()
            await r2.say("!move Qh9")
            assert r2.t.sent == []
    run(go())


def test_dm_votes_work_and_are_answered_by_dm():
    async def go():
        async with Rig(opponent=Scripted()) as r:
            await r.tick()
            r.take()
            await r.say("!move e4", name="Ann", dm=True, key="ab" * 16)
            assert r.game.data["votes"] and len(r.t.dms) == 1 and r.t.sent == []
            await r.say("!chess move d4", name="Bob", dm=True, key="cd" * 16)
            assert len(r.game.data["votes"]) == 2
    run(go())


def test_votes_while_the_bot_is_thinking_are_refused_not_lost():
    async def go():
        async with Rig(opponent=Scripted()) as r:
            await r.tick()
            r.take()
            r.m.busy = True
            await r.say("!move e4")
            assert r.game.data["votes"] == {} and "thinking" in r.t.sent[0]
    run(go())


def test_engine_failure_keeps_the_votes_and_retries():
    class Flaky(Scripted):
        def choose(self, board, level):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("stockfish crashed")
            return super().choose(board, level)

    async def go():
        async with Rig(opponent=Flaky("e7e5")) as r:
            await r.tick()
            r.take()
            await r.say("!move e4")
            await r.tick(301)
            assert r.t.sent == [] and r.game.data["votes"] and r.game.data["moves"] == []
            await r.tick(61)
            assert any("1.e4" in x and "1...e5" in x for x in r.t.sent)
    run(go())


def test_board_moves_top_and_newgame_commands():
    async def go():
        async with Rig(opponent=Scripted("e7e5"), site_url="https://example.github.io/chess/") as r:
            await r.tick()
            await r.say("!move e4", name="Ann")
            await r.tick(301)
            r.take()
            await r.say("!board")
            assert "move 2" in r.t.sent[0] and "1.e4 e5" in r.t.sent[0] and "example.github.io" in r.t.sent[0]
            await r.say("!moves")
            assert r.t.sent[1].startswith("[CHESS] legal:") and "Nf3" in " ".join(r.t.sent[1:])
            await r.say("!top")
            assert "crowd 0W 0L 0D" in r.t.sent[-1] and "Ann 1" in r.t.sent[-1]
            await r.say("!chess")                                        # the bare module name answers with the board
            assert "move 2" in r.t.sent[-1]
            r.take()
            await r.say("!newgame 4")                                    # no admins configured: admin commands are off
            assert "Admin commands are off" in r.t.sent[0] and r.game.data["id"] == 1
            CFG["admin_names"] = ["Boss"]
            r.take()
            await r.say("!newgame 4", name="Boss")
            assert r.game.data["id"] == 2 and r.game.data["level"] == 3 and "Club" in r.t.sent[0]
    run(go())


def test_newgame_is_admin_only_when_admins_are_configured():
    async def go():
        CFG["admin_names"] = ["Boss"]
        async with Rig() as r:
            await r.tick()
            r.take()
            await r.say("!newgame", name="Ann")
            assert "Only admins" in r.t.sent[0] and r.game.data["id"] == 1
            await r.say("!newgame", name="Boss")
            assert r.game.data["id"] == 2
    run(go())


# ================================================================== whole games
def play_random_game(seed, level_name, max_rounds=400):
    """A random crowd (1-4 voters) against the real engine until the game ends; returns (rig-state, all posts)."""
    rng = random.Random(seed)

    async def go():
        posts = []
        async with Rig(opponent=Opponent(think_seconds=2, rng=random.Random(seed)), start_level=level_name) as r:
            await r.tick()
            first_id = r.game.data["id"]
            for _ in range(max_rounds):
                g = r.game
                moves = [g.board.san(m) for m in g.board.legal_moves]
                for i in range(rng.randint(1, 4)):
                    await r.say(f"!move {rng.choice(moves)}", name=f"P{i}")
                await r.tick(301)
                st_ = r.m.site_state()
                json.dumps(st_)                                         # always serialisable
                b = chess.Board()
                for u in r.m.state.data["game"]["moves"]:
                    b.push_uci(u)
                assert st_["game"]["fen"] == b.fen()
                if r.m.state.data["game"]["id"] != first_id:
                    break
            posts = list(r.t.sent)
            return r.m.state.data, posts, first_id
    return run(go())


@pytest.mark.parametrize("seed,start", [(1, 1), (2, 1), (3, 2)])
def test_whole_random_games_invariants(seed, start):
    data, posts, first_id = play_random_game(seed, start)
    assert all(len(p.encode()) <= 130 for p in posts), [p for p in posts if len(p.encode()) > 130]
    finals = [p for p in posts if "FINAL" in p]
    assert len(finals) == (1 if data["game"]["id"] != first_id else 0)
    rec = data["record"]
    assert sum(rec.values()) == len(data["history"]) == len(finals)
    assert posts[0].startswith("♟ New game")
    assert all(not p.startswith("♟ New game") for p in posts[1:]) or len(finals) == 0


def test_restart_at_every_round_changes_nothing_and_says_nothing():
    """After every round, a freshly built bot on the same state file must hold the same game and post nothing."""
    async def go():
        rng = random.Random(7)
        async with Rig(opponent=Opponent(think_seconds=1, rng=random.Random(7))) as r:
            await r.tick()
            for _ in range(12):
                g = r.game
                moves = [g.board.san(m) for m in g.board.legal_moves]
                await r.say(f"!move {rng.choice(moves)}", name="Ann")
                await r.tick(301)
                snap = json.dumps(r.m.state.data["game"], sort_keys=True)
                async with Rig() as again:
                    again.now[0] = r.now[0]
                    await again.tick()
                    await again.tick(10)
                    assert again.t.sent == []
                    assert json.dumps(again.m.state.data["game"], sort_keys=True) == snap or \
                        again.m.state.data["game"]["window_end"] != r.m.state.data["game"]["window_end"]
    run(go())


# ================================================================== the daily puzzle
def puzzle_dict():
    return pz.from_lichess(LICHESS, "2026-10-14")


def test_lichess_puzzle_parses_and_the_whole_line_is_legal():
    p = puzzle_dict()
    assert p["id"] == "p99h5" and p["side"] == "black" and p["rating"] == LICHESS["puzzle"]["rating"] and len(p["solution"]) == 5
    assert pz.from_lichess({"puzzle": {"id": "x", "fen": "bad", "solution": ["e2e4"]}}, "d") is None
    assert pz.from_lichess({}, "d") is None
    bad = json.loads(json.dumps(LICHESS))
    bad["puzzle"]["solution"] = bad["puzzle"]["solution"][:4]       # a solution must end on the solver's move
    assert pz.from_lichess(bad, "d") is None


def test_puzzle_solution_line_solves_it_and_wrong_moves_cost_tries():
    p = puzzle_dict()
    assert pz.attempt(p, "Ann", "Rf8")["status"] == "wrong"         # a legal but wrong move
    assert pz.attempt(p, "Ann", "Zz9")["status"] == "illegal"       # not counted
    assert p["tries"]["Ann"] == 1
    r = pz.attempt(p, "Ann", "Rb8")
    assert r["status"] == "right" and r["reply_san"] == "d7"
    assert pz.attempt(p, "Ann", "b2")["status"] == "right"
    r = pz.attempt(p, "Ann", "b1=Q")
    assert r["status"] == "solved" and r["n"] == 1 and p["solved"] == ["Ann"]
    assert pz.attempt(p, "Ann", "Rb8")["status"] == "done"
    assert pz.answer_text(p).startswith("1...Rb8 2.d7")
    q = puzzle_dict()
    for _ in range(3):
        assert pz.attempt(q, "Bob", "Rf8")["status"] == "wrong"
    assert pz.attempt(q, "Bob", "Rb8")["status"] == "locked"


def test_any_mate_counts_as_solving():
    puz = {"id": "m", "date": "d", "fen": "6k1/5ppp/8/8/8/8/5PPP/RR4K1 w - - 0 1", "solution": ["a1a8"], "rating": 1000,
           "side": "white", "progress": {}, "tries": {}, "solved": []}
    assert pz.attempt(puz, "Ann", "Rb8#")["status"] == "solved"      # the listed answer is Ra8#; Rb8# mates too


def test_daily_puzzle_is_posted_once_in_its_hour_and_quiet_otherwise(monkeypatch):
    fetched = []

    def fake_fetch(session, date, timeout=15.0):
        fetched.append(date)
        return pz.from_lichess(LICHESS, date)
    monkeypatch.setattr(pz, "fetch_daily", fake_fetch)
    zone = ZoneInfo("America/Toronto")

    def at(h, m=0, d=14):
        monkeypatch.setattr(core, "CLOCK", lambda: datetime(2026, 10, d, h, m, tzinfo=zone))

    async def go():
        async with Rig(puzzle=True, puzzle_hour=9, opponent=Scripted(), site_url="https://x.test/c/") as r:
            at(14)                                                       # first ever start, mid-afternoon: stored, silent
            await r.tick()
            assert r.m.state.data["puzzle"]["id"] == "p99h5"
            assert not any("🧩" in x for x in r.t.sent)
            r.take()
            at(9, 5, d=15)                                               # next morning, in the hour: posted
            r.m.state.data["puzzle"]["id"] = "old"                       # (as if yesterday's puzzle were different)
            await r.tick()
            posts = [x for x in r.t.sent if "🧩" in x]
            assert len(posts) == 1 and "Black to move" in posts[0] and "https://x.test/c/" in posts[0]
            assert len(posts[0].encode()) <= 130
            await r.tick(30)
            await r.tick(30)
            assert len([x for x in r.t.sent if "🧩" in x]) == 1          # not again the same day
            r.take()
            at(11, d=16)                                                 # the bot was down at 9: no late puzzle post
            r.m.state.data["puzzle"]["id"] = "old"
            await r.tick()
            assert not any("🧩" in x for x in r.t.sent)
    run(go())


def test_puzzle_commands_through_the_hub_do_not_leak_the_answer(monkeypatch):
    async def go():
        async with Rig(puzzle=False, opponent=Scripted()) as r:
            r.m.state.data["puzzle"] = puzzle_dict()
            r.m.state.data["puzzle_date"] = "2026-10-14"
            await r.tick()
            r.take()
            await r.say("!puzzle")
            assert "Black to move" in r.t.sent[-1]
            await r.say("!puzzle Rb8", name="Ann")
            assert "Rb8 ✓" in r.t.sent[-1] and "d7" in r.t.sent[-1]
            await r.say("!puzzle Rf8", name="Bob")
            assert "2 tries left" in r.t.sent[-1]
            await r.say("!puzzle b2", name="Ann")
            await r.say("!puzzle b1=Q", name="Ann")
            assert "Ann solved it" in r.t.sent[-1]
            assert r.m.state.data["players"]["Ann"]["puzzles"] == 1
            s = json.dumps(r.m.site_state())
            assert "solution" not in s and "b2b1q" not in s and '"Ann"' in s
            await r.say("!answer")
            assert "Rb8" in " ".join(r.t.sent[-2:])
    run(go())


# ================================================================== the web board
def have_git():
    return shutil.which("git") is not None


@pytest.mark.skipif(not have_git(), reason="git not installed")
def test_publisher_commits_pushes_throttles_and_survives_failure(tmp_path, monkeypatch):
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"), ("GIT_COMMITTER_NAME", "t"),
                 ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(k, v)
    remote, site = tmp_path / "remote.git", tmp_path / "site"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(site)], check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-B", "main"], cwd=site, check=True, capture_output=True)
    now = [1000.0]
    pub = SitePublisher(str(site), min_interval=60, clock=lambda: now[0])

    def commits():
        r = subprocess.run(["git", "--git-dir", str(remote), "rev-list", "--count", "main"], capture_output=True, text=True)
        return int(r.stdout.strip() or 0) if r.returncode == 0 else 0

    s = {"updated": 1, "game": {"fen": "a"}}
    assert pub.tick(s) is True and commits() == 1
    files = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                           capture_output=True, text=True).stdout.split()
    assert {"state.json", "index.html"} <= set(files)
    s2 = {"updated": 3, "game": {"fen": "b"}}
    now[0] += 10
    assert pub.tick(s2) is False and commits() == 1                         # changed, but inside the interval
    now[0] += 60
    assert pub.tick(s2) is True and commits() == 2                          # interval over: pushed
    now[0] += 120
    assert pub.tick({**s2, "updated": 4}) is False and commits() == 2       # only "updated" changed: nothing to publish
    assert json.load(open(site / "state.json"))["game"]["fen"] == "b"
    # the remote disappears: no exception, a later retry works again
    hidden = tmp_path / "gone.git"
    remote.rename(hidden)
    now[0] += 120
    assert pub.tick({"updated": 5, "game": {"fen": "c"}}) is False and pub.failures >= 1
    hidden.rename(remote)
    now[0] += 3600
    assert pub.tick({"updated": 6, "game": {"fen": "c"}}) is True and commits() == 3 and pub.failures == 0


def test_publisher_without_a_git_clone_just_writes_the_file(tmp_path):
    site = tmp_path / "plain"
    pub = SitePublisher(str(site), min_interval=0)
    assert pub.tick({"updated": 1, "a": 1}) is False
    assert json.load(open(site / "state.json"))["a"] == 1 and (site / "index.html").exists()
    assert SitePublisher("").tick({"a": 1}) is False


def test_channel_join_link_is_built_from_the_radios_channel_secret():
    from types import SimpleNamespace
    from meshscore.radio import LibTransport

    class Cmds:
        async def get_channel(self, idx):
            assert idx == 8
            return SimpleNamespace(type="ok", payload={"channel_name": "#chess\x00", "channel_secret": bytes(range(16))})

    t = LibTransport()
    t.ET = SimpleNamespace(ERROR="err")
    t.mc = SimpleNamespace(commands=Cmds())
    t.chans = {"chess": 8}
    uri = asyncio.run(t.channel_uri("#chess"))
    assert uri == "meshcore://channel/add?name=%23chess&secret=000102030405060708090a0b0c0d0e0f"
    assert asyncio.run(t.channel_uri("#nope")) == ""


def test_channel_card_is_published_and_the_name_alone_when_unknown(tmp_path):
    async def go():
        async with Rig(site_dir=str(tmp_path / "s"), site_push=False, opponent=Scripted()) as r:
            r.t.uri, r.t.curi = "meshcore://aa", "meshcore://channel/add?name=%23chess&secret=00"
            await r.tick()
            st_ = json.load(open(tmp_path / "s" / "state.json", encoding="utf-8"))
            assert (st_["channel"]["name"], st_["channel"]["uri"]) == ("#chess", "meshcore://channel/add?name=%23chess&secret=00")
        os.remove("chess_state.json")
        async with Rig(site_dir=str(tmp_path / "s2"), site_push=False) as r2:
            await r2.tick()
            assert json.load(open(tmp_path / "s2" / "state.json", encoding="utf-8"))["channel"] == {"name": "#chess"}
    run(go())


def test_the_page_is_kept_in_step_unless_sync_is_off(tmp_path):
    site = tmp_path / "s"
    SitePublisher(str(site), min_interval=0).tick({"a": 1})
    page = site / "index.html"
    shipped = page.read_text(encoding="utf-8")
    page.write_text("old page", encoding="utf-8")
    SitePublisher(str(site), min_interval=0).tick({"a": 2})
    assert page.read_text(encoding="utf-8") == shipped                    # restored to the shipped version
    page.write_text("my own page", encoding="utf-8")
    SitePublisher(str(site), min_interval=0, sync_page=False).tick({"a": 3})
    assert page.read_text(encoding="utf-8") == "my own page"


def test_the_radios_contact_card_is_published_for_the_page(tmp_path):
    async def go():
        async with Rig(site_dir=str(tmp_path / "site"), site_push=False, opponent=Scripted()) as r:
            r.t.uri = "meshcore://c0ffee00"
            r.t.own_name = "SportsBot"
            await r.tick()
            st_ = json.load(open(tmp_path / "site" / "state.json", encoding="utf-8"))
            assert (st_["contact"]["name"], st_["contact"]["uri"]) == ("SportsBot", "meshcore://c0ffee00")
        async with Rig(site_dir=str(tmp_path / "site2"), site_push=False, show_contact=False) as r2:
            r2.t.uri = "meshcore://c0ffee00"
            await r2.tick()
            assert json.load(open(tmp_path / "site2" / "state.json", encoding="utf-8"))["contact"] is None
        async with Rig(site_dir=str(tmp_path / "site3"), site_push=False) as r3:      # a radio that cannot export one
            await r3.tick()                                   # (keeps the card remembered from an earlier start)
            assert json.load(open(tmp_path / "site3" / "state.json", encoding="utf-8"))["contact"]["uri"] == "meshcore://c0ffee00"
        os.remove("chess_state.json")
        async with Rig(site_dir=str(tmp_path / "site4"), site_push=False) as r4:      # ... and none when never known
            await r4.tick()
            assert json.load(open(tmp_path / "site4" / "state.json", encoding="utf-8"))["contact"] is None
    run(go())


def test_the_bundled_page_exists_and_reads_the_documented_state_keys():
    html = open(os.path.join(os.path.dirname(core.__file__), "chessplay", "site", "index.html"), encoding="utf-8").read()
    for key in ("state.json", "game.fen", "window_end", "votes", "puzzle", "players", "record", "contact", "channel", "chancard"):
        assert key.split(".")[-1] in html, key
