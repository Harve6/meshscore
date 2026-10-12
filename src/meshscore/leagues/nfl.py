"""NFL, any team (ESPN site API, no key needed)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests

from ..core import CFG, DIGEST_GRACE_HOURS, ModeMixin, PhaseMixin, State, _i, fit, fmt_time, now, pack, session, start_first, tzinfo, when

log = logging.getLogger("meshscore")


# ==========================================================================
# NFL - any team (site.api.espn.com, no key needed).  Used for #bills.
# ==========================================================================
ESPN_NFL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
FOOTBALL_KINDS = {
    "max": {"score", "start", "quarter", "final", "correction"},
    "min": {"quarter", "final"},          # the score at the end of each quarter, then the final
    "off": set(),
}
NFL_SEASON_TYPES = {"pre": 1, "reg": 2, "post": 3}
NFL_QTR = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4", 5: "OT"}
NFL_TD_HOLD = 90        # seconds a touchdown waits for its extra point / 2-pt result (one message instead of two)
NFL_PLAY_TRIES, NFL_PLAY_WAIT = 3, 5   # ESPN's play list can trail the scoreboard
SCORE_TAG = {"TD": "TD", "FG": "FG", "SF": "safety", "TD-2PT": "TD+2pt"}


def espn_get(url, params=None):
    """Parsed JSON or None. Blocking - call via asyncio.to_thread."""
    try:
        res = session.get(url, params=params, timeout=10)
        if res.status_code == 200:
            return json.loads(res.content.decode("utf-8"))
        log.warning("ESPN HTTP %s for %s", res.status_code, url)
    except (requests.RequestException, ValueError) as e:
        log.warning("ESPN error: %s", e)
    return None


@dataclass
class FGame:
    id: str
    start: Optional[datetime]
    state: str            # preview | live | final
    phase: str             # pre | reg | post
    week: int
    away: str
    home: str
    ar: int
    hr: int
    period: int
    clock: str
    away_rec: str
    home_rec: str

    @property
    def live(self): return self.state == "live"
    @property
    def final(self): return self.state == "final"
    @property
    def scheduled(self): return self.state == "preview"


def parse_nfl_game(comp, phase, gid, start_iso, week=0):
    """comp = a competitions[0] dict, from either the team schedule or a game summary's header - same shape."""
    try:
        st = comp["status"]
        t = st["type"]
        state = "final" if t.get("completed") else "live" if t.get("state") == "in" else "preview"
        start = None
        if start_iso:
            dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            start = dt.astimezone(tzinfo()) if tzinfo() else dt.astimezone()

        def side(c):
            sc = c.get("score")
            val = _i(sc.get("value")) if isinstance(sc, dict) else _i(sc)
            rec = next((r.get("displayValue", "") for r in (c.get("record") or []) if r.get("type") == "total"), "")
            return c["team"]["abbreviation"], val, rec
        a = next(side(c) for c in comp["competitors"] if c["homeAway"] == "away")
        h = next(side(c) for c in comp["competitors"] if c["homeAway"] == "home")
        return FGame(id=str(gid), start=start, state=state, phase=phase, week=week,
                     away=a[0], home=h[0], ar=a[1], hr=h[1],
                     period=_i(st.get("period")), clock=str(st.get("displayClock", "")),
                     away_rec=a[2], home_rec=h[2])
    except Exception:
        log.exception("Could not parse NFL game")
        return None


