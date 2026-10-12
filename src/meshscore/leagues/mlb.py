"""MLB, any team (statsapi.mlb.com, no key needed)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import requests

from ..core import BASEBALL_KINDS, CFG, DIGEST_GRACE_HOURS, ModeMixin, PhaseMixin, State, _i, fit, now, ord_, pack, sched_days, session, start_first, tzinfo, when

log = logging.getLogger("meshscore")


# ==========================================================================
# MLB - any team   (statsapi.mlb.com, no key needed)
# ==========================================================================
MLB_API = "https://statsapi.mlb.com/api"
FEED_LAG_WAIT = 8      # seconds to wait for the play feed to catch up with the (earlier-updating) scoreboard
DIVISIONS = {"ale": 201, "alc": 202, "alw": 200, "nle": 204, "nlc": 205, "nlw": 203}
DIV_ALIAS = {"east": "ale", "central": "alc", "west": "alw"}


def mlb_get(path, params=None):
    """Parsed JSON or None. Blocking - call via asyncio.to_thread."""
    try:
        res = session.get(MLB_API + path, params=params, timeout=10)
        if res.status_code == 200:
            return json.loads(res.content.decode("utf-8"))  # decode ourselves: requests guesses latin-1
        log.warning("MLB HTTP %s", res.status_code)
    except (requests.RequestException, ValueError) as e:
        log.warning("MLB error: %s", e)
    return None


def last_name(full):
    parts = full.split()
    while len(parts) > 1 and parts[-1].lower().strip(".") in ("jr", "sr", "ii", "iii", "iv"):
        parts.pop()                       # "Vladimir Guerrero Jr." -> "Guerrero"
    return parts[-1] if parts else ""


def trim_to(text, nbytes):
    if len(text.encode("utf-8")) <= nbytes:
        return text
    return text.encode("utf-8")[: max(nbytes - 3, 0)].decode("utf-8", errors="ignore").rstrip() + "..."


@dataclass
class MGame:
    pk: int
    start: Optional[datetime]
    state: str            # preview | live | final | postponed
    detail: str
    delayed: bool
    away: str
    home: str
    away_id: int
    home_id: int
    ar: int
    hr: int
    inning: int
    inn_txt: str          # "Top 5th"
    ar_rec: str
    hr_rec: str
    away_sp: str
    home_sp: str
    win: str
    loss: str
    save: str
    gtype: str = "R"      # R regular season, F wild card, D division series, L league championship, W world series
    sdesc: str = ""       # "AL Division Series"
    sgame: int = 0        # game number within the series
    sstat: Optional[dict] = None   # MLB seriesStatus (wins/losses/leader)
    ah: int = -1          # hits by the away / home batters (-1 = unknown)
    hh: int = -1

    def involves(self, team_id): return team_id in (self.away_id, self.home_id)
    @property
    def live(self): return self.state == "live"
    @property
    def final(self): return self.state == "final"
    @property
    def scheduled(self): return self.state == "preview"
    def is_home(self, team_id): return self.home_id == team_id
    def rec_of(self, team_id): return self.hr_rec if self.home_id == team_id else self.ar_rec
    def won_by(self, team_id): return (self.hr > self.ar) if self.home_id == team_id else (self.ar > self.hr)


def parse_mgame(g):
    try:
        st = g.get("status", {})
        ab, det = st.get("abstractGameState", ""), st.get("detailedState", "")
        # MLB reports pregame "Warmup" (and delayed starts) as abstractGameState "Live"; that is not first pitch yet
        pregame = det in ("Warmup", "Pre-Game", "Scheduled") or det.startswith("Delayed Start")
        state = ("postponed" if any(w in det for w in ("Postponed", "Cancelled")) else
                 "final" if ab == "Final" else "live" if ab == "Live" and not pregame else "preview")
        t, ls = g.get("teams", {}), g.get("linescore") or {}

        def side(k):
            d = t.get(k, {})
            team, rec = d.get("team", {}), d.get("leagueRecord", {})
            return (team.get("abbreviation") or team.get("teamName") or "?", _i(team.get("id")), _i(d.get("score")),
                    f"{rec.get('wins', '?')}-{rec.get('losses', '?')}",
                    last_name((d.get("probablePitcher") or {}).get("fullName", "")))

        a, h = side("away"), side("home")
        raw = g.get("gameDate")
        start = None
        if raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            start = dt.astimezone(tzinfo()) if tzinfo() else dt.astimezone()
        half = {"Top": "Top", "Middle": "Mid", "Bottom": "Bot", "End": "End"}.get(ls.get("inningState", ""), "")
        inn = _i(ls.get("currentInning"))
        dec = g.get("decisions") or {}
        name = lambda k: last_name((dec.get(k) or {}).get("fullName", ""))
        reason = st.get("reason", "")
        lt = ls.get("teams") or {}
        return MGame(pk=_i(g.get("gamePk")), start=start, state=state, detail=f"{det} ({reason})" if reason else det,
                     delayed=det.startswith("Delayed"),
                     away=a[0], home=h[0], away_id=a[1], home_id=h[1], ar=a[2], hr=h[2], inning=inn,
                     inn_txt=f"{half} {ls.get('currentInningOrdinal', '')}".strip() if inn else "",
                     ar_rec=a[3], hr_rec=h[3], away_sp=a[4], home_sp=h[4],
                     win=name("winner"), loss=name("loser"), save=name("save"),
                     gtype=g.get("gameType", "R"), sdesc=g.get("seriesDescription", ""),
                     sgame=_i(g.get("seriesGameNumber")), sstat=g.get("seriesStatus"),
                     ah=_i((lt.get("away") or {}).get("hits"), -1), hh=_i((lt.get("home") or {}).get("hits"), -1))
    except Exception:
        log.exception("Could not parse MLB game")
        return None


def dedupe_mgames(games):
    """MLB lists a rescheduled game twice under the same gamePk: the original slot (Postponed) and the makeup date.
    Keep one entry per game, preferring the one that is not the postponed slot, then the later date."""
    best = {}
    for g in games:
        cur = best.get(g.pk)
        if cur is None:
            best[g.pk] = g
        elif (cur.state == "postponed") != (g.state == "postponed"):
            if cur.state == "postponed":
                best[g.pk] = g
        elif (g.start or now()) > (cur.start or now()):
            best[g.pk] = g
    return list(best.values())


TEAM_ABBR = {}          # MLB team id -> abbreviation (loaded once from the API)
SERIES_SHORT = {"AL Wild Card Series": "ALWC", "NL Wild Card Series": "NLWC", "AL Division Series": "ALDS",
                "NL Division Series": "NLDS", "AL Championship Series": "ALCS", "NL Championship Series": "NLCS",
                "World Series": "WS"}
POST_TYPES = "F,D,L,W"  # wild card, division, league championship, world series
PLAYER_ALIASES = {}   # nickname -> MLB player id; extended from the "player_aliases" setting
LEADER_CATS = {   # user word -> (API category, stat group, label)
    "hr": ("homeRuns", "hitting", "HR"), "avg": ("battingAverage", "hitting", "AVG"),
    "rbi": ("runsBattedIn", "hitting", "RBI"), "h": ("hits", "hitting", "H"), "r": ("runs", "hitting", "R"),
    "sb": ("stolenBases", "hitting", "SB"), "obp": ("onBasePercentage", "hitting", "OBP"),
    "slg": ("sluggingPercentage", "hitting", "SLG"), "ops": ("onBasePlusSlugging", "hitting", "OPS"),
    "2b": ("doubles", "hitting", "2B"), "3b": ("triples", "hitting", "3B"), "bb": ("walks", "hitting", "BB"),
    "k": ("strikeouts", "pitching", "K"), "so": ("strikeouts", "pitching", "K"),
    "era": ("earnedRunAverage", "pitching", "ERA"), "w": ("wins", "pitching", "W"), "sv": ("saves", "pitching", "SV"),
    "whip": ("walksAndHitsPerInningPitched", "pitching", "WHIP"), "hld": ("holds", "pitching", "HLD"),
    "ip": ("inningsPitched", "pitching", "IP"),
}
TEAM_LEADER_LABELS = {("homeRuns", "hitting"): "HR", ("battingAverage", "hitting"): "AVG", ("runsBattedIn", "hitting"): "RBI",
                      ("stolenBases", "hitting"): "SB", ("wins", "pitching"): "W", ("strikeouts", "pitching"): "K",
                      ("earnedRunAverage", "pitching"): "ERA", ("saves", "pitching"): "SV"}


def series_short(desc):
    return SERIES_SHORT.get(desc, desc)


def series_line(g):
    """'ALDS G3: NYY lead 2-1' once the game is final (seriesStatus then includes it); '' for regular season."""
    ss = g.sstat if isinstance(g.sstat, dict) else None
    if not ss or g.gtype == "R":
        return ""
    w, l = ss.get("wins", 0), ss.get("losses", 0)
    team = TEAM_ABBR.get(_i((ss.get("winningTeam") or {}).get("id")), "")
    status = (f"{team} win {w}-{l}" if ss.get("isOver") else f"tied {w}-{l}" if ss.get("isTied") else f"{team} lead {w}-{l}")
    return f"{series_short(g.sdesc)} G{g.sgame}: {status}" if g.sgame else f"{series_short(g.sdesc)}: {status}"


def series_brief(g):
    """'ALDS NYY 2-1' for the !post board."""
    ss = g.sstat if isinstance(g.sstat, dict) else None
    if not ss:
        return ""
    w, l = ss.get("wins", 0), ss.get("losses", 0)
    team = TEAM_ABBR.get(_i((ss.get("winningTeam") or {}).get("id")), "")
    return f"{series_short(g.sdesc)} {'tied' if ss.get('isTied') else team} {w}-{l}"


def short_name(full):
    parts = full.split()
    return f"{parts[0][0]}. {' '.join(parts[1:])}" if len(parts) > 1 else full


TEAM_NAMES = {}         # MLB team id -> {"name", "short", "club", "nick"}
TEAM_ALIASES = {"jays": 141, "bluejays": 141, "sox": 0, "yanks": 147, "cards": 138, "halos": 108, "bucs": 134, "dbacks": 109,
                "snakes": 109, "nats": 120, "bosox": 111, "whitesox": 145, "redsox": 111}


# ==========================================================================
# SHARED MLB PIECES (team / player lookups, follow a player) - used by the team bot and the postseason bot
# ==========================================================================
class MlbBase:
    def _init_mlb(self):
        self.state.data.setdefault("pfollows", {})
        self.pteam = {}                       # player id -> (team id, fetched at)
        self.day_games, self.day_at, self.fstate = [], 0.0, {}       # every game today (only fetched while someone follows a player)
        self.day_extra = {}                   # extra schedule filters (the postseason bot adds gameType)

    # ---------------- team / player lookups ----------------
    async def load_teams(self):
        if not TEAM_ABBR:
            data = await asyncio.to_thread(mlb_get, "/v1/teams", {"sportId": 1, "season": now().year})
            for t in (data or {}).get("teams", []):
                TEAM_ABBR[t["id"]] = t.get("abbreviation", "")
                TEAM_NAMES[t["id"]] = {"name": t.get("name", ""), "short": t.get("teamName", ""),
                                       "club": t.get("clubName", ""), "nick": t.get("shortName", "")}
        return bool(TEAM_ABBR)

    @staticmethod
    def resolve_team(query):
        """Team id for an exact abbreviation / nickname / name ("nyy", "yankees", "blue jays"); 0 if not a team."""
        q = re.sub(r"[^a-z0-9]", "", query.lower())
        if not q:
            return 0
        if q in TEAM_ALIASES:
            return TEAM_ALIASES[q]
        hits = set()
        for tid, abbr in TEAM_ABBR.items():
            n = TEAM_NAMES.get(tid, {})
            keys = {abbr, n.get("name", ""), n.get("short", ""), n.get("club", ""), n.get("nick", "")}
            if q in {re.sub(r"[^a-z0-9]", "", k.lower()) for k in keys if k}:
                hits.add(tid)
        return hits.pop() if len(hits) == 1 else 0

    async def find_player(self, query):
        """('ok', {id,name,team,pos}) | ('many', [people]) | ('none', None). Active MLB players only."""
        await self.load_teams()
        q = query.strip()
        pid = PLAYER_ALIASES.get(q.lower()) or (int(q) if q.isdigit() else 0)
        if pid:
            data = await asyncio.to_thread(mlb_get, f"/v1/people/{pid}", {"hydrate": "currentTeam"})
            people = (data or {}).get("people", [])
        else:
            data = await asyncio.to_thread(mlb_get, "/v1/people/search",
                                           {"names": q, "sportId": 1, "hydrate": "currentTeam"})
            people = [p for p in (data or {}).get("people", [])
                      if p.get("active") and (p.get("currentTeam") or {}).get("id") in TEAM_ABBR]
        if not people:
            return "none", None
        exact = [p for p in people if p.get("fullName", "").lower() == q.lower()]
        pick = exact or people
        if len(pick) > 1:
            return "many", pick[:4]
        p = pick[0]
        return "ok", {"id": p["id"], "name": p["fullName"], "team": _i((p.get("currentTeam") or {}).get("id")),
                      "pos": (p.get("primaryPosition") or {}).get("abbreviation", "")}

    def _pick_msg(self, people):
        items = [f"{short_name(p['fullName'])} {TEAM_ABBR.get(_i((p.get('currentTeam') or {}).get('id')), '')}" for p in people]
        return pack(f"[{self.tag}] Several players:", items) + [f"[{self.tag}] Use the full name, e.g. !follow Aaron Judge"]

    async def player_team(self, pid):
        hit = self.pteam.get(pid)
        if hit and time.time() - hit[1] < 6 * 3600:
            return hit[0]
        data = await asyncio.to_thread(mlb_get, f"/v1/people/{pid}", {"hydrate": "currentTeam"})
        people = (data or {}).get("people", [])
        tid = _i((people[0].get("currentTeam") or {}).get("id")) if people else 0
        if tid:
            self.pteam[pid] = (tid, time.time())
        return tid or (hit[0] if hit else 0)

    # ---------------- follow a player (DMs) ----------------
    def followed(self):
        """{player id: {"name": str, "keys": [contact key, ...]}}"""
        out = {}
        for key, sub in self.state.data["pfollows"].items():
            for p in sub["players"]:
                out.setdefault(p["id"], {"name": p["name"], "keys": []})["keys"].append(key)
        return out

    def _identify(self, inc):
        if inc.is_dm and inc.sender_key:
            return inc.sender_key.lower()
        c = self.hub.t.find_contact(name=inc.sender_name) if inc.sender_name else None
        return c["public_key"] if c else None

    async def cmd_follow(self, inc, arg):
        q = " ".join(inc.rest).strip()
        if not q:
            return [f"[{self.tag} BOT] !follow PLAYER (e.g. !follow judge, !follow pca). DMs: HR, RBI, final line"]
        status, p = await self.find_player(q)
        if status == "none":
            return [f"[{self.tag} BOT] No active MLB player found for '{q}'."]
        if status == "many":
            return self._pick_msg(p)
        key = self._identify(inc)
        if not key:
            return [f"[{self.tag} BOT] I can't find you in my contacts. DM me !{self.name} follow {q} (send an advert first)."]
        sub = self.state.data["pfollows"].setdefault(key, {"name": inc.sender_name, "players": []})
        sub["name"] = inc.sender_name or sub.get("name", "")
        if any(x["id"] == p["id"] for x in sub["players"]):
            return [f"[{self.tag} BOT] Already following {short_name(p['name'])}."]
        if len(sub["players"]) >= CFG["max_follows"]:
            return [f"[{self.tag} BOT] Max {CFG['max_follows']} players. !unfollow one first."]
        sub["players"].append({"id": p["id"], "name": p["name"]})
        self.pteam[p["id"]] = (p["team"], time.time())
        self.state.save()
        return [f"[{self.tag} BOT] Following {short_name(p['name'])} ({TEAM_ABBR.get(p['team'], '')}): HR/RBI/final line by DM."]

    async def cmd_unfollow(self, inc, arg):
        key = self._identify(inc)
        sub = self.state.data["pfollows"].get(key or "")
        if not sub:
            return [f"[{self.tag} BOT] You're not following any players."]
        q = " ".join(inc.rest).lower()
        sub["players"] = [x for x in sub["players"] if q not in x["name"].lower()] if q else []
        if not sub["players"]:
            del self.state.data["pfollows"][key]
        self.state.save()
        left = ", ".join(short_name(x["name"]) for x in sub["players"])
        return [f"[{self.tag} BOT] Updated. " + (f"Following: {left}" if left else "No players left.")]

    async def cmd_following(self, inc, arg):
        sub = self.state.data["pfollows"].get(self._identify(inc) or "")
        if not sub:
            return [f"[{self.tag} BOT] Not following any players. !follow NAME"]
        return [f"[{self.tag} BOT] Following: " + ", ".join(short_name(x["name"]) for x in sub["players"])]

    async def refresh_day(self, max_age=30):
        """Every MLB game today (only fetched while someone follows a player)."""
        if time.time() - self.day_at < max_age:
            return self.day_games
        if not await self.load_teams():
            return None
        start, end = sched_days()
        data = await asyncio.to_thread(mlb_get, "/v1/schedule", {
            "sportId": 1, "startDate": start, "endDate": end, "hydrate": "linescore,team,decisions", **self.day_extra})
        if data is None:
            return None
        self.day_games = dedupe_mgames([x for x in (parse_mgame(g) for day in data.get("dates", []) for g in day.get("games", [])) if x])
        self.day_at = time.time()
        return self.day_games

    async def process_follows(self):
        """DM followers about their players; returns the games involved (to pace polling)."""
        fol = self.followed()
        if not fol:
            return []
        day = await self.refresh_day()
        if day is None:
            return []
        teams = {t for t in [await self.player_team(pid) for pid in fol] if t}
        ok = getattr(self, "phase_ok", lambda g: True)
        mine = [g for g in day if (g.away_id in teams or g.home_id in teams) and ok(g)]
        for g in mine:
            st = self.fstate.get(g.pk)
            if st is None:
                st = self.fstate[g.pk] = {"seen": -1, "k": {}, "entered": set(), "final": g.final}
                if g.live or g.final:  # bot started mid-game: catch up silently
                    await self.follow_plays(g, st, fol, silent=True)
                continue
            if g.live:
                await self.follow_plays(g, st, fol, silent=False)
            if g.final and not st["final"]:
                await self.follow_plays(g, st, fol, silent=False)  # any last plays, then the box-score lines
                st["final"] = True
                await self.follow_final(g, fol)
        for pk in set(self.fstate) - {g.pk for g in day}:
            del self.fstate[pk]
        return mine

    def follow_dm(self, info, head, desc, score, inn):
        tail = f" | {score} | {inn}"
        room = CFG["max_msg_bytes"] - len(f"{head} {tail}".encode("utf-8"))
        text = f"{head} {trim_to(desc, room)}{tail}" if desc and room > 12 else f"{head}{tail}"
        for key in info["keys"]:
            self.hub.say_dm(key, text)

    async def follow_plays(self, g, st, fol, silent):
        data = await asyncio.to_thread(mlb_get, f"/v1.1/game/{g.pk}/feed/live", {
            "fields": "liveData,plays,allPlays,about,atBatIndex,inning,halfInning,isComplete,result,eventType,description,"
                      "rbi,awayScore,homeScore,matchup,batter,pitcher,id,fullName"})
        try:
            plays = data["liveData"]["plays"]["allPlays"]
        except (TypeError, KeyError):
            return
        for p in plays:
            about = p.get("about", {})
            idx = about.get("atBatIndex", -1)
            if idx <= st["seen"] or not about.get("isComplete"):
                continue
            st["seen"] = idx
            res, mu = p.get("result", {}), p.get("matchup", {})
            ev = res.get("eventType", "")
            bid, pid = _i((mu.get("batter") or {}).get("id")), _i((mu.get("pitcher") or {}).get("id"))
            score = f"{g.away} {_i(res.get('awayScore'))}-{_i(res.get('homeScore'))} {g.home}"
            inn = f"{'Top' if about.get('halfInning') == 'top' else 'Bot'} {ord_(_i(about.get('inning')))}"
            if bid in fol and not silent:
                if ev == "home_run":
                    self.follow_dm(fol[bid], "💥 HR!", res.get("description", ""), score, inn)
                elif _i(res.get("rbi")) > 0:
                    self.follow_dm(fol[bid], "⚾ RBI:", res.get("description", ""), score, inn)
            if pid in fol:
                first = pid not in st["entered"]
                st["entered"].add(pid)
                name = short_name(fol[pid]["name"])
                if first and not silent:
                    self.follow_dm(fol[pid], f"🧢 {name} on the mound", "", f"{g.away} @ {g.home}", inn)
                if ev.startswith("strikeout"):
                    k = st["k"][pid] = st["k"].get(pid, 0) + 1
                    if k % 5 == 0 and not silent:
                        self.follow_dm(fol[pid], f"🔥 {name} {k} K", "", score, inn)

    async def follow_final(self, g, fol):
        data = await asyncio.to_thread(mlb_get, f"/v1/game/{g.pk}/boxscore")
        teams = (data or {}).get("teams", {})
        score = f"{g.away} {g.ar}-{g.hr} {g.home}"
        for pid, info in fol.items():
            for side in ("away", "home"):
                pl = ((teams.get(side) or {}).get("players") or {}).get(f"ID{pid}")
                if not pl:
                    continue
                stats = pl.get("stats", {})
                b, pt = stats.get("batting") or {}, stats.get("pitching") or {}
                lines = []
                if _i(b.get("plateAppearances")) > 0 and b.get("summary"):
                    lines.append(b["summary"])
                if pt.get("inningsPitched") not in (None, "", "0.0") and pt.get("summary"):
                    lines.append(f"{pt['summary']} {pt.get('note', '')}".strip())
                for line in lines:
                    for key in info["keys"]:
                        self.hub.say_dm(key, fit(f"🏁 {short_name(info['name'])}: {line} | {score}"))

    async def scoring_play(self, g):
        total = g.ar + g.hr
        for attempt in (0, 1):
            data = await asyncio.to_thread(mlb_get, f"/v1.1/game/{g.pk}/feed/live", {
                "fields": "liveData,plays,scoringPlays,allPlays,result,description,awayScore,homeScore"})
            try:
                plays = data["liveData"]["plays"]
                res = plays["allPlays"][plays["scoringPlays"][-1]]["result"]
                if _i(res.get("awayScore")) + _i(res.get("homeScore")) == total:
                    return res.get("description", "")
            except (TypeError, KeyError, IndexError):
                pass
            if attempt == 0:
                await asyncio.sleep(FEED_LAG_WAIT)  # the feed can lag the scoreboard by a few seconds
        return None


class MlbModule(MlbBase, PhaseMixin, ModeMixin):
    """One MLB team's games: live scores, every-3-innings results, finals, standings, leaders, player follows.
    Config: team = "TOR" (abbreviation or nickname), tag = "JAYS" (text shown in messages; default the abbreviation)."""
    type_name, label = "mlb", "MLB baseball"
    kinds, min_blurb = BASEBALL_KINDS, "score every 3 innings + final"
    arg_cmds = ("!standings", "!follow", "!player", "!leaders", "!series")
    defer_cmds = ("!series", "!bracket")      # on a channel shared with the postseason bot, its fuller answer wins

    def __init__(self, hub, name):
        self.hub, self.name = hub, name
        cfg = CFG["modules"][name]
        self.cfg = cfg
        self.channel = cfg["channel"]
        self.team_q = str(cfg.get("team", "TOR"))
        self.team_id = _i(self.team_q) or self.resolve_team(self.team_q) or 0      # filled in by load_teams() if still unknown
        self.abbr = self.team_q.upper() if not self.team_q.isdigit() and len(self.team_q) <= 3 else ""
        self.tag = str(cfg.get("tag") or self.abbr or self.team_q.upper())
        self.label = cfg.get("label") or self.team_q
        self.blurb = f"{self.label} scores"
        self.digest_hour = int(cfg.get("digest_hour", 10))
        PLAYER_ALIASES.update({str(k).lower(): int(v) for k, v in (CFG.get("player_aliases") or {}).items()})
        self.state = State(f"{name}_state.json")
        self._init_phases()
        self.games, self.games_at, self.ok_at = [], 0.0, 0.0
        self.gstate = {}
        self.standings, self.standings_at = None, 0.0
        self._init_mlb()
        self.thirds = {}                      # game -> the 3rd / 6th innings already reported (for "min")
        self.commands = {
            "!score": self.cmd_score, "!scores": self.cmd_score, "!next": self.cmd_next, "!last": self.cmd_last,
            "!standings": self.cmd_standings, "!mode": self.cmd_mode, "!ping": self.cmd_ping,
            "!botstatus": self.cmd_botstatus, "!help": self.cmd_help,
            "!leaders": self.cmd_leaders, "!player": self.cmd_player,
            "!follow": self.cmd_follow, "!unfollow": self.cmd_unfollow, "!following": self.cmd_following,
            "!phase": self.cmd_phase, "!series": self.cmd_series, "!bracket": self.cmd_bracket,
        }

    def game_phase(self, g):
        return "pre" if g.gtype in ("S", "E") else "post" if g.gtype in ("F", "D", "L", "W") else "reg"

    async def load_teams(self):
        ok = await super().load_teams()
        self.ensure_team()
        return ok

    @property
    def code(self):
        """Short team code for messages (TOR)."""
        return self.abbr or TEAM_ABBR.get(self.team_id) or self.tag

    def ensure_team(self):
        """Resolve the configured team once the team list is loaded; False if it is not a known MLB team."""
        if not self.team_id:
            self.team_id = self.resolve_team(self.team_q)
        if self.team_id and not self.abbr:
            self.abbr = TEAM_ABBR.get(self.team_id, "")
        return bool(self.team_id)

    def status_extra(self):
        return ""

    def say_chan(self, texts):
        self.hub.say_chan(self.channel, texts)

    # ---------------- data ----------------
    async def refresh(self, max_age=25):
        if time.time() - self.games_at < max_age:
            return self.games
        if not self.ensure_team():
            return None
        today = now().date()
        data = await asyncio.to_thread(mlb_get, "/v1/schedule", {
            "sportId": 1, "teamId": self.team_id, "hydrate": "linescore,team,decisions,probablePitcher,seriesStatus",
            "startDate": (today - timedelta(days=8)).isoformat(), "endDate": (today + timedelta(days=10)).isoformat()})
        if data is None:
            return None
        games = dedupe_mgames([x for x in (parse_mgame(g) for day in data.get("dates", []) for g in day.get("games", [])) if x])
        self.games = sorted(games, key=lambda g: g.start or now())
        self.games_at = self.ok_at = time.time()
        return self.games

    def today(self, games=None):
        d = now().date()
        return [g for g in (games if games is not None else self.games) if g.start and g.start.date() == d]

    def opp(self, g):
        return f"vs {g.away} {when(g)}" if g.is_home(self.team_id) else f"@ {g.home} {when(g)}"

    @staticmethod
    def line(g):
        if g.live:
            return f"{g.away} {g.ar} @ {g.home} {g.hr} | {g.inn_txt}{' (' + g.detail + ')' if g.delayed else ''}"
        if g.final:
            return f"FINAL{'/' + str(g.inning) if g.inning > 9 else ''}: {g.away} {g.ar} @ {g.home} {g.hr}"
        if g.state == "postponed":
            return f"{g.away} @ {g.home} POSTPONED"
        sp = f" | {g.away_sp} v {g.home_sp}" if g.away_sp and g.home_sp else ""
        return f"{g.away} @ {g.home} {when(g)}{sp}"

    # ---------------- commands ----------------
    async def cmd_score(self, inc, arg):
        allg = await self.refresh()
        if allg is None:
            return [f"[{self.tag}] MLB data unavailable, try again shortly."]
        games = self.today(allg)
        if not games:
            nxt = [g for g in allg if g.scheduled and g.start and g.start > now() and self.phase_ok(g)]
            return [f"[{self.tag}] No game today." + (f" Next: {self.opp(nxt[0])}" if nxt else "")]
        out = pack(f"[{self.tag}]", [self.line(g) for g in games])
        if len(games) == 1 and games[0].final and games[0].win:
            g = games[0]
            out.append(f"[{self.tag}] W {g.win}, L {g.loss}" + (f", S {g.save}" if g.save else "") + f" | {self.code} {g.rec_of(self.team_id)}")
        return out[: CFG["max_reply_msgs"]]

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
        last5 = "".join("W" if x.won_by(self.team_id) else "L" for x in fin[-5:])
        return [f"[{self.tag} LAST] {'W' if g.won_by(self.team_id) else 'L'} {g.away} {g.ar} @ {g.home} {g.hr}"
                + (f" | W {g.win} L {g.loss}" if g.win else "") + f" | L{len(fin[-5:])}: {last5}"]

    async def _standings(self):
        if self.standings is None or time.time() - self.standings_at > 300:
            data = await asyncio.to_thread(mlb_get, "/v1/standings", {
                "leagueId": "103,104", "season": now().year, "standingsTypes": "regularSeason", "hydrate": "team"})
            if data:
                self.standings, self.standings_at = data.get("records", []), time.time()
        return self.standings

    def _my_division(self, recs):
        """(division key like 'ale', league id) for the configured team, from the standings records."""
        for r in recs:
            if any(t["team"]["id"] == self.team_id for t in r["teamRecords"]):
                key = next((k for k, v in DIVISIONS.items() if v == r["division"]["id"]), "ale")
                return key, r["league"]["id"]
        return "ale", 103

    async def cmd_standings(self, inc, arg):
        recs = await self._standings()
        if not recs:
            return [f"[{self.tag} STANDINGS] Data unavailable."]
        mine, my_league = self._my_division(recs)
        a = arg.lower()
        if a in ("wc", "wildcard"):
            league = "AL" if my_league == 103 else "NL"
            teams = sorted((t for r in recs if r["league"]["id"] == my_league for t in r["teamRecords"] if t.get("wildCardRank")),
                           key=lambda t: int(t["wildCardRank"]))
            items = [f"{t['wildCardRank']}.{t['team']['abbreviation']} {t.get('wildCardGamesBack', '-')}" for t in teams[:6]]
            if not any(t["team"]["id"] == self.team_id for t in teams[:6]):
                items += [f"{t['wildCardRank']}.{self.code} {t.get('wildCardGamesBack', '-')}" for t in teams if t["team"]["id"] == self.team_id]
            return pack(f"[{self.tag} {league} WC]", items)
        # "east" / "central" / "west" mean the division in the bot's own league
        code = DIV_ALIAS.get(a, a) or mine
        if code in ("ale", "alc", "alw") and my_league == 104 and a in DIV_ALIAS:
            code = "nl" + code[2]
        if code not in DIVISIONS:
            return [f"[{self.tag}] !standings [east|central|west|wc|ale|alc|alw|nle|nlc|nlw]"]
        rec = next((r for r in recs if r["division"]["id"] == DIVISIONS[code]), None)
        if not rec:
            return [f"[{self.tag} STANDINGS] Data unavailable."]
        rows = sorted(rec["teamRecords"], key=lambda t: _i(t.get("divisionRank"), 9))
        items = [f"{t['team']['abbreviation']} {t['wins']}-{t['losses']}" + ("" if i == 0 else f" {t['gamesBack']}")
                 for i, t in enumerate(rows)]
        return pack(f"[{self.tag} {code.upper()}]", items)

    async def cmd_ping(self, inc, arg):
        return await self.hub.cmd_ping(self, inc)

    async def cmd_botstatus(self, inc, arg):
        return await self.hub.status(self, self.ok_at)

    async def cmd_help(self, inc, arg):
        return [f"[{self.tag} BOT] !score !next !last !standings !series !bracket !leaders hr !player X !follow X !mode !phase !ping"]

    # ---------------- events ----------------
    def emit(self, kind, texts):
        if kind in self.kinds.get(self.level(), set()):
            self.say_chan(texts)

    async def events(self, g, prev):
        score = f"{g.away} {g.ar}-{g.hr} {g.home}"
        evs = []
        d_a, d_h = g.ar - prev["ar"], g.hr - prev["hr"]
        if d_a < 0 or d_h < 0:
            evs.append(("correction", [f"⚠️ Score update: {score} | {g.inn_txt}".rstrip(" |")]))
        elif d_a or d_h:
            scorer_home = d_h > d_a
            ours_scored = scorer_home == g.is_home(self.team_id)
            n, team = d_a + d_h, (g.home if scorer_home else g.away)
            head = f"{self.tag if ours_scored else team} {'RUN' if n == 1 else f'{n} RUNS'}!"
            tail = f" | {score} | {g.inn_txt}".rstrip(" |")
            desc = await self.scoring_play(g) if n else None
            room = CFG["max_msg_bytes"] - len(f"⚾ {head} {tail}".encode("utf-8"))
            evs.append(("run", [f"⚾ {head}{' ' + trim_to(desc, room) if desc and room > 12 else ''}{tail}"]))

        sent = self.thirds.setdefault(g.pk, set())
        if g.live and not g.delayed:                     # "min": the score after 3 and after 6 innings
            for n3 in (3, 6):
                if n3 not in sent and (g.inning > n3 or (g.inning == n3 and g.inn_txt.startswith("End"))):
                    sent.add(n3)
                    evs.append(("third", [f"⚾ End of {ord_(n3)}: {score}" if g.inning == n3 else f"⚾ {g.inn_txt}: {score}"]))
        if prev["state"] == "preview" and g.state == "live":
            sp = f" | {g.away_sp} v {g.home_sp}" if g.away_sp and g.home_sp else ""
            ser = f" | {series_short(g.sdesc)} G{g.sgame}" if g.gtype != "R" else ""
            evs.append(("start", [fit(f"⚾ Play ball: {g.away} @ {g.home}{ser}{sp}")]))
        elif g.live and g.inning >= 10 and prev["inning"] < 10:
            evs.append(("extras", [f"⏱ Extra innings: {score}"]))
        if g.delayed and not prev["delayed"]:
            evs.append(("delay", [f"🌧 {g.detail}: {score}"]))
        if g.state == "postponed" and prev["state"] != "postponed":
            evs.append(("delay", [f"🌧 {g.away} @ {g.home} postponed"]))
        if g.final and prev["state"] != "final":
            text = f"🛑 FINAL{'/' + str(g.inning) if g.inning > 9 else ''}: {g.away} {g.ar} @ {g.home} {g.hr}"
            if g.win:
                text += f" | W {g.win} L {g.loss}" + (f" S {g.save}" if g.save else "")
            evs.append(("final", [fit(f"{text} | {series_line(g)}" if series_line(g) else f"{text} | {self.code} {g.rec_of(self.team_id)}")]))
        return start_first(evs)

    @staticmethod
    def plain_events(g, prev):
        """What to say if the detailed alert logic hit an unexpected error: just the score change and the final."""
        score, evs = f"{g.away} {g.ar}-{g.hr} {g.home}", []
        if g.ar >= prev["ar"] and g.hr >= prev["hr"] and (g.ar, g.hr) != (prev["ar"], prev["hr"]):
            evs.append(("run", [f"⚾ Run | {score} | {g.inn_txt}".rstrip(" |")]))
        if g.final and prev["state"] != "final":
            evs.append(("final", [f"🛑 FINAL: {g.away} {g.ar} @ {g.home} {g.hr}"]))
        return evs

    async def process(self, games):
        games = [g for g in games if self.phase_ok(g)]     # skip spring training / regular season / playoff games that are switched off
        seen = set()
        for g in games:
            seen.add(g.pk)
            cur = {"ar": g.ar, "hr": g.hr, "state": g.state, "inning": g.inning, "delayed": g.delayed}
            prev, self.gstate[g.pk] = self.gstate.get(g.pk), cur
            if prev is None:
                self.thirds[g.pk] = {n for n in (3, 6) if g.live and n < g.inning}      # already past when we started watching
                continue  # first sighting: baseline only
            try:
                evs = await self.events(g, prev)
            except Exception:
                log.exception("%s: alert logic failed for game %s - sending a plain update", self.name, g.pk)
                evs = self.plain_events(g, prev)
            for kind, texts in evs:
                self.emit(kind, texts)
        for pk in set(self.gstate) - seen:
            del self.gstate[pk]
            self.thirds.pop(pk, None)

    # ---------------- league leaders / player lines ----------------
    async def cmd_leaders(self, inc, arg):
        rest = [w.lower() for w in inc.rest]
        if not rest:
            return [f"[{self.tag}] !leaders hr|avg|rbi|h|r|sb|obp|slg|ops|2b|bb|k|era|w|sv|whip [al|nl]  or  !leaders team"]
        await self.load_teams()
        year = now().year
        if rest[0] in ("team", self.tag.lower(), self.name):
            data = await asyncio.to_thread(mlb_get, f"/v1/teams/{self.team_id}/leaders", {
                "leaderCategories": "homeRuns,battingAverage,runsBattedIn,stolenBases,wins,strikeouts,earnedRunAverage,saves",
                "season": year, "limit": 1})
            items = []
            for c in (data or {}).get("teamLeaders", []):
                label = TEAM_LEADER_LABELS.get((c.get("leaderCategory"), c.get("statGroup")))
                if label and c.get("leaders"):
                    l = c["leaders"][0]
                    items.append(f"{label} {last_name(l['person']['fullName'])} {l['value']}")
            return pack(f"[{self.tag} TEAM]", items) if items else [f"[{self.tag}] Team leaders unavailable right now."]
        cat = LEADER_CATS.get(rest[0])
        if not cat:
            return [f"[{self.tag}] Unknown stat. Send !leaders for the list."]
        league = {"al": 103, "nl": 104}.get(rest[1]) if len(rest) > 1 else None
        params = {"leaderCategories": cat[0], "season": year, "sportId": 1, "limit": 5, "statGroup": cat[1]}
        if league:
            params["leagueId"] = league
        data = await asyncio.to_thread(mlb_get, "/v1/stats/leaders", params)
        leaders = (((data or {}).get("leagueLeaders") or [{}])[0]).get("leaders") or []
        if not leaders:
            return [f"[{self.tag}] Leaders unavailable right now."]
        items = [f"{l['rank']}.{last_name(l['person']['fullName'])} {TEAM_ABBR.get(_i(l['team'].get('id')), '')} {l['value']}"
                 for l in leaders]
        return pack(f"[{self.tag} {cat[2]}{' ' + rest[1].upper() if league else ''}]", items)

    async def cmd_player(self, inc, arg):
        q = " ".join(inc.rest).strip()
        if not q:
            return [f"[{self.tag}] !player NAME (e.g. !player judge) - season stat line"]
        status, p = await self.find_player(q)
        if status == "none":
            return [f"[{self.tag}] No active MLB player found for '{q}'."]
        if status == "many":
            return self._pick_msg(p)
        groups = ["pitching"] if p["pos"] == "P" else ["hitting", "pitching"] if p["pos"] == "TWP" else ["hitting"]
        lines = []
        for grp in groups:
            data = await asyncio.to_thread(mlb_get, f"/v1/people/{p['id']}/stats",
                                           {"stats": "season", "group": grp, "season": now().year})
            splits = (((data or {}).get("stats") or [{}])[0]).get("splits") or []
            if not splits:
                continue
            st = splits[0]["stat"]
            body = (f"{st.get('avg', '-')} {st.get('homeRuns', 0)}HR {st.get('rbi', 0)}RBI {st.get('stolenBases', 0)}SB "
                    f"{st.get('ops', '-')}OPS" if grp == "hitting" else
                    f"{st.get('wins', 0)}-{st.get('losses', 0)} {st.get('era', '-')}ERA {st.get('inningsPitched', '0')}IP "
                    f"{st.get('strikeOuts', 0)}K {st.get('whip', '-')}WHIP")
            lines.append(f"[{self.tag}] {short_name(p['name'])} {TEAM_ABBR.get(p['team'], '')} {p['pos']}: {body}")
        return lines[: CFG["max_reply_msgs"]] or [f"[{self.tag}] No {now().year} stats yet for {p['name']}."]

    # ---------------- postseason series / bracket ----------------
    async def cmd_series(self, inc, arg, everything=False):
        """!series: this team's postseason series (or, if it isn't in the playoffs, the series being played).
        !bracket / !series all: every series, latest round first, this team's first."""
        from .mlb_postseason import ROUND_ORDER, build_series, series_item
        await self.load_teams()
        n = now()
        data = await asyncio.to_thread(mlb_get, "/v1/schedule/postseason/series",
                                       {"season": n.year if n.month >= 3 else n.year - 1, "hydrate": "team"})
        series = build_series(data)
        if not series:
            return [f"[{self.tag}] The postseason bracket isn't available yet."]
        mine = [x for x in series if self.team_id in (x.a, x.b)]
        if everything or arg.lower() in ("all", "bracket"):
            rest = sorted((x for x in series if x not in mine and x.known), key=lambda x: (-ROUND_ORDER.get(x.gtype, 0), x.sid))
            pool = sorted(mine, key=lambda x: -ROUND_ORDER.get(x.gtype, 0)) + rest
        elif mine:
            pool = [max(mine, key=lambda x: ROUND_ORDER.get(x.gtype, 0))]      # its furthest series
        else:
            pool = [x for x in series if x.known and x.started and not x.over] or [x for x in series if x.over][-3:]
            if not pool:
                return [f"[{self.tag}] No postseason series to show yet."]
        return pack(f"[{self.tag}]", [series_item(x) for x in pool])

    async def cmd_bracket(self, inc, arg):
        return await self.cmd_series(inc, arg, everything=True)

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
                await self.load_teams()
                if not self.ensure_team():
                    log.error("%s: %r is not an MLB team (use an abbreviation like NYY or a nickname)", self.name, self.team_q)
                    await asyncio.sleep(CFG["poll_idle_seconds"])
                    continue
                await self.check_digest()
                delays = []
                games = await self.refresh(0)
                if games is not None:
                    await self.process(games)
                    delays.append(self.next_delay(games))
                followed_games = await self.process_follows()
                if followed_games:
                    delays.append(self.next_delay(followed_games))
                delay = min(delays) if delays else delay
            except Exception:
                log.exception(f"{self.name} poll loop")
            await asyncio.sleep(delay)

