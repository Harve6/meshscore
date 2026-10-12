"""Whole-game replays of REAL finished games (tests/fixtures/games.json.gz, recorded by tests/record_games.py).

For each game we rebuild what a poller would have seen - the score, period/inning and clock after every goal / run,
every intermission, and the final - and feed it to the bot the way the poll loop does. Then we check *invariants* that
must hold for any game, however the polls happen to fall:

* nothing is announced twice, nothing is announced out of order, scores never go backwards;
* every goal / run is accounted for (a "2 goals" message counts as two), the final comes exactly once, last, with the
  true score and the right overtime / extra-innings tag, and carries the winner / three stars from the real data;
* every message fits in 130 bytes;
* the answer is the same however coarse the polling is (we skip snapshots with strides 1..5 at every offset);
* **a restart at any point of the game announces nothing that already happened** (baseline at snapshot k, then the
  rest of the game still goes out correctly).

No network, no radio; deterministic.
"""
import asyncio
import dataclasses
import gzip
import json
import os
import re

import pytest

from meshscore import config
from meshscore.core import CFG
from meshscore.hub import Hub
from meshscore.leagues.mlb import MlbModule, parse_mgame
from meshscore.leagues.nhl import NGame, NhlModule
from meshscore.radio import Transport

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "games.json.gz")
with gzip.open(FIXTURE, "rt", encoding="utf-8") as _f:
    GAMES = json.load(_f)

STRIDES = [(1, 0), (2, 0), (2, 1), (3, 0), (3, 1), (3, 2), (5, 0), (5, 3)]       # (take every k-th snapshot, offset)
MAX_BYTES = 130


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, log_file="", timezone="America/Toronto")
    CFG["modules"] = {
        "leafs": {"type": "nhl", "team": "TOR", "channel": "#leafs"},
        "jays": {"type": "mlb", "team": "TOR", "tag": "JAYS", "channel": "#jays"},
        "ohl": {"type": "ohl", "channel": "#ohl"},
    }
    yield
    config.reset()


def drain(mod):
    out = []
    while not mod.hub.out.empty():
        out.append(mod.hub.out.get_nowait()[2])
    return out


def repeats(snaps):
    """A real poller sees the same state again and again between changes - and the final one until the game drops off
    the schedule. Each snapshot is delivered twice, the final three times: nothing may be announced twice."""
    out = []
    for i, s in enumerate(snaps):
        out += [s] * (3 if i == len(snaps) - 1 else 2)
    return out


def choose(snaps, stride, offset, start_at=0):
    """The snapshots a poller with this rhythm sees: always the first it starts with and the last one (the final),
    plus every `stride`-th in between."""
    pool = snaps[start_at:]
    keep = [pool[0]] + [s for i, s in enumerate(pool[1:-1], 1) if (i + offset) % stride == 0] + [pool[-1]]
    return keep


# ============================================================ NHL
def nhl_snapshots(landing):
    away, home = landing["awayTeam"]["abbrev"], landing["homeTeam"]["abbrev"]
    gid, gtype, date = landing["id"], landing["gameType"], landing["gameDate"]

    def snap(state, period, ptype, clock, ar, hr, goals, inter=False, last=""):
        return NGame(id=gid, date=date, start=None, state=state, gtype=gtype, away=away, home=home, ar=ar, hr=hr,
                     period=period, ptype=ptype, clock=clock, inter=inter, last_ptype=last, goals=list(goals), series="")

    snaps = [snap("preview", 0, "REG", "20:00", 0, 0, [])]
    goals, ar, hr = [], 0, 0
    for p in landing["summary"]["scoring"]:
        period, ptype = p["periodDescriptor"]["number"], p["periodDescriptor"]["periodType"]
        length = 20 * 60 if (ptype == "REG" or gtype == 3) else 5 * 60
        snaps.append(snap("live", period, ptype, f"{length // 60:02d}:00", ar, hr, goals))
        for g in p["goals"]:
            goals.append(g)
            ar, hr = g["awayScore"], g["homeScore"]
            m, s = map(int, g["timeInPeriod"].split(":"))
            left = max(length - (m * 60 + s), 0)
            snaps.append(snap("live", period, ptype, f"{left // 60:02d}:{left % 60:02d}", ar, hr, goals))
        if period in (1, 2):
            snaps.append(snap("live", period, ptype, "00:00", ar, hr, goals, inter=True))
    end = landing["periodDescriptor"]
    if end["periodType"] == "SO":                               # the shootout winner's goal only shows in the final score
        snaps.append(snap("live", 5, "SO", "00:00", ar, hr, goals))
    snaps.append(snap("final", end["number"], end["periodType"], "00:00", landing["awayTeam"]["score"],
                      landing["homeTeam"]["score"], goals, last=end["periodType"]))
    return snaps


