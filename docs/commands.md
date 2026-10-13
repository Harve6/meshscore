# Commands

Send them in the bot's channel, or as a direct message. When several bots share a channel, put the bot's name first:
`!leafs score`, `!jays next`. Replies are at most two messages. A command is answered once per cooldown (15 s), and
identical answers from two bots are posted once ([same-air](same-air.md)).

## Every bot

| command | |
|---|---|
| `!help` | the commands this bot supports |
| `!mode` | show the alert level; `!mode max` / `min` / `off` sets it (see `admin_names` / `admin_keys`) |
| `!ping` | reply with hops and SNR of your message |
| `!botstatus` | uptime, data freshness, queue, alerts per hour, peer bots heard |
| `!phase` | which parts of the season are covered; `!phase pre|reg|post on|off` |

## NHL (`type = "nhl"`)

`!score` · `!next` · `!last` · `!standings [atl|met|cen|pac|east|west|wc]` · `!series [1-4|cf|scf]` (current round, your
team's series first; `!playoffs` / `!bracket` are the same)

## MLB (`type = "mlb"`)

`!score` · `!next` · `!last` · `!standings [east|central|west|wc|ale|alc|alw|nle|nlc|nlw]` ·
`!series` (your team's furthest series) · `!bracket` (all series, latest round first) ·
`!leaders hr|avg|rbi|...  [al|nl]` · `!leaders team` · `!player NAME` · `!follow PLAYER` / `!unfollow` / `!following`
(DM alerts: HR, RBI, pitcher lines, final line)

## MLB postseason (`type = "mlb-postseason"`)

`!scores` · `!series [al|nl|wc|ds|cs|ws]` · `!bracket` · `!ws` · `!next` · `!yesterday` · `!pitchers` · `!team X` ·
`!leaders` · `!player` · `!follow` ...

## Markets (`type = "market"`)

`!market` (the indices) · `!quote SYMBOL` (`AAPL`, `SHOP.TO`, `^GSPC`, `gold`, `btc` ...) · `!gainers` · `!losers` ·
`!active` · `!crypto [BTC ETH]` · `!commodities` / `!gold` / `!oil` ... · `!fx [USDCAD]` · `!news` · `!watch` ·
`!brief` · `!about` - details in [market.md](market.md)

## NFL (`type = "nfl"`)

`!score` · `!next` · `!last` · `!standings [afc|nfc]` · `!bracket [wc|div|conf|sb]` (`!playoffs` is the same)

## OHL / HockeyTech (`type = "ohl"`)

`!scores` · `!tomorrow` · `!team CODE` · `!next CODE` · `!standings [east|central|midwest|west|eastern|western]` ·
`!cup` (Memorial Cup) · `!priority CODE...` · `!follow CODE` / `!unfollow` / `!following` (DM alerts for a team)

## Alerts (what the channel gets)

| | `max` | `min` |
|---|---|---|
| hockey | puck drop, every goal (scorer, PP/SH/EN, hat tricks), end of 1st/2nd with shots, OT, final, three stars | end of 1st/2nd, OT, final, three stars |
| baseball | first pitch with the starters, every run (the play), extra innings, delays, final (W/L/S) | score after 3 and 6 innings, delays, final |
| football | kickoff, every score (scorer + play; TD and conversion as one message), end of each quarter, final | end of each quarter, final |

Corrections (a feed revising a score) are always announced at `max`; a tied "final" that is really overtime is never
announced as a final.
