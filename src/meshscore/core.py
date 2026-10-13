"""Shared plumbing: configuration, logging, time zone, text helpers, persistent state, season phases and alert levels."""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

log = logging.getLogger("meshscore")

USER_AGENT = "MeshScore (+https://github.com/Harve6/meshscore)"
SPORTS_FILE = "meshscore_phases.json"

# ==========================================================================
# CONFIG  (the live settings dict; meshscore.config fills it from meshscore.toml)
# ==========================================================================
DEFAULTS = {
    # --- radio ---
    "transport": "serial",            # "serial" (USB) | "ble" (Bluetooth) | "tcp" (WiFi companion)
    "port": "",                       # serial: COM3, /dev/ttyUSB0 ...
    "baud": 115200,
    "host": "",                       # tcp: IP address of the WiFi companion radio
    "tcp_port": 5000,
    "ble_address": "",                # ble: device address (meshscore scan-ble lists them)
    "ble_name": "",
    "ble_pin": "",                    # ble: pairing PIN (empty = MeshCore's default, 123456)
    # --- bot behaviour ---
    "modules": {},                    # the bots: {"leafs": {"type": "nhl", "team": "TOR", "channel": "#leafs"}, ...}
    "phases": {"pre": False, "reg": True, "post": True},   # which parts of the season each bot covers
    "dm_default": "",                 # which bot a plain DM command (!score) goes to (empty = the first one)
    "announce_on_start": False,       # post "... active on #channel" when the bot starts (false = silent restarts)
    "instance_port": 47653,           # local port used to detect a second copy on THIS computer (0 = no check)
    "respond_to_dms": True,
    "log_chat": False,                # log EVERY channel message the radio hears (HEARD ...), not just the bot's own traffic
    "timezone": "",                   # IANA name like "America/Toronto"; empty = this computer's zone
    "poll_live_seconds": 20,          # while games are live / about to start
    "poll_idle_seconds": 300,         # when nothing is happening
    "busy_live_games": 3,             # league-wide channels: this many games live at once = "busy night" (0 = never)
    "command_cooldown": 15,
    "send_gap": 6,                    # min seconds between transmissions
    "max_msg_bytes": 130,             # MeshCore's channel message limit
    "max_reply_msgs": 2,
    "max_follows": 3,
    "admin_names": [],                # node names allowed to use !mode / !phase (names can be spoofed in a channel)
    "admin_keys": [],                 # public-key prefixes allowed to do the same by DM (authenticated); both empty = anyone
    "default_mode": "max",            # alert level for new bots: max (everything) | min (period results + final) | off
    "dm_even_if_broadcast": False,    # DM followers even when the channel already got the alert
    "full_days_back": 14,
    "full_days_ahead": 10,
    "log_file": "meshscore.log",
    "player_aliases": {},             # MLB: nickname -> player id, for !player / !follow shortcuts
    # --- HockeyTech feed (OHL by default; other HockeyTech leagues need their own client code and public key) ---
    "api_key": "f1aa699db3d81487",
    "client_code": "ohl",
    "digest_hour": 12,
    "early_final_seconds": 90,        # announce the final this long after the clock hits 00:00 with a winner, if the feed is slow
    "summary_fetch": True,            # fetch scorer / shots / stars from the game summary
    # --- running several bots on the same air ---
    "same_air": {
        "enabled": True,              # listen before sending: skip a post another bot already made
        "window_seconds": 180,        # how long a heard post counts as "already announced"
        "slot_seconds": 5,            # line-up spacing: the bot in position 1 waits this long, position 2 twice that...
        "jitter_seconds": 1.5,        # small random extra so two bots never tie
        "rank": -1,                   # position in the line-up (0 first); -1 = automatic, by sorted node name
        "peers": [],                  # node names of other MeshScore bots (otherwise learned by listening)
    },
    # --- airtime ---
    "airtime": {
        "max_per_hour": 0,            # alert messages per hour across all channels (0 = no limit)
    },
}
CFG = {**DEFAULTS, "modules": {}, "same_air": dict(DEFAULTS["same_air"]), "airtime": dict(DEFAULTS["airtime"])}
TZ = None

