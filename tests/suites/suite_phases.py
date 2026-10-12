"""Season phases (pre / reg / post) for every sport, channel sharing, and the Leafs playoffs overview.
Fake radio; real APIs for reads (OHL season list, Leafs schedule, the real 2025 NHL playoffs)."""
import asyncio, builtins, copy, dataclasses, os, re, sys, tempfile, time
from datetime import datetime
import suitelib as tl
m = tl.bootstrap()
TMP = os.getcwd()  # bootstrap() chdir'd into the fresh temp dir; this is it
m.SPORTS_FILE = os.path.join(TMP, "last_sports.json")
REAL_NOW, REAL_NHL = m.now, m.nhl_get
at = tl.at_factory(m)
Fake = tl.make_fake(m, {"ohl": 0, "jays": 1, "leafs": 2, "mlb": 3})
drain = tl.drain_texts
settle = tl.settle


def reset_file():
    if os.path.exists(m.SPORTS_FILE):
        os.remove(m.SPORTS_FILE)


async def main():
    t = Fake()
    hub = m.Hub(t)
    ohl, jays, leafs = m.OhlModule(hub), m.JaysModule(hub), m.NhlModule(hub, "leafs")
    hub.add(ohl); hub.add(jays); hub.add(leafs)
    workers = [asyncio.create_task(hub.cmd_worker())]

    async def say(text, chan="", dm=False, name="Tester"):
        t.sent.clear()
        tx = asyncio.create_task(hub.tx_worker())
        await hub.on_message(m.Incoming(text, dm, name, "abcdef123456" if dm else "", 2, 7.5, chan))
        await settle(hub, 1.5)
        tx.cancel()
        print(f"  > [{('DM' if dm else '#' + chan)}] {text}")
        for c, x in t.sent:
            print(f"      -> {c}: {x}  ({len(x.encode())}B)")
        assert all(len(x.encode()) <= m.CFG["max_msg_bytes"] for _, x in t.sent), "message too long"
        return [x for _, x in t.sent]

    print("== phases: defaults skip preseason, keep the regular season and the playoffs")
    reset_file()
    assert m.get_phases("leafs") == {"pre": False, "reg": True, "post": True}
    assert m.get_phases("ohl") == {"pre": False, "reg": True, "post": True}
    m.CFG["modules"]["jays"]["phases"] = {"pre": True}          # a per-sport default in config.json
    assert m.get_phases("jays") == {"pre": True, "reg": True, "post": True}
    del m.CFG["modules"]["jays"]["phases"]
    m.save_phases("leafs", {"pre": True, "reg": True, "post": False})
    assert m.get_phases("leafs") == {"pre": True, "reg": True, "post": False}
    assert m.get_phases("ohl") == {"pre": False, "reg": True, "post": True}      # other sports untouched
    reset_file()
    print("  ok")

    print("\n== !phase command (all three sports; remembered across restarts)")
    r = await say("!phase", "leafs");                assert r == ["[LEAFS BOT] Games covered: pre off | reg ON | post ON. Set: !phase pre|reg|post on|off"], r
    r = await say("!phase pre on", "leafs");         assert "Preseason games ON" in r[0] and "pre ON" in r[0], r
    r = await say("!phase playoffs off", "leafs");   assert "Playoffs games off" in r[0] and "post off" in r[0], r
    r = await say("!phase spring on", "jays");       assert "Preseason games ON" in r[0], r
    r = await say("!phase regular off", "ohl");      assert "Regular season games off" in r[0], r
    r = await say("!phase bogus on", "leafs");       assert "!phase pre|reg|post on|off" in r[0], r
    r = await say("!phase pre maybe", "leafs");      assert "!phase pre|reg|post on|off" in r[0], r
    assert leafs.phases == {"pre": True, "reg": True, "post": False}
    again = m.NhlModule(hub, "leafs")                # "restart": a new module reads what was saved
    assert again.phases == {"pre": True, "reg": True, "post": False} and m.OhlModule(hub).phases["reg"] is False
    m.CFG["admin_names"] = ["boss"]
    r = await say("!phase reg off", "leafs", name="someone");  assert "Only admins" in r[0], r
    r = await say("!phase reg on", "leafs", name="Boss");      assert "Regular season games ON" in r[0], r
    m.CFG["admin_names"] = []
    leafs.phases, jays.phases, ohl.phases = m.get_phases("leafs"), m.get_phases("jays"), m.get_phases("ohl")
    for mod in (leafs, jays, ohl):                   # back to the defaults for the rest of the tests
        mod.phases = dict(m.PHASE_DEFAULT)
    reset_file()

    print("\n== how each feed labels preseason / regular season / playoffs (real data)")
    m.load_ohl_seasons()
    print("   OHL seasons:", {k: m.OHL_PHASES.get(k) for k in (88, 87, 85, 84)})
    assert m.OHL_PHASES[88] == "reg" and m.OHL_PHASES[87] == "pre" and m.OHL_PHASES[85] == "post" and m.OHL_PHASES[84] == "other"
    for gt, want in (("S", "pre"), ("E", "pre"), ("R", "reg"), ("A", "reg"), ("F", "post"), ("D", "post"), ("L", "post"), ("W", "post")):
        g = m.parse_mgame({"gamePk": 1, "gameType": gt, "gameDate": "2026-03-01T18:00:00Z", "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
                           "teams": {"away": {"team": {"id": 141, "abbreviation": "TOR"}}, "home": {"team": {"id": 140, "abbreviation": "TEX"}}}})
        assert jays.game_phase(g) == want, (gt, jays.game_phase(g))
    sched = REAL_NHL("/club-schedule-season/tor/now")
    real = [m.parse_ngame(g) for g in sched["games"]]
    assert {leafs.game_phase(g) for g in real if g.gtype == 1} == {"pre"} and {leafs.game_phase(g) for g in real if g.gtype == 2} == {"reg"}
    print("   Leafs: preseason games =", sum(1 for g in real if g.gtype == 1), "| regular season =", sum(1 for g in real if g.gtype == 2),
          "| first regular season game:", next(g.date for g in real if g.gtype == 2))
    po25 = REAL_NHL("/club-schedule-season/tor/20242025")
    assert {leafs.game_phase(m.parse_ngame(g)) for g in po25["games"] if g["gameType"] == 3} == {"post"}
    print("  ok")

    print("\n== a switched-off phase is ignored: alerts, the morning post, !next, !last")
    # Leafs: a preseason game does nothing with pre off, alerts with pre on
    pre_g = next(g for g in real if g.gtype == 1)
    base = dataclasses.replace(pre_g, state="preview", ar=0, hr=0, id=1)
    live0 = dataclasses.replace(base, state="live", period=1, clock="20:00", ptype="REG")
    goal = dataclasses.replace(live0, ar=1, clock="15:00")
    for phases_pre, want_msgs in ((False, 0), (True, 1)):
        leafs.phases["pre"] = phases_pre
        leafs.gstate.clear(); leafs.state.data["mode"] = "all"
        for step in (base, live0, goal):
            await leafs.process([step])
        out = drain(leafs)
        print(f"   Leafs preseason goal, pre={'ON' if phases_pre else 'off'}:", out)
        assert (len(out) >= 1) == bool(want_msgs) and (not out or any("goal" in x.lower() for x in out)), out
    leafs.phases["pre"] = False
    reg_g = dataclasses.replace(base, gtype=2, id=2)
    leafs.gstate.clear()
    for step in (reg_g, dataclasses.replace(reg_g, state="live", period=1, clock="20:00"), dataclasses.replace(reg_g, state="live", period=1, clock="15:00", ar=1)):
        await leafs.process([step])
    assert any("goal" in x.lower() for x in drain(leafs)), "regular season games still alert"
    fut = m.now() + m.timedelta(days=2)
    pre_next = dataclasses.replace(base, id=3, state="preview", start=fut)
    reg_next = dataclasses.replace(base, id=4, state="preview", gtype=2, start=fut + m.timedelta(days=3))
    pre_last = dataclasses.replace(base, id=5, state="final", ar=1, hr=2, start=m.now() - m.timedelta(days=1))
    reg_last = dataclasses.replace(base, id=6, state="final", gtype=2, ar=3, hr=2, start=m.now() - m.timedelta(days=5))
    leafs.games, leafs.games_at = [reg_last, pre_last, pre_next, reg_next], time.time()
    r = await say("!next", "leafs");  assert r and "PRE" not in r[0] and r[0].count("@") + r[0].count("vs") == 1, r
    r = await say("!last", "leafs");  assert "3 @" in r[0], r
    leafs.phases["pre"] = True
    r = await say("!next", "leafs");  assert r[0].count("@") + r[0].count("vs") == 2, r      # both games listed with pre on
    leafs.phases["pre"] = False
    leafs.games_at = 0

    # Blue Jays: spring training
    def mg(pk, gt, **kw):
        g = m.parse_mgame({"gamePk": pk, "gameType": gt, "gameDate": "2026-03-01T18:00:00Z",
                           "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
                           "teams": {"away": {"team": {"id": 141, "abbreviation": "TOR"}}, "home": {"team": {"id": 140, "abbreviation": "TEX"}}}})
        return dataclasses.replace(g, **kw)
    for gt, phases_pre, want in (("S", False, False), ("S", True, True), ("R", False, True)):
        jays.phases["pre"] = phases_pre
        jays.gstate.clear(); jays.state.data["mode"] = "all"
        g0 = mg(70, gt)
        await jays.process([g0])
        await jays.process([dataclasses.replace(g0, state="live", inning=1, inn_txt="Top 1st")])
        await jays.process([dataclasses.replace(g0, state="live", inning=2, inn_txt="Top 2nd", ar=1)])
        out = drain(jays)
        print(f"   Jays game type {gt}, spring training {'ON' if phases_pre else 'off'}: {len(out)} message(s)")
        assert bool(out) == want, out
    jays.phases["pre"] = False
    st = mg(71, "S", start=m.now() + m.timedelta(days=1)); rg = mg(72, "R", start=m.now() + m.timedelta(days=4))
    jays.games, jays.games_at = [st, rg], time.time()
    r = await say("!next", "jays");   assert r and r[0].count("@") + r[0].count("vs") == 1, r          # spring training skipped
    jays.games_at = 0

    # OHL: preseason season id
    live_games = m.extract_games(m.api_get(m.scorebar_url(0, 1))) or []
    assert live_games, "no OHL games in the scorebar"
    og = dataclasses.replace(live_games[0], hg=0, ag=0, code=1, period=1, clock="20:00", intermission=False)
    ohl.state.data["mode"] = "full"
    for season, phases_pre, want in ((87, False, False), (87, True, True), (88, False, True)):
        ohl.phases["pre"] = phases_pre
        ohl.gstate.clear()
        await ohl.process_games([dataclasses.replace(og, season=season)])
        await ohl.process_games([dataclasses.replace(og, season=season, code=2, clock="19:00")])
        m.CFG["summary_fetch"] = False
        await ohl.process_games([dataclasses.replace(og, season=season, code=2, clock="18:00", hg=1)])
        m.CFG["summary_fetch"] = True
        out = drain(ohl)
        print(f"   OHL season {season} ({m.OHL_PHASES[season]}), preseason {'ON' if phases_pre else 'off'}: {len(out)} message(s)")
        assert bool(out) == want, out
    ohl.phases["pre"] = False

    print("\n== Memorial Cup: every game in #ohl, WHL and QMJHL teams included (real 2025 tournament)")
    REAL_API = m.api_get
    wide = REAL_API(m.scorebar_url(520, 0, "chl"))["SiteKit"]["Scorebar"]
    MC = [g for g in wide if str(g.get("SeasonID")) == "80"]
    print("   real tournament games:", [f"{g['VisitorCode']}@{g['HomeCode']}" for g in MC])
    assert len(MC) == 8

    def fake_api(url):
        if "client_code=chl" in url and "view=scorebar" in url:
            back, ahead = int(re.search(r"numberofdaysback=(\d+)", url).group(1)), int(re.search(r"numberofdaysahead=(\d+)", url).group(1))
            lo = (m.now().date() - m.timedelta(days=back)).isoformat()
            hi = (m.now().date() + m.timedelta(days=ahead)).isoformat()
            return {"SiteKit": {"Scorebar": [g for g in MC if lo <= g["Date"] <= hi]}}
        return REAL_API(url)
    m.api_get = fake_api
    m.load_ohl_seasons()
    assert m.CUP_SEASONS[80] == ("2025-05-20", "2025-06-02") and m.CUP_SEASONS[82] == ("2026-05-22", "2026-06-01"), m.CUP_SEASONS
    ohl.phases = m.get_phases("ohl", m.OhlModule.phase_keys)
    assert ohl.phases == {"pre": False, "reg": True, "post": True, "cup": True}, ohl.phases

    at(2025, 9, 26)
    assert not m.cup_active() and await ohl.cup_games(0) == []            # nothing is fetched outside a Memorial Cup
    r = await say("!cup", "ohl");   assert r == ["[MC] The Memorial Cup isn't on right now. Next: May 22 - Jun 1."], r
    at(2025, 5, 25, 12)
    assert m.cup_active()
    cg = await ohl.cup_games(0)
    print("   on 2025-05-25 (yesterday/today/tomorrow):", [(g.id, f"{g.away}@{g.home}") for g in cg])
    assert [g.id for g in cg] == ["chl:566", "chl:567", "chl:568"] and all(g.cup for g in cg)
    assert m.game_line(cg[0]).startswith("MC MON 2 @ LDN 3 F")
    r = await say("!scores", "ohl");  assert any("MC " in x for x in r), r
    r = await say("!phase", "ohl");   assert "cup ON" in r[0] and "!phase pre|reg|post|cup on|off" in r[0], r
    r = await say("!phase cup off", "ohl");  assert "Memorial Cup games off" in r[0] and "cup off" in r[0], r
    assert await ohl.cup_games(0) == []                                    # switched off: not even fetched
    r = await say("!phase memorial on", "ohl");  assert "Memorial Cup games ON" in r[0], r
    ohl.phases["cup"] = True

    print("   -- the real championship game (LDN @ MH, June 1): every goal, ends of periods, final with stars")
    raw = next(g for g in MC if g["ID"] == "573")
    base = m.parse_game(raw, "chl")
    assert base.id == "chl:573" and base.final and (base.away, base.home, base.ag, base.hg) == ("LDN", "MH", 4, 1)
    summary = REAL_API(m.summary_url("chl:573"))
    goals = summary["GC"]["Gamesummary"]["goals"]
    state = {"k": 0}

    async def cup_summary(gid):
        d = copy.deepcopy(summary)
        d["GC"]["Gamesummary"]["goals"] = goals[: state["k"]] if state["k"] < len(goals) else goals
        return d
    ohl.summary = cup_summary
    m.CFG["summary_fetch"] = True
    ohl.state.data["mode"] = "all"; ohl.gstate.clear()
    cur = dataclasses.replace(base, code=1, ag=0, hg=0, period=1, clock="20:00", period_name="1st")
    await ohl.process_games([cur]); drain(ohl)
    cur = dataclasses.replace(cur, code=2, clock="19:00")
    await ohl.process_games([cur])
    out = drain(ohl)
    assert out and out[0] == "🏒 Puck drop: LDN @ MH | Memorial Cup", out
    ag = hg = 0
    said = []
    for i, gl in enumerate(goals, 1):
        state["k"] = i
        if gl["home"] == "1":
            hg += 1
        else:
            ag += 1
        per = int(gl["period_id"])
        cur = dataclasses.replace(cur, ag=ag, hg=hg, period=per, period_name={1: "1st", 2: "2nd", 3: "3rd"}.get(per, "OT"), clock="10:00")
        await ohl.process_games([cur])
        texts = drain(ohl)
        who = m.player_name(gl["goal_scorer"])
        goal_msgs = [x for x in texts if x.startswith("🚨")]
        print(f"     goal {i}: {goal_msgs}")
        assert len(goal_msgs) == 1 and who in goal_msgs[0], (who, texts)
        said += texts
    state["k"] = len(goals)
    cur = dataclasses.replace(base, period=3, period_name="3rd", clock="00:00", code=4)
    await ohl.process_games([cur])
    fin = drain(ohl)
    print("     final:", fin)
    assert fin[0].startswith("🛑 FINAL: LDN 4 @ MH 1") and "Memorial Cup" in fin[0] and "SOG 24-32" in fin[0], fin
    # the real record for this game has only ever had 1 of its 3 stars entered (still true over a year later) -
    # exactly the kind of partial list that must never be shown; correctly, nothing star-related goes out
    assert not any(x.startswith("⭐") for x in fin), fin
    assert any(x.startswith("⏸ End of") for x in said), "period ends are announced too"

    print("   -- a busy OHL night thins out OHL alerts but never the Memorial Cup")
    m.CFG["busy_live_games"] = 3
    ohl.priority = []                                   # (the copied games may involve KIT / GUE, which would exempt them)
    ohl.busy, ohl.gstate = False, {}
    ogs = [dataclasses.replace(live_games[0], id=str(9000 + i), code=2, hg=0, ag=0, period=1, clock="10:00", intermission=False, season=88) for i in range(3)]
    cup_live = dataclasses.replace(base, code=2, ag=0, hg=0, period=1, period_name="1st", clock="10:00")
    await ohl.process_games(ogs + [cup_live]); drain(ohl)
    assert ohl.busy, "3 OHL games live = busy night"
    state["k"] = 1
    ogs[0] = dataclasses.replace(ogs[0], ag=1, clock="09:00")
    cup_goal = dataclasses.replace(cup_live, ag=1, clock="09:00")
    await ohl.process_games(ogs + [cup_goal])
    out = drain(ohl)
    print("     ", out)
    assert len(out) == 1 and out[0].startswith("🚨 LDN GOAL") and "|" in out[0], out          # the OHL goal stayed quiet, the Memorial Cup goal did not
    ohl.busy = False

    print("   -- !cup: standings and results (real numbers)")
    at(2025, 6, 1, 18)
    ohl.cup_all_at = 0
    r = await say("!cup", "ohl")
    assert r[0] == "[MC] LDN 4-1 | MH 3-1 | MON 1-3 | RIM 0-3", r
    assert "MON 2-5 LDN" in r[1] and "LDN 4-1 MH" in r[1], r

    print("   -- morning post lists yesterday's and today's Memorial Cup games")
    at(2025, 5, 26, 12, 5)
    ohl.state.data["last_digest"] = ""
    await ohl.check_digest()
    out = drain(ohl)
    print("     ", out)
    assert any(x.startswith("[OHL RECAP]") and "MC RIM 1-3 LDN" in x for x in out) and any(x.startswith("[OHL TODAY]") and "MC MON@MH" in x for x in out), out
    print("   -- a restart AFTER the digest hour stays quiet (no stale 'today' list, no catch-up recap)")
    at(2025, 5, 26, 14, 40)                      # digest hour is 12: this is 2h40m late
    ohl.state.data["last_digest"] = ""
    await ohl.check_digest()
    assert not drain(ohl) and ohl.state.data["last_digest"] == "2025-05-26", "late start must skip today's digest"
    at(2025, 5, 27, 12, 30)                      # next day, inside its own hour: normal
    await ohl.check_digest()
    assert any(x.startswith("[OHL") for x in drain(ohl))
    m.api_get, m.now = REAL_API, REAL_NOW
    del ohl.summary
    print("  ok")

    print("\n== two modules can share one channel (#mlb postseason bot inside #jays)")
    m.CFG["modules"]["postseason"]["channel"] = "#jays"
    hub2 = m.Hub(Fake())
    j2, p2 = m.JaysModule(hub2), m.PostseasonModule(hub2, "postseason")
    hub2.add(j2); hub2.add(p2)
    assert p2.channel == "#jays"
    r1 = hub2.route(m.Incoming("!score", False, "x", "", None, None, "jays"))
    r2 = hub2.route(m.Incoming("!series", False, "x", "", None, None, "jays"))
    r3 = hub2.route(m.Incoming("!ps series", False, "x", "", None, None, "jays"))
    r4 = hub2.route(m.Incoming("!scores", False, "x", "", None, None, "jays"))
    print("   !score ->", r1[0].name, "| !series ->", r2[0].name, "| !ps series ->", r3[0].name, "| !scores ->", r4[0].name)
    assert r1[0] is j2 and r2[0] is p2 and r3[0] is p2 and r4[0] is j2       # Jays commands stay the Jays'; postseason-only ones work plainly
    tt = Fake(); hub3 = m.Hub(tt); j3, p3 = m.JaysModule(hub3), m.PostseasonModule(hub3, "postseason")
    hub3.add(j3); hub3.add(p3)
    m.CFG["announce_on_start"] = True
    task = asyncio.create_task(hub3.transport_supervisor())
    await asyncio.sleep(0.4); task.cancel()
    announces = [x for _, _, x, *_ in list(hub3.out._queue)]
    print("   start-up announcements on the shared channel:", announces)
    assert len(announces) == 1, announces
    m.CFG["modules"]["postseason"]["channel"] = "#mlb"

    print("\n== Leafs alert levels: max = every goal, min = period results + final, off = nothing")

    async def fake_landing(gid):
        return {"awayTeam": {"sog": 11}, "homeTeam": {"sog": 14},
                "summary": {"threeStars": [{"name": {"default": "A. Matthews"}}, {"name": {"default": "T. Tkachuk"}},
                                           {"name": {"default": "W. Nylander"}}]}}
    leafs.landing = fake_landing

    def gl(team, last, pid):
        return {"teamAbbrev": team, "firstName": {"default": "X"}, "lastName": {"default": last}, "playerId": pid, "strength": "ev", "goalModifier": "none"}
    g1x, g2x, g3x = gl("OTT", "Tkachuk", 1), gl("TOR", "Matthews", 2), gl("TOR", "Nylander", 3)
    reg = dataclasses.replace(next(g for g in real if g.gtype == 2), id=8800, away="OTT", home="TOR", start=m.now(), ar=0, hr=0, goals=[],
                              state="preview", period=1, ptype="REG", clock="20:00", inter=False, last_ptype="", series="", ss=None)

    def st(**kw):
        return dataclasses.replace(reg, state="live", **kw)
    game = [("baseline", reg),
            ("puck drop", st()),
            ("OTT goal", st(ar=1, clock="15:00", goals=[g1x])),
            ("end of the 1st", st(ar=1, clock="00:00", inter=True, goals=[g1x])),
            ("TOR goal", st(ar=1, hr=1, period=2, clock="10:00", goals=[g1x, g2x])),
            ("end of the 2nd", st(ar=1, hr=1, period=2, clock="00:00", inter=True, goals=[g1x, g2x])),
            ("tied after 3 = overtime", st(ar=1, hr=1, period=4, ptype="OT", clock="20:00", goals=[g1x, g2x])),
            ("TOR OT winner, final", dataclasses.replace(reg, state="final", ar=1, hr=2, period=4, ptype="OT", clock="18:00", last_ptype="OT",
                                                        goals=[g1x, g2x, g3x]))]

    async def play_game(level):
        leafs.gstate.clear(); leafs.state.data["mode"] = level
        drain(leafs)
        seen = []
        for label, g in game:
            await leafs.process([g])
            out = drain(leafs)
            print(f"   {level}: {label}: {out if out else '(nothing)'}")
            seen.append(out)
        return seen
    mx = await play_game("max")
    assert len(mx[1]) == 1 and "Puck drop" in mx[1][0] and "goal" in mx[2][0].lower() and "End of 1st" in mx[3][0] and "SOG 11-14" in mx[3][0], mx
    assert any("FINAL/OT" in x for x in mx[-1]) and any(x.startswith("⭐") for x in mx[-1])
    mn = await play_game("min")
    assert not mn[1] and not mn[2] and not mn[4], mn                                                    # no puck drop, no goals
    assert mn[3] == ["⏸ End of 1st: OTT 1-0 TOR | SOG 11-14"] and mn[5] == ["⏸ End of 2nd: OTT 1-1 TOR | SOG 11-14"], mn
    assert mn[6] == ["⏱ OT: OTT 1-1 TOR"], mn[6]
    assert any("FINAL/OT: OTT 1 @ TOR 2" in x for x in mn[7]) and any(x.startswith("⭐") for x in mn[7]) and not any("goal" in x.lower() for x in mn[7]), mn[7]
    assert not any(await play_game("off"))
    leafs.state.data["mode"] = "max"
    print("  ok")

    print("\n== Leafs: scorer name when the scoreboard's goal list lags its score (real bug: 2 of 13 goals went out unnamed)")
    real_landing, real_refresh, real_lag = leafs.landing, leafs.refresh, m.GOAL_LAG_WAIT
    m.GOAL_LAG_WAIT = 0.02
    leafs.state.data["mode"] = "max"
    calls = {"landing": 0}
    lagging = dataclasses.replace(reg, id=8801, state="live", ar=1, hr=1, period=2, clock="19:27", goals=[g1x])   # score 1-1, only 1 goal listed

    async def no_refresh(max_age=25):
        return leafs.games
    leafs.refresh = no_refresh
    leafs.games = [lagging]

    def landing_with(goal_lists):
        async def f(gid):
            calls["landing"] += 1
            k = min(calls["landing"], len(goal_lists)) - 1
            return {"awayTeam": {"sog": 5}, "homeTeam": {"sog": 5}, "summary": {"scoring": [{"goals": goal_lists[k]}], "threeStars": []}}
        return f
    prev = {"ar": 1, "hr": 0, "state": "live", "inter": False, "period": 2}
    # A: the game page already has both goals - the scorer is found on the first look
    leafs.landing = landing_with([[g1x, g2x]]); calls["landing"] = 0
    evs = await leafs.events(lagging, prev)
    print("   A:", [t for _, ts in evs for t in ts])
    assert any("TOR GOAL X. Matthews" in t for _, ts in evs for t in ts) and calls["landing"] == 1, evs
    # B: the game page lags too on the first look but has it on the second
    leafs.landing = landing_with([[g1x], [g1x, g2x]]); calls["landing"] = 0
    evs = await leafs.events(lagging, prev)
    print("   B:", [t for _, ts in evs for t in ts])
    assert any("TOR GOAL X. Matthews" in t for _, ts in evs for t in ts) and calls["landing"] == 2, evs
    # C: neither ever catches up - the goal is still announced, just without a name
    leafs.landing = landing_with([[g1x]]); calls["landing"] = 0
    evs = await leafs.events(lagging, prev)
    print("   C:", [t for _, ts in evs for t in ts])
    assert [t for _, ts in evs for t in ts] == ["🚨 TOR GOAL | OTT 1-1 TOR | 2nd 19:27"] and calls["landing"] == 2, evs
    leafs.landing, leafs.refresh, m.GOAL_LAG_WAIT, leafs.games = real_landing, real_refresh, real_lag, []

    print("\n== a game first seen already underway announces the start before the score (real log: goal came before puck drop)")
    ev1, ev2 = m.Event("goal", ("A", "B"), ["g"]), m.Event("start", ("A", "B"), ["s"])
    assert [e.kind for e in m.start_first([ev1, ev2])] == ["start", "goal"]                 # OHL-style objects
    assert [e[0] for e in m.start_first([("run", ["r"]), ("start", ["s"]), ("final", ["f"])])] == ["start", "run", "final"]
    assert [e[0] for e in m.start_first([("goal", 1), ("final", 2)])] == ["goal", "final"]    # nothing to move: order kept
    leafs.gstate.clear(); leafs.state.data["mode"] = "max"; drain(leafs)
    await leafs.process([reg])                                                              # baseline: not started yet
    await leafs.process([st(ar=1, clock="17:46", goals=[g1x])])                             # live AND a goal already scored
    out = drain(leafs)
    print("   ", out)
    assert len(out) == 2 and out[0].startswith("🏒 Puck drop") and out[1].startswith("🚨"), out
    leafs.gstate.clear()
    print("  ok")

    print("\n== Leafs playoffs overview when the Leafs are NOT in the playoffs (real 2025 playoffs)")
    def fake_nhl(path):
        if path.startswith("/club-schedule-season/tor/now"):
            return REAL_NHL("/club-schedule-season/tor/20242025")     # last season's schedule stands in for "now"
        if path.startswith("/score/") and OV["days"].get(path[7:]) is not None:
            return OV["days"][path[7:]]
        return REAL_NHL(path)
    OV = {"days": {}}
    m.nhl_get = fake_nhl
    m.CFG["modules"]["leafs"]["season_id"] = 20242025
    at(2025, 5, 26)
    lf = m.NhlModule(hub, "leafs")
    lf.state.data["mode"] = "all"
    await lf.refresh(0)
    print("   Leafs 2025 playoff games on their schedule:", sum(1 for g in lf.season if g.gtype == 3))
    full_season = list(lf.season)
    assert not lf.team_alive() and lf.overview_on(), "eliminated in round 2 (lost game 7 to Florida)"
    lf.season = [g for g in full_season if g.date <= "2025-05-01"]
    assert lf.team_alive() and not lf.overview_on(), "up 4-2 in round 1 and still going: no overview"
    lf.season = [g for g in full_season if g.date <= "2025-05-14"]           # won round 1, round 2 under way
    assert lf.team_alive()
    lf.season = [g for g in full_season if g.gtype != 3]
    assert not lf.team_alive()                                               # never made it
    lf.season = full_season
    lf.phases["post"] = False
    assert not lf.overview_on(); lf.phases["post"] = True                    # playoffs switched off = no overview either
    at(2025, 9, 26); assert not lf.overview_on(); at(2025, 5, 26)            # and only in April-June

    # a first-round day with several games (Toronto not playing), with the Leafs treated as "never made it"
    lf.season = [g for g in full_season if g.gtype != 3]
    assert not lf.team_alive() and lf.overview_on()

    def day(d):
        return REAL_NHL(f"/score/{d}")

    def others(sc):
        return [g for g in sc["games"] if g["gameType"] == 3 and "TOR" not in (g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"])]
    multi = None
    for i in range(40):
        d = (datetime(2025, 4, 20) + m.timedelta(days=i)).strftime("%Y-%m-%d")
        sc = day(d)
        if len(others(sc)) >= 3:
            multi = (d, sc); break
    assert multi, "no day with 3+ playoff games found"
    d, sc = multi
    print("   games on", d, [f"{g['awayTeam']['abbrev']}@{g['homeTeam']['abbrev']}" for g in others(sc)])
    y, mo, dd = map(int, d.split("-"))
    at(y, mo, dd, 12)

    def view(sc, states):
        c = copy.deepcopy(sc)
        for g, st in zip(others(c), states):
            if st != "keep":
                g["gameState"] = st
                if st in ("FUT", "LIVE"):
                    g["awayTeam"].pop("score", None); g["homeTeam"].pop("score", None)
        return c
    n_po = len(others(sc))
    OV["days"][d] = view(sc, ["FUT"] * n_po)
    lf.ov_state.clear(); lf.ov_done.clear(); lf.ov_pending.clear()
    await lf.process_overview()                                  # baseline: nothing yet
    assert not drain(lf)
    OV["days"][d] = view(sc, ["keep"] + ["LIVE"] * (n_po - 1))   # the first game ends, the others are still on
    await lf.process_overview()
    assert not drain(lf) and len(lf.ov_pending) == 1, "results wait for the other games (one message, not several)"
    OV["days"][d] = view(sc, ["keep"] * n_po)                    # everything is final
    await lf.process_overview()
    out = drain(lf)
    print("   batched results:", out)
    real_games = [m.parse_ngame(g) for g in others(sc)]
    assert len(out) == 1 and out[0].startswith("[NHL R1]") and all(m.NhlModule.overview_item(g) in out[0] for g in real_games), out
    await lf.process_overview()
    assert not drain(lf), "no repeats"
    print("   morning post:")
    lf.state.data["last_po_digest"] = ""
    at(y, mo, dd, 10, 5)
    OV["days"][d] = view(sc, ["FUT"] * n_po)
    await lf.check_overview_digest()
    out = drain(lf)
    print("   ", out)
    assert len(out) >= 1 and "TODAY]" in out[0] and "@" in out[0], out
    await lf.check_overview_digest()
    assert not drain(lf), "once a day"

    print("   the Stanley Cup Final: announced straight away, with the champions")
    at(2025, 6, 17, 21, 0)
    sc = day("2025-06-17")
    cup_raw = next(g for g in sc["games"] if g["gameType"] == 3)
    assert (cup_raw.get("seriesStatus") or {}).get("round") == 4, cup_raw.get("seriesStatus")
    OV["days"]["2025-06-17"] = view(sc, ["LIVE"])
    lf.ov_state.clear(); lf.ov_done.clear(); lf.ov_pending.clear()
    await lf.process_overview()
    OV["days"]["2025-06-17"] = view(sc, ["keep"])
    await lf.process_overview()
    out = drain(lf)
    print("   ", out)
    assert out[0].startswith("[NHL SCF] ") and "FLA win 4-2" in out[0] and out[1] == "🏆 FLA WIN THE STANLEY CUP (4-2)!", out
    print("   !series (real 2025 carousel):")
    r = await lf.cmd_series(m.Incoming("!series", False), "")
    print("   ", r)
    assert r[0].startswith("[NHL SCF] ") and "FLA def EDM 4-2" in r[0], r
    print("   !series 1 / !bracket 2 (earlier rounds; the bot's own team - TOR - listed first):")
    r = await lf.cmd_series(m.Incoming("!series", False), "1")
    print("   ", r)
    assert r[0].startswith("[NHL R1] TOR def OTT 4-2"), r
    r = await lf.cmd_series(m.Incoming("!bracket", False), "2")
    print("   ", r)
    assert r[0].startswith("[NHL R2] FLA def TOR 4-3") or r[0].startswith("[NHL R2] TOR"), r
    r = await lf.cmd_series(m.Incoming("!series", False), "9")
    assert r == ["[LEAFS] !series [1|2|3|4|cf|scf]"], r
    lf.state.data["mode"] = "off"
    lf.ov_pending.append((time.time(), m.parse_ngame(cup_raw)))
    lf.flush_overview([])
    assert not drain(lf), "!mode off keeps the overview quiet"
    m.nhl_get = REAL_NHL
    del m.CFG["modules"]["leafs"]["season_id"]
    m.now = REAL_NOW

    for w in workers:
        w.cancel()

    print("\n== season parts are remembered between runs (!phase writes them; a restarted bot reads them back)")
    reset_file()
    m.save_phases("leafs", {"pre": True, "reg": True, "post": False})
    m.save_phases("jays", {"pre": True, "reg": True, "post": True})
    m.save_phases("ohl", {"pre": False, "reg": False, "post": True, "cup": False})
    got = {n: m.get_phases(n) for n in ("ohl", "jays", "leafs")}
    assert got["leafs"] == {"pre": True, "reg": True, "post": False}
    assert got["jays"] == {"pre": True, "reg": True, "post": True}
    assert m.NhlModule(hub, "leafs").phases == got["leafs"]                 # the bot picks them up on start
    assert m.OhlModule(hub).phases == {"pre": False, "reg": False, "post": True, "cup": False}, m.OhlModule(hub).phases
    assert "postseason" not in (m._sports_file().get("phases") or {})
    print("   ok")
    print("\nALL OK")


asyncio.run(main())
