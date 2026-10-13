# The market bot: stocks, indices, commodities, crypto and headlines

A bot that isn't tied to a team: put it on a `#market` (or `#stocks`, `#finance` ...) channel and it posts a morning
snapshot, a closing recap, big moves and the odd headline - and answers `!quote AAPL` for anyone on the channel.
It is built on the same plugin API as every league (`type = "market"`), so `!mode`, `!ping`, airtime caps and quiet
restarts all work the way they do for hockey and baseball.

```
☀️ Pre-open: S&P fut 7,848 -0.1% | Nasdaq fut 30,779 -0.4% | Dow fut 52,132 0.0% | Gold 4,188 -0.1% | Silver 60.39 -0.2%
🔔 Close: S&P 7,807 -0.1% | Nasdaq 27,246 -0.4% | Dow 51,851 +0.4% | TSX 35,665 +1.5%
📈 FIGS +12.4% FICO +10.8% CCC +8.1% | 📉 CBRS -10.7% SMTC -10.2% CIEN -10.0%
🔊 Active: ONDS 74.1M | INTC 73.6M | SPCX 67.0M | Gold 4,188 -0.1% | Silver 60.39 -0.2% | Oil 92.93 -0.9%
🪙 Crypto: BTC 81,503 -2.7% | ETH 2,431 -4.2% | SOL 104.22 -6.2% | XRP 1.33 -5.3% | DOGE 0.0817 -5.7%
📰 Tuesday's big stock stories: What's likely to move the market in the next trading session (CNBC)
```

> **Information only.** Quotes come from free, unofficial public sources and can be delayed or wrong. Nothing the bot
> says is financial advice, a recommendation or an offer. `!about` says so on the air.

## Set it up

1. Add the channel to the radio in the MeshCore app (e.g. `#market`).
2. Add the bot - either run `meshscore init` / `meshscore bots add` and pick **Markets**, or add this to `meshscore.toml`:

```toml
[bots.market]
type = "market"
channel = "#market"
tag = "MKT"                       # the label in replies: "[MKT] S&P 7,807 ..."
watch = ["AAPL", "NVDA", "SHOP.TO"]   # optional: stocks to alert on and to show with !watch
```

3. `meshscore check` probes each data source; restart the bot.

Because the bot is named `market`, plain `!market` answers with the index snapshot. Any name works
(`[bots.stocks]` -> `!stocks`); every command also works as `!market quote aapl`.

## What it posts

Times are **New York (market) time**, which is also Toronto's. Weekends and NYSE holidays are skipped (the holiday
calendar is computed from the NYSE's rules, including "observed" days).

| kind | when | what |
|---|---|---|
| `brief` | 09:00 on trading days | index futures, gold / silver / oil, BTC and ETH |
| `recap` | 16:20 on trading days | the four indices, top gainers and losers, most active, commodities. Only if the data is from today. |
| `crypto` | 08:00, every day | your coins plus the biggest movers of the top 100 |
| `move` | market hours | an index crossing 1 / 2 / 3 / 5 / 7 % on the day - once per level and direction |
| `stock` | market hours | a `watch` stock moving 5 % or more - once a day each |
| `coin` | any time | one of your coins moving 7 % or more in 24 h - at most once per 12 h each |
| `news` | 07:00-21:00 | a new business headline: at most one per 2 hours and 4 a day, never one older than 3 hours |

**Quiet by design.** A scheduled post is only sent if the bot is up at its time (or within 10 minutes of it): a
restart at lunchtime does not replay the morning brief. After a restart the first look at the markets is just noted -
a 2 % drop that happened before the bot started is not announced as news. A daily cap (`max_alerts_per_day`, default
10) stops a wild day from flooding the channel, and `[airtime] max_per_hour` applies as for every bot.

`!mode min` keeps only the brief and the recap. `!mode off` silences everything (commands still answer).

## Commands

