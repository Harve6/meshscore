# Changelog

## Unreleased

* **Pokemon Red bot** (`type = "pokemon"`): the whole channel plays one game. The first button vote of a round starts a
  30-second clock, the most popular button is pressed in a headless PyBoy emulator, and the screen goes to a web page
  (the screenshot repo is amended and force-pushed so it never bloats). The channel hears events read from the game's
  memory - area changes, battles, level ups, evolutions, badges, new Pokemon, wipe-outs. Saves resume after restarts.
  You supply the ROM; `*.gb` / `*.state` are git-ignored. Needs `pip install pyboy pillow`. See docs/pokemon.md.
* The channel / contact QR logic is shared between the chess and Pokemon pages.
* **Chess bot** (`type = "chess"`): the whole channel plays White in one game against a bot, by voting. The first
  `!move` of a round starts a 5-minute clock, the most popular move is played, the bot answers, one post per round.
  Eight levels (a built-in pure-Python engine; optional Stockfish for the upper ones); the bot moves up a level each
  time the crowd wins. A daily Lichess puzzle (`!puzzle e4`) and a web board: the bot commits `state.json` to a
  GitHub Pages repo and the bundled `index.html` shows the position, live votes, move list, level ladder and the
  puzzle. Needs `pip install chess` (the `[chess]` extra). See docs/chess.md.
* Plugins can list commands in `no_cooldown_cmds` to skip the shared per-channel command cooldown (a vote bot needs
  every voter's message, not one per 15 seconds).

* **Market bot** (`type = "market"`): indices, stocks, commodities, crypto and business headlines for a `#market`
  channel - a pre-open brief, a closing recap, index / watchlist / coin move alerts, `!quote` and friends. Built on the
  plugin API; no API keys. See docs/market.md.
* **MLB postseason as an option on a team channel**: `meshscore bots add --type mlb --team TOR --channel "#jays"
  --postseason` (and a question in `init` / `bots add`) adds the postseason bot beside the team bot with `skip_teams`
  set. Adding a team bot to a channel that already has the postseason bot updates its `skip_teams` automatically.
* **Cleaner play text**: when an MLB play description does not fit a message, runner moves go first, then the batted-ball
  clause and fielders' names are shortened, then names become last names - the hit and who scored are kept
  instead of cutting off mid-word.
* **Fix: late kickoff / first pitch / puck drop.** A game whose start time had passed but whose feed still said
  "scheduled" dropped the poll to the idle rate (5 minutes), so the start alert could be minutes late. It now keeps the
  live rate until the game flips (up to 3 hours).
* **Fix: channel scan.** All 16 radio slots are read; a channel after four or more empty slots was not found.
* `bare_command`: a bot can say what `!name` alone answers (the market bot: the index snapshot).

## 0.1.0 - first public release

MeshScore grew out of a private single-file bot that ran a real mesh's #ohl / #jays / #leafs / #bills channels
through a season. This release turns it into a package anyone can run.

* **One bot per team, any team**: NHL, MLB, NFL, OHL/CHL (HockeyTech), MLB postseason.
* **TOML configuration**, `meshscore init` wizard, `meshscore check`, `teams`, `ports`, `scan-ble`, `dump`.
* **Same-air coordination**: listen-before-send dedupe and an automatic line-up between bots, no extra airtime.
* **Airtime budget**: optional `max_per_hour` with priorities (finals first).
* **Plugin API** (`LeaguePlugin`, entry points, plugin directories).
* **Manage bots after install**: `meshscore bots` (list / add / remove / enable / disable, comment-preserving, with a
  `.bak` of the previous file) and `meshscore edit`.
* **Playoff coverage**: `!series` / `!bracket` for NHL, MLB and NFL.
* NFL touchdowns wait for their conversion so TD + PAT / 2-pt is one message.
* Hardened feeds: tied-final guard with "(corrected)" safety net, stale-update ignoring, goal-lag second source,
  three stars only when complete, silent restarts (no replay of what already happened).
* OHL: a shootout winner's +1 is no longer announced as a separate goal (the final carries it), like the NHL bot.
* Alert logic that hits an unexpected error falls back to a plain score/final update instead of losing the alert;
  alerts queued through a long radio outage are dropped as stale (finals are kept).
* Admin controls by node name and (DM-authenticated) public-key prefix.
* Docker image, Windows quick start, systemd unit, CI.
