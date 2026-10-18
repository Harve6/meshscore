"""Weather: `!weather` for a place, plus Environment Canada warnings and watches as they are issued.

Needs `lat` and `lon` (and a `place` name). The forecast comes from Open-Meteo (free, no key); alerts from Environment
Canada's public weather-alerts API (Canada only; set `alerts = false` elsewhere). New warnings/watches are posted once
(a restart never repeats them); a daily morning forecast is optional (`morning = true`, `morning_hour`). See
docs/daily-games.md.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from ..core import fit, session
from ..dailyplay import wmo
from ..dailyplay.base import DailyModule

log = logging.getLogger("meshscore")

FORECAST = "https://api.open-meteo.com/v1/forecast"
ALERTS = "https://api.weather.gc.ca/collections/weather-alerts/items"
BOX = 0.04                     # degrees around the place (about 4 km) for the alerts query


def get_json(url, params):
    r = session.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def parse_alerts(data: dict, levels=("warning", "watch")) -> list[dict]:
    """Environment Canada weather-alerts GeoJSON -> [{"id","kind","name","short","text","expires"}]. Never raises."""
    out = []
    for f in (data or {}).get("features", []) or []:
        try:
            p = f["properties"]
            kind = str(p.get("alert_type", "")).lower()
            if kind not in levels:
                continue
            out.append({"id": str(f.get("id") or f"{p.get('alert_code')}-{p.get('publication_datetime')}"), "kind": kind,
                        "name": str(p.get("alert_name_en") or p.get("alert_short_name_en") or kind),
                        "text": str(p.get("alert_text_en") or "").strip(), "expires": str(p.get("expiration_datetime") or "")})
        except (KeyError, TypeError, AttributeError):
            continue
    return out


def first_sentence(text: str, limit: int = 70) -> str:
    t = " ".join(text.split())
    cut = t.find(". ")
    t = t[: cut + 1] if 0 < cut < limit + 20 else t
    return t if len(t) <= limit else t[: limit - 3].rstrip() + "..."


class WeatherModule(DailyModule):
    label = "Weather"
    json_name = "weather"
    board_title = "Mesh Weather"
    kinds = {"max": {"warning", "morning"}, "min": {"warning"}, "off": set()}
    min_blurb = "warnings only"
    bare_command = "!weather"

    def __init__(self, hub, name):
        super().__init__(hub, name)
        c = self.cfg
        self.blurb = "weather"
        self.place = str(c.get("place", "") or "here")
        self.lat, self.lon = c.get("lat"), c.get("lon")
        self.alerts_on = bool(c.get("alerts", True))
        self.levels = tuple(str(x).lower() for x in (c.get("alert_levels") or ["warning", "watch"]))
        self.morning = bool(c.get("morning", False))
        self.morning_hour = int(c.get("morning_hour", 7))
        self.get_json = get_json                                   # tests replace this
        self.wx, self.wx_at = None, 0.0
        self.active: list[dict] = []
        self.alerts_at = 0.0
        d = self.state.data
        d.setdefault("seen", [])           # alert ids already announced
        d.setdefault("baselined", False)   # the first look at the alerts is silent
        d.setdefault("morning_date", "")
        d.setdefault("last_alert", None)

    @property
    def located(self) -> bool:
        return self.lat is not None and self.lon is not None

    # ---------------------------------------------------------------- data
    async def refresh_weather(self, now):
        if not self.located or now - self.wx_at < 900:
            return
        self.wx_at = now
        try:
            self.wx = await asyncio.to_thread(self.get_json, FORECAST, {
                "latitude": self.lat, "longitude": self.lon, "timezone": "auto", "forecast_days": 3,
                "current": "temperature_2m,wind_speed_10m,wind_direction_10m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"})
        except Exception as e:
            log.warning("Weather fetch failed: %s", e)

    async def refresh_alerts(self, now):
        if not (self.located and self.alerts_on) or now - self.alerts_at < 600:
            return
        self.alerts_at = now
        try:
            data = await asyncio.to_thread(self.get_json, ALERTS, {
                "f": "json", "lang": "en", "limit": 20,
                "bbox": f"{float(self.lon) - BOX},{float(self.lat) - BOX},{float(self.lon) + BOX},{float(self.lat) + BOX}"})
        except Exception as e:
            log.warning("Weather alerts fetch failed: %s", e)
            return
        self.active = parse_alerts(data, self.levels)
        d = self.state.data
        new = [a for a in self.active if a["id"] not in d["seen"]]
        d["seen"] = ([a["id"] for a in self.active] + d["seen"])[:60]
        if d["baselined"]:
            for a in new:
                text = self.alert_text(a)
                d["last_alert"] = {"text": text, "at": int(now)}
                self.alert("warning", text)
        d["baselined"] = True
        self.state.save()

    # ---------------------------------------------------------------- texts
    def alert_text(self, a: dict) -> str:
        until = ""
        try:
            e = datetime.fromisoformat(a["expires"].replace("Z", "+00:00")).timestamp()
            from .pickembot import clock_text
            until = f" until {clock_text(e)}"
        except (ValueError, KeyError, AttributeError):
            pass
        return fit(f"⚠ {self.place}: {a['name'].capitalize()}{until}. {first_sentence(a['text'])}")

    def now_text(self) -> str:
        wx = self.wx
        if not wx:
            return f"[{self.tag}] No weather data yet"
        c, dly = wx.get("current", {}), wx.get("daily", {})

        def day(i, name):
            try:
                hi, lo = round(dly["temperature_2m_max"][i]), round(dly["temperature_2m_min"][i])
                pp = dly["precipitation_probability_max"][i]
                return f"{name} {hi}/{lo}" + (f" {pp}%" if pp not in (None, 0) else "")
            except (KeyError, IndexError, TypeError):
                return ""
        wind = f", wind {round(c['wind_speed_10m'])} {wmo.compass(c.get('wind_direction_10m'))}".rstrip() if "wind_speed_10m" in c else ""
        warn = f" | ⚠ {len(self.active)} alert{'s' if len(self.active) != 1 else ''}" if self.active else ""
        t = c.get("temperature_2m")
        head = f"[{self.tag}] {self.place} {round(t)}C {wmo.describe(c.get('weather_code'))}{wind}" if t is not None else f"[{self.tag}] {self.place}"
        return fit(" | ".join(x for x in (head, day(0, "today"), day(1, "tmrw")) if x) + warn)

    def morning_text(self) -> str:
        return self.now_text().replace(f"[{self.tag}] ", "🌤 ", 1)

    # ---------------------------------------------------------------- the loop
    async def poll(self):
        now = self.clock()
        await self.refresh_weather(now)
        await self.refresh_alerts(now)
        d = self.state.data
        today = self.today()
        if self.morning and self.wx and d["morning_date"] != today and self.past_hour(self.morning_hour):
            d["morning_date"] = today
            self.state.save()
            if self.in_hour(self.morning_hour):                   # a restart later in the day stays quiet
                self.alert("morning", self.morning_text())
        await self.publish()
        await self._refresh_cards(now)
        self.mark_ok()
        return 120

    # ---------------------------------------------------------------- commands
    def extra_commands(self):
        return {"!weather": self.cmd_weather, "!wx": self.cmd_weather, "!warnings": self.cmd_warnings}

    def help_text(self):
        return "!weather | !warnings"

    async def cmd_weather(self, inc, arg):
        if not self.located:
            return [f"[{self.tag}] No location set: add lat and lon to [bots.{self.name}]"]
        if self.wx is None:
            await self.refresh_weather(self.clock() + 10 ** 6)
        return [self.now_text()]

    async def cmd_warnings(self, inc, arg):
        if not self.alerts_on:
            return [f"[{self.tag}] Warnings are switched off"]
        if not self.active:
            return [fit(f"[{self.tag}] No warnings or watches for {self.place}")]
        return [self.alert_text(a) for a in self.active[:2]]

    # ---------------------------------------------------------------- the shared web board
    def site_state(self) -> dict:
        wx = self.wx or {}
        c, dly = wx.get("current", {}), wx.get("daily", {})
        days = []
        for i, day in enumerate(dly.get("time", [])[:3]):
            try:
                days.append({"date": day, "hi": round(dly["temperature_2m_max"][i]), "lo": round(dly["temperature_2m_min"][i]),
                             "pop": dly["precipitation_probability_max"][i], "text": wmo.describe(dly["weather_code"][i])})
            except (KeyError, IndexError, TypeError):
                continue
        return {
            **self.base_state(),
            "place": self.place,
            "now": ({"temp": c.get("temperature_2m"), "text": wmo.describe(c.get("weather_code")),
                     "wind": c.get("wind_speed_10m"), "dir": wmo.compass(c.get("wind_direction_10m"))} if c else None),
            "days": days,
            "alerts": [{"name": a["name"], "kind": a["kind"], "expires": a["expires"], "text": first_sentence(a["text"], 160)}
                       for a in self.active],
        }

