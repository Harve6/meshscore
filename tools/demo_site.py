"""Builds demo copies of MeshScore's web boards with MADE-UP players, for screenshots and for trying the pages offline.

    python tools/demo_site.py [out_dir]          # default: demo-site/   (git-ignored)
    cd demo-site/daily && python -m http.server 8000      # then open http://localhost:8000

Nothing here talks to a radio, the internet or a game ROM: it drives the real bot code with a fake transport and a fake
clock, then writes the same `state.json` / `word.json` / ... files the bots upload. The Pokemon page uses a plain
placeholder picture instead of game art.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import random
import shutil
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from meshscore import config, core                       # noqa: E402
from meshscore.cli import build_hub                       # noqa: E402
from meshscore.core import CFG                            # noqa: E402
from meshscore.radio import Incoming, Transport           # noqa: E402

ZONE = ZoneInfo("America/Toronto")
CHANNEL_URI = "meshcore://channel/add?name=%23daily&secret=00112233445566778899aabbccddeeff"
KEY = "ab" * 32
CONTACT = {"name": "Sportsbot", "uri": "meshcore://11" + KEY, "link": f"meshcore://contact/add?name=Sportsbot&public_key={KEY}&type=1",
           "public_key": KEY}
PEOPLE = ["Alex", "Sam", "Jordan", "Riley", "Casey", "Morgan", "Taylor", "Jamie", "Quinn"]


class Fake(Transport):
    name, alive = "demo", True

    def __init__(self):
        self.dms = []

    async def send_channel(self, channel, text):
        return True

    async def send_dm(self, key, text):
        self.dms.append(text)
        return True


def write(out, name, data):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def copy_page(src_dir, out):
    os.makedirs(out, exist_ok=True)
    shutil.copyfile(os.path.join(src_dir, "index.html"), os.path.join(out, "index.html"))


def cards(m, channel, links=None):
    m.state.data["channel_card"] = {"name": channel, "uri": CHANNEL_URI.replace("%23daily", "%23" + channel.lstrip("#"))}
    m.state.data["contact"] = dict(CONTACT)
    if links is not None:
        m._board_links = lambda: links
    m.hub.heard = {channel.lstrip("#"): {n: time.time() - i * 600 for i, n in enumerate(PEOPLE[:4])}}


def demo_hub(modules):
    config.reset()
    CFG.update(send_gap=0, command_cooldown=0, log_file="", timezone="America/Toronto")
    core.init_tz()
    CFG["modules"] = {k: {"enabled": True, "channel": "#daily", **v} for k, v in modules.items()}
    return build_hub(Fake())


async def say(hub, who, text, dm=True, hops=1, snr=8.0):
    if not getattr(hub, "_demo_workers", None):                 # the command / send loops the real bot runs
        hub._demo_workers = [asyncio.create_task(hub.cmd_worker()), asyncio.create_task(hub.tx_worker())]
    await hub.on_message(Incoming(text, dm, who, ("k" + who.lower() * 8)[:32] if dm else "", hops, snr, "" if dm else "daily"))
    for _ in range(100):
        await asyncio.sleep(0.005)
        if hub.cmdq.empty() and hub.out.empty():
            await asyncio.sleep(0.01)
            break


# ---------------------------------------------------------------------------------------------- the daily page
async def daily(out):
    sitedir = os.path.join(ROOT, "src", "meshscore", "dailyplay", "site")
    hub = demo_hub({"word": {"type": "word"}, "trivia": {"type": "trivia"}, "pickem": {"type": "pickem"},
                    "coverage": {"type": "coverage"}, "weather": {"type": "weather", "lat": 43.45, "lon": -80.49, "place": "Kitchener"}})
    core.CLOCK = lambda: datetime(2026, 10, 14, 15, 30, tzinfo=ZONE)
    now = time.time()
    links = [{"title": "Mesh Chess", "url": "#"}, {"title": "Mesh Plays Pokemon", "url": "#"}]
    for m in hub.modules.values():
        m.clock = lambda: now
        cards(m, "#daily", links)
    # --- word
    w = hub.modules["word"]
    w.rng = random.Random(4)
    await w.poll()
    ans = w.state.data["puzzle"]["answer"]
    other = "crane" if ans != "crane" else "slate"
    await say(hub, "Alex", f"!guess {other}")
    await say(hub, "Alex", f"!guess {ans}")
    for g in (other, "pilot", "mound", ans):
        await say(hub, "Sam", f"!guess {g}")
    for g in ("irate", "pound", "light", "stove", "brick", "flame"):
        await say(hub, "Jordan", f"!guess {g if g != ans else 'zzzzz'}")
    await say(hub, "Riley", f"!guess {other}")
    w.state.data["history"] = [{"no": 6 - i, "date": f"2026-10-{13 - i}", "answer": a, "solved": {f"p{k}": 3 for k in range(s)}, "played": p}
                               for i, (a, s, p) in enumerate([("brave", 4, 6), ("lemon", 5, 6), ("charm", 3, 5), ("pulse", 4, 4)])]
    w.state.data["stats"] = {"Alex": {"played": 9, "won": 8, "streak": 6, "best": 6, "last_won": "2026-10-14"},
                             "Sam": {"played": 8, "won": 6, "streak": 3, "best": 4, "last_won": "2026-10-14"},
                             "Jordan": {"played": 7, "won": 3, "streak": 0, "best": 2, "last_won": ""}}
    write(os.path.join(out, "daily"), "word.json", w.site_state())
    # --- trivia
    t = hub.modules["trivia"]
    t.rng = random.Random(7)
    await t.poll()
    cur = t.state.data["current"]
    for who, right in (("Alex", True), ("Sam", True), ("Jordan", False), ("Riley", True)):
        await say(hub, who, f"!trivia {'ABCD'[cur['answer'] if right else (cur['answer'] + 1) % 4]}")
    t.state.data["history"] = [{"no": 11, "date": "2026-10-13", "q": "Which team plays in Edmonton?", "answer": "Oilers", "right": 3, "played": 4}]
    t.state.data["stats"] = {"Alex": {"played": 10, "right": 9, "streak": 5, "best": 7, "last": "2026-10-14"},
                             "Riley": {"played": 6, "right": 4, "streak": 2, "best": 3, "last": "2026-10-14"}}
    write(os.path.join(out, "daily"), "trivia.json", t.site_state())
    # --- pick'em
    from meshscore.dailyplay.espn import Game
    p = hub.modules["pickem"]
    base = now + 3 * 3600
    games = [Game("1", "nhl", base, "pre", "TOR", "BUF"), Game("2", "nhl", base + 1800, "pre", "BOS", "MTL"),
             Game("3", "nhl", now - 600, "in", "EDM", "VAN", 2, 1), Game("4", "nhl", now - 9000, "post", "COL", "DAL", 4, 2, "COL")]
    slate = {"date": "2026-10-14", "games": {g.id: {**g.to_dict(), "scored": g.state == "post"} for g in games},
             "picks": {"Alex": {"1": "TOR", "2": "BOS", "4": "COL"}, "Sam": {"1": "BUF", "4": "DAL"}, "Jordan": {"1": "TOR", "3": "VAN"}},
             "posted": True, "recapped": False}
    p.state.data["slates"] = [slate]
    p.state.data["stats"] = {"Alex": {"season": 41, "correct": 41, "picks": 60, "week": 5, "week_id": "2026-W42"},
                             "Sam": {"season": 37, "correct": 37, "picks": 61, "week": 4, "week_id": "2026-W42"},
                             "Jordan": {"season": 29, "correct": 29, "picks": 50, "week": 2, "week_id": "2026-W42"}}
    write(os.path.join(out, "daily"), "pickem.json", p.site_state())
    # --- coverage
    c = hub.modules["coverage"]
    rng = random.Random(11)
    for day in range(13, -1, -1):
        stamp = now - day * 86400 - rng.randint(0, 40000)
        c.clock = lambda s=stamp: s
        for _ in range(rng.randint(3, 22)):
            who = rng.choice(PEOPLE)
            hops = rng.choices([0, 1, 2, 3, 4], [30, 30, 25, 10, 5])[0]
            c.on_heard(Incoming("hi", False, who, "", hops, round(rng.gauss(10 - hops * 2.5, 3), 1), "public"))
    c.clock = lambda: now
    write(os.path.join(out, "daily"), "coverage.json", c.site_state())
    # --- weather
    wx = hub.modules["weather"]
    wx.wx = {"current": {"temperature_2m": 11.4, "wind_speed_10m": 19.0, "wind_direction_10m": 250, "weather_code": 3},
             "daily": {"time": ["2026-10-14", "2026-10-15", "2026-10-16"], "temperature_2m_max": [13.2, 10.1, 9.0],
                       "temperature_2m_min": [5.1, 3.4, 2.2], "precipitation_probability_max": [20, 70, 40], "weather_code": [3, 63, 80]}}
    wx.active = [{"id": "d1", "kind": "watch", "name": "frost advisory", "text": "Temperatures near freezing are expected overnight. Protect tender plants.",
                  "expires": "2026-10-15T12:00:00Z"}]
    write(os.path.join(out, "daily"), "weather.json", wx.site_state())
    copy_page(sitedir, os.path.join(out, "daily"))


# ---------------------------------------------------------------------------------------------- chess
async def chess_page(out):
    import chess
    from meshscore.chessplay import puzzle as pz
    hub = demo_hub({"chess": {"type": "chess", "channel": "#chess", "puzzle": False, "site_url": "#"}})
    m = hub.modules["chess"]
    now = time.time()
    m.clock = lambda: now
    cards(m, "#chess", [{"title": "Mesh Plays Pokemon", "url": "#"}])
    m.state.data["level"] = 2
    g = m.game()
    for u in ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "c2c3", "g8f6", "d2d4", "e5d4", "c3d4", "c5b4", "b1c3", "f6e4"]:
        g.data["moves"].append(u)
    g.data["votes"] = {"Alex": ["c3e4", 1], "Sam": ["c3e4", 2], "Jordan": ["e1g1", 3], "Riley": ["c4f7", 4]}
    g.data["seq"] = 4
    g.data["window_end"] = now + 38
    m.state.data["record"] = {"crowd": 2, "bot": 1, "draw": 1}
    m.state.data["players"] = {"Alex": {"moves": 14, "votes": 20, "puzzles": 3}, "Sam": {"moves": 9, "votes": 15, "puzzles": 1},
                               "Jordan": {"moves": 4, "votes": 11, "puzzles": 0}}
    m.state.data["history"] = [{"id": 3, "level": 1, "result": "crowd", "reason": "checkmate", "moves": "1.e4 e5"},
                               {"id": 2, "level": 1, "result": "bot", "reason": "checkmate", "moves": "1.d4 d5"}]
    fx = os.path.join(ROOT, "tests", "fixtures", "lichess_daily.json")
    if os.path.isfile(fx):
        puz = pz.from_lichess(json.load(open(fx, encoding="utf-8")), "2026-10-14")
        puz["solved"] = ["Alex", "Sam"]
        m.state.data["puzzle"] = puz
    _ = chess
    write(os.path.join(out, "chess"), "state.json", m.site_state())
    copy_page(os.path.join(ROOT, "src", "meshscore", "chessplay", "site"), os.path.join(out, "chess"))


# ---------------------------------------------------------------------------------------------- pokemon (no game art)
def placeholder_png() -> bytes:
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (160, 144), (212, 222, 170))
    d = ImageDraw.Draw(im)
    for y in range(0, 144, 8):
        d.line([(0, y), (160, y)], fill=(200, 212, 156))
    d.rectangle([6, 6, 153, 137], outline=(60, 70, 50), width=2)
    d.text((38, 52), "DEMO SCREEN", fill=(40, 50, 35))
    d.text((24, 70), "your game appears here", fill=(40, 50, 35))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


async def pokemon_page(out):
    from meshscore.pokeplay import ram
    hub = demo_hub({"pokemon": {"type": "pokemon", "channel": "#pokemon", "emulator": "fake", "site_url": "#"}})
    m = hub.modules["pokemon"]
    now = time.time()
    m.clock = lambda: now
    cards(m, "#pokemon", [{"title": "Mesh Chess", "url": "#"}])
    m.status = ram.Status(started=True, player="RED", map_id=12, map_name="Route 1", x=10, y=24, badges=1, money=1840, owned=7, seen=19,
                          party=[ram.Mon("SPARKY", "PIKACHU", 14, 31, 38), ram.Mon("ROCKY", "GEODUDE", 9, 5, 28),
                                 ram.Mon("PIDGE", "PIDGEY", 7, 22, 22)])
    m.png = placeholder_png()
    m.rnd["votes"] = {"Alex": ["up", 5, 1], "Sam": ["up", 5, 2], "Jordan": ["a", 0, 3]}
    m.rnd["window_end"] = now + 21
    m.state.data["rounds"] = 214
    m.state.data["recent"] = [{"n": 214 - i, "action": a, "votes": 2, "voters": 3, "who": ["Alex", "Sam"]} for i, a in enumerate(["upx5", "a", "leftx2", "a", "b"])]
    m.state.data["events"] = [{"t": int(now) - 60 * i, "text": t} for i, t in enumerate(["📍 Route 1", "⬆ SPARKY is now L14!", "⚔ A wild Pokemon appeared!"])]
    m.state.data["players"] = {"Alex": {"presses": 88, "votes": 140}, "Sam": {"presses": 61, "votes": 99}, "Jordan": {"presses": 30, "votes": 70}}
    state = m.site_state()
    write(os.path.join(out, "pokemon"), "state.json", state)
    with open(os.path.join(out, "pokemon", "screen.png"), "wb") as f:
        f.write(m.png)
    copy_page(os.path.join(ROOT, "src", "meshscore", "pokeplay", "site"), os.path.join(out, "pokemon"))


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "demo-site")
    old = os.getcwd()
    tmp = tempfile.mkdtemp()
    os.chdir(tmp)                                       # the bots keep their state files in the working directory
    try:
        async def go():
            await daily(os.path.abspath(out) if os.path.isabs(out) else os.path.join(old, out))
            await chess_page(os.path.abspath(out) if os.path.isabs(out) else os.path.join(old, out))
            await pokemon_page(os.path.abspath(out) if os.path.isabs(out) else os.path.join(old, out))
        asyncio.run(go())
    finally:
        os.chdir(old)
        core.CLOCK = None
        shutil.rmtree(tmp, ignore_errors=True)
    print("Demo boards written to", out, "(daily/, chess/, pokemon/). Serve one with: python -m http.server --directory", os.path.join(out, "daily"))


if __name__ == "__main__":
    main()
