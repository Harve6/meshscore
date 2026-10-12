"""Offline test: fake radio, real APIs for reads, simulated game changes for events."""
import asyncio, copy, dataclasses, os, sys, tempfile
import suitelib as tl
m = tl.bootstrap()
Fake = tl.make_fake(m, {"ohl": 0, "jays": 1, "leafs": 2})


async def main():
    t = Fake()
    hub = m.Hub(t)
    ohl, jays = m.OhlModule(hub), m.JaysModule(hub)
    leafs = m.NhlModule(hub, "leafs")
    leafs.phases["pre"] = True          # these tests use a real preseason game; the default now skips preseason
    hub.add(ohl); hub.add(jays); hub.add(leafs)
    workers = [asyncio.create_task(hub.tx_worker()), asyncio.create_task(hub.cmd_worker())]
    settle = tl.settle
    say = tl.make_say_raw(hub, t, m)

    print("== MLB pregame is not live")
    for det, ab, want in (("Warmup", "Live", "preview"), ("Pre-Game", "Preview", "preview"), ("Delayed Start: Rain", "Live", "preview"),
                          ("In Progress", "Live", "live"), ("Final", "Final", "final"), ("Postponed", "Final", "postponed")):
        g = m.parse_mgame({"gamePk": 1, "gameDate": "2026-09-20T18:35:00Z", "status": {"abstractGameState": ab, "detailedState": det},
                           "teams": {"away": {"team": {"id": 141, "abbreviation": "TOR"}}, "home": {"team": {"id": 140, "abbreviation": "TEX"}}}})
        assert g.state == want, (det, g.state)
    d = m.parse_mgame({"gamePk": 2, "gameDate": "2026-09-21T22:35:00Z", "status": {"abstractGameState": "Preview", "detailedState": "Delayed Start", "reason": "Inclement Weather"},
                       "teams": {"away": {"team": {"id": 141, "abbreviation": "TOR"}}, "home": {"team": {"id": 110, "abbreviation": "BAL"}}}})
    assert d.state == "preview" and d.delayed and d.detail == "Delayed Start (Inclement Weather)", (d.state, d.detail)
    print("  ok")

    print("== OHL pregame (status 2 but clock still 20:00) is not puck drop")
    raw = m.extract_games(m.api_get(m.scorebar_url(0, 1)))[0]
    pre = dataclasses.replace(raw, code=2, period=1, clock="20:00", hg=0, ag=0, intermission=False)
    assert pre.pregame and not pre.live and pre.scheduled and pre.state_code == 1
    go = dataclasses.replace(pre, clock="19:41")
    assert go.live and not go.pregame and go.state_code == 2
    assert dataclasses.replace(pre, clock="20:00", hg=1).live          # a goal at 20:00 still counts
    o = m.OhlModule(m.Hub(Fake()))
    o.gstate = {}
    await o.process_games([dataclasses.replace(raw, code=1, hg=0, ag=0, period=1, clock="20:00", intermission=False)])                # baseline: scheduled
    evs = await o.game_events(pre, o.gstate[raw.id], [pre])
    assert not evs, evs                                                # pregame: silent
    await o.process_games([pre])
    evs = await o.game_events(go, o.gstate[raw.id], [go])
    assert [e.kind for e in evs] == ["start"], evs                     # clock moves: puck drop
    print("  ok")

    print("== OHL game summary (real finished game 29002, Brantford 6 - Guelph 0)")
    data = m.api_get(m.summary_url("29002"))
    goals, shots, stars = m.parse_goals(data), m.parse_shots(data), m.parse_stars(data)
    print(f"  goals={len(goals)} first={goals[0] if goals else None} shots={shots} stars={stars}")
    assert len(goals) == 6 and all(x["scorer"] for x in goals), goals
    assert shots == (31, 45), shots                        # (away, home)
    assert len(stars) == 3 and all(stars), stars

    print("== second copy of the bot is refused")
    import subprocess, socket
    m.CFG["instance_port"] = 47699
    assert m.acquire_single_instance()                      # first copy gets it
    probe = socket.socket(); busy = False
    try:
        probe.bind(("127.0.0.1", 47699))
    except OSError:
        busy = True
    probe.close()
    assert busy, "port should be held by the first copy"
    m._instance_lock.close(); m._instance_lock = None       # released when the first copy exits
    assert m.acquire_single_instance()
    m._instance_lock.close(); m._instance_lock = None
    print("  ok")

    print("== alert levels max / min / off (OHL) and busy nights")
    m.CFG.update(busy_live_games=3, summary_fetch=False)
    o3 = m.OhlModule(m.Hub(Fake()))
    o3.state.data.update(mode="max", subs={"abc123": {"name": "fan", "teams": ["AAA"]}})

    def mk(i, a, h, ag, hg, code=2, inter=False, period=1):
        return dataclasses.replace(raw, id=str(i), code=code, period=period, period_name="1st", clock="10:00", intermission=inter, ag=ag, hg=hg,
                                   vs=dataclasses.replace(raw.vs, code=a), hs=dataclasses.replace(raw.hs, code=h))

    def drain(mod):
        items = []
        while not mod.hub.out.empty():
            items.append(mod.hub.out.get_nowait()[:3])
        return items

    def show(label, items):
        print(f"  {label}: " + (" || ".join(f"{k}:{x}" for k, _, x in items) if items else "(nothing)"))

    g1, g2 = mk(1, "AAA", "BBB", 0, 0), mk(2, "CCC", "DDD", 0, 0)
    await o3.process_games([g1, g2]); drain(o3)                       # baseline, 2 live: not busy
    assert not o3.busy
    await o3.process_games([mk(1, "AAA", "BBB", 1, 0), g2])
    q = drain(o3); show("max, 2 games live, goal", q)
    assert any(k == "chan" and "GOAL" in x for k, _, x in q)          # normal night: the goal goes to the channel

    g3 = mk(3, "EEE", "FFF", 0, 0)
    await o3.process_games([mk(1, "AAA", "BBB", 1, 0), g2, g3])
    q = drain(o3); show("3rd game starts", q)
    assert o3.busy and len(q) == 1 and "Busy night" in q[0][2]        # one notice
    assert o3.effective_level() == "min" and o3.level() == "max"

    await o3.process_games([mk(1, "AAA", "BBB", 2, 0), g2, g3])
    q = drain(o3); show("busy: goal", q)
    assert not any(k == "chan" for k, _, _ in q)                      # the channel stays quiet
    assert any(k == "dm" and "AAA" in x for k, _, x in q)             # but the AAA follower still gets a DM

    await o3.process_games([mk(1, "AAA", "BBB", 2, 0, inter=True), g2, g3])
    q = drain(o3); show("busy: end of a period", q)
    assert [x for k, _, x in q if k == "chan"] == ["⏸ End of 1st: AAA 2-0 BBB"]        # period results still go out

    await o3.process_games([mk(1, "AAA", "BBB", 2, 0, inter=True), mk(2, "CCC", "DDD", 3, 1, code=4), g3])
    q = drain(o3); show("busy: a game ends", q)
    assert len(q) == 1 and "FINAL" in q[0][2]

    await o3.process_games([mk(1, "AAA", "BBB", 2, 0, code=4), mk(2, "CCC", "DDD", 3, 1, code=4), mk(3, "EEE", "FFF", 0, 0, code=4)])
    drain(o3)
    assert not o3.busy                                                # nothing live: back to normal
    await o3.process_games([mk(1, "AAA", "BBB", 2, 0, code=4), mk(2, "CCC", "DDD", 3, 1, code=4), mk(3, "EEE", "FFF", 0, 0, code=4)])

    print("  -- min: period results + final, no goals; off: nothing; old words still work")
    o4 = m.OhlModule(m.Hub(Fake())); o4.state.data.update(mode="min", subs={})
    await o4.process_games([mk(1, "AAA", "BBB", 0, 0)]); drain(o4)
    await o4.process_games([mk(1, "AAA", "BBB", 1, 0)])
    assert not drain(o4), "min: a goal is silent"
    await o4.process_games([mk(1, "AAA", "BBB", 1, 0, inter=True)])
    assert [x for _, _, x in drain(o4)] == ["⏸ End of 1st: AAA 1-0 BBB"]
    await o4.process_games([mk(1, "AAA", "BBB", 3, 0, code=4, inter=True)])
    assert any("FINAL" in x for _, _, x in drain(o4))
    print("  -- restart during an intermission: the period that already ended is NOT announced (seen live Oct 12)")
    o6 = m.OhlModule(m.Hub(Fake())); o6.state.data.update(mode="max", subs={})
    await o6.process_games([mk(1, "AAA", "BBB", 1, 0, inter=True)])               # first sighting: already in the 1st intermission
    await o6.process_games([mk(1, "AAA", "BBB", 1, 0, inter=True)])
    assert not drain(o6), "stale End of 1st announced after a restart"
    await o6.process_games([mk(1, "AAA", "BBB", 1, 0, period=2)])                # 2nd period under way
    drain(o6)
    await o6.process_games([mk(1, "AAA", "BBB", 1, 0, inter=True, period=2)])
    assert [x for _, _, x in drain(o6)] == ["⏸ End of 2nd: AAA 1-0 BBB"], "the next intermission must still be announced"
    o5x = m.OhlModule(m.Hub(Fake())); o5x.state.data.update(mode="off", subs={})
    await o5x.process_games([mk(1, "AAA", "BBB", 0, 0)]); drain(o5x)
    await o5x.process_games([mk(1, "AAA", "BBB", 1, 0, inter=True)])
    await o5x.process_games([mk(1, "AAA", "BBB", 3, 0, code=4)])
    assert not drain(o5x), "off: nothing"
    for word, level in (("all", "max"), ("full", "max"), ("digest", "min"), ("goals", "min"), ("runs", "min"), ("finals", "min"), ("off", "off"), ("max", "max")):
        assert m.norm_mode(word) == level, word
    legacy = m.OhlModule(m.Hub(Fake())); legacy.state.data["mode"] = "finals"
    assert legacy.level() == "min"
    print("  -- !mode text")
    inc = m.Incoming("!mode", False, "Tester", "", None, None, "ohl")
    r = (await o3.cmd_mode(inc, ""))[0]; print("   ", r); assert r.startswith("[OHL BOT] Alert level: max") and "!mode max|min|off" in r and "period results" in r
    r = (await o3.cmd_mode(inc, "digest"))[0]; print("   ", r); assert r.startswith("[OHL BOT] Alert level set to min.")
    r = (await o3.cmd_mode(inc, "bogus"))[0]; print("   ", r); assert "!mode max (everything) | min" in r
    r = (await o3.cmd_mode(inc, "max"))[0]; assert "set to max" in r
    m.CFG.update(busy_live_games=3, summary_fetch=True)
    print("  ok")

    print("== priority teams (#ohl): KIT and GUE always get full alerts")
    m.CFG.update(busy_live_games=3, summary_fetch=False)
    assert m.OhlModule(m.Hub(Fake())).priority == ["KIT", "GUE"]                          # the default
    op = m.OhlModule(m.Hub(Fake()))
    op.state.data.update(mode="min", subs={}, priority=None)
    op.priority = ["KIT", "GUE"]
    await op.process_games([mk(1, "KIT", "BBB", 0, 0), mk(2, "AAA", "CCC", 0, 0)]); drain(op)
    await op.process_games([mk(1, "KIT", "BBB", 1, 0), mk(2, "AAA", "CCC", 1, 0)])
    out = [x for _, _, x in drain(op)]
    print("   min: KIT game and another game each score:", out)
    assert out == ["🚨 KIT GOAL | KIT 1-0 BBB | 1st 10:00"], out                          # the priority game is at max, the other stays at min
    await op.process_games([mk(1, "KIT", "BBB", 1, 0, inter=True), mk(2, "AAA", "CCC", 1, 0, inter=True)])
    assert sorted(x for _, _, x in drain(op)) == ["⏸ End of 1st: AAA 1-0 CCC", "⏸ End of 1st: KIT 1-0 BBB"]
    op.state.data["mode"] = "off"
    await op.process_games([mk(1, "KIT", "BBB", 2, 0, inter=True), mk(2, "AAA", "CCC", 1, 0, inter=True)])
    assert not drain(op), "off means off, priority or not"
    op.state.data["mode"] = "max"
    op.gstate.clear()
    print("   busy night: 3 other games live, and GUE")
    others = [mk(10 + i, "OA" + str(i), "OB" + str(i), 0, 0) for i in range(3)]
    gue = mk(20, "GUE", "ZZZ", 0, 0)
    await op.process_games(others + [gue]); q = drain(op)
    assert op.busy and len(q) == 1 and "Busy night" in q[0][2] and "KIT/GUE stay full" in q[0][2], q
    await op.process_games([dataclasses.replace(others[0], ag=1)] + others[1:] + [dataclasses.replace(gue, hg=1)])
    out = [x for _, _, x in drain(op)]
    print("   ", out)
    assert out == ["🚨 ZZZ GOAL | GUE 0-1 ZZZ | 1st 10:00"], out                          # only the priority game's goal is announced
    print("   priority games do not count towards a busy night")
    op2 = m.OhlModule(m.Hub(Fake())); op2.state.data.update(mode="max", subs={}); op2.priority = ["KIT", "GUE"]
    await op2.process_games([mk(30, "KIT", "A", 0, 0), mk(31, "GUE", "B", 0, 0), mk(32, "C", "D", 0, 0)]); drain(op2)
    assert not op2.busy
    print("   !priority")
    inc = m.Incoming("!priority", False, "Tester", "", None, None, "ohl")
    async def prio(*words):
        inc.rest = list(words)
        return (await op2.cmd_priority(inc, words[0] if words else ""))[0]
    r = await prio(); print("   ", r); assert r.startswith("[OHL BOT] Priority teams: KIT, GUE") and "!priority off" in r
    r = await prio("LDN", "kitchener"); print("   ", r); assert r == "[OHL BOT] Priority teams: LDN, KIT - their games always get full alerts.", r
    r = await prio("zzz"); assert "Unknown team (ZZZ)" in r, r
    r = await prio("off"); assert r == "[OHL BOT] Priority teams: none.", r
    assert op2.priority == [] and op2.state.data["priority"] == []
    m.CFG["admin_names"] = ["boss"]
    inc.sender_name = "someone"; r = await prio("KIT"); assert "Only admins" in r, r
    m.CFG["admin_names"] = []
    r = await prio("KIT", "GUE"); assert "KIT, GUE" in r
    m.CFG.update(busy_live_games=3, summary_fetch=True)
    print("  ok")

    print("== OHL period end (clock 00:00) and one-off stale updates")
    m.CFG.update(summary_fetch=False)
    o5 = m.OhlModule(m.Hub(Fake()))
    o5.state.data.update(mode="full", subs={})

    def mk2(period, clock, ag, hg, code=2):
        return dataclasses.replace(mk(9, "NBB", "OSS", ag, hg, period=period, code=code), clock=clock,
                                   period_name={1: "1st", 2: "2nd", 3: "3rd"}[period])

    async def step(label, game, expect):
        await o5.process_games([game])
        texts = [x for _, _, x in drain(o5)]
        print(f"  {label}: {texts if texts else '(nothing)'}")
        assert len(texts) == len(expect) and all(e in x for e, x in zip(expect, texts)), (label, texts)

    await o5.process_games([mk2(1, "05:00", 1, 3)]); drain(o5)                     # baseline
    await step("end of 1st (clock 00:00)", mk2(1, "00:00", 1, 3), ["End of 1st"])
    await step("2nd about to start", mk2(2, "20:00", 1, 3), [])
    await step("stale: back to 1st", mk2(1, "02:24", 1, 3), [])                     # the flicker seen on the real feed
    await step("2nd again", mk2(2, "20:00", 1, 3), [])                              # no repeat intermission, no correction
    await step("stale: older score", mk2(2, "18:00", 0, 3), [])
    await step("score back to normal", mk2(2, "17:40", 1, 3), [])
    await step("lower score once", mk2(2, "17:00", 0, 3), [])
    await step("lower score twice = real correction", mk2(2, "16:40", 0, 3), ["Score update"])
    await step("end of 2nd", mk2(2, "00:00", 0, 3), ["End of 2nd"])
    await step("3rd period running", mk2(3, "05:00", 0, 3), [])
    await step("end of 3rd is not an intermission", mk2(3, "00:00", 0, 3), [])
    assert "End 2nd" in m.game_line(mk2(2, "00:00", 0, 3))
    print("  -- the scoreboard skips 00:00 and jumps to the next period (seen live, ~2 min feed lag)")
    o6 = m.OhlModule(m.Hub(Fake()))
    o6.state.data.update(mode="full", subs={})

    async def step6(label, game, expect):
        await o6.process_games([game])
        texts = [x for _, _, x in drain(o6)]
        print(f"  {label}: {texts if texts else '(nothing)'}")
        assert len(texts) == len(expect) and all(e in x for e, x in zip(expect, texts)), (label, texts)
    await o6.process_games([mk2(2, "00:56", 1, 5)]); drain(o6)                      # baseline, late in the 2nd
    await step6("jumps to 3rd", mk2(3, "20:00", 1, 5), ["End of 2nd"])              # 00:00 never seen
    await step6("3rd, later", mk2(3, "18:00", 1, 5), [])                             # no repeat
    o7 = o6.__class__(o6.hub); o7.state.data.update(mode="full", subs={})
    o6 = o7
    await o6.process_games([mk2(1, "03:00", 0, 0)]); drain(o6)
    await step6("jumps from 1st to 2nd", mk2(2, "19:20", 0, 0), ["End of 1st"])
    await step6("00:00 seen and then the jump: only one alert", mk2(2, "00:00", 0, 0), ["End of 2nd"])
    await step6("...then 3rd", mk2(3, "20:00", 0, 0), [])
    m.CFG.update(summary_fetch=True)
    print("  ok")

    print("== MLB leaders, player lines and !follow PLAYER (real 2026 data)")
    await say("!leaders", "jays")
    r = await say("!leaders hr", "jays")
    assert r and r[0][1].startswith("[JAYS HR]") and r[0][1].count(".") >= 5, r
    r = await say("!leaders k al", "jays")
    assert r and r[0][1].startswith("[JAYS K AL]"), r
    r = await say("!leaders era", "jays")
    r = await say("!leaders jays", "jays")
    assert r and r[0][1].startswith("[JAYS TEAM]") and "HR" in r[0][1], r
    r = await say("!leaders bogus", "jays")
    assert "Unknown stat" in r[0][1], r
    r = await say("!player pca", "jays")
    assert r and "Crow-Armstrong" in r[0][1] and "HR" in r[0][1], r
    await say("!player Aaron Judge", "jays")
    r = await say("!player guerrero", "jays")
    assert "Several players" in r[0][1], r
    r = await say("!player zzzznotaplayer", "jays")
    assert "No active MLB player" in r[0][1], r

    key = "abcdef123456"
    r = await say("!jays follow pca", dm=True, key=key)
    assert "Following P. Crow-Armstrong" in r[0][1], r
    r = await say("!jays follow Aaron Judge", dm=True, key=key)
    assert "Following A. Judge" in r[0][1], r
    r = await say("!jays following", dm=True, key=key)
    assert "P. Crow-Armstrong" in r[0][1] and "A. Judge" in r[0][1], r
    r = await say("!jays follow pca", dm=True, key=key)
    assert "Already following" in r[0][1], r
    await say("!jays follow ohtani", dm=True, key=key)
    r = await say("!jays follow Juan Soto", dm=True, key=key)
    assert "Max 3 players" in r[0][1], r
    r = await say("!jays unfollow judge", dm=True, key=key)
    assert "Updated" in r[0][1] and "A. Judge" not in r[0][1], r
    r = await say("!jays unfollow", dm=True, key=key)
    assert "No players left" in r[0][1], r

    print("\n== follow a hitter and a pitcher: replay of a real game (2025 World Series Game 7)")
    real_mlb_get = m.mlb_get
    GAME = 813024
    feed = real_mlb_get(f"/v1.1/game/{GAME}/feed/live", {"fields": "liveData,plays,allPlays,about,atBatIndex,inning,halfInning,isComplete,result,eventType,description,rbi,awayScore,homeScore,matchup,batter,pitcher,id,fullName"})
    all_plays = feed["liveData"]["plays"]["allPlays"]
    shown = {"n": 0}

    def fake_mlb_get(path, params=None):
        if path.endswith("/feed/live"):
            return {"liveData": {"plays": {"allPlays": all_plays[: shown["n"]]}}}
        return real_mlb_get(path, params)
    m.mlb_get = fake_mlb_get
    sched = real_mlb_get("/v1/schedule", {"sportId": 1, "gamePk": GAME, "hydrate": "team,linescore,decisions"})
    gm = m.parse_mgame(sched["dates"][0]["games"][0])
    jf = m.JaysModule(m.Hub(Fake()))
    jf.state.data["pfollows"] = {"fan1": {"name": "fan", "players": [{"id": 666182, "name": "Bo Bichette"},
                                                                     {"id": 660271, "name": "Shohei Ohtani"}]}}
    fol = jf.followed()
    st = {"seen": -1, "k": {}, "entered": set(), "final": False}
    shown["n"] = 3
    await jf.follow_plays(gm, st, fol, silent=True)                    # bot starts mid-game: no alerts
    assert not drain(jf), "baseline must be silent"
    seen_before = st["seen"]
    shown["n"] = len(all_plays)
    await jf.follow_plays(gm, st, fol, silent=False)
    items = drain(jf)
    assert all(dest == "fan1" for k, dest, x in items if k == "dm")          # only the follower is messaged
    dms = [x for k, _, x in items if k == "dm"]
    for x in dms:
        print("   ", x)
    later = all_plays[seen_before + 1:]
    bichette_hr = sum(1 for p in later if p["matchup"]["batter"]["id"] == 666182 and p["result"]["eventType"] == "home_run")
    is_k = lambda p: p["matchup"]["pitcher"]["id"] == 660271 and p["result"]["eventType"].startswith("strikeout")
    k_base, k_all = sum(map(is_k, all_plays[: seen_before + 1])), sum(map(is_k, all_plays))
    print(f"    (Bichette HRs after the baseline: {bichette_hr}; Ohtani strikeouts: {k_base} before, {k_all} total)")
    assert sum("💥 HR!" in x for x in dms) == bichette_hr
    assert sum("K |" in x and "Ohtani" in x for x in dms) == k_all // 5 - k_base // 5
    await jf.follow_final(gm, fol)
    finals = [x for k, _, x in drain(jf) if k == "dm"]
    for x in finals:
        print("   ", x)
    assert any("Bichette" in x and "🏁" in x for x in finals), finals
    assert any("Ohtani" in x and "IP" in x for x in finals), finals          # pitcher line too

    print("  -- synthetic: strikeout milestones, entering the game, RBI")
    def play(i, batter, pitcher, ev="strikeout", rbi=0, desc="strikes out swinging."):
        return {"about": {"atBatIndex": i, "isComplete": True, "halfInning": "top", "inning": 3},
                "result": {"eventType": ev, "description": desc, "rbi": rbi, "awayScore": 0, "homeScore": 2},
                "matchup": {"batter": {"id": batter}, "pitcher": {"id": pitcher}}}
    fake_feed = [play(i, 5000 + i, 111) for i in range(12)] + [play(12, 222, 333, "single", 2, "Test Hitter singles. Runner scores.")]
    all_plays[:] = fake_feed
    shown["n"] = len(fake_feed)
    js = m.JaysModule(m.Hub(Fake()))
    js.state.data["pfollows"] = {"k1": {"name": "x", "players": [{"id": 111, "name": "Test Pitcher"}, {"id": 222, "name": "Test Hitter"}]}}
    fol2, st2 = js.followed(), {"seen": -1, "k": {}, "entered": set(), "final": False}
    await js.follow_plays(gm, st2, fol2, silent=False)
    got = [x for k, _, x in drain(js) if k == "dm"]
    for x in got:
        print("   ", x)
    assert sum("on the mound" in x for x in got) == 1 and any("5 K" in x for x in got) and any("10 K" in x for x in got)
    assert not any("15 K" in x for x in got) and any("RBI:" in x and "Test Hitter singles" in x for x in got)
    await js.follow_plays(gm, st2, fol2, silent=False)                        # same plays again: no repeats
    assert not drain(js), "plays must not be announced twice"
    m.mlb_get = real_mlb_get

    print("\n== Blue Jays alert levels: max = every run, min = score after 3 and 6 innings + final, off = nothing")

    def bjs(pk=501):
        return m.parse_mgame({"gamePk": pk, "gameType": "R", "gameDate": "2026-09-20T18:35:00Z",
                              "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
                              "teams": {"away": {"team": {"id": 141, "abbreviation": "TOR"}}, "home": {"team": {"id": 110, "abbreviation": "BAL"}}}})

    async def no_play(g):
        return ""

    def lvg(g, ar, hr, inn, txt, **kw):
        return dataclasses.replace(g, state="live", ar=ar, hr=hr, inning=inn, inn_txt=txt, **kw)

    async def run_game(level, steps, start=None):
        jl = m.JaysModule(m.Hub(Fake()))
        jl.state.data["mode"] = level
        jl.scoring_play = no_play
        g0 = bjs()
        await jl.process([start(g0) if start else g0])            # baseline
        drain(jl)
        seen = []
        for label, g in steps(g0):
            await jl.process([g])
            out = [x for _, _, x in drain(jl)]
            print(f"   {level}: {label}: {out if out else '(nothing)'}")
            seen.append(out)
        return seen

    def steps(g0):
        return [("first pitch", lvg(g0, 0, 0, 1, "Top 1st")),
                ("run in the 2nd", lvg(g0, 1, 0, 2, "Top 2nd")),
                ("end of the 3rd", lvg(g0, 1, 0, 3, "End 3rd")),
                ("same again", lvg(g0, 1, 0, 3, "End 3rd")),
                ("run in the 5th", lvg(g0, 2, 0, 5, "Top 5th")),
                ("poll gap: already in the 7th", lvg(g0, 2, 1, 7, "Top 7th")),
                ("extra innings", lvg(g0, 2, 2, 10, "Top 10th")),
                ("rain delay", dataclasses.replace(lvg(g0, 2, 2, 10, "Top 10th"), delayed=True, detail="Delayed: Rain")),
                ("final", dataclasses.replace(g0, state="final", ar=3, hr=2, inning=10, inn_txt="", win="Gausman", loss="Eovaldi"))]
    mx = await run_game("max", steps)
    assert mx[0] and "Play ball" in mx[0][0] and "RUN" in mx[1][0] and not mx[2] and not mx[3], mx      # every run, no inning scores
    assert any("FINAL/10" in x for x in mx[-1])
    mn = await run_game("min", steps)
    assert not mn[0] and not mn[1] and mn[2] == ["⚾ End of 3rd: TOR 1-0 BAL"] and not mn[3] and not mn[4], mn   # no first pitch, no runs
    assert mn[5] == ["⚾ Top 7th: TOR 2-1 BAL"], mn[5]                                                       # the 6th was missed: reported when seen
    assert any("Extra innings" in x for x in mn[6]) and any("Delayed" in x for x in mn[7]) and any("FINAL/10" in x for x in mn[8]), mn
    of = await run_game("off", steps)
    assert not any(of), of
    late = await run_game("min", lambda g0: [("2 outs later, 4th inning", lvg(g0, 1, 0, 4, "Top 4th")), ("end of the 6th", lvg(g0, 1, 0, 6, "End 6th"))],
                          start=lambda g: lvg(g, 1, 0, 4, "Bot 4th"))             # the bot started mid-game: the 3rd is not reported late
    assert late == [[], ["⚾ End of 6th: TOR 1-0 BAL"]], late
    old = m.JaysModule(m.Hub(Fake())); old.state.data["mode"] = "runs"
    assert old.level() == "min"
    inc = m.Incoming("!mode", False, "Tester", "", None, None, "jays")
    r = (await old.cmd_mode(inc, ""))[0]; print("   ", r)
    assert r.startswith("[JAYS BOT] Alert level: min") and "score every 3 innings" in r
    print("  ok")

    print("== OHL final when the feed is slow to close a finished game (seen live: ~20 min late)")
    m.CFG.update(summary_fetch=False, early_final_seconds=0.3)
    o8 = m.OhlModule(m.Hub(Fake())); o8.state.data.update(mode="full", subs={})

    async def step8(label, game, expect):
        await o8.process_games([game])
        texts = [x for _, _, x in drain(o8)]
        print(f"  {label}: {texts if texts else '(nothing)'}")
        assert len(texts) == len(expect) and all(e in x for e, x in zip(expect, texts)), (label, texts)
    await o8.process_games([mk2(3, "04:00", 2, 7)]); drain(o8)                                # baseline, 3rd period
    await step8("3rd, 00:00, one team ahead (just seen)", mk2(3, "00:00", 2, 7), [])
    await asyncio.sleep(0.5)
    await step8("still 00:00 after the wait: final goes out", mk2(3, "00:00", 2, 7), ["FINAL"])
    await step8("feed finally says Final: no duplicate", mk2(3, "00:00", 2, 7, code=4), [])
    o9 = m.OhlModule(m.Hub(Fake())); o9.state.data.update(mode="full", subs={}); o8 = o9
    await o8.process_games([mk2(3, "04:00", 3, 3)]); drain(o8)
    await step8("3rd, 00:00, tied: no final (overtime is coming)", mk2(3, "00:00", 3, 3), [])
    await asyncio.sleep(0.5)
    await step8("still tied", mk2(3, "00:00", 3, 3), [])
    o10 = m.OhlModule(m.Hub(Fake())); o10.state.data.update(mode="full", subs={}); o8 = o10
    await o8.process_games([mk2(3, "04:00", 2, 6)]); drain(o8)
    await step8("00:00 then the feed closes the game on time", mk2(3, "00:00", 2, 6, code=4), ["FINAL"])
    m.CFG.update(summary_fetch=True, early_final_seconds=90)
    print("  ok")

    print("== OHL: a hockey game can't really end tied (real bug, Sept 26: KIT/ER final falsely reported 3-3)")
    m.CFG.update(summary_fetch=False, early_final_seconds=90)
    o11 = m.OhlModule(m.Hub(Fake())); o11.state.data.update(mode="full", subs={})

    async def step11(label, game, expect):
        await o11.process_games([game])
        texts = [x for _, _, x in drain(o11)]
        print(f"  {label}: {texts if texts else '(nothing)'}")
        assert len(texts) == len(expect) and all(e in x for e, x in zip(expect, texts)), (label, texts)
        return texts
    def ot2(clock, ag, hg, code=2):
        return dataclasses.replace(mk2(3, clock, ag, hg, code), period=4, period_name="OT")
    await o11.process_games([mk2(3, "05:00", 3, 3)]); drain(o11)                          # baseline, still in the 3rd
    await step11("OT starts, still tied 3-3", ot2("05:00", 3, 3), ["OT:"])
    await step11("feed glitch: reports Final at 3-3 (must be ignored - hockey has no tied finals)",
                 ot2("05:00", 3, 3, code=4), [])
    await step11("OT winner scored: goal announced as normal", ot2("04:02", 4, 3), ["GOAL"])
    clean_final = await step11("feed properly finals the game: a clean final, not a correction", ot2("04:02", 4, 3, code=4), ["FINAL"])
    assert "(corrected)" not in clean_final[0], clean_final
    await step11("same final seen again: no repeat", ot2("04:02", 4, 3, code=4), [])

    print("  -- safety net: if a wrong final ever slips out anyway, a real one later corrects it")
    o12 = m.OhlModule(m.Hub(Fake())); o12.state.data.update(mode="full", subs={})
    await o12.process_games([mk2(3, "05:00", 3, 3)]); drain(o12)
    o12.gstate["9"]["final_sent"], o12.gstate["9"]["final_score"] = True, (3, 3)           # simulate a final that slipped through wrong
    r = await o12.game_events(ot2("04:02", 4, 3, code=4), o12.gstate["9"], [])
    texts = [x for e in r for x in e.texts]
    print("   ", texts)
    assert any("FINAL" in x and "(corrected)" in x and "KIT" in x for x in texts) or any("FINAL" in x and "(corrected)" in x for x in texts), texts
    m.CFG.update(summary_fetch=True, early_final_seconds=90)
    print("  ok")

    print("== a rescheduled game listed twice with the same gamePk (real bug, Sept 23: 6 junk messages at restart)")
    real_get = m.mlb_get

    def entry(date_iso, detail, ar=None, hr=None, ab="Final"):
        team = lambda i, ab_: {"team": {"id": i, "abbreviation": ab_}, **({"score": ar if i == 141 else hr} if ar is not None else {})}
        return {"gamePk": 824785, "gameDate": date_iso, "status": {"abstractGameState": ab, "detailedState": detail},
                "teams": {"away": team(141, "TOR"), "home": team(110, "BAL")},
                "decisions": {"winner": {"fullName": "Kyle Bassitt"}, "loser": {"fullName": "Max Scherzer"}}}
    postponed = entry("2026-09-22T22:35:00Z", "Postponed")
    played = entry("2026-09-23T17:35:00Z", "Final", ar=2, hr=4)
    for label, dates in (("postponed first", [postponed, played]), ("played first", [played, postponed])):
        m.mlb_get = lambda path, params=None, _d=dates: {"dates": [{"date": "x", "games": _d}]}
        dj = m.JaysModule(m.Hub(Fake()))
        gs = await dj.refresh(0)
        assert len(gs) == 1 and gs[0].final and (gs[0].ar, gs[0].hr) == (2, 4), [(g.state, g.ar, g.hr) for g in gs]
        for i in range(3):
            await dj.process(await dj.refresh(0)); dj.games_at = 0
        assert not drain(dj), "restart must be silent"
        print(f"  {label}: one game (final 2-4), no messages")
    only_post = m.dedupe_mgames([m.parse_mgame(postponed), m.parse_mgame(postponed)])
    assert len(only_post) == 1 and only_post[0].state == "postponed"
    m.mlb_get = real_get

    print("== silent restart option")
    class Up(Fake):
        async def start(self, on_message, channels):
            self.alive = True
    for flag in (True, False):
        m.CFG["announce_on_start"] = flag
        tt = Up(); hh = m.Hub(tt); hh.add(m.JaysModule(hh))
        task = asyncio.create_task(hh.transport_supervisor())
        await tl.until(lambda: hh.out.qsize() >= 1, timeout=1.0 if flag else 0.3)   # silent case: nothing may arrive
        task.cancel()
        queued = hh.out.qsize()
        print(f"  announce_on_start={flag}: {queued} startup message(s) queued")
        assert queued == (1 if flag else 0)
    m.CFG["announce_on_start"] = True

    print("== routing / commands")
    await say("!score", "jays")
    await say("!scores", "ohl")
    await say("!score", "ohl")                      # jays-only command on the OHL channel -> ignored
    await say("!jays next", "ohl")                  # explicit prefix works anywhere
    await say("!next", "jays")
    await say("!last", "jays")
    await say("!standings", "jays")
    await say("!standings wc", "jays")
    await say("!standings west", "jays")
    await say("!team KIT", "ohl")
    await say("!standings east", "ohl")
    await say("!scores", dm=True)                   # DM default -> ohl
    await say("!jays score", dm=True)
    await say("!follow KIT", dm=True)
    await say("!ping", "jays")
    await say("!botstatus", "jays")
    await say("!botstatus", "ohl")
    await say("!mode runs", "jays")
    await say("!help", "jays")
    await say("!jays", "jays")
    await say("[JAYS] hello !score", "jays")        # own echo ignored
    await say("hello", "jays")

    print("\n== leafs commands")
    await say("!score", "leafs")
    await say("!next", "leafs")
    await say("!last", "leafs")
    await say("!standings", "leafs")
    await say("!standings wc", "leafs")
    await say("!standings west", "leafs")
    await say("!standings pac", "leafs")
    await say("!leafs next", "ohl")
    await say("!leafs", "leafs")
    await say("!ping", "leafs")
    await say("!botstatus", "leafs")
    await say("!score", "jays")                     # still routed to the jays

    print("\n== simulated Leafs game")
    lg = await leafs.refresh(0)
    lb = next(g for g in lg if g.scheduled and g.start > m.now())
    leafs.state.data["mode"] = "all"
    tor_home = lb.home == "TOR"
    other = lb.away if tor_home else lb.home

    def goal(team, first, last, pid, strength="ev", mod="none"):
        return {"teamAbbrev": team, "firstName": {"default": first}, "lastName": {"default": last},
                "playerId": pid, "strength": strength, "goalModifier": mod}
    G1 = goal(other, "Cole", "Caufield", 1)
    G2 = goal("TOR", "Auston", "Matthews", 34, "pp")
    G3 = goal("TOR", "Auston", "Matthews", 34)
    G4 = goal("TOR", "Auston", "Matthews", 34, "ev", "empty-net")

    async def fake_landing(gid):
        return {"awayTeam": {"sog": 21}, "homeTeam": {"sog": 30},
                "summary": {"threeStars": [{"name": {"default": "A. Matthews"}}, {"name": {"default": "C. Caufield"}},
                                           {"name": {"default": "J. Woll"}}]}}
    leafs.landing = fake_landing

    def ls(**kw):
        d = dict(state="live", period=1, ptype="REG", clock="12:00", inter=False)
        d.update(kw)
        return dataclasses.replace(lb, **d)

    def sc(tor, opp):  # (away, home) scores from TOR / other
        return {"ar": opp if tor_home else tor, "hr": tor if tor_home else opp}

    async def feed2(g):
        t.sent.clear()
        await leafs.process([g])
        await settle(hub, 1.0)
        for c, x in t.sent:
            print(f"      -> {c}: {x}  ({len(x.encode())}B)")
        if not t.sent:
            print("      (nothing)")
        assert all(len(x.encode()) <= m.CFG["max_msg_bytes"] for _, x in t.sent), "message too long"

    O2 = goal(other, "Nick", "Suzuki", 2)
    O3 = goal(other, "Juraj", "Slafkovsky", 3, "sh")
    print("  baseline");             await feed2(lb)
    print("  puck drop");            await feed2(ls(**sc(0, 0)))
    print("  opp goal 0-1");         await feed2(ls(period=1, clock="04:27", goals=[G1], **sc(0, 1)))
    print("  TOR PP goal 1-1");      await feed2(ls(period=2, clock="16:04", goals=[G1, G2], **sc(1, 1)))
    print("  intermission");         await feed2(ls(period=2, clock="00:00", inter=True, goals=[G1, G2], **sc(1, 1)))
    print("  TOR goal 2-1");         await feed2(ls(period=3, clock="12:10", goals=[G1, G2, G3], **sc(2, 1)))
    print("  TOR hat trick 3-1");    await feed2(ls(period=3, clock="07:10", goals=[G1, G2, G3, G3], **sc(3, 1)))
    print("  opp goal 3-2");         await feed2(ls(period=3, clock="02:00", goals=[G1, G2, G3, G3, O2], **sc(3, 2)))
    print("  opp SH goal 3-3, OT");  await feed2(ls(period=4, ptype="OT", clock="19:00", goals=[G1, G2, G3, G3, O2, O3], **sc(3, 3)))
    print("  TOR OT winner, final"); await feed2(ls(state="final", period=4, ptype="OT", clock="01:12", last_ptype="OT",
                                                    goals=[G1, G2, G3, G3, O2, O3, G4], **sc(4, 3)))
    leafs.state.data["mode"] = "goals"
    print("  same final seen again (silent)")
    await feed2(ls(state="final", period=4, ptype="OT", clock="01:12", last_ptype="OT",
                   goals=[G1, G2, G3, G3, O2, O3, G4], **sc(4, 3)))

    print("\n== goal clock comes from the goal record (real game 29008, Ottawa @ Kingston)")
    goals = m.parse_goals(m.api_get(m.summary_url("29008")))
    print("  " + " | ".join(x["when"] for x in goals))
    assert [x["when"] for x in goals] == ["1st 18:28", "2nd 09:14", "2nd 05:54", "3rd 02:27", "OT 02:50"], goals

    print("\n== three stars that appear after the final are posted later")
    m.LATE_STARS_WAIT = 0.05
    calls = {"n": 0}
    star_list = [{"first_name": "A", "last_name": "Misiak"}, {"first_name": "R", "last_name": "Smith"},
                 {"first_name": "A", "last_name": "Laurin"}]

    async def slow_summary(gid):
        calls["n"] += 1
        return {"GC": {"Gamesummary": {"mvps": [] if calls["n"] < 3 else star_list,
                                       "totalShots": {"visitor": 28, "home": 26}, "goals": []}}}
    o2 = m.OhlModule(m.Hub(Fake()))
    o2.summary = slow_summary
    o2.priority = [raw.home, raw.away]                # force priority so this test isn't at the mercy of who's actually playing today
    fin = dataclasses.replace(raw, code=4, ag=2, hg=3, period=4, clock="02:50", intermission=False)
    evs = await o2.game_events(fin, {"ag": 2, "hg": 3, "code": 2, "inter": False, "period": 4}, [fin])
    finals = [e for e in evs if e.kind == "final"]
    assert len(finals) == 1 and len(finals[0].texts) == 1, evs           # final goes out without stars
    await tl.until(lambda: not o2.hub.out.empty())
    queued = []
    while not o2.hub.out.empty():
        queued.append(o2.hub.out.get_nowait()[:3])
    print("  OHL follow-up:", [q[2] for q in queued])
    assert any("⭐ A. Misiak" in q[2] or "Misiak" in q[2] for q in queued), queued

    leafs.state.data["mode"] = "all"
    nh = {"n": 0}

    async def slow_landing(gid):
        nh["n"] += 1
        d = {"awayTeam": {"sog": 21}, "homeTeam": {"sog": 30}, "summary": {"threeStars": []}}
        if nh["n"] >= 3:
            d["summary"]["threeStars"] = [{"name": {"default": "A. Matthews"}}, {"name": {"default": "C. Caufield"}},
                                          {"name": {"default": "J. Woll"}}]
        return d
    leafs.landing = slow_landing
    t.sent.clear()
    fin2 = ls(state="final", period=3, ptype="REG", last_ptype="REG", **sc(2, 1))
    evs = await leafs.events(fin2, {"ar": fin2.ar, "hr": fin2.hr, "state": "live", "inter": False, "period": 3})
    assert [k for k, _ in evs] == ["final"] and len(evs[0][1]) == 1, evs
    await tl.until(lambda: any("A. Matthews" in x for _, x in t.sent))
    await settle(hub, 1.0)
    print("  NHL follow-up:", [x for _, x in t.sent])
    assert any("A. Matthews" in x for _, x in t.sent), t.sent
    m.LATE_STARS_WAIT = 45
    print("  ok")

    print("\n== OHL: three stars only chased for priority teams / the Memorial Cup, and never a partial list")
    m.LATE_STARS_WAIT = 0.05
    m.CFG["summary_fetch"] = True
    star3 = [{"first_name": "A", "last_name": "One"}, {"first_name": "B", "last_name": "Two"}, {"first_name": "C", "last_name": "Three"}]

    def fin_game(**kw):
        return dataclasses.replace(raw, code=4, ag=2, hg=3, period=3, clock="00:00", intermission=False, **kw)

    async def no_stars_yet(gid):
        return {"GC": {"Gamesummary": {"mvps": [], "totalShots": {"visitor": 1, "home": 1}, "goals": []}}}

    o3n = m.OhlModule(m.Hub(Fake())); o3n.priority = ["KIT", "GUE"]     # explicit, real-world default
    o3n.summary = no_stars_yet
    fg = fin_game(vs=dataclasses.replace(raw.vs, code="AAA"), hs=dataclasses.replace(raw.hs, code="BBB"))
    ev = await o3n.game_events(fg, {"ag": 2, "hg": 2, "code": 2, "inter": False, "period": 3}, [fg])
    assert len(ev[0].texts) == 1, ev                          # not a priority team, not the Memorial Cup: no stars chased
    await asyncio.sleep(0.3)
    assert o3n.hub.out.empty(), "no late-stars follow-up for a non-priority game"

    async def two_of_three(gid):
        return {"GC": {"Gamesummary": {"mvps": [star3[0], None, None], "totalShots": {"visitor": 1, "home": 1}, "goals": []}}}
    o3p = m.OhlModule(m.Hub(Fake())); o3p.priority = ["KIT", "GUE"]
    o3p.summary = two_of_three
    fg2 = fin_game(vs=dataclasses.replace(raw.vs, code="KIT"), hs=dataclasses.replace(raw.hs, code="ZZZ"))
    ev = await o3p.game_events(fg2, {"ag": 2, "hg": 2, "code": 2, "inter": False, "period": 3}, [fg2])
    assert len(ev[0].texts) == 1, ev                          # a priority team, but only 1 of 3 stars in: no partial list yet

    async def now_complete(gid):
        return {"GC": {"Gamesummary": {"mvps": star3, "totalShots": {"visitor": 1, "home": 1}, "goals": []}}}
    o3p.summary = now_complete                                # completes on the next poll
    await tl.until(lambda: not o3p.hub.out.empty())
    posted = []
    while not o3p.hub.out.empty():
        posted.append(o3p.hub.out.get_nowait()[:3])
    print("  priority team, stars complete on retry:", [p[2] for p in posted])
    assert len(posted) == 1 and posted[0][2] == "⭐ A. One, B. Two, C. Three", posted

    o3c = m.OhlModule(m.Hub(Fake())); o3c.priority = []        # not a priority team, but IS the Memorial Cup
    o3c.summary = no_stars_yet
    fg3 = dataclasses.replace(fg2, client="chl")
    assert fg3.cup
    ev = await o3c.game_events(fg3, {"ag": 2, "hg": 2, "code": 2, "inter": False, "period": 3}, [fg3])
    assert len(ev[0].texts) == 1, ev
    o3c.summary = now_complete
    await tl.until(lambda: not o3c.hub.out.empty())
    posted = []
    while not o3c.hub.out.empty():
        posted.append(o3c.hub.out.get_nowait()[:3])
    print("  Memorial Cup game (not a priority team), stars still chased:", [p[2] for p in posted])
    assert len(posted) == 1 and "A. One, B. Two, C. Three" in posted[0][2], posted
    m.LATE_STARS_WAIT = 45
    print("  ok")

    print("\n== !series / !bracket on the team bot (real 2025 postseason: the Jays reached the World Series)")
    real_now = m.now
    from datetime import datetime as _dt
    m.now = lambda: _dt(2025, 11, 5, 12, tzinfo=m.TZ)
    r = await jays.cmd_series(m.Incoming("!series", False), "")
    print("   ", r)
    assert r == ["[JAYS] WS LAD def TOR 4-3"], r
    r = await jays.cmd_bracket(m.Incoming("!bracket", False), "")
    print("   ", r)
    assert r[0].startswith("[JAYS] WS LAD def TOR 4-3 | ALCS TOR def SEA 4-3 | ALDS TOR def NYY 3-1"), r
    m.now = real_now

    print("\n== simulated Jays game")
    games = await jays.refresh(0)
    # prefer a game still to come; on the actual last day of the season there may be none left, so fall
    # back to the most recent one - it is only used as a template (every field the test cares about is overridden)
    base = next((g for g in games if g.scheduled and g.start.date() >= m.now().date()), None) or (games[-1] if games else None)
    if base is None:      # offseason: the schedule window is empty - use a synthetic template (every field is overridden)
        base = m.MGame(pk=1, start=m.now() + m.timedelta(hours=1), state="preview", detail="Scheduled", delayed=False,
                       away="TOR", home="NYY", away_id=141, home_id=147, ar=0, hr=0, inning=0, inn_txt="",
                       ar_rec="", hr_rec="", away_sp="", home_sp="", win="", loss="", save="")
    jays.state.data["mode"] = "all"

    def step(**kw):
        return dataclasses.replace(base, **kw)

    async def feed(g):
        t.sent.clear()
        await jays.process([g])
        await settle(hub, 1.0)
        for c, x in t.sent:
            print(f"      -> {c}: {x}  ({len(x.encode())}B)")
        if not t.sent:
            print("      (nothing)")
        assert all(len(x.encode()) <= m.CFG["max_msg_bytes"] for _, x in t.sent), "message too long"

    async def fake_play(g):
        return "Vladimir Guerrero Jr. homers (30) on a fly ball to left center field. George Springer scores."
    jays.scoring_play = fake_play
    home = base.is_home(141)
    print("  baseline (preview)");            await feed(base)
    print("  live, 0-0");                     await feed(step(state="live", inning=1, inn_txt="Top 1st"))
    print("  Jays score 2");                  await feed(step(state="live", inning=3, inn_txt="Bot 3rd",
                                                            **({"hr": 2} if home else {"ar": 2})))
    print("  opponent scores 1");             await feed(step(state="live", inning=5, inn_txt="Top 5th",
                                                            **({"hr": 2, "ar": 1} if home else {"ar": 2, "hr": 1})))
    print("  rain delay");                    await feed(step(state="live", inning=5, inn_txt="Top 5th", delayed=True,
                                                            detail="Delayed: Rain", **({"hr": 2, "ar": 1} if home else {"ar": 2, "hr": 1})))
    print("  extra innings");                 await feed(step(state="live", inning=10, inn_txt="Top 10th",
                                                            **({"hr": 2, "ar": 2} if home else {"ar": 2, "hr": 2})))
    print("  final");                         await feed(step(state="final", inning=10, win="Gausman", loss="Eovaldi", save="Swanson",
                                                            **({"hr": 3, "ar": 2} if home else {"ar": 3, "hr": 2})))
    jays.state.data["mode"] = "finals"
    print("  mode=finals, a run scores (silent)")
    await feed(step(state="live", inning=6, inn_txt="Bot 6th", **({"hr": 4, "ar": 2} if home else {"ar": 4, "hr": 2})))

    for w in workers:
        w.cancel()
    print("\nALL OK")

asyncio.run(main())
