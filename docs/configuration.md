# Configuration

MeshScore reads one TOML file, `meshscore.toml`. It is looked for in this order:

1. `--config FILE` / `-c FILE`
2. the `MESHSCORE_CONFIG` environment variable
3. `./meshscore.toml`
4. `%APPDATA%\meshscore\meshscore.toml` (Windows) or `~/.config/meshscore/meshscore.toml`

State files (alert level, followers, phases, last digest) and the log are written next to the config file, or into
`--data-dir`. `meshscore init` writes a starter file; `meshscore check` validates it and tests everything.

Unknown sections and unknown setting names are errors, so typos don't silently do nothing.

## `[radio]`

| setting | default | |
|---|---|---|
| `transport` | `"serial"` | `serial` (USB), `ble` (Bluetooth) or `tcp` (WiFi companion firmware) |
| `port` | | serial: `COM3`, `/dev/ttyUSB0` ... (`meshscore ports` lists them) |
| `baud` | `115200` | serial speed |
| `host`, `tcp_port` | | tcp: radio IP address and port (default `5000`) |
| `ble_address`, `ble_name`, `ble_pin` | | ble: device address (`meshscore scan-ble`), pairing PIN (MeshCore default `123456`) |

A companion radio accepts one client at a time: close the MeshCore phone/desktop app before starting the bot.

## `[bot]`

| setting | default | |
|---|---|---|
| `timezone` | system | IANA name such as `America/Toronto`; game times are shown in this zone |
| `default_mode` | `"max"` | alert level a new bot starts at: `max` / `min` / `off` (changed later with `!mode`) |
| `announce_on_start` | `false` | post a one-line "active on #channel" at start (restarts stay silent by default) |
| `dm_default` | first bot | which bot answers a plain `!command` sent by DM |
| `respond_to_dms` | `true` | answer commands sent by direct message |
| `admin_names` | `[]` | node names allowed to use `!mode`, `!phase`, `!priority`. Empty = anyone. **Names can be spoofed in a channel.** |
| `admin_keys` | `[]` | public-key prefixes allowed to do the same *by DM* - DMs are authenticated by MeshCore |
| `poll_live_seconds` | `20` | how often to poll while a game is on or about to start |
| `poll_idle_seconds` | `300` | how often to poll when nothing is happening |
| `busy_live_games` | `3` | league-wide channels: that many live games at once = "busy night", `max` thins down to `min` (0 = never) |
| `command_cooldown` | `15` | seconds before the same command is answered again |
| `send_gap` | `6` | minimum seconds between transmissions |
| `max_msg_bytes` | `130` | MeshCore's channel message limit - leave it |
| `max_reply_msgs` | `2` | longest reply to a command, in messages |
| `max_follows` | `3` | teams / players one person can follow by DM |
| `dm_even_if_broadcast` | `false` | DM followers even when the channel already got the alert |
| `instance_port` | `47653` | local port used to stop a second copy starting on the same computer (0 = no check) |
| `log_file` | `"meshscore.log"` | rotating log (empty = console only) |
| `phases` | `{pre=false, reg=true, post=true}` | which parts of the season every bot covers; per bot with `phases = {...}` or live with `!phase` |
| `player_aliases` | `{}` | MLB nicknames: `{ vlad = 665489 }` makes `!player vlad` work |

## `[bots.NAME]`

One table per bot. `NAME` is also its command prefix on shared channels (`!leafs score`) and the name of its state file.

| setting | applies to | |
|---|---|---|
| `type` | all | `nhl`, `mlb`, `nfl`, `ohl` / `hockeytech`, `mlb-postseason`, or a plugin's type |
| `channel` | all | channel name on the radio, e.g. `"#leafs"` (several bots may share one) |
| `channel_index` | all | the radio's slot number, if auto-detection by name fails |
| `enabled` | all | `false` keeps the table but doesn't start the bot |
| `team` | nhl, mlb, nfl, ohl | team code (`meshscore teams nhl` lists them). For `ohl` empty = the whole league |
| `tag` | all | the text in messages and replies instead of the team code, e.g. `"JAYS"` |
| `label` | all | long name used in the "active" message and `check` |
| `digest_hour` | all | local hour of the morning "game today" post (posted only during that hour) |
| `phases` | all | `{pre = true}` ... per-bot override of which parts of the season to cover |
| `priority_teams` | ohl | `["KIT","GUE"]`: always full alerts, even on busy nights and at `!mode min`; listed first |
| `overview`, `overview_months`, `overview_batch_minutes` | nhl | when your team is out of the playoffs, a light league-wide results post (months: `[4,5,6]`) |
| `season_id` | nhl | pin a season (e.g. `20242025`) - used by the tests |
| `tiers` | mlb-postseason | how much of each round the channel gets: `{F="merged", D="key", L="lead", W="full"}` |
| `skip_teams` | mlb-postseason | team codes that have their own channel and shouldn't repeat here |
| `headsup_minutes`, `season` | mlb-postseason | "game starting soon" notice lead time; pin a season |
| `watch`, `indices`, `commodities`, `crypto`, `open_brief`, `close_recap`, `crypto_daily`, `news`, `move_alert_pct`, ... | market | the market bot's settings - all in [market.md](market.md) |

### Alert levels (`!mode`)

| level | hockey | baseball | football |
|---|---|---|---|
| `max` | every goal, period results, final, stars | every run, innings 3/6, final | every score, quarter results, final |
| `min` | end of 1st / 2nd, OT, final, stars | score after 3 and 6 innings, final | score at the end of each quarter, final |
| `off` | nothing | nothing | nothing |

## `[hockeytech]`

The OHL/CHL feed. Defaults work for the OHL.

| setting | default | |
|---|---|---|
| `client_code` | `"ohl"` | the league's HockeyTech client code |
| `api_key` | the OHL widget key | the league website's public widget key |
| `digest_hour` | `12` | recap/"today" hour |
| `early_final_seconds` | `90` | announce the final this long after the clock hits 00:00 with a winner if the feed is slow to close the game |
| `summary_fetch` | `true` | fetch scorer / shots / three stars from the game summary |

## `[same_air]`

See [same-air.md](same-air.md).

| setting | default | |
|---|---|---|
| `enabled` | `true` | listen before sending and skip posts another bot already made |
| `window_seconds` | `180` | how long a heard post counts as "already announced" |
| `slot_seconds` | `5` | line-up spacing: position 1 waits this long, position 2 twice that ... |
| `jitter_seconds` | `1.5` | small random extra so two bots never tie |
| `rank` | `-1` | position in the line-up (0 first); `-1` = automatic, by sorted node name |
| `peers` | `[]` | node names of your other bots, if the line-up should be active before they are heard |

## `[airtime]`

| setting | default | |
|---|---|---|
| `max_per_hour` | `0` | cap on alert messages per hour across all channels (0 = none). Finals and game starts always go out; goals/runs stop at the cap; period/inning results stop at 80% of it. Replies to commands are never capped. |

## `[plugins]`

```toml
[plugins]
dirs = ["./my_plugins"]     # every *.py there that defines LEAGUES = {...} is loaded
```

Packages can also register types with a `meshscore.leagues` entry point.
