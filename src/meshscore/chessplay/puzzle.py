"""The daily puzzle: fetched from Lichess, solved one DM / channel message at a time.

Lichess's daily puzzle (https://lichess.org/api/puzzle/daily, free, no key) comes with the position the solver starts
from (``puzzle.fen``) and the solution as UCI moves that alternate solver, opponent, solver, ... ending on a solver move.
A solver who reaches checkmate with an alternative move is accepted, as Lichess does.
"""
from __future__ import annotations

from typing import Optional

import chess

from .game import parse_move

DAILY_URL = "https://lichess.org/api/puzzle/daily"
MAX_TRIES = 3


def from_lichess(data: dict, date: str) -> Optional[dict]:
    """Daily-puzzle API response -> the dict kept in the bot's state, or None when it is unusable."""
    try:
        p = data["puzzle"]
        fen, sol = p["fen"], [str(m) for m in p["solution"]]
        board = chess.Board(fen)
        for u in sol:                                    # the whole line must be legal from the start position
            board.push_uci(u)
        if not sol or len(sol) % 2 == 0:
            return None
        return {"id": str(p["id"]), "date": date, "fen": fen, "solution": sol, "rating": int(p.get("rating") or 0),
                "side": "white" if chess.Board(fen).turn == chess.WHITE else "black",
                "progress": {}, "tries": {}, "solved": []}
    except (KeyError, TypeError, ValueError):
        return None


def fetch_daily(session, date: str, timeout: float = 15.0) -> Optional[dict]:
    r = session.get(DAILY_URL, timeout=timeout)
    r.raise_for_status()
    return from_lichess(r.json(), date)


def position(puz: dict, solver: str) -> chess.Board:
    """The board `solver` is looking at: the start position plus the moves already played in their line."""
    b = chess.Board(puz["fen"])
    for u in puz["solution"][: int(puz["progress"].get(solver, 0))]:
        b.push_uci(u)
    return b


def attempt(puz: dict, solver: str, text: str) -> dict:
    """Try a move. Mutates `puz` (progress / tries / solved) and returns
    {"status": "illegal"|"ambiguous"|"wrong"|"right"|"solved"|"locked"|"done", "san", "reply_san", "tries_left"}."""
    if solver in puz["solved"]:
        return {"status": "done"}
    if int(puz["tries"].get(solver, 0)) >= MAX_TRIES:
        return {"status": "locked"}
    board = position(puz, solver)
    mv, err = parse_move(board, text)
    if mv is None:
        return {"status": err}
    prog = int(puz["progress"].get(solver, 0))
    expected = puz["solution"][prog]
    san = board.san(mv)
    board.push(mv)
    if mv.uci() != expected and not board.is_checkmate():
        left = MAX_TRIES - int(puz["tries"].get(solver, 0)) - 1
        puz["tries"][solver] = MAX_TRIES - left
        return {"status": "wrong", "san": san, "tries_left": left}
    prog += 1
    if board.is_checkmate() or prog >= len(puz["solution"]):
        puz["progress"][solver] = len(puz["solution"])
        puz["solved"].append(solver)
        return {"status": "solved", "san": san, "n": len(puz["solved"])}
    reply = chess.Move.from_uci(puz["solution"][prog])
    reply_san = board.san(reply)
    puz["progress"][solver] = prog + 1
    return {"status": "right", "san": san, "reply_san": reply_san}


def answer_text(puz: dict) -> str:
    b = chess.Board(puz["fen"])
    out = []
    for u in puz["solution"]:
        mv = chess.Move.from_uci(u)
        num = f"{b.fullmove_number}." if b.turn == chess.WHITE else (f"{b.fullmove_number}..." if not out else "")
        out.append(f"{num}{b.san(mv)}")
        b.push(mv)
    return " ".join(out)
