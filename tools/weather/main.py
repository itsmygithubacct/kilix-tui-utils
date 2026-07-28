"""kilix-weather — forecast from Open-Meteo.

The only tool here that reaches the network, in a stack that is otherwise
loopback-first, so it is explicit about that: Open-Meteo needs no account and no
key, so nothing secret is ever stored; the location is configured rather than
derived from the IP address, so using it does not disclose where the machine is
to a geolocation service; and the last good response is cached, so an offline
machine still renders with a visible "last updated" instead of an error.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src"))

from kilix_tui import app, keys as keymap, proc  # noqa: E402

ENDPOINT = "https://api.open-meteo.com/v1/forecast"
CODES = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    95: "thunderstorm", 96: "thunderstorm with hail",
}


def cache_path() -> str:
    base = os.environ.get("KILIX_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local/gpu_terminal/kilix/cache")
    return os.path.join(base, "weather.json")


def location() -> tuple[float, float, str]:
    """Latitude/longitude come from settings, never from IP geolocation."""
    from kilix_tui import theme
    lat = theme.setting("KILIX_WEATHER_LAT", "")
    lon = theme.setting("KILIX_WEATHER_LON", "")
    name = theme.setting("KILIX_WEATHER_PLACE", "")
    try:
        return float(lat), float(lon), name or f"{lat},{lon}"
    except ValueError:
        return 0.0, 0.0, ""


def fetch(lat: float, lon: float, timeout: int = 10) -> dict:
    query = urllib.parse.urlencode({
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code",
        "timezone": "auto", "forecast_days": "5",
    })
    with urllib.request.urlopen(f"{ENDPOINT}?{query}", timeout=timeout) as body:
        return json.load(body)


def load_cache() -> dict | None:
    try:
        with open(cache_path(), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def save_cache(payload: dict) -> None:
    path = cache_path()
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(path, 0o600)
    except OSError:
        pass


class State:
    def __init__(self) -> None:
        self.lat, self.lon, self.place = location()
        self.data: dict | None = None
        self.fetched_at = 0.0
        self.status = ""
        cached = load_cache()
        if cached:
            self.data = cached.get("data")
            self.fetched_at = cached.get("fetched_at", 0.0)
            self.status = "cached"
        if self.lat or self.lon:
            self.refresh()

    def refresh(self) -> None:
        if not (self.lat or self.lon):
            self.status = "no location configured"
            return
        try:
            self.data = fetch(self.lat, self.lon)
            self.fetched_at = time.time()
            self.status = "live"
            save_cache({"data": self.data, "fetched_at": self.fetched_at})
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            # Offline is an ordinary state for this tool, not an error.
            self.status = "offline — showing cached data" if self.data else \
                "offline and no cached data"


def render(surface, state: State) -> None:
    height, width = surface.getmaxyx()
    surface.addstr(0, 0, f"Kilix Weather — {state.place or 'no location'}"[: width - 1])
    if not (state.lat or state.lon):
        surface.addstr(2, 0, "Set a location to use this tool:"[: width - 1])
        surface.addstr(3, 0,
                       "  kilix settings --set weather_lat=51.5 weather_lon=-0.12"[: width - 1])
        surface.addstr(4, 0,
                       "The location is configured, never derived from your IP."[: width - 1])
        surface.addstr(height - 1, 0, "q quit"[: width - 1])
        return
    stamp = (time.strftime("%Y-%m-%d %H:%M", time.localtime(state.fetched_at))
             if state.fetched_at else "never")
    surface.addstr(1, 0, f"{state.status} · last updated {stamp}"[: width - 1])
    data = state.data or {}
    current = data.get("current", {})
    if current:
        code = int(current.get("weather_code", -1))
        surface.addstr(3, 0,
                       f"{current.get('temperature_2m', '?')}°C  "
                       f"{CODES.get(code, 'unknown')}  "
                       f"humidity {current.get('relative_humidity_2m', '?')}%  "
                       f"wind {current.get('wind_speed_10m', '?')} km/h"[: width - 1])
    daily = data.get("daily", {})
    times = daily.get("time", [])
    row = 5
    for index, day in enumerate(times):
        if row >= height - 1:
            break
        high = daily.get("temperature_2m_max", [None] * len(times))[index]
        low = daily.get("temperature_2m_min", [None] * len(times))[index]
        code = daily.get("weather_code", [-1] * len(times))[index]
        surface.addstr(row, 0,
                       f"{day}  {low:>5}°  {high:>5}°  "
                       f"{CODES.get(int(code), '')}"[: width - 1])
        row += 1
    surface.addstr(height - 1, 0, "r refresh · q quit"[: width - 1])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = State()
    if path := app.screenshot_argv(argv):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(app.render_to_text(render, state) + "\n")
        return 0

    def handle(key: int, s: State) -> bool:
        if keymap.is_quit(key):
            return False
        if keymap.is_refresh(key):
            s.refresh()
        return True

    return app.run(render, state, handle=handle)


if __name__ == "__main__":
    raise SystemExit(main())
