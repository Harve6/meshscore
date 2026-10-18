"""Weather: Open-Meteo forecast text and Environment Canada alerts (real recorded responses)."""
import json
import os

import pytest

from meshscore import config, core
from meshscore.core import CFG
from meshscore.dailyplay import wmo
from meshscore.leagues import weatherbot as wb

from daily_rig import DRig, run, set_time

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
OM = json.load(open(os.path.join(FIX, "openmeteo.json"), encoding="utf-8"))
EC = json.load(open(os.path.join(FIX, "ec_alerts.json"), encoding="utf-8"))


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, command_cooldown=0, log_file="", timezone="America/Toronto")
    core.init_tz()
    yield
    monkeypatch.setattr(core, "CLOCK", None)
    config.reset()


def feed(alerts=EC, forecast=OM):
    calls = []

    def get_json(url, params):
        calls.append(url)
        return alerts if "alerts" in url else forecast
    get_json.calls = calls
    return get_json


def test_weather_codes_and_compass():
    assert wmo.describe(0) == "clear" and wmo.describe(63) == "rain" and wmo.describe(None) == "weather" and wmo.describe(1234) == "weather"
    assert [wmo.compass(d) for d in (0, 44, 90, 216, 350, None)] == ["N", "NE", "E", "SW", "N", ""]


def test_alert_parsing_uses_the_real_environment_canada_shape():
    a = wb.parse_alerts(EC)
    assert len(a) == 3 and all(x["kind"] == "warning" and x["name"] == "wind warning" and x["id"] for x in a)
    assert wb.parse_alerts(EC, levels=("watch",)) == []                       # only the levels asked for
    assert wb.parse_alerts(None) == [] and wb.parse_alerts({"features": [{"properties": None}, 3, {}]}) == []
    assert wb.first_sentence("Damaging winds are occurring.\n\nStrong westerly winds gusting.") == "Damaging winds are occurring."
    assert len(wb.first_sentence("x" * 300).encode()) <= 70


def test_weather_command_text_fits_and_reads_well():
    async def go():
        async with DRig("weather", lat=43.45, lon=-80.49, place="Kitchener", cooldown=0) as r:
            r.m.get_json = feed()
            await r.say("!weather", name="Ann")
            out = r.t.sent[-1]
            assert out.startswith("[WEATHER] Kitchener ") and "C " in out and "wind" in out and "today " in out and "tmrw " in out
            assert len(out.encode()) <= 130
            await r.say("!wx", name="Ann")
            assert r.t.sent[-1] == out                                          # (served from the 15-minute cache)
    run(go())


def test_no_location_set():
    async def go():
        async with DRig("weather") as r:
            await r.say("!weather", name="Ann")
            assert "No location set" in r.t.sent[-1]
    run(go())


def test_first_look_is_silent_then_only_new_warnings_are_posted(monkeypatch):
    set_time(monkeypatch, h=14, mi=0)

    async def go():
        async with DRig("weather", lat=50, lon=-98, place="Winnipeg") as r:
            r.m.get_json = feed()
            await r.tick()
            assert r.t.sent == [] and len(r.m.active) == 3                       # the existing warnings are the baseline
            await r.tick(700)
            assert r.t.sent == []                                                # and are not repeated
            newer = json.loads(json.dumps(EC))
            newer["features"].append(json.loads(json.dumps(EC["features"][0])))
            newer["features"][-1]["id"] = "brand-new-1"
            newer["features"][-1]["properties"]["alert_name_en"] = "tornado watch"
            newer["features"][-1]["properties"]["alert_type"] = "watch"
            newer["features"][-1]["properties"]["alert_text_en"] = "Conditions are favourable for severe thunderstorms. Be ready."
            r.m.get_json = feed(alerts=newer)
            await r.tick(700)
            assert len(r.t.sent) == 1
            post = r.t.sent[0]
            assert post.startswith("⚠ Winnipeg: Tornado watch until ") and "Conditions are favourable" in post and len(post.encode()) <= 130
            await r.tick(700)
            assert len(r.t.sent) == 1
            await r.say("!warnings", name="Ann")
            assert r.t.sent[-1].startswith("⚠ Winnipeg:")
        # a restart does not repeat what was announced
        async with DRig("weather", lat=50, lon=-98, place="Winnipeg") as r2:
            r2.m.get_json = feed(alerts=newer)
            await r2.tick()
            assert r2.t.sent == []
    run(go())


def test_feed_errors_never_raise_and_alerts_can_be_switched_off(monkeypatch):
    set_time(monkeypatch, h=14, mi=0)

    async def go():
        async with DRig("weather", lat=43, lon=-80) as r:
            def boom(url, params):
                raise OSError("offline")
            r.m.get_json = boom
            await r.tick()
            await r.tick(5000)
            assert r.t.sent == []
        async with DRig("weather", lat=43, lon=-80, alerts=False) as r2:
            g = feed()
            r2.m.get_json = g
            await r2.tick()
            assert not any("alerts" in u for u in g.calls)
            await r2.say("!warnings", name="Ann")
            assert "switched off" in r2.t.sent[-1]
    run(go())


def test_morning_forecast_is_optional_once_a_day_and_quiet_after_a_late_restart(monkeypatch):
    async def go():
        set_time(monkeypatch, d=14, h=7, mi=5)
        async with DRig("weather", lat=43, lon=-80, place="Kitchener", morning=True, alerts=False) as r:
            r.m.get_json = feed()
            await r.tick()
            await r.tick(300)
            posts = [x for x in r.t.sent if x.startswith("🌤 Kitchener")]
            assert len(posts) == 1 and len(posts[0].encode()) <= 130
        set_time(monkeypatch, d=15, h=13, mi=0)
        async with DRig("weather", lat=43, lon=-80, place="Kitchener", morning=True, alerts=False) as r2:
            r2.m.get_json = feed()
            await r2.tick()
            assert r2.t.sent == []
        set_time(monkeypatch, d=14, h=7, mi=5)
        async with DRig("weather", lat=43, lon=-80, morning=False, alerts=False) as r3:      # off by default
            r3.m.get_json = feed()
            await r3.tick()
            assert r3.t.sent == []
    run(go())


def test_web_state():
    async def go():
        async with DRig("weather", lat=43.45, lon=-80.49, place="Kitchener") as r:
            r.m.get_json = feed()
            await r.tick()
            st = r.m.site_state()
            json.dumps(st)
            assert st["place"] == "Kitchener" and st["now"]["text"] and len(st["days"]) == 3 and len(st["alerts"]) == 3
    run(go())
