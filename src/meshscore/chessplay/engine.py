"""The bot's opponents.

``PyEngine`` is a small alpha-beta searcher over python-chess (material + piece-square tables, quiescence, mop-up
for won endings). It needs nothing beyond ``chess`` and plays roughly beginner -> club strength, which is what the
first levels need. Weaker levels deliberately add noise and the odd random move; stronger ones search deeper, always
inside a time limit (iterative deepening), so a move is never late.

``Opponent`` picks the engine for a level: Stockfish (``UCI_Elo``) from level 3 up when a binary is configured and
works, otherwise the pure-Python engine. Both are blocking calls - run them with ``asyncio.to_thread``.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

import chess

log = logging.getLogger("meshscore")

MATE = 100_000
INF = 10 ** 9
VALUE = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330, chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}


@dataclass(frozen=True)
class Level:
    name: str
    elo: int            # nominal strength (used for Stockfish's UCI_Elo and shown to players)
    depth: int          # search depth for the pure-Python engine
    noise: int = 0      # centipawns of random jitter added to each root move's score
    blunder: float = 0  # chance of simply playing a random legal move


LEVELS = [
    Level("Beginner", 500, 1, 220, 0.30),
    Level("Novice", 800, 1, 100, 0.10),
    Level("Casual", 1100, 2, 60, 0.04),
    Level("Club", 1400, 2, 20),
    Level("Strong", 1700, 3),
    Level("Expert", 2000, 4),
    Level("Master", 2400, 5),
    Level("Grandmaster", 2800, 6),
]
STOCKFISH_FROM = 3     # levels at/above this use Stockfish when it is available

_PST = {
    chess.PAWN: [0, 0, 0, 0, 0, 0, 0, 0, 50, 50, 50, 50, 50, 50, 50, 50, 10, 10, 20, 30, 30, 20, 10, 10,
                 5, 5, 10, 25, 25, 10, 5, 5, 0, 0, 0, 20, 20, 0, 0, 0, 5, -5, -10, 0, 0, -10, -5, 5,
                 5, 10, 10, -20, -20, 10, 10, 5, 0, 0, 0, 0, 0, 0, 0, 0],
    chess.KNIGHT: [-50, -40, -30, -30, -30, -30, -40, -50, -40, -20, 0, 0, 0, 0, -20, -40, -30, 0, 10, 15, 15, 10, 0, -30,
                   -30, 5, 15, 20, 20, 15, 5, -30, -30, 0, 15, 20, 20, 15, 0, -30, -30, 5, 10, 15, 15, 10, 5, -30,
                   -40, -20, 0, 5, 5, 0, -20, -40, -50, -40, -30, -30, -30, -30, -40, -50],
    chess.BISHOP: [-20, -10, -10, -10, -10, -10, -10, -20, -10, 0, 0, 0, 0, 0, 0, -10, -10, 0, 5, 10, 10, 5, 0, -10,
                   -10, 5, 5, 10, 10, 5, 5, -10, -10, 0, 10, 10, 10, 10, 0, -10, -10, 10, 10, 10, 10, 10, 10, -10,
                   -10, 5, 0, 0, 0, 0, 5, -10, -20, -10, -10, -10, -10, -10, -10, -20],
    chess.ROOK: [0, 0, 0, 0, 0, 0, 0, 0, 5, 10, 10, 10, 10, 10, 10, 5, -5, 0, 0, 0, 0, 0, 0, -5,
                 -5, 0, 0, 0, 0, 0, 0, -5, -5, 0, 0, 0, 0, 0, 0, -5, -5, 0, 0, 0, 0, 0, 0, -5,
                 -5, 0, 0, 0, 0, 0, 0, -5, 0, 0, 0, 5, 5, 0, 0, 0],
    chess.QUEEN: [-20, -10, -10, -5, -5, -10, -10, -20, -10, 0, 0, 0, 0, 0, 0, -10, -10, 0, 5, 5, 5, 5, 0, -10,
                  -5, 0, 5, 5, 5, 5, 0, -5, 0, 0, 5, 5, 5, 5, 0, -5, -10, 5, 5, 5, 5, 5, 0, -10,
                  -10, 0, 5, 0, 0, 0, 0, -10, -20, -10, -10, -5, -5, -10, -10, -20],
}
_KING_MG = [-30, -40, -40, -50, -50, -40, -40, -30, -30, -40, -40, -50, -50, -40, -40, -30,
            -30, -40, -40, -50, -50, -40, -40, -30, -30, -40, -40, -50, -50, -40, -40, -30,
            -20, -30, -30, -40, -40, -30, -30, -20, -10, -20, -20, -20, -20, -20, -20, -10,
            20, 20, 0, 0, 0, 0, 20, 20, 20, 30, 10, 0, 0, 10, 30, 20]
_KING_EG = [-50, -40, -30, -20, -20, -30, -40, -50, -30, -20, -10, 0, 0, -10, -20, -30,
            -30, -10, 20, 30, 30, 20, -10, -30, -30, -10, 30, 40, 40, 30, -10, -30,
            -30, -10, 30, 40, 40, 30, -10, -30, -30, -10, 20, 30, 30, 20, -10, -30,
            -30, -30, 0, 0, 0, 0, -30, -30, -50, -30, -30, -30, -30, -30, -30, -50]


def _edge_distance(sq: int) -> int:
    """0 on the centre squares ... 3 in the corners (how far a king has been driven out)."""
    f, r = chess.square_file(sq), chess.square_rank(sq)
    return max(abs(2 * f - 7), abs(2 * r - 7)) // 2


_EDGE = [_edge_distance(s) for s in chess.SQUARES]


class _OutOfTime(Exception):
    pass


def evaluate(board: chess.Board) -> int:
    """Static score in centipawns from the side to move's point of view."""
    white = black = 0
    non_pawn = 0
    kings = {}
    for sq, p in board.piece_map().items():
        pt = p.piece_type
        if pt == chess.KING:
            kings[p.color] = sq
            continue
        v = VALUE[pt]
        if pt != chess.PAWN:
            non_pawn += v
        if p.color == chess.WHITE:
            white += v + _PST[pt][chess.square_mirror(sq)]
        else:
            black += v + _PST[pt][sq]
    endgame = non_pawn <= 2600
    kt = _KING_EG if endgame else _KING_MG
    if chess.WHITE in kings:
        white += kt[chess.square_mirror(kings[chess.WHITE])]
    if chess.BLACK in kings:
        black += kt[kings[chess.BLACK]]
    score = white - black
    if endgame and len(kings) == 2 and abs(score) > 300:
        # mop-up: the stronger side drives the other king to the edge and brings its own king close
        winner = chess.WHITE if score > 0 else chess.BLACK
        bonus = 12 * _EDGE[kings[not winner]] + 4 * (7 - chess.square_distance(kings[winner], kings[not winner]))
        score += bonus if winner == chess.WHITE else -bonus
    return score if board.turn == chess.WHITE else -score


