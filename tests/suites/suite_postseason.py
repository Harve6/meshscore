"""Postseason bot test: fake radio, REAL 2025 postseason data (bracket, finals, a replayed World Series game 7),
simulated live games for the alert tiers, the sports menu and the channel scan."""
import asyncio, builtins, copy, dataclasses, os, sys, tempfile, time
from datetime import datetime
import suitelib as tl
m = tl.bootstrap()
TMP = os.getcwd()  # bootstrap() chdir'd into the fresh temp dir; this is it
m.CFG["modules"]["postseason"]["season"] = 2025
REAL_NOW, REAL_GET = m.now, m.mlb_get
at = tl.at_factory(m)
Fake = tl.make_fake(m, {"ohl": 0, "jays": 1, "mlb": 3})
settle = tl.settle
drain = tl.drain_raw


async def main():
    t = Fake()
    hub = m.Hub(t)
    jays, pm = m.JaysModule(hub), m.PostseasonModule(hub, "postseason")
    hub.add(jays); hub.add(pm)
    workers = [asyncio.create_task(hub.cmd_worker())]

    async def say(text, chan="", dm=False, name="Tester", key="abcdef123456"):
        t.sent.clear()
        tx = asyncio.create_task(hub.tx_worker())
        await hub.on_message(m.Incoming(text, dm, name, key if dm else "", 2, 7.5, chan))
        await settle(hub, 1.5)
        tx.cancel()
        print(f"  > [{('DM' if dm else '#' + chan)}] {text}")
        for c, x in t.sent:
            print(f"      -> {c}: {x}  ({len(x.encode())}B)")
        if not t.sent:
            print("      (no reply)")
        assert all(len(x.encode()) <= m.CFG["max_msg_bytes"] for _, x in t.sent), "message too long"
        return [x for _, x in t.sent]

    # ---------- real 2025 data ----------
    SER = REAL_GET("/v1/schedule/postseason/series", {"season": 2025, "hydrate": "team"})
    SCHED = REAL_GET("/v1/schedule", {"sportId": 1, "gameType": m.POST_TYPES, "season": 2025,
                                      "hydrate": "linescore,team,decisions,probablePitcher,seriesStatus"})
    assert SER and SCHED, "no network?"
    await pm.load_teams()
    pk_series = {g["gamePk"]: s["series"]["id"] for s in SER["series"] for g in s["games"]}
    GAMES = [m.parse_mgame(g) for d in SCHED["dates"] for g in d["games"]]
    GAMES = {(pk_series[g.pk], g.sgame): g for g in GAMES if g and g.pk in pk_series}

    def G(sid, n):
        return GAMES[(sid, n)]

    state = {"series": SER}

    def truncate(sid, keep):
        """The real series JSON with the games after `keep` not played yet."""
        d = copy.deepcopy(SER)
        for s in d["series"]:
            if s["series"]["id"] == sid:
                for g in s["games"][keep:]:
                    g["status"] = {"abstractGameState": "Preview", "detailedState": "Scheduled"}
                    for side in ("away", "home"):
                        g["teams"][side].pop("score", None); g["teams"][side].pop("isWinner", None)
        return d

    def fake_get(path, params=None):
        if path.endswith("/schedule/postseason/series"):
            return state["series"]
        if path == "/v1/schedule" and params and params.get("date"):
            day = params["date"]
            return {"dates": [{"date": day, "games": [g for d in SCHED["dates"] if d["date"] == day for g in d["games"]]}]}
        return REAL_GET(path, params)
    m.mlb_get = fake_get

    print("== the bracket, built from the real 2025 postseason")
    at(2025, 11, 2)
    series = await pm.get_series(0)
    items = [m.series_item(s) for s in series]
    for i in items:
        print("  ", i)
    assert len(series) == 11 and "WS LAD def TOR 4-3" in items and "ALDS TOR def NYY 3-1" in items and "ALWC DET def CLE 2-1" in items
    assert "ALCS TOR def SEA 4-3" in items and "NLCS LAD def MIL 4-0" in items and "NLDS MIL def CHC 3-2" in items

    print("\n== commands on the finished postseason")
    r = await say("!series", "mlb");      assert r[0] == "[MLB] WS LAD def TOR 4-3", r
    r = await say("!bracket", "mlb");     assert "WS LAD def TOR 4-3" in r[0] and "NLCS LAD def MIL 4-0" in r[0] and len(r) <= 2, r
    r = await say("!series al", "mlb");   assert "ALCS TOR def SEA 4-3" in r[0] and "NLCS" not in r[0], r
    r = await say("!series ws", "mlb");   assert r == ["[MLB] WS LAD def TOR 4-3"], r
    r = await say("!series bogus", "mlb"); assert "!series [al" in r[0]
    r = await say("!ws", "mlb");          assert "won the World Series 4-3" in r[0] and "Los Angeles Dodgers" in r[0] and "G7 LAD 5-4 TOR" in r[1], r
    r = await say("!team TOR", "mlb");    assert r == ["[MLB TOR] Eliminated by LAD 4-3 in the WS."], r
    r = await say("!team dodgers", "mlb"); assert "WORLD SERIES CHAMPIONS" in r[0], r
    r = await say("!team yankees", "mlb"); assert "Eliminated by TOR 3-1 in the ALDS" in r[0], r
    r = await say("!team BAL", "mlb");    assert "not in the 2025 postseason bracket" in r[0], r
    r = await say("!team zzz", "mlb");    assert "not found" in r[0], r
    r = await say("!scores", "mlb");      assert "No postseason games today" in r[0], r
    r = await say("!next", "mlb");        assert "No upcoming" in r[0], r
    r = await say("!leaders hr", "mlb");  assert r[0].startswith("[MLB HR]") and "Guerrero" in r[0], r
    r = await say("!leaders hr ws", "mlb"); assert r[0].startswith("[MLB HR WS]"), r
    r = await say("!leaders k al", "mlb"); assert r[0].startswith("[MLB K]") or r[0].startswith("[MLB K AL"), r
    r = await say("!leaders", "mlb");     assert "postseason leaders" in r[0]
    r = await say("!player Aaron Judge", "mlb"); assert "PS:" in r[0] and "NYY" in r[0], r
    r = await say("!player Shohei Ohtani ws", "mlb"); assert "WS:" in r[0], r
    r = await say("!player guerrero", "mlb"); assert "Several players" in r[0]
    at(2025, 10, 5)
    r = await say("!yesterday", "mlb");   assert r[0].startswith("[MLB YDAY]") and "[SERIES]" in r[1], r
    at(2025, 11, 2)

    print("\n== routing: !ps / !postseason work anywhere; the Jays keep their own !post")
    r = await say("!ps series", "ohl");          assert r[0].startswith("[MLB]"), r
    r = await say("!postseason ws", "jays");     assert r[0].startswith("[WS]"), r
    r = await say("!ps series", dm=True);        assert r[0].startswith("[MLB]"), r
    r = await say("!ps", "ohl");                 assert "[MLB BOT]" in r[0] and "!scores" in r[0]
    r = await say("!ping", "mlb");        assert r[0].startswith("[MLB BOT] pong")
    r = await say("!botstatus", "mlb");   assert r[0].startswith("[MLB BOT] up ")
    print("  ok")

    print("\n== mid-World Series (after game 5, TOR up 3-2): what the commands say")
    state["series"] = truncate("W_1", 5)
    pm.series_at = 0
    at(2025, 10, 30, 14)
    r = await say("!series", "mlb");      assert r == ["[MLB] WS TOR 3-2 LAD"], r
    r = await say("!ws", "mlb");          assert r[0].startswith("[WS] TOR 3-2 LAD | G6 ") and "@ TOR" in r[0] and "G5 TOR 6-1 LAD" in r[1], r
    r = await say("!team LAD", "mlb");    assert r[0].startswith("[MLB LAD] WS vs TOR: trail 2-3 | Next: G6 ") and "@ TOR" in r[0], r
    r = await say("!team TOR", "mlb");    assert "lead 3-2" in r[0], r
    r = await say("!next", "mlb");        assert "WS G6 LAD@TOR" in r[0] and "WS G7" in r[0], r
    r = await say("!bracket", "mlb");     assert r[0].startswith("[MLB] WS TOR 3-2 LAD") and "ALDS TOR def NYY 3-1" in " ".join(r), r
    print("\n== morning post and the 30-minute heads-up for game 6")
    g6 = dataclasses.replace(G("W_1", 6), state="preview", ar=0, hr=0)
    pm.state.data["last_digest"] = ""
    at(2025, 10, 31, 10, 5)
    pm.series_at = 0
    await pm.check_digest([g6])
    out = [x for _, _, x in drain(pm)]
    for x in out:
        print("   ", x)
    assert out[0].startswith("[MLB TODAY] WS G6 LAD@TOR") and out[0].endswith("⚠") and "TOR can clinch" in out[1], out
    at(2025, 10, 31, 19, 40)
    await pm.check_headsup([g6])
    out = [x for _, _, x in drain(pm)]
    print("   ", out)
    want_min = int((g6.start - m.now()).total_seconds() // 60)
    assert 0 < want_min <= 30 and len(out) == 1 and out[0].startswith(f"🏟 WS G6 in {want_min}m: LAD @ TOR | TOR lead 3-2"), (want_min, out)
    await pm.check_headsup([g6])
    assert not drain(pm), "heads-up must fire once"
    state["series"] = SER
    pm.series_at = 0
    at(2025, 11, 2)

    print("\n== series news from the real finals (advance, champion, elimination, winner-take-all)")
    def se(sid, n):
        return [x[1][0] for x in pm.series_events(G(sid, n))]
    cases = [("W_1", 7, "🏆 Los Angeles Dodgers WIN THE WORLD SERIES (4-3)!"),
             ("L_1", 7, "🏆 Toronto Blue Jays win the ALCS (4-3) and are headed to the World Series!"),
             ("L_2", 4, "🏆 Los Angeles Dodgers win the NLCS (4-0) and are headed to the World Series!"),
             ("D_1", 4, "✅ TOR win the ALDS 3-1 and advance to the ALCS"),
             ("F_1", 3, "✅ DET win the ALWC 2-1 and advance to the ALDS"),
             ("F_1", 1, "⚠️ CLE face elimination in Game 2 | DET one win from advancing"),
             ("D_2", 4, "⚠️ ALDS tied 2-2: winner-take-all Game 5"),
             ("W_1", 5, "⚠️ LAD face elimination in Game 6 | TOR one win from the title"),
             ("W_1", 2, None), ("W_1", 1, None)]
    for sid, n, want in cases:
        got = se(sid, n)
        print(f"   {sid} G{n}: {got}")
        assert (got == [want]) if want else (got == []), (sid, n, got)

    print("\n== replay of the real World Series game 7 (every run, lead changes, final, champions)")
    g7 = G("W_1", 7)
    feed = REAL_GET(f"/v1.1/game/{g7.pk}/feed/live", {"fields": "liveData,plays,allPlays,about,inning,halfInning,result,awayScore,homeScore,description,eventType"})
    plays = feed["liveData"]["plays"]["allPlays"]
    steps, last, desc = [], (0, 0), {}
    for p in plays:
        r, ab = p["result"], p["about"]
        cur = (r.get("awayScore", 0), r.get("homeScore", 0))
        if cur != last:
            steps.append((cur, ab["inning"], "Top" if ab["halfInning"] == "top" else "Bot"))
            desc[cur] = r.get("description", "")
            last = cur
    print(f"   {len(steps)} scoring plays in the real game")
    ws = g7
    state["series"] = truncate("W_1", 6)                  # game 7 not played yet: the series is 3-3
    pm.series_at = 0

    async def fake_play(g):
        return desc.get((g.ar, g.hr), "")
    pm.scoring_play = fake_play
    pm.state.data["mode"] = "all"
    pm.gstate.pop(ws.pk, None)

    def live(ar, hr, inn, half):
        return dataclasses.replace(ws, state="live", ar=ar, hr=hr, inning=inn, inn_txt=f"{half} {m.ord_(inn)}", ah=6, hh=6,
                                   win="", loss="", save="")
    await pm.process([dataclasses.replace(ws, state="preview", ar=0, hr=0, inning=0, inn_txt="")])
    await pm.process([live(0, 0, 1, "Top")])
    texts = [x for _, _, x in drain(pm)]
    assert len(texts) == 1 and texts[0].startswith("⚾ WS G7: LAD @ TOR | Series tied 3-3"), texts
    print("   ", texts[0])
    runs = []
    for (a, h), inn, half in steps:
        await pm.process([live(a, h, inn, half)])
        runs += [x for _, _, x in drain(pm)]
    for x in runs:
        print("   ", x)
    run_texts = [x for x in runs if x.startswith("⚾ WS: ")]
    assert len(run_texts) == len(steps), (len(run_texts), len(steps))
    assert any("extras" in x for x in runs), "the 11th inning should be announced as extras"
    assert any("TIE IT UP" in x and "Rojas" in x for x in runs) and any("GO AHEAD" in x and "Will Smith" in x for x in runs), runs
    await pm.process([dataclasses.replace(ws, inn_txt="")])
    fin = [x for _, _, x in drain(pm)]
    for x in fin:
        print("   ", x)
    assert fin[0].startswith("🛑 FINAL/11: LAD 5 @ TOR 4 | WS G7: LAD win 4-3 | W ") and fin[-1] == "🏆 Los Angeles Dodgers WIN THE WORLD SERIES (4-3)!", fin
    await pm.process([dataclasses.replace(ws, inn_txt="")])
    assert not drain(pm), "the final must not repeat"
    await pm.process([live(5, 4, 11, "Top")])
    assert not drain(pm), "a game that finished cannot un-finish"
    state["series"] = SER
    pm.series_at = 0

    print("\n== tiers: Wild Card merged / Division key / Championship lead / busy-night quiet")
    pm.state.data["mode"] = "all"

    async def play(label, game):
        await pm.process([game])
        out = [x for _, _, x in drain(pm)]
        print(f"   {label}: {out if out else '(nothing)'}")
        return out

    def lv(g, ar, hr, inn, txt, **kw):
        kw = {"ah": 5, "hh": 5, **kw}
        return dataclasses.replace(g, state="live", ar=ar, hr=hr, inning=inn, inn_txt=txt, **kw)

    wc = dataclasses.replace(G("F_1", 1), pk=8001)
    await play("WC baseline", dataclasses.replace(wc, state="preview", ar=0, hr=0))
    out = await play("WC first pitch", lv(wc, 0, 0, 1, "Top 1st"))
    assert out[0].startswith("⚾ ALWC G1: DET @ CLE") and "Skubal" in out[0], out
    assert not await play("WC away scores 1", lv(wc, 1, 0, 5, "Top 5th"))
    assert not await play("WC away scores 2 more", lv(wc, 3, 0, 5, "Top 5th"))
    out = await play("WC half-inning over", lv(wc, 3, 0, 5, "Mid 5th"))
    assert out == ["⚾ ALWC: DET 3 RUNS in the top 5th | DET 3-0 CLE"], out
    ds = dataclasses.replace(G("D_2", 4), pk=8002)
    await play("DS baseline", dataclasses.replace(ds, state="preview", ar=0, hr=0))
    await play("DS live", lv(ds, 0, 0, 1, "Top 1st"))
    assert not await play("DS 5th-inning run (silent)", lv(ds, 1, 0, 5, "Top 5th"))
    out = await play("DS 8th, home takes the lead", lv(ds, 1, 2, 8, "Bot 8th"))
    assert len(out) == 1 and "TAKE THE LEAD" in out[0], out
    assert not await play("DS 9th insurance run (silent)", lv(ds, 1, 3, 9, "Bot 9th"))
    lc = dataclasses.replace(G("L_1", 3), pk=8003)
    await play("LCS baseline", dataclasses.replace(lc, state="preview", ar=0, hr=0))
    await play("LCS live", lv(lc, 0, 0, 1, "Top 1st"))
    out = await play("LCS 3rd: first run = lead change", lv(lc, 1, 0, 3, "Top 3rd"))
    assert len(out) == 1 and "GO AHEAD" in out[0], out
    assert not await play("LCS 3rd: a second run, no lead change (silent)", lv(lc, 2, 0, 3, "Top 3rd"))
    assert not await play("LCS 4th: cuts the lead, no lead change (silent)", lv(lc, 2, 1, 4, "Bot 4th"))
    out = await play("LCS 5th: tie", lv(lc, 2, 2, 5, "Bot 5th"))
    assert len(out) == 1 and "TIE IT UP" in out[0], out
    out = await play("LCS 7th: home goes ahead", lv(lc, 2, 3, 7, "Bot 7th"))
    assert len(out) == 1 and "GO AHEAD" in out[0], out
    out = await play("LCS 7th: a plain extra run counts from the 7th", lv(lc, 2, 4, 7, "Bot 7th"))
    assert len(out) == 1 and "GO AHEAD" not in out[0] and "TAKE THE LEAD" not in out[0] and " RUN!" in out[0], out

    print("\n== grand slam, walk-off, no-hitter watch, rain delay, extras")
    ws2 = dataclasses.replace(G("W_1", 4), pk=8004)
    home_abbr = ws2.home
    await play("baseline", dataclasses.replace(ws2, state="preview", ar=0, hr=0))
    await play("live", lv(ws2, 0, 0, 1, "Top 1st"))

    async def slam(g):
        return "Test Hitter hits a grand slam (1) on a fly ball to left field. Runners score."
    pm.scoring_play = slam
    out = await play("grand slam", lv(ws2, 0, 4, 2, "Bot 2nd"))
    assert len(out) == 1 and "GRAND SLAM!" in out[0], out
    pm.scoring_play = fake_play
    out = await play("no-hit watch: away hitless thru 6", lv(ws2, 0, 4, 7, "Top 7th", ah=0, hh=5))
    assert any(x.startswith("⚠️ NO-HIT WATCH: " + ws2.away + " hitless through 6 vs " + ws2.home) for x in out), out
    out = await play("still hitless, next inning", lv(ws2, 0, 4, 8, "Top 8th", ah=0, hh=5))
    assert any("hitless through 7" in x for x in out), out
    assert not any("NO-HIT" in x for x in await play("same inning again", lv(ws2, 0, 4, 8, "Top 8th", ah=0, hh=5)))
    out = await play("delay", dataclasses.replace(lv(ws2, 0, 4, 8, "Mid 8th", ah=1, hh=5), delayed=True, detail="Delayed: Rain"))
    assert out == [f"🌧 WS: Delayed: Rain: {ws2.away} 0-4 {ws2.home}"], out
    out = await play("extras", lv(ws2, 4, 4, 10, "Top 10th"))
    assert any("extras" in x for x in out), out
    wo = dataclasses.replace(G("W_1", 3), pk=8005)
    await play("walk-off baseline", dataclasses.replace(wo, state="preview", ar=0, hr=0))
    await play("walk-off live", lv(wo, 0, 0, 1, "Top 1st"))
    await play("walk-off: tied in the 9th", lv(wo, 5, 5, 9, "Bot 9th"))
    out = await play("walk-off run", lv(wo, 5, 6, 9, "Bot 9th"))
    assert "GO AHEAD" in out[0], out
    out = await play("walk-off final", dataclasses.replace(wo, inn_txt=""))
    assert out[0].startswith("🎉 WALK-OFF"), out
    nh = dataclasses.replace(G("D_1", 2), pk=8006)
    await play("no-hitter baseline", dataclasses.replace(nh, state="preview", ar=0, hr=0))
    await play("no-hitter live", lv(nh, 0, 1, 8, "Top 8th", ah=0, hh=3))
    out = await play("no-hitter final", dataclasses.replace(nh, ah=0, hh=3, inning=9))
    assert any(x.startswith("🚫 NO-HITTER!") for x in out) and out[0].startswith("🛑 FINAL"), out

    print("\n== a busy day: 3 division/wild-card games at once drop to min (3-inning scores + finals); championship series / World Series stay max")
    m.CFG.update(busy_live_games=3)
    pm.busy, pm.busy_announced = False, ""
    b = [dataclasses.replace(G(sid, n), pk=9100 + i) for i, (sid, n) in enumerate((("F_1", 1), ("F_2", 1), ("F_4", 1)))]
    await pm.process([dataclasses.replace(x, state="preview", ar=0, hr=0) for x in b])
    drain(pm)
    await pm.process([lv(x, 0, 0, 1, "Top 1st") for x in b])
    out = [x for _, _, x in drain(pm)]
    print("   three start:", out)
    assert pm.busy and len(out) == 1 and "playoff games live" in out[0], out           # one notice; the first pitches stay quiet
    b[0] = lv(b[0], 1, 0, 6, "Top 6th")
    await pm.process([b[0], lv(b[1], 0, 0, 1, "Top 1st"), lv(b[2], 0, 0, 1, "Top 1st")])
    out = [x for _, _, x in drain(pm)]
    print("   a run and a poll gap:", out)
    assert len(out) == 1 and "Top 6th: DET 1-0 CLE" in out[0] and "RUN" not in out[0], out    # the run is silent; the 3-inning score is not
    b[0] = lv(b[0], 1, 0, 6, "End 6th")
    await pm.process([b[0], lv(b[1], 0, 0, 1, "Top 1st"), lv(b[2], 0, 0, 1, "Top 1st")])
    out = [x for _, _, x in drain(pm)]
    print("   end of the 6th:", out)
    assert out == ["⚾ ALWC End of 6th: DET 1-0 CLE"], out
    day = [b[0], lv(b[1], 0, 0, 1, "Top 1st"), lv(b[2], 0, 0, 1, "Top 1st")]
    await pm.process(day + [lv(ws2, 4, 4, 10, "Top 10th")]); drain(pm)      # the World Series game joins the day's list
    await pm.process(day + [lv(ws2, 6, 7, 11, "Bot 11th")])
    out = [x for _, _, x in drain(pm)]
    print("   a World Series run on a busy day:", out)
    assert pm.busy and any(x.startswith("⚾ WS: ") for x in out), out
    m.CFG.update(busy_live_games=3)
    pm.busy = False

    print("\n== follow a team by DM (works even when the channel is quiet)")
    r = await say("!ps follow nyy", dm=True);     assert "Following NYY" in r[0], r
    r = await say("!ps follow Aaron Judge", dm=True); assert "Following A. Judge" in r[0], r
    r = await say("!ps following", dm=True);      assert r == ["[MLB BOT] Following: NYY, A. Judge"], r
    r = await say("!ps follow nyy", dm=True);     assert "Already following NYY" in r[0], r
    r = await say("!ps follow dodgers", dm=True); r = await say("!ps follow tor", dm=True); r = await say("!ps follow bos", dm=True)
    assert "Max 3 teams" in r[0], r
    g_n = dataclasses.replace(G("F_2", 1), pk=9200)           # BOS @ NYY
    pm.tiers = {**pm.tiers, "F": "key"}
    pm.state.data["mode"] = "min"
    await pm.process([dataclasses.replace(g_n, state="preview", ar=0, hr=0)])
    await pm.process([lv(g_n, 0, 0, 1, "Top 1st")])
    drain(pm)
    await pm.process([lv(g_n, 0, 1, 5, "Bot 5th")])
    items = drain(pm)
    print("   ", items)
    dms = [i for i in items if i[0] == "dm"]
    assert len(dms) == 1 and dms[0][1] == "abcdef123456" and dms[0][2].startswith("⚾ ALWC: NYY GO AHEAD!"), items
    assert all(i[2].startswith("⚾ ALWC") and "Bot 5th" in i[2] for i in items if i[0] == "chan"), items      # the channel only got the 3-inning score
    await pm.process([dataclasses.replace(lv(g_n, 0, 1, 9, "Bot 9th"), state="final", inn_txt="")])
    items = drain(pm)
    assert items and {k for k, _, _ in items} == {"chan"} and any(i[2].startswith("🛑 FINAL") for i in items), items   # channel got it; no duplicate DM
    pm.tiers = {**pm.tiers, "F": "merged"}
    pm.state.data["mode"] = "max"
    r = await say("!ps unfollow nyy", dm=True);   assert r == ["[MLB BOT] Unfollowed NYY."], r
    r = await say("!ps unfollow", dm=True);       assert "Unfollowed" in r[0], r
    r = await say("!ps following", dm=True);      assert "Not following anyone" in r[0], r

    print("\n== alert levels max / min / off")
    r = await say("!mode", "mlb");         assert r[0].startswith("[MLB BOT] Alert level: max") and "score every 3 innings" in r[0], r
    r = await say("!mode min", "mlb");     assert "set to min" in r[0], r
    g_m = dataclasses.replace(G("L_2", 1), pk=9300)
    await pm.process([dataclasses.replace(g_m, state="preview", ar=0, hr=0)])
    await pm.process([lv(g_m, 0, 0, 1, "Top 1st")])
    assert not drain(pm), "min: no first pitch"
    await pm.process([lv(g_m, 1, 0, 2, "Top 2nd")])
    assert not drain(pm), "min: runs are silent"
    await pm.process([lv(g_m, 1, 0, 3, "End 3rd")])
    out = [x for _, _, x in drain(pm)]
    assert len(out) == 1 and out[0].startswith("⚾ NLCS End of 3rd: "), out
    await pm.process([dataclasses.replace(lv(g_m, 1, 0, 9, "Top 9th"), state="final", inn_txt="")])
    out = [x for _, _, x in drain(pm)]
    assert any(x.startswith("🛑 FINAL") for x in out) and not any("RUN" in x for x in out), out
    r = await say("!mode off", "mlb");     assert "set to off" in r[0], r
    g_o = dataclasses.replace(G("L_1", 2), pk=9301)
    await pm.process([dataclasses.replace(g_o, state="preview", ar=0, hr=0)])
    await pm.process([lv(g_o, 0, 0, 1, "Top 1st")])
    await pm.process([lv(g_o, 1, 0, 3, "End 3rd")])
    await pm.process([dataclasses.replace(lv(g_o, 1, 0, 9, "Top 9th"), state="final", inn_txt="")])
    assert not drain(pm), "off: nothing at all"
    r = await say("!mode bogus", "mlb");   assert "!mode max (everything)" in r[0], r
    r = await say("!mode all", "mlb");     assert "set to max" in r[0], r                      # the old words still work

    print("\n== a missing #mlb channel must not jam the send queue")
    pm.hub.t.chans = {"ohl": 0}
    pm.say_chan(["hello"])
    assert not drain(pm)
    pm.hub.t.chans = {"ohl": 0, "mlb": 3}
    pm.say_chan(["hello"])
    assert [x for _, _, x in drain(pm)] == ["hello"]

    print("\n== the same game listed twice (postponed slot + makeup date) does not flood")
    dup = dataclasses.replace(G("D_1", 1), pk=9400)
    a = dataclasses.replace(dup, state="postponed", ar=0, hr=0)
    b2 = dataclasses.replace(dup, state="final", ar=2, hr=4)
    assert len(m.dedupe_mgames([a, b2])) == 1 and m.dedupe_mgames([a, b2])[0].final
    print("  ok")

    for w in workers:
        w.cancel()
    m.now, m.mlb_get = REAL_NOW, REAL_GET

    print("\n== reading the radio's channels (fake radio)")
    from meshcore import EventType

    class Resp:
        def __init__(self, name=None, err=False):
            self.type = EventType.ERROR if err else "ok"
            self.payload = {"channel_name": name or ""}

    class FakeCmds:
        def __init__(self, names): self.names = names
        async def get_channel(self, i):
            if i in self.names:
                return Resp(self.names[i])
            return Resp("") if i < 8 else Resp(err=True)

    class FakeMc:
        def __init__(self, names): self.commands = FakeCmds(names)

    lt = m.LibTransport()
    lt.mc, lt.ET = FakeMc({0: "Public", 1: "#ohl", 2: "#jays", 3: "#leafs", 4: "#mlb"}), EventType
    got = await lt._read_channel_names()
    assert got == {0: "Public", 1: "#ohl", 2: "#jays", 3: "#leafs", 4: "#mlb"}, got
    lt.radio_names = got
    await lt._map_channels({"ohl": {"channel": "#ohl", "channel_index": None},
                            "postseason": {"channel": "#mlb", "channel_index": None}})
    assert lt.chans == {"ohl": 1, "mlb": 4} and lt.radio_names is None, lt.chans
    lt2 = m.LibTransport(); lt2.mc, lt2.ET = FakeMc({0: "Public", 1: "#ohl"}), EventType
    await lt2._map_channels({"ohl": {"channel": "#ohl", "channel_index": None}, "postseason": {"channel": "#mlb", "channel_index": None}})
    assert lt2.chans == {"ohl": 1}                              # the missing one is logged, the rest still works
    ltg = m.LibTransport(); ltg.mc, ltg.ET = FakeMc({0: "Public", 1: "#ohl", 12: "#bills"}), EventType
    assert await ltg._read_channel_names() == {0: "Public", 1: "#ohl", 12: "#bills"}   # a long run of empty slots is not the end
    print("   ok")

    print("\n== the real connect path: open() for the menu, then start() reusing that connection (and a plain reconnect)")

    class LiveMc(FakeMc):
        def __init__(self, names):
            super().__init__(names)
            self.subs, self.fetching, self.contacts, self.dropped = [], False, {}, False
        def subscribe(self, event, cb): self.subs.append(event)
        async def start_auto_message_fetching(self): self.fetching = True
        async def disconnect(self): self.dropped = True

    made = []

    async def fake_connect():
        made.append(LiveMc({0: "Public", 1: "#ohl", 2: "#jays", 4: "#mlb"}))
        return made[-1]
    channels = {"ohl": {"channel": "#ohl", "channel_index": None}, "postseason": {"channel": "#mlb", "channel_index": None}}

    async def on_msg(inc): pass
    lt3 = m.LibTransport()
    lt3._connect = fake_connect
    names = await lt3.open()                                   # what the startup menu does
    assert names[4] == "#mlb" and len(made) == 1 and lt3.mc is made[0]
    await lt3.start(on_msg, channels)                          # the bot then starts on the SAME connection
    assert len(made) == 1 and lt3.alive and lt3.chans == {"ohl": 1, "mlb": 4}, (len(made), lt3.chans)
    assert made[0].fetching and len(made[0].subs) >= 2
    await lt3.stop()
    assert made[0].dropped and not lt3.alive and lt3.mc is None
    await lt3.start(on_msg, channels)                          # a reconnect after a dropped link opens a fresh connection
    assert len(made) == 2 and lt3.alive and made[1].fetching
    lt4 = m.LibTransport(); lt4._connect = fake_connect        # no menu (--sports / connect_menu false): start() alone
    await lt4.start(on_msg, channels)
    assert lt4.alive and lt4.chans == {"ohl": 1, "mlb": 4}
    print("   ok")
    print("\nALL OK")


asyncio.run(main())
