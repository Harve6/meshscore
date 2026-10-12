"""Shared plumbing for the offline test suites (test_offline.py, test_postseason.py, test_phases.py, test_nfl.py):
a fake radio transport, the isolated-tempdir bootstrap, and the drain/settle helpers every suite needs.
Keeps each suite's real content - the real API data and the scenarios replayed against it - front and centre
instead of buried in setup boilerplate. Nothing here talks to a real radio or writes into the project folder."""
import asyncio
import os
import sys
import tempfile

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src")
sys.path.insert(0, os.path.abspath(SRC))

# The settings the suites were written against (a few bots on the usual channels). Real configs are TOML.
LEGACY_BOTS = {
    "ohl": {"enabled": True, "type": "ohl", "channel": "#ohl", "channel_index": None, "priority_teams": ["KIT", "GUE"]},
    "jays": {"enabled": True, "type": "mlb", "team": "TOR", "tag": "JAYS", "label": "Blue Jays",
             "channel": "#jays", "channel_index": None, "digest_hour": 10},
    "leafs": {"enabled": True, "type": "nhl", "team": "TOR", "label": "Maple Leafs",
              "channel": "#leafs", "channel_index": None, "digest_hour": 10,
              "overview": True, "overview_months": [4, 5, 6], "overview_batch_minutes": 20},
    "bills": {"enabled": False, "type": "nfl", "team": "BUF", "label": "Buffalo Bills",
              "channel": "#bills", "channel_index": None, "digest_hour": 10},
    "postseason": {"enabled": False, "type": "postseason", "channel": "#mlb", "channel_index": None,
                   "digest_hour": 10, "headsup_minutes": 30, "season": 0,
                   "tiers": {"F": "merged", "D": "key", "L": "lead", "W": "full"}},
}


class Facade:
    """One object exposing every name of the meshscore package, as the suites (written against a single-file
    bot) expect. Reads find the name in whichever module defines it; assignments (the suites monkey-patch `now`,
    `mlb_get`, ...) are written to EVERY module that has that name, so the patch takes effect wherever it is used."""

    def __init__(self, modules):
        object.__setattr__(self, "_mods", modules)

    def __getattr__(self, name):
        for mod in self._mods:
            if hasattr(mod, name):
                return getattr(mod, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        hit = False
        for mod in self._mods:
            if hasattr(mod, name):
                setattr(mod, name, value)
                hit = True
        if not hit:
            object.__setattr__(self, name, value)

    # the original class names
    def JaysModule(self, hub):
        return self.MlbModule(hub, "jays")

    def sports_file(self):
        return self._sports_file()


def bootstrap(extra_cfg=None):
    """Fresh temp working directory (state/log files land there, never in the project folder), the package loaded
    with a config suited to fast offline runs. Returns a Facade over the package."""
    os.chdir(tempfile.mkdtemp())
    import importlib
    names = ["meshscore.core", "meshscore.config", "meshscore.sameair", "meshscore.radio", "meshscore.hub",
             "meshscore.leagues.hockeytech", "meshscore.leagues.mlb", "meshscore.leagues.mlb_postseason",
             "meshscore.leagues.nfl", "meshscore.leagues.nhl", "meshscore.cli"]
    mods = [importlib.import_module(n) for n in names]
    m = Facade(mods)
    m.reset()
    m.CFG["modules"] = {k: dict(v) for k, v in LEGACY_BOTS.items()}
    m.CFG.update(send_gap=0, command_cooldown=0, log_file="", timezone="America/Toronto", announce_on_start=True,
                 dm_default="ohl", player_aliases={"pca": 691718, "vlad": 665489, "vladdy": 665489})
    if extra_cfg:
        m.CFG.update(extra_cfg)
    m.setup_logging()
    m.init_tz()
    return m


def make_fake(m, chans):
    """A Transport subclass that records what it would have sent instead of touching real hardware.
    `chans` is the {module_name: slot} map the fake radio pretends to have."""
    class Fake(m.Transport):
        name = "fake"
        alive = True

        def __init__(self):
            self.sent = []
            self.chans = dict(chans)

        async def start(self, on_message, channels):
            self.alive = True

        async def send_channel(self, ch, text):
            self.sent.append((ch, text))
            return True

        async def send_dm(self, key, text):
            self.sent.append(("DM:" + key, text))
            return True

    return Fake


async def settle(hub, t=0.2):
    """Wait for a hub's command queue and outbox to drain, then a little more for anything they trigger."""
    for _ in range(40):
        await asyncio.sleep(t / 10)
        if hub.cmdq.empty() and hub.out.empty():
            break
    await asyncio.sleep(t)


def drain_raw(mod):
    """Pop everything a module's hub has queued, as (kind, dest, text) tuples."""
    items = []
    while not mod.hub.out.empty():
        items.append(mod.hub.out.get_nowait()[:3])
    return items


def drain_texts(mod):
    """Pop everything a module's hub has queued, as just the message texts."""
    return [x for _, _, x in drain_raw(mod)]


def _say_common(hub, t, m):
    async def say(text, chan="", dm=False, name="Tester", key="abcdef123456"):
        t.sent.clear()
        await hub.on_message(m.Incoming(text, dm, name, key if dm else "", 2, 7.5, chan))
        await settle(hub, 1.5)
        print(f"  > [{('DM' if dm else '#' + chan)}] {text}")
        for c, x in t.sent:
            print(f"      -> {c}: {x}  ({len(x.encode())}B)")
        if not t.sent:
            print("      (no reply)")
        assert all(len(x.encode()) <= m.CFG["max_msg_bytes"] for _, x in t.sent), "message too long"
    return say


def make_say(hub, t, m):
    """A say(text, chan=..., dm=...) helper bound to one hub/fake-transport pair: feeds a message in, waits for
    the reply, prints the exchange, and returns the reply texts. Used by most suites' command-by-command checks."""
    say = _say_common(hub, t, m)

    async def wrapped(*a, **kw):
        await say(*a, **kw)
        return [x for _, x in t.sent]
    return wrapped


def make_say_raw(hub, t, m):
    """Like make_say, but returns the raw (dest, text) tuples - what test_offline.py's checks index into."""
    say = _say_common(hub, t, m)

    async def wrapped(*a, **kw):
        await say(*a, **kw)
        return list(t.sent)
    return wrapped


def at_factory(m):
    """at(y, mo, d, h=12, mi=0) - point the bot's clock at a specific moment, for replaying a real past game
    as though it were happening now. Caller is responsible for restoring m.now afterwards."""
    from datetime import datetime

    def at(y, mo, d, h=12, mi=0):
        m.now = lambda: datetime(y, mo, d, h, mi, tzinfo=m.TZ)
    return at
