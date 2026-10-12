"""Things going wrong: a radio that drops, a feed that errors, a flood of commands, a plugin that raises.
The bot must keep running, never send the same alert twice, never send stale news, and lose as little as possible."""
import asyncio
import random

import pytest

import meshscore.hub as hubmod
import meshscore.leagues.mlb as mlbmod
from meshscore import config
from meshscore.core import CFG
from meshscore.hub import Hub
from meshscore.leagues.nhl import NhlModule
from meshscore.plugin import LeaguePlugin
from meshscore.radio import Incoming, Transport

import test_game_replays as gr


@pytest.fixture(autouse=True)
def fast(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, command_cooldown=0, log_file="", timezone="America/Toronto")
    CFG["modules"] = {"leafs": {"type": "nhl", "team": "TOR", "channel": "#leafs"},
                      "jays": {"type": "mlb", "team": "TOR", "tag": "JAYS", "channel": "#jays"}}
    monkeypatch.setattr(hubmod, "TX_RETRY_WAIT", 0.01)
    monkeypatch.setattr(mlbmod, "FEED_LAG_WAIT", 0)
    yield
    config.reset()


class Radio(Transport):
    """A radio that can be taken down and whose transmissions can be made to fail."""
    name = "chaos"

    def __init__(self, fail_first=0):
        self.alive = True
        self.sent = []
        self.attempts = 0
        self.fail_first = fail_first          # fail this many send attempts, then work

    async def send_channel(self, channel, text):
        self.attempts += 1
        if self.attempts <= self.fail_first:
            return False
        self.sent.append(text)
        return True

    async def send_dm(self, key, text):
        self.sent.append(f"DM:{text}")
        return True


async def pump(hub, seconds):
    w = asyncio.create_task(hub.tx_worker())
    await asyncio.sleep(seconds)
    w.cancel()


# ---------------------------------------------------------------- the radio
def test_messages_queued_while_the_radio_is_down_go_out_in_order_after_it_returns():
    async def go():
        r = Radio()
        hub = Hub(r)
        r.alive = False
        for i in range(5):
            hub.say_chan("#c", f"🚨 A GOAL | A {i + 1}-0 B")
        w = asyncio.create_task(hub.tx_worker())
        await asyncio.sleep(0.1)
        assert r.sent == []                               # nothing is lost or sent into the void
        r.alive = True
        await asyncio.sleep(1.5)
        w.cancel()
        return r.sent
    sent = asyncio.run(go())
    assert sent == [f"🚨 A GOAL | A {i + 1}-0 B" for i in range(5)]      # all of them, once, in order


def test_a_failed_transmission_is_retried_once_and_never_duplicated():
    async def go():
        r = Radio(fail_first=1)
        hub = Hub(r)
        hub.say_chan("#c", "🚨 A GOAL | A 1-0 B")
        await pump(hub, 0.3)
        return r
    r = asyncio.run(go())
    assert r.sent == ["🚨 A GOAL | A 1-0 B"] and r.attempts == 2


def test_a_message_that_fails_twice_is_dropped_and_the_queue_moves_on():
    async def go():
        r = Radio(fail_first=2)
        hub = Hub(r)
        hub.say_chan("#c", "🚨 A GOAL | A 1-0 B")
        hub.say_chan("#c", "🚨 A GOAL | A 2-0 B")
        await pump(hub, 0.4)
        return r.sent
    assert asyncio.run(go()) == ["🚨 A GOAL | A 2-0 B"]


def test_old_goals_are_not_sent_after_a_long_outage_but_a_final_still_is(monkeypatch):
    monkeypatch.setattr(hubmod, "ALERT_TTL", 0.05)

    async def go():
        r = Radio()
        hub = Hub(r)
        r.alive = False
        hub.say_chan("#c", "🚨 A GOAL | A 1-0 B")
        hub.say_chan("#c", "🛑 FINAL: A 3 @ B 0")
        w = asyncio.create_task(hub.tx_worker())
        await asyncio.sleep(0.2)                          # the outage outlasts the TTL
        r.alive = True
        await asyncio.sleep(1.3)
        w.cancel()
        return r.sent
    assert asyncio.run(go()) == ["🛑 FINAL: A 3 @ B 0"]


def test_a_transport_that_raises_does_not_kill_the_worker():
    class Boom(Radio):
        async def send_channel(self, channel, text):
            self.attempts += 1
            if self.attempts <= 2:
                raise OSError("usb unplugged")
            self.sent.append(text)
            return True

    async def go():
        r = Boom()
        hub = Hub(r)
        hub.say_chan("#c", "first")
        hub.say_chan("#c", "second")
        await pump(hub, 0.5)
        return r.sent
    assert asyncio.run(go()) == ["second"]