# Starts of messages the bot itself (or another MeshScore bot) posts - never treated as commands.
SELF_MARKERS = ("🚨", "🛑", "🏁", "🏒", "⏸", "⏱", "⭐", "⚠️", "⚾", "🌧", "🏈")
# ALERT LEVELS - the same three in every sport:
#   max = everything            min = a result at the end of each period / every 3 innings, then the final            off = nothing
# (football, when it exists: min = the score at the end of each quarter + the final)
MODE_ALIASES = {"max": "max", "all": "max", "full": "max", "min": "min", "digest": "min", "goals": "min", "runs": "min",
                "finals": "min", "off": "off"}                       # the old words still work


def norm_mode(x):
    return MODE_ALIASES.get(str(x).strip().lower(), "max")


HOCKEY_KINDS = {
    "max": {"goal", "start", "intermission", "period", "final", "stars", "correction", "series"},
    "min": {"intermission", "period", "final", "stars", "series"},          # end of 1st / 2nd, OT starting, final
    "off": set(),
}
BASEBALL_KINDS = {
    "max": {"run", "start", "final", "extras", "delay", "correction"},
    "min": {"third", "extras", "delay", "final"},                           # after 3 and 6 innings, extras, delays, final
    "off": set(),
}
POST_KINDS = {
    "max": {"run", "start", "final", "series", "extras", "delay", "nohit", "correction", "heads"},
    "min": {"third", "final", "series", "extras", "delay", "nohit"},
    "off": set(),
}
LATE_STARS_WAIT = 45   # seconds between checks for the three stars, which are entered after the final

DIGEST_GRACE_HOURS = 1   # a daily digest is only sent during its own hour - a restart later in the day stays quiet
GOAL_LAG_WAIT = 8      # seconds to wait for a feed's goal list to catch up with its (earlier-updating) score
SUB_KINDS = {"goal", "final", "start", "correction"}  # what followers get by DM
CONFERENCE = {"East": "Eastern", "Central": "Eastern", "Midwest": "Western", "West": "Western"}


def setup_logging():
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252 and choke on the emoji
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in list(log.handlers):             # safe to call twice
        log.removeHandler(h)
    log.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if CFG["log_file"]:
        fh = RotatingFileHandler(CFG["log_file"], maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)


def init_tz():
    global TZ
    if not CFG.get("timezone"):
        TZ = None
        return
    try:
        TZ = ZoneInfo(CFG["timezone"])
    except Exception:
        TZ = None
        log.warning("Timezone %r unavailable (pip install tzdata); using system time", CFG["timezone"])


CLOCK = None      # tests: a callable returning the aware datetime to treat as "now" (None = the real clock)


def now():
    if CLOCK is not None:
        base = CLOCK()
        return base.astimezone(TZ) if TZ else base.astimezone()
    return datetime.now(TZ) if TZ else datetime.now().astimezone()


# ==========================================================================
# TEXT HELPERS
# ==========================================================================
def fits(text, limit=None):
    return len(text.encode("utf-8")) <= (limit or CFG["max_msg_bytes"])


def fit(text, limit=None):
    limit = limit or CFG["max_msg_bytes"]
    if fits(text, limit):
        return text
    return text.encode("utf-8")[: limit - 3].decode("utf-8", errors="ignore") + "..."


def pack(header, items, sep=" | ", max_msgs=None):
    """Pack items into as few <=limit messages as possible, each prefixed by header."""
    max_msgs = max_msgs or CFG["max_reply_msgs"]
    msgs, cur = [], []
    for it in items:
        if fits(f"{header} {sep.join(cur + [it])}"):
            cur.append(it)
            continue
        if cur:
            msgs.append(fit(f"{header} {sep.join(cur)}"))   # a lone over-long item is trimmed, never sent oversized
        cur = [it]
    if cur:
        msgs.append(fit(f"{header} {sep.join(cur)}"))
    return msgs[:max_msgs]


