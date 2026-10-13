"""The crowd-vs-bot chess game behind the `chess` bot type (see docs/chess.md).

* ``engine``  - the opponents: a small pure-Python engine (no downloads) and, optionally, Stockfish
* ``game``    - move parsing, votes, round resolution, results (no radio, no clock of its own)
* ``puzzle``  - the Lichess daily puzzle
* ``publish`` - the public ``state.json`` + git push that feeds the web board
"""
