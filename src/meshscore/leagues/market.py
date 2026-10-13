"""Markets: indices, stocks, commodities, crypto and business headlines - a non-sports bot (``type = "market"``).

It is built on the same plugin API as any league (``meshscore.plugin.LeaguePlugin``): `poll()` watches the data and
posts what changed, commands answer on demand, and `!mode` / `!ping` / state / airtime budget come from the framework.

What it posts (all times are New York market time; weekends and NYSE holidays are skipped):
    brief   a pre-open snapshot: index futures, gold, oil, bitcoin             (default 09:00)
    recap   the close: indices, top gainers / losers, most active, commodities (default 16:20)
    crypto  a daily crypto summary, 7 days a week                              (default 08:00)
    move    an index crossing +-1 / 2 / 3 / 5 / 7 % on the day (once per level, per direction)
    stock   a watchlist stock moving +-5 % on the day                          (once per day)
    coin    a coin moving +-7 % in 24 h                                        (once per 12 h)
    news    a new business headline, at most a few a day

Data: Yahoo Finance (unofficial public endpoints), CoinGecko, CNBC / MarketWatch RSS. No API keys. Quotes can be
delayed. Everything is informational - nothing here is financial advice.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote as urlquote

from .. import core
from ..core import CFG, fit, pack, session
from ..plugin import LeaguePlugin
from ..radio import chan_key

log = logging.getLogger("meshscore")

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:                                           # pragma: no cover - no tzdata on Windows: use local time
    NY = None

MARKET_KINDS = {
    "max": {"brief", "recap", "crypto", "move", "stock", "coin", "news"},
    "min": {"brief", "recap"},                              # the morning snapshot and the closing recap only
    "off": set(),
}

YAHOO = "https://query1.finance.yahoo.com"
COINGECKO = "https://api.coingecko.com/api/v3/coins/markets"
CNBC_INVESTING = "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069"
MW_PULSE = "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"
DEFAULT_FEEDS = [("CNBC", CNBC_INVESTING), ("MW", MW_PULSE)]

INDICES = [("^GSPC", "S&P"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow"), ("^GSPTSE", "TSX")]
FUTURES = [("ES=F", "S&P fut"), ("NQ=F", "Nasdaq fut"), ("YM=F", "Dow fut")]
COMMODITIES = {"gold": ("GC=F", "Gold"), "silver": ("SI=F", "Silver"), "oil": ("CL=F", "Oil"), "brent": ("BZ=F", "Brent"),
               "natgas": ("NG=F", "NatGas"), "copper": ("HG=F", "Copper")}
DEFAULT_COMMODITIES = ["gold", "silver", "oil", "natgas"]
DEFAULT_CRYPTO = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
NAMES = {**dict(INDICES), **dict(FUTURES), **{s: n for s, n in COMMODITIES.values()}, "^VIX": "VIX", "USDCAD=X": "USD/CAD"}
ALIASES = {"spx": "^GSPC", "sp500": "^GSPC", "s&p": "^GSPC", "nasdaq": "^IXIC", "dow": "^DJI", "tsx": "^GSPTSE",
           "vix": "^VIX", "btc": "BTC-USD", "eth": "ETH-USD", "usdcad": "USDCAD=X", "cad": "USDCAD=X",
           **{k: v[0] for k, v in COMMODITIES.items()}, "wti": "CL=F", "crude": "CL=F"}
STABLE_OR_WRAPPED = {"usdt", "usdc", "dai", "usde", "fdusd", "tusd", "usds", "pyusd", "usdd", "busd", "usdp", "gusd", "usdy",
                     "steth", "wsteth", "weth", "wbtc", "weeth", "cbbtc", "reth", "wbeth"}
_SYMBOL = re.compile(r"^[A-Za-z0-9.^=\-]{1,12}$")
FETCH_TIMEOUT = 25            # seconds one fetch (a command's lookup or a poll step) may take before it is abandoned
EMPTY = {"quote": None, "quotes": dict, "quotes_fast": dict, "screener": list, "coins": list, "news": list}
STARTUP_GRACE_MIN = 10        # a scheduled post is only sent if we are up at its time (or within this many minutes of it)
NEWS_MAX_AGE = 3 * 3600       # a headline older than this is not "news" any more
DISCLAIMER = "Quotes: Yahoo Finance, CoinGecko; news: CNBC, MarketWatch. May be delayed. Information only, not financial advice."


# ==========================================================================
# MARKET CALENDAR (NYSE).  Rules last verified October 2026 against NYSE's published holiday schedule.
# ==========================================================================
def easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth(year, month, weekday, n):
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last(year, month, weekday):
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d):
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def nyse_holidays(year):
    """{date: name} of the days the NYSE is closed. A holiday on a Saturday is observed on Friday and one on a Sunday
    on Monday - except New Year's Day, which the NYSE does not move back into the previous year."""
    out = {}
    ny = date(year, 1, 1)
    if ny.weekday() != 5:
        out[_observed(ny)] = "New Year's Day"
    out[_nth(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    out[_nth(year, 2, 0, 3)] = "Presidents Day"
    out[easter(year) - timedelta(days=2)] = "Good Friday"
    out[_last(year, 5, 0)] = "Memorial Day"
    out[_observed(date(year, 6, 19))] = "Juneteenth"
    out[_observed(date(year, 7, 4))] = "Independence Day"
    out[_nth(year, 9, 0, 1)] = "Labor Day"
    out[_nth(year, 11, 3, 4)] = "Thanksgiving"
    out[_observed(date(year, 12, 25))] = "Christmas"
    return out


def is_trading_day(d):
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def market_state(t):
    """'open' | 'pre' | 'after' | 'weekend' | 'holiday' for an aware datetime in market time."""
    d = t.date()
    if d.weekday() >= 5:
        return "weekend"
    if d in nyse_holidays(d.year):
        return "holiday"
    m = t.hour * 60 + t.minute
    return "pre" if m < 9 * 60 + 30 else "open" if m < 16 * 60 else "after"


def parse_hhmm(v):
    """'09:00' -> (9, 0); 'off' / '' / False / None -> None."""
    if v in (None, False, "", "off", "none", "no"):
        return None
    try:
        h, m = str(v).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except ValueError:
        pass
    return None


# ==========================================================================
# DATA
# ==========================================================================
@dataclass
class Quote:
    symbol: str
    name: str
    price: float
    prev: float
    vol: int = 0
    lo: float = 0.0
    hi: float = 0.0
    mtime: int = 0
    kind: str = ""

    @property
    def chg(self):
        return self.price - self.prev if self.prev else 0.0

    @property
    def pct(self):
        return (self.price - self.prev) / self.prev * 100 if self.prev else 0.0


@dataclass
class Mover:
    symbol: str
    name: str
    price: float
    pct: float
    vol: int
    avg_vol: int = 0
    cap: float = 0.0


@dataclass
class Coin:
    sym: str
    name: str
    price: float
    pct: float
    cap: float = 0.0
    vol: float = 0.0


@dataclass
class News:
    title: str
    guid: str
    src: str
    ts: float = 0.0


def _num(v, d=0.0):
    try:
        x = float(v)
        return x if x == x else d             # NaN -> default
    except (TypeError, ValueError):
        return d


def parse_chart(data, symbol=""):
    """Yahoo v8 chart (range=1d) -> Quote, or None."""
    try:
        meta = data["chart"]["result"][0]["meta"]
        price, prev = _num(meta.get("regularMarketPrice")), _num(meta.get("chartPreviousClose"))
        if price <= 0:
            return None
        sym = meta.get("symbol") or symbol
        return Quote(sym, str(meta.get("shortName") or meta.get("longName") or ""), price, prev,
                     int(_num(meta.get("regularMarketVolume"))), _num(meta.get("regularMarketDayLow")),
                     _num(meta.get("regularMarketDayHigh")), int(_num(meta.get("regularMarketTime"))),
                     str(meta.get("instrumentType") or ""))
    except (KeyError, IndexError, TypeError):
        return None


def parse_spark(data):
    """Yahoo v8 spark (many symbols, one request) -> {symbol: Quote}. Names / volume are not in it."""
    out = {}
    if not isinstance(data, dict):
        return out
    for sym, v in data.items():
        try:
            closes = [c for c in v.get("close") or [] if c is not None]
            prev = _num(v.get("chartPreviousClose"))
            if closes and prev > 0:
                stamps = v.get("timestamp") or [0]
                out[str(v.get("symbol") or sym)] = Quote(str(v.get("symbol") or sym), "", float(closes[-1]), prev,
                                                          mtime=int(_num(stamps[-1])))
        except (AttributeError, TypeError, IndexError):
            continue
    return out


def parse_screener(data, min_price=1.0):
    try:
        quotes = data["finance"]["result"][0]["quotes"]
    except (KeyError, IndexError, TypeError):
        return []
    out = []
    for q in quotes:
        if str(q.get("quoteType", "EQUITY")) != "EQUITY":
            continue
        price = _num(q.get("regularMarketPrice"))
        if price < min_price or not q.get("symbol"):
            continue
        out.append(Mover(str(q["symbol"]), str(q.get("shortName") or q.get("longName") or ""), price,
                         _num(q.get("regularMarketChangePercent")), int(_num(q.get("regularMarketVolume"))),
                         int(_num(q.get("averageDailyVolume3Month"))), _num(q.get("marketCap"))))
    return out


def parse_coins(data):
    out = []
    for c in data if isinstance(data, list) else []:
        try:
            if c.get("current_price") is None:
                continue
            out.append(Coin(str(c["symbol"]).upper(), str(c.get("name") or ""), float(c["current_price"]),
                            _num(c.get("price_change_percentage_24h")), _num(c.get("market_cap")), _num(c.get("total_volume"))))
        except (KeyError, TypeError, ValueError):
            continue
    return out


_TYPO = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                       "…": "...", " ": " "})


