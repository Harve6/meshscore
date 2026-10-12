"""NFL module (#bills) test: fake radio, REAL data - the Sept 27 Chargers @ Bills game's actual scoring plays replayed
one by one, the real schedule/summary shapes for a game in progress, and a real completed game (Lions @ Bills)
for the final. Pinned to specific past games, so it doesn't depend on who the Bills play today. No radio needed."""
import asyncio, dataclasses, os, sys, tempfile, time
import suitelib as tl
m = tl.bootstrap()
m.CFG["modules"]["bills"] = {"enabled": True, "type": "nfl", "team": "BUF", "label": "Buffalo Bills",
                             "channel": "#bills", "channel_index": None, "digest_hour": 10}
Fake = tl.make_fake(m, {"bills": 0})
settle = tl.settle
drain = tl.drain_texts


async def main():
    t = Fake()
    hub = m.Hub(t)
    nfl = m.NflModule(hub, "bills")
    hub.add(nfl)
    workers = [asyncio.create_task(hub.tx_worker()), asyncio.create_task(hub.cmd_worker())]
    say = tl.make_say(hub, t, m)

    # Chargers @ Bills, Sept 27 2026: a real game (BUF won 24-16), now completed. Its actual scoring plays are the script
    # for the replay below - pinned to this one game so the suite doesn't depend on who the Bills play today.
    GAME_ID = "401872953"
    print("== real data: the season schedule, and the Sept 27 Chargers @ Bills game")
    games = await nfl.refresh(0)
    assert games, "no games returned - no network?"
    live = next(g for g in games if g.id == GAME_ID)
    print("  ", nfl.line(live))
    assert {live.away, live.home} == {"LAC", "BUF"} and live.phase == "reg" and live.final and (live.ar, live.hr) == (16, 24)

    print("\n== a game in progress: the schedule feed has no score for it, the game summary does (real shapes)")
    import copy as _copy
    sched = m.espn_get(f"{m.ESPN_NFL}/teams/buf/schedule", {"seasontype": 2})
    ev = next(e for e in sched["events"] if e["id"] == GAME_ID)
    in_progress = {"clock": 455.0, "displayClock": "7:35", "period": 4,
                   "type": {"id": "2", "name": "STATUS_IN_PROGRESS", "state": "in", "completed": False,
                            "description": "In Progress", "detail": "7:35 - 4th Quarter"}}
    sched_comp, summ_comp = _copy.deepcopy(ev["competitions"][0]), _copy.deepcopy(ev["competitions"][0])
    sched_comp["status"] = summ_comp["status"] = in_progress
    for c in sched_comp["competitors"]:
        c["score"] = c["winner"] = None                       # exactly what the real schedule feed does for a live game
    for c in summ_comp["competitors"]:
        c["score"] = {"value": 17.0 if c["homeAway"] == "home" else 13.0, "displayValue": "x"}
        c["winner"] = None
    only_sched = m.parse_nfl_game(sched_comp, "reg", GAME_ID, ev["date"])
    assert only_sched.live and (only_sched.ar, only_sched.hr) == (0, 0)       # the schedule alone would say 0-0
    real_get0 = m.espn_get

    def live_espn(url, params=None):
        if url.endswith("/schedule"):
            if (params or {}).get("seasontype") == 2:
                return {"events": [{"id": GAME_ID, "date": ev["date"], "week": ev.get("week"), "competitions": [sched_comp]}]}
            return {"events": []}
        if params and "event" in params:
            return {"header": {"competitions": [summ_comp]}, "scoringPlays": []}
        return real_get0(url, params)
    m.espn_get = live_espn
    nfl_live = m.NflModule(hub, "bills")
    lg = next(x for x in await nfl_live.refresh(0) if x.id == GAME_ID)
    m.espn_get = real_get0
    print("  ", nfl_live.line(lg))
    assert lg.live and (lg.ar, lg.hr) == (13, 17) and lg.period == 4 and lg.clock == "7:35", lg     # score comes from the summary

    print("\n== commands against real data")
    r = await say("!score", "bills");     assert r and r[0].startswith("[BILLS]"), r        # today's game, or "No game today"
    r = await say("!next", "bills");      assert r and "NEXT" in r[0], r
    r = await say("!last", "bills");      assert r and r[0].startswith("[BILLS LAST]") and "BUF" in r[0], r
    r = await say("!standings", "bills"); assert r[0].startswith("[BILLS AFC]") and "BUF" in " ".join(r), r
    r = await say("!standings afc", "bills"); assert r[0].startswith("[BILLS AFC]"), r
    r = await say("!standings nfc", "bills"); assert r[0].startswith("[BILLS NFC]"), r
    r = await say("!phase", "bills");     assert "pre off" in r[0] and "reg ON" in r[0] and "post ON" in r[0], r
    r = await say("!mode", "bills");      assert r[0].startswith("[BILLS BOT] Alert level: max") and "end of each quarter" in r[0], r
    r = await say("!ping", "bills");      assert r[0].startswith("[BILLS BOT] pong")
    r = await say("!botstatus", "bills"); assert r[0].startswith("[BILLS BOT] up ")
    r = await say("!help", "bills");      assert "!score" in r[0] and "!standings" in r[0]

    print("\n== replay of the real game's scoring plays (Chargers @ Bills, Sept 27)")
    data = await asyncio.to_thread(m.espn_get, f"{m.ESPN_NFL}/summary", {"event": live.id})
    plays = data["scoringPlays"]
    print(f"   {len(plays)} real scoring plays")
    for p in plays:
        print("   ", p["period"]["number"], p["clock"]["displayValue"], p["team"]["abbreviation"], p["type"]["abbreviation"], p["text"])
    assert len(plays) >= 6

    async def fake_summary(gid, _resp={"plays": None}):
        return {"scoringPlays": _resp["plays"]}

    nfl.state.data["mode"] = "max"
    base = dataclasses.replace(live, state="preview", ar=0, hr=0, period=0, clock="")

    def step_for(i):
        p = plays[i]
        return dataclasses.replace(live, state="live", ar=_i_(p["awayScore"]), hr=_i_(p["homeScore"]),
                                   period=p["period"]["number"], clock=p["clock"]["displayValue"])

    def _i_(v):
        return int(v)

    real_espn_get = m.espn_get

    def stub_espn(url, params=None):
        if params and "event" in params:
            return {"scoringPlays": stub_espn.plays}
        return real_espn_get(url, params)
    stub_espn.plays = []
    m.espn_get = stub_espn

    await nfl.process([base])
    assert not drain(nfl)
    live0 = dataclasses.replace(live, state="live", ar=0, hr=0, period=1, clock="15:00")
    await nfl.process([live0])
    out = drain(nfl)
    assert out == ["🏈 Kickoff: LAC @ BUF"], out
    print("   kickoff:", out)

    seen_periods = {live0.period}
    for i, p in enumerate(plays):
        stub_espn.plays = plays[: i + 1]
        g = step_for(i)
        await nfl.process([g])
        out = drain(nfl)
        want_team = p["team"]["abbreviation"]
        want_tag = m.SCORE_TAG.get(p["type"]["abbreviation"], p["type"]["abbreviation"])
        print(f"   play {i + 1}: {out}")
        score_msgs = [x for x in out if x.startswith("🏈") and "Kickoff" not in x]
        assert len(score_msgs) == 1 and want_team in score_msgs[0] and want_tag in score_msgs[0], (i, out)
        if p["period"]["number"] not in seen_periods:
            seen_periods.add(p["period"]["number"])
    # the real plays cross quarters (1 -> 2 -> 3 -> 4): confirm at least one quarter-end fired somewhere in the replay
    assert len(seen_periods) >= 2

    print("\n== a touchdown waits for its conversion: ONE message (real NE @ BUF play shape, Oct 4)")
    NE_TD = {"id": "1", "period": {"number": 1}, "clock": {"displayValue": "7:01"}, "team": {"abbreviation": "NE"},
             "type": {"abbreviation": "TD"}, "awayScore": 7, "homeScore": 0,
             "text": "Romeo Doubs 14 Yd pass from Drake Maye (Andy Borregales Kick)"}
    NE_2PT = {**NE_TD, "awayScore": 8, "text": "Romeo Doubs 14 Yd pass from Drake Maye (Two-Point Pass Good)"}
    BUF_FG = {"id": "2", "period": {"number": 2}, "clock": {"displayValue": "3:10"}, "team": {"abbreviation": "BUF"},
              "type": {"abbreviation": "FG"}, "awayScore": 6, "homeScore": 3, "text": "Tyler Bass 45 Yd Field Goal"}
    m.NFL_PLAY_WAIT = 0.01
    base_g = dataclasses.replace(live, away="NE", home="BUF", away_rec="", home_rec="", state="live", period=1,
                                 clock="7:30", ar=0, hr=0)

    def at(ar, hr, clock="7:01", period=1):
        return dataclasses.replace(base_g, ar=ar, hr=hr, clock=clock, period=period)

    async def run_steps(steps, mode="max"):
        """steps: (game, ESPN scoringPlays, seconds to age any held TD by before this poll) -> messages per poll."""
        n = m.NflModule(hub, "bills"); n.state.data["mode"] = mode
        m.espn_get = stub_espn
        stub_espn.plays = []
        await n.process([at(0, 0, "7:30")]); drain(n)
        outs = []
        for g, pl, age in steps:
            stub_espn.plays = pl
            for st in n.gstate.values():
                if st["pend"]:
                    st["pend"]["t0"] -= age
            await n.process([g])
            outs.append(drain(n))
        m.espn_get = real_espn_get
        return outs

    outs = await run_steps([(at(6, 0), [], 0), (at(7, 0), [NE_TD], 0)])
    print("   TD then PAT :", outs)
    assert outs[0] == [] and outs[1] == ["🏈 NE TD! Romeo Doubs 14 Yd pass from Drake Maye (Andy Borregales Kick) | NE 7-0 BUF | Q1 7:01"], outs
    outs = await run_steps([(at(6, 0), [], 0), (at(8, 0), [NE_TD, NE_2PT], 0)])
    print("   TD then 2pt :", outs)
    assert outs[0] == [] and len(outs[1]) == 1 and "TD+2pt!" in outs[1][0] and "NE 8-0 BUF" in outs[1][0], outs
    outs = await run_steps([(at(6, 0), [NE_TD], 0), (at(6, 0), [NE_TD], 30), (at(6, 0), [NE_TD], 100)])
    print("   TD, no PAT  :", outs)
    assert outs[0] == [] and outs[1] == [] and len(outs[2]) == 1 and "NE TD!" in outs[2][0] and "NE 6-0 BUF" in outs[2][0], outs
    outs = await run_steps([(at(6, 0), [NE_TD], 0), (at(6, 0), [NE_TD], 100), (at(7, 0), [NE_TD], 0)])
    print("   late PAT    :", outs)
    assert len(outs[1]) == 1 and outs[2] == ["🏈 NE PAT! | NE 7-0 BUF | Q1 7:01"], outs
    outs = await run_steps([(at(0, 3, "3:10", 1), [BUF_FG], 0)])
    print("   field goal  :", outs)
    assert len(outs[0]) == 1 and outs[0][0] == "🏈 BUF FG! Tyler Bass 45 Yd Field Goal | NE 0-3 BUF | Q1 3:10", outs
    outs = await run_steps([(at(6, 0), [NE_TD], 0), (at(6, 3, "6:30"), [NE_TD, BUF_FG], 0)])
    print("   TD, then the other team scores:", outs)
    assert len(outs[1]) == 2 and "NE TD! Romeo Doubs" in outs[1][0] and "BUF FG! Tyler Bass" in outs[1][1], outs
    outs = await run_steps([(at(6, 0, "0:05"), [NE_TD], 0), (at(6, 0, "15:00", 2), [NE_TD], 0)])
    print("   TD at the buzzer:", outs)
    assert outs[0] == [] and any("NE TD!" in x for x in outs[1]) and any("End of Q1" in x for x in outs[1]), outs
    outs = await run_steps([(at(6, 0), [], 0), (at(7, 0), [], 0)])
    print("   ESPN play list lagging:", outs)
    assert outs[1] == ["🏈 NE TD! | NE 7-0 BUF | Q1 7:01"], outs
    outs = await run_steps([(at(6, 0), [], 0), (at(7, 0), [NE_TD], 0)], mode="min")
    assert outs == [[], []], outs

    print("\n== explicit quarter-end detection (Q1 -> Q2, Q2 -> Q3 labelled 'Half')")
    nfl2 = m.NflModule(hub, "bills"); nfl2.state.data["mode"] = "max"
    stub_espn.plays = []
    await nfl2.process([dataclasses.replace(live, state="live", ar=3, hr=7, period=1, clock="15:00")])
    drain(nfl2)
    out = drain2 = None
    await nfl2.process([dataclasses.replace(live, state="live", ar=3, hr=7, period=2, clock="15:00")])
    out = drain(nfl2)
    assert out == ["⏱ End of Q1: LAC 3-7 BUF"], out
    await nfl2.process([dataclasses.replace(live, state="live", ar=3, hr=7, period=3, clock="15:00")])
    out = drain(nfl2)
    assert out == ["⏱ End of Half: LAC 3-7 BUF"], out
    await nfl2.process([dataclasses.replace(live, state="live", ar=3, hr=7, period=4, clock="15:00")])
    out = drain(nfl2)
    assert out == ["⏱ End of Q3: LAC 3-7 BUF"], out

    print("\n== alert levels: min = quarter results + final only; off = nothing")
    nfl3 = m.NflModule(hub, "bills")
    nfl3.state.data["mode"] = "min"
    stub_espn.plays = []
    await nfl3.process([dataclasses.replace(live, state="preview", ar=0, hr=0, period=0, clock="")]); drain(nfl3)
    await nfl3.process([dataclasses.replace(live, ar=0, hr=0, period=1, clock="15:00", state="live")])
    assert not drain(nfl3), "min: no kickoff message"
    await nfl3.process([dataclasses.replace(live, ar=7, hr=0, period=1, clock="10:00", state="live")])
    assert not drain(nfl3), "min: scores are silent"
    await nfl3.process([dataclasses.replace(live, ar=7, hr=0, period=2, clock="15:00", state="live")])
    assert drain(nfl3) == ["⏱ End of Q1: LAC 7-0 BUF"]
    m.espn_get = real_espn_get

    print("\n== real completed game (Lions @ Bills, 41-31 final) - the real final text")
    fin_data = None
    for g in games:
        if g.state == "final" and {"DET", "BUF"} <= {g.away, g.home}:
            fin_data = g
    assert fin_data, "no finished Lions@Bills game in the fetched schedule"
    nfl4 = m.NflModule(hub, "bills"); nfl4.state.data["mode"] = "max"
    await nfl4.process([dataclasses.replace(fin_data, state="live", ar=fin_data.ar - 1)])
    drain(nfl4)
    await nfl4.process([fin_data])
    out = drain(nfl4)
    print("   ", out)
    assert any(x.startswith("🏁 FINAL: DET 31 @ BUF 41") and "BUF" in x for x in out), out

    print("\n== !bracket: the real 2025 playoffs (Feb 2026), the Bills' game first")
    real_now = m.now
    tl.at_factory(m)(2026, 2, 20, 12)
    r = await say("!bracket", "bills");        print("   latest :", r)
    assert r == ["[BILLS SB] SEA 29 NE 13"], r
    r = await say("!bracket conf", "bills");   print("   conf   :", r)
    assert r[0].startswith("[BILLS CONF] ") and "NE 10 DEN 7" in r[0] and "SEA 31 LAR 27" in r[0], r
    r = await say("!bracket div", "bills");    print("   div    :", r)
    assert r[0].startswith("[BILLS DIV] DEN 33 BUF 30 OT"), r            # the Bills' own game is listed first
    r = await say("!playoffs wc", "bills");   print("   wc     :", r)
    assert r[0].startswith("[BILLS WC] ") and "LAR 34 CAR 31" in r[0], r
    r = await say("!bracket nonsense", "bills")
    assert r == ["[BILLS] !bracket [wc|div|conf|sb]"], r
    tl.at_factory(m)(2026, 9, 1, 12)                                      # preseason of the next season: no playoffs yet
    r = await say("!bracket", "bills")
    assert r == ["[BILLS] No NFL playoff games yet this season."], r
    m.now = real_now

    print("\n== phase filtering: a preseason game does nothing at the default phases (pre off)")
    pre_data = m.parse_nfl_game({
        "status": {"type": {"completed": False, "state": "pre"}}, "period": 0, "displayClock": "0:00",
        "competitors": [{"homeAway": "away", "team": {"abbreviation": "CAR"}, "score": None, "record": []},
                        {"homeAway": "home", "team": {"abbreviation": "BUF"}, "score": None, "record": []}]},
        "pre", "999999", "2026-08-15T17:00Z")
    nfl5 = m.NflModule(hub, "bills"); nfl5.state.data["mode"] = "max"
    await nfl5.process([dataclasses.replace(pre_data, state="preview", ar=0, hr=0)])
    await nfl5.process([dataclasses.replace(pre_data, state="live", ar=0, hr=0, period=1, clock="15:00")])
    await nfl5.process([dataclasses.replace(pre_data, state="live", ar=7, hr=0, period=1, clock="10:00")])
    assert not drain(nfl5), "preseason games are off by default"
    nfl5.phases["pre"] = True
    nfl5.gstate.clear()
    await nfl5.process([dataclasses.replace(pre_data, state="preview", ar=0, hr=0)])
    await nfl5.process([dataclasses.replace(pre_data, state="live", ar=0, hr=0, period=1, clock="15:00")])
    assert drain(nfl5) == ["🏈 Kickoff: CAR @ BUF"], "with pre on, preseason games alert normally"

    for w in workers:
        w.cancel()
    print("\nALL OK")


asyncio.run(main())
