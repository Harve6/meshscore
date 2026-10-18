"""ESPN's public scoreboard feed (the same one the NFL bot uses): today's games, their state and the winners."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

LEAGUES = {"nhl": "hockey/nhl", "mlb": "baseball/mlb", "nfl": "football/nfl", "nba": "basketball/nba"}
URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"


@dataclass
class Game:
    id: str
    league: str
    start: float                  # unix time
    state: str                    # "pre" | "in" | "post" | "void" (postponed / cancelled)
    home: str
    away: str
    home_score: int = 0
    away_score: int = 0
    winner: Optional[str] = None  # the winning team's abbreviation once the game is over

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @property
    def over(self) -> bool:
        return self.state in ("post", "void")


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def parse_scoreboard(data: dict, league: str) -> list[Game]:
    """ESPN scoreboard JSON -> games. Anything that does not look like a game is skipped, never an exception."""
    out = []
    for e in (data or {}).get("events", []) or []:
        try:
            comp = e["competitions"][0]
            teams = {t["homeAway"]: t for t in comp["competitors"]}
            home, away = teams["home"], teams["away"]
            st = e["status"]["type"]
            state = st.get("state", "pre")
            if any(w in str(st.get("name", "")).upper() for w in ("POSTPONED", "CANCELED", "CANCELLED", "SUSPENDED")):
                state = "void"
            start = datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            g = Game(str(e["id"]), league, start.timestamp(), state, home["team"]["abbreviation"].upper(),
                     away["team"]["abbreviation"].upper(), _int(home.get("score")), _int(away.get("score")))
            if state == "post":
                w = home if home.get("winner") else away if away.get("winner") else None
                if w is None and g.home_score != g.away_score:           # older feeds: decide by the score
                    w = home if g.home_score > g.away_score else away
                g.winner = w["team"]["abbreviation"].upper() if w else None
                if g.winner is None:                                     # a tie: nobody to pick
                    g.state = "void"
            out.append(g)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


def fetch_games(session, league: str, day: str, timeout: float = 15.0) -> list[Game]:
    """day = 'YYYY-MM-DD'."""
    r = session.get(URL.format(path=LEAGUES[league]), params={"dates": day.replace("-", "")}, timeout=timeout)
    r.raise_for_status()
    return parse_scoreboard(r.json(), league)
