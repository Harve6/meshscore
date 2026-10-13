"""The market bot (leagues/market.py): parsers on recorded data, the NYSE calendar, the scheduled posts, the alerts
(once per level, silent on restart), headlines, commands through a real Hub. Offline: a fake internet and a fake clock."""
import asyncio
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from meshscore import config, core  # noqa: E402
from meshscore.core import CFG  # noqa: E402
from meshscore.hub import Hub  # noqa: E402
from meshscore.leagues import market as m  # noqa: E402
from meshscore.leagues import registry  # noqa: E402
from meshscore.radio import Incoming, Transport  # noqa: E402

NY = ZoneInfo("America/New_York")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "market")
MON = datetime(2026, 10, 12, tzinfo=NY)          # a Monday, no holiday


def fx(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def at(day, hh, mm=0, ss=0):
    return day.replace(hour=hh, minute=mm, second=ss)


# ---------------------------------------------------------------- the fake internet
def chart_json(sym, price, prev, mtime, itype="EQUITY", name="", vol=1_000_000):
    return json.dumps({"chart": {"result": [{"meta": {
        "symbol": sym, "shortName": name or sym, "instrumentType": itype, "regularMarketPrice": price,
        "chartPreviousClose": prev, "regularMarketVolume": vol, "regularMarketTime": mtime,
        "regularMarketDayLow": min(price, prev), "regularMarketDayHigh": max(price, prev)}}]}})


def rss_xml(items):
    body = "".join(f"<item><title>{t}</title><link>https://x/{g}</link><guid>{g}</guid><pubDate>{d}</pubDate></item>"
                   for t, g, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'


class World:
    """URL -> canned response, and the clock. `prices` is {symbol: (price, previous close)}."""

    def __init__(self, when):
        self.when = when
        self.prices = {}
        self.fail = set()                  # URL fragments that "time out"
        self.mtime = None                  # market time of the chart data (default: now)
        self.coins = json.loads(fx("coingecko.json"))
        self.cnbc, self.mw = [], []        # [(title, guid, pubDate)]
        self.calls = []

    def stamp(self, dt):
        return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")

    def get(self, url, timeout=15):
        self.calls.append(url)
        if any(f in url for f in self.fail):
            return 0, ""
        if "/v8/finance/spark" in url:
            syms = unquote(parse_qs(urlparse(url).query)["symbols"][0]).split(",")
            out = {s: {"timestamp": [int(self.when.timestamp())], "symbol": s, "chartPreviousClose": self.prices[s][1],
                       "close": [self.prices[s][0]]} for s in syms if s in self.prices}
            return 200, json.dumps(out)
        if "/v8/finance/chart/" in url:
            sym = unquote(url.split("/chart/")[1].split("?")[0])
            if sym not in self.prices:
                return 404, "{}"
            price, prev = self.prices[sym]
            itype = "FUTURE" if sym.endswith("=F") else "INDEX" if sym.startswith("^") else "EQUITY"
            return 200, chart_json(sym, price, prev, self.mtime or int(self.when.timestamp()), itype, name=f"{sym} Inc.")
        if "/screener/predefined/saved" in url:
            return 200, fx(f"screener_{parse_qs(urlparse(url).query)['scrIds'][0]}.json")
        if "coingecko" in url:
            return 200, json.dumps(self.coins)
        if "id=15839069" in url:
            return 200, rss_xml(self.cnbc)
        if "mw_marketpulse" in url:
            return 200, rss_xml(self.mw)
        return 404, ""


@contextmanager
def env(world, bot=None, **cfg):
    """A fresh config with one market bot, a temp working dir (state files), the fake clock and the fake internet."""
    old_http, old_clock, cwd = m.http_get, core.CLOCK, os.getcwd()
    tmp = tempfile.TemporaryDirectory()
    try:
        os.chdir(tmp.name)
        config.reset()
        CFG.update(send_gap=0, command_cooldown=0, log_file="", **cfg)
        CFG["modules"] = {"market": {"enabled": True, "type": "market", "channel": "#market", "tag": "MKT", **(bot or {})}}
        core.CLOCK = lambda: world.when
        m.http_get = world.get
        yield tmp.name
    finally:
        m.http_get, core.CLOCK = old_http, old_clock
        os.chdir(cwd)
        config.reset()
        try:
            tmp.cleanup()
        except OSError:
            pass


class Quiet(Transport):
    name = "quiet"
    alive = True

    def __init__(self):
        self.sent = []

    async def send_channel(self, channel, text):
        self.sent.append((channel, text))
        return True


def make(world=None):
    hub = Hub(Quiet())
    return hub, m.MarketModule(hub, "market")


def drain(hub):
    out = []
    while not hub.out.empty():
        out.append(hub.out.get_nowait()[2])
    return out


def poll(mod, world, when=None):
    if when is not None:
        world.when = when
    mod.data.cache.clear()
    return asyncio.run(mod.poll())


def indices(world, **pct):
    """Set the four indices to given % changes on the day (default flat)."""
    for sym, base in (("^GSPC", 7800.0), ("^IXIC", 27000.0), ("^DJI", 51000.0), ("^GSPTSE", 35000.0)):
        p = pct.get(sym, pct.get("all", 0.0))
        world.prices[sym] = (round(base * (1 + p / 100), 2), base)


def everything(world):
    indices(world)
    for sym in ("ES=F", "NQ=F", "YM=F", "GC=F", "SI=F", "CL=F", "NG=F", "BZ=F", "HG=F"):
        world.prices[sym] = (100.0, 99.0)


def assert_fit(msgs):
    for t in msgs:
        assert len(t.encode("utf-8")) <= 130, (len(t.encode()), t)


# ---------------------------------------------------------------- parsers on real, recorded responses
def test_parsers_read_the_recorded_responses():
    q = m.parse_chart(json.loads(fx("chart_gspc.json")))
    assert q.symbol == "^GSPC" and q.price > 0 and q.prev > 0 and abs(q.pct) < 20 and q.mtime > 0
    assert m.parse_chart(json.loads(fx("chart_gold.json"))).symbol == "GC=F"
    assert m.parse_chart({"chart": {"result": None}}) is None and m.parse_chart("nonsense") is None
    sp = m.parse_spark(json.loads(fx("spark.json")))
    assert {"AAPL", "^GSPC"} <= set(sp) and sp["AAPL"].prev > 0
    for scr, sign in (("day_gainers", 1), ("day_losers", -1), ("most_actives", 0)):
        rows = m.parse_screener(json.loads(fx(f"screener_{scr}.json")))
        assert rows and all(r.symbol and r.price >= 1 for r in rows), scr
        if sign:
            assert all(r.pct * sign > 0 for r in rows), scr
    coins = m.parse_coins(json.loads(fx("coingecko.json")))
    assert coins[0].sym == "BTC" and coins[0].price > 0 and len(coins) >= 20
    for name in ("cnbc.xml", "marketwatch.xml"):
        news = m.parse_rss(fx(name), "X")
        assert len(news) >= 3 and all(n.title and "&#" not in n.title and "&apos;" not in n.title for n in news), name
        assert all(n.guid for n in news)


def test_parsers_survive_garbage():
    assert m.parse_screener({}) == [] and m.parse_screener(None) == []
    assert m.parse_coins({"status": {"error_code": 429}}) == []
    assert m.parse_rss("<html>not rss") == [] and m.parse_rss("") == []
    assert m.parse_spark({"X": {"close": [None], "chartPreviousClose": 0}}) == {}


# ---------------------------------------------------------------- text
def test_number_formats():
    assert m.fmt_price(7807.24) == "7,807" and m.fmt_price(340.97) == "340.97" and m.fmt_price(0.0817) == "0.0817"
    assert m.fmt_price(1.4282, "USDCAD=X") == "1.4282"
    assert m.fmt_pct(1.26) == "+1.3%" and m.fmt_pct(-0.04) == "0.0%" and m.fmt_pct(-12.44) == "-12.4%"
    assert m.fmt_vol(74_148_499) == "74.1M" and m.fmt_vol(950_000) == "950K" and m.fmt_vol(2.4e9) == "2.4B"


def test_headline_is_cut_at_a_word_and_keeps_the_source():
    n = m.News("Stocks tumble as bond yields climb to the highest level in sixteen years on inflation fears", "g", "CNBC")
    out = m.headline(n, 60)
    assert len(out.encode()) <= 60 and out.endswith("(CNBC)") and "..." in out and "yiel" not in out.replace("yields", "")
    assert m.headline(m.News("Short one", "g", "MW"), 60) == "Short one (MW)"
    assert m.clean_title("It&#x2019;s &amp;quot;big&amp;quot; — really") == "It's \"big\" - really"


def test_symbols_are_validated_and_aliased():
    assert m.resolve("aapl") == "AAPL" and m.resolve("gold") == "GC=F" and m.resolve("tsx") == "^GSPTSE"
    assert m.resolve("SHOP.TO") == "SHOP.TO" and m.resolve("btc") == "BTC-USD"
    for bad in ("../etc/passwd", "a b", "x" * 40, "AAPL;rm", "", "a/b"):
        assert m.resolve(bad) is None, bad


# ---------------------------------------------------------------- the NYSE calendar
def test_nyse_holidays():
    h26 = m.nyse_holidays(2026)
    expect = {(1, 1), (1, 19), (2, 16), (4, 3), (5, 25), (6, 19), (7, 3), (9, 7), (11, 26), (12, 25)}   # Jul 4 is a Saturday
    assert {(d.month, d.day) for d in h26} == expect
    h27 = m.nyse_holidays(2027)
    assert {(d.month, d.day) for d in h27} == {(1, 1), (1, 18), (2, 15), (3, 26), (5, 31), (6, 18), (7, 5), (9, 6), (11, 25), (12, 24)}
    assert (12, 31) not in {(d.month, d.day) for d in m.nyse_holidays(2027)}      # New Year's 2028 is a Saturday: no Friday off
    assert m.easter(2026).isoformat() == "2026-04-05" and m.easter(2027).isoformat() == "2027-03-28"


def test_market_state():
    mon = datetime(2026, 10, 12, tzinfo=NY)
    assert m.market_state(at(mon, 9, 29)) == "pre" and m.market_state(at(mon, 9, 30)) == "open"
    assert m.market_state(at(mon, 15, 59)) == "open" and m.market_state(at(mon, 16)) == "after"
    assert m.market_state(at(datetime(2026, 10, 10, tzinfo=NY), 12)) == "weekend"
    assert m.market_state(at(datetime(2026, 11, 26, tzinfo=NY), 12)) == "holiday"
    assert m.is_trading_day(mon.date()) and not m.is_trading_day(datetime(2026, 12, 25).date())
    assert m.parse_hhmm("09:05") == (9, 5) and m.parse_hhmm("off") is None and m.parse_hhmm("25:00") is None


# ---------------------------------------------------------------- scheduled posts
def test_morning_brief_posts_once_and_not_again_after_a_restart():
    w = World(at(MON, 8, 59))
    everything(w)
    with env(w):
        hub, mod = make()
        assert poll(mod, w) is not None and drain(hub) == []                 # 08:59: not yet
        poll(mod, w, at(MON, 9, 0, 20))
        out = drain(hub)
        assert 1 <= len(out) <= 2 and out[0].startswith("☀️ Pre-open:") and "S&P fut" in out[0], out
        assert_fit(out)
        poll(mod, w, at(MON, 9, 5))
        assert drain(hub) == []                                              # once a day
        hub2, mod2 = make()                                                  # a restart reads the same state file
        poll(mod2, w, at(MON, 9, 10))
        assert drain(hub2) == []


def test_a_start_long_after_the_slot_stays_silent():
    w = World(at(MON, 9, 40))
    everything(w)
    with env(w):
        hub, mod = make()
        poll(mod, w)
        assert [t for t in drain(hub) if t.startswith("☀️")] == []           # 40 minutes after the 09:00 slot: old news
        assert mod.state.data["sent"]["brief"] == "2026-10-12"               # ...and it will not be reconsidered
        poll(mod, w, at(MON, 10, 5))
        assert [t for t in drain(hub) if t.startswith("☀️")] == []
    w = World(at(MON, 11, 0))
    everything(w)
    with env(w):
        hub, mod = make()
        poll(mod, w)
        assert [t for t in drain(hub) if t.startswith("☀️")] == []           # past the window altogether


def test_weekend_and_holiday_have_no_brief_or_recap_but_crypto_goes_out_daily():
    sat = datetime(2026, 10, 10, tzinfo=NY)
    w = World(at(sat, 7, 59, 50))
    everything(w)
    with env(w):
        hub, mod = make()
        poll(mod, w)
        poll(mod, w, at(sat, 8, 0, 30))
        out = drain(hub)
        assert out and out[0].startswith("🪙 Crypto:") and "BTC" in out[0]
        assert_fit(out)
        poll(mod, w, at(sat, 9, 5))
        poll(mod, w, at(sat, 16, 25))
        assert drain(hub) == []
    thanks = datetime(2026, 11, 26, tzinfo=NY)
    w = World(at(thanks, 8, 59, 50))
    everything(w)
    with env(w):
        hub, mod = make()
        poll(mod, w)
        poll(mod, w, at(thanks, 9, 1))
        assert [t for t in drain(hub) if t.startswith("☀️")] == []


def test_closing_recap_has_indices_movers_and_active_and_fits():
    w = World(at(MON, 16, 19, 50))
    everything(w)
    with env(w):
        hub, mod = make()
        poll(mod, w)
        poll(mod, w, at(MON, 16, 21))
        out = drain(hub)
        assert len(out) == 3, out
        assert out[0].startswith("🔔 Close:") and "S&P" in out[0] and "TSX" in out[0]
        assert out[1].startswith("📈 ") and "📉" in out[1]
        assert out[2].startswith("🔊 Active:") and "Gold" in out[2]
        assert_fit(out)


def test_recap_waits_for_todays_data_then_gives_up_quietly():
    w = World(at(MON, 16, 19, 50))
    everything(w)
    with env(w):
        w.mtime = int(at(MON - timedelta(days=1), 16).timestamp())          # the feed still shows yesterday's close
        hub, mod = make()
        poll(mod, w)
        poll(mod, w, at(MON, 16, 21))
        poll(mod, w, at(MON, 16, 35))
        assert drain(hub) == [] and "recap" not in mod.state.data["sent"]    # still trying inside the first 30 minutes
        poll(mod, w, at(MON, 16, 51))
        assert drain(hub) == [] and mod.state.data["sent"]["recap"] == "2026-10-12"   # gave up for today, silently
        w.mtime = None
        poll(mod, w, at(MON, 17, 5))
        assert drain(hub) == []                                              # and does not post a late recap


def test_the_next_poll_never_oversleeps_a_scheduled_post():
    w = World(at(MON, 8, 50))
    everything(w)
    with env(w):
        hub, mod = make()
        d = poll(mod, w)
        assert 15 <= d <= 603, d                                             # 10 minutes to the brief: wakes at 09:00 +3 s
        assert poll(mod, w, at(MON, 8, 59, 55)) == 15                        # never less than 15 s
        d = poll(mod, w, at(MON, 9, 30, 10))                                 # market open: the watch cadence
        assert d <= 180
        w.when = at(MON, 22, 0)
        assert 15 <= poll(mod, w) <= 600


# ---------------------------------------------------------------- alerts
def test_index_move_alerts_fire_once_per_level_and_stay_quiet_on_restart():
    w = World(at(MON, 11, 0))
    with env(w, bot={"open_brief": "off", "close_recap": "off", "crypto_daily": "off", "news": False}):
        indices(w, all=-1.2)
        hub, mod = make()
        poll(mod, w)
        assert drain(hub) == []                                              # first look: already -1.2 % -> noted, not announced
        indices(w, **{"^GSPC": -2.1, "^IXIC": -2.6, "^DJI": -1.6, "^GSPTSE": -1.0})
        poll(mod, w, at(MON, 11, 4))
        out = drain(hub)
        assert len(out) == 1 and out[0].startswith("📉") and "S&P 7,636 -2.1%" in out[0], out
        assert_fit(out)
        poll(mod, w, at(MON, 11, 8))
        indices(w, **{"^GSPC": -2.4, "^IXIC": -2.9, "^DJI": -1.9, "^GSPTSE": -1.0})
        poll(mod, w, at(MON, 11, 12))
        assert drain(hub) == []                                              # same level: nothing new
        indices(w, **{"^GSPC": -3.2, "^IXIC": -3.5, "^DJI": -2.6, "^GSPTSE": -1.0})
        poll(mod, w, at(MON, 11, 16))
        assert len(drain(hub)) == 1                                          # the next level down
        hub2, mod2 = make()                                                  # restart: remembers the levels it announced
        poll(mod2, w, at(MON, 11, 20))
        assert drain(hub2) == []


def test_up_moves_use_the_up_arrow_and_mode_min_silences_alerts():
    w = World(at(MON, 11, 0))
    with env(w, bot={"open_brief": "off", "close_recap": "off", "crypto_daily": "off", "news": False}):
        indices(w)
        hub, mod = make()
        poll(mod, w)
        indices(w, all=2.2)
        poll(mod, w, at(MON, 11, 5))
        out = drain(hub)
        assert len(out) == 1 and out[0].startswith("📈")
        mod.state.data["mode"] = "min"
        indices(w, all=5.4)
        poll(mod, w, at(MON, 11, 10))
        assert drain(hub) == []


def test_watchlist_stock_alert_once_a_day():
    w = World(at(MON, 11, 0))
    with env(w, bot={"open_brief": "off", "close_recap": "off", "crypto_daily": "off", "news": False, "watch": ["NVDA", "AAPL"]}):
        indices(w)
        w.prices.update({"NVDA": (900.0, 880.0), "AAPL": (200.0, 199.0)})
        hub, mod = make()
        poll(mod, w)
        w.prices["NVDA"] = (930.0, 880.0)                                    # +5.7 %
        poll(mod, w, at(MON, 11, 4))
        out = drain(hub)
        assert len(out) == 1 and "NVDA +5.7%" in out[0] and out[0].startswith("📈"), out
        w.prices["NVDA"] = (940.0, 880.0)
        poll(mod, w, at(MON, 11, 8))
        assert drain(hub) == []


def test_alert_cap_per_day():
    w = World(at(MON, 11, 0))
    with env(w, bot={"open_brief": "off", "close_recap": "off", "crypto_daily": "off", "news": False,
                     "move_alert_pct": [1, 2, 3, 4, 5, 6], "max_alerts_per_day": 2}):
        indices(w)
        hub, mod = make()
        poll(mod, w)
        for i, p in enumerate((-1.5, -2.5, -3.5, -4.5), 1):
            indices(w, all=p)
            poll(mod, w, at(MON, 11, 4 * i))
        assert len(drain(hub)) == 2


def test_crypto_alert_after_baseline_and_not_again_for_12_hours():
    w = World(at(MON, 12, 0))
    with env(w, bot={"open_brief": "off", "close_recap": "off", "crypto_daily": "off", "news": False, "crypto": ["BTC", "ETH"]}):
        indices(w)
        for c in w.coins:
            c["price_change_percentage_24h"] = 0.5
        hub, mod = make()
        poll(mod, w)
        assert drain(hub) == []
        w.coins[0]["price_change_percentage_24h"] = -8.3                     # BTC
        poll(mod, w, at(MON, 12, 10))
        out = drain(hub)
        assert len(out) == 1 and out[0].startswith("🪙 BTC -8.3%"), out
        poll(mod, w, at(MON, 12, 20))
        assert drain(hub) == []
        poll(mod, w, at(MON, 12, 20) + timedelta(hours=13))
        assert len(drain(hub)) == 1                                          # still down 8 % thirteen hours later


# ---------------------------------------------------------------- headlines
def test_news_baseline_then_one_new_headline_at_a_time():
    w = World(at(MON, 10, 0))
    old = w.stamp(datetime(2026, 10, 12, 13, 0))
    w.cnbc = [("Old story one", "c1", old), ("Old story two", "c2", old)]
    with env(w, bot={"open_brief": "off", "close_recap": "off", "crypto_daily": "off", "news_min_minutes": 60, "news_per_day": 2}):
        indices(w)
        hub, mod = make()
        poll(mod, w)
        assert drain(hub) == []                                              # the page as it was at start is not news
        fresh = w.stamp(datetime(2026, 10, 12, 14, 30))
        w.cnbc.insert(0, ("Fed holds rates steady, signals one more cut this year", "c3", fresh))
        poll(mod, w, at(MON, 10, 35))
        out = drain(hub)
        assert out == ["📰 Fed holds rates steady, signals one more cut this year (CNBC)"], out
        w.mw = [("Retail sales jump in September", "m1", w.stamp(datetime(2026, 10, 12, 14, 50)))]
        poll(mod, w, at(MON, 10, 50))
        assert drain(hub) == []                                              # inside the 60 minute gap
        poll(mod, w, at(MON, 11, 40))
        assert [t[0] for t in drain(hub)] == ["📰"]                          # gap over: the next one goes
        w.mw.insert(0, ("Third headline today", "m2", w.stamp(datetime(2026, 10, 12, 16, 0))))
        poll(mod, w, at(MON, 13, 0))
        assert drain(hub) == []                                              # per-day cap of 2


def test_stale_headlines_and_night_time_are_not_posted():
    w = World(at(MON, 10, 0))
    with env(w, bot={"open_brief": "off", "close_recap": "off", "crypto_daily": "off", "news_min_minutes": 1,
                     "crypto": []}):                                         # (no coin alerts: this test is about headlines)
        indices(w)
        hub, mod = make()
        poll(mod, w)
        w.cnbc = [("Ancient history", "a1", w.stamp(datetime(2026, 10, 11, 12, 0)))]     # a day old
        poll(mod, w, at(MON, 10, 30))
        assert drain(hub) == []
        w.cnbc.insert(0, ("Breaking overnight", "a2", w.stamp(datetime(2026, 10, 13, 3, 0))))
        poll(mod, w, at(MON, 23, 30))
        assert drain(hub) == []                                              # 23:30 ET: not at night


# ---------------------------------------------------------------- commands, through a real Hub
def test_commands_end_to_end():
    w = World(at(MON, 16, 30))
    everything(w)
    w.prices["AAPL"] = (340.97, 336.64)
    w.cnbc = [("Markets close mixed as yields ease", "n1", w.stamp(datetime(2026, 10, 12, 20, 0)))]
    with env(w, bot={"watch": ["AAPL"]}):
        async def go():
            t = Quiet()
            hub = Hub(t)
            mod = m.MarketModule(hub, "market")
            hub.add(mod)
            tx = asyncio.create_task(hub.tx_worker())
            cw = asyncio.create_task(hub.cmd_worker())
            for text in ("!market", "!quote aapl", "!quote gold", "!quote ../etc", "!gainers", "!losers", "!active", "!crypto",
                         "!crypto sol", "!commodities", "!fx", "!news", "!watch", "!about", "!help", "!mode", "!market quote tsx"):
                await hub.on_message(Incoming(text, False, "Tester", "", 1, 5.0, "market"))
                await asyncio.sleep(0.15)                                   # the hub's command queue holds 10; real chat is slower
            await asyncio.sleep(0.6)
            tx.cancel(); cw.cancel()
            return [x for _, x in t.sent]
        sent = asyncio.run(go())
    assert_fit(sent)
    assert sent[0].startswith("[MKT] S&P 7,800") and "TSX" in sent[0] and "(closed)" in sent[0], sent[0]   # "!market" = the indices
    joined = "\n".join(sent)
    assert "[MKT] AAPL 340.97 +1.3%" in joined
    assert "[MKT] Gold 100.00" in joined or "[MKT] Gold 100" in joined
    assert "[MKT] !quote SYMBOL" in joined                                   # the rejected symbol gets usage, not a fetch
    assert "Gainers:" in joined and "Losers:" in joined and "Most active:" in joined
    assert "Crypto: BTC" in joined and "Crypto: SOL" in joined
    assert "📰 Markets close mixed as yields ease (CNBC)" in joined
    assert "not financial advice" in joined and "[MKT BOT] !indices" in joined
    assert not any("../" in u for u in w.calls)


def test_data_outage_gives_a_polite_answer_and_never_raises():
    w = World(at(MON, 12, 0))
    everything(w)
    w.fail = {"finance", "coingecko", "cnbc", "dowjones"}
    with env(w):
        hub, mod = make()
        out = asyncio.run(mod.cmd_indices(Incoming("!indices", False, "T", "", 1, 5.0, "market"), ""))
        assert out == ["[MKT] Market data unavailable right now, try again soon"]
        poll(mod, w)                                                         # a poll with every source down is just quiet
        assert drain(hub) == []


# ---------------------------------------------------------------- wiring
def test_registered_validated_and_in_the_wizard(tmp_path=None):
    assert "market" in registry()
    config.reset()
    CFG.update(transport="serial", port="COM3", modules={"mkt": {"enabled": True, "type": "market", "channel": "#market"}})
    assert config.problems() == []                                           # no team needed
    config.reset()
    from meshscore.cli import run_init
    answers = iter(["6", "", "AAPL shop.to", "n", "America/Toronto"])      # league 6 = markets, default channel, watchlist
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "meshscore.toml")
        run_init(out, ask=lambda prompt="": next(answers), say=lambda *a: None,
                 radio={"transport": "serial", "port": "COM9"}, channels=["market", "public"])
        config.load_config(out, strict=True)
        b = CFG["modules"]["market"]
        assert b["type"] == "market" and b["channel"] == "#market" and b["tag"] == "MKT" and b["watch"] == ["AAPL", "SHOP.TO"]
    config.reset()


def test_a_sports_bot_alone_is_still_its_help_page():
    class Sport:
        name, channel, tag = "jays", "#jays", "JAYS"
        commands = {"!help": None, "!score": None}
        aliases = ()

    hub = Hub(Quiet())
    hub.add(Sport())
    inc = Incoming("!jays", False, "T", "", 1, 5.0, "x")
    mod, cmd, _ = hub.route(inc)
    assert cmd == "!help"


# ---------------------------------------------------------------- hardening
def test_a_slow_source_cannot_hold_up_the_command_worker():
    import time as _time
    w = World(at(MON, 12, 0))
    everything(w)
    with env(w):
        def slow(url, timeout=15):
            _time.sleep(1.5)
            return w.get(url)
        m.http_get = slow
        old = m.FETCH_TIMEOUT
        m.FETCH_TIMEOUT = 0.3
        try:
            hub, mod = make()

            async def go():
                t0 = _time.time()
                out = await mod.cmd_indices(Incoming("!indices", False, "T", "", 1, 5.0, "market"), "")
                return out, _time.time() - t0          # (asyncio.run itself waits for the stuck worker thread to finish)
            out, took = asyncio.run(go())
            assert took < 1.0 and out == ["[MKT] Market data unavailable right now, try again soon"], (took, out)
        finally:
            m.FETCH_TIMEOUT = old


def test_a_dead_source_is_not_retried_symbol_by_symbol_forever():
    w = World(at(MON, 12, 0))
    w.fail = {"finance"}
    with env(w):
        hub, mod = make()
        assert mod.data.quotes(["^GSPC", "^IXIC", "^DJI", "^GSPTSE", "AAPL", "MSFT"]) == {}
        assert len(w.calls) == 2                                           # gave up after two misses


def test_bad_settings_do_not_stop_the_bot_starting():
    w = World(at(MON, 12, 0))
    with env(w, bot={"poll_seconds": "soon", "news_per_day": "many", "move_alert_pct": ["a", "b"], "open_brief": "9 o'clock",
                     "movers_count": None, "commodities": ["gold", "unobtainium"], "crypto": "btc, eth"}):
        hub, mod = make()
        assert mod.poll_s == 180 and mod.news_cap == 4 and mod.tiers == [1.0, 2.0, 3.0, 5.0, 7.0]
        assert mod.sched["brief"] is None and mod.commodities == ["gold"] and mod.crypto == ["BTC", "ETH"]


def test_a_channel_that_is_not_on_the_radio_is_skipped_quietly():
    w = World(at(MON, 12, 0))
    with env(w):
        t = Quiet()
        t.chans = {"leafs": 3}
        hub = Hub(t)
        mod = m.MarketModule(hub, "market")
        mod.alert("brief", ["☀️ test"])
        assert drain(hub) == []
        t.chans["market"] = 4
        mod.alert("brief", ["☀️ test"])
        assert drain(hub) == ["☀️ test"]