class _Search:
    def __init__(self, deadline: float | None, clock=time.monotonic):
        self.deadline, self.clock, self.nodes = deadline, clock, 0

    def tick(self):
        self.nodes += 1
        if self.deadline is not None and not self.nodes & 255 and self.clock() > self.deadline:
            raise _OutOfTime

    @staticmethod
    def order(board, moves):
        def key(m):
            s = 0
            if board.is_capture(m):
                victim = board.piece_type_at(m.to_square) or chess.PAWN     # en passant: the target square is empty
                s = 10_000 + 10 * VALUE[victim] - VALUE[board.piece_type_at(m.from_square)]
            if m.promotion:
                s += 9_000 + VALUE[m.promotion]
            return -s
        return sorted(moves, key=key)

    def qs(self, board, alpha, beta, ply, depth=4):
        self.tick()
        if board.is_check():
            moves = list(board.legal_moves)
            if not moves:
                return -MATE + ply
            if depth <= 0:
                return evaluate(board)
            best = -INF
            for m in self.order(board, moves):
                board.push(m)
                score = -self.qs(board, -beta, -alpha, ply + 1, depth - 1)
                board.pop()
                best = max(best, score)
                alpha = max(alpha, best)
                if alpha >= beta:
                    break
            return best
        stand = evaluate(board)
        if depth <= 0 or stand >= beta:
            return stand
        alpha = max(alpha, stand)
        caps = [m for m in board.legal_moves if board.is_capture(m) or m.promotion]
        for m in self.order(board, caps):
            board.push(m)
            score = -self.qs(board, -beta, -alpha, ply + 1, depth - 1)
            board.pop()
            if score >= beta:
                return score
            alpha = max(alpha, score)
        return alpha

    def ab(self, board, depth, alpha, beta, ply):
        self.tick()
        if depth <= 0:
            return self.qs(board, alpha, beta, ply)
        moves = list(board.legal_moves)
        if not moves:
            return -MATE + ply if board.is_check() else 0
        if board.is_insufficient_material() or board.halfmove_clock >= 100 or board.is_repetition(2):
            return 0
        best = -INF
        for m in self.order(board, moves):
            board.push(m)
            score = -self.ab(board, depth - 1, -beta, -alpha, ply + 1)
            board.pop()
            if score > best:
                best = score
            alpha = max(alpha, best)
            if alpha >= beta:
                break
        return best


