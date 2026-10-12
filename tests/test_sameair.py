"""Same-air coordination: the pure logic, the hub's transmit path, and whole bots on one simulated channel.
No network, no radio."""
import asyncio
import random

import pytest

from meshscore import config
from meshscore.core import CFG
from meshscore.hub import Hub
from meshscore.radio import Incoming, Transport
from meshscore.sameair import AirBudget, SameAir, kind_of, score_key


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make(clock=None, **cfg):
    c = {"enabled": True, "window_seconds": 180, "slot_seconds": 5, "jitter_seconds": 1.5, **cfg}
    return SameAir(c, rng=random.Random(1), clock=clock or Clock())


# ---------------------------------------------------------------- signatures
def test_score_key_reads_live_and_final_formats():
    assert score_key("🚨 GUE GOAL E. Frossard | KIT 0-5 GUE | 3rd 11:43") == ("KIT", "GUE", 0, 5)
    assert score_key("🛑 FINAL: KIT 0 @ GUE 6 | SOG 19-25") == ("KIT", "GUE", 0, 6)
    assert score_key("🏒 Puck drop: KIT @ GUE") is None


def test_kind_groups_equivalent_posts():
    assert kind_of("🛑 FINAL: A 1 @ B 2") == kind_of("🏁 FINAL: A 1 @ B 2") == "final"
    assert kind_of("🏒 Puck drop: A @ B") == kind_of("🏈 Kickoff: A @ B") == "start"
    assert kind_of("🚨 A GOAL | A 1-0 B") == "🚨"
    assert kind_of("[TOR] No game today") == "text"


# ---------------------------------------------------------------- listen before send
def test_equivalent_post_is_a_duplicate_but_a_new_score_is_not():
    sa = make()
    sa.hear("#leafs", "🚨 TOR GOAL M. Matthews | TOR 1-0 MTL | 1st 12:00", "OtherBot")
    assert sa.is_duplicate("#leafs", "🚨 TOR goal Matthews | TOR 1-0 MTL | 1st 12:01")      # same event, other wording
    assert not sa.is_duplicate("#leafs", "🚨 MTL goal | TOR 1-1 MTL | 1st 14:00")              # next goal
    assert not sa.is_duplicate("#other", "🚨 TOR GOAL M. Matthews | TOR 1-0 MTL | 1st 12:00")  # another channel


def test_heard_posts_expire():
    clock = Clock()
    sa = make(clock)
    sa.hear("#c", "🛑 FINAL: A 1 @ B 2", "Bot2")
    assert sa.is_duplicate("#c", "🛑 FINAL: A 1 @ B 2")
    clock.advance(181)
    assert not sa.is_duplicate("#c", "🛑 FINAL: A 1 @ B 2")


def test_own_posts_never_count():
    sa = make()
    sa.own_name = "MyBot"
    sa.hear("#c", "🛑 FINAL: A 1 @ B 2", "MyBot")
    assert not sa.is_duplicate("#c", "🛑 FINAL: A 1 @ B 2")


def test_replies_dedupe_by_text_not_by_score():
    sa = make()
    sa.hear("#c", "[TOR] TOR 2 @ MTL 1 | 2nd", "Bot2")
    assert sa.is_duplicate("#c", "[TOR] TOR 3 @ MTL 1 | 2nd", is_reply=True)    # same words, digits may differ
    assert not sa.is_duplicate("#c", "🚨 TOR GOAL | TOR 2-1 MTL | 2nd 1:00")    # an alert is not hidden by a reply


# ---------------------------------------------------------------- the line-up only exists once a peer is known
def test_no_delay_until_another_bot_is_heard():
    sa = make()
    sa.own_name = "BotB"
    assert sa.delay() == 0
    sa.hear("#c", "just a person chatting", "Alice")
    assert sa.delay() == 0                                   # a person is not a bot
    sa.hear("#c", "[LEAFS BOT] pong", "BotA")
    assert sa.rank() == 1 and 5 <= sa.delay() <= 6.5         # BotA sorts first, so BotB waits one slot


def test_lineup_is_computed_identically_by_every_bot():
    a, b, c = make(), make(), make()
    for sa, me in ((a, "Alpha"), (b, "Bravo"), (c, "Charlie")):
        sa.own_name = me
        for other in ("Alpha", "Bravo", "Charlie"):
            if other != me:
                sa.hear("#c", "[X BOT] hi", other)
    assert (a.rank(), b.rank(), c.rank()) == (0, 1, 2)
    assert a.delay() <= 1.5 and 5 <= b.delay() <= 6.5 and 10 <= c.delay() <= 11.5