async def nhl_replay(landing, snaps):
    mod = NhlModule(Hub(Transport()), "leafs")
    mod.state.data["mode"] = "max"

    async def land(gid):
        return landing
    mod.landing = land
    got = []
    for g in snaps:
        await mod.process([g])
        got += drain(mod)
    # late three-stars job may still be pending only when the feed lacked them; the real landing has them at once
    return got


def parse_score(text):
    m = re.search(r"\b([A-Z]{2,4}) (\d+)-(\d+) ([A-Z]{2,4})\b", text)
    return (int(m.group(2)), int(m.group(3))) if m else None


NHL_IDS = sorted(GAMES["nhl"])


def expected_goal_alerts(snaps):
    """The total goals a correct bot must have accounted for by the end of these snapshots. The shootout is the one
    exception: the winner's +1 shows only in the final score (it is not a goal alert), and goals between the last
    regulation/OT poll and a shootout final are folded into the final line."""
    total = lambda g: g.ar + g.hr          # noqa: E731
    counted = [g for g in snaps if g.ptype != "SO"]
    return total(counted[-1]) if counted else total(snaps[0])


def check_nhl(got, landing, full_game, expect_claimed=None, start=(0, 0)):
    away, home = landing["awayTeam"], landing["homeTeam"]
    finals = [i for i, t in enumerate(got) if "FINAL" in t]
    assert len(finals) == 1, got
    fin = got[finals[0]]
    assert f"{away['abbrev']} {away['score']} @ {home['abbrev']} {home['score']}" in fin, fin
    end = landing["periodDescriptor"]["periodType"]
    assert (f"FINAL/{end}" in fin) if end != "REG" else ("FINAL:" in fin), fin
    assert all(len(t.encode("utf-8")) <= MAX_BYTES for t in got), [t for t in got if len(t.encode()) > MAX_BYTES]
    after = got[finals[0] + 1:]
    assert all(t.startswith("⭐") for t in after), after                 # only the stars may follow the final
    stars = landing["summary"].get("threeStars") or []
    if len(stars) == 3:
        assert len(after) == 1 and all(s["name"]["default"] in after[0] for s in stars), (after, stars)
    # goals: scores only move forward, every increment is announced, none twice
    last = start
    claimed = 0
    for t in got[: finals[0]]:
        if not t.startswith("🚨"):
            continue
        sc = parse_score(t)
        assert sc and sc[0] >= last[0] and sc[1] >= last[1] and sc != last, (t, last)
        n = int(re.match(r"🚨 (\d+) goals", t).group(1)) if re.match(r"🚨 \d+ goals", t) else 1
        assert (sc[0] - last[0]) + (sc[1] - last[1]) == n, (t, last)
        claimed += n
        last = sc
    if expect_claimed is not None:
        assert claimed == expect_claimed, (claimed, expect_claimed, got)
    if full_game:
        assert any(t.startswith("🏒 Puck drop") for t in got), got
        assert got[0].startswith("🏒 Puck drop"), got[0]
    assert len([t for t in got if t.startswith("🏒")]) <= 1


@pytest.mark.parametrize("stride,offset", STRIDES)
@pytest.mark.parametrize("gid", NHL_IDS)
def test_nhl_whole_game(gid, stride, offset):
    landing = GAMES["nhl"][gid]["landing"]
    snaps = choose(nhl_snapshots(landing), stride, offset)
    got = asyncio.run(nhl_replay(landing, repeats(snaps)))
    check_nhl(got, landing, full_game=True, expect_claimed=expected_goal_alerts(snaps))
    if stride == 1:                                     # polled at every change: each goal names its real scorer
        singles = [t for t in got if t.startswith("🚨") and "goals |" not in t]
        names = [f"{g['firstName']['default'][:1]}. {g['lastName']['default']}"
                 for p in landing["summary"]["scoring"] if p["periodDescriptor"]["periodType"] != "SO" for g in p["goals"]]
        assert len(singles) == len(names)
        for t, n in zip(singles, names):
            assert n in t, (t, n)
        ints = [t for t in got if t.startswith("⏸")]
        assert [re.match(r"⏸ End of (\w+):", t).group(1) for t in ints] == ["1st", "2nd"], ints
        assert all("SOG" in t for t in ints)


