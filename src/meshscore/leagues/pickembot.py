"""Pick'em: DM your pick for the day's games, the bot keeps score.

Each morning (`pick_hour`, default 9) the bot posts the day's slate. Players DM `!pick TOR` any time before that game
starts; when the games finish the bot scores them, posts one short recap (who got how many right) and keeps a weekly
and season leaderboard on the shared web board. Picks are private (DM) so nobody can copy them. Games come from
ESPN's public scoreboard (nhl, mlb, nfl, nba); `teams = ["TOR", "BUF"]` limits the slate to games involving them.
See docs/daily-games.md.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from .. import core
from ..core import fit, pack, session
from ..dailyplay import espn
from ..dailyplay.base import DailyModule

log = logging.getLogger("meshscore")

KEEP_DAYS = 4
MAX_SLATE = 10


def local(ts: float) -> datetime:
    d = datetime.fromtimestamp(ts)
    return d.astimezone(core.TZ) if core.TZ else d.astimezone()


def clock_text(ts: float) -> str:
    d = local(ts)
    h = d.hour % 12 or 12
    return f"{h}{'' if d.minute == 0 else ':%02d' % d.minute}{'a' if d.hour < 12 else 'p'}"


class PickemModule(DailyModule):
    label = "Pick'em"
    json_name = "pickem"
    board_title = "Mesh Pick'em"
    kinds = {"max": {"slate", "recap"}, "min": {"recap"}, "off": set()}
    min_blurb = "the daily recap only"
    bare_command = "!picks"
    no_cooldown_cmds = ("!pick", "!picks")

    def __init__(self, hub, name):
        super().__init__(hub, name)
        c = self.cfg
        self.blurb = "daily pick'em"
        self.leagues = [str(x).lower() for x in (c.get("leagues") or ["nhl"]) if str(x).lower() in espn.LEAGUES]
        self.teams = {str(x).upper() for x in (c.get("teams") or [])}
        self.hour = int(c.get("pick_hour", 9))
        self.refresh_seconds = float(c.get("refresh_seconds", 300))
        self.fetch = lambda league, day: espn.fetch_games(session, league, day)      # tests replace this
        d = self.state.data
        d.setdefault("slates", [])         # [{"date", "games": {id: game dict}, "picks": {voter: {id: team}}, "posted", "recapped"}]
        d.setdefault("stats", {})          # voter -> {"season","correct","picks","week","week_id"}
        self._last_fetch = 0.0

    # ---------------------------------------------------------------- the loop
    async def poll(self):
        now = self.clock()
        d = self.state.data
        today = self.today()
        if not self._slate(today):
            d["slates"].append({"date": today, "games": {}, "picks": {}, "posted": False, "recapped": False})
        if now - self._last_fetch >= self.refresh_seconds:
            self._last_fetch = now
            for s in [x for x in d["slates"] if not x["recapped"] and x["date"] <= today]:
                await self._refresh(s)
        for x in d["slates"]:
            if x["date"] < today and not x["games"]:
                x["recapped"] = True                    # a day with no games: nothing to score
        slate = self._slate(today)
        if slate and slate["games"] and not slate["posted"] and self.in_hour(self.hour):
            slate["posted"] = True
            self.state.save()
            self.alert("slate", self.slate_text(slate))
        for s in d["slates"]:
            if not s["recapped"] and s["games"] and all(g["state"] in ("post", "void") for g in s["games"].values()):
                s["recapped"] = True
                self.state.save()
                text = self.recap_text(s)
                if text:
                    self.alert("recap", text)
        d["slates"] = [s for s in d["slates"] if s["date"] >= (core.now().date() - timedelta(days=KEEP_DAYS)).isoformat()]
        await self.publish()
        await self._refresh_cards(now)
        self.mark_ok()
        live = any(g["state"] == "in" for s in d["slates"] for g in s["games"].values())
        return 60 if live else 120

    def _slate(self, day: str):
        for s in self.state.data["slates"]:
            if s["date"] == day:
                return s
        return None

    async def _refresh(self, s: dict):
        games = []
        for league in self.leagues:
            try:
                games += await asyncio.to_thread(self.fetch, league, s["date"])
            except Exception as e:
                log.warning("Pick'em %s fetch failed: %s", league, e)
                return
        if self.teams:
            games = [g for g in games if g.home in self.teams or g.away in self.teams]
        games = sorted(games, key=lambda g: g.start)[:MAX_SLATE]
        if not games and not s["games"]:
            return
        for g in games:
            old = s["games"].get(g.id)
            if old and old.get("scored"):
                continue
            s["games"][g.id] = {**g.to_dict(), "scored": bool(old and old.get("scored"))}
        self._score(s)
        self.state.save()

    def _score(self, s: dict):
        stats = self.state.data["stats"]
        for gid, g in s["games"].items():
            if g["scored"] or g["state"] not in ("post", "void"):
                continue
            g["scored"] = True
            if g["state"] != "post" or not g.get("winner"):
                continue
            week = datetime.fromisoformat(s["date"]).strftime("%G-W%V")
            for who, picks in s["picks"].items():
                team = picks.get(gid)
                if not team:
                    continue
                st = stats.setdefault(who, {"season": 0, "correct": 0, "picks": 0, "week": 0, "week_id": week})
                if st["week_id"] != week:
                    st["week"], st["week_id"] = 0, week
                st["picks"] += 1
                if team == g["winner"]:
                    st["correct"] += 1
                    st["season"] += 1
                    st["week"] += 1

    # ---------------------------------------------------------------- texts
    def slate_text(self, s: dict) -> str:
        items = [f"{g['away']}@{g['home']} {clock_text(g['start'])}" for g in s["games"].values()]
        body = pack("🎯 Pick'em:", items, sep=" ", max_msgs=1)[0]
        tail = " DM !pick TOR"
        return body + tail if len((body + tail).encode()) <= 130 else body

    def recap_text(self, s: dict) -> str:
        games = [g for g in s["games"].values() if g["state"] == "post"]
        if not games or not s["picks"]:
            return ""
        scores = []
        for who, picks in s["picks"].items():
            right = sum(1 for g in games if picks.get(g["id"]) == g.get("winner"))
            made = sum(1 for g in games if g["id"] in picks)
            if made:
                scores.append((right, made, who))
        if not scores:
            return ""
        scores.sort(key=lambda x: (-x[0], x[1], x[2]))
        line = ", ".join(f"{who} {right}/{made}" for right, made, who in scores[:5])
        lead = max(self.state.data["stats"].items(), key=lambda kv: (kv[1]["season"], kv[0]), default=None)
        tail = f" | season: {lead[0]} {lead[1]['season']}" if lead else ""
        return fit(f"🎯 Pick'em {s['date'][5:]}: {line}{tail}")

    # ---------------------------------------------------------------- commands
    def extra_commands(self):
        return {"!pick": self.cmd_pick, "!picks": self.cmd_picks, "!picktop": self.cmd_top}

    def help_text(self):
        return "!pick <TEAM> (by DM) | !picks the slate + your picks | !picktop"

    def status_extra(self):
        s = self._slate(self.today())
        return f" | slate {len(s['games']) if s else 0} games"

    def _find(self, team: str):
        """(slate, game) for the first open game today involving this team."""
        s = self._slate(self.today())
        if not s:
            return None, None
        for g in s["games"].values():
            if team in (g["home"], g["away"]):
                return s, g
        return s, None

    async def cmd_pick(self, inc, arg):
        if not inc.is_dm:
            return self._dm_only(inc, "your pick", "!pick TOR")
        team = (arg or "").strip().upper()
        if not team:
            return await self.cmd_picks(inc, "")
        s, g = self._find(team)
        if not s or not s["games"]:
            return [fit(f"[{self.tag}] No games on today's slate yet")]
        if g is None:
            teams = sorted({t for x in s["games"].values() for t in (x["away"], x["home"])})
            return [fit(f"[{self.tag}] No {team} game today. Teams: {' '.join(teams)}")]
        if g["state"] != "pre" or self.clock() >= g["start"]:
            return [fit(f"[{self.tag}] {g['away']}@{g['home']} has started - picks are locked")]
        who = self._voter(inc)
        s["picks"].setdefault(who, {})[g["id"]] = team
        self.state.save()
        n = len(s["picks"][who])
        return [fit(f"[{self.tag}] ✓ {team} in {g['away']}@{g['home']} ({clock_text(g['start'])}). {n}/{len(s['games'])} picked")]

    async def cmd_picks(self, inc, arg):
        s = self._slate(self.today())
        if not s or not s["games"]:
            return [f"[{self.tag}] No games on today's slate"]
        who = self._voter(inc) if inc.is_dm else None
        mine = s["picks"].get(who, {}) if who else {}
        items = []
        for gid, g in s["games"].items():
            mark = ""
            if gid in mine:
                mark = f" {mine[gid]}" + (" ✓" if g.get("winner") == mine[gid] else " ✗" if g["state"] == "post" else "")
            result = f" {g['away_score']}-{g['home_score']}" if g["state"] in ("in", "post") else f" {clock_text(g['start'])}"
            items.append(f"{g['away']}@{g['home']}{result}{mark}")
        return pack(f"[{self.tag}]", items, sep=" | ")

    async def cmd_top(self, inc, arg):
        stats = self.state.data["stats"]
        if not stats:
            return [f"[{self.tag}] Nobody has scored yet"]
        wk = sorted(stats.items(), key=lambda kv: (-kv[1]["week"], kv[0]))[:3]
        ss = sorted(stats.items(), key=lambda kv: (-kv[1]["season"], kv[0]))[:3]
        return [fit(f"[{self.tag}] week: " + ", ".join(f"{n} {s['week']}" for n, s in wk) +
                    " | season: " + ", ".join(f"{n} {s['season']}" for n, s in ss))]

    # ---------------------------------------------------------------- the shared web board
    def site_state(self) -> dict:
        d = self.state.data
        s = self._slate(self.today())
        games = []
        if s:
            for g in s["games"].values():
                n_picks = sum(1 for p in s["picks"].values() if g["id"] in p)
                row = {"away": g["away"], "home": g["home"], "start": int(g["start"]), "state": g["state"], "picks": n_picks}
                if g["state"] in ("in", "post"):
                    row.update(away_score=g["away_score"], home_score=g["home_score"])
                if g["state"] == "post":
                    row["winner"] = g.get("winner")
                games.append(row)
        top = sorted(d["stats"].items(), key=lambda kv: (-kv[1]["season"], kv[0]))[:10]
        return {
            **self.base_state(),
            "date": s["date"] if s else self.today(),
            "games": games,
            "leaders": [{"name": n, "season": v["season"], "week": v["week"], "picks": v["picks"], "correct": v["correct"]} for n, v in top],
            "hour": self.hour,
            "leagues": self.leagues,
        }