def clean_title(t):
    t = html.unescape(html.unescape(str(t or ""))).translate(_TYPO)
    return re.sub(r"\s+", " ", t).strip()


def parse_rss(text, src=""):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter("item"):
        title = clean_title(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link or title).strip()
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
        except (TypeError, ValueError):
            ts = 0.0
        if title:
            out.append(News(title, guid, src, ts))
    return out


def http_get(url, timeout=15):
    """Blocking GET -> (status, text); (0, "") on a network error. Module level so the tests can replace it."""
    try:
        r = session.get(url, timeout=timeout)
        return r.status_code, r.text
    except Exception as e:                                  # requests.RequestException and friends: never raise to poll()
        log.warning("Market data fetch failed (%s): %s", url.split("?")[0], e)
        return 0, ""


def _json(text):
    import json
    try:
        return json.loads(text)
    except ValueError:
        return None


class MarketData:
    """Blocking fetchers with a small time-based cache. Call them through asyncio.to_thread."""

    def __init__(self, clock=None):
        self.cache = {}
        self.clock = clock or time.time

    def _cached(self, key, ttl, fn):
        hit = self.cache.get(key)
        if hit and self.clock() - hit[0] < ttl:
            return hit[1]
        val = fn()
        if val:                                             # only successes are cached; a failure is retried next time
            self.cache[key] = (self.clock(), val)
            return val
        return hit[1] if hit and self.clock() - hit[0] < ttl * 10 else val   # stale beats nothing, for a while

    def quote(self, symbol, ttl=45):
        def fetch():
            st, body = http_get(f"{YAHOO}/v8/finance/chart/{urlquote(symbol, safe='')}?interval=1d&range=1d")
            return parse_chart(_json(body), symbol) if st == 200 else None
        return self._cached(("q", symbol), ttl, fetch)

    def quotes(self, symbols, ttl=45):
        out, misses, started = {}, 0, time.monotonic()
        for s in symbols:
            if misses >= 2 or time.monotonic() - started > 20:
                break                                       # the source is down or slow: do not spend minutes on it
            q = self.quote(s, ttl)
            if q:
                out[s] = q
                misses = 0
            else:
                misses += 1
        return out

    def quotes_fast(self, symbols, ttl=45):
        """One request for all symbols (spark); whatever it misses is fetched one by one."""
        syms = list(dict.fromkeys(symbols))

        def fetch():
            url = f"{YAHOO}/v8/finance/spark?symbols={','.join(urlquote(s, safe='') for s in syms)}&range=1d&interval=1d"
            st, body = http_get(url)
            return parse_spark(_json(body)) if st == 200 else {}
        got = self._cached(("spark", tuple(syms)), ttl, fetch) or {}
        out = {s: got[s] for s in syms if s in got}
        for s in syms:
            if s not in out:
                q = self.quote(s, ttl)
                if q:
                    out[s] = q
        return out

    def screener(self, scr_id, count=25, min_price=1.0, ttl=120):
        def fetch():
            url = f"{YAHOO}/v1/finance/screener/predefined/saved?scrIds={scr_id}&count={count}"
            st, body = http_get(url)
            return parse_screener(_json(body), min_price) if st == 200 else []
        return self._cached(("scr", scr_id, count), ttl, fetch) or []

    def coins(self, ttl=240):
        def fetch():
            url = f"{COINGECKO}?vs_currency=usd&order=market_cap_desc&per_page=100&price_change_percentage=24h"
            st, body = http_get(url)
            return parse_coins(_json(body)) if st == 200 else []
        return self._cached(("coins",), ttl, fetch) or []

    def news(self, feeds, ttl=600):
        def fetch():
            items = []
            for src, url in feeds:
                st, body = http_get(url)
                if st == 200:
                    items += parse_rss(body, src)
            items.sort(key=lambda n: n.ts, reverse=True)
            return items
        return self._cached(("news", tuple(u for _, u in feeds)), ttl, fetch) or []


