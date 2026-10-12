# Changelog

## 0.1.0 - first public release

MeshScore grew out of a private single-file bot that ran a real mesh's #ohl / #jays / #leafs / #bills channels
through a season. This release turns it into a package anyone can run.

* **One bot per team, any team**: NHL, MLB, NFL, OHL/CHL (HockeyTech), MLB postseason.
* **TOML configuration**, `meshscore init` wizard, `meshscore check`, `teams`, `ports`, `scan-ble`, `dump`.
* **Same-air coordination**: listen-before-send dedupe and an automatic line-up between bots, no extra airtime.
* **Airtime budget**: optional `max_per_hour` with priorities (finals first).
* **Plugin API** (`LeaguePlugin`, entry points, plugin directories).
* **Playoff coverage**: `!series` / `!bracket` for NHL, MLB and NFL.
* NFL touchdowns wait for their conversion so TD + PAT / 2-pt is one message.
* Hardened feeds: tied-final guard with "(corrected)" safety net, stale-update ignoring, goal-lag second source,
  three stars only when complete, silent restarts (no replay of what already happened).
* Admin controls by node name and (DM-authenticated) public-key prefix.
* Docker image, Windows quick start, systemd unit, CI.
