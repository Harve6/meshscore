# Changelog

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