# ==========================================================================
# TEXT - everything has to fit 130 bytes
# ==========================================================================
def fmt_price(x, symbol=""):
    ax = abs(x)
    if symbol.endswith("=X"):
        return f"{x:.4f}"
    if ax >= 1000:
        return f"{x:,.0f}"
    if ax >= 1:
        return f"{x:,.2f}"
    return f"{x:.4f}" if ax >= 0.01 else f"{x:.6f}"


def fmt_pct(p):
    return "0.0%" if round(p, 1) == 0 else f"{p:+.1f}%"


def fmt_vol(n):
    n = float(n or 0)
    return f"{n / 1e9:.1f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else f"{n:.0f}"


def short_name(sym):
    return NAMES.get(sym) or sym.lstrip("^").replace("-USD", "").replace("=F", "").replace("=X", "")


def qline(q, label=None):
    return f"{label or short_name(q.symbol)} {fmt_price(q.price, q.symbol)} {fmt_pct(q.pct)}"


def headline(n, room):
    """'title (SRC)' cut at a word boundary to `room` bytes."""
    tail = f" ({n.src})" if n.src else ""
    text = n.title + tail
    if len(text.encode("utf-8")) <= room:
        return text
    keep = n.title.encode("utf-8")[: max(room - len(tail.encode()) - 3, 10)].decode("utf-8", errors="ignore")
    keep = keep.rsplit(" ", 1)[0] if " " in keep else keep
    return keep.rstrip(" ,;:-") + "..." + tail


