"""Crowd-vs-bot game logic: parsing what people type, collecting votes, resolving a round.

No radio and no clock in here: the bot module feeds it times, which keeps every rule easy to test. The whole game is
a plain dict (``CrowdGame.data``) so it persists in the bot's state file; the board is rebuilt from the move list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import chess

CROWD = chess.WHITE            # the crowd always plays White; the bot answers as Black


# ---------------------------------------------------------------- parsing what people type
def parse_move(board: chess.Board, text: str):
    """-> (move, error). `error` is None, "illegal", "ambiguous" or "empty". Accepts SAN (Nf3, exd5, O-O, e8=Q),
    UCI (e2e4, e7e8q), and sloppy forms: lower-case pieces (nf3), zeros for castling (0-0), a trailing +/#/!/?."""
    raw = (text or "").strip()
    if not raw:
        return None, "empty"
    raw = raw.strip(".,;:!?'\"` ")
    s = re.sub(r"[+#!?]+$", "", raw)
    s = s.replace("0-0-0", "O-O-O").replace("0-0", "O-O").replace("o-o-o", "O-O-O").replace("o-o", "O-O")
    s = re.sub(r"^\d+\.+", "", s)               # "12.Nf3" / "12...Nc6"
    candidates = [s]
    if s[:1] in "nbrqk":                         # "nf3" -> "Nf3", but "bxc3" / "b4" stay pawn moves (tried first)
        candidates.append(s[0].upper() + s[1:])
    if s[:1] in "ACDEFGH":                       # "E4" -> "e4" (B is a bishop, so "B4" is left alone)
        candidates.append(s[0].lower() + s[1:])
    if re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbnQRBN]?", s):
        candidates.insert(0, s.lower())
    if re.fullmatch(r"[a-h][1-8]-[a-h][1-8]", s):       # e2-e4
        candidates.insert(0, s.replace("-", "").lower())
    if re.fullmatch(r"[a-h]x?[a-h]?[18]=?[qrbnQRBN]", s):    # "e8q" is a promotion written without "="
        candidates.append(re.sub(r"=?([qrbnQRBN])$", lambda m: "=" + m.group(1).upper(), s))
    ambiguous = False
    for c in candidates:
        if re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", c):
            try:
                mv = chess.Move.from_uci(c)
            except ValueError:
                continue
            if mv in board.legal_moves:
                return mv, None
            q = chess.Move.from_uci(c + "q") if len(c) == 4 else None     # "e7e8" with no piece = queen
            if q is not None and q in board.legal_moves and board.piece_type_at(mv.from_square) == chess.PAWN:
                return q, None
            continue
        try:
            return board.parse_san(c), None
        except chess.AmbiguousMoveError:
            ambiguous = True
        except ValueError:                        # invalid / illegal / empty-SAN errors all derive from ValueError
            continue
    return None, "ambiguous" if ambiguous else "illegal"


def san_line(board: chess.Board, ucis, limit: Optional[int] = None) -> list[str]:
    """Numbered SAN tokens for a list of UCI moves played from `board`: ['1.e4', 'e5', '2.Nf3', ...]."""
    b = board.copy()
    out = []
    for u in ucis:
        mv = chess.Move.from_uci(u)
        num = f"{b.fullmove_number}." if b.turn == chess.WHITE else (f"{b.fullmove_number}..." if not out else "")
        out.append(f"{num}{b.san(mv)}")
        b.push(mv)
    return out[:limit] if limit else out


def outcome_of(board: chess.Board):
    """-> None while the game goes on, else (winner, reason): winner is "crowd" | "bot" | "draw"."""
    if board.is_checkmate():
        return ("bot" if board.turn == CROWD else "crowd"), "checkmate"
    o = board.outcome()                       # stalemate, insufficient material, fivefold, 75-move
    if o is not None:
        reason = {chess.Termination.STALEMATE: "stalemate",
                  chess.Termination.INSUFFICIENT_MATERIAL: "insufficient material",
                  chess.Termination.FIVEFOLD_REPETITION: "repetition",
                  chess.Termination.SEVENTYFIVE_MOVES: "75-move rule"}.get(o.termination, o.termination.name.lower())
        return "draw", reason
    if board.is_repetition(3):                # nobody is going to claim it on a radio: the bot calls these draws
        return "draw", "repetition"
    if board.halfmove_clock >= 100:
        return "draw", "50-move rule"
    return None