class PyEngine:
    """Pure-Python opponent. ``best_move`` always returns a legal move for a board that has one."""

    def __init__(self, rng: random.Random | None = None, clock=time.monotonic):
        self.rng, self.clock = rng or random.Random(), clock

    def best_move(self, board: chess.Board, level: Level, seconds: float = 15.0) -> chess.Move:
        moves = list(board.legal_moves)
        if not moves:
            raise ValueError("no legal moves")
        if len(moves) == 1:
            return moves[0]
        if level.blunder and self.rng.random() < level.blunder:
            return self.rng.choice(moves)
        board = board.copy(stack=True)          # the caller's board is never touched
        deadline = self.clock() + seconds
        if level.noise:
            return self._noisy(board, moves, level, deadline)
        return self._deepening(board, moves, level, deadline)

    def _noisy(self, board, moves, level, deadline):
        s = _Search(deadline, self.clock)
        base = len(board.move_stack)
        scored = []
        try:
            for m in moves:
                board.push(m)
                score = -s.ab(board, level.depth - 1, -INF, INF, 1)
                board.pop()
                scored.append((score + self.rng.gauss(0, level.noise), m))
        except _OutOfTime:
            while len(board.move_stack) > base:          # the abandoned search left moves pushed
                board.pop()
        if not scored:
            return self.rng.choice(moves)
        return max(scored, key=lambda x: x[0])[1]

    def _deepening(self, board, moves, level, deadline):
        s = _Search(deadline, self.clock)
        ordered = s.order(board, moves)
        best = ordered[0]
        base = len(board.move_stack)
        for depth in range(1, level.depth + 1):
            try:
                best_score, cand = -INF, None
                for m in ordered:
                    board.push(m)
                    score = -s.ab(board, depth - 1, -INF, -best_score if best_score > -INF else INF, 1)
                    board.pop()
                    if score > best_score:
                        best_score, cand = score, m
                best = cand
                ordered = [best] + [m for m in ordered if m != best]
                if best_score >= MATE - 100:             # a forced mate is found: no need to look deeper
                    break
            except _OutOfTime:
                while len(board.move_stack) > base:      # the abandoned search left moves pushed
                    board.pop()
                break
        return best


class Opponent:
    """Chooses a move for the bot at a given level (index into LEVELS)."""

    def __init__(self, stockfish_path: str = "", think_seconds: float = 15.0, rng: random.Random | None = None):
        self.stockfish_path = (stockfish_path or "").strip()
        self.think_seconds = float(think_seconds)
        self.py = PyEngine(rng)

    @property
    def uses_stockfish(self) -> bool:
        return bool(self.stockfish_path)

    def choose(self, board: chess.Board, level_index: int) -> chess.Move:
        level = LEVELS[max(0, min(level_index, len(LEVELS) - 1))]
        if self.stockfish_path and level_index >= STOCKFISH_FROM:
            try:
                return self._stockfish(board, level)
            except Exception as e:                       # missing binary, crash, timeout: the Python engine still plays
                log.warning("Stockfish failed (%s) - using the built-in engine for this move", e)
        return self.py.best_move(board, level, self.think_seconds)

    def _stockfish(self, board, level):
        import chess.engine
        with chess.engine.SimpleEngine.popen_uci(self.stockfish_path, timeout=20) as eng:
            if "UCI_Elo" in eng.options:
                opt = eng.options["UCI_Elo"]
                elo = max(opt.min or 1320, min(opt.max or 3190, level.elo))
                eng.configure({"UCI_LimitStrength": True, "UCI_Elo": elo})
            result = eng.play(board, chess.engine.Limit(time=min(2.0, self.think_seconds)))
        if result.move is None or result.move not in board.legal_moves:
            raise RuntimeError("engine returned no legal move")
        return result.move
