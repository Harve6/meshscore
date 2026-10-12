"""Team code lookups for the setup wizard and `meshscore teams` (blocking HTTP; no radio needed)."""
from __future__ import annotations


from .core import now, session

TIMEOUT = 10


def _get(url, params=None):
    try:
        r = session.get(url, params=params, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def nhl():
    data = _get("https://api-web.nhle.com/v1/standings/now") or {}
    return sorted((t["teamAbbrev"]["default"], t["teamName"]["default"]) for t in data.get("standings", []))


def mlb():
    data = _get("https://statsapi.mlb.com/api/v1/teams", {"sportId": 1, "season": now().year}) or {}
    return sorted((t.get("abbreviation", ""), t.get("name", "")) for t in data.get("teams", []) if t.get("abbreviation"))


def nfl():
    data = _get("https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams") or {}
    out = []
    for sport in data.get("sports", []):
        for lg in sport.get("leagues", []):
            for t in lg.get("teams", []):
                tm = t.get("team", {})
                if tm.get("abbreviation"):
                    out.append((tm["abbreviation"], tm.get("displayName", "")))
    return sorted(out)


def hockeytech():
    from .leagues.hockeytech import api_get, build_teams, extract_games, scorebar_url
    games = extract_games(api_get(scorebar_url(14, 14)), "") or []
    return sorted((t.code, t.long or f"{t.city} {t.nick}".strip()) for t in build_teams(games).values())


SOURCES = {"nhl": nhl, "mlb": mlb, "nfl": nfl, "hockeytech": hockeytech, "ohl": hockeytech}


def team_list(kind):
    """[(code, name)] for a league type, [] when the data source can't be reached."""
    fn = SOURCES.get(kind)
    return fn() if fn else []
