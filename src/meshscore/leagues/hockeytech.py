"""HockeyTech leagues (OHL, and the CHL for the Memorial Cup): the data feed, game models and the league module."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import requests

from ..core import is_admin, CFG, CONFERENCE, DIGEST_GRACE_HOURS, HOCKEY_KINDS, LATE_STARS_WAIT, ModeMixin, PHASE_KEYS, PhaseMixin, SUB_KINDS, State, _i, fit, fmt_day, fmt_time, now, ord_, pack, session, start_first, truthy, tzinfo, when
from ..hub import Event

log = logging.getLogger("meshscore")


# ==========================================================================
# API + MODELS
# ==========================================================================
_BASE = "https://lscluster.hockeytech.com/feed/index.php?feed=modulekit"
def scorebar_url(back, ahead, client=None):
    return (f"{_BASE}&view=scorebar&numberofdaysback={back}&numberofdaysahead={ahead}"
            f"&client_code={client or CFG['client_code']}&key={CFG['api_key']}&fmt=json")


def summary_url(game_id):
    # the modulekit feed has no gamesummary view ("Undefined Tab"); the GameCenter feed (feed=gc) does.
    # Memorial Cup games carry ids like "chl:573" (they come from the CHL feed, not the OHL one).
    client, _, raw = str(game_id).rpartition(":")
    return ("https://lscluster.hockeytech.com/feed/index.php?feed=gc&tab=gamesummary"
            f"&game_id={raw}&client_code={client or CFG['client_code']}&key={CFG['api_key']}&lang_code=en&fmt=json")


def api_get(url):
    """Parsed JSON or None. Blocking - call via asyncio.to_thread."""
    try:
        res = session.get(url, timeout=10)
        if res.status_code == 200 and res.text.strip():
            return res.json()
        log.warning("API HTTP %s", res.status_code)
    except (requests.RequestException, ValueError) as e:
        log.warning("API error: %s", e)
    return None


@dataclass
class Side:
    code: str
    city: str = ""
    nick: str = ""
    long: str = ""
    div: str = ""
    w: int = 0
    rl: int = 0
    otl: int = 0
    sol: int = 0


@dataclass
class Game:
    id: str
    season: int
    start: Optional[datetime]
    hs: Side
    vs: Side
    hg: int
    ag: int
    code: int              # GameStatus: 1 scheduled, 2 live, 3 end of game, 4 final
    status: str
    status_long: str
    period: int
    period_name: str
    clock: str             # time REMAINING in the period
    intermission: bool
    game_type: str = ""
    letter: str = ""
    client: str = ""       # "chl" for Memorial Cup games (other CHL leagues' teams too); "" = OHL

    cup = property(lambda s: s.client == "chl")
    home = property(lambda s: s.hs.code)
    away = property(lambda s: s.vs.code)
    # HockeyTech flips a game to status 2 (live) several minutes before puck drop, while the clock still reads a
    # full 20:00. Treat that as pregame until the clock actually moves (or something happens).
    pregame = property(lambda s: s.code == 2 and s.period == 1 and s.clock in ("20:00", "20:00.0")
                       and s.hg + s.ag == 0 and not s.intermission)
    live = property(lambda s: s.code == 2 and not s.pregame)
    # The feed's own Intermission flag stays 0 all night. A period has ended when the clock hits 00:00.
    period_end = property(lambda s: s.code == 2 and (s.intermission or (s.clock == "00:00" and s.period in (1, 2))))
    final = property(lambda s: s.code in (3, 4))
    scheduled = property(lambda s: s.code in (0, 1) or s.pregame)
    state_code = property(lambda s: 1 if s.pregame else s.code)
    playoff = property(lambda s: bool(s.letter) or "playoff" in s.game_type.lower())


def parse_game(g, client=""):
    start = None
    iso = g.get("GameDateISO8601")
    if iso:
        try:
            start = datetime.fromisoformat(iso).astimezone(tzinfo())
        except ValueError:
            pass

    def side(p, div_key):
        return Side(
            code=str(g.get(f"{p}Code") or "?"), city=str(g.get(f"{p}City") or ""),
            nick=str(g.get(f"{p}Nickname") or ""), long=str(g.get(f"{p}LongName") or ""),
            div=str(g.get(div_key) or "").replace(" Division", "").strip(),
            w=_i(g.get(f"{p}Wins")), rl=_i(g.get(f"{p}RegulationLosses")),
            otl=_i(g.get(f"{p}OTLosses")), sol=_i(g.get(f"{p}ShootoutLosses")),
        )

    return Game(
        id=(f"{client}:" if client else "") + str(g.get("ID") or ""), season=_i(g.get("SeasonID")), start=start,
        hs=side("Home", "HomeDivision"), vs=side("Visitor", "VisitingDivision"),
        hg=_i(g.get("HomeGoals")), ag=_i(g.get("VisitorGoals")),
        code=_i(g.get("GameStatus"), 1), status=str(g.get("GameStatusString") or ""),
        status_long=str(g.get("GameStatusStringLong") or ""),
        period=_i(g.get("Period"), 1), period_name=str(g.get("PeriodNameLong") or g.get("PeriodNameShort") or ""),
        clock=str(g.get("GameClock") or ""), intermission=str(g.get("Intermission")) == "1",
        game_type=str(g.get("game_type") or ""), letter=str(g.get("game_letter") or ""), client=client,
    )


def extract_games(data, client=""):
    if data is None:
        return None
    sk = data.get("SiteKit", {})
    if "Undefined" in sk:
        log.error("API says: %s", sk["Undefined"])
    raw = sk.get("Scorebar") or sk.get("Scoreboard") or []
    if isinstance(raw, dict):
        raw = raw.get("games") or list(raw.values())
    games = [parse_game(g, client) for g in raw if isinstance(g, dict)]
    games.sort(key=lambda g: g.start or datetime.max.replace(tzinfo=tzinfo()))
    return games


def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def player_name(p):
    if isinstance(p, dict):
        fn, ln = str(p.get("first_name") or ""), str(p.get("last_name") or "")
        if ln:
            return f"{fn[:1]}. {ln}" if fn else ln
        return str(p.get("name") or "")
    return str(p or "")


def parse_goals(data):
    raw = find_key(data, "goals")
    periods = find_key(data, "periods")
    out = []
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, dict):
                sc = x.get("goal_scorer") or x.get("scorer") or x.get("player") or {}
                out.append({"scorer": player_name(sc), "pp": truthy(x.get("power_play")),
                            "sh": truthy(x.get("short_handed")), "en": truthy(x.get("empty_net")),
                            "when": goal_clock(x, periods)})
    return out


def goal_clock(goal, periods):
    """'2nd 09:14' (time remaining, like the scoreboard) from a goal record's elapsed time; '' if unknown."""
    try:
        pid = str(goal.get("period_id", ""))
        per = periods.get(pid, {}) if isinstance(periods, dict) else {}
        mm, ss = str(goal.get("time", "")).split(":")
        length = _i(per.get("length"), 1200 if pid in ("1", "2", "3") else 300)
        rem = max(length - (int(mm) * 60 + int(ss)), 0)
        label = per.get("long_name") or {"1": "1st", "2": "2nd", "3": "3rd"}.get(pid, "OT")
        return f"{label} {rem // 60:02d}:{rem % 60:02d}"
    except (ValueError, AttributeError, TypeError):
        return ""


