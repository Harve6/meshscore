# Running MeshScore day to day (Windows)

Everything you need to keep the bots going without help. Commands are for **PowerShell** in the meshscore folder:

```
cd C:\path\to\meshscore
```

(`python` below means `.\.venv\Scripts\python.exe`, the copy with everything installed. You can also write
`python -m meshscore ...` after activating the venv with `.\.venv\Scripts\Activate.ps1`.)

## 1. What is where

| thing | where |
|---|---|
| the program | the folder you cloned / unzipped |
| your settings (bots, radio, tokens, admin key) | `meshscore.toml` - **private**, never on GitHub |
| the log | `meshscore.log` (+ `.1`, `.2`, `.3` older ones) |
| game saves / bot memory | `*_state.json`, `pokemon-red.state` (and blue/yellow), `pokemon-inputs.log` |
| backups | `backups\meshscore-YYYYMMDD-HHMMSS.zip` (newest 14) |
| last health check | `health-latest.txt`, history in `health-history.log` |
| ROMs | wherever you keep them - never commit or share |
| web boards | the `site_url` of each bot in `meshscore.toml` |
| Cloudflare | optional: one Worker per board, see section 8 |
| board code | `contrib\cloudflare-board` |
| Wi-Fi radio notes | [wifi-radio.md](wifi-radio.md) |

## 2. Is everything OK?

```
.\.venv\Scripts\python.exe -m meshscore health
```

```
[OK  ] bot running    yes
[OK  ] radio link     connected 3 min ago
[OK  ] sending        last message sent 3 min ago
[OK  ] board chess    updated 3 min ago
...
All good.
```

With the scheduled task "MeshScore daily check" (`windows\daily-check.ps1`, section 10) it runs by itself every morning at 9:30: it makes a backup, writes
`health-latest.txt`, and **pops up a Windows notification only if something is wrong**.

Other quick looks:
* the last log lines: `Get-Content meshscore.log -Tail 30`
* watch it live: `Get-Content meshscore.log -Wait -Tail 20` (Ctrl+C to stop watching - the bot keeps running)
* the boards' footer says when the radio last heard someone.

## 3. Start / stop / restart

The bot can run as a Windows scheduled task named **MeshScore** (Task Scheduler: action `pythonw -m meshscore run --forever`
started from the meshscore folder, trigger at logon and every 5 minutes, "do not start a new instance"). It starts when you log in, Windows re-checks it every
5 minutes (and starts it if it is not running), and it restarts itself after an unexpected error.

```
Stop-ScheduledTask -TaskName MeshScore      # stop
Start-ScheduledTask -TaskName MeshScore     # start
```

Restart = stop, then start. If a stop leaves a copy behind (rare):
`Get-CimInstance Win32_Process | ? { $_.CommandLine -match 'meshscore' } | % { Stop-Process -Id $_.ProcessId -Force }`

It has no window. **Don't** start it from a terminal you then close (`python -m meshscore run` in a window is fine for
testing, but stop the task first: only one program can use the radio).

The PC must stay **on, awake and logged in**. Settings -> System -> Power: Screen can turn off; set "Sleep" to Never
when plugged in.

## 4. When something is wrong

| you see | it means | do this |
|---|---|---|
| health: `radio link FAIL ... not functioning` or lots of `TX FAIL` in the log | the radio's USB is stuck (bumped cable, cold, radio crashed) | unplug the radio, wait 5 s, plug in. It reconnects by itself within ~2 min. If not, press the radio's reset button |
| `could not open port 'COM3'` | the radio is unplugged, or Windows gave it a new port number | `python -m meshscore ports` - if it is now COM4 (etc.), edit `port = "COM4"` in `meshscore.toml`, restart |
| health: `bot running FAIL` | the bot is not running | `Start-ScheduledTask -TaskName MeshScore`, then look at the end of `meshscore.log` for why it stopped |
| `Another copy of MeshScore is already running` | two copies | stop the task, kill leftovers (section 3), start the task |
| a web board `WARN ... may look offline` | uploads aren't arriving | check the internet; look for `board upload failed` in the log; `HTTP 401` = token wrong (section 8) |
| `NHL error` / `MLB error` / `API error` now and then | a league website hiccup | nothing - it retries. Every day for a week = the league changed its feed: open a GitHub issue with the log lines |
| Pokemon screen looks stuck | nobody has pressed anything | send `!where` on `#pokemon`; the picture only changes when a vote is played |
| chess: "Which one? Send !move N" | two pieces can reach that square | answer `!move 1` or `!move 2` |
| the notification "MeshScore needs a hand" | the daily check found a FAIL | open `health-latest.txt` |

