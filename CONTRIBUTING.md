# Contributing

Thanks for helping. MeshScore lives on shared radio airtime, so the guiding rules are small and strict.

## Principles

1. **Airtime is precious.** Short messages (<= 130 bytes), no filler, nothing that repeats itself. A new alert type needs a
   good reason and a place in the `min` / `max` levels.
2. **Restarts are silent.** On start, record the current state and announce nothing that already happened.
3. **Real data first.** Alert logic is tested by replaying real past games, not invented fixtures. Pin games by id or
   date so tests don't depend on what's being played today.
4. **Be a polite API client.** Cache, poll gently, identify as MeshScore.
5. **MeshCore only.** The project is deliberately focused; Meshtastic/Discord ports are out of scope here.

## Setup

```bash
git clone https://github.com/Harve6/meshscore && cd meshscore
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -m "not network"     # everything offline, ~2 min (details: TESTING.md)
pytest -m network           # the live-API canary (needs internet)
ruff check .
```

## What to work on

* **A new league** - write a plugin ([guide](docs/writing-a-plugin.md)). If it's generally useful we'll move it into the
  core `leagues/` package.
* **Another HockeyTech league** (WHL, QMJHL, AHL, PWHL) - needs testing with the real feed; PRs with a short note on
  what differed are welcome.
* **Bugs** - open an issue with the log lines around it (`meshscore.log`) and the game id if you have it.
* **Docs** - anything that confused you.

## Pull requests

* Keep a PR to one change; include a test (a real-data replay for alert logic, an offline test otherwise).
* `pytest -m "not network"` and `ruff check .` must pass; run the real-data suites if you touched a league.
* Update `docs/` and `CHANGELOG.md` for user-visible changes.

## Code layout

```
src/meshscore/
  core.py        settings dict, text helpers, state, phases, alert levels
  config.py      meshscore.toml loading / validation / writing
  hub.py         transmit queue, command routing, airtime budget
  sameair.py     listen-before-send dedupe + line-up between bots
  radio.py       MeshCore link (serial / BLE / TCP) via the `meshcore` library
  plugin.py      LeaguePlugin base class for new leagues
  leagues/       nhl.py mlb.py mlb_postseason.py nfl.py hockeytech.py (+ plugin registry)
  cli.py         the `meshscore` command
tests/           offline tests (pytest) + suites/ real-data replays
examples/plugins a working plugin to copy
```