def test_rank_can_be_pinned():
    sa = make(rank=0)
    sa.own_name = "Zed"
    sa.hear("#c", "[X BOT] hi", "Alpha")
    assert sa.rank() == 0                                    # would be 1 alphabetically


def test_declared_peers_start_the_lineup_immediately():
    sa = make(peers=["BotA"])
    sa.own_name = "BotB"
    assert sa.rank() == 1 and sa.delay() >= 5                # nothing heard yet


def test_disabled_does_nothing():
    sa = make(enabled=False)
    sa.hear("#c", "🛑 FINAL: A 1 @ B 2", "Bot2")
    assert not sa.is_duplicate("#c", "🛑 FINAL: A 1 @ B 2") and sa.delay() == 0


def test_peers_survive_a_restart(tmp_path):
    p = str(tmp_path / "air.json")
    a = SameAir({"enabled": True}, rng=random.Random(1), path=p)
    a.hear("#c", "[LEAFS BOT] pong", "OtherBot")
    b = SameAir({"enabled": True}, rng=random.Random(1), path=p)
    assert "OtherBot" in b.peers


# ---------------------------------------------------------------- airtime budget
def test_budget_tiers():
    clock = Clock()
    ab = AirBudget({"max_per_hour": 10}, clock=clock)
    for _ in range(8):
        ab.record()
    assert ab.allow("🛑 FINAL: A 1 @ B 2")                          # finals always
    assert ab.allow("🚨 A GOAL | A 1-0 B")                          # goals until the cap
    assert not ab.allow("⏸ End of 1st: A 1-0 B")                   # period results go first (80%)
    ab.record()
    ab.record()
    assert not ab.allow("🚨 A GOAL | A 2-0 B") and ab.allow("🛑 FINAL: A 3 @ B 0")
    clock.advance(3601)
    assert ab.allow("🚨 A GOAL | A 2-0 B")                          # an hour later the window has emptied


def test_budget_off_by_default():
    ab = AirBudget({"max_per_hour": 0})
    assert all(ab.allow("🚨 A GOAL | A 1-0 B") for _ in range(100))


# ---------------------------------------------------------------- the hub's transmit path
class Recorder(Transport):
    name = "rec"
    alive = True

    def __init__(self):
        self.sent = []

    async def send_channel(self, channel, text):
        self.sent.append((channel, text))
        return True


async def _mk(t):
    return Hub(t)


@pytest.fixture
def hub_env():
    config.reset()
    CFG.update(send_gap=0, same_air={"enabled": True, "window_seconds": 180, "slot_seconds": 0.01, "jitter_seconds": 0.01},
               airtime={"max_per_hour": 0})
    yield
    config.reset()


def run_hub(hub, seconds=0.4):
    async def go():
        w = asyncio.create_task(hub.tx_worker())
        await asyncio.sleep(seconds)
        w.cancel()
    asyncio.run(go())


def test_hub_skips_a_post_another_bot_already_made(hub_env):
    t = Recorder()
    hub = asyncio.run(_mk(t))
    hub.same_air.hear("#leafs", "🚨 TOR GOAL Matthews | TOR 1-0 MTL | 1st 12:00", "OtherBot")
    hub.say_chan("#leafs", "🚨 TOR GOAL Matthews | TOR 1-0 MTL | 1st 12:01")        # duplicate: dropped
    hub.say_chan("#leafs", "🚨 MTL GOAL | TOR 1-1 MTL | 1st 15:00")                 # new: sent
    run_hub(hub)
    assert [x for _, x in t.sent] == ["🚨 MTL GOAL | TOR 1-1 MTL | 1st 15:00"]
    assert hub.same_air.skipped == 1


def test_hub_budget_drops_goals_but_not_finals(hub_env):
    CFG["airtime"] = {"max_per_hour": 2}
    t = Recorder()
    hub = asyncio.run(_mk(t))
    hub.say_chan("#c", "🚨 A GOAL | A 1-0 B")
    hub.say_chan("#c", "🚨 A GOAL | A 2-0 B")
    hub.say_chan("#c", "🚨 A GOAL | A 3-0 B")        # over the cap
    hub.say_chan("#c", "🛑 FINAL: A 3 @ B 0")        # finals always go
    run_hub(hub)
    assert [x for _, x in t.sent] == ["🚨 A GOAL | A 1-0 B", "🚨 A GOAL | A 2-0 B", "🛑 FINAL: A 3 @ B 0"]


def test_replies_are_never_budget_capped(hub_env):
    CFG["airtime"] = {"max_per_hour": 1}
    t = Recorder()
    hub = asyncio.run(_mk(t))
    hub.say_chan("#c", "🚨 A GOAL | A 1-0 B")
    for i in range(3):
        hub.say_chan("#c", f"[TOR] answer {i}", reply=True)
    run_hub(hub)
    assert len(t.sent) == 4


