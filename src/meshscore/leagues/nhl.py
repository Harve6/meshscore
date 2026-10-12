"""NHL, any team (api-web.nhle.com, no key needed)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests

from ..core import CFG, DIGEST_GRACE_HOURS, GOAL_LAG_WAIT, HOCKEY_KINDS, LATE_STARS_WAIT, ModeMixin, PhaseMixin, State, _i, fit, fmt_time, now, pack, sched_days, session, start_first, tzinfo, when

log = logging.getLogger("meshscore")


# ==========================================================================
# NHL - any team (api-web.nhle.com, no key needed).  Used for #leafs.
# ==========================================================================
NHL_API = "https://api-web.nhle.com/v1"
NHL_DIVS = {"atl": "Atlantic", "atlantic": "Atlantic", "met": "Metropolitan", "metro": "Metropolitan",
            "metropolitan": "Metropolitan", "cen": "Central", "central": "Central", "pac": "Pacific", "pacific": "Pacific"}
NHL_CONFS = {"east": "Eastern", "eastern": "Eastern", "west": "Western", "western": "Western"}


def nhl_get(path):
    """Parsed JSON or None. Blocking - call via asyncio.to_thread."""
    try:
        res = session.get(NHL_API + path, timeout=10)
        if res.status_code == 200:
            return json.loads(res.content.decode("utf-8"))
        log.warning("NHL HTTP %s for %s", res.status_code, path)
    except (requests.RequestException, ValueError) as e:
        log.warning("NHL error: %s", e)
    return None


def period_label(number, ptype, gtype):
    if ptype == "SO":
        return "SO"
    if ptype == "OT":
        return "OT" if gtype != 3 or number <= 4 else f"{number - 3}OT"
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(number, f"P{number}")


@dataclass
class NGame:
    id: int
    date: str
    start: Optional[datetime]
    state: str            # preview | live | final | postponed
    gtype: int            # 1 preseason, 2 regular, 3 playoffs
    away: str
    home: str
    ar: int
    hr: int
    period: int
    ptype: str            # REG | OT | SO
    clock: str
    inter: bool
    last_ptype: str       # how the game ended (REG/OT/SO), once final
    goals: list
    series: str
    ss: Optional[dict] = None      # raw playoff seriesStatus (round, wins, neededToWin ...)

    @property
    def live(self): return self.state == "live"
    @property
    def final(self): return self.state == "final"
    @property
    def scheduled(self): return self.state == "preview"
    @property
    def plabel(self): return period_label(self.period, self.ptype, self.gtype)
    @property
    def end_tag(self): return "" if self.last_ptype in ("", "REG") else f"/{self.last_ptype}"


def parse_ngame(g):
    try:
        raw, sched = g.get("gameState", ""), g.get("gameScheduleState", "OK")
        state = ("postponed" if sched in ("PPD", "CNCL") else "final" if raw in ("FINAL", "OFF")
                 else "live" if raw in ("LIVE", "CRIT") else "preview")
        a, h = g.get("awayTeam", {}), g.get("homeTeam", {})
        pd, clock = g.get("periodDescriptor") or {}, g.get("clock") or {}
        start = None
        if g.get("startTimeUTC"):
            dt = datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00"))
            start = dt.astimezone(tzinfo()) if tzinfo() else dt.astimezone()
        series = ""
        ss = g.get("seriesStatus")
        if isinstance(ss, dict) and g.get("gameType") == 3:
            series = (f"{ss.get('seriesAbbrev', 'Series')} G{ss.get('gameNumberOfSeries', '?')} "
                      f"{ss.get('topSeedTeamAbbrev', '')} {ss.get('topSeedWins', 0)}-{ss.get('bottomSeedWins', 0)}").strip()
        return NGame(id=_i(g.get("id")), date=g.get("gameDate", ""), start=start, state=state, gtype=_i(g.get("gameType"), 2),
                     away=a.get("abbrev", "?"), home=h.get("abbrev", "?"), ar=_i(a.get("score")), hr=_i(h.get("score")),
                     period=_i(pd.get("number")), ptype=pd.get("periodType", "REG"),
                     clock=clock.get("timeRemaining", ""), inter=bool(clock.get("inIntermission")),
                     last_ptype=(g.get("gameOutcome") or {}).get("lastPeriodType", ""),
                     goals=g.get("goals") or [], series=series, ss=ss if isinstance(ss, dict) and g.get("gameType") == 3 else None)
    except Exception:
        log.exception("Could not parse NHL game")
        return None


def landing_goals(land):
    """The game page's goal list, same shape as the scoreboard feed's. The two update independently, and the
    scoreboard's team score often moves a while before its goal list does."""
    try:
        return [x for p in land["summary"]["scoring"] for x in p["goals"]]
    except (TypeError, KeyError):
        return []


