"""Re-record tests/fixtures/market/ from the live sources (run after a feed changes shape):

    python tests/record_market_fixtures.py

Responses are trimmed to what the market bot reads so the files stay small. Commit the result."""
import json
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from meshscore.core import session  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "market")
Y = "https://query1.finance.yahoo.com"


def get(url):
    r = session.get(url, timeout=20)
    r.raise_for_status()
    return r.text


def save(name, text):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("recorded", name, len(text), "bytes")


def chart(sym, name):
    d = json.loads(get(f"{Y}/v8/finance/chart/{sym}?interval=1d&range=1d"))
    res = d["chart"]["result"][0]
    res["meta"].pop("currentTradingPeriod", None)
    res.pop("indicators", None)
    save(name, json.dumps(d))


def main():
    chart("%5EGSPC", "chart_gspc.json")
    chart("AAPL", "chart_aapl.json")
    chart("GC%3DF", "chart_gold.json")
    save("spark.json", get(f"{Y}/v8/finance/spark?symbols=AAPL,%5EGSPC,%5EIXIC,%5EDJI,%5EGSPTSE&range=1d&interval=1d"))
    for scr in ("day_gainers", "day_losers", "most_actives"):
        d = json.loads(get(f"{Y}/v1/finance/screener/predefined/saved?scrIds={scr}&count=10"))
        res = d["finance"]["result"][0]
        keep = ("symbol", "shortName", "quoteType", "regularMarketPrice", "regularMarketChangePercent",
                "regularMarketVolume", "averageDailyVolume3Month", "marketCap")
        res["quotes"] = [{k: q.get(k) for k in keep} for q in res["quotes"][:10]]
        save(f"screener_{scr}.json", json.dumps({"finance": {"result": [{"quotes": res["quotes"]}], "error": None}}))
    coins = json.loads(get("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc"
                           "&per_page=100&price_change_percentage=24h"))
    keep = ("id", "symbol", "name", "current_price", "market_cap", "total_volume", "price_change_percentage_24h")
    save("coingecko.json", json.dumps([{k: c.get(k) for k in keep} for c in coins[:40]]))
    for fname, url in (("cnbc.xml", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069"),
                       ("marketwatch.xml", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse")):
        root = ET.fromstring(get(url))
        for channel in root.iter("channel"):
            for extra in channel.findall("item")[5:]:        # keep five stories, whatever else the feed wraps around them
                channel.remove(extra)
        save(fname, ET.tostring(root, encoding="unicode"))


if __name__ == "__main__":
    main()
