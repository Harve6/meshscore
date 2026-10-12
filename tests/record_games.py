"""Download the real finished games used by tests/test_game_replays.py into tests/fixtures/games.json.gz.

    python tests/record_games.py

Needs internet. The game ids are fixed (finished games never change), so re-running mostly reproduces the same file;
add ids to the lists below to cover another kind of game.
"""
import gzip
import json
import os
import sys
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fixtures", "games.json.gz")
UA = {"User-Agent": "MeshScore-tests (+https://github.com/Harve6/meshscore)"}

# NHL 2025-26 regular season: what each one is good for
NHL = {
    2025020049: "NSH 4 @ TOR 7 - eleven goals in regulation",
    2025020079: "SEA 4 @ TOR 3 - decided in overtime",
    2025020869: "TOR 3 @ VAN 2 - decided in a shootout",
    2025020004: "MTL 2 @ TOR 5 - opening week, regulation",
}
# MLB 2025 (Blue Jays games): pk -> note
MLB = {
    778465: "TOR 0 @ NYM 5 - shutout",
    778435: "TOR 1 @ NYM 2 - one-run pitchers' duel",
    778258: "SEA 8 @ TOR 4 - 12 innings",
    777935: "TB 11 @ TOR 9 - slugfest",
    813032: "World Series G3: TOR 5 @ LAD 6 - 18 innings",
}
# OHL (HockeyTech) games: id -> note. The scoreboard keeps only ~2 weeks, so these must be recorded while they are recent.
OHL = {
    "29009": "NB 2 @ OSH 7 - regulation",
    "29008": "OTT 2 @ KGN 3 - overtime",
    "29036": "BRAM 4 @ BFD 5 - shootout",
    "29023": "KIT 4 @ ER 3 - overtime (priority team)",
}
FEED_FIELDS = ("gameData,teams,away,home,abbreviation,id,liveData,plays,allPlays,scoringPlays,about,atBatIndex,inning,"
               "halfInning,isComplete,isScoringPlay,result,eventType,description,awayScore,homeScore,rbi")


def get(url, **params):
    r = requests.get(url, params=params, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def hockeytech_base():
    sys.path.insert(0, os.path.join(HERE, "..", "src"))
    from meshscore.core import CFG
    return CFG["api_key"], CFG["client_code"]


def main():
    blob = {"recorded_at": datetime.now(timezone.utc).isoformat(), "nhl": {}, "mlb": {}, "ohl": {}}
    if os.path.exists(OUT):                  # keep OHL games recorded earlier: the scoreboard forgets them after ~2 weeks
        with gzip.open(OUT, "rt", encoding="utf-8") as f:
            blob["ohl"] = json.load(f).get("ohl", {})
    for gid, note in NHL.items():
        print("NHL", gid, note)
        land = get(f"https://api-web.nhle.com/v1/gamecenter/{gid}/landing")
        blob["nhl"][str(gid)] = {"note": note, "landing": land}
    for pk, note in MLB.items():
        print("MLB", pk, note)
        sched = get("https://statsapi.mlb.com/api/v1/schedule", sportId=1, gamePk=pk, hydrate="linescore,team,decisions,seriesStatus")
        game = sched["dates"][0]["games"][0]
        feed = get(f"https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live", fields=FEED_FIELDS)
        blob["mlb"][str(pk)] = {"note": note, "game": game, "feed": feed}
    key, client = hockeytech_base()
    feed = "https://lscluster.hockeytech.com/feed/index.php"
    board = get(feed, feed="modulekit", view="scorebar", numberofdaysback=21, numberofdaysahead=0, client_code=client,
                key=key, fmt="json")["SiteKit"]["Scorebar"]
    for gid, note in OHL.items():
        entry = next((x for x in board if x["ID"] == gid), None)
        if entry is None:
            print("OHL", gid, "not on the scoreboard any more - keeping the earlier recording" if gid in blob["ohl"] else "MISSING")
            continue
        print("OHL", gid, note)
        summ = get(feed, feed="gc", tab="gamesummary", game_id=gid, client_code=client, key=key, lang_code="en", fmt="json")
        blob["ohl"][gid] = {"note": note, "entry": entry, "summary": summ}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(blob, f, separators=(",", ":"), sort_keys=True)
    print(f"saved {OUT} ({os.path.getsize(OUT) // 1024} KB)")


if __name__ == "__main__":
    sys.exit(main())
