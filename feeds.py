"""
External feeds: transit, weather and travel time.

Two rules hold for everything in here, and they come from how the CalDAV
cache already behaves:

  * The page renders from SQLite. A fetch updates the cache; it is never in
    the path of a render.
  * A failure keeps the last good payload. The Pi's WLAN drops and these APIs
    time out, and neither event should be visible on a wall.

Providers sit behind a small interface, mirroring the SensorSource pattern in
presence/daemon.py. That is not speculative generality: the obvious German
transit API turned out to be unreliable while this was being written, so
having a second implementation is the actual requirement rather than a
hypothetical one.

`requests` is declared in requirements.txt. It used to come in through
caldav, which dropped it for niquests in 3.x — hence the explicit pin.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone

import requests

import database

logger = logging.getLogger("wallcal.feeds")

#: Kept short: a wall display that blocks for 30s on a dead API delays every
#: other feed behind it in the poller thread.
TIMEOUT = 8
USER_AGENT = "WallCal/1.0 (+https://github.com/ivanzgroot/WallCal)"


def _get(url, params=None):
    r = requests.get(url, params=params or {}, timeout=TIMEOUT,
                     headers={"User-Agent": USER_AGENT,
                              "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def cached(feed, ttl, fetch, allow_fetch=True):
    """Refresh ``feed`` if its TTL has expired; always return the cache entry.

    The return value is whatever is cached, fresh or not — callers render it
    either way and let the staleness marker do the talking.

    ``allow_fetch=False`` is the render path: answer from SQLite and never
    reach the network, however stale the copy is. Refusing to answer at all
    would put a blank widget on the wall every time the Pi lost its WLAN,
    which is the one outcome this module exists to prevent.
    """
    if not allow_fetch or database.feed_is_fresh(feed):
        return database.get_feed(feed)
    try:
        database.cache_feed(feed, fetch(), ttl)
    except Exception as exc:
        logger.info("feed %s failed (%s) — serving the cached copy", feed, exc)
        database.mark_feed_error(feed, exc, ttl)
    return database.get_feed(feed)


# ---------------------------------------------------------------------------
# Transit
# ---------------------------------------------------------------------------

class TransitProvider:
    """Departures and station search for one public-transport backend."""

    name = "base"
    label = "base"

    def search(self, query: str) -> list:
        """[{id, name, lat, lon}] for a station name."""
        raise NotImplementedError

    def departures(self, station_id: str, minutes: int = 60) -> list:
        """[{line, direction, planned, expected, delay_minutes, cancelled}]."""
        raise NotImplementedError


class TransitousProvider(TransitProvider):
    """Transitous — a community MOTIS instance covering Germany via GTFS.

    Chosen over v6.db.transport.rest: that wraps Deutsche Bahn only, its HAFAS
    backend was shut off permanently, and its data endpoints were returning
    503 while this was written. Transitous is keyless, covers every German
    operator through DELFI and the regional feeds rather than DB alone, and
    the same instance also does the routing that travel time needs.
    """

    name = "transitous"
    label = "Transitous (DELFI, ganz Deutschland)"
    BASE = "https://api.transitous.org/api/v1"

    def search(self, query):
        rows = _get(f"{self.BASE}/geocode", {"text": query}) or []
        out = []
        for row in rows:
            if row.get("type") != "STOP":
                continue
            out.append({
                "id": row.get("id"),
                "name": row.get("name"),
                "lat": row.get("lat"),
                "lon": row.get("lon"),
                "area": (row.get("areas") or [{}])[0].get("name", ""),
                "modes": row.get("modes") or [],
            })
        return out[:12]

    def departures(self, station_id, minutes=60):
        data = _get(f"{self.BASE}/stoptimes", {
            "stopId": station_id,
            "n": 12,
            "time": _now_iso(),
            "arriveBy": "false",
        })
        out = []
        for st in (data or {}).get("stopTimes", []):
            planned = st.get("place", {}).get("scheduledDeparture")
            actual = st.get("place", {}).get("departure")
            delay = _delay_minutes(planned, actual)
            out.append({
                "line": (st.get("routeShortName")
                         or st.get("displayName") or "?").strip(),
                "direction": (st.get("headsign") or "").strip(),
                "planned": planned,
                "expected": actual or planned,
                "delay_minutes": delay,
                "cancelled": bool(st.get("cancelled")),
                "mode": st.get("mode", ""),
            })
        return out


class DbRestProvider(TransitProvider):
    """v6.db.transport.rest — Deutsche Bahn only.

    Kept as the second implementation because it is the better answer for
    long-distance rail when it is up. Not the default: see TransitousProvider.
    """

    name = "db-rest"
    label = "Deutsche Bahn (v6.db.transport.rest)"
    BASE = "https://v6.db.transport.rest"

    def search(self, query):
        rows = _get(f"{self.BASE}/locations",
                    {"query": query, "results": 12, "stops": "true"}) or []
        return [{"id": r.get("id"), "name": r.get("name"),
                 "lat": (r.get("location") or {}).get("latitude"),
                 "lon": (r.get("location") or {}).get("longitude"),
                 "area": "", "modes": []}
                for r in rows if r.get("type") in ("stop", "station")]

    def departures(self, station_id, minutes=60):
        data = _get(f"{self.BASE}/stops/{station_id}/departures",
                    {"duration": minutes, "results": 12})
        rows = data.get("departures", data) if isinstance(data, dict) else data
        out = []
        for d in rows or []:
            out.append({
                "line": ((d.get("line") or {}).get("name") or "?").strip(),
                "direction": (d.get("direction") or "").strip(),
                "planned": d.get("plannedWhen"),
                "expected": d.get("when") or d.get("plannedWhen"),
                "delay_minutes": int(round((d.get("delay") or 0) / 60.0)),
                "cancelled": bool(d.get("cancelled")),
                "mode": (d.get("line") or {}).get("product", ""),
            })
        return out


PROVIDERS = {p.name: p for p in (TransitousProvider, DbRestProvider)}


def transit_provider(name=None):
    name = name or database.get_setting("transit_provider", "transitous")
    return PROVIDERS.get(str(name), TransitousProvider)()


def transit_departures(settings=None, allow_fetch=True):
    """Cached departures for the configured station, filtered and shaped.

    The filtering and the count live here, on the far side of the cache: the
    cache holds whatever the provider returned, and every caller wants it cut
    down the same way. A caller that reads the cache row itself gets the raw
    twelve, which is how the count setting came to do nothing at all.
    """
    settings = settings or database.get_all_settings()
    station = (settings.get("transit_station_id") or "").strip()
    if not station:
        return None

    ttl = _int(settings.get("transit_refresh_seconds"), 60)
    provider = transit_provider(settings.get("transit_provider"))
    feed = f"transit:{provider.name}:{station}"
    entry = cached(feed, ttl, lambda: provider.departures(station), allow_fetch)
    if not entry:
        return None

    rows = entry["payload"] or []
    line_filter = _csv(settings.get("transit_filter_lines"))
    dir_filter = _csv(settings.get("transit_filter_directions"))
    limit = _int(settings.get("transit_count"), 3)

    out = []
    for row in rows:
        if line_filter and row.get("line", "").lower() not in line_filter:
            continue
        if dir_filter and not any(
                d in row.get("direction", "").lower() for d in dir_filter):
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return {"departures": out, "meta": _meta(entry)}


# ---------------------------------------------------------------------------
# Weather — Open-Meteo, no key
# ---------------------------------------------------------------------------

def weather(settings=None, allow_fetch=True):
    """Current conditions plus the single most actionable fact for today."""
    settings = settings or database.get_all_settings()
    lat = settings.get("weather_lat")
    lon = settings.get("weather_lon")
    if not lat or not lon:
        return None

    units = settings.get("weather_units", "metric")
    tz = settings.get("timezone", "auto")
    ttl = _int(settings.get("weather_refresh_seconds"), 900)

    def fetch():
        return _get("https://api.open-meteo.com/v1/forecast", {
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,apparent_temperature,precipitation,"
                       "weather_code,wind_speed_10m,is_day",
            "hourly": "temperature_2m,precipitation_probability,precipitation,"
                      "weather_code",
            "daily": "temperature_2m_min,temperature_2m_max,"
                     "precipitation_probability_max,weather_code",
            "timezone": tz if tz and tz != "auto" else "auto",
            "forecast_days": 2,
            "temperature_unit": "fahrenheit" if units == "imperial" else "celsius",
            "wind_speed_unit": "mph" if units == "imperial" else "kmh",
        })

    entry = cached("weather", ttl, fetch, allow_fetch)
    if not entry:
        return None
    data = entry["payload"] or {}
    return {
        "current": data.get("current") or {},
        "hourly": data.get("hourly") or {},
        "daily": data.get("daily") or {},
        "units": units,
        "headline": _weather_headline(data, settings),
        "meta": _meta(entry),
    }


#: Only these justify space on a wall. "Partly cloudy, 18°" answers nothing.
def _weather_headline(data, settings):
    """The single most actionable fact, or None when there is nothing to say.

    The dynamic visibility rule depends on this returning None: a widget with
    nothing to report should not be on the wall at all.
    """
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    rain = hourly.get("precipitation") or []
    prob = hourly.get("precipitation_probability") or []
    temps = hourly.get("temperature_2m") or []
    daily = data.get("daily") or {}
    current = data.get("current") or {}
    now = datetime.now()

    # Rain starting within the next 12 hours, and when.
    for i, stamp in enumerate(times[:24]):
        at = _parse_local(stamp)
        if not at or at < now:
            continue
        if (i < len(rain) and (rain[i] or 0) >= 0.2) or \
           (i < len(prob) and (prob[i] or 0) >= 60):
            return {"kind": "rain", "at": at.strftime("%H:%M"),
                    "text": f"Regen ab {at.strftime('%H:%M')}"}

    # Frost tonight.
    mins = daily.get("temperature_2m_min") or []
    if mins and mins[0] is not None and mins[0] <= 0:
        return {"kind": "frost", "text": "Frost heute Nacht"}

    wind = current.get("wind_speed_10m")
    if wind is not None and wind >= 40:
        return {"kind": "wind", "text": f"Windig, {round(wind)} km/h"}

    highs = daily.get("temperature_2m_max") or []
    if highs and highs[0] is not None and highs[0] >= 30:
        return {"kind": "heat", "text": f"Heiß, bis {round(highs[0])}°"}

    # Nothing worth saying. The widget hides itself in dynamic mode.
    if temps:
        return None
    return None


# ---------------------------------------------------------------------------
# Travel time
# ---------------------------------------------------------------------------

def travel_time(destination, settings=None, allow_fetch=True):
    """Minutes to ``destination`` by public transport, or None.

    Geocoding is cached hard and separately from the route: event locations
    repeat constantly — the same dentist every six months — and the address
    is the expensive half.
    """
    settings = settings or database.get_all_settings()
    if not destination or not str(destination).strip():
        return None
    home_lat, home_lon = settings.get("home_lat"), settings.get("home_lon")
    if not home_lat or not home_lon:
        return None

    dest = _geocode(destination, allow_fetch)
    if not dest:
        return None      # unparseable address: skip silently, never an error

    feed = f"travel:{round(float(home_lat),3)},{round(float(home_lon),3)}" \
           f"->{round(dest['lat'],3)},{round(dest['lon'],3)}"

    def fetch():
        data = _get(f"{TransitousProvider.BASE}/plan", {
            "fromPlace": f"{home_lat},{home_lon}",
            "toPlace": f"{dest['lat']},{dest['lon']}",
            "time": _now_iso(),
            "numItineraries": 1,
        })
        its = (data or {}).get("itineraries") or []
        if not its:
            raise ValueError("no itinerary")
        return {"minutes": int(round((its[0].get("duration") or 0) / 60.0))}

    entry = cached(feed, _int(settings.get("travel_refresh_seconds"), 1800), fetch,
                   allow_fetch)
    if not entry or not entry["payload"]:
        return None
    return {"minutes": entry["payload"].get("minutes"),
            "destination": dest["name"], "meta": _meta(entry)}


def geocode_search(text):
    """Free-text place search for the settings location pickers."""
    rows = _get(f"{TransitousProvider.BASE}/geocode", {"text": text}) or []
    out = []
    for row in rows[:10]:
        out.append({"name": row.get("name"), "lat": row.get("lat"),
                    "lon": row.get("lon"), "type": row.get("type"),
                    "area": (row.get("areas") or [{}])[0].get("name", "")})
    return out


def _geocode(text, allow_fetch=True):
    """Resolve a free-text location once and keep it for a month."""
    key = "geo:" + " ".join(str(text).split()).lower()[:120]
    entry = cached(key, 30 * 86400, lambda: _geocode_fetch(text), allow_fetch)
    if not entry or not entry["payload"]:
        return None
    return entry["payload"]


def _geocode_fetch(text):
    rows = _get(f"{TransitousProvider.BASE}/geocode", {"text": text}) or []
    if not rows:
        raise ValueError("no match")
    best = rows[0]
    return {"name": best.get("name"), "lat": best.get("lat"), "lon": best.get("lon")}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _meta(entry):
    return {"age_seconds": entry.get("age_seconds"),
            "stale": entry.get("stale"), "ok": entry.get("ok"),
            "error": entry.get("error")}


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_local(stamp):
    try:
        return datetime.fromisoformat(str(stamp))
    except ValueError:
        return None


def _delay_minutes(planned, actual):
    if not planned or not actual or planned == actual:
        return 0
    a, b = parse_iso(planned), parse_iso(actual)
    if not a or not b:
        return 0
    return int(round((b - a).total_seconds() / 60.0))


def parse_iso(stamp):
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _int(value, default):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _csv(value):
    return [p.strip().lower() for p in str(value or "").split(",") if p.strip()]