def test_config_defaults_have_same_air_on():
    config.reset()
    assert CFG["same_air"]["enabled"] is True and CFG["airtime"]["max_per_hour"] == 0


# ---------------------------------------------------------------- two whole bots on one simulated air
class Air:
    """A shared channel: whatever one radio transmits, every other radio hears a moment later."""

    def __init__(self, latency=0.02):
        self.radios, self.log, self.latency = [], [], latency

    def radio(self, node_name):
        air = self

        class R(Transport):
            name = node_name
            alive = True
            own_name = node_name
            hub = None

            async def send_channel(self, channel, text):
                air.log.append((node_name, text))
                for other in air.radios:
                    if other is not self:
                        asyncio.get_running_loop().call_later(
                            air.latency, lambda o=other: asyncio.ensure_future(
                                o.hub.on_message(Incoming(text, False, node_name, "", 1, 5.0, channel.lstrip("#")))))
                return True

        r = R()
        air.radios.append(r)
        return r


def two_bots(slot=0.3, peers_known=True):
    air = Air()
    hubs = []
    for i, name in enumerate(("BotA", "BotB")):
        r = air.radio(name)
        hub = Hub(r)
        r.hub = hub
        hub.same_air = SameAir({"enabled": True, "window_seconds": 180, "slot_seconds": slot, "jitter_seconds": 0.05,
                                "peers": ["BotB" if i == 0 else "BotA"] if peers_known else []},
                               rng=random.Random(i + 1))
        hub.same_air.own_name = name
        hubs.append(hub)
    return air, hubs


def run_two(hubs, seconds=1.5, plan=()):
    """plan: [(at_second, hub_index, text)] posts to make."""
    async def go():
        ws = [asyncio.create_task(h.tx_worker()) for h in hubs]
        loop = asyncio.get_running_loop()
        for at, i, text in plan:
            loop.call_later(at, hubs[i].say_chan, "#leafs", text)
        await asyncio.sleep(seconds)
        for w in ws:
            w.cancel()
    asyncio.run(go())


def test_two_bots_same_goal_one_post(hub_env):
    air, hubs = two_bots()
    goal = "🚨 TOR GOAL Matthews | TOR 1-0 MTL | 1st 12:00"
    run_two(hubs, plan=[(0.0, 0, goal), (0.0, 1, goal)])
    assert air.log == [("BotA", goal)]                               # exactly one copy, from the bot at the front
    assert hubs[1].same_air.skipped == 1


def test_two_bots_a_whole_game_no_duplicates(hub_env):
    air, hubs = two_bots()
    events = ["🏒 Puck drop: TOR @ MTL", "🚨 TOR GOAL | TOR 1-0 MTL | 1st 15:00", "🚨 MTL GOAL | TOR 1-1 MTL | 1st 9:00",
              "⏸ End of 1st: TOR 1-1 MTL | SOG 9-8", "🛑 FINAL: TOR 3 @ MTL 1"]
    plan = []
    for k, e in enumerate(events):
        plan += [(k * 0.8, 0, e), (k * 0.8, 1, e)]
    run_two(hubs, seconds=0.8 * len(events) + 1.0, plan=plan)
    assert [t for _, t in air.log] == events and {n for n, _ in air.log} == {"BotA"}


def test_second_bot_covers_when_the_first_is_down(hub_env):
    air, hubs = two_bots()
    goal = "🚨 TOR GOAL | TOR 2-1 MTL | 2nd 3:00"
    run_two(hubs, plan=[(0.0, 1, goal)])                             # only BotB noticed (BotA is off)
    assert air.log == [("BotB", goal)]


def test_a_command_reply_is_not_posted_twice(hub_env):
    air, hubs = two_bots()
    for h in hubs:
        h.say_chan("#leafs", "[LEAFS] TOR 2 @ MTL 1 | 2nd 3:00", reply=True)
    run_two(hubs)
    assert len(air.log) == 1


def test_first_meeting_can_double_post_once_then_never_again(hub_env):
    """Honest limit: two bots that have never heard each other (and declared no peers) both post the first event; they
    learn of each other from it, and from then on the line-up keeps them to one post."""
    air, hubs = two_bots(peers_known=False)
    e1, e2 = "🚨 TOR GOAL | TOR 1-0 MTL | 1st 10:00", "🚨 MTL GOAL | TOR 1-1 MTL | 1st 5:00"
    run_two(hubs, seconds=2.0, plan=[(0.0, 0, e1), (0.0, 1, e1), (1.0, 0, e2), (1.0, 1, e2)])
    texts = [t for _, t in air.log]
    assert texts.count(e1) == 2 and texts.count(e2) == 1, air.log