def start_first(evs):
    """A game first seen already underway with a score on the board (a goal landed before the first poll noticed
    it was live) should still announce the start before the score. Stable, so the rest keep their order."""
    return sorted(evs, key=lambda e: (e.kind if hasattr(e, "kind") else e[0]) != "start")


def fmt_time(dt):
    if not dt:
        return "TBD"
    s = dt.strftime("%I:%M%p").lstrip("0")
    return s[:-2] + s[-2].lower()  # 7:05p


def fmt_day(dt):
    return f"{dt.strftime('%a %b')} {dt.day}"


START_OVERDUE_HOURS = 3   # a game still "scheduled" this long after its start time is stuck, not about to begin


def scheduled_in(games):
    """Seconds until each not-yet-started game's scheduled start. A start time that has already passed (the feed
    has not flipped to live yet) counts as negative seconds, so callers keep polling fast through kickoff."""
    cutoff = now() - timedelta(hours=START_OVERDUE_HOURS)
    return [(g.start - now()).total_seconds() for g in games if g.scheduled and g.start and g.start > cutoff]


def when(g):
    if not g.start:
        return "TBD"
    d, t = g.start.date(), now().date()
    day = "Today" if d == t else "Tmrw" if d == t + timedelta(days=1) else g.start.strftime("%a")
    return f"{day} {fmt_time(g.start)}"


def fmt_uptime(sec):
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return f"{d}d{h}h" if d else f"{h}h{m}m" if h else f"{m}m"


# ==========================================================================
# PERSISTENT STATE
# ==========================================================================
class State:
    def __init__(self, path):
        self.path = path
        self.data = {"mode": norm_mode(CFG["default_mode"]), "subs": {}, "last_digest": ""}
        try:
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.data["mode"] = norm_mode(self.data.get("mode"))       # older state files hold the old words

    def save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("Could not save state: %s", e)


# ==========================================================================
# SEASON PHASES  (pre = preseason / spring training, reg = regular season, post = playoffs)
# ==========================================================================
PHASE_KEYS = ("pre", "reg", "post")
PHASE_LABEL = {"pre": "preseason", "reg": "regular season", "post": "playoffs", "cup": "Memorial Cup"}
PHASE_WORDS = {"pre": ("pre", "preseason", "pre-season", "spring", "exhibition"),
               "reg": ("reg", "regular", "season"),
               "post": ("post", "playoffs", "playoff", "postseason"),
               "cup": ("cup", "memorial", "memcup", "mc")}
PHASE_DEFAULT = {"pre": False, "reg": True, "post": True, "cup": True}