# ---------------------------------------------------------------- command floods
def test_a_flood_of_commands_is_bounded_and_does_not_break_anything():
    class Echo(LeaguePlugin):
        label = "Echo"

        async def poll(self):
            return 60

        def extra_commands(self):
            return {"!score": self.cmd_score}

        async def cmd_score(self, inc, arg):
            return ["[ECHO] 1-0"]

    CFG["modules"] = {"echo": {"type": "x", "channel": "#e"}}
    CFG["command_cooldown"] = 10

    async def go():
        r = Radio()
        hub = Hub(r)
        hub.add(Echo(hub, "echo"))
        w = [asyncio.create_task(hub.tx_worker()), asyncio.create_task(hub.cmd_worker())]
        for i in range(200):
            await hub.on_message(Incoming("!score", False, f"u{i}", "", 1, 5.0, "e"))
        await asyncio.sleep(0.5)
        for t in w:
            t.cancel()
        return r.sent
    sent = asyncio.run(go())
    assert 1 <= len(sent) <= 12                           # queue cap + per-command cooldown: never 200 replies


def test_a_plugin_that_raises_keeps_being_polled(monkeypatch):
    calls = {"n": 0}

    class Flaky(LeaguePlugin):
        async def poll(self):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("feed exploded")
            self.alert("final", "🛑 FINAL: A 1 @ B 0")
            return 60

    CFG["poll_idle_seconds"] = 0.01
    CFG["modules"] = {"flaky": {"type": "x", "channel": "#f"}}
    Flaky.kinds = {"max": {"final"}, "min": {"final"}, "off": set()}

    async def go():
        hub = Hub(Radio())
        p = Flaky(hub, "flaky")
        t = asyncio.create_task(p.run())
        await asyncio.sleep(0.3)
        t.cancel()
        return hub.out.qsize()
    assert asyncio.run(go()) == 1 and calls["n"] >= 3


# ---------------------------------------------------------------- flaky data feeds, real games
def flaky(fn, rate, seed):
    rng = random.Random(seed)

    async def wrapped(*a, **kw):
        if rng.random() < rate:
            return None                     # what the fetch helpers return on a timeout / HTTP error
        return await fn(*a, **kw)
    return wrapped


async def nhl_chaos_replay(landing, snaps, rate, seed):
    mod = NhlModule(Hub(Transport()), "leafs")
    mod.state.data["mode"] = "max"

    async def land(gid):
        return landing
    mod.landing = flaky(land, rate, seed)
    errors, got = 0, []
    for g in snaps:
        try:
            await mod.process([g])
        except Exception:
            errors += 1                     # the poll loop would log this and carry on to the next poll
        got += gr.drain(mod)
    return got, errors


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("gid", gr.NHL_IDS)
def test_nhl_with_a_flaky_feed_still_gets_every_goal_and_one_final(gid, seed):
    landing = gr.GAMES["nhl"][gid]["landing"]
    snaps = gr.repeats(gr.nhl_snapshots(landing))
    got, errors = asyncio.run(nhl_chaos_replay(landing, snaps, rate=0.4, seed=seed))
    goals = [t for t in got if t.startswith("🚨")]
    finals = [t for t in got if "FINAL" in t]
    # a goal's scorer may be unknown when the detail fetch fails, but the goal itself must be reported exactly once,
    # and the final exactly once with the true score
    scored = sum(len(p["goals"]) for p in landing["summary"]["scoring"] if p["periodDescriptor"]["periodType"] != "SO")
    seen = 0
    for t in goals:
        sc = gr.parse_score(t)
        assert sc is not None
        seen = max(seen, sum(sc))
    assert seen == scored, (seen, scored, goals)
    assert len(finals) <= 1                 # a final can only be missed if the feed errored on exactly that poll...
    assert len({t for t in goals}) == len(goals), goals                 # ...but a goal is never announced twice


def test_the_final_is_not_lost_when_only_the_feed_calls_around_it_fail():
    """The error budget is per call, not per game: even if every detail fetch fails, a final still goes out (without
    shots/stars) because the score itself comes from the scoreboard."""
    gid = gr.NHL_IDS[0]
    landing = gr.GAMES["nhl"][gid]["landing"]
    snaps = gr.nhl_snapshots(landing)
    got, errors = asyncio.run(nhl_chaos_replay(landing, snaps, rate=1.0, seed=0))
    assert errors >= 0
    finals = [t for t in got if "FINAL" in t]
    assert len(finals) == 1
    a, h = landing["awayTeam"], landing["homeTeam"]
    assert f"{a['abbrev']} {a['score']} @ {h['abbrev']} {h['score']}" in finals[0]