def nhl_series_text(ss):
    """'CAR 3-1' / 'tied 2-2' / 'CAR win 4-2' from a playoff seriesStatus."""
    top, bot = ss.get("topSeedTeamAbbrev", "?"), ss.get("bottomSeedTeamAbbrev", "?")
    tw, bw, need = _i(ss.get("topSeedWins")), _i(ss.get("bottomSeedWins")), _i(ss.get("neededToWin"), 4)
    if tw >= need:
        return f"{top} win {tw}-{bw}"
    if bw >= need:
        return f"{bot} win {bw}-{tw}"
    if tw == bw:
        return f"tied {tw}-{bw}"
    return f"{top} {tw}-{bw}" if tw > bw else f"{bot} {bw}-{tw}"


class NhlModule(PhaseMixin, ModeMixin):
    arg_cmds = ("!standings", "!series", "!bracket")
    kinds, min_blurb = HOCKEY_KINDS, "period results + final"

    def __init__(self, hub, name):
        cfg = CFG["modules"][name]
        self.hub, self.name = hub, name
        self.channel = cfg["channel"]
        self.team = str(cfg.get("team", "TOR")).upper()
        self.tag = str(cfg.get("tag") or name).upper()
        self.blurb = f"{cfg.get('label', self.team)} scores"
        self.digest_hour = int(cfg.get("digest_hour", 10))
        self.cfg = cfg
        self.state = State(f"{name}_state.json")
        self._init_phases()
        self.state.data.setdefault("last_po_digest", "")
        self.season, self.season_at, self.cur_season = [], 0.0, 0
        self.games, self.games_at, self.ok_at = [], 0.0, 0.0
        self.gstate = {}
        self._bg = set()
        self.standings, self.standings_at = None, 0.0
        self.overview_months = tuple(cfg.get("overview_months") or (4, 5, 6))
        self.ov_batch_min = float(cfg.get("overview_batch_minutes", 20))
        self.ov_state, self.ov_done, self.ov_pending = {}, set(), []      # league-wide playoff games (see "overview")
        self.commands = {
            "!score": self.cmd_score, "!scores": self.cmd_score, "!next": self.cmd_next, "!last": self.cmd_last,
            "!standings": self.cmd_standings, "!mode": self.cmd_mode, "!ping": self.cmd_ping,
            "!botstatus": self.cmd_botstatus, "!help": self.cmd_help, "!phase": self.cmd_phase,
            "!series": self.cmd_series, "!playoffs": self.cmd_series, "!bracket": self.cmd_series,
        }

    def game_phase(self, g):
        return "pre" if g.gtype == 1 else "post" if g.gtype == 3 else "reg"

    def status_extra(self):
        return ""

    def say_chan(self, texts):
        self.hub.say_chan(self.channel, texts)

    # ---------------- data ----------------
    def mine(self, g):
        return self.team in (g.get("awayTeam", {}).get("abbrev"), g.get("homeTeam", {}).get("abbrev"))

    def dates_needed(self, games):
        """Dates whose live scoreboard we need: today, plus any earlier game that has not finished yet."""
        today = now().date().isoformat()
        need = {g.date for g in games if g.date == today}
        for g in games:
            if not g.final and g.state != "postponed" and g.start and g.start <= now() and g.date != today:
                need.add(g.date)
        return sorted(need)[:2]

    async def refresh(self, max_age=25):
        if time.time() - self.games_at < max_age:
            return self.games
        if not self.season or time.time() - self.season_at > 900:
            data = await asyncio.to_thread(nhl_get, f"/club-schedule-season/{self.team.lower()}/now")
            if data:
                self.season = [x for x in (parse_ngame(g) for g in data.get("games", [])) if x]
                self.season_at = time.time()
                self.cur_season = _i(data.get("currentSeason"), self.cur_season)
            elif not self.season:
                return None
        games = {g.id: g for g in self.season}
        for d in self.dates_needed(list(games.values())):
            sc = await asyncio.to_thread(nhl_get, f"/score/{d}")
            if sc:
                for g in sc.get("games", []):
                    if self.mine(g) and (p := parse_ngame(g)):
                        games[p.id] = p
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
        pre = "PRE " if g.gtype == 1 else ""
        if g.live:
            when_ = f"{g.plabel} INT" if g.inter else f"{g.plabel} {g.clock}"
            return f"{pre}{g.away} {g.ar} @ {g.home} {g.hr} | {when_}"
        if g.final:
            return f"{pre}FINAL{g.end_tag}: {g.away} {g.ar} @ {g.home} {g.hr}"
        if g.state == "postponed":
            return f"{pre}{g.away} @ {g.home} POSTPONED"
        return f"{pre}{g.away} @ {g.home} {when(g)}"

    # ---------------- commands ----------------
    async def cmd_score(self, inc, arg):
        allg = await self.refresh()
        if allg is None:
            return [f"[{self.tag}] NHL data unavailable, try again shortly."]
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
        return [f"[{self.tag} LAST] {'W' if self.won(g) else 'L'} {g.away} {g.ar} @ {g.home} {g.hr}{g.end_tag}"
                f" | L{len(fin[-5:])}: {last5}"]

    async def cmd_standings(self, inc, arg):
        if self.standings is None or time.time() - self.standings_at > 300:
            data = await asyncio.to_thread(nhl_get, "/standings/now")
            if data:
                self.standings, self.standings_at = data.get("standings", []), time.time()
        rows = self.standings
        if not rows:
            return [f"[{self.tag} STANDINGS] Data unavailable."]
        me = next((t for t in rows if t["teamAbbrev"]["default"] == self.team), None)

        def item(t):
            return f"{t['teamAbbrev']['default']} {t['points']}pt {t['wins']}-{t['losses']}-{t['otLosses']}"

        a = arg.lower()
        if a in ("wc", "wildcard"):
            conf = me["conferenceName"] if me else "Eastern"
            wc = sorted((t for t in rows if t["conferenceName"] == conf and t.get("wildcardSequence")),
                        key=lambda t: t["wildcardSequence"])[:6]
            return pack(f"[{self.tag} {conf[:1]}WC]", [f"{t['wildcardSequence']}.{item(t)}" for t in wc])
        if a in NHL_CONFS:
            rows2 = sorted((t for t in rows if t["conferenceName"] == NHL_CONFS[a]), key=lambda t: t["conferenceSequence"])[:8]
            return pack(f"[{self.tag} {NHL_CONFS[a][:1]}]", [item(t) for t in rows2])
        div = NHL_DIVS.get(a) or (me["divisionName"] if me and not a else None)
        if not div:
            return [f"[{self.tag}] !standings [atl|met|cen|pac|east|west|wc]"]
        rows2 = sorted((t for t in rows if t["divisionName"] == div), key=lambda t: t["divisionSequence"])
        return pack(f"[{self.tag} {div[:3].upper()}]", [item(t) for t in rows2])

    async def cmd_ping(self, inc, arg):
        return await self.hub.cmd_ping(self, inc)

    async def cmd_botstatus(self, inc, arg):
        return await self.hub.status(self, self.ok_at)

    async def cmd_help(self, inc, arg):
        return [f"[{self.tag} BOT] !score !next !last !standings [atl|east|wc] !series [1-4] !mode !phase !ping"]

    # ---------------- events ----------------
    def emit(self, kind, texts):
        if kind in self.kinds.get(self.level(), set()):
            self.say_chan(texts)

    async def landing(self, gid):
        return await asyncio.to_thread(nhl_get, f"/gamecenter/{gid}/landing")

    @staticmethod
    def sog(land):
        try:
            return land["awayTeam"]["sog"], land["homeTeam"]["sog"]
        except (TypeError, KeyError):
            return None

    @staticmethod
    def stars(land):
        try:
            return [s["name"]["default"] for s in land["summary"]["threeStars"]]
        except (TypeError, KeyError):
            return []

    async def goal_info(self, g):
        """Latest goal: (scorer, tag, hat-trick count). If the goals list lags the score, wait and look again, then
        try the game page's own goal list (twice, a little apart) before giving up on the name."""
        total = g.ar + g.hr
        goals = g.goals
        if len(goals) != total:
            await asyncio.sleep(GOAL_LAG_WAIT)
            await self.refresh(0)
            fresh = next((x for x in self.games if x.id == g.id), None)
            goals = fresh.goals if fresh else []
        tries = 0
        while len(goals) != total and tries < 2:
            tries += 1
            goals = landing_goals(await self.landing(g.id))
            if len(goals) != total and tries < 2:
                await asyncio.sleep(GOAL_LAG_WAIT)
        if len(goals) != total or not goals:
            return None
        last = goals[-1]
        name = f"{last.get('firstName', {}).get('default', '')[:1]}. {last.get('lastName', {}).get('default', '')}".strip(". ")
        tag = {"pp": " (PP)", "sh": " (SH)"}.get(last.get("strength"), "")
        mod = last.get("goalModifier", "none")
        tag += " (EN)" if mod == "empty-net" else " (PS)" if mod == "penalty-shot" else ""
        count = sum(1 for x in goals if x.get("playerId") == last.get("playerId"))
        return name, tag, count

    async def events(self, g, prev):
        score = f"{g.away} {g.ar}-{g.hr} {g.home}"
        clock = (f"{g.plabel} INT" if g.inter else f"{g.plabel} {g.clock}").strip()
        evs = []
        d_a, d_h = g.ar - prev["ar"], g.hr - prev["hr"]
        if d_a < 0 or d_h < 0:
            evs.append(("correction", [f"⚠️ Score update: {score} | {clock}"]))
        elif (d_a or d_h) and g.ptype != "SO":
            n, scorer_home = d_a + d_h, d_h > d_a
            team = g.home if scorer_home else g.away
            mine = team == self.team
            if n == 1:
                info = await self.goal_info(g)
                who, tags = (f" {info[0]}", info[1] + (" 🔥HAT TRICK!" if info[2] == 3 else f" ({info[2]} goals)" if info[2] > 3 else "")) if info else ("", "")
                evs.append(("goal", [f"🚨 {team} GOAL{tags}{who} | {score} | {clock}" if mine else
                                     f"🚨 {team} goal{tags}{who} | {score} | {clock}"]))
            else:
                evs.append(("goal", [f"🚨 {n} goals | {score} | {clock}"]))

        if prev["state"] == "preview" and g.state == "live":
            extra = f" | {g.series}" if g.series else " (preseason)" if g.gtype == 1 else ""
            evs.append(("start", [f"🏒 Puck drop: {g.away} @ {g.home}{extra}"]))
        elif g.live and g.inter and not prev["inter"]:
            text = f"⏸ End of {g.plabel}: {score}"
            shots = self.sog(await self.landing(g.id))
            if shots:
                text += f" | SOG {shots[0]}-{shots[1]}"
            evs.append(("intermission", [text]))
        elif g.live and g.period >= 4 and prev["period"] < 4:
            evs.append(("period", [f"⏱ {g.plabel}: {score}"]))
        if g.state == "postponed" and prev["state"] != "postponed":
            evs.append(("period", [f"🌧 {g.away} @ {g.home} postponed"]))

        if g.final and prev["state"] != "final":
            land = await self.landing(g.id)
            text = f"🛑 FINAL{g.end_tag}: {g.away} {g.ar} @ {g.home} {g.hr}"
            shots, stars = self.sog(land), self.stars(land)
            if shots:
                text += f" | SOG {shots[0]}-{shots[1]}"
            if g.series:
                text += f" | {g.series}"
            evs.append(("final", [fit(text)] + (["⭐ " + ", ".join(stars)] if len(stars) == 3 else [])))
            if len(stars) != 3:
                self._late_stars(g)
        return start_first(evs)

    def _late_stars(self, g):
        """Wait for all 3 stars to be entered; never post a partial list."""
        async def job():
            try:
                for _ in range(14):
                    await asyncio.sleep(LATE_STARS_WAIT)
                    stars = self.stars(await self.landing(g.id))
                    if len(stars) == 3:
                        self.emit("stars", ["⭐ " + ", ".join(stars)])
                        return
            except Exception:
                log.exception("late stars")
        task = asyncio.create_task(job())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    @staticmethod
    def plain_events(g, prev):
        """What to say if the detailed alert logic hit an unexpected error: just the score change and the final."""
        score, evs = f"{g.away} {g.ar}-{g.hr} {g.home}", []
        if g.ptype != "SO" and g.ar >= prev["ar"] and g.hr >= prev["hr"] and (g.ar, g.hr) != (prev["ar"], prev["hr"]):
            evs.append(("goal", [f"🚨 Goal | {score}"]))
        if g.final and prev["state"] != "final":
            evs.append(("final", [f"🛑 FINAL: {g.away} {g.ar} @ {g.home} {g.hr}"]))
        return evs

    async def process(self, games):
        games = [g for g in games if self.phase_ok(g)]     # skip preseason / regular season / playoff games that are switched off
        seen = set()
        for g in games:
            seen.add(g.id)
            cur = {"ar": g.ar, "hr": g.hr, "state": g.state, "inter": g.inter, "period": g.period}
            prev, self.gstate[g.id] = self.gstate.get(g.id), cur
            if prev is None:
                continue  # first sighting: baseline only
            try:
                evs = await self.events(g, prev)
            except Exception:
                log.exception("%s: alert logic failed for game %s - sending a plain update", self.name, g.id)
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

    # ---------------- playoffs overview: only while this team is NOT in the playoffs ----------------
    def team_alive(self):
        """True while this team is in the playoffs (a series still on, or won and waiting for the next round)."""
        po = [g for g in self.season if g.gtype == 3]
        if not po:
            return False
        finals = sorted((g for g in po if g.final and g.ss), key=lambda g: g.start or now())
        if finals:
            last = finals[-1]      # if that game ended a series, whoever won it won the series (the schedule names no sides)
            need = _i(last.ss.get("neededToWin"), 4)
            if max(_i(last.ss.get("topSeedWins")), _i(last.ss.get("bottomSeedWins"))) >= need and not self.won(last):
                return False
        return True

    def overview_on(self):
        return bool(self.cfg.get("overview", True) and self.phases["post"] and now().month in self.overview_months
                    and not self.team_alive())

    async def refresh_league(self):
        """Playoff games of every other team (yesterday's late games count after midnight)."""
        start, end = sched_days()
        games = {}
        for d in sorted({start, end}):
            sc = await asyncio.to_thread(nhl_get, f"/score/{d}")
            for g in (sc or {}).get("games", []):
                if g.get("gameType") == 3 and not self.mine(g) and (p := parse_ngame(g)):
                    games[p.id] = p
        return sorted(games.values(), key=lambda g: g.start or now())

    @staticmethod
    def overview_item(g):
        return f"{g.away} {g.ar}-{g.hr} {g.home}{g.end_tag}" + (f" ({nhl_series_text(g.ss)})" if g.ss else "")

    async def process_overview(self):
        """One batched results message instead of a message per game; the Cup Final is announced straight away."""
        if not self.overview_on():
            self.ov_state.clear()
            self.ov_pending.clear()
            return None
        games = await self.refresh_league()
        seen = set()
        for g in games:
            seen.add(g.id)
            prev, self.ov_state[g.id] = self.ov_state.get(g.id), g.state
            if prev is None:
                continue                       # first sighting: baseline only
            if g.final and prev != "final" and g.id not in self.ov_done:
                self.ov_done.add(g.id)
                self.ov_pending.append((time.time(), g))
        for gid in set(self.ov_state) - seen:
            del self.ov_state[gid]
        self.flush_overview(games)
        return games

    def flush_overview(self, games):
        if not self.ov_pending:
            return
        cup = any((g.ss or {}).get("round") == 4 for _, g in self.ov_pending)
        oldest = min(t for t, _ in self.ov_pending)
        if not (cup or not any(x.live for x in games) or time.time() - oldest >= self.ov_batch_min * 60):
            return                             # more games are still on: wait and send one message for all of them
        done, self.ov_pending = [g for _, g in self.ov_pending], []
        rounds = {}
        for g in done:
            rounds.setdefault((g.ss or {}).get("seriesAbbrev") or "PO", []).append(g)
        for short, gs in rounds.items():
            self.emit("final", pack(f"[NHL {short}]", [self.overview_item(g) for g in gs]))
        for g in done:
            ss = g.ss or {}
            top, bot = ss.get("topSeedTeamAbbrev", "?"), ss.get("bottomSeedTeamAbbrev", "?")
            tw, bw, need = _i(ss.get("topSeedWins")), _i(ss.get("bottomSeedWins")), _i(ss.get("neededToWin"), 4)
            if ss.get("round") == 4 and max(tw, bw) >= need:
                self.emit("series", [f"🏆 {top if tw > bw else bot} WIN THE STANLEY CUP ({max(tw, bw)}-{min(tw, bw)})!"])

    async def check_overview_digest(self):
        if not self.overview_on():
            return
        n = now()
        today = n.date().isoformat()
        if n.hour < self.digest_hour or self.state.data.get("last_po_digest") == today:
            return
        if n.hour >= self.digest_hour + DIGEST_GRACE_HOURS or self.level() == "off":
            self.state.data["last_po_digest"] = today
            self.state.save()
            return
        games = await self.refresh_league()
        self.state.data["last_po_digest"] = today
        self.state.save()
        rounds = {}
        for g in games:
            if g.scheduled and g.start and g.start.date() == n.date():
                rounds.setdefault((g.ss or {}).get("seriesAbbrev") or "PO", []).append(f"{g.away}@{g.home} {fmt_time(g.start)}")
        for short, items in rounds.items():
            self.say_chan(pack(f"[NHL {short} TODAY]", items))

    ROUND_WORDS = {"1": 1, "r1": 1, "first": 1, "2": 2, "r2": 2, "second": 2, "3": 3, "r3": 3, "cf": 3, "conf": 3,
                   "4": 4, "r4": 4, "scf": 4, "final": 4, "finals": 4, "cup": 4}

    async def cmd_series(self, inc, arg):
        """!series [1-4|cf|scf]: the current playoff round (or the one asked for), this team's series first."""
        sid = int(self.cfg.get("season_id") or self.cur_season or 0)
        data = await asyncio.to_thread(nhl_get, f"/playoff-series/carousel/{sid}/") if sid else None
        rounds = (data or {}).get("rounds") or []
        if not rounds:
            return [f"[{self.tag}] No playoffs right now."]
        want = self.ROUND_WORDS.get(arg.lower())
        if arg and not want:
            return [f"[{self.tag}] !series [1|2|3|4|cf|scf]"]
        cur = want or _i(data.get("currentRound"), len(rounds))
        rd = next((r for r in rounds if r.get("roundNumber") == cur), None)
        if rd is None:
            return [f"[{self.tag}] No round {cur} yet."]
        items, mine = [], []
        for x in rd.get("series", []):
            t, b = x.get("topSeed") or {}, x.get("bottomSeed") or {}
            if not t.get("abbrev") or not b.get("abbrev"):
                continue
            tw, bw, need = _i(t.get("wins")), _i(b.get("wins")), _i(x.get("neededToWin"), 4)
            lead, trail, lw, tw2 = (t["abbrev"], b["abbrev"], tw, bw) if tw >= bw else (b["abbrev"], t["abbrev"], bw, tw)
            line = (f"{lead} def {trail} {lw}-{tw2}" if lw >= need else f"{lead}-{trail} tied {lw}-{tw2}" if lw == tw2
                    else f"{lead} {lw}-{tw2} {trail}")
            (mine if self.team in (t["abbrev"], b["abbrev"]) else items).append(line)
        items = mine + items
        short = {1: "R1", 2: "R2", 3: "CF", 4: "SCF"}.get(cur, "PO")
        return pack(f"[NHL {short}]", items) if items else [f"[{self.tag}] No playoff series yet."]

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
                overview = await self.process_overview()
                await self.check_overview_digest()
                if overview:
                    delay = min(delay, self.next_delay(overview))
            except Exception:
                log.exception(f"{self.name} poll loop")
            await asyncio.sleep(delay)