@pytest.mark.parametrize("gid", NHL_IDS)
def test_nhl_restart_at_every_point_announces_nothing_old(gid):
    """Start the bot (baseline) at snapshot k for every k: the message count must equal the changes after k, never
    replay what had already happened, and the game must still end with exactly one correct final."""
    landing = GAMES["nhl"][gid]["landing"]
    full = nhl_snapshots(landing)
    for k in range(len(full) - 1):
        got = asyncio.run(nhl_replay(landing, repeats(full[k:])))
        base = full[k]
        check_nhl(got, landing, full_game=False, expect_claimed=expected_goal_alerts(full[k:]) - (base.ar + base.hr),
                  start=(base.ar, base.hr))
        singles = [t for t in got if t.startswith("🚨")]
        steps = [(a, b) for a, b in zip(full[k:], full[k + 1:]) if b.ar + b.hr > a.ar + a.hr and b.state != "final" and b.ptype != "SO"]
        assert len(singles) == len(steps), (k, got)
        assert not any(t.startswith(("🏒", "⏸ End of")) for t in got if base.state == "live" and base.period >= 3), (k, got)


# ============================================================ MLB
def mlb_snapshots(game, feed):
    final = parse_mgame(game)
    plays = feed["liveData"]["plays"]["allPlays"]
    snaps = [(dataclasses.replace(final, state="preview", ar=0, hr=0, inning=0, inn_txt="", win="", loss="", save=""), -1)]
    ar = hr = 0
    cur = None
    for i, p in enumerate(plays):
        about, res = p["about"], p["result"]
        half = "Top" if about["halfInning"] == "top" else "Bot"
        ordn = {1: "1st", 2: "2nd", 3: "3rd"}.get(about["inning"], f"{about['inning']}th")
        key = (about["inning"], half)
        base = dict(state="live", inning=about["inning"], inn_txt=f"{half} {ordn}", win="", loss="", save="", delayed=False)
        if key != cur:
            cur = key
            snaps.append((dataclasses.replace(final, ar=ar, hr=hr, **base), i - 1))
        if about.get("isComplete") and (res["awayScore"], res["homeScore"]) != (ar, hr):
            ar, hr = res["awayScore"], res["homeScore"]
            snaps.append((dataclasses.replace(final, ar=ar, hr=hr, **base), i))
    snaps.append((final, len(plays) - 1))
    return snaps


async def mlb_replay(game, feed, snaps, mode="max"):
    mod = MlbModule(Hub(Transport()), "jays")
    mod.state.data["mode"] = mode
    mod.team_id = 141
    cursor = {"i": 10 ** 9}
    import meshscore.leagues.mlb as mlbmod
    real = mlbmod.mlb_get

    def fake_get(path, params=None):
        if "/feed/live" in path:
            i = cursor["i"]
            lp = feed["liveData"]["plays"]
            return {"liveData": {"plays": {"allPlays": lp["allPlays"][: i + 1],
                                           "scoringPlays": [x for x in lp["scoringPlays"] if x <= i]}}}
        return None
    mlbmod.mlb_get = fake_get
    got = []
    try:
        for g, idx in snaps:
            cursor["i"] = idx
            await mod.process([g])
            got += drain(mod)
    finally:
        mlbmod.mlb_get = real
    return got


MLB_PKS = sorted(GAMES["mlb"])


def check_mlb(got, game, full_game, start=(0, 0)):
    final = parse_mgame(game)
    finals = [i for i, t in enumerate(got) if "FINAL" in t]
    assert len(finals) == 1 and finals[0] == len(got) - 1, got
    fin = got[-1]
    assert f"{final.away} {final.ar} @ {final.home} {final.hr}" in fin, fin
    assert ("FINAL/" + str(final.inning) in fin) == (final.inning > 9), fin
    assert all(len(t.encode("utf-8")) <= MAX_BYTES for t in got), [t for t in got if len(t.encode()) > MAX_BYTES]
    last = start
    claimed = 0
    for t in got:
        if not (t.startswith("⚾") and re.search(r"\bRUNS?!", t)):
            continue
        sc = parse_score(t)
        assert sc and sc[0] >= last[0] and sc[1] >= last[1] and sc != last, (t, last)
        m = re.search(r"(\d+) RUNS!", t)
        n = int(m.group(1)) if m else 1
        assert (sc[0] - last[0]) + (sc[1] - last[1]) == n, (t, last)
        claimed += n
        last = sc
    if full_game:
        assert claimed == final.ar + final.hr, (claimed, final.ar, final.hr)
        assert got[0].startswith("⚾ Play ball"), got[0]
        assert last == (final.ar, final.hr)
        assert len([t for t in got if t.startswith("⏱ Extra innings")]) == (1 if final.inning > 9 else 0)
    assert len([t for t in got if t.startswith("⚾ Play ball")]) <= 1


