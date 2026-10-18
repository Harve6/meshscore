"""Open-Meteo weather codes (WMO) in a few words, and compass points."""
from __future__ import annotations

CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "showers", 81: "showers", 82: "heavy showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm, hail", 99: "thunderstorm, hail",
}
_POINTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def describe(code) -> str:
    try:
        return CODES.get(int(code), "weather")
    except (TypeError, ValueError):
        return "weather"


def compass(deg) -> str:
    try:
        return _POINTS[int(((float(deg) % 360) + 22.5) // 45) % 8]
    except (TypeError, ValueError):
        return ""
