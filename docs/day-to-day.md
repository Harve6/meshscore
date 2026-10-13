# Day to day: start it, check it, change it (Windows)

Everything lives in one folder (yours is `Downloads\meshscore`). The parts you will touch:

| what | where | |
|---|---|---|
| **your setup** | `meshscore.toml` | the radio, and one `[bots.NAME]` table per bot |
| **the log** | `meshscore.log` | every post and command, plus warnings. Open it first when something is odd |
| **the start button** | `windows\run-meshscore.bat` | runs the bot and restarts it 10 seconds after any crash |
| **the program** | `.venv\` | Python and MeshScore itself - you never edit this |

Open a terminal in the folder (File Explorer -> the folder -> type `powershell` in the address bar), and use this
form for every command - it always works, whatever PATH looks like:

```powershell
.venv\Scripts\python.exe -m meshscore COMMAND
```

## 1. Start it

1. Close the MeshCore app. A companion radio talks to one program at a time, so if the app has it the bot can't.
2. Double-click `windows\run-meshscore.bat` (or run it from the terminal).
3. A window opens and prints a few lines. You want to see **`Connected. Channels: ...`** listing your channels.

**Stop it** by closing that window (or pressing Ctrl+C twice). A single Ctrl+C only stops the bot - the bat file then
restarts it after 10 seconds, which is what you want after a crash but not when you mean to stop.

## 2. Is it working?

* The window / `meshscore.log` shows `TX OK [#channel]: ...` for every post.
* From the mesh, on a bot's channel: `!ping` (answers "pong"), `!botstatus` (uptime, last data fetch, queue), `!score`.
* `meshscore check` tests the settings, every data source and the radio in one go:

```powershell
.venv\Scripts\python.exe -m meshscore check
```
(Stop the bot first, so the radio is free for the check.)

## 3. Change it - the easy ways, easiest first

**From the mesh, no restart** (anyone, or only `admin_names` if you set them):

| send | does |
|---|---|
| `!mode min` | that bot posts only period / inning / quarter results and finals. `!mode max` is everything, `!mode off` is silent |
| `!phase post on` / `!phase pre off` | which parts of a season a bot covers |
| `!jays mode min` | the same, addressed to a bot by name when several share a channel |

**From the terminal, then restart** - the `bots` commands edit `meshscore.toml` for you, keep your comments and save
the previous file as `meshscore.toml.bak`:

```powershell
.venv\Scripts\python.exe -m meshscore bots                      # list what you have
.venv\Scripts\python.exe -m meshscore bots add                  # asks: league, team, channel
.venv\Scripts\python.exe -m meshscore bots add --type market --channel "#market" --name market
.venv\Scripts\python.exe -m meshscore bots add --type mlb --team TOR --channel "#jays" --name jays --postseason
.venv\Scripts\python.exe -m meshscore bots disable market       # keeps its settings, stops it
.venv\Scripts\python.exe -m meshscore bots enable market
.venv\Scripts\python.exe -m meshscore bots remove market
.venv\Scripts\python.exe -m meshscore teams nhl                 # team codes for a league
```

**By hand**: `meshscore edit` opens `meshscore.toml` in Notepad. Settings are in
[configuration.md](configuration.md); the market bot's are in [market.md](market.md).

## 4. The loop for any change

1. **Stop** the bot (close its window).
2. **Change** it (a `bots` command, or `meshscore edit` and save).
3. **Check** it: `meshscore check` - it names the problem in plain words if the file is wrong.
4. **Start** it again.

A bad edit is never lost: the `bots` commands save the previous file as `meshscore.toml.bak`; copy it back over
`meshscore.toml` to undo.

## 5. Common jobs

| I want to ... | do |
|---|---|
| follow another team | `bots add` (answers the questions), add its channel on the radio first |
| quieter channel | `!mode min` from the mesh, or `default_mode = "min"` under `[bot]` |
| cap airtime | `[airtime]` -> `max_per_hour = 20` |
| playoffs of every team on my team's channel | `bots add ... --postseason` (MLB) |
| stocks / crypto / headlines | add the `market` bot (see [market.md](market.md)) |
| change what the bot is called in messages | `tag = "JAYS"` in that bot's table |
| change the time zone | `timezone = "America/Toronto"` under `[bot]` |

Channels must exist on the radio first (add them in the MeshCore app, then close the app).

## 6. When something is off

| you see | it means / do |
|---|---|
| `Could not connect to COM3` | the MeshCore app (or another program) has the radio, or the cable is out. Close it, re-plug, start again |
| `Channel(s) not on the radio: #x` | add `#x` in the MeshCore app, or fix the spelling in `meshscore.toml` |
| a bot is quiet | check `!botstatus` ("API ... ago"), then the log for warnings; the data source may be down - it retries by itself |
| `meshscore.toml` errors on start | `meshscore check` shows the exact line to fix; or restore `meshscore.toml.bak` |
| posts look doubled | two copies running (a second window?) - one radio, one bot. Close the extra window |

## 7. Updating the code

This install follows the `src` folder directly (an *editable* install). After any change to the code - yours or from
an update - just **restart the bot**; nothing needs reinstalling. If you ever make a fresh virtual environment, run
`windows\install.bat` again, then `.venv\Scripts\python.exe -m pip install -e . --no-deps`.