@pytest.mark.parametrize("stride,offset", STRIDES)
@pytest.mark.parametrize("pk", MLB_PKS)
def test_mlb_whole_game(pk, stride, offset):
    g = GAMES["mlb"][pk]
    snaps = choose(mlb_snapshots(g["game"], g["feed"]), stride, offset)
    got = asyncio.run(mlb_replay(g["game"], g["feed"], repeats(snaps)))
    check_mlb(got, g["game"], full_game=True)
    if stride == 1:                                   # every run alert carries the real play description
        runs = [t for t in got if t.startswith("⚾") and re.search(r"\bRUNS?!", t)]
        assert runs and all(" | " in t for t in runs)
        assert any(len(t) > 60 for t in runs), runs


@pytest.mark.parametrize("pk", MLB_PKS)
def test_mlb_min_mode_is_quiet_but_complete(pk):
    g = GAMES["mlb"][pk]
    got = asyncio.run(mlb_replay(g["game"], g["feed"], repeats(mlb_snapshots(g["game"], g["feed"])), mode="min"))
    assert not any(re.search(r"\bRUNS?!", t) for t in got), got         # no per-run chatter at "min"
    thirds = [t for t in got if t.startswith("⚾ End of") or re.match(r"⚾ (Top|Bot|Mid|End) ", t)]
    assert len(thirds) <= 2, thirds                                       # after 3 and 6 innings, once each
    assert sum("FINAL" in t for t in got) == 1


@pytest.mark.parametrize("pk", MLB_PKS)
def test_mlb_restart_at_every_point_announces_nothing_old(pk):
    g = GAMES["mlb"][pk]
    full = mlb_snapshots(g["game"], g["feed"])
    for k in range(len(full) - 1):
        got = asyncio.run(mlb_replay(g["game"], g["feed"], repeats(full[k:])))
        check_mlb(got, g["game"], full_game=False, start=(full[k][0].ar, full[k][0].hr))
        seq = [s for s, _ in full[k:]]
        later_runs = sum(1 for a, b in zip(seq, seq[1:]) if b.ar + b.hr > a.ar + a.hr and b.state != "final")
        base = seq[0]
        assert len([t for t in got if t.startswith("⚾") and re.search(r"\bRUNS?!", t)]) == later_runs, (k, got)
        assert not any(t.startswith("⚾ Play ball") for t in got) or base.state == "preview", (k, got)


# ============================================================ OHL (HockeyTech)
from meshscore.leagues.hockeytech import OhlModule, parse_game  # noqa: E402

OHL_IDS = sorted(GAMES.get("ohl", {}))


def ohl_snapshots(entry, summary):
    """(Game, revealed goal count) for every state a poller would see in this real OHL game."""
    final = parse_game(entry)
    gs = summary["GC"]["Gamesummary"]
    goals = gs["goals"]
    last_period = max([int(g["period_id"]) for g in goals] + [final.period if final.period_name != "SO" else 4, 3])
    names = {1: "1st", 2: "2nd", 3: "3rd", 4: "OT"}
    base = dataclasses.replace(final, code=1, ag=0, hg=0, period=0, period_name="", clock="20:00", intermission=False)
    snaps = [(base, 0)]
    ag = hg = shown = 0
    for p in range(1, last_period + 1):
        length = 1200 if p <= 3 else 300
        snaps.append((dataclasses.replace(base, code=2, period=p, period_name=names[p], clock=f"{length // 60:02d}:00",
                                          ag=ag, hg=hg), shown))
        for g in [x for x in goals if int(x["period_id"]) == p]:
            if g["home"] == "1":
                hg += 1
            else:
                ag += 1
            shown += 1
            m, s = map(int, g["time"].split(":"))
            left = max(length - (m * 60 + s), 0)
            snaps.append((dataclasses.replace(base, code=2, period=p, period_name=names[p], clock=f"{left // 60:02d}:{left % 60:02d}",
                                              ag=ag, hg=hg), shown))
        if p in (1, 2):
            snaps.append((dataclasses.replace(base, code=2, period=p, period_name=names[p], clock="00:00", ag=ag, hg=hg,
                                              intermission=True), shown))
    if final.period_name == "SO":
        snaps.append((dataclasses.replace(base, code=2, period=4, period_name="SO", clock="00:00", ag=ag, hg=hg), shown))
    snaps.append((final, shown))
    return snaps


