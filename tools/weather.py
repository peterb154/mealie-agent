"""Weather tool — Open-Meteo, no API key.

One tool: ``get_weather(location, days)``. The agent is responsible for
knowing the user's location (stored in household memory) — this tool
just resolves the city string to lat/lon via Open-Meteo's geocoding,
then fetches a current-conditions + daily forecast block.

Useful for weather-aligned recipe suggestions: BBQ on the warm day,
soup when it's cold, sheet-pan dinners when nobody wants to stand at
a stove in 95°F humidity.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from strands import tool

logger = logging.getLogger(__name__)

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# WMO weather code → short human label. Open-Meteo returns ints; we map
# the common ones. Anything unmapped falls through to "code N".
# Reference: https://open-meteo.com/en/docs (Weather variable docs)
_WMO: dict[int, str] = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "rain showers",
    81: "heavy rain showers",
    82: "violent rain showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm w/ hail",
    99: "severe thunderstorm",
}


def _wmo_label(code: int | None) -> str:
    if code is None:
        return "?"
    return _WMO.get(int(code), f"code {code}")


def _geocode(location: str) -> dict[str, Any] | None:
    """Resolve a place string to lat/lon + display name.

    Open-Meteo's geocoder takes a city name only — it doesn't parse
    "Des Moines, IA". So we split on the first comma, geocode the city
    half, and use the region half (state name, country name, or country
    code) to disambiguate among multiple matches client-side.
    """
    city, _, region = location.partition(",")
    city = city.strip()
    region = region.strip().lower()
    if not city:
        return None
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(_GEOCODE_URL, params={"name": city, "count": 5})
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
    if not results:
        return None
    if region:
        for hit in results:
            haystacks = (
                str(hit.get("admin1", "")).lower(),
                str(hit.get("country", "")).lower(),
                str(hit.get("country_code", "")).lower(),
            )
            if any(region == h or h.startswith(region) for h in haystacks):
                return hit
    return results[0]


@tool
def get_weather(location: str, days: int = 3) -> str:
    """Current conditions + daily forecast for a location.

    Use this when the user asks about weather, OR when you want to
    factor weather into a meal-plan suggestion (BBQ, grilling, soup,
    chili, no-cook salads on hot days, etc.).

    The agent is responsible for knowing where the user lives. Check
    `recall_household` for a stored `location:` first. If none is
    stored, ask the user (city, state/country) and save it with
    `remember_household` BEFORE calling this tool.

    Args:
        location: City + region, e.g. "Des Moines, IA" or "Lyon, France".
            Required — do not pass empty.
        days: Forecast horizon in days (1-7). Default 3.
    """
    loc = (location or "").strip()
    if not loc:
        return ("(weather error: no location given — ask the user where "
                "they are and store it via remember_household)")
    days = max(1, min(int(days), 7))

    try:
        place = _geocode(loc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("geocode failed for %r", loc)
        return f"(weather error: geocoding failed: {exc})"
    if not place:
        return f"(weather error: couldn't find a place named {loc!r})"

    lat, lon = place["latitude"], place["longitude"]
    pretty = ", ".join(
        x for x in (place.get("name"), place.get("admin1"), place.get("country_code")) if x
    )

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,weather_code",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "auto",
        "forecast_days": days,
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(_FORECAST_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.exception("forecast failed for %r", loc)
        return f"(weather error: forecast fetch failed: {exc})"

    out: list[str] = [f"# Weather for {pretty}"]
    cur = data.get("current") or {}
    if cur:
        out.append(
            f"**Now:** {cur.get('temperature_2m', '?')}°F "
            f"(feels {cur.get('apparent_temperature', '?')}°F), "
            f"{_wmo_label(cur.get('weather_code'))}, "
            f"wind {cur.get('wind_speed_10m', '?')} mph, "
            f"precip {cur.get('precipitation', 0)}\""
        )

    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    if dates:
        out.append("\n## Forecast")
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        precip = daily.get("precipitation_sum") or []
        pop = daily.get("precipitation_probability_max") or []
        codes = daily.get("weather_code") or []
        for i, d in enumerate(dates):
            out.append(
                f"- **{d}** — "
                f"{_wmo_label(codes[i] if i < len(codes) else None)}, "
                f"high {highs[i] if i < len(highs) else '?'}°F / "
                f"low {lows[i] if i < len(lows) else '?'}°F, "
                f"precip {precip[i] if i < len(precip) else 0}\" "
                f"({pop[i] if i < len(pop) else 0}% chance)"
            )

    return "\n".join(out)


def weather_tools() -> list[Any]:
    """Tool list for the agent. No client binding — Open-Meteo is public."""
    return [get_weather]