def _sports_file():
    try:
        with open(SPORTS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_sports_file(data):
    try:
        with open(SPORTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except OSError as e:
        log.error("Could not save %s: %s", SPORTS_FILE, e)


def get_phases(name, keys=PHASE_KEYS):
    """Which season phases a sport covers: config defaults, then what was last set in the menu / with !phase."""
    base = {**PHASE_DEFAULT, **(CFG.get("phases") or {}), **((CFG["modules"].get(name) or {}).get("phases") or {})}
    saved = (_sports_file().get("phases") or {}).get(name) or {}
    return {k: bool(saved.get(k, base[k])) for k in keys}


def save_phases(name, phases):
    data = _sports_file()
    data.setdefault("phases", {})[name] = {k: bool(v) for k, v in phases.items()}
    _write_sports_file(data)


def is_admin(inc):
    """May this sender change settings (!mode, !phase, !priority)? With neither admin_names nor admin_keys set,
    anyone may. Names can be spoofed on a public channel; admin_keys (public-key prefixes) are checked on DMs,
    where MeshCore authenticates the sender."""
    names = [x.lower() for x in CFG.get("admin_names") or []]
    keys = [x.lower() for x in CFG.get("admin_keys") or []]
    if not names and not keys:
        return True
    if inc.sender_name and inc.sender_name.lower() in names:
        return True
    return bool(inc.is_dm and inc.sender_key and any(inc.sender_key.lower().startswith(k) for k in keys))


def is_strict_admin(inc):
    """Like is_admin, but for commands that destroy progress (reset a game, switch a game): with no admin_names /
    admin_keys configured NOBODY is an admin, rather than everybody."""
    if not (CFG.get("admin_names") or CFG.get("admin_keys")):
        return False
    return is_admin(inc)


def admin_refusal(what, inc=None):
    """The reply to someone who may not use an admin-only command."""
    if not (CFG.get("admin_names") or CFG.get("admin_keys")):
        return "Admin commands are off: set admin_keys (or admin_names) in meshscore.toml [bot]"
    if inc is not None and not inc.is_dm and CFG.get("admin_keys") and not CFG.get("admin_names"):
        return f"Admin commands are DM-only: send me this as a direct message to {what}."
    return f"Only admins can {what}."


class PhaseMixin:
    """Lets a sport ignore preseason / regular season / playoff games. Subclasses define game_phase(g)."""
    has_phases = True
    phase_keys = PHASE_KEYS

    def _init_phases(self):
        self.phases = get_phases(self.name, self.phase_keys)

    def game_phase(self, g):
        return "reg"

    def phase_ok(self, g):
        return self.phases.get(self.game_phase(g), False)

    def phases_text(self):
        return " | ".join(f"{k} {'ON' if self.phases[k] else 'off'}" for k in self.phase_keys)

    async def cmd_phase(self, inc, arg):
        words = [w.lower() for w in inc.rest]
        if not words:
            return [f"[{self.tag} BOT] Games covered: {self.phases_text()}. Set: !phase {'|'.join(self.phase_keys)} on|off"]
        key = next((k for k in self.phase_keys if words[0] in PHASE_WORDS[k]), None)
        val = words[1] if len(words) > 1 else ""
        if not key or val not in ("on", "off"):
            return [f"[{self.tag} BOT] !phase {'|'.join(self.phase_keys)} on|off  (pre = preseason, reg = regular season, post = playoffs"
                    + (", cup = Memorial Cup)" if "cup" in self.phase_keys else ")")]
        if not is_admin(inc):
            return [f"[{self.tag} BOT] Only admins can change that."]
        self.phases[key] = val == "on"
        save_phases(self.name, self.phases)
        label = PHASE_LABEL[key][0].upper() + PHASE_LABEL[key][1:]
        return [fit(f"[{self.tag} BOT] {label} games {val.upper() if val == 'on' else 'off'}. {self.phases_text()}")]


class ModeMixin:
    """!mode max|min|off - the same three alert levels in every sport. Subclasses set kinds and min_blurb."""
    kinds = HOCKEY_KINDS
    min_blurb = "period results + final"

    def level(self):
        return norm_mode(self.state.data.get("mode"))

    def effective_level(self):
        return self.level()

    async def cmd_mode(self, inc, arg):
        cur = self.level()
        if not arg:
            note = f" (busy night: acting as {self.effective_level()})" if self.effective_level() != cur else ""
            return [fit(f"[{self.tag} BOT] Alert level: {cur}{note}. Set: !mode max|min|off. min = {self.min_blurb}")]
        a = MODE_ALIASES.get(arg.lower())
        if not a:
            return [fit(f"[{self.tag} BOT] !mode max (everything) | min ({self.min_blurb}) | off")]
        if not is_admin(inc):
            return [f"[{self.tag} BOT] Only admins can change the alert level."]
        self.state.data["mode"] = a
        self.state.save()
        return [fit(f"[{self.tag} BOT] Alert level set to {a}. min = {self.min_blurb}")]


# ---- shared helpers used by every league ----
session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/javascript, */*; q=0.01",
})
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=2, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))))


def _i(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes")


def ord_(n):
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def sched_days():
    """(start, end) dates to ask the schedule for; after midnight (until 6am) it also covers late games that began yesterday."""
    n = now()
    return (n.date() - timedelta(days=1 if n.hour < 6 else 0)).isoformat(), n.date().isoformat()


def tzinfo():
    """The configured time zone (None = system local time)."""
    return TZ
