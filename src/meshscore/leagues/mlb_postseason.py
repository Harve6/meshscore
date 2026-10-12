"""MLB postseason: every team, every round, the World Series in full."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta


from ..core import CFG, DIGEST_GRACE_HOURS, ModeMixin, POST_KINDS, State, _i, fit, fmt_day, fmt_time, now, ord_, pack, sched_days, start_first, when
from ..hub import Hub
from .mlb import LEADER_CATS, MlbBase, POST_TYPES, TEAM_ABBR, TEAM_NAMES, dedupe_mgames, last_name, mlb_get, parse_mgame, series_brief, series_line, series_short, short_name, trim_to
from ..radio import Transport, chan_key

log = logging.getLogger("meshscore")


# ==========================================================================
# MLB POSTSEASON - every team, every round, the World Series in full.  Module type "postseason".
# Switch it on or off at startup (sports menu / --postseason / "enabled" in config.json). Outside October it stays idle.
# ==========================================================================
POST_DM_KINDS = {"run", "start", "final", "series"}          # what team followers get by DM
POST_TIERS = {"F": "merged", "D": "key", "L": "lead", "W": "full"}   # per round: how much of each game the channel gets
ROUND_LEN = {"F": 3, "D": 5, "L": 7, "W": 7}                  # best-of
ROUND_ORDER = {"F": 0, "D": 1, "L": 2, "W": 3}
ROUND_SHORT = {"F": "WC", "D": "DS", "L": "CS", "W": "WS"}


def game_tag(g):
    return series_short(g.sdesc) or ROUND_SHORT.get(g.gtype, "MLB")


@dataclass
class Series:
    sid: str
    gtype: str
    games: list
    a: int = 0                 # team ids (a = away team of the first game)
    b: int = 0
    a_txt: str = "TBD"         # what MLB calls the slot until the team is decided ("AL Low", "CWS/CLE")
    b_txt: str = "TBD"
    wa: int = 0                # wins so far
    wb: int = 0

    @property
    def short(self): return series_short(self.games[0].sdesc) if self.games and self.games[0].sdesc else ROUND_SHORT.get(self.gtype, "")
    @property
    def need(self): return ROUND_LEN.get(self.gtype, 7) // 2 + 1
    @property
    def known(self): return self.a in TEAM_ABBR and self.b in TEAM_ABBR
    @property
    def over(self): return self.known and max(self.wa, self.wb) >= self.need
    @property
    def started(self): return self.wa + self.wb > 0 or any(g.live for g in self.games)
    @property
    def winner(self): return (self.a if self.wa > self.wb else self.b) if self.over else 0
    @property
    def next_game(self): return next((g for g in self.games if g.state in ("preview", "live")), None)
    def label(self, tid): return TEAM_ABBR.get(tid) or (self.a_txt if tid == self.a else self.b_txt)


def build_series(data):
    out = []
    for s in (data or {}).get("series", []):
        info = s.get("series", {})
        gs = dedupe_mgames([x for x in (parse_mgame(g) for g in s.get("games", [])) if x])
        gs.sort(key=lambda g: (g.sgame, g.start.timestamp() if g.start else 0))
        if not gs:
            continue
        se = Series(sid=info.get("id", ""), gtype=info.get("gameType") or gs[0].gtype, games=gs,
                    a=gs[0].away_id, b=gs[0].home_id, a_txt=gs[0].away, b_txt=gs[0].home)
        first = next((g for g in gs if g.away_id in TEAM_ABBR and g.home_id in TEAM_ABBR), None)
        if first:
            se.a, se.b, se.a_txt, se.b_txt = first.away_id, first.home_id, first.away, first.home
        for g in gs:
            if g.final and {g.away_id, g.home_id} == {se.a, se.b}:
                if (g.home_id if g.hr > g.ar else g.away_id) == se.a:
                    se.wa += 1
                else:
                    se.wb += 1
        out.append(se)
    out.sort(key=lambda s: (ROUND_ORDER.get(s.gtype, 9), s.sid))
    return out


def series_item(s):
    A, B = s.label(s.a), s.label(s.b)
    if not s.known:
        return f"{s.short} {A} v {B}"
    if s.over:
        w, l = (A, B) if s.wa > s.wb else (B, A)
        return f"{s.short} {w} def {l} {max(s.wa, s.wb)}-{min(s.wa, s.wb)}"
    if not s.started:
        ng = s.next_game
        return f"{s.short} {A} v {B}" + (f" {ng.start.strftime('%a')}" if ng and ng.start else "")
    if s.wa == s.wb:
        return f"{s.short} {A}-{B} tied {s.wa}-{s.wb}"
    lead, trail = (A, B) if s.wa > s.wb else (B, A)
    return f"{s.short} {lead} {max(s.wa, s.wb)}-{min(s.wa, s.wb)} {trail}"


class PostseasonModule(MlbBase, ModeMixin):
    tag, blurb, label = "MLB", "MLB postseason scores", "MLB Postseason (all teams)"
    kinds, min_blurb = POST_KINDS, "score every 3 innings + final"
    aliases = ("ps", "mlb")
    arg_cmds = ("!follow", "!player", "!leaders", "!team", "!series")

    def __init__(self, hub, name="postseason"):
        cfg = CFG["modules"][name]
        self.hub, self.name, self.cfg = hub, name, cfg
        self.channel = cfg["channel"]
        self.state = State(f"{name}_state.json")
        self._init_mlb()
        self.day_extra = {"gameType": POST_TYPES}
        self.tiers = {**POST_TIERS, **(cfg.get("tiers") or {})}
        self.skip_teams = {str(x).upper() for x in (cfg.get("skip_teams") or [])}   # teams that have their own channel
        self.digest_hour = int(cfg.get("digest_hour", 10))
        self.headsup_min = int(cfg.get("headsup_minutes", 30))
        self.games, self.games_at, self.ok_at = [], 0.0, 0.0
        self.series, self.series_at = [], 0.0
        self.gstate, self.pending = {}, {}
        self.done, self.started, self.headsup, self.walkoff, self.nohit = set(), set(), set(), set(), {}
        self.busy, self.busy_announced = False, ""
        self.thirds = {}
        self._warned = False
        self.commands = {
            "!scores": self.cmd_scores, "!score": self.cmd_scores, "!today": self.cmd_scores,
            "!series": self.cmd_series, "!bracket": self.cmd_bracket, "!ws": self.cmd_ws, "!worldseries": self.cmd_ws,
            "!next": self.cmd_next, "!yesterday": self.cmd_yesterday, "!pitchers": self.cmd_pitchers,
            "!team": self.cmd_team, "!leaders": self.cmd_leaders, "!player": self.cmd_player,
            "!follow": self.cmd_follow, "!unfollow": self.cmd_unfollow, "!following": self.cmd_following,
            "!mode": self.cmd_mode, "!ping": self.cmd_ping, "!botstatus": self.cmd_botstatus, "!help": self.cmd_help,
        }

    def status_extra(self):
        return " | BUSY" if self.busy else ""

    def season(self):
        return int(self.cfg.get("season") or now().year)

    def say_chan(self, texts):
        chans = getattr(self.hub.t, "chans", None)
        if chans and chan_key(self.channel) not in chans:   # the channel is not on the radio: do not jam the send queue
            if not self._warned:
                self._warned = True
                log.warning("Channel %s is not on the radio; postseason messages are not being sent", self.channel)
            return
        self.hub.say_chan(self.channel, texts)

    # ---------------- data ----------------
    async def refresh_live(self, max_age=25):
        """Postseason games from yesterday (after midnight) and today, teams known only."""
        if time.time() - self.games_at < max_age:
            return self.games
        if not await self.load_teams():
            return None
        start, end = sched_days()
        data = await asyncio.to_thread(mlb_get, "/v1/schedule", {
            "sportId": 1, "gameType": POST_TYPES, "startDate": start, "endDate": end,
            "hydrate": "linescore,team,decisions,probablePitcher,seriesStatus"})
        if data is None:
            return None
        games = dedupe_mgames([x for x in (parse_mgame(g) for day in data.get("dates", []) for g in day.get("games", []))
                               if x and x.away_id in TEAM_ABBR and x.home_id in TEAM_ABBR
                               and not ({x.away, x.home} & self.skip_teams)])   # skips "AL Low"-style placeholder teams
        self.games = sorted(games, key=lambda g: g.start or now())
        self.games_at = self.ok_at = time.time()
        return self.games

    async def get_series(self, max_age=120):
        if self.series and time.time() - self.series_at < max_age:
            return self.series
        if not await self.load_teams():
            return self.series
        data = await asyncio.to_thread(mlb_get, "/v1/schedule/postseason/series", {"season": self.season(), "hydrate": "team"})
        if data:
            self.series, self.series_at = build_series(data), time.time()
        return self.series

    @staticmethod
    def series_of(g, series):
        return next((s for s in series if any(x.pk == g.pk for x in s.games)), None)

    async def series_pre(self, g):
        """Series score before this game, e.g. 'LAD lead 3-2' ('' for game 1)."""
        s = self.series_of(g, await self.get_series(max_age=20))
        if not s or not s.known or s.wa + s.wb == 0:
            return ""
        if s.wa == s.wb:
            return f"Series tied {s.wa}-{s.wb}"
        lead = s.label(s.a if s.wa > s.wb else s.b)
        return f"{lead} lead {max(s.wa, s.wb)}-{min(s.wa, s.wb)}"

    def tier_of(self, g):
        return self.tiers.get(g.gtype, "key")

    def game_level(self, g):
        """Busy day: Wild Card / Division games drop from max to min (the championship series and World Series never do)."""
        lvl = self.level()
        return "min" if lvl == "max" and self.busy and self.tier_of(g) in ("merged", "key") else lvl

    def followers(self, g):
        ids = {TEAM_ABBR.get(g.away_id), TEAM_ABBR.get(g.home_id)}
        return [k for k, sub in self.state.data["subs"].items() if set(sub.get("teams", [])) & ids]

    async def board(self):
        games = await self.refresh_live()
        if games is None:
            return None
        d = now().date()
        return [g for g in games if g.live or (g.start and g.start.date() == d)]

    @staticmethod
    def board_line(g):
        if g.live:
            return f"{g.away} {g.ar}-{g.hr} {g.home} {g.inn_txt}" + (" (delay)" if g.delayed else "")
        if g.final:
            return f"{g.away} {g.ar}-{g.hr} {g.home} F{'/' + str(g.inning) if g.inning > 9 else ''}"
        if g.state == "postponed":
            return f"{g.away}@{g.home} PPD"
        return f"{g.away}@{g.home} {fmt_time(g.start)}"

    # ---------------- commands ----------------
    async def cmd_scores(self, inc, arg):
        games = await self.board()
        if games is None:
            return ["[MLB] MLB data unavailable, try again shortly."]
        if not games:
            series = await self.get_series()
            nxt = sorted((g for s in series for g in s.games if g.state == "preview" and g.start and g.start > now()
                          and g.away_id in TEAM_ABBR and g.home_id in TEAM_ABBR), key=lambda g: g.start)
            if nxt:
                g = nxt[0]
                return [f"[MLB] No postseason games today. Next: {game_tag(g)} {g.away}@{g.home} {when(g)}"]
            first = min((g for s in series for g in s.games if g.start), key=lambda g: g.start, default=None)
            return ["[MLB] No postseason games today." + (f" Postseason starts {fmt_day(first.start)}." if first and first.start > now() else "")]
        tags = list(dict.fromkeys(game_tag(g) for g in games))
        items = [(f"{game_tag(g)} " if len(tags) > 1 else "") + self.board_line(g) for g in games]
        return pack(f"[MLB {tags[0]}]" if len(tags) == 1 else "[MLB]", items)

    async def cmd_series(self, inc, arg, everything=False):
        series = await self.get_series()
        if not series:
            return ["[MLB] The postseason bracket isn't available yet."]
        a = arg.lower()
        want = {"al": lambda s: s.short.startswith("AL"), "nl": lambda s: s.short.startswith("NL"),
                "ws": lambda s: s.gtype == "W", "wc": lambda s: s.gtype == "F", "ds": lambda s: s.gtype == "D",
                "cs": lambda s: s.gtype == "L"}.get(a)
        if a and not want:
            return ["[MLB] !series [al|nl|wc|ds|cs|ws]  or  !bracket"]
        pool = [s for s in series if want(s)] if want else series
        if not everything and not want:
            active = [s for s in pool if s.known and s.started and not s.over]
            if active:
                pool = active
            else:
                finished = [s for s in pool if s.over]
                top = max((ROUND_ORDER.get(s.gtype, 0) for s in finished), default=None)
                pool = [s for s in finished if ROUND_ORDER.get(s.gtype, 0) == top] if top is not None else \
                    [s for s in pool if s.known][:4]
        pool = [s for s in pool if s.known or want or everything]
        if everything:
            pool.sort(key=lambda s: (-ROUND_ORDER.get(s.gtype, 0), s.sid))   # latest round first: a long list never cuts the World Series
        if not pool:
            return ["[MLB] No postseason series to show yet."]
        return pack("[MLB]", [series_item(s) for s in pool])

    async def cmd_bracket(self, inc, arg):
        return await self.cmd_series(inc, arg, everything=True)

    async def cmd_ws(self, inc, arg):
        series = await self.get_series()
        ws = next((s for s in series if s.gtype == "W"), None)
        if not ws:
            return ["[WS] The World Series bracket isn't posted yet."]
        if not ws.known:
            left = sum(1 for s in series if s.gtype != "W" and not s.over)
            return [f"[WS] World Series starts {fmt_day(ws.games[0].start)}. Teams TBD ({left} series still to play)."]
        A, B = ws.label(ws.a), ws.label(ws.b)
        if ws.over:
            head = f"[WS] 🏆 {TEAM_NAMES.get(ws.winner, {}).get('name') or TEAM_ABBR[ws.winner]} won the World Series {max(ws.wa, ws.wb)}-{min(ws.wa, ws.wb)}"
        else:
            lead, trail, lw, tw = (A, B, ws.wa, ws.wb) if ws.wa >= ws.wb else (B, A, ws.wb, ws.wa)
            head = f"[WS] {lead} {lw}-{tw} {trail}"
            ng = ws.next_game
            if ng:
                live = next((x for x in (await self.refresh_live() or []) if x.pk == ng.pk), None)
                if live and live.live:
                    head += f" | G{ng.sgame} LIVE {live.away} {live.ar}-{live.hr} {live.home} {live.inn_txt}"
                else:
                    sp = f" | {live.away_sp} v {live.home_sp}" if live and live.away_sp and live.home_sp else ""
                    head += f" | G{ng.sgame} {when(ng)} @ {ng.home}{sp}"
        results = [f"G{g.sgame} {g.away} {g.ar}-{g.hr} {g.home}" for g in ws.games if g.final]
        return ([fit(head)] + (pack("[WS]", results, max_msgs=1) if results else []))[: CFG["max_reply_msgs"]]

    async def cmd_next(self, inc, arg):
        series = await self.get_series()
        nxt = sorted((g for s in series for g in s.games if g.state == "preview" and g.start and g.start > now()
                      and g.away_id in TEAM_ABBR and g.home_id in TEAM_ABBR), key=lambda g: g.start)[:4]
        if not nxt:
            return ["[MLB] No upcoming postseason games with teams set yet."]
        return pack("[MLB NEXT]", [f"{game_tag(g)} G{g.sgame} {g.away}@{g.home} {when(g)}" for g in nxt])

    async def cmd_yesterday(self, inc, arg):
        d = (now().date() - timedelta(days=1)).isoformat()
        data = await asyncio.to_thread(mlb_get, "/v1/schedule", {
            "sportId": 1, "gameType": POST_TYPES, "date": d, "hydrate": "linescore,team,decisions,seriesStatus"})
        if data is None:
            return ["[MLB] MLB data unavailable, try again shortly."]
        await self.load_teams()
        games = [x for x in (parse_mgame(g) for day in data.get("dates", []) for g in day.get("games", [])) if x and x.final]
        if not games:
            return ["[MLB] No postseason games yesterday."]
        out = pack("[MLB YDAY]", [f"{g.away} {g.ar}-{g.hr} {g.home}{'/' + str(g.inning) if g.inning > 9 else ''}" for g in games], max_msgs=1)
        briefs = list(dict.fromkeys(b for b in (series_brief(g) for g in games) if b))
        return out + (pack("[SERIES]", briefs, max_msgs=1) if briefs else [])

    async def cmd_pitchers(self, inc, arg):
        games = await self.board()
        items = [f"{g.away} {g.away_sp} v {g.home} {g.home_sp}" for g in games or [] if g.scheduled and g.away_sp and g.home_sp]
        return pack("[MLB SP]", items) if items else ["[MLB] No probable pitchers posted for today's games yet."]

    async def cmd_team(self, inc, arg):
        q = " ".join(inc.rest).strip()
        if not q:
            return ["[MLB] !team NYY (or a name: !team yankees) - series status and next game"]
        await self.load_teams()
        tid = self.resolve_team(q)
        if not tid:
            return [f"[MLB] Team '{q}' not found. Use the abbreviation, e.g. !team NYY"]
        me = TEAM_ABBR[tid]
        mine = [s for s in await self.get_series() if s.known and tid in (s.a, s.b)]
        if not mine:
            return [f"[MLB] {me} is not in the {self.season()} postseason bracket."]
        s = max(mine, key=lambda x: ROUND_ORDER.get(x.gtype, 0))
        opp_id = s.b if tid == s.a else s.a
        opp = TEAM_ABBR[opp_id]
        mw, ow = (s.wa, s.wb) if tid == s.a else (s.wb, s.wa)
        if s.over:
            if s.winner == tid:
                nxt = {"F": "the division series", "D": "the championship series", "L": "the World Series"}.get(s.gtype)
                return [fit(f"[MLB {me}] 🏆 Won the {s.short} over {opp} {mw}-{ow}" + (f" and advanced to {nxt}." if nxt else " - WORLD SERIES CHAMPIONS!"))]
            return [f"[MLB {me}] Eliminated by {opp} {ow}-{mw} in the {s.short}."]
        status = "tied" if mw == ow else f"lead {mw}-{ow}" if mw > ow else f"trail {mw}-{ow}"
        if not s.started:
            status = "series not started"
        elif mw == ow:
            status = f"tied {mw}-{ow}"
        ng = s.next_game
        live = next((x for x in (await self.refresh_live() or []) if ng and x.pk == ng.pk), None)
        nxt = ""
        if ng and live and live.live:
            nxt = f" | LIVE {live.away} {live.ar}-{live.hr} {live.home} {live.inn_txt}"
        elif ng:
            nxt = f" | Next: G{ng.sgame} {when(ng)} {'vs' if ng.home_id == tid else '@'} {opp}"
        return [fit(f"[MLB {me}] {s.short} vs {opp}: {status}{nxt}")]

    async def cmd_leaders(self, inc, arg):
        rest = [w.lower() for w in inc.rest]
        if not rest:
            return ["[MLB] !leaders hr|avg|rbi|h|r|sb|obp|ops|k|era|w|sv|whip [ws|al|nl] - postseason leaders"]
        cat = LEADER_CATS.get(rest[0])
        if not cat:
            return ["[MLB] Unknown stat. Send !leaders for the list."]
        await self.load_teams()
        ws = "ws" in rest[1:]
        league = next(({"al": 103, "nl": 104}[w] for w in rest[1:] if w in ("al", "nl")), None)
        params = {"leaderCategories": cat[0], "season": self.season(), "sportId": 1, "limit": 5, "statGroup": cat[1],
                  "leaderGameTypes": "W" if ws else "P"}
        if league:
            params["leagueId"] = league
        data = await asyncio.to_thread(mlb_get, "/v1/stats/leaders", params)
        leaders = (((data or {}).get("leagueLeaders") or [{}])[0]).get("leaders") or []
        if not leaders:
            return ["[MLB] No postseason stats yet."]
        items = [f"{l['rank']}.{last_name(l['person']['fullName'])} {TEAM_ABBR.get(_i(l['team'].get('id')), '')} {l['value']}" for l in leaders]
        return pack(f"[MLB {cat[2]}{' WS' if ws else ''}]", items)

    async def cmd_player(self, inc, arg):
        words = [w for w in inc.rest if w.lower() != "ws"]
        ws = len(words) != len(inc.rest)
        q = " ".join(words).strip()
        if not q:
            return ["[MLB] !player NAME [ws] (e.g. !player judge, !player pca ws) - postseason stat line"]
        status, p = await self.find_player(q)
        if status == "none":
            return [f"[MLB] No active MLB player found for '{q}'."]
        if status == "many":
            return self._pick_msg(p)
        groups = ["pitching"] if p["pos"] == "P" else ["hitting", "pitching"] if p["pos"] == "TWP" else ["hitting"]
        lines = []
        for grp in groups:
            data = await asyncio.to_thread(mlb_get, f"/v1/people/{p['id']}/stats", {
                "stats": "season", "group": grp, "season": self.season(), "gameType": "W" if ws else "P"})
            splits = (((data or {}).get("stats") or [{}])[0]).get("splits") or []
            if not splits:
                continue
            st = splits[0]["stat"]
            body = (f"{st.get('gamesPlayed', 0)}G {st.get('avg', '-')} {st.get('homeRuns', 0)}HR {st.get('rbi', 0)}RBI {st.get('ops', '-')}OPS"
                    if grp == "hitting" else
                    f"{st.get('wins', 0)}-{st.get('losses', 0)} {st.get('era', '-')}ERA {st.get('inningsPitched', '0')}IP "
                    f"{st.get('strikeOuts', 0)}K")
            lines.append(f"[MLB] {short_name(p['name'])} {TEAM_ABBR.get(p['team'], '')} {'WS' if ws else 'PS'}: {body}")
        return lines[: CFG["max_reply_msgs"]] or [f"[MLB] No {'World Series' if ws else 'postseason'} stats yet for {p['name']}."]

    async def cmd_follow(self, inc, arg):
        q = " ".join(inc.rest).strip()
        if not q:
            return ["[MLB BOT] !follow TEAM or PLAYER (!follow nyy, !follow judge). DMs: runs, finals, series news"]
        await self.load_teams()
        tid = self.resolve_team(q)
        if not tid:
            return await super().cmd_follow(inc, arg)
        key = self._identify(inc)
        if not key:
            return [f"[MLB BOT] I can't find you in my contacts. DM me !{self.name} follow {q} (send an advert first)."]
        abbr = TEAM_ABBR[tid]
        sub = self.state.data["subs"].setdefault(key, {"name": inc.sender_name, "teams": []})
        sub["name"] = inc.sender_name or sub.get("name", "")
        if abbr in sub["teams"]:
            return [f"[MLB BOT] Already following {abbr}."]
        if len(sub["teams"]) >= CFG["max_follows"]:
            return [f"[MLB BOT] Max {CFG['max_follows']} teams. !unfollow one first."]
        sub["teams"].append(abbr)
        self.state.save()
        return [f"[MLB BOT] Following {abbr}: runs, finals and series news by DM."]

    async def cmd_unfollow(self, inc, arg):
        key = self._identify(inc)
        sub = self.state.data["subs"].get(key or "")
        q = " ".join(inc.rest).strip()
        removed = []
        if sub:
            if q:
                await self.load_teams()
                tid = self.resolve_team(q)
                if tid and TEAM_ABBR[tid] in sub["teams"]:
                    sub["teams"].remove(TEAM_ABBR[tid])
                    removed = [TEAM_ABBR[tid]]
            else:
                removed, sub["teams"] = list(sub["teams"]), []
            if not sub["teams"]:
                del self.state.data["subs"][key]
            self.state.save()
        if removed:
            if not q:
                await super().cmd_unfollow(inc, arg)     # "!unfollow" with no name clears the players too
            return [f"[MLB BOT] Unfollowed {', '.join(removed)}."]
        return await super().cmd_unfollow(inc, arg)

    async def cmd_following(self, inc, arg):
        key = self._identify(inc) or ""
        teams = (self.state.data["subs"].get(key) or {}).get("teams", [])
        players = [short_name(x["name"]) for x in (self.state.data["pfollows"].get(key) or {}).get("players", [])]
        if not teams and not players:
            return ["[MLB BOT] Not following anyone. !follow NYY or !follow judge"]
        return [fit(f"[MLB BOT] Following: {', '.join(teams + players)}")]

    async def cmd_ping(self, inc, arg):
        return await self.hub.cmd_ping(self, inc)

    async def cmd_botstatus(self, inc, arg):
        return await self.hub.status(self, self.ok_at)

    async def cmd_help(self, inc, arg):
        return ["[MLB BOT] !scores !series !ws !next !yesterday !pitchers !team X !leaders hr !player X !follow X !mode"]

    # ---------------- alerts ----------------
    def emit(self, kind, texts, g=None, dm_only=False):
        lvl = self.game_level(g) if g is not None else self.level()
        broadcast = not dm_only and kind in self.kinds.get(lvl, set())
        if broadcast:
            self.say_chan(texts)
        if g is not None and kind in POST_DM_KINDS and (not broadcast or CFG["dm_even_if_broadcast"]):
            for key in self.followers(g):
                self.hub.say_dm(key, texts[0])

    def series_events(self, g):
        """After a final: who advanced / won it all / who is facing elimination."""
        ss = g.sstat if isinstance(g.sstat, dict) else None
        if not ss or g.gtype == "R":
            return []
        w, l = _i(ss.get("wins")), _i(ss.get("losses"))
        win_id, lose_id = _i((ss.get("winningTeam") or {}).get("id")), _i((ss.get("losingTeam") or {}).get("id"))
        tag, need = game_tag(g), ROUND_LEN.get(g.gtype, 7) // 2 + 1
        if ss.get("isTied"):                    # MLB names no leading team when a series is tied
            if w == need - 1:
                return [("series", [f"⚠️ {tag} tied {w}-{l}: winner-take-all Game {w + l + 1}"], False)]
            return []
        if win_id not in TEAM_ABBR:
            return []
        wname = TEAM_NAMES.get(win_id, {}).get("name") or TEAM_ABBR[win_id]
        pre = g.sdesc[:2] if g.sdesc[:3] in ("AL ", "NL ") else ""
        if ss.get("isOver"):
            if g.gtype == "W":
                text = f"🏆 {wname} WIN THE WORLD SERIES ({w}-{l})!"
            elif g.gtype == "L":
                text = f"🏆 {wname} win the {tag} ({w}-{l}) and are headed to the World Series!"
            else:
                text = f"✅ {TEAM_ABBR[win_id]} win the {tag} {w}-{l} and advance to the {pre + 'DS' if g.gtype == 'F' else pre + 'CS'}"
            return [("series", [fit(text)], False)]
        if w == need - 1 and lose_id in TEAM_ABBR:
            what = "one win from the title" if g.gtype == "W" else "one win from advancing"
            return [("series", [fit(f"⚠️ {TEAM_ABBR[lose_id]} face elimination in Game {w + l + 1} | {TEAM_ABBR[win_id]} {what}")], False)]
        return []

    async def post_events(self, g, prev):
        """(kind, texts, dm_only) for one game. Tiers say how much of "max" a round gets: full (every run), lead (lead changes +
        every run from the 7th), key (lead changes from the 7th + extras), merged (one message per scoring half-inning)."""
        tier, tag = self.tier_of(g), game_tag(g)
        wants = "run" in self.kinds.get(self.game_level(g), set())      # does the channel get run alerts right now?
        fol = self.followers(g)
        score = f"{g.away} {g.ar}-{g.hr} {g.home}"
        evs = []
        p = self.pending.get(g.pk)
        if p and (g.inn_txt != p["key"] or not g.live):           # the scoring half-inning is over: one message for it
            del self.pending[g.pk]
            evs.append(("run", [fit(f"⚾ {tag}: {p['team']} {'RUN' if p['n'] == 1 else str(p['n']) + ' RUNS'} in the {p['when']} | {p['score']}")], False))
        d_a, d_h = g.ar - prev["ar"], g.hr - prev["hr"]
        if d_a < 0 or d_h < 0:
            evs.append(("correction", [f"⚠️ {tag} score update: {score} | {g.inn_txt}".rstrip(" |")], False))
        elif d_a or d_h:
            home_scored = d_h > d_a
            team, n = (g.home if home_scored else g.away), d_a + d_h
            tb = (prev["hr"] - prev["ar"]) if home_scored else (prev["ar"] - prev["hr"])
            ta = (g.hr - g.ar) if home_scored else (g.ar - g.hr)
            flip = "" if not (ta > 0 >= tb or ta == 0 > tb) else "TIE IT UP" if ta == 0 else "GO AHEAD" if tb == 0 else "TAKE THE LEAD"
            if home_scored and g.inn_txt.startswith("Bot") and g.inning >= 9 and ta > 0 >= tb:
                self.walkoff.add(g.pk)
            show = (tier == "full" or (tier == "lead" and (flip or g.inning >= 7))
                    or (tier == "key" and ((flip and g.inning >= 7) or g.inning >= 10)))
            if tier == "merged" and not (wants or fol):
                pass
            elif tier == "merged":
                q = self.pending.get(g.pk)
                if q and q["key"] == g.inn_txt and q["team"] == team:
                    q["n"], q["score"] = q["n"] + n, score
                else:
                    self.pending[g.pk] = {"key": g.inn_txt, "team": team, "n": n, "score": score,
                                          "when": g.inn_txt.replace("Top", "top").replace("Bot", "bottom")}
            elif (show and wants) or fol:
                desc = await self.scoring_play(g)
                head = (f"{tag}: {team} {flip}!" if flip else f"{tag}: {team} {'RUN' if n == 1 else f'{n} RUNS'}!")
                if desc and "grand slam" in desc.lower():
                    head = f"{tag}: {team} GRAND SLAM!"
                tail = f" | {score} | {g.inn_txt}".rstrip(" |")
                room = CFG["max_msg_bytes"] - len(f"⚾ {head} {tail}".encode("utf-8"))
                evs.append(("run", [f"⚾ {head}{' ' + trim_to(desc, room) if desc and room > 12 else ''}{tail}"], not show))

        if prev["state"] == "preview" and g.state == "live" and g.pk not in self.started:
            self.started.add(g.pk)
            pre = await self.series_pre(g)
            sp = f" | {g.away_sp} v {g.home_sp}" if g.away_sp and g.home_sp else ""
            evs.append(("start", [fit(f"⚾ {tag} G{g.sgame}: {g.away} @ {g.home}{' | ' + pre if pre else ''}{sp}")], False))
        elif g.live and g.inning >= 10 and prev["inning"] < 10:
            evs.append(("extras", [f"⏱ {tag} extras: {score}"], False))
        if g.delayed and not prev["delayed"]:
            evs.append(("delay", [fit(f"🌧 {tag}: {g.detail}: {score}")], False))
        if g.state == "postponed" and prev["state"] != "postponed":
            evs.append(("delay", [f"🌧 {tag}: {g.away} @ {g.home} postponed"], False))
        sent = self.thirds.setdefault(g.pk, set())
        if g.live and not g.delayed:                     # "min": the score after 3 and after 6 innings
            for n3 in (3, 6):
                if n3 not in sent and (g.inning > n3 or (g.inning == n3 and g.inn_txt.startswith("End"))):
                    sent.add(n3)
                    evs.append(("third", [fit(f"⚾ {tag} End of {ord_(n3)}: {score}" if g.inning == n3 else f"⚾ {tag} {g.inn_txt}: {score}")], False))

        if g.live and g.ah >= 0 and g.hh >= 0 and g.inning >= 6:
            for side, hits in (("away", g.ah), ("home", g.hh)):
                if hits:
                    continue
                batting, pitching = (g.away, g.home) if side == "away" else (g.home, g.away)
                st = g.inn_txt
                done = (g.inning - 1 if st.startswith("Top") else g.inning) if side == "away" else (g.inning if st.startswith("End") else g.inning - 1)
                if done >= 6 and done > self.nohit.get((g.pk, side), 0):
                    self.nohit[(g.pk, side)] = done
                    evs.append(("nohit", [fit(f"⚠️ NO-HIT WATCH: {batting} hitless through {done} vs {pitching} | {score}")], False))

        if g.final and prev["state"] != "final" and g.pk not in self.done:
            self.done.add(g.pk)
            lead = "🎉 WALK-OFF" if g.pk in self.walkoff else "🛑 FINAL"
            text = f"{lead}{'/' + str(g.inning) if g.inning > 9 else ''}: {g.away} {g.ar} @ {g.home} {g.hr}"
            if series_line(g):
                text += f" | {series_line(g)}"
            if g.win:
                text += f" | W {g.win} L {g.loss}" + (f" S {g.save}" if g.save else "")
            evs.append(("final", [fit(text)], False))
            if g.inning >= 9 and g.ah >= 0 and g.hh >= 0 and 0 in (g.ah, g.hh):
                winner = g.home if g.ah == 0 else g.away
                evs.append(("nohit", [fit(f"🚫 NO-HITTER! {winner} no-hit {g.away if winner == g.home else g.home}: {g.away} {g.ar} @ {g.home} {g.hr}")], False))
            evs += self.series_events(g)
        return start_first(evs)

    def update_busy(self, n_live):
        n = int(CFG["busy_live_games"])
        if not self.busy and n > 0 and n_live >= n:
            self.busy = True
            today = now().date().isoformat()
            if self.level() == "max" and self.busy_announced != today:
                self.busy_announced = today
                self.say_chan(f"⚡ {n_live} playoff games live: Wild Card / Division games show 3-inning scores + finals only "
                              f"(LCS and World Series stay full)")
        elif self.busy and n_live <= max(n - 2, 0):
            self.busy = False

    async def check_headsup(self, games):
        """World Series / championship series games get a heads-up shortly before first pitch."""
        for g in games:
            if not g.scheduled or g.pk in self.headsup or not g.start or self.tier_of(g) not in ("full", "lead"):
                continue
            mins = (g.start - now()).total_seconds() / 60
            if 0 < mins <= self.headsup_min:
                self.headsup.add(g.pk)
                pre = await self.series_pre(g)
                sp = f" | {g.away_sp} v {g.home_sp}" if g.away_sp and g.home_sp else ""
                self.emit("heads", [fit(f"🏟 {game_tag(g)} G{g.sgame} in {int(mins)}m: {g.away} @ {g.home}{' | ' + pre if pre else ''}{sp}")], g)

    async def process(self, games):
        seen = set()
        self.update_busy(sum(1 for g in games if g.live and self.tiers.get(g.gtype, "key") in ("merged", "key")))
        for g in games:
            seen.add(g.pk)
            cur = {"ar": g.ar, "hr": g.hr, "state": g.state, "inning": g.inning, "delayed": g.delayed}
            prev = self.gstate.get(g.pk)
            if prev and prev["state"] == "final" and g.state != "final":
                log.info("Postseason game %s went from final back to %s; ignoring the update", g.pk, g.state)
                continue
            self.gstate[g.pk] = cur
            if prev is None:
                self.thirds[g.pk] = {n for n in (3, 6) if g.live and n < g.inning}      # already past when we started watching
                continue                       # first sighting: baseline only
            for kind, texts, dm_only in await self.post_events(g, prev):
                self.emit(kind, texts, g, dm_only)
        for pk in set(self.gstate) - seen:
            del self.gstate[pk]
            self.pending.pop(pk, None)
            self.thirds.pop(pk, None)
        await self.check_headsup(games)

    # ---------------- daily "postseason today" ----------------
    async def check_digest(self, games):
        n = now()
        today, h = n.date().isoformat(), self.digest_hour
        if n.hour < h or self.state.data["last_digest"] == today:
            return
        if n.hour >= h + DIGEST_GRACE_HOURS or self.level() == "off":
            self.state.data["last_digest"] = today
            self.state.save()
            return
        self.state.data["last_digest"] = today
        self.state.save()
        todays = [g for g in games if g.scheduled and g.start and g.start.date() == n.date()]
        if not todays:
            return
        series = await self.get_series(max_age=60)
        items, flags = [], []
        for g in todays:
            s = self.series_of(g, series)
            mark = ""
            if s and s.known and not s.over and max(s.wa, s.wb) == s.need - 1:
                mark = " ⚠"
                who = "winner-take-all" if s.wa == s.wb else f"{s.label(s.a if s.wa > s.wb else s.b)} can clinch"
                flags.append(f"{s.short} {who}")
            items.append(f"{game_tag(g)} G{g.sgame} {g.away}@{g.home} {fmt_time(g.start)}{mark}")
        msgs = pack("[MLB TODAY]", items)
        if flags:
            msgs += [fit("⚠️ " + ", ".join(dict.fromkeys(flags)))]
        self.say_chan(msgs)

    # ---------------- loop ----------------
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
                games = await self.refresh_live(0)
                if games is not None:
                    await self.process(games)
                    await self.check_digest(games)
                    delay = self.next_delay(games)
                followed = await self.process_follows()
                if followed:
                    delay = min(delay, self.next_delay(followed))
            except Exception:
                log.exception("postseason poll loop")
            await asyncio.sleep(delay)


async def dump_post(year):
    """--dump-post [YEAR]: print the bracket as the bot sees it."""
    CFG["modules"]["postseason"]["season"] = year or 0
    mod = PostseasonModule(Hub(Transport()), "postseason")
    series = await mod.get_series(0)
    for s in series:
        print(f"{s.sid:>4}  {series_item(s)}  ({len(s.games)} games listed)")
    print(f"({len(series)} series, season {mod.season()})")

