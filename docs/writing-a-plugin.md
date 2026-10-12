# Writing a league plugin

A plugin is a Python class. MeshScore gives it a name, a channel, the alert-level machinery (`!mode`), `!ping`,
`!botstatus`, `!help`, per-bot state, message fitting, same-air dedupe and the airtime budget. You supply two things:

1. **`poll()`** - fetch your data source, notice what changed, call `self.alert(kind, text)`, return how many seconds to
   wait before polling again.
2. **commands** (optional) - `!score` and friends, returning a list of reply strings.

## A complete example

`examples/plugins/coinflip.py` in this repo is a runnable template:

```python
from meshscore.plugin import LeaguePlugin

class CoinFlip(LeaguePlugin):
    label = "Coin flips"
    kinds = {"max": {"flip", "final"}, "min": {"final"}, "off": set()}   # alert kinds sent at each !mode level
    min_blurb = "every 5th flip"                                          # what "min" means (shown by !mode)

    def __init__(self, hub, name):
        super().__init__(hub, name)
        self.heads = self.tails = 0

    async def poll(self):
        ...                                                               # YOUR DATA SOURCE HERE
        self.alert("flip", f"🪙 Flip #{n}: heads {self.heads} - tails {self.tails}")
        self.mark_ok()                                                    # shown by !botstatus as "API 3s ago"
        return 60                                                         # seconds until the next poll

    def extra_commands(self):
        return {"!score": self.cmd_score}

    async def cmd_score(self, inc, arg):
        return [f"[{self.tag}] heads {self.heads} - tails {self.tails}"]

LEAGUES = {"coinflip": CoinFlip}
```

Enable it:

```toml
[plugins]
dirs = ["examples/plugins"]

[bots.coin]
type = "coinflip"
channel = "#coin"
```

## What you get from `LeaguePlugin`

| attribute / method | |
|---|---|
| `self.name`, `self.tag`, `self.channel`, `self.cfg` | from `[bots.NAME]` (`tag` defaults to the name, upper-cased) |
| `self.state.data` | a dict persisted to `NAME_state.json` - keep what must survive a restart here, then `self.state.save()` |
| `self.alert(kind, texts)` | post to the channel *if* the bot's `!mode` includes `kind` |
| `kinds` | `{"max": {...}, "min": {...}, "off": set()}` - which alert kinds each level sends |
| `extra_commands()` | `{"!cmd": async fn(inc, arg) -> [replies]}`; `inc.sender_name`, `inc.is_dm`, `inc.rest` (all words after the command) |
| `arg_cmds` | commands whose *argument* matters for the cooldown (e.g. `("!team",)`) |
| `help_text()` | the text after `[TAG BOT]` in `!help` |
| `self.mark_ok()` | call after each successful fetch (feeds `!botstatus`) |
| `CFG`, `fit`, `pack` | `from meshscore.plugin import CFG, fit, pack` - settings; `fit(text)` trims to the byte limit, `pack(header, items)` packs items into as few messages as fit |

## Rules of the road

* **Messages are at most 130 bytes (UTF-8).** `alert()` and replies are trimmed with `fit()`, but design the text to fit.
  Short team codes, no filler, one emoji at the front is plenty.
* **Never announce what already happened.** On the first poll after start, record the current state and say nothing; only
  *changes* are news. (Every built-in league does this; restarts must stay silent.)
* **Poll gently.** Return 20-30 s while a game is live and minutes when nothing is on. Cache. Send a
  `User-Agent` via `meshscore.core.session` (it already identifies MeshScore).
* **Blocking HTTP**: use `await asyncio.to_thread(requests_call, ...)` - the radio loop must never stall.
* **Fail soft**: an exception in `poll()` is logged and retried; return `None` data as "try again later", don't crash.
* **Tag your alert kinds** sensibly so `!mode min` is actually quieter.

## Distributing a plugin

Either drop the file in a folder listed in `[plugins] dirs`, or publish a package that declares an entry point:

```toml
# your plugin's pyproject.toml
[project.entry-points."meshscore.leagues"]
nba = "meshscore_nba:NbaLeague"
```

`pip install meshscore-nba` and `type = "nba"` just works. Please open an issue or PR to get your plugin listed in the README.

## Testing a plugin

You don't need a radio. `tests/test_config_cli.py::test_example_plugin_loads_and_answers_commands` shows the pattern:
build a `Hub` on a recording `Transport`, call `await plugin.poll()`, run `hub.tx_worker()`, and feed `Incoming(...)`
messages to `hub.on_message()`.