def parse_shots(data):
    ts = find_key(data, "totalShots")
    if isinstance(ts, dict):
        h, v = ts.get("home"), ts.get("visiting") or ts.get("visitor") or ts.get("away")
        if h is not None and v is not None:
            return _i(v), _i(h)  # (away, home)
    return None


def parse_stars(data):
    raw = find_key(data, "mvps") or find_key(data, "mostValuablePlayers") or find_key(data, "three_stars") or find_key(data, "threeStars")
    names = []
    if isinstance(raw, list):
        for x in raw[:3]:
            if isinstance(x, dict):
                n = player_name(x.get("player") or x)
                if n:
                    names.append(n)
    return names


# ---- standings / teams (computed from scorebar records; no extra endpoint needed) ----
@dataclass
class Team:
    code: str
    city: str
    nick: str
    long: str
    div: str
    w: int
    rl: int
    otl: int
    sol: int

    gp = property(lambda t: t.w + t.rl + t.otl + t.sol)
    ol = property(lambda t: t.otl + t.sol)
    pts = property(lambda t: 2 * t.w + t.otl + t.sol)
    conf = property(lambda t: CONFERENCE.get(t.div, ""))


def build_teams(games):
    if not games:
        return {}
    regular = [g for g in games if not g.playoff]
    season = max((g.season for g in (regular or games)))
    teams = {}
    for g in games:  # records are a current snapshot, identical across a team's games
        if g.season != season:
            continue
        for s in (g.hs, g.vs):
            teams[s.code] = Team(s.code, s.city, s.nick, s.long, s.div, s.w, s.rl, s.otl, s.sol)
    return teams


def rank_sort(teams):
    return sorted(teams, key=lambda t: (-t.pts, t.gp, -t.w, t.code))


def find_team(q, teams):
    q = q.strip().lower().replace("'", "")
    for code, t in teams.items():
        if code.lower() == q:
            return t
    hits = [t for t in teams.values()
            if any(f.lower().replace("'", "").startswith(q) for f in (t.code, t.city, t.nick, t.long))
            or q in t.long.lower().replace("'", "").split()]
    return hits[0] if len(hits) == 1 else None


def series_tag(g, allg):
    if g.cup:
        return "Memorial Cup"
    if not g.playoff:
        return ""
    pair = {g.home, g.away}
    ser = sorted((x for x in allg if x.season == g.season and x.playoff and {x.home, x.away} == pair),
                 key=lambda x: x.start or now())
    wins = {g.home: 0, g.away: 0}
    for x in ser:
        if x.final and x.hg != x.ag:
            wins[x.home if x.hg > x.ag else x.away] += 1
    n = next((i + 1 for i, x in enumerate(ser) if x.id == g.id), len(ser))
    a, b = wins[g.away], wins[g.home]
    lead = f"{g.away} leads {a}-{b}" if a > b else f"{g.home} leads {b}-{a}" if b > a else f"tied {a}-{b}"
    return f"Gm{n} {lead}"


