# Data sources

MeshScore doesn't use paid or official feeds. Every league reads a free, public web endpoint of the kind the
league's own website or app uses. That keeps it free to run - and means you should know the trade-offs.

| `type` | Endpoint | Auth | Notes |
|---|---|---|---|
| `nhl` | `https://api-web.nhle.com/v1/...` | none | The NHL's web API. Goal lists lag the score by several seconds; MeshScore waits and cross-checks a second endpoint before naming a scorer. |
| `mlb`, `mlb-postseason` | `https://statsapi.mlb.com/api/v1/...` | none | MLB Stats API. Warm-up is reported as "live", rescheduled games appear twice under one id - both handled. |
| `nfl` | `https://site.api.espn.com/apis/site/v2/sports/football/nfl/...` | none | ESPN's site API. The team schedule has no live score, so live games are overlaid from the game summary. Scoring plays list the score *after* the extra point and can lag the scoreboard. |
| `ohl`, `hockeytech` | `https://lscluster.hockeytech.com/feed/index.php` | public widget key | The feed behind the OHL (and many other hockey leagues') own websites. It lags, skips 00:00, sometimes reports a tied "Final" before overtime and fills the three stars one at a time; MeshScore guards against each of these. |

## What that means

* **They're unofficial and undocumented.** They can change shape, rate-limit or vanish without notice. When one
  breaks, the bot logs a warning and keeps retrying; open an issue with the log lines and we'll adapt.
* **Be a good citizen.** Defaults poll every 20 s only while a game is live and every 5 min otherwise, and the
  responses are cached. Please don't lower the intervals, and don't run dozens of bots for one team - run one and let the
  mesh share it.
* **Terms of use.** Each league/provider has its own terms for its data; MeshScore is intended for personal,
  non-commercial use on a hobby radio mesh. You are responsible for how you use it. Don't use it for betting.
* **Scores can be wrong or late.** Feeds revise scores; MeshScore announces corrections as `⚠️ Score update`, but it can't
  know what a feed hasn't told it.

## The HockeyTech key

HockeyTech powers the websites of many hockey leagues (OHL, WHL, QMJHL, AHL, PWHL and more). Each league's pages load
the feed with a **public client code and key** that anyone can read from the page source - the OHL's is the default in
`[hockeytech]`. To point a bot at another HockeyTech league, set `client_code` and `api_key` there. MeshScore ships only
the OHL's public key and has only been tested with the OHL/CHL feeds; other leagues use the same format but may differ in
small ways - reports welcome.

## No live data when there's no game

Outside a season the bots idle (one gentle request every few minutes at most). Playoff commands (`!series`, `!bracket`)
answer "no playoffs right now" when there's nothing to show.