## 5. Changing settings

```
.\.venv\Scripts\python.exe -m meshscore edit      # opens meshscore.toml
.\.venv\Scripts\python.exe -m meshscore bots      # list bots
.\.venv\Scripts\python.exe -m meshscore bots add  # add one (asks questions)
.\.venv\Scripts\python.exe -m meshscore check --offline   # is the file OK?
```

Then restart (section 3). Useful settings: chess `window_minutes`, Pokemon `window_seconds`, `vote_hour`,
`puzzle_hour`, `[bot] log_chat = true` (log everything the radio hears - noisy), `[bot] admin_keys`.
Every setting is described in `docs\configuration.md`, `docs\chess.md`, `docs\pokemon.md`.

**Admin commands** only work as a **DM** from your key (`admin_keys = ["ab12cd"]`: the first hex digits of your key; use at least 6, longer is safer):
`!pokemon reset confirm`, `!pokemon switch blue`, `!chess newgame 3`, `!mode min` (any bot).

## 6. Backups

Made every morning by the daily check, or any time:

```
.\.venv\Scripts\python.exe -m meshscore backup
```

The zip holds `meshscore.toml` (with your upload tokens!) and all game/bot state, not the ROMs. To put one back:

```
Stop-ScheduledTask -TaskName MeshScore
.\.venv\Scripts\python.exe -m meshscore backup --restore backups\meshscore-20261018-123331.zip
Start-ScheduledTask -TaskName MeshScore
```

(the files it replaces are kept as `*.bak`). For safety against a dead disk, copy the `backups` folder to a USB stick
now and then - keep it private.

## 7. Updating the program

If you (or anyone) change the code on GitHub:

```
git pull
.\.venv\Scripts\python.exe -m pip install -e ".[chess,pokemon,qr,dev]"
.\.venv\Scripts\python.exe -m pytest -m "not network" -q        # ~4 minutes; all should pass
```

then restart. Your `meshscore.toml`, state and backups are not touched by `git pull`.

## 8. The web boards (Cloudflare)

* You need Node.js; run `npx wrangler login` once (it may ask again after a few months).
* Change the board page: edit `src\meshscore\chessplay\site\index.html` or `src\meshscore\pokeplay\site\index.html`, restart
  the bot - it uploads the new page by itself.
* Change the Worker itself (`contrib\cloudflare-board\src\worker.js`):
  `cd contrib\cloudflare-board; npx wrangler deploy --name <your-board-name>`
* **New upload token** (if one leaks): make a long random string, then
  `npx wrangler secret put TOKEN --name <your-board-name>` (paste it), put the same string in `site_token` of the
  bot(s) using that board, restart.
* Free-plan limits are generous for this; idle boards upload once every 10 minutes.

## 9. Moving the radio to Wi-Fi

See [wifi-radio.md](wifi-radio.md) (needs an ESP32-based radio; nRF52 boards such as the Heltec T096 have no Wi-Fi).

## 10. Phone alerts and the watchdog

Optional. In `meshscore.toml`:

```toml
[alerts]
ntfy_topic = "a-long-random-name"      # free ntfy app: subscribe to this topic on your phone (treat it like a password)
discord_webhook = ""                   # and/or a Discord channel webhook
```

`meshscore health --notify` tells you only when something *changes* (a problem, a reminder, "recovered");
`meshscore health --test-alert` proves the path. `windows\watchdog.ps1` runs it every 15 minutes as a scheduled task, and
`windows\daily-check.ps1` makes the morning backup.

## 11. Getting help later

* The MeshCore community knows the radios and firmware; MeshScore questions go in GitHub issues.
* When you ask, include: what you did, the output of `meshscore health`, and the last 30 lines of `meshscore.log`.
* The tests (`pytest -m "not network"`) tell you whether a change broke something.
* This guide plus `docs\*.md` explain the whole system - hand them to whoever (or whatever) is helping you first.