def _list(v, default):
    if v is None:
        return list(default)
    if isinstance(v, str):
        v = re.split(r"[\s,]+", v)
    return [str(x).strip() for x in v if str(x).strip()]


def _cfg(c, key, default, cast):
    """A numeric setting; a bad value is logged and replaced by the default rather than stopping the bot."""
    try:
        return cast(c.get(key, default))
    except (TypeError, ValueError):
        log.warning("[bots.*] %s = %r is not a valid %s; using %s", key, c.get(key), cast.__name__, default)
        return default


def resolve(word):
    """What the user typed -> a Yahoo symbol, or None if it cannot be one."""
    w = str(word).strip()
    if not w:
        return None
    if w.lower() in ALIASES:
        return ALIASES[w.lower()]
    return w.upper() if _SYMBOL.match(w) else None


# ==========================================================================
# THE BOT
# ==========================================================================
class MarketModule(LeaguePlugin):
    """Config (all optional but channel):  tag, indices, watch, commodities, crypto, news_feeds, open_brief, close_recap,
    crypto_daily, news, news_min_minutes, news_per_day, move_alert_pct, watch_alert_pct, crypto_alert_pct,
    max_alerts_per_day, min_price, movers_count, poll_seconds."""
    label = "Markets"
    kinds = MARKET_KINDS
    min_blurb = "the morning snapshot and the closing recap only"
    bare_command = "!indices"                 # "!market" on its own answers with the index snapshot, not the help text
    arg_cmds = ("!quote", "!q", "!crypto", "!fx", "!news")

    def __init__(self, hub, name, data=None):
        super().__init__(hub, name)
        c = self.cfg
        self.tag = str(c.get("tag") or "MKT").upper()
        self.blurb = "Market updates"
        self.data = data or MarketData(clock=self.ts)
        self.indices = [(s.split(":")[0], s.split(":")[1] if ":" in s else NAMES.get(s.split(":")[0]) or short_name(s.split(":")[0]))
                        for s in _list(c.get("indices"), [s for s, _ in INDICES])]
        self.watch = [s.upper() for s in _list(c.get("watch"), [])]
        self.commodities = [k.lower() for k in _list(c.get("commodities"), DEFAULT_COMMODITIES) if k.lower() in COMMODITIES]
        self.crypto = [s.upper() for s in _list(c.get("crypto"), DEFAULT_CRYPTO)]
        feeds = c.get("news_feeds")
        self.feeds = [("", u) for u in _list(feeds, [])] if feeds else list(DEFAULT_FEEDS)
        self.sched = {"brief": parse_hhmm(c.get("open_brief", "09:00")),
                      "recap": parse_hhmm(c.get("close_recap", "16:20")),
                      "crypto": parse_hhmm(c.get("crypto_daily", "08:00"))}
        self.window = _cfg(c, "window_minutes", 90, int)       # a scheduled post this late is skipped, not sent stale
        self.news_on = bool(c.get("news", True))
        self.news_gap = _cfg(c, "news_min_minutes", 120, float) * 60
        self.news_cap = _cfg(c, "news_per_day", 4, int)
        try:
            self.tiers = sorted(float(x) for x in _list(c.get("move_alert_pct"), [1, 2, 3, 5, 7]))
        except ValueError:
            log.warning("[bots.%s] move_alert_pct must be numbers like [1, 2, 3]; using the defaults", name)
            self.tiers = [1.0, 2.0, 3.0, 5.0, 7.0]
        self.watch_pct = _cfg(c, "watch_alert_pct", 5, float)
        self.coin_pct = _cfg(c, "crypto_alert_pct", 7, float)
        self.alert_cap = _cfg(c, "max_alerts_per_day", 10, int)
        self.min_price = _cfg(c, "min_price", 1.0, float)
        self.movers_n = max(1, min(_cfg(c, "movers_count", 3, int), 6))
        self.poll_s = max(30, _cfg(c, "poll_seconds", 180, int))
        self.primed = False
        self.state.data.setdefault("sent", {})
        self.state.data.setdefault("alerts", {})
        self.state.data.setdefault("news", {})
        self.state.data.setdefault("coin_alert", {})

    # ---------------- plumbing ----------------
    async def _run(self, fn, *args):
        """A blocking MarketData call off the event loop, with a time limit: a slow source must not hold up the hub's
        command worker (it serves every bot). Returns the call's empty value when it fails or times out."""
        empty = EMPTY.get(getattr(fn, "__name__", ""))
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), FETCH_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("market data call %s timed out after %ds", getattr(fn, "__name__", "?"), FETCH_TIMEOUT)
        except Exception:
            log.exception("market data call %s failed", getattr(fn, "__name__", "?"))
        return empty() if callable(empty) else empty

    def alert(self, kind, texts):
        """LeaguePlugin.alert, unless the channel is not on the radio (yet): then say so once instead of failing every send."""
        chans = getattr(self.hub.t, "chans", None)
        if chans and chan_key(self.channel) not in chans:
            if not getattr(self, "_warned", False):
                self._warned = True
                log.warning("Channel %s is not on the radio; market posts are not being sent (add it in the MeshCore app)",
                            self.channel)
            return
        super().alert(kind, texts)

    # ---------------- clock ----------------
    def ts(self):
        """Seconds since the epoch on the bot's clock (the real one, or the tests' fake one)."""
        return core.now().timestamp()

    def ny(self):
        n = core.now()
        return n.astimezone(NY) if NY else n

    def _slot(self, t, kind):
        at = self.sched[kind]
        return t.replace(hour=at[0], minute=at[1], second=0, microsecond=0) if at else None

    # ---------------- help / commands ----------------
    def help_text(self):
        return "!indices !quote SYM !gainers !losers !active !crypto !gold !oil !fx !news !watch !brief !about"

    def extra_commands(self):
        cmds = {"!indices": self.cmd_indices, "!index": self.cmd_indices, "!quote": self.cmd_quote, "!q": self.cmd_quote,
                "!price": self.cmd_quote, "!stock": self.cmd_quote, "!gainers": self.cmd_gainers, "!winners": self.cmd_gainers,
                "!losers": self.cmd_losers, "!active": self.cmd_active, "!volume": self.cmd_active, "!crypto": self.cmd_crypto,
                "!commodities": self.cmd_commodities, "!fx": self.cmd_fx, "!news": self.cmd_news, "!watch": self.cmd_watch,
                "!brief": self.cmd_brief, "!about": self.cmd_about}
        for word in ("gold", "silver", "oil", "brent", "natgas", "copper", "btc", "eth"):
            cmds["!" + word] = self._shortcut(word)
        return cmds

    def _shortcut(self, word):
        async def run(inc, arg):
            return await self._quote_reply(resolve(word))
        return run

    def _bad(self):
        return [f"[{self.tag}] Market data unavailable right now, try again soon"]

    async def cmd_about(self, inc, arg):
        return [fit(f"[{self.tag}] {DISCLAIMER}")]

    async def cmd_indices(self, inc, arg):
        qs = await self._run(self.data.quotes, [s for s, _ in self.indices])
        if not qs:
            return self._bad()
        self.mark_ok()
        st = market_state(self.ny())
        tail = {"open": "", "pre": " (pre-market)", "after": " (closed)", "weekend": " (weekend)", "holiday": " (holiday)"}[st]
        body = " | ".join(qline(qs[s], lab) for s, lab in self.indices if s in qs)
        return [fit(f"[{self.tag}] {body}{tail}")]

    async def _quote_reply(self, sym):
        if not sym:
            return [f"[{self.tag}] !quote SYMBOL (AAPL, ^GSPC, GC=F, BTC-USD, gold, oil, tsx)"]
        q = await self._run(self.data.quote, sym)
        if not q:
            return [f"[{self.tag}] No quote for {sym[:12]}"]
        self.mark_ok()
        label = short_name(q.symbol)                         # Gold, S&P, BTC, AAPL
        parts = [f"{label} {fmt_price(q.price, q.symbol)} {fmt_pct(q.pct)}"]
        if q.name and q.name.lower() != label.lower() and q.symbol not in NAMES:
            parts.append(q.name)                             # the company / coin name
        elif q.symbol != label:
            parts.append(q.symbol)                           # Gold -> GC=F, so people can reuse the symbol
        if q.vol and q.kind in ("EQUITY", "ETF", "CRYPTOCURRENCY", "MUTUALFUND"):
            parts.append(f"vol {fmt_vol(q.vol)}")
        if q.lo and q.hi:
            parts.append(f"day {fmt_price(q.lo, q.symbol)}-{fmt_price(q.hi, q.symbol)}")
        return [fit(f"[{self.tag}] " + " | ".join(parts))]

    async def cmd_quote(self, inc, arg):
        out = []
        for w in (inc.rest or [])[:2] or [""]:
            out += await self._quote_reply(resolve(w))
        return out

    async def _movers(self, scr, label):
        rows = await self._run(self.data.screener, scr, 25, self.min_price)
        if not rows:
            return self._bad()
        self.mark_ok()
        if scr == "most_actives":
            items = [f"{m.symbol} {fmt_vol(m.vol)} {fmt_pct(m.pct)}" for m in rows[:6]]
        else:
            items = [f"{m.symbol} {fmt_pct(m.pct)} {fmt_price(m.price)}" for m in rows[:6]]
        return pack(f"[{self.tag}] {label}:", items, sep=" | ", max_msgs=1)

    async def cmd_gainers(self, inc, arg):
        return await self._movers("day_gainers", "Gainers")

    async def cmd_losers(self, inc, arg):
        return await self._movers("day_losers", "Losers")

    async def cmd_active(self, inc, arg):
        return await self._movers("most_actives", "Most active")

    async def _coins(self, syms):
        coins = await self._run(self.data.coins)
        by = {}
        for c in coins:
            by.setdefault(c.sym, c)                        # the first (largest) coin that uses a ticker wins
        out, missing = [], []
        for s in syms:
            (out if s in by else missing).append(by[s] if s in by else s)
        if missing:                                        # outside the top 100, or CoinGecko is down: Yahoo has the majors
            qs = await self._run(self.data.quotes, [f"{s}-USD" for s in missing])
            for s in missing:
                q = qs.get(f"{s}-USD")
                if q:
                    out.append(Coin(s, q.name, q.price, q.pct))
        return out

    async def cmd_crypto(self, inc, arg):
        syms = [w.upper().replace("-USD", "") for w in (inc.rest or [])[:5] if _SYMBOL.match(w)] or self.crypto
        coins = await self._coins(syms)
        if not coins:
            return self._bad()
        self.mark_ok()
        return pack(f"[{self.tag}] Crypto:", [f"{c.sym} {fmt_price(c.price)} {fmt_pct(c.pct)}" for c in coins], max_msgs=1)

    async def cmd_commodities(self, inc, arg):
        syms = [COMMODITIES[k][0] for k in self.commodities]
        qs = await self._run(self.data.quotes, syms)
        if not qs:
            return self._bad()
        self.mark_ok()
        return pack(f"[{self.tag}]", [qline(qs[s]) for s in syms if s in qs], max_msgs=1)

    async def cmd_fx(self, inc, arg):
        w = (arg or "usdcad").lower().replace("/", "").replace("-", "")
        sym = ALIASES.get(w) or (f"{w.upper()}=X" if re.fullmatch(r"[a-z]{6}", w) else None)
        if not sym:
            return [f"[{self.tag}] !fx USDCAD | EURUSD | GBPUSD ..."]
        q = await self._run(self.data.quote, sym)
        if not q:
            return [f"[{self.tag}] No rate for {w.upper()[:8]}"]
        self.mark_ok()
        pair = sym[:3] + "/" + sym[3:6] if sym.endswith("=X") else sym
        return [f"[{self.tag}] {pair} {fmt_price(q.price, sym)} {fmt_pct(q.pct)}"]

    async def cmd_news(self, inc, arg):
        items = await self._run(self.data.news, self.feeds)
        if not items:
            return self._bad()
        self.mark_ok()
        n = 2 if (arg or "") not in ("1",) else 1
        return [f"📰 {headline(i, CFG['max_msg_bytes'] - 6)}" for i in items[:n]]

    async def cmd_watch(self, inc, arg):
        if not self.watch:
            return [f"[{self.tag}] No watchlist yet. Add watch = [\"AAPL\", \"MSFT\"] to this bot in meshscore.toml"]
        qs = await self._run(self.data.quotes, self.watch[:12])
        if not qs:
            return self._bad()
        self.mark_ok()
        return pack(f"[{self.tag}] Watch:", [qline(qs[s]) for s in self.watch if s in qs], max_msgs=2)

    async def cmd_brief(self, inc, arg):
        return await self.build_brief("📊 Snapshot:") or self._bad()

    # ---------------- what the scheduled posts say ----------------
    async def build_brief(self, label="☀️ Pre-open:"):
        fut = [s for s, _ in FUTURES]
        com = [COMMODITIES[k][0] for k in self.commodities[:3]]
        qs = await self._run(self.data.quotes, fut + com)
        coins = await self._coins(self.crypto[:2])
        if not qs and not coins:
            return []
        items = [qline(qs[s]) for s in fut + com if s in qs] + [f"{c.sym} {fmt_price(c.price)} {fmt_pct(c.pct)}" for c in coins]
        return pack(label, items, max_msgs=2)

    async def build_recap(self):
        """The closing recap, or [] when the data is not today's (holiday / feed down)."""
        idx = [s for s, _ in self.indices]
        com = [COMMODITIES[k][0] for k in self.commodities[:3]]
        qs = await self._run(self.data.quotes, idx + com)
        main = next((qs[s] for s in idx if s in qs), None)
        if not main:
            return []
        traded = datetime.fromtimestamp(main.mtime, NY or None).date() if main.mtime else None
        if traded != self.ny().date():                      # yesterday's close (holiday / early feed): not today's recap
            return []
        msgs = pack("🔔 Close:", [qline(qs[s], lab) for s, lab in self.indices if s in qs], max_msgs=1)
        up = await self._run(self.data.screener, "day_gainers", 25, self.min_price)
        dn = await self._run(self.data.screener, "day_losers", 25, self.min_price)
        act = await self._run(self.data.screener, "most_actives", 25, self.min_price)
        n = self.movers_n
        if up or dn:
            line = " ".join(f"{m.symbol} {fmt_pct(m.pct)}" for m in up[:n])
            line2 = " ".join(f"{m.symbol} {fmt_pct(m.pct)}" for m in dn[:n])
            msgs.append(fit(f"📈 {line} | 📉 {line2}"))
        extra = [f"{m.symbol} {fmt_vol(m.vol)}" for m in act[:n]]
        extra += [qline(qs[s]) for s in com if s in qs]
        if extra:
            msgs += pack("🔊 Active:" if act else "🛢", extra, max_msgs=1)
        return msgs

    async def build_crypto_daily(self):
        coins = await self._run(self.data.coins)
        if not coins:
            return []
        by = {}
        for c in coins:
            by.setdefault(c.sym, c)
        mine = [by[s] for s in self.crypto if s in by]
        msgs = pack("🪙 Crypto:", [f"{c.sym} {fmt_price(c.price)} {fmt_pct(c.pct)}" for c in mine], max_msgs=1)
        pool = [c for c in coins[:100] if c.sym.lower() not in STABLE_OR_WRAPPED and c.sym not in self.crypto]
        pool.sort(key=lambda c: c.pct, reverse=True)
        if len(pool) >= 4:
            hi, lo = pool[:2], pool[-2:][::-1]
            msgs.append(fit("🚀 Top 100: " + " ".join(f"{c.sym} {fmt_pct(c.pct)}" for c in hi) +
                            " | 📉 " + " ".join(f"{c.sym} {fmt_pct(c.pct)}" for c in lo)))
        return msgs

    # ---------------- polling ----------------
    def _due(self, kind, t, state):
        """True while a scheduled post is wanted: today's slot has arrived, it is not sent yet and the window is open."""
        slot = self._slot(t, kind)
        if not slot:
            return False
        if kind != "crypto" and state in ("weekend", "holiday"):
            return False
        if self.state.data["sent"].get(kind) == t.date().isoformat():
            return False
        return slot <= t < slot + timedelta(minutes=self.window)

    async def scheduled(self, t, state):
        builders = {"brief": self.build_brief, "recap": self.build_recap, "crypto": self.build_crypto_daily}
        today = t.date().isoformat()
        for kind, build in builders.items():
            if not self._due(kind, t, state):
                continue
            slot = self._slot(t, kind)
            if not self.primed and t - slot > timedelta(minutes=STARTUP_GRACE_MIN):
                self.state.data["sent"][kind] = today       # (re)started well after the slot: it is yesterday's news
                self.state.save()
                continue
            try:
                msgs = await build()
            except Exception:
                log.exception("market %s failed", kind)
                continue
            if msgs:
                self.alert(kind, msgs)
                self.mark_ok()
            elif kind == "recap" and t - slot >= timedelta(minutes=30):
                log.info("market recap: no fresh close data 30 minutes after the slot, skipping today")
            else:
                continue                                    # the data is not ready: look again next poll, inside the window
            self.state.data["sent"][kind] = today
            self.state.save()

    def _alert_day(self, t):
        a = self.state.data["alerts"]
        day = t.date().isoformat()
        if a.get("day") != day:
            a.clear()
            a.update({"day": day, "idx": {}, "stk": {}, "n": 0, "seeded": False})
        return a

    def _capped(self, a):
        if a["n"] >= self.alert_cap:
            log.info("market alert cap reached (%d today)", self.alert_cap)
            return True
        return False

    async def watch_market(self, t):
        """Index tier crossings and watchlist moves, while the US market is open."""
        a = self._alert_day(t)
        syms = [s for s, _ in self.indices] + self.watch
        qs = await self._run(self.data.quotes_fast, syms)
        if not qs:
            return
        self.mark_ok()
        seed = not a["seeded"] and not self.primed        # first look after a (re)start: note the state, say nothing
        crossed = []
        for s, lab in self.indices:
            q = qs.get(s)
            if not q:
                continue
            lvl = max((x for x in self.tiers if abs(q.pct) >= x), default=0)
            key = f"{s}{'+' if q.pct >= 0 else '-'}"
            if lvl > a["idx"].get(key, 0):
                a["idx"][key] = lvl
                if not seed:
                    crossed.append((q, lab))
        if crossed and not self._capped(a):
            worst = max((q for q, _ in crossed), key=lambda q: abs(q.pct))
            body = " | ".join(qline(qs[s], lab) for s, lab in self.indices if s in qs)
            self.alert("move", fit(f"{'📈' if worst.pct > 0 else '📉'} {body}"))
            a["n"] += 1
        for s in self.watch:
            q = qs.get(s)
            if not q or abs(q.pct) < self.watch_pct:
                continue
            key = f"{s}{'+' if q.pct >= 0 else '-'}"
            if a["stk"].get(key):
                continue
            a["stk"][key] = True
            if not seed and not self._capped(a):
                vol = f" | vol {fmt_vol(q.vol)}" if q.vol else ""
                self.alert("stock", fit(f"{'📈' if q.pct > 0 else '📉'} {s} {fmt_pct(q.pct)} {fmt_price(q.price, s)}{vol}"))
                a["n"] += 1
        a["seeded"] = True
        self.state.save()

    async def watch_crypto(self, t):
        coins = await self._run(self.data.coins)
        if not coins:
            return
        self.mark_ok()
        a = self._alert_day(t)
        done = self.state.data["coin_alert"]
        stamp = self.ts()
        for c in coins[:20]:
            if c.sym not in self.crypto or abs(c.pct) < self.coin_pct:
                continue
            if stamp - done.get(c.sym, 0) < 12 * 3600:
                continue
            done[c.sym] = stamp
            if self.primed and not self._capped(a):
                self.alert("coin", fit(f"🪙 {c.sym} {fmt_pct(c.pct)} in 24h | {fmt_price(c.price)}"))
                a["n"] += 1
        self.state.save()

    async def watch_news(self, t):
        if not self.news_on or not (7 <= t.hour < 21):
            return
        items = await self._run(self.data.news, self.feeds)
        if not items:
            return
        self.mark_ok()
        st = self.state.data["news"]
        seen = st.setdefault("seen", [])
        day = t.date().isoformat()
        if st.get("day") != day:
            st["day"], st["n"] = day, 0
        fresh = [n for n in items if n.guid not in seen]
        if not st.get("primed"):                            # first ever look: everything on the page is old news
            st["primed"] = True
            seen[:] = [n.guid for n in items][-300:]
            self.state.save()
            return
        if not fresh or self.ts() - st.get("last", 0) < self.news_gap or st["n"] >= self.news_cap:
            return
        # the newest story that is actually recent; everything on the page up to it counts as seen once one goes out
        pick = next((n for n in fresh if not n.ts or self.ts() - n.ts < NEWS_MAX_AGE), None)
        if pick:
            self.alert("news", f"📰 {headline(pick, CFG['max_msg_bytes'] - 6)}")
            st["last"], st["n"] = self.ts(), st["n"] + 1
            seen.extend(n.guid for n in fresh)
            del seen[:-300]
            self.state.save()

    def next_delay(self, t, state):
        base = self.poll_s if state == "open" else 600
        soonest = None
        for kind in self.sched:
            slot = self._slot(t, kind)
            if not slot or self.state.data["sent"].get(kind) == t.date().isoformat():
                continue
            if kind != "crypto" and state in ("weekend", "holiday"):
                continue
            gap = (slot - t).total_seconds()
            if gap < 0 and t < slot + timedelta(minutes=self.window):
                gap = 60                                    # due and the data was not ready: look again in a minute
            if gap >= 0 and (soonest is None or gap < soonest):
                soonest = gap
        return max(15, min(base, soonest + 3 if soonest is not None else base))

    async def poll(self):
        t = self.ny()
        state = market_state(t)
        steps = [self.scheduled(t, state)]
        if state == "open":
            steps.append(self.watch_market(t))
        steps += [self.watch_crypto(t), self.watch_news(t)]
        for step in steps:
            try:
                await step
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("market poll step failed")
        self.primed = True
        return self.next_delay(t, state)

    async def self_test(self):
        """`meshscore check`: one request to each source."""
        q = await self._run(self.data.quote, "^GSPC", 0)
        scr = await self._run(self.data.screener, "day_gainers", 5, self.min_price, 0)
        coins = await self._run(self.data.coins, 0)
        news = await self._run(self.data.news, self.feeds, 0)
        got = [("quotes", bool(q)), ("movers", bool(scr)), ("crypto", bool(coins)), ("news", bool(news))]
        if not any(ok for _, ok in got):
            raise RuntimeError("no market data source reachable")
        return ", ".join(f"{n} {'ok' if ok else 'UNAVAILABLE'}" for n, ok in got)


LEAGUES = {"market": MarketModule}
