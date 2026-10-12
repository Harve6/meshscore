# Testing MeshScore

A scoreboard bot that says the wrong thing on a shared radio channel is worse than one that says nothing, so the test
suite is built around *real games* and *invariants*, not just examples.

```bash
pip install -e ".[dev]"
pytest -m "not network"     # everything, offline - what CI runs on every push (~2 minutes)
pytest tests/test_game_replays.py tests/test_properties.py   # the fast core (seconds)
pytest -m network           # the same real-data suites against the LIVE APIs (feed-drift canary)
```

## The layers

| layer | files | what it proves |
|---|---|---|
| **Whole-game replays** | `tests/test_game_replays.py` | Real finished games (NHL regulation / OT / shootout, MLB shutout / extras / 18 innings) are rebuilt as the stream of states a poller would see and pushed through the bot. For *every* polling rhythm (every poll, every 2nd, every 3rd, every 5th at each offset) and with every state delivered repeatedly: no alert twice, none out of order, scores never go backwards, every goal/run accounted for, exactly one final with the true score and the right OT / extra-innings tag, three stars / winner from the real data, every message <= 130 bytes. **And a restart at every single point of every game announces nothing that already happened.** |
| **Property-based** | `tests/test_properties.py` (hypothesis) | For *any* text: `fit` never exceeds the byte limit and stays valid UTF-8; `pack` messages always fit and keep order; signatures never raise; line-up delays are bounded; for any strings a user types, the config we write parses back to the same values. (It found two real bugs when first written.) |
| **Resilience / chaos** | `tests/test_resilience.py` | Radio drops mid-queue (messages delivered once, in order), transmissions fail (one retry, no dupes), stale alerts after a long outage are dropped but a final is kept, command floods are bounded, plugins that raise keep being polled, feeds that fail 40% of the time still yield every goal once and one final, an unexpected exception in the alert logic still announces the score and the final, three bots at random timings. |
| **Same-air** | `tests/test_sameair.py` | The listen-before-send logic, the line-up, the airtime budget, and whole bots on a simulated shared channel. |
| **Command line + radio link** | `tests/test_cli_radio.py`, `tests/test_config_cli.py` | `run` / `check` / `init` / `teams`, the radio pickers with scripted input, config validation with specific messages, the MeshCore link against a fake `meshcore` library (all three transports, channel and DM parsing, sends, error paths), plugin loading. |
| **Real-data suites** | `tests/suites/suite_*.py`, run by `tests/test_suites.py` | Long scenario scripts: every command of every league against real responses, tied-final and stale-update guards, OHL three-stars gating, goal-lag second source, NFL touchdown + conversion, season phases, shared channels, the radio connect path. |

## Fixtures: real data, offline, deterministic

The real-data suites used to need the internet and drifted with the calendar. Now:

* `tests/fixtures/<suite>.json.gz` holds every API response the suite asked for, recorded once from the live APIs, plus the
  moment of recording. Replay serves them and **freezes the bot's clock at that moment**, so "today +/- 8 days"
  requests match. A request that isn't in the fixture fails loudly (`FIXTURE MISS: <url>`).
* `tests/fixtures/games.json.gz` holds the finished games used by the whole-game replays.

```bash
python tests/record_fixtures.py            # re-record all suites (needs internet; a suite that fails keeps its old fixture)
python tests/record_fixtures.py nfl        # just one
python tests/record_games.py               # re-download the replay games (add ids at the top of the file for new cases)
MESHSCORE_FIXTURES=live python tests/suites/suite_nfl.py     # one suite against the live API
```

When a league changes its feed the weekly live canary (`pytest -m network`, scheduled in CI) goes red; re-record,
look at `git diff --stat`, fix the parser if needed, commit.

## Writing a test

* **A new alert rule?** Find a real game that exercises it (`tests/record_games.py` lists how), add its id there, and
  assert the invariant in `test_game_replays.py` - not just the one message you expect.
* **A bug report?** Reproduce it as a snapshot sequence from the game's feed, make it fail, fix it, keep the test.
* **A parser?** Parse a whole recorded season in the suites' fixtures; assert no exception and sane field ranges.
* Don't sleep to wait for async work - poll with `suitelib.until(...)`. Don't depend on today's date; pin it.

## Does the suite actually catch regressions?

It should. When changing alert logic, try breaking it on purpose and watch tests fail - for instance announce on
first sighting, or drop the `prev["state"] != "final"` guard. Both are caught within seconds by the whole-game replays
(that is how their repeated-delivery design was checked).

## Coverage

```bash
pytest -m "not network" --cov=meshscore --cov-report=term-missing
```

The in-process tests cover the core logic; the scenario suites run as subprocesses and add the league modules (combined
coverage is about 75%; the interactive wizard pickers and BLE scanning are the main untested surface).
