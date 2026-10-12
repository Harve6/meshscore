# Changing your setup: add, remove or change bots (Windows and everywhere else)

Your whole setup is one text file, `meshscore.toml`, in the folder you ran `meshscore init` from. Bots are the
`[bots.NAME]` tables in it. There is nothing to reinstall to change them - edit, then restart.

> If `meshscore` isn't recognised on Windows, use `python -m meshscore ...` everywhere below (same thing).

## 1. See what you have

```bat
python -m meshscore bots
```
```
  tor            nhl             TOR    #leafs       on
  buf            nfl             BUF    #bills       on
  kit            ohl             KIT    #ohl         on
```

## 2. Add a bot

Close MeshScore (Ctrl+C in its window), then:

```bat
python -m meshscore bots add --type mlb-postseason --channel "#jays" --name mlb
```

or just `python -m meshscore bots add` and answer the questions (league, team, channel).

Types: `nhl`, `mlb`, `nfl`, `ohl`, `mlb-postseason`. Team codes: `python -m meshscore teams nhl`.

Rules of thumb:

* **The channel must already exist on the radio** (add it in the MeshCore app first), spelled the same.
* **Several bots can share a channel.** Plain commands (`!score`) go to the bot that has them; `!<name> command`
  (e.g. `!mlb series`) always reaches a specific one. Give each bot a short, distinct `--name`.
* **A team bot and the postseason bot on one channel** (e.g. the Jays bot and the MLB postseason bot on `#jays`): the
  postseason bot automatically skips that team's games (`skip_teams`) so nothing is posted twice.
* Each bot is independent: its own alert level (`!mode`) and its own state.

## 3. Turn a bot off, back on, or remove it

```bat
python -m meshscore bots disable mlb     REM keeps its settings, just stops it
python -m meshscore bots enable mlb
python -m meshscore bots remove mlb      REM deletes its [bots.mlb] table
```

## 4. Change anything else

```bat
python -m meshscore edit                 REM opens meshscore.toml in Notepad
```

Typical edits: a bot's `channel`, `team` or `tag`, the `[bot] timezone`, `admin_names`/`admin_keys`, `[airtime]
max_per_hour`. Everything is in [configuration.md](configuration.md). Every `bots` command and `edit` keeps your comments
and saves the previous file as `meshscore.toml.bak`, so a bad edit is one copy away from undone.

## 5. Check, then restart

```bat
python -m meshscore check                REM validates the file, tests each data source, the radio and each channel
python -m meshscore run
```

Changes are read at start-up, so restart the bot after any edit. (If it runs from `windows\run-meshscore.bat`, closing
the window and re-opening the batch file is enough.)

## Installing, updating, uninstalling

| | |
|---|---|
| **Update** | `pip install -U meshscore` (or, from a source folder, `git pull` - an editable install picks it up). Restart. |
| **Move to another PC** | copy `meshscore.toml` (and, if you like, the `*_state.json` files) next to a fresh install |
| **Start over** | `python -m meshscore init` (it asks before overwriting; your old file is kept as `.bak` by the `bots` commands) |
| **Uninstall** | `pip uninstall meshscore`, then delete the folder (config, state and logs live there) |
| **Windows virtual environment** | `windows\install.bat` created `.venv` in the project folder - deleting the folder removes everything |
