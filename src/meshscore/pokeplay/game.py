"""Votes and rounds for crowd-plays-Pokemon: no emulator, no radio, no clock of its own.

A round: the first vote starts a clock; when it runs out the most-voted action is played (ties: whichever got its
first vote earliest). An action is a button, and for the d-pad optionally how many tiles (``!up 5``). Everything is a
plain dict so it persists in the bot's state file.
"""
from __future__ import annotations

from .emulator import BUTTONS, DIRECTIONS, norm_button

MAX_STEPS = 8


def new_round() -> dict:
    return {"votes": {}, "seq": 0, "window_end": None}


def parse_vote(word: str, count: str = ""):
    """('up', 3) from ('up', '3'); ('a', 0) for a plain button; None when `word` is not a button. 0 = the default."""
    b = norm_button(word)
    if b is None:
        return None
    steps = 0
    if b in DIRECTIONS and str(count).strip().isdigit():
        steps = max(1, min(MAX_STEPS, int(str(count).strip())))
    return b, steps


def vote(rnd: dict, voter: str, word: str, count: str = ""):
    """Record or change a vote. -> (button, steps) or None when the word is not a button."""
    a = parse_vote(word, count)
    if a is None:
        return None
    rnd["seq"] += 1
    rnd["votes"][voter] = [a[0], a[1], rnd["seq"]]
    return a


def tally(rnd: dict) -> list[tuple[tuple[str, int], int]]:
    """[((button, steps), votes)] best first; ties by earliest first vote."""
    counts, first = {}, {}
    for b, s, seq in rnd["votes"].values():
        k = (b, s)
        counts[k] = counts.get(k, 0) + 1
        first[k] = min(first.get(k, seq), seq)
    return [(k, counts[k]) for k in sorted(counts, key=lambda k: (-counts[k], first[k]))]


def winner(rnd: dict):
    t = tally(rnd)
    return t[0] if t else None


def label(action: tuple[str, int]) -> str:
    b, s = action
    return f"{b}x{s}" if s else b


__all__ = ["BUTTONS", "MAX_STEPS", "new_round", "parse_vote", "vote", "tally", "winner", "label"]
