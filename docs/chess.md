# Chess: the whole channel against a bot

Everybody on a channel plays **White together** by voting, and a bot plays Black. It is correspondence chess for a
radio mesh: one channel post per move, nobody has to be there at the same time, and a web page shows the board.

```
 #chess:  ♟ New game #1: crowd (White) vs bot L1 Beginner. Vote: !move e4 (any move)
 Ann:     !move e4
 Bob:     !move d4          <- the first vote started a 5 minute clock
 Cy:      !move e4
 #chess:  ♟ 1.e4 (2 votes) 1...e5 | L1 Beginner | !move <move>, clock starts at 1st vote (5m)
```

When the crowd checkmates the bot it levels up (Beginner, Novice, Casual, Club, Strong, Expert, Master, Grandmaster).
A loss or a draw starts a fresh game at the same level. A daily puzzle from Lichess is posted every morning.

## Set it up

```bat
pip install chess
python -m meshscore bots add --type chess --channel "#chess" --name chess
```

Create the channel in the MeshCore app first (the bot needs `#chess` on its radio). Restart MeshScore.

| `[bots.chess]` setting | default | |
|---|---|---|
| `channel` | | the channel everyone plays on |
| `window_minutes` | `5` | the vote clock; it starts at the **first** vote of a round. Fractions work (`0.5` = 30 s, the minimum) |
| `start_level` | `1` | 1-8, where a brand-new bot starts |
| `max_level` | `8` | the level cap (the crowd can beat the top level again and again) |
| `ack` | `"dm"` | how a vote is answered: `dm` (privately, when the radio knows the sender), `chan` (a short channel reply) or `off`. Illegal moves get a short channel reply unless `off` |
| `puzzle` | `true` | post the daily puzzle |
| `puzzle_hour` | `9` | local hour the puzzle is posted (only in this hour - a restart later in the day stays quiet) |
| `think_seconds` | `15` | time limit for the bot's move |
| `stockfish` | | path to a Stockfish binary: levels 4-8 then play as Stockfish at 1400-2800 Elo (otherwise the built-in engine) |
| `site_dir` | | local clone of the web board's GitHub Pages repo (see below) |
| `site_url` | | the board's address, quoted in the start and puzzle posts |
| `publish_min_seconds` | `60` | at most one `git push` per this many seconds |
| `site_push` | `true` | `false` writes `state.json` into `site_dir` without any git (to test locally) |
| `site_sync` | `true` | keep `index.html` in `site_dir` the same as the page that ships with MeshScore (`false` = your own edits stay) |
| `show_contact` | `true` | show the radio's contact QR on the page, so players can add the bot and receive its DMs |

## Commands

| | |
|---|---|
| `!move e4` / `!move Nf3` / `!move O-O` | vote (also `e2e4`, `nf3`, `0-0`; `!m` is a short form). Change your mind by voting again |
| `!move g1 f3` | **from-to**: the square the piece is on, then where it goes. Never ambiguous - the easy way when two pieces can reach a square |
| `!move knight e3`, `!move pawn e4` | piece name + square (`n`, `horse`, `bishop`, `rook`, `queen`, `king`, `pawn`). If two pieces can go there the bot answers `Which one? Send !move N: 1=Nce3(c2) \| 2=Nge3(g2)` - reply `!move 1` |
| `!move castle short` / `castle long` | castling in words |
| `!board` (or just `!chess`) | game number, level, last moves, vote counts, time left |
| `!moves` | the legal moves. `!moves g1` = what the piece on g1 can do; `!moves knight` = that piece type |
| `!top` | the crowd's record and who picks the winning moves |
| `!puzzle` / `!puzzle Qh5` | the daily puzzle / an answer (3 tries; a multi-move puzzle replies with the opponent's move) |
| `!answer` | the puzzle's solution |
| `!newgame [level]` | admins: abandon this game and start a new one |
| `!mode max\|min\|off` | `min` skips the per-move posts and sends only results and the puzzle |

Vote by DM too (`!chess move e4`) - a DM never touches the channel.

## The web board (GitHub Pages)

A GitHub Pages site can't listen to a radio, so the bot publishes the game and the page reads it:

1. Make a **public** repo (say `meshscore-chess`), turn on Pages for the `main` branch, clone it somewhere.
2. Put that folder in `site_dir` and the page's address in `site_url`.
3. Restart. The bot copies `index.html` into the folder and, whenever the game changes (at most once a minute),
   commits and pushes `state.json`. The page shows the board, the live votes and clock, the move list, the level
   ladder, the top contributors, the last games and the puzzle. It refreshes itself every 15 seconds, and warns
   when the bot hasn't published for 3 hours.

GitHub takes a minute or so to deploy a push, so the board runs about 1-2 minutes behind the radio. The state file
holds the names players used on the mesh and the position - never the puzzle's solution. If a push fails (no
internet, rejected) it is logged and retried with a growing delay; the game itself is never affected.

## What it keeps quiet about

* **Restarts post nothing.** The game is rebuilt from `chess_state.json`; if the vote clock ran out while the bot was
  down it starts again with the votes kept.
* **No votes, no posts.** A round's clock starts at the first vote, so an empty channel costs no airtime.
* **One post per round**, and the game-over post carries the result and the new game together when they fit.
* Votes during the bot's thinking time are refused (and told), not silently lost.

## Under the hood

* `chessplay/engine.py` - the built-in engine (alpha-beta, quiescence, mop-up) with per-level noise and depth, inside
  a time limit; `Opponent` uses Stockfish from level 4 up when `stockfish` is set and falls back to the built-in
  engine if it fails.
* `chessplay/game.py` - move parsing, votes, round resolution (threefold repetition and the 50-move rule are called
  automatically; nobody can claim a draw over a radio).
* `chessplay/puzzle.py` - the Lichess daily puzzle (`https://lichess.org/api/puzzle/daily`, no key). Any mating move
  solves a mate-in-N, as on Lichess.
* `tests/test_chess.py` - engine, rules, whole random games against the real engine, restart-at-every-round, the
  puzzle, and the git publisher against a real bare repository.