class NflModule(PhaseMixin, ModeMixin):
    arg_cmds = ("!standings", "!series", "!bracket")
    kinds, min_blurb = FOOTBALL_KINDS, "the score at the end of each quarter + final"

    def __init__(self, hub, name):
        cfg = CFG["modules"][name]
        self.hub, self.name, self.cfg = hub, name, cfg
        self.channel = cfg["channel"]
        self.team = str(cfg.get("team", "BUF")).upper()
        self.tag = str(cfg.get("tag") or name).upper()
        self.blurb = f"{cfg.get('label', self.team)} scores"
        self.digest_hour = int(cfg.get("digest_hour", 10))
        self.state = State(f"{name}_state.json")
        self._init_phases()
        self.season, self.season_at = [], 0.0
        self.games, self.games_at, self.ok_at = [], 0.0, 0.0
        self.gstate = {}
        self.standings, self.standings_at = None, 0.0
        self.commands = {
            "!score": self.cmd_score, "!scores": self.cmd_score, "!next": self.cmd_next, "!last": self.cmd_last,
            "!standings": self.cmd_standings, "!mode": self.cmd_mode, "!ping": self.cmd_ping,
            "!botstatus": self.cmd_botstatus, "!help": self.cmd_help, "!phase": self.cmd_phase,
            "!bracket": self.cmd_bracket, "!playoffs": self.cmd_bracket,
        }

    def game_phase(self, g):
        return g.phase

    def status_extra(self):
        return ""

    def say_chan(self, texts):
        self.hub.say_chan(self.channel, texts)

    # ---------------- data ----------------
    async def load_season(self, max_age=900):
        """The team's schedule (all three season types), cached; live/completed scores are already in it."""
        if self.season and time.time() - self.season_at < max_age:
            return self.season
        games = {}
        for phase, code in NFL_SEASON_TYPES.items():
            data = await asyncio.to_thread(espn_get, f"{ESPN_NFL}/teams/{self.team.lower()}/schedule", {"seasontype": code})
            for e in (data or {}).get("events", []):
                g = parse_nfl_game(e["competitions"][0], phase, e["id"], e.get("date"), _i((e.get("week") or {}).get("number")))
                if g:
                    games[g.id] = g
        if games:
            self.season, self.season_at = games, time.time()
        return self.season

    async def refresh(self, max_age=25):
        """Season schedule, with today's/in-progress game(s) overlaid from the live summary (the schedule alone has no live score)."""
        if time.time() - self.games_at < max_age:
            return self.games
        season = await self.load_season()
        if not season:
            return None
        games = dict(season)
        today = now().date()
        need = [g for g in games.values() if (g.start and g.start.date() == today) or (g.live)]
        for g in need:
            data = await asyncio.to_thread(espn_get, f"{ESPN_NFL}/summary", {"event": g.id})
            comp = ((data or {}).get("header") or {}).get("competitions")
            if comp:
                p = parse_nfl_game(comp[0], g.phase, g.id, g.start.isoformat() if g.start else None, g.week)
                if p:
                    games[g.id] = p
        self.games = sorted(games.values(), key=lambda g: g.start or now())
        self.games_at = self.ok_at = time.time()
        return self.games

    def today(self, games=None):
        d = now().date()
        return [g for g in (games if games is not None else self.games) if g.start and g.start.date() == d]

    def opp(self, g):
        return f"vs {g.away} {when(g)}" if g.home == self.team else f"@ {g.home} {when(g)}"

    def won(self, g):
        return (g.hr > g.ar) if g.home == self.team else (g.ar > g.hr)

    @staticmethod
    def line(g):
        pre = "PRE " if g.phase == "pre" else ""
        if g.live:
            return f"{pre}{g.away} {g.ar} @ {g.home} {g.hr} | {NFL_QTR.get(g.period, f'Q{g.period}')} {g.clock}"
        if g.final:
            return f"{pre}FINAL: {g.away} {g.ar} @ {g.home} {g.hr}"
        return f"{pre}{g.away} @ {g.home} {when(g)}"

    # ---------------- commands ----------------
    async def cmd_score(self, inc, arg):
        allg = await self.refresh()
        if allg is None:
            return [f"[{self.tag}] NFL data unavailable, try again shortly."]
        games = self.today(allg)
        if not games:
            nxt = [g for g in allg if g.scheduled and g.start and g.start > now() and self.phase_ok(g)]
            return [f"[{self.tag}] No game today." + (f" Next: {self.opp(nxt[0])}" if nxt else "")]
        return pack(f"[{self.tag}]", [self.line(g) for g in games])

    async def cmd_next(self, inc, arg):
        allg = await self.refresh()
        nxt = [g for g in (allg or []) if g.scheduled and g.start and g.start > now() and self.phase_ok(g)][:3]
        if not nxt:
            return [f"[{self.tag}] No upcoming games found."]
        return pack(f"[{self.tag} NEXT]", [self.opp(g) for g in nxt])

    async def cmd_last(self, inc, arg):
        allg = await self.refresh()
        fin = [g for g in (allg or []) if g.final and self.phase_ok(g)]
        if not fin:
            return [f"[{self.tag}] No recent results."]
        g = fin[-1]
        last5 = "".join("W" if self.won(x) else "L" for x in fin[-5:])
        return [f"[{self.tag} LAST] {'W' if self.won(g) else 'L'} {g.away} {g.ar} @ {g.home} {g.hr}"
                f" | L{len(fin[-5:])}: {last5}"]

    async def cmd_standings(self, inc, arg):
        if self.standings is None or time.time() - self.standings_at > 900:
            data = await asyncio.to_thread(espn_get, "https://site.api.espn.com/apis/v2/sports/football/nfl/standings")
            if data:
                self.standings, self.standings_at = data.get("children", []), time.time()
        groups = self.standings
        if not groups:
            return [f"[{self.tag} STANDINGS] Data unavailable."]
        a = arg.lower()
        want = {"afc": "AFC", "nfc": "NFC"}.get(a)
        if not want:
            allg = await self.refresh()
            me = next((g for g in (allg or []) if self.team in (g.home, g.away)), None)
            want = "AFC" if not me or self.team in ("BUF", "MIA", "NE", "NYJ", "BAL", "CIN", "CLE", "PIT",
                                                     "HOU", "IND", "JAX", "TEN", "DEN", "KC", "LV", "LAC") else "NFC"
        grp = next((g for g in groups if g.get("abbreviation") == want), groups[0])
        entries = grp["standings"]["entries"]

        def stat(e, name):
            return next((s.get("displayValue", "-") for s in e["stats"] if s["name"] == name), "-")
        items = [f"{e['team']['abbreviation']} {stat(e, 'wins')}-{stat(e, 'losses')}" for e in entries]
        return pack(f"[{self.tag} {want}]", items)

    async def cmd_ping(self, inc, arg):
        return await self.hub.cmd_ping(self, inc)

    async def cmd_botstatus(self, inc, arg):
        return await self.hub.status(self, self.ok_at)

    async def cmd_help(self, inc, arg):
        return [f"[{self.tag} BOT] !score !next !last !standings [afc|nfc] !bracket [wc|div|conf|sb] !mode !phase !ping"]

    # ---------------- playoff bracket ----------------
    BRACKET_WEEK = {"1": 1, "wc": 1, "wildcard": 1, "2": 2, "div": 2, "divisional": 2, "3": 3, "conf": 3, "cc": 3,
                    "champ": 3, "championship": 3, "5": 5, "sb": 5, "superbowl": 5, "final": 5}
    BRACKET_LABEL = {1: "WC", 2: "DIV", 3: "CONF", 5: "SB"}        # ESPN's week 4 is the Pro Bowl: never shown

    @staticmethod
    def season_year():
        """The NFL season a date belongs to (the January playoffs are the previous year's season)."""
        n = now()
        return n.year if n.month >= 7 else n.year - 1

    def bracket_item(self, ev):
        c = (ev.get("competitions") or [{}])[0]
        sides = c.get("competitors") or []
        if len(sides) != 2:
            return None
        abbr = [(x.get("team") or {}).get("abbreviation", "?") for x in sides]
        if any(a in ("TBD", "?", "") for a in abbr):          # a placeholder slot of a bracket that isn't set yet
            return None
        score = [_i(x.get("score")) for x in sides]
        st = (ev.get("status") or {}).get("type") or {}
        state, detail = st.get("state", ""), st.get("shortDetail", "")
        away = next((i for i, x in enumerate(sides) if x.get("homeAway") == "away"), 0)
        home = 1 - away
        if state == "post":
            w, l = (home, away) if score[home] >= score[away] else (away, home)
            return f"{abbr[w]} {score[w]} {abbr[l]} {score[l]}" + (" OT" if "OT" in detail else "")
        if state == "in":
            return f"{abbr[away]} {score[away]}-{score[home]} {abbr[home]} {detail}".strip()
        start = None
        try:
            dt = datetime.fromisoformat(str(ev.get("date", "")).replace("Z", "+00:00"))
            start = dt.astimezone(tzinfo()) if tzinfo() else dt.astimezone()
        except ValueError:
            pass
        return f"{abbr[away]}@{abbr[home]} {start.strftime('%a')} {fmt_time(start)}" if start else f"{abbr[away]}@{abbr[home]}"

    async def cmd_bracket(self, inc, arg):
        """!bracket [wc|div|conf|sb]: that round's games (default: the newest round that has any), your team's first."""
        year = self.season_year()
        want = self.BRACKET_WEEK.get(arg.lower().replace(" ", "").replace("-", ""))
        if arg and not want:
            return [f"[{self.tag}] !bracket [wc|div|conf|sb]"]
        for wk in ([want] if want else [5, 3, 2, 1]):
            data = await asyncio.to_thread(espn_get, f"{ESPN_NFL}/scoreboard", {"seasontype": 3, "week": wk, "dates": year})
            events = (data or {}).get("events") or []
            if events:
                mine = lambda e: any((x.get("team") or {}).get("abbreviation") == self.team       # noqa: E731
                                     for x in (e.get("competitions") or [{}])[0].get("competitors", []))
                items = [i for i in (self.bracket_item(e) for e in sorted(events, key=lambda e: not mine(e))) if i]
                if items:
                    return pack(f"[{self.tag} {self.BRACKET_LABEL[wk]}]", items)
        return [f"[{self.tag}] No NFL playoff games yet this season."]

    # ---------------- events ----------------
    def emit(self, kind, texts):
        if kind in self.kinds.get(self.level(), set()):
            self.say_chan(texts)

    async def scoring_play(self, g, team):
        """(play text, ESPN type) of the scoring play that just happened for `team`, or (None, None).  ESPN's play list
        reports the score AFTER the extra point and can trail the scoreboard by a poll, so match on team + 'score has
        reached the scoreboard's' (not equality) and retry briefly."""
        for attempt in range(NFL_PLAY_TRIES):
            if attempt:
                await asyncio.sleep(NFL_PLAY_WAIT)
            data = await asyncio.to_thread(espn_get, f"{ESPN_NFL}/summary", {"event": g.id})
            mine = [p for p in ((data or {}).get("scoringPlays") or [])
                    if (p.get("team") or {}).get("abbreviation") == team
                    and _i(p.get("awayScore" if team == g.away else "homeScore")) >= (g.ar if team == g.away else g.hr)]
            if mine:
                td = [p for p in mine if (p.get("type") or {}).get("abbreviation", "").startswith("TD")]
                p = (td or mine)[-1]
                return (p.get("text") or "").strip(), (p.get("type") or {}).get("abbreviation", "")
        return None, None

    async def score_event(self, g, team, delta):
        """One scoring alert: scorer + play description from ESPN, the score now, the clock."""
        desc, kind = await self.scoring_play(g, team) if delta != 1 else (None, None)   # a lone PAT has no play of its own
        tag = {6: "TD", 7: "TD", 8: "TD+2pt", 3: "FG", 2: "safety", 1: "PAT"}.get(delta) or SCORE_TAG.get(kind, kind or "score")
        score = f"{g.away} {g.ar}-{g.hr} {g.home}"
        clock = f"{NFL_QTR.get(g.period, f'Q{g.period}')} {g.clock}".strip()
        text = f"🏈 {team} {tag}!" + (f" {desc}" if desc else "") + f" | {score} | {clock}"
        return ("score", [fit(text)])

    async def events(self, g, prev, cur):
        """A touchdown (+6) is held until the extra point / 2-pt try shows on the scoreboard - or NFL_TD_HOLD seconds
        pass - so a TD and its conversion go out as ONE message."""
        score = f"{g.away} {g.ar}-{g.hr} {g.home}"
        clock = f"{NFL_QTR.get(g.period, f'Q{g.period}')} {g.clock}".strip()
        evs = []
        pend = prev.get("pend")
        base = pend["from"] if pend else (prev["ar"], prev["hr"])
        d_a, d_h = g.ar - base[0], g.hr - base[1]
        round_over = g.final or g.period != prev["period"] or g.state != prev["state"]
        if d_a < 0 or d_h < 0:
            evs.append(("correction", [f"⚠️ Score update: {score} | {clock}"]))
        elif pend:
            own, other = (d_a, d_h) if pend["team"] == g.away else (d_h, d_a)
            if other == 0 and own in (7, 8):
                evs.append(await self.score_event(g, pend["team"], own))
            elif other == 0 and own == 6 and not round_over and time.monotonic() - pend["t0"] < NFL_TD_HOLD:
                cur["pend"] = pend                      # still waiting on the conversion
            else:                                       # timed out / quarter or game ended / something else scored
                evs.append(await self.score_event(g, pend["team"], 6))
                if other:                               # the other team scored too: announce that separately
                    o_team = g.home if pend["team"] == g.away else g.away
                    evs.append(await self.score_event(g, o_team, other))
                elif own not in (6, 7, 8):
                    evs.append(await self.score_event(g, pend["team"], own - 6))
        elif d_a or d_h:
            team, d = (g.away, d_a) if d_a else (g.home, d_h)
            if d_a and d_h:                                         # both moved between polls: report as a score update
                evs.append(("correction", [f"⚠️ Score update: {score} | {clock}"]))
            elif d == 6 and not round_over:
                cur["pend"] = {"team": team, "from": base, "t0": time.monotonic()}
            else:
                evs.append(await self.score_event(g, team, d))
        if prev["state"] == "preview" and g.state == "live":
            evs.append(("start", [f"🏈 Kickoff: {g.away} @ {g.home}"]))
        elif g.live and g.period > prev["period"] and prev["period"] >= 1:
            label = "Half" if prev["period"] == 2 else NFL_QTR.get(prev["period"], f"Q{prev['period']}")
            evs.append(("quarter", [f"⏱ End of {label}: {score}"]))
        if g.final and prev["state"] != "final":
            evs.append(("final", [fit(f"🏁 FINAL: {g.away} {g.ar} @ {g.home} {g.hr} | {g.away} {g.away_rec}, {g.home} {g.home_rec}")]))
        return start_first(evs)

    @staticmethod
    def plain_events(g, prev):
        """What to say if the detailed alert logic hit an unexpected error: just the score change and the final."""
        score, evs = f"{g.away} {g.ar}-{g.hr} {g.home}", []
        if g.ar >= prev["ar"] and g.hr >= prev["hr"] and (g.ar, g.hr) != (prev["ar"], prev["hr"]):
            evs.append(("score", [f"🏈 Score | {score}"]))
        if g.final and prev["state"] != "final":
            evs.append(("final", [f"🏁 FINAL: {g.away} {g.ar} @ {g.home} {g.hr}"]))
        return evs

    async def process(self, games):
        games = [g for g in games if self.phase_ok(g)]
        seen = set()
        for g in games:
            seen.add(g.id)
            cur = {"ar": g.ar, "hr": g.hr, "state": g.state, "period": g.period, "pend": None}
            prev, self.gstate[g.id] = self.gstate.get(g.id), cur
            if prev is None:
                continue  # first sighting: baseline only
            try:
                evs = await self.events(g, prev, cur)
            except Exception:
                log.exception("%s: alert logic failed for game %s - sending a plain update", self.name, g.id)
                cur["pend"] = None
                evs = self.plain_events(g, prev)
            for kind, texts in evs:
                self.emit(kind, texts)
        for gid in set(self.gstate) - seen:
            del self.gstate[gid]

    # ---------------- daily "game today" ----------------
    async def check_digest(self):
        n = now()
        today, h = n.date().isoformat(), self.digest_hour
        if n.hour < h or self.state.data["last_digest"] == today:
            return
        if n.hour >= h + DIGEST_GRACE_HOURS or self.level() == "off":
            self.state.data["last_digest"] = today
            self.state.save()
            return
        games = await self.refresh(0)
        if games is None:
            return
        todays = [g for g in self.today(games) if g.scheduled and self.phase_ok(g)]
        if todays:
            self.say_chan(pack(f"[{self.tag} TODAY]", [self.line(g) for g in todays]))
        self.state.data["last_digest"] = today
        self.state.save()

    # ---------------- loops ----------------
    def next_delay(self, games):
        live_s, idle_s = CFG["poll_live_seconds"], CFG["poll_idle_seconds"]
        if any(g.live for g in games):
            return live_s
        soon = [(g.start - now()).total_seconds() for g in games if g.scheduled and g.start and g.start > now()]
        if soon:
            t = min(soon)
            return live_s if t <= 900 else max(live_s, min(idle_s, t - 600))
        return idle_s

    async def run(self):
        n = now()
        if not self.state.data["last_digest"] and n.hour >= self.digest_hour + DIGEST_GRACE_HOURS:
            self.state.data["last_digest"] = n.date().isoformat()
        while True:
            delay = CFG["poll_idle_seconds"]
            try:
                await self.check_digest()
                games = await self.refresh(0)
                if games is not None:
                    await self.process(games)
                    delay = self.next_delay(games)
            except Exception:
                log.exception(f"{self.name} poll loop")
            await asyncio.sleep(delay)

