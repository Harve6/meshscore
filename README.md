# MeshScore

**Live sports scores for [MeshCore](https://github.com/meshcore-dev) mesh channels.** One small program, one companion radio,
and your favourite team's goals, runs, touchdowns and finals show up on your mesh - in 130 bytes or less.

```
🏒 Puck drop: TOR @ MTL
🚨 TOR GOAL (PP) M. Matthews | TOR 1-0 MTL | 1st 12:41
⏸ End of 1st: TOR 1-0 MTL | SOG 14-9
🛑 FINAL: TOR 3 @ MTL 1 | SOG 31-24
⚾ JAYS 2 RUNS! Vladimir Guerrero Jr. homers (30) on a fly ball to left center field. | TOR 2-0 NYY | Bot 3rd
🏈 BUF TD! Josh Allen 2 Yd Rush (Tyler Bass Kick) | NE 14-14 BUF | Q2 1:00
```

MeshScore is built for MeshCore only (no Meshtastic, no Discord): short messages, airtime-aware, safe to run on a busy
mesh - including **more than one bot on the same air** ([how that works](docs/same-air.md)).

## What you get

* **One bot per team, any team**: NHL, MLB, NFL, plus the OHL/CHL (HockeyTech) - or a whole league / the MLB postseason.
* **Alerts that respect airtime**: three levels per bot (`max` every goal/run/score, `min` period/inning results + final,
  `off`), changeable from the mesh with `!mode`. Optional cap on messages per hour. Finals always go out.
* **Commands for everyone on the channel**: `!score` `!next` `!last` `!standings` `!series` `!bracket` `!player` `!leaders`
  and more ([full list](docs/commands.md)).
* **Several bots, one mesh**: bots listen before they send and form a line-up, so two of them following the same game
  post once - no coordination traffic, no config required.
* **Plugins**: add a league in ~40 lines ([guide](docs/writing-a-plugin.md)).
* **Not just sports**: a market bot posts a morning snapshot, a closing recap, big index moves, crypto and the odd
  headline, and answers `!quote AAPL` ([docs](docs/market.md)).
* **Chess for the whole channel**: everyone votes the next move with `!move`, the bot answers, and it gets stronger
  every time the crowd wins. A web board on GitHub Pages and a daily Lichess puzzle ([docs](docs/chess.md)).
* **Easy setup**: `meshscore init` finds your radio and channels, `meshscore check` tests everything before you go live.
* **Quiet by default**: no "I'm online!" spam on restarts, no replaying of games that were already over.
* **Real-data tests**: the alert logic is replayed against real past games (see [Testing](#testing)).

## Quick start

You need a MeshCore **companion** radio (USB, Bluetooth or WiFi firmware), a computer that stays on, and Python 3.10+.
Add the channel(s) you want to the radio first (in the MeshCore app: e.g. `#leafs`), then close the app so the bot can
use the radio - a companion radio talks to one program at a time.

```bash
pip install meshscore            # add  "meshscore[ble]"  for Bluetooth radios
meshscore init                   # radio, channels, teams -> meshscore.toml
meshscore check                  # validates the file, probes the data sources, connects to the radio
meshscore run
```

> **"meshscore is not recognized"?** pip installed the command into a folder that isn't on your PATH (common on
> Windows). `python -m meshscore init` (and `... check`, `... run`) does the same thing, or use `windows\install.bat`,
> which sets up a virtual environment where the command always works.

`meshscore init` asks how the radio is connected, reads the channels from it and offers the team codes for each league.
The result is a small file you can edit by hand:

```toml
[radio]
transport = "serial"          # serial (USB) | ble | tcp (WiFi)
port = "COM3"                 # /dev/ttyUSB0 on Linux

[bot]
timezone = "America/Toronto"

[bots.leafs]                  # the name is also the command prefix: "!leafs score"
type = "nhl"
team = "TOR"
channel = "#leafs"

[bots.jays]
type = "mlb"
team = "TOR"
tag = "JAYS"                  # what the messages say instead of the team code
channel = "#jays"
```

See [docs/configuration.md](docs/configuration.md) for every setting, and [`meshscore.example.toml`](meshscore.example.toml).

Add, remove or switch off bots later with `meshscore bots add|remove|enable|disable` and open the file with
`meshscore edit` - see [docs/managing-bots.md](docs/managing-bots.md).

### Docker, Windows, Raspberry Pi

| | |
|---|---|
| **Docker** | `docker compose up -d` - see [docs/deployment.md](docs/deployment.md) (a USB radio needs device passthrough) |
| **Windows** | double-click `windows\install.bat`, then `windows\run-meshscore.bat` |
| **systemd** | [`contrib/meshscore.service`](contrib/meshscore.service) |

## Leagues

| `type` | Covers | Data source | Notes |
|---|---|---|---|
| `nhl` | any NHL team | `api-web.nhle.com` | goals with scorer and PP/SH/EN tags, intermissions with shots, three stars, playoff `!series` |
| `mlb` | any MLB team | `statsapi.mlb.com` | runs with the play description, every-3-innings results, finals with W/L/S, `!player`, `!leaders`, `!follow` a player |
| `mlb-postseason` | every team, every round | `statsapi.mlb.com` | the whole bracket, tiered by round, World Series in full |
| `nfl` | any NFL team | ESPN site API | touchdown held for its conversion so TD + PAT is **one** message, quarter results, `!bracket` |
| `ohl` / `hockeytech` | OHL (default); one team or the league | HockeyTech feed | priority teams, Memorial Cup coverage, three stars; other HockeyTech leagues work with their own key ([notes](docs/data-sources.md)) |

These are free, unofficial endpoints; see [docs/data-sources.md](docs/data-sources.md) for what that means in practice.

## Using it on the mesh

Anyone on the channel can use the commands (use `!<botname> command` when several bots share a channel):

```
!score            today's game (or the next one)
!next / !last     upcoming games / the last result
!standings        division, conference or league table
!series / !bracket   playoff series status / bracket
!mode             show the alert level   (!mode max | min | off)
!ping !botstatus  link check / uptime, data freshness, airtime, peers
```

Send `!help` in the channel for the list that bot supports.

## Running more than one bot

Several people on one mesh each running MeshScore for their own team is the normal case - and so is two people running
the *same* team. MeshScore bots never talk to each other. They **listen**: a post another bot already made is not made
again, and when several bots are on the air they take turns in a fixed line-up (so there is a natural standby if one is
down). Details, limits and how to tune it: [docs/same-air.md](docs/same-air.md).

## Airtime

Every message is fitted to MeshCore's 130-byte channel limit, replies are capped at two messages, busy nights thin out
league-wide channels, and `[airtime] max_per_hour` puts a hard cap on alerts (finals always go out). On a quiet team
channel MeshScore sends a handful of messages a game.

## Writing a plugin

```python
from meshscore.plugin import LeaguePlugin

class MyLeague(LeaguePlugin):
    kinds = {"max": {"score", "final"}, "min": {"final"}, "off": set()}

    async def poll(self):
        ...                                  # fetch, compare, then:
        self.alert("score", "🏀 HOME 3-2 AWAY | Q2")
        return 30                            # seconds until the next poll

LEAGUES = {"myleague": MyLeague}
```

Point `[plugins] dirs = ["./my_plugins"]` at the folder (or publish it with a `meshscore.leagues` entry point) and use
`type = "myleague"`. Full guide with a working example: [docs/writing-a-plugin.md](docs/writing-a-plugin.md).

## Testing

```bash
pip install -e ".[dev]"
pytest -m "not network"      # everything offline, ~2 min: unit, property-based, whole-game replays, chaos, recorded real-data suites
pytest -m network            # the same suites against the live league APIs (feed-drift canary)
```

The alert logic is replayed against **real finished games** at every polling rhythm, with a restart at every point, and
against flaky feeds and dropping radios. How it works and how to add a case: [TESTING.md](TESTING.md).

## Contributing

New leagues, bug reports with a log excerpt, and docs fixes are all welcome - see [CONTRIBUTING.md](CONTRIBUTING.md).
Please keep messages short: every byte is airtime someone else on the mesh needs.

## Disclaimer

MeshScore is an independent hobby project. It is not affiliated with or endorsed by MeshCore, the NHL, MLB, NFL, the
CHL/OHL, ESPN or any team. Scores come from public, unofficial web endpoints that can change or disappear without
notice, and may be delayed or wrong - do not use them for betting or anything that matters. Be a good API citizen: the
default polling intervals are deliberately gentle; please don't lower them.

## License

[MIT](LICENSE)