def ot_tag(g):
    words = g.status_long.upper().split()
    if "SO" in words:
        return "/SO"
    if "OT" in words:
        return "/OT"
    if g.final and g.period_name.upper() in ("OT", "SO"):
        return "/" + g.period_name.upper()
    return ""


def game_line(g):
    pre = "MC " if g.cup else ""
    if g.final:
        return f"{pre}{g.away} {g.ag} @ {g.home} {g.hg} F{ot_tag(g)}"
    if g.live:
        if g.period_end:
            return f"{pre}{g.away} {g.ag}-{g.hg} {g.home} End {g.period_name}"
        return f"{pre}{g.away} {g.ag}-{g.hg} {g.home} {g.period_name} {g.clock}"
    if g.scheduled:
        return f"{pre}{g.away}@{g.home} {fmt_time(g.start)}"
    return f"{pre}{g.away}@{g.home} {g.status}"


def period_ended(g, prev):
    """1 or 2 when that period has just ended, else 0. The scoreboard can lag by minutes and jump straight from late in
    a period to the next one without ever showing 00:00, so the period number moving on counts as well."""
    if not g.live:
        return 0
    if g.period_end and g.period in (1, 2):
        p = g.period
    elif g.period > prev["period"] and prev["period"] in (1, 2):
        p = min(g.period - 1, 2)
    else:
        return 0
    return p if p > prev.get("ended", 0) else 0


# ==========================================================================
# SCORE STORE (cached so commands don't hammer the API)
# ==========================================================================
class Scores:
    def __init__(self):
        self.live, self.full = [], []
        self.live_at = self.full_at = self.ok_at = 0.0

    async def _fetch(self, back, ahead):
        games = extract_games(await asyncio.to_thread(api_get, scorebar_url(back, ahead)))
        if games is not None:
            self.ok_at = time.time()
        return games

    async def refresh_live(self, max_age=25):
        if time.time() - self.live_at >= max_age:
            g = await self._fetch(1, 1)
            if g is None:
                return None
            self.live, self.live_at = g, time.time()
        return self.live

    async def refresh_full(self, max_age=600):
        if time.time() - self.full_at >= max_age:
            g = await self._fetch(CFG["full_days_back"], CFG["full_days_ahead"])
            if g is not None:
                self.full, self.full_at = g, time.time()
        return self.full

    def all_games(self):
        merged = {g.id: g for g in self.full}
        merged.update({g.id: g for g in self.live})
        return sorted(merged.values(), key=lambda g: g.start or now())

    def today(self, games=None):
        d = now().date()
        return [g for g in (games if games is not None else self.all_games()) if g.start and g.start.date() == d]


OHL_PHASES = {}          # HockeyTech season id -> "pre" | "reg" | "post" | "other"
OHL_PHASES_AT = 0.0
CUP_SEASONS = {}         # CHL season id -> (start date, end date) of each Memorial Cup


def cup_active():
    """True from a day before to a day after a Memorial Cup (the CHL feed is only asked then)."""
    today = now().date()
    for start, end in CUP_SEASONS.values():
        try:
            if datetime.fromisoformat(start).date() - timedelta(days=1) <= today <= datetime.fromisoformat(end).date() + timedelta(days=1):
                return True
        except ValueError:
            continue
    return False


def active_cup_seasons():
    today = now().date()
    out = set()
    for sid, (start, end) in CUP_SEASONS.items():
        try:
            if datetime.fromisoformat(start).date() - timedelta(days=1) <= today <= datetime.fromisoformat(end).date() + timedelta(days=1):
                out.add(sid)
        except ValueError:
            continue
    return out


def load_ohl_seasons():
    """Which season id is preseason / regular season / playoffs, from the league's season list (blocking)."""
    global OHL_PHASES_AT
    data = api_get(f"{_BASE}&view=seasons&client_code={CFG['client_code']}&key={CFG['api_key']}&fmt=json")
    seasons = (((data or {}).get("SiteKit") or {}).get("Seasons")) or []
    for x in seasons:
        name = str(x.get("season_name", "")).lower()
        OHL_PHASES[_i(x.get("season_id"))] = ("post" if str(x.get("playoff")) == "1" else "pre" if re.search(r"\bpre", name)
                                              else "reg" if str(x.get("career")) == "1" else "other")
    if seasons:
        OHL_PHASES_AT = time.time()
    chl = api_get(f"{_BASE}&view=seasons&client_code=chl&key={CFG['api_key']}&fmt=json")
    for x in (((chl or {}).get("SiteKit") or {}).get("Seasons")) or []:
        if "memorial cup" in str(x.get("season_name", "")).lower() and x.get("start_date") and x.get("end_date"):
            CUP_SEASONS[_i(x.get("season_id"))] = (str(x["start_date"]), str(x["end_date"]))


