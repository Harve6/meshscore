"""Saying a chess move in simple words: from-to, 'knight e3', numbered choices when two pieces can go there."""

import chess
import pytest

from meshscore import config
from meshscore.chessplay.game import candidates, parse_move
from meshscore.core import CFG

from test_chess import Rig, Scripted, run


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, command_cooldown=0, log_file="", timezone="America/Toronto")
    yield
    config.reset()


@pytest.mark.parametrize("text", ["g1 f3", "g1f3", "g1-f3", "g1 to f3", "knight f3", "n f3", "horse f3", "Knight  F3", "Nf3", "nf3"])
def test_the_ways_to_say_knight_to_f3(text):
    mv, err = parse_move(chess.Board(), text)
    assert err is None and mv.uci() == "g1f3", text


@pytest.mark.parametrize("text", ["pawn e4", "p e4", "e4", "e2 e4", "e2e4", "E4"])
def test_the_ways_to_say_pawn_to_e4(text):
    assert parse_move(chess.Board(), text)[0].uci() == "e2e4", text


def test_two_knights_can_reach_a_square_so_the_bot_has_to_ask():
    b = chess.Board("4k3/8/8/8/8/8/2N3N1/4K3 w - - 0 1")
    for t in ("Ne3", "knight e3", "n e3"):
        assert parse_move(b, t) == (None, "ambiguous"), t
        assert sorted(b.san(m) for m in candidates(b, t)) == ["Nce3", "Nge3"]
    assert parse_move(b, "c2 e3")[0].uci() == "c2e3"
    assert parse_move(b, "Nce3")[0].uci() == "c2e3"
    assert parse_move(b, "knight e5") == (None, "illegal")


def test_castling_and_promotion_in_words():
    b = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    assert parse_move(b, "castle short")[0].uci() == "e1g1"
    assert parse_move(b, "castle long")[0].uci() == "e1c1"
    assert parse_move(b, "castle") == (None, "ambiguous")
    p = chess.Board("8/4P3/8/8/8/8/8/k6K w - - 0 1")
    assert parse_move(p, "e7 e8")[0].uci() == "e7e8q"                         # no piece named: a queen
    assert parse_move(p, "e7 e8 knight")[0].uci() == "e7e8n"


PLY = ["g1f3", "a7a6", "d2d3", "b7b6"]                                           # Nb1 and Nf3 can both go to d2


def test_choice_flow_through_the_bot_with_numbered_answers():
    async def go():
        async with Rig(ack="chan", opponent=Scripted("e7e5")) as r:
            await r.tick()
            r.m.state.data["game"]["moves"] = list(PLY)
            r.take()
            await r.say("!move knight d2", name="Ann")
            ask = r.t.sent[-1]
            assert ask.startswith("Which one?") and "1=Nbd2(b1)" in ask and "2=Nfd2(f3)" in ask and len(ask.encode()) <= 130
            assert r.game.data["votes"] == {}
            await r.say("!move 2", name="Ann")
            assert r.game.data["votes"]["Ann"][0] == "f3d2"
            await r.say("!move 7", name="Bob")                                     # no choices waiting for Bob
            assert "no choices waiting" in r.t.sent[-1] and "Bob" not in r.game.data["votes"]
            await r.say("!move g1 f3", name="Cy")                                  # two words are one move
            await r.say("!move Nd2", name="Di")                                    # the old way still asks which
            assert r.t.sent[-1].startswith("Which one?")
    run(go())


def test_moves_can_be_asked_for_one_square_or_one_piece():
    async def go():
        async with Rig(opponent=Scripted()) as r:
            await r.tick()
            r.take()
            await r.say("!moves g1", name="Ann")
            assert r.t.sent[-1] == "[CHESS] knight g1: Nf3 Nh3"
            await r.say("!moves knight", name="Ann")
            assert r.t.sent[-1] == "[CHESS] knight: Na3 Nc3 Nf3 Nh3"
            await r.say("!moves e5", name="Ann")
            assert "Nothing of yours on e5 can move" in r.t.sent[-1]
            await r.say("!moves", name="Ann")
            assert r.t.sent[-1].startswith("[CHESS] legal:")
    run(go())