async def ohl_replay(summary, snaps, priority=()):
    mod = OhlModule(Hub(Transport()), "ohl")
    mod.state.data["mode"] = "max"
    mod.priority = list(priority)
    gs = summary["GC"]["Gamesummary"]
    cur = {"n": 0}

    async def fake_summary(gid):
        body = dict(gs)
        body["goals"] = gs["goals"][: cur["n"]]
        if cur["n"] < len(gs["goals"]) or not snaps[-1][0].final:
            body["mvps"] = []
        return {"GC": {"Gamesummary": body}}
    mod.summary = fake_summary
    got = []
    for g, n in snaps:
        cur["n"] = n
        await mod.process_games([g])
        got += [t for _, _, t, *_ in _drain_all(mod)]
    return got


def _drain_all(mod):
    out = []
    while not mod.hub.out.empty():
        out.append(mod.hub.out.get_nowait())
    return out


def check_ohl(got, final, full_game, expect_total, start=(0, 0)):
    finals = [i for i, t in enumerate(got) if "FINAL" in t]
    assert len(finals) == 1, got
    fin = got[finals[0]]
    assert f"{final.away} {final.ag} @ {final.home} {final.hg}" in fin, fin
    assert (("FINAL/" + ("SO" if final.period_name == "SO" else "OT")) in fin) == (final.period >= 4), fin
    assert all(len(t.encode("utf-8")) <= MAX_BYTES for t in got)
    assert all(t.startswith("⭐") for t in got[finals[0] + 1:]), got
    last, claimed = start, 0
    for t in got[: finals[0]]:
        if not t.startswith("🚨"):
            continue
        sc = parse_score(t)
        assert sc and sc[0] >= last[0] and sc[1] >= last[1] and sc != last, (t, last)
        m = re.match(r"🚨 (\d+) goals", t)
        n = int(m.group(1)) if m else 1
        assert (sc[0] - last[0]) + (sc[1] - last[1]) == n, (t, last)
        claimed += n
        last = sc
    assert claimed == expect_total, (claimed, expect_total, got)
    if full_game:
        assert got[0].startswith("🏒 Puck drop"), got[:2]


def ohl_expected(snaps):
    """Goals accounted for by the delivered snapshots; a shootout winner's +1 only appears in the final score."""
    counted = [g for g, _ in snaps if g.period_name != "SO"] or [snaps[0][0]]
    return counted[-1].ag + counted[-1].hg


@pytest.mark.parametrize("stride,offset", STRIDES)
@pytest.mark.parametrize("gid", OHL_IDS)
def test_ohl_whole_game(gid, stride, offset):
    g = GAMES["ohl"][gid]
    snaps = choose(ohl_snapshots(g["entry"], g["summary"]), stride, offset)
    final = parse_game(g["entry"])
    got = asyncio.run(ohl_replay(g["summary"], repeats(snaps), priority=[final.home, final.away]))
    check_ohl(got, final, full_game=True, expect_total=ohl_expected(snaps))
    stars = [(m["first_name"], m["last_name"]) for m in g["summary"]["GC"]["Gamesummary"]["mvps"]]
    if len(stars) == 3:                                   # a priority team's game: the three stars follow the final
        assert got[-1].startswith("⭐") and all(ln in got[-1] for _, ln in stars), got[-2:]
    if stride == 1:
        singles = [t for t in got if t.startswith("🚨") and not re.match(r"🚨 \d+ goals", t)]
        assert len(singles) == len(g["summary"]["GC"]["Gamesummary"]["goals"])
        ints = [re.match(r"⏸ End of (\w+):", t).group(1) for t in got if t.startswith("⏸")]
        assert ints == ["1st", "2nd"], ints


@pytest.mark.parametrize("gid", OHL_IDS)
def test_ohl_restart_at_every_point_announces_nothing_old(gid):
    g = GAMES["ohl"][gid]
    full = ohl_snapshots(g["entry"], g["summary"])
    final = parse_game(g["entry"])
    for k in range(len(full) - 1):
        got = asyncio.run(ohl_replay(g["summary"], repeats(full[k:]), priority=[final.home, final.away]))
        base = full[k][0]
        check_ohl(got, final, full_game=False, expect_total=ohl_expected(full[k:]) - (base.ag + base.hg), start=(base.ag, base.hg))
        # a restart inside an intermission must not announce the period that just ended (seen live, Oct 12)
        if base.intermission:
            ended = base.period_name
            assert not any(t.startswith(f"⏸ End of {ended}") for t in got), (k, got)
        # (HockeyTech flips a game to "live" before the puck drops - that pregame state still counts as not started)
        assert not any(t.startswith("🏒 Puck drop") for t in got) or base.state_code == 1