class HockeyTechModule(PhaseMixin, ModeMixin):
    """A HockeyTech-powered league (OHL by default) in one channel.
    Config: team = "KIT" follows one team's games (empty = the whole league), priority_teams = [...] gets full alerts
    even on busy nights, tag = text shown in messages (default "OHL")."""
    type_name, label = "hockeytech", "OHL hockey"
    phase_keys = PHASE_KEYS + ("cup",)      # cup = the Memorial Cup (every game, including the WHL and QMJHL teams)
    kinds, min_blurb = HOCKEY_KINDS, "period results + final"
    arg_cmds = ("!team", "!next", "!standings", "!follow")

    def __init__(self, hub, name="ohl"):
        self.hub, self.name = hub, name
        cfg = CFG["modules"][name]
        self.cfg = cfg
        self.channel = cfg["channel"]
        self.tag = str(cfg.get("tag") or "OHL")
        self.blurb = "Scoreboard"
        self.digest_hour = int(cfg.get("digest_hour", 12))
        self.team = str(cfg.get("team") or "").upper()          # "" = every team in the league
        self.scores = Scores()
        self.state = State(f"{name}_state.json")
        self._init_phases()
        self.gstate = {}
        self._bg = set()
        self.cup, self.cup_at, self.cup_all, self.cup_all_at = [], 0.0, [], 0.0
        saved = self.state.data.get("priority")
        self.priority = [str(c).upper() for c in (saved if isinstance(saved, list) else cfg.get("priority_teams") or ([self.team] if self.team else []))]
        self.busy, self.busy_announced = False, ""
        self.commands = {
            "!scores": self.cmd_scores, "!schedule": self.cmd_scores, "!tomorrow": self.cmd_tomorrow,
            "!cup": self.cmd_cup, "!memorialcup": self.cmd_cup, "!priority": self.cmd_priority,
            "!team": self.cmd_team, "!next": self.cmd_next, "!standings": self.cmd_standings,
            "!follow": self.cmd_follow, "!unfollow": self.cmd_unfollow, "!following": self.cmd_following,
            "!mode": self.cmd_mode, "!ping": self.cmd_ping, "!botstatus": self.cmd_botstatus,
            "!help": self.cmd_help, "!phase": self.cmd_phase,
        }

    def in_scope(self, g):
        """With a configured team, only its games raise alerts (commands still cover the whole league)."""
        return not self.team or self.team in (g.home, g.away)

    def is_priority(self, g):
        return bool(self.priority) and (g.home in self.priority or g.away in self.priority)

    async def cmd_priority(self, inc, arg):
        words = [w.upper() for w in re.split(r"[\s,]+", " ".join(inc.rest)) if w]
        if not words:
            return [fit(f"[{self.tag} BOT] Priority teams: {', '.join(self.priority) or 'none'} - their games always get full alerts. "
                        f"!priority KIT GUE | !priority off")]
        if not is_admin(inc):
            return [f"[{self.tag} BOT] Only admins can change the priority teams."]
        if words[0] in ("OFF", "NONE", "CLEAR"):
            codes = []
        else:
            await self.scores.refresh_live()
            await self.scores.refresh_full()
            teams = build_teams(self.scores.all_games())
            found = [find_team(w, teams) for w in words]
            if not all(found):
                bad = ", ".join(w for w, f in zip(words, found) if not f)
                return pack(f"[{self.tag} BOT] Unknown team ({bad}). Codes:", sorted(teams), sep=" ")
            codes = list(dict.fromkeys(t.code for t in found))
        self.priority = codes
        self.state.data["priority"] = codes
        self.state.save()
        return [fit(f"[{self.tag} BOT] Priority teams: {', '.join(codes) or 'none'}" + (" - their games always get full alerts." if codes else "."))]

    def game_phase(self, g):
        if g.cup:
            return "cup"
        return OHL_PHASES.get(g.season) or ("post" if g.playoff else "reg")

    async def cup_games(self, max_age=25):
        """Memorial Cup games (CHL feed): only fetched while a Memorial Cup is on and the "cup" part is switched on."""
        if not self.phases.get("cup", True) or not cup_active():
            return []
        if time.time() - self.cup_at >= max_age:
            games = extract_games(await asyncio.to_thread(api_get, scorebar_url(1, 1, "chl")), "chl")
            if games is not None:
                ids = active_cup_seasons()
                self.cup, self.cup_at = [g for g in games if g.season in ids], time.time()
        return self.cup

    async def cmd_cup(self, inc, arg):
        if not CUP_SEASONS:
            await asyncio.to_thread(load_ohl_seasons)
        if not cup_active():
            nxt = sorted((v for v in CUP_SEASONS.values() if v[0] > now().date().isoformat()), key=lambda v: v[0])
            when_ = ""
            if nxt:
                a, b = datetime.fromisoformat(nxt[0][0]), datetime.fromisoformat(nxt[0][1])
                when_ = f" Next: {a.strftime('%b')} {a.day} - {b.strftime('%b')} {b.day}."
            return [f"[MC] The Memorial Cup isn't on right now.{when_}"]
        if time.time() - self.cup_all_at >= 300:
            games = extract_games(await asyncio.to_thread(api_get, scorebar_url(16, 16, "chl")), "chl")
            if games is not None:
                ids = active_cup_seasons()
                self.cup_all, self.cup_all_at = [g for g in games if g.season in ids], time.time()
        games = self.cup_all
        if not games:
            return ["[MC] No Memorial Cup games found yet."]
        rec = {}
        for g in games:
            if g.final and g.ag != g.hg:
                w, l = (g.home, g.away) if g.hg > g.ag else (g.away, g.home)
                rec.setdefault(w, [0, 0])[0] += 1
                rec.setdefault(l, [0, 0])[1] += 1
        out = []
        if rec:
            out += pack("[MC]", [f"{c} {w}-{l}" for c, (w, l) in sorted(rec.items(), key=lambda kv: (-kv[1][0], kv[1][1], kv[0]))], max_msgs=1)
        live = [f"{g.away} {g.ag}-{g.hg} {g.home} {g.period_name}" for g in games if g.live]
        last = [f"{g.away} {g.ag}-{g.hg} {g.home}{ot_tag(g)}" for g in games if g.final][-2:]
        nxt = [f"next {g.away}@{g.home} {when(g)}" for g in games if g.scheduled and g.start and g.start > now()][:2]
        items = live + last + nxt
        if items:
            out += pack("[MC]", items, max_msgs=1)
        return out[: CFG["max_reply_msgs"]] or ["[MC] No Memorial Cup games found yet."]

    @property
    def t(self):
        return self.hub.t

    def status_extra(self):
        return f" | {len(self.state.data['subs'])} subs" + (" | BUSY" if self.busy else "")

    async def run(self):
        n = now()
        if not self.state.data["last_digest"] and n.hour >= self.digest_hour + DIGEST_GRACE_HOURS:
            self.state.data["last_digest"] = n.date().isoformat()
        await self.poll_loop()

    # ---------------- sending ----------------
    def say_chan(self, texts):
        self.hub.say_chan(self.channel, texts)

    def say_dm(self, key, text):
        self.hub.say_dm(key, text)

    # ---------------- commands ----------------
    async def _games(self):
        await self.scores.refresh_live()
        return self.scores.all_games()

    async def cmd_scores(self, inc, arg):
        allg = await self._games()
        allg = allg + await self.cup_games()
        games = sorted(self.scores.today(allg), key=lambda g: not self.is_priority(g))
        if not games:
            nxt = [g for g in allg if g.scheduled and g.start and g.start > now()]
            if not nxt:
                return [f"[{self.tag}] No games today."]
            d = nxt[0].start.date()
            n = sum(1 for g in nxt if g.start.date() == d)
            return [f"[{self.tag}] No games today. Next: {fmt_day(nxt[0].start)} ({n} game{'s' if n > 1 else ''})"]
        return pack(f"[{self.tag}]", [game_line(g) for g in games])

    async def cmd_tomorrow(self, inc, arg):
        allg = await self._games()
        d = now().date() + timedelta(days=1)
        games = [g for g in allg if g.start and g.start.date() == d and self.phase_ok(g)]
        if not games:
            return [f"[{self.tag} TMRW] No games tomorrow."]
        return pack(f"[{self.tag} TMRW]", [f"{g.away}@{g.home} {fmt_time(g.start)}" for g in games])

    def _team_ctx(self, arg):
        allg = self.scores.all_games()
        teams = build_teams(allg)
        return allg, teams, find_team(arg, teams) if arg else None

    async def cmd_team(self, inc, arg):
        if not arg:
            return [f"[{self.tag} BOT] Usage: !team CODE (e.g. !team KIT)"]
        await self.scores.refresh_live()
        await self.scores.refresh_full()
        allg, teams, t = self._team_ctx(arg)
        if not t:
            return pack(f"[{self.tag}] Unknown team. Codes:", sorted(teams), sep=" ")
        g = next((x for x in self.scores.today(allg) if t.code in (x.home, x.away)), None)
        ser = f" | {series_tag(g, allg)}" if g and series_tag(g, allg) else ""
        if g and g.final:
            line = f"[{t.code} FINAL{ot_tag(g)}] {g.away} {g.ag}-{g.hg} {g.home}{ser}"
        elif g and g.live:
            line = f"[{t.code} LIVE] {g.away} {g.ag}-{g.hg} {g.home} | {g.period_name} {g.clock}{ser}"
        elif g:
            line = f"[{t.code} TODAY] {g.away}@{g.home} {fmt_time(g.start)}{ser}"
        else:
            nxt = [x for x in self._next_games(allg, t.code, 40) if self.phase_ok(x)][:1]
            line = f"[{t.code}] No game today." + (f" Next: {self._opp(nxt[0], t.code)}" if nxt else "")
        div_rank = rank_sort([x for x in teams.values() if x.div == t.div]).index(t) + 1
        conf_rank = rank_sort([x for x in teams.values() if x.conf == t.conf]).index(t) + 1
        rec = f"[{t.code}] {t.w}-{t.rl}-{t.ol} {t.pts}pts | #{div_rank} {t.div}, #{conf_rank} {t.conf}"
        return [line, rec]

    @staticmethod
    def _next_games(allg, code, n):
        return [g for g in allg if g.scheduled and g.start and g.start > now() and code in (g.home, g.away)][:n]

    @staticmethod
    def _opp(g, code):
        return f"vs {g.away} {when(g)}" if g.home == code else f"@ {g.home} {when(g)}"

    async def cmd_next(self, inc, arg):
        if not arg:
            return [f"[{self.tag} BOT] Usage: !next CODE (e.g. !next KIT)"]
        await self.scores.refresh_live()
        await self.scores.refresh_full()
        allg, teams, t = self._team_ctx(arg)
        if not t:
            return pack(f"[{self.tag}] Unknown team. Codes:", sorted(teams), sep=" ")
        nxt = [x for x in self._next_games(allg, t.code, 40) if self.phase_ok(x)][:3]
        if not nxt:
            return [f"[{t.code}] No upcoming games found."]
        return pack(f"[{t.code} NEXT]", [self._opp(g, t.code) for g in nxt])

    async def cmd_standings(self, inc, arg):
        await self.scores.refresh_full()
        teams = build_teams(self.scores.all_games())
        if not teams:
            return [f"[{self.tag} STANDINGS] Data unavailable."]
        a = arg.lower()
        divs = {t.div.lower(): t.div for t in teams.values()}
        if a in ("", "league", "all"):
            rows, head = rank_sort(teams.values())[:8], f"[{self.tag}]"
        elif a in divs:
            rows, head = rank_sort([t for t in teams.values() if t.div == divs[a]]), f"[{self.tag} {divs[a]}]"
        elif a in ("eastern", "western"):
            rows = rank_sort([t for t in teams.values() if t.conf.lower() == a])
            head = f"[{self.tag} {a.title()}]"
        else:
            return [f"[{self.tag}] !standings [east|central|midwest|west|eastern|western]"]
        return pack(head, [f"{t.code} {t.pts}pt {t.w}-{t.rl}-{t.ol}" for t in rows])

    def _identify(self, inc):
        if inc.is_dm and inc.sender_key:
            return inc.sender_key.lower()
        c = self.t.find_contact(name=inc.sender_name) if inc.sender_name else None
        return c["public_key"] if c else None

    async def cmd_follow(self, inc, arg):
        if not arg:
            return [f"[{self.tag} BOT] Usage: !follow CODE (e.g. !follow KIT). Alerts arrive by DM."]
        await self.scores.refresh_live()
        await self.scores.refresh_full()
        allg, teams, t = self._team_ctx(arg)
        if not t:
            return pack(f"[{self.tag}] Unknown team. Codes:", sorted(teams), sep=" ")
        key = self._identify(inc)
        if not key:
            return [f"[{self.tag} BOT] I can't find you in my contacts. DM me !follow " + t.code + " (send an advert first)."]
        sub = self.state.data["subs"].setdefault(key, {"name": inc.sender_name, "teams": []})
        sub["name"] = inc.sender_name or sub.get("name", "")
        if t.code in sub["teams"]:
            return [f"[{self.tag} BOT] Already following {t.code}."]
        if len(sub["teams"]) >= CFG["max_follows"]:
            return [f"[{self.tag} BOT] Max {CFG['max_follows']} teams. !unfollow one first."]
        sub["teams"].append(t.code)
        self.state.save()
        return [f"[{self.tag} BOT] Following {t.code}: goals & finals by DM."]

    async def cmd_unfollow(self, inc, arg):
        key = self._identify(inc)
        sub = self.state.data["subs"].get(key or "")
        if not sub:
            return [f"[{self.tag} BOT] You're not following anything."]
        if arg:
            code = arg.upper()
            if code in sub["teams"]:
                sub["teams"].remove(code)
        else:
            sub["teams"] = []
        if not sub["teams"]:
            del self.state.data["subs"][key]
        self.state.save()
        return [f"[{self.tag} BOT] Updated. " + (f"Following: {', '.join(sub['teams'])}" if sub["teams"] else "No follows left.")]

    async def cmd_following(self, inc, arg):
        sub = self.state.data["subs"].get(self._identify(inc) or "")
        return [f"[{self.tag} BOT] Following: {', '.join(sub['teams'])}" if sub else f"[{self.tag} BOT] Not following any team. !follow CODE"]

    async def cmd_ping(self, inc, arg):
        return await self.hub.cmd_ping(self, inc)

    async def cmd_botstatus(self, inc, arg):
        return await self.hub.status(self, self.scores.ok_at)

    async def cmd_help(self, inc, arg):
        return [f"[{self.tag} BOT] !scores !tomorrow !team X !next X !standings [div] !cup !follow X !mode !phase !priority !ping"]

    # ---------------- events -> messages ----------------
    def effective_level(self):
        return "min" if self.level() == "max" and self.busy else self.level()

    def emit(self, ev):
        lvl = self.level()
        # Memorial Cup games are never thinned out on a busy night; priority teams' games get full alerts even at "min"
        eff = "max" if (ev.cup and lvl == "max") or (ev.prio and lvl in ("max", "min")) else self.effective_level()
        broadcast = ev.kind in self.kinds.get(eff, set())
        if broadcast:
            self.say_chan(ev.texts)
        if ev.kind in SUB_KINDS and (not broadcast or CFG["dm_even_if_broadcast"]):
            for key, sub in self.state.data["subs"].items():
                if set(sub["teams"]) & set(ev.teams):
                    self.say_dm(key, ev.texts[0])

    def update_busy(self, games):
        """Busy night: enough games live at once. Enter at N live, leave when N-2 or fewer are left."""
        live, n = sum(1 for g in games if g.live and not g.cup and not self.is_priority(g)), int(CFG["busy_live_games"])
        if not self.busy and n > 0 and live >= n:
            self.busy = True
            today = now().date().isoformat()
            if self.level() == "max" and self.busy_announced != today:
                self.busy_announced = today
                self.say_chan(f"⚡ Busy night: {live} games live. Period results + finals only until it quiets down"
                              + (f" ({'/'.join(self.priority)} stay full)" if self.priority else ""))
        elif self.busy and live <= max(n - 2, 0):
            self.busy = False

    async def summary(self, gid):
        return await asyncio.to_thread(api_get, summary_url(gid))

    def _late_stars(self, g):
        """The scorekeeper enters the three stars one at a time after the final; wait for all 3, don't post a partial list."""
        async def job():
            try:
                for _ in range(14):
                    await asyncio.sleep(LATE_STARS_WAIT)
                    stars = parse_stars(await self.summary(g.id))
                    if len(stars) == 3:
                        self.emit(Event("stars", (g.away, g.home), ["⭐ " + ", ".join(stars)], cup=g.cup))
                        return
            except Exception:
                log.exception("late stars")
        task = asyncio.create_task(job())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def goal_info(self, g):
        total = g.ag + g.hg
        goals = []
        for attempt in (0, 1):
            goals = parse_goals(await self.summary(g.id))
            if len(goals) == total:
                break
            if attempt == 0:
                await asyncio.sleep(8)  # scorekeeper may still be entering the goal
        if len(goals) != total or not goals:
            return None
        last = goals[-1]
        count = sum(1 for x in goals if last["scorer"] and x["scorer"] == last["scorer"])
        return {**last, "count": count}

    async def game_events(self, g, prev, allg, early=False):
        teams = (g.away, g.home)
        score = f"{g.away} {g.ag}-{g.hg} {g.home}"
        clock = f"{g.period_name} {g.clock}".strip()
        evs = []
        d_a, d_h = g.ag - prev["ag"], g.hg - prev["hg"]

        if d_a < 0 or d_h < 0:
            evs.append(Event("correction", teams, [f"⚠️ Score update: {score} | {clock}"]))
        elif (d_a > 0 or d_h > 0) and g.period_name != "SO":     # the shootout winner's +1 is the final's news, not a goal
            n = d_a + d_h
            if n == 1:
                scorer_team = g.away if d_a else g.home
                info = await self.goal_info(g) if CFG["summary_fetch"] else None
                tags, who = "", ""
                if info:
                    clock = info.get("when") or clock
                    who = f" {info['scorer']}" if info["scorer"] else ""
                    tags = " (PP)" if info["pp"] else " (SH)" if info["sh"] else ""
                    tags += " (EN)" if info["en"] else ""
                    if info["count"] == 3:
                        tags += " 🔥HAT TRICK!"
                    elif info["count"] > 3:
                        tags += f" ({info['count']} goals)"
                evs.append(Event("goal", teams, [f"🚨 {scorer_team} GOAL{tags}{who} | {score} | {clock}"]))
            else:
                evs.append(Event("goal", teams, [f"🚨 {n} goals | {score} | {clock}"]))

        if prev["code"] in (0, 1) and g.state_code == 2:
            ser = series_tag(g, allg)
            evs.append(Event("start", teams, [f"🏒 Puck drop: {g.away} @ {g.home}" + (f" | {ser}" if ser else "")]))
        elif period_ended(g, prev):
            text = f"⏸ End of {ord_(period_ended(g, prev))}: {score}"
            if CFG["summary_fetch"]:
                shots = parse_shots(await self.summary(g.id))
                if shots:
                    text += f" | SOG {shots[0]}-{shots[1]}"
            evs.append(Event("intermission", teams, [text]))
        elif g.live and g.period >= 4 and prev["period"] < 4:
            evs.append(Event("period", teams, [f"⏱ {g.period_name}: {score}"]))

        # A hockey game can never really end tied (OT/SO always decides it); the feed briefly reports the tied
        # regulation score as "Final" right as it hands off to OT, before flipping back to live. Never trust that.
        was_sent, sent_score = prev.get("final_sent", False), prev.get("final_score")
        corrected = was_sent and g.final and g.ag != g.hg and sent_score != (g.ag, g.hg)
        if g.ag != g.hg and ((g.final and prev["code"] not in (3, 4) and not was_sent) or early or corrected):
            text = f"🛑 FINAL{ot_tag(g)}{' (corrected)' if corrected else ''}: {g.away} {g.ag} @ {g.home} {g.hg}"
            texts = []
            # Three stars: the OHL feed fills the 3 slots in one by one after the final, sometimes minutes apart, so
            # a read mid-way can have just 1 or 2 - never show fewer than 3. Also, to keep this reliable, only chase
            # them down for the games most worth it: priority teams and the Memorial Cup; everyone else gets the score.
            want_stars = self.is_priority(g) or g.cup
            if CFG["summary_fetch"]:
                data = await self.summary(g.id)
                shots = parse_shots(data)
                if shots:
                    text += f" | SOG {shots[0]}-{shots[1]}"
                if want_stars:
                    stars = parse_stars(data)
                    if len(stars) == 3:
                        texts.append("⭐ " + ", ".join(stars))
                    else:
                        self._late_stars(g)
            ser = series_tag(g, allg)
            if ser:
                text += f" | {ser}"
            evs.append(Event("final", teams, [text] + texts))
        return start_first(evs)

    async def process_games(self, games):
        games = [g for g in games if self.phase_ok(g) and self.in_scope(g)]     # skip preseason / regular season / playoff games that are switched off
        allg = self.scores.all_games()
        seen = set()
        self.update_busy(games)
        for g in games:
            seen.add(g.id)
            cur = {"ag": g.ag, "hg": g.hg, "code": g.state_code, "inter": g.period_end, "period": g.period}
            prev = self.gstate.get(g.id)
            if prev is not None and not prev.get("suspect") and (g.ag < prev["ag"] or g.hg < prev["hg"] or g.period < prev["period"]):
                # The scoring feed sometimes returns one stale copy (an older score/period). Only trust it if it repeats.
                prev["suspect"] = True
                log.info("Ignoring a one-off stale OHL update for game %s", g.id)
                continue
            # First sighting (bot just started): a period that is ALREADY over is history - mark it announced, or the next
            # poll would post a stale "End of 1st".
            first_ended = g.period if g.live and g.period_end and g.period in (1, 2) else 0
            cur["ended"] = max((prev or {}).get("ended", 0), period_ended(g, prev) if prev else first_ended)
            # The feed can leave a finished game at 00:00 for many minutes before it is closed (seen live: ~20 min).
            # Regulation or OT over at 00:00 with a winner = the game is over; announce it once the score has held.
            over = g.live and g.period >= 3 and g.clock == "00:00" and g.ag != g.hg
            cur["ending"] = ((prev or {}).get("ending") or time.time()) if over else None
            cur["final_sent"] = (prev or {}).get("final_sent", False)
            cur["final_score"] = (prev or {}).get("final_score")
            self.gstate[g.id] = cur
            if prev is None:
                continue  # first sighting: baseline only
            early = bool(cur["ending"]) and time.time() - cur["ending"] >= CFG["early_final_seconds"] and not cur["final_sent"]
            if early:
                log.info("OHL game %s: 00:00 with a winner for %ds and the feed is still live - announcing the final",
                         g.id, int(time.time() - cur["ending"]))
            for ev in await self.game_events(g, prev, allg, early=early):
                ev.cup, ev.prio = g.cup, self.is_priority(g)
                if ev.kind == "final":
                    cur["final_sent"], cur["final_score"] = True, (g.ag, g.hg)
                self.emit(ev)
        for gid in set(self.gstate) - seen:
            del self.gstate[gid]

    # ---------------- daily digest ----------------
    async def check_digest(self):
        n = now()
        today = n.date().isoformat()
        h = self.digest_hour
        if n.hour < h or self.state.data["last_digest"] == today:
            return
        if n.hour >= h + DIGEST_GRACE_HOURS or self.level() == "off":  # too late (or muted): skip today
            self.state.data["last_digest"] = today
            self.state.save()
            return
        games = await self.scores.refresh_live(0)
        if games is None:
            return
        games = sorted(games + await self.cup_games(0), key=lambda g: not self.is_priority(g))
        yday = n.date() - timedelta(days=1)
        recap = [("MC " if g.cup else "") + f"{g.away} {g.ag}-{g.hg} {g.home}{ot_tag(g)}" for g in games
                 if g.final and g.start and g.start.date() == yday and self.phase_ok(g) and self.in_scope(g)]
        todays = [("MC " if g.cup else "") + f"{g.away}@{g.home} {fmt_time(g.start)}" for g in self.scores.today(games) if self.phase_ok(g) and self.in_scope(g)]
        if recap:
            self.say_chan(pack(f"[{self.tag} RECAP]", recap))
        if todays:
            self.say_chan(pack(f"[{self.tag} TODAY]", todays))
        self.state.data["last_digest"] = today
        self.state.save()

    # ---------------- loops ----------------
    def next_delay(self, games):
        live_s, idle_s = CFG["poll_live_seconds"], CFG["poll_idle_seconds"]
        if any(g.live or g.pregame or g.code == 3 for g in games):
            return live_s
        soon = [(g.start - now()).total_seconds() for g in games if g.scheduled and g.start and g.start > now()]
        if soon:
            t = min(soon)
            return live_s if t <= 900 else max(live_s, min(idle_s, t - 600))
        return idle_s

    async def poll_loop(self):
        while True:
            delay = CFG["poll_idle_seconds"]
            try:
                if time.time() - OHL_PHASES_AT > 12 * 3600:
                    await asyncio.to_thread(load_ohl_seasons)
                await self.check_digest()
                games = await self.scores.refresh_live(0)
                if games is not None:
                    games = games + await self.cup_games(0)
                    await self.process_games(games)
                    delay = self.next_delay(games)
                    if self.scores.full_at == 0:
                        await self.scores.refresh_full()
            except Exception:
                log.exception("poll loop")
            await asyncio.sleep(delay)



OhlModule = HockeyTechModule      # the original name