# ---------------------------------------------------------------- the game
@dataclass
class Round:
    number: int                 # full-move number the crowd's move was played on
    crowd_san: str
    votes: int                  # votes the winning move got
    voters: int                 # everybody who voted this round
    bot_san: Optional[str]      # None when the crowd's move ended the game
    winner: Optional[str]       # None while the game goes on, else "crowd" | "bot" | "draw"
    reason: str = ""
    chosen_by: tuple = ()       # voter ids whose vote was the move that got played
    voter_ids: tuple = ()


class CrowdGame:
    """One game: `data` is JSON-safe and lives in the bot's state file."""

    @staticmethod
    def new(game_id: int, level: int, window_end: float, now: float) -> dict:
        return {"id": game_id, "level": level, "moves": [], "votes": {}, "seq": 0, "window_end": window_end,
                "started": now, "over": False, "result": "", "reason": "", "announced": False}

    def __init__(self, data: dict):
        self.data = data

    # ---- position
    @property
    def board(self) -> chess.Board:
        b = chess.Board()
        for u in self.data["moves"]:
            b.push_uci(u)
        return b

    @property
    def over(self) -> bool:
        return bool(self.data.get("over"))

    def move_list(self) -> list[str]:
        return san_line(chess.Board(), self.data["moves"])

    def last_move(self):
        m = self.data["moves"]
        return (m[-1][:2], m[-1][2:4]) if m else None

    # ---- votes
    def vote(self, voter: str, text: str):
        """Record or change a vote. -> (status, san): status "ok" | "illegal" | "ambiguous" | "empty" | "over"."""
        if self.over:
            return "over", ""
        board = self.board
        if board.turn != CROWD:                  # never: the bot's reply is applied in the same step as the crowd's move
            return "over", ""
        mv, err = parse_move(board, text)
        if mv is None:
            return err, ""
        self.data["seq"] += 1
        self.data["votes"][voter] = [mv.uci(), self.data["seq"]]
        return "ok", board.san(mv)

    def tally(self) -> list[tuple[str, int]]:
        """[(san, votes)] best first; ties by whichever move got its first vote earliest."""
        board = self.board
        counts, first = {}, {}
        for u, seq in self.data["votes"].values():
            counts[u] = counts.get(u, 0) + 1
            first[u] = min(first.get(u, seq), seq)
        order = sorted(counts, key=lambda u: (-counts[u], first[u]))
        out = []
        for u in order:
            try:
                out.append((board.san(chess.Move.from_uci(u)), counts[u]))
            except ValueError:
                continue
        return out

    def leading_move(self) -> Optional[str]:
        counts, first = {}, {}
        for u, seq in self.data["votes"].values():
            counts[u] = counts.get(u, 0) + 1
            first[u] = min(first.get(u, seq), seq)
        if not counts:
            return None
        return sorted(counts, key=lambda u: (-counts[u], first[u]))[0]

    # ---- one full round
    def play_round(self, pick_bot_move) -> Optional[Round]:
        """Apply the winning vote and the bot's reply. `pick_bot_move(board) -> chess.Move` is only called when the
        game continues. Returns None when nobody voted (nothing changes). Clears the votes."""
        u = self.leading_move()
        if u is None or self.over:
            return None
        board = self.board
        number = board.fullmove_number
        crowd_mv = chess.Move.from_uci(u)
        crowd_san = board.san(crowd_mv)
        votes = sum(1 for m, _ in self.data["votes"].values() if m == u)
        voters = len(self.data["votes"])
        chosen = tuple(v for v, (m, _) in self.data["votes"].items() if m == u)
        voter_ids = tuple(self.data["votes"])
        board.push(crowd_mv)
        self.data["moves"].append(u)
        res = outcome_of(board)
        bot_san = None
        if res is None:
            bot_mv = pick_bot_move(board)
            bot_san = board.san(bot_mv)
            board.push(bot_mv)
            self.data["moves"].append(bot_mv.uci())
            res = outcome_of(board)
        self.data["votes"] = {}
        winner, reason = res if res else (None, "")
        if winner:
            self.data["over"], self.data["result"], self.data["reason"] = True, winner, reason
        return Round(number, crowd_san, votes, voters, bot_san, winner, reason, chosen, voter_ids)