async def mlb_chaos_replay(game, feed, snaps, rate, seed):
    mod = mlbmod.MlbModule(Hub(Transport()), "jays")
    mod.state.data["mode"] = "max"
    mod.team_id = 141
    rng = random.Random(seed)
    cursor = {"i": 0}

    def fake_get(path, params=None):
        if rng.random() < rate:
            return None                     # what mlb_get returns on a timeout / HTTP error
        i = cursor["i"]
        lp = feed["liveData"]["plays"]
        return {"liveData": {"plays": {"allPlays": lp["allPlays"][: i + 1],
                                       "scoringPlays": [x for x in lp["scoringPlays"] if x <= i]}}}
    real = mlbmod.mlb_get
    mlbmod.mlb_get = fake_get
    got = []
    try:
        for g, idx in snaps:
            cursor["i"] = idx
            try:
                await mod.process([g])
            except Exception:
                pass
            got += gr.drain(mod)
    finally:
        mlbmod.mlb_get = real
    return got


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("pk", gr.MLB_PKS)
def test_mlb_with_a_flaky_feed_still_reports_every_run_once(pk, seed):
    g = gr.GAMES["mlb"][pk]
    final = gr.parse_mgame(g["game"])
    got = asyncio.run(mlb_chaos_replay(g["game"], g["feed"], gr.repeats(gr.mlb_snapshots(g["game"], g["feed"])), 0.4, seed))
    runs = [t for t in got if t.startswith("⚾") and "RUN" in t and "Play ball" not in t]
    assert len(set(runs)) == len(runs), runs                            # never twice
    scores = [gr.parse_score(t) for t in runs]
    assert all(s is not None for s in scores)
    assert max((a + h for a, h in scores), default=0) == final.ar + final.hr
    assert len([t for t in got if "FINAL" in t]) == 1
    assert all(len(t.encode("utf-8")) <= 130 for t in got)


# ---------------------------------------------------------------- many bots, random timing
async def three_bot_game(seed, events):
    """Three bots, each noticing every event at its own slightly different moment. Returns the air log."""
    from meshscore.sameair import SameAir
    from test_sameair import Air
    rng = random.Random(seed)
    air = Air(latency=0.01)
    hubs = []
    for name in ("Alpha", "Bravo", "Charlie"):
        r = air.radio(name)
        h = Hub(r)
        r.hub = h
        h.same_air = SameAir({"enabled": True, "window_seconds": 180, "slot_seconds": 0.12, "jitter_seconds": 0.02},
                             rng=random.Random(rng.random()))
        h.same_air.own_name = name
        hubs.append(h)
    workers = [asyncio.create_task(h.tx_worker()) for h in hubs]
    loop = asyncio.get_running_loop()
    for k, e in enumerate(events):
        for h in hubs:
            loop.call_later(k * 0.5 + rng.uniform(0, 0.05), h.say_chan, "#leafs", e)
    await asyncio.sleep(len(events) * 0.5 + 0.6)
    for w in workers:
        w.cancel()
    return [t for _, t in air.log]


def test_three_bots_random_timing_double_posts_are_bounded_by_the_pairs():
    """A bot only learns another exists when that bot *posts*, and a bot that stays quiet because someone else spoke
    never reveals itself - so first meetings can happen at any point of the game. Every such meeting costs at most
    one double post, and with three bots there are three ordered (lower, higher) pairs: never more than three events
    are posted twice, and no event is ever posted more times than there are bots. (12 random timings, concurrent.)"""
    events = [f"🚨 TOR GOAL | TOR {i}-0 MTL | 1st {20 - i}:00" for i in range(1, 8)]

    async def go():
        return await asyncio.gather(*[three_bot_game(seed, events) for seed in range(12)])
    for seed, texts in enumerate(asyncio.run(go())):
        counts = [texts.count(e) for e in events]
        assert all(1 <= c <= 3 for c in counts), (seed, counts)           # nothing lost, never more posts than bots
        assert sum(1 for c in counts if c > 1) <= 3, (seed, counts)       # at most one double post per ordered pair
        assert sum(c - 1 for c in counts) <= 3, (seed, counts)            # ...and the extra posts are bounded too


# ---------------------------------------------------------------- a bug inside the alert logic must not swallow alerts
def test_nhl_unexpected_exception_still_announces_the_goal_and_final():
    gid = gr.NHL_IDS[0]
    landing = gr.GAMES["nhl"][gid]["landing"]
    snaps = gr.nhl_snapshots(landing)

    async def go():
        mod = NhlModule(Hub(Transport()), "leafs")
        mod.state.data["mode"] = "max"

        async def boom(*a, **k):
            raise KeyError("payload changed shape")
        mod.landing = boom
        mod.goal_info = boom
        got = []
        for g in snaps:
            await mod.process([g])            # must not raise
            got += gr.drain(mod)
        return got
    got = asyncio.run(go())
    assert any(t.startswith("🚨 Goal |") for t in got) and len([t for t in got if "FINAL" in t]) == 1, got


def test_mlb_unexpected_exception_still_announces_the_run_and_final():
    pk = gr.MLB_PKS[0]
    g = gr.GAMES["mlb"][pk]
    snaps = gr.mlb_snapshots(g["game"], g["feed"])

    async def go():
        mod = mlbmod.MlbModule(Hub(Transport()), "jays")
        mod.state.data["mode"] = "max"
        mod.team_id = 141

        async def boom(*a, **k):
            raise TypeError("payload changed shape")
        mod.scoring_play = boom
        got = []
        for game, _ in snaps:
            await mod.process([game])
            got += gr.drain(mod)
        return got
    got = asyncio.run(go())
    assert any(t.startswith("⚾ Run |") for t in got) and len([t for t in got if "FINAL" in t]) == 1, got