| command | answers |
|---|---|
| `!market` / `!indices` | S&P, Nasdaq, Dow, TSX - and whether the market is open, pre-market or closed |
| `!quote SYMBOL` (`!q`, `!price`, `!stock`) | any Yahoo Finance symbol: `AAPL`, `SHOP.TO`, `^GSPC`, `GC=F`, `BTC-USD`, `EURUSD=X` ... or a word: `gold`, `oil`, `tsx`, `nasdaq`, `btc` |
| `!gainers` / `!losers` | the biggest movers of the day (US stocks over $1) |
| `!active` (`!volume`) | most actively traded, with volume and the move |
| `!crypto [BTC ETH ...]` | your coins, or the ones you name |
| `!commodities`, `!gold`, `!silver`, `!oil`, `!brent`, `!natgas`, `!copper` | futures prices |
| `!btc`, `!eth` | quick coin quotes |
| `!fx [PAIR]` | `USDCAD` by default, or `eurusd`, `gbpusd` ... |
| `!news` | the two latest headlines |
| `!watch` | your watchlist |
| `!brief` | the snapshot the morning brief carries, on demand |
| `!about` | where the data comes from, and the not-advice note |
| `!mode`, `!ping`, `!botstatus`, `!help` | as for every bot |

Replies are one or two messages of 130 bytes or less. A symbol is checked before it is looked up
(letters, digits and `. ^ = -` only, 12 characters at most).

## Settings

All optional except `channel`. Put them in the bot's table.

| setting | default | |
|---|---|---|
| `tag` | `"MKT"` | label in replies |
| `indices` | `["^GSPC","^IXIC","^DJI","^GSPTSE"]` | Yahoo symbols; `"^RUT:Russell"` sets the label |
| `watch` | `[]` | stocks for `!watch` and `stock` alerts (`"SHOP.TO"` for the TSX) |
| `commodities` | `["gold","silver","oil","natgas"]` | from `gold silver oil brent natgas copper` |
| `crypto` | `["BTC","ETH","SOL","XRP","DOGE"]` | tickers (top-100 coins via CoinGecko; others via Yahoo `XXX-USD`) |
| `open_brief`, `close_recap`, `crypto_daily` | `"09:00"`, `"16:20"`, `"08:00"` | `"HH:MM"` New York time, or `"off"` |
| `window_minutes` | `90` | how long after its time a scheduled post may still go out (when its data was not ready) |
| `news` | `true` | headlines on or off |
| `news_feeds` | CNBC Investing + MarketWatch MarketPulse | list of RSS URLs |
| `news_min_minutes`, `news_per_day` | `120`, `4` | headline pacing |
| `move_alert_pct` | `[1,2,3,5,7]` | the index levels that trigger a `move` post |
| `watch_alert_pct` | `5` | a watchlist stock's move that triggers a `stock` post |
| `crypto_alert_pct` | `7` | a coin's 24 h move that triggers a `coin` post |
| `max_alerts_per_day` | `10` | cap on `move` + `stock` + `coin` + `news` posts per day |
| `min_price` | `1.0` | ignore cheaper stocks in gainers / losers / most active |
| `movers_count` | `3` | names per list in the recap (1-6) |
| `poll_seconds` | `180` | how often it looks at the market while it is open |

## Where the data comes from

| | source | notes |
|---|---|---|
| quotes, indices, futures, FX, screeners | Yahoo Finance public endpoints (`query1.finance.yahoo.com`) | unofficial, no key. Stocks can be delayed up to 15 minutes depending on the exchange. |
| crypto | CoinGecko public API | no key; rate limited, so results are cached for 4 minutes |
| headlines | CNBC and MarketWatch RSS | titles only, with the source in brackets |

These are the same kind of free endpoints the sports bots use (see [data-sources.md](data-sources.md)): they can change
or disappear. When one does, the bot logs a warning, answers "Market data unavailable right now", and keeps going - the
others are unaffected. Use it for a hobby mesh, not for decisions.

## Plugin notes

`src/meshscore/leagues/market.py` is a worked example of a non-sports plugin: a `LeaguePlugin` subclass with its own
alert kinds, scheduled posts (with a poll interval clamped to the next scheduled time, so nothing runs late),
state that survives restarts, and a fake-internet / fake-clock test suite in `tests/test_market.py`. To re-record the
test data after a feed changes: `python tests/record_market_fixtures.py`.
