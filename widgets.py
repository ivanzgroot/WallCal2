"""
Widget assembly: visibility rules and display shaping.

Each widget has one three-way visibility setting — always / dynamic / off —
and its own rule for what "dynamic" means. The rules live here rather than in
the browser because they decide whether a feed is worth fetching at all, and
the cheapest network request is the one nobody makes.

The shapes returned are deliberately final: the wall display renders them
without deciding anything, so a widget can never be half-rendered while the
browser works out whether it should be there.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time as dtime, timedelta, timezone

import database
import feeds
from presence import runtime

logger = logging.getLogger("wallcal.widgets")

ALWAYS, DYNAMIC, OFF = "always", "dynamic", "off"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: How often the refresher re-runs the visibility rules. It only reaches the
#: network when a feed's own TTL has lapsed, so this is the granularity of
#: staleness rather than a request rate: the shortest TTL is transit's 60 s.
REFRESH_INTERVAL = 20.0


def visibility(settings, key):
    value = str(settings.get("widget_" + key, DYNAMIC)).strip().lower()
    return value if value in (ALWAYS, DYNAMIC, OFF) else DYNAMIC


def collect(settings, allow_fetch=True, now=None):
    """Everything the wall display needs, already decided.

    ``visible`` is the widget's own verdict. ``slot`` is a stable grid area
    name: reserved whether or not the widget is showing, so a bus becoming due
    never reshuffles the layout around it.

    ``allow_fetch`` is what separates the two callers. The Refresher passes
    True on its own thread and may go to the network; the HTTP handler passes
    False and is answered from SQLite alone. Both run the same rules, so the
    decision about whether a feed is worth having exists in exactly one place.
    """
    now = now or datetime.now()
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "transit": _transit(settings, allow_fetch, now),
        "weather": _weather(settings, allow_fetch, now),
        "travel": _travel(settings, allow_fetch, now),
        "qr": _qr(settings),
        "feeds": database.feed_freshness(),
    }


# ---------------------------------------------------------------------------
# Background refresh
#
# The fetch used to happen inside the /api/widgets handler, which put an
# 8 s-per-feed network timeout in front of a request the wall abandons after
# 10 s: on a slow WLAN the widgets stopped updating even though the cache was
# fine. feeds.py already says a fetch is never in the path of a render — this
# is the thread that makes that true for widgets too.
# ---------------------------------------------------------------------------

class Refresher:
    """Keeps the feed cache warm so the HTTP handler never has to wait."""

    def __init__(self, interval=REFRESH_INTERVAL):
        self.interval = interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self.last_run = None

    def start(self):
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="widgets")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._wake.set()

    def poke(self):
        """Refresh now — after a settings change invalidated a feed."""
        self._wake.set()

    def refresh_once(self):
        """One pass. Discards the result: the point is the cache behind it."""
        settings = database.get_all_settings()

        # Never poll a dark screen. The daemon publishes the panel state, so a
        # sleeping wall stops costing anyone's rate limit.
        if runtime.read_state().get("display_on") is False:
            return False

        collect(settings, allow_fetch=True)
        self.last_run = datetime.now()
        return True

    def _run(self):
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception:
                logger.exception("widget refresh loop")
            self._wake.wait(self.interval)
            self._wake.clear()


# ---------------------------------------------------------------------------
# ÖPNV
# ---------------------------------------------------------------------------

def _transit(settings, allow_fetch, now):
    vis = visibility(settings, "transit")
    slot = {"slot": "transit", "visible": False, "departures": []}
    if vis == OFF or not (settings.get("transit_station_id") or "").strip():
        return slot

    in_window = _in_any_window(settings.get("transit_windows", ""), now)
    slot["in_window"] = in_window
    slot["station"] = settings.get("transit_station_name", "")

    # Dynamic means "only during the configured windows". Outside them the
    # feed is not fetched either — a departure board nobody is reading should
    # not be spending a rate limit.
    if vis == DYNAMIC and not in_window:
        return slot
    if not allow_fetch and not database.get_feed_any_transit(settings):
        return slot

    data = feeds.transit_departures(settings) if allow_fetch else None
    if data is None:
        cached = database.get_feed(_transit_feed_name(settings))
        data = {"departures": (cached or {}).get("payload") or [],
                "meta": cached} if cached else None
    if not data:
        return slot

    threshold = _int(settings.get("transit_relative_below_min"), 20)
    rows = []
    for d in data["departures"]:
        rows.append(_shape_departure(d, now, threshold))
    slot["departures"] = rows
    slot["meta"] = data.get("meta")
    # `always` keeps the board up even when there is nothing on it; `dynamic`
    # withdraws rather than showing an empty frame.
    slot["visible"] = bool(rows) or vis == ALWAYS
    return slot


def _transit_feed_name(settings):
    provider = str(settings.get("transit_provider", "transitous"))
    return f"transit:{provider}:{settings.get('transit_station_id','')}"


def _shape_departure(d, now, relative_below_min):
    """Shape one departure for display.

    The API answers in UTC and ``now`` is naive local, so both are pinned to
    an absolute instant before subtracting. Mixing the two silently produces
    an hour-wrong "in N min" for most of the year in Berlin.
    """
    when = feeds.parse_iso(d.get("expected") or d.get("planned"))
    minutes = None
    local = None
    if when is not None:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        reference = now.astimezone() if now.tzinfo is None else now
        minutes = int(round((when - reference).total_seconds() / 60.0))
        local = when.astimezone()
    return {
        "line": d.get("line", ""),
        "direction": d.get("direction", ""),
        # Relative under the threshold, absolute above it: "in 4 min" is what
        # you act on, "17:42" is what you plan around.
        "when": (f"in {minutes} min"
                 if minutes is not None and 0 <= minutes < relative_below_min
                 else (local.strftime("%H:%M") if local else "")),
        "minutes": minutes,
        "delay": d.get("delay_minutes", 0),
        "cancelled": bool(d.get("cancelled")),
        "mode": d.get("mode", ""),
    }


def _in_any_window(spec, now):
    """Per-weekday windows: 'mon=06:30-09:00,16:00-18:30|sat='."""
    today = WEEKDAYS[now.weekday()]
    for part in str(spec or "").split("|"):
        day, _, ranges = part.partition("=")
        if day.strip().lower() != today:
            continue
        for window in ranges.split(","):
            start, _, end = window.partition("-")
            a, b = _hhmm(start), _hhmm(end)
            if a and b and a <= now.time() < b:
                return True
        return False
    return False


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------

def _weather(settings, allow_fetch, now):
    vis = visibility(settings, "weather")
    slot = {"slot": "weather", "visible": False}
    if vis == OFF or not settings.get("weather_lat"):
        return slot

    # Never `if allow_fetch else None`: the render path is the one that has
    # to answer, and it answers from SQLite. Gating the whole call on the
    # flag left the widget permanently invisible the moment the fetching
    # moved to its own thread.
    data = feeds.weather(settings, allow_fetch)
    if not data:
        return slot

    current = data.get("current") or {}
    temp = current.get("temperature_2m")
    headline = data.get("headline")
    feels = current.get("apparent_temperature")
    slot.update({
        "temperature": round(temp) if temp is not None else None,
        "feels_like": round(feels) if feels is not None else None,
        "code": current.get("weather_code"),
        "is_day": bool(current.get("is_day", 1)),
        "units": "°F" if data.get("units") == "imperial" else "°",
        "headline": headline["text"] if headline else None,
        "hourly": _hourly_slice(data.get("hourly") or {}, now),
        "tomorrow": _tomorrow(data.get("daily") or {}),
        "place": settings.get("weather_place", ""),
        "meta": data.get("meta"),
    })
    # Actionable content only. With nothing to warn about, a dynamic weather
    # widget is a temperature readout nobody needed.
    slot["visible"] = bool(headline) or vis == ALWAYS
    return slot


def _hourly_slice(hourly, now, hours=12):
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    precip = hourly.get("precipitation_probability") or []
    codes = hourly.get("weather_code") or []
    out = []
    for i, stamp in enumerate(times):
        at = feeds._parse_local(stamp)
        if not at or at < now.replace(minute=0, second=0, microsecond=0):
            continue
        out.append({
            "at": at.strftime("%H"),
            "temp": round(temps[i]) if i < len(temps) and temps[i] is not None else None,
            "rain": precip[i] if i < len(precip) else None,
            "code": codes[i] if i < len(codes) else None,
            # Icons differ between day and night for clear skies, the same way
            # a phone shows a moon after sunset.
            "is_day": 6 <= at.hour < 21,
        })
        if len(out) >= hours:
            break
    return out


def _tomorrow(daily):
    mins, maxs = daily.get("temperature_2m_min") or [], daily.get("temperature_2m_max") or []
    if len(mins) < 2 or len(maxs) < 2:
        return None
    codes = daily.get("weather_code") or []
    return {"min": round(mins[1]) if mins[1] is not None else None,
            "max": round(maxs[1]) if maxs[1] is not None else None,
            "rain": (daily.get("precipitation_probability_max") or [None, None])[1],
            "code": codes[1] if len(codes) > 1 else None}


# ---------------------------------------------------------------------------
# Travel time
# ---------------------------------------------------------------------------

def _travel(settings, allow_fetch, now):
    vis = visibility(settings, "travel")
    slot = {"slot": "travel", "visible": False}
    if vis == OFF or not settings.get("home_lat"):
        return slot

    event = _next_event_with_location(settings, now)
    if not event:
        return slot          # no address: skip silently, never an error

    start = _parse_event_start(event)
    if not start:
        return slot
    window = _int(settings.get("travel_window_minutes"), 90)
    minutes_until = (start - now).total_seconds() / 60.0
    if vis == DYNAMIC and not (0 <= minutes_until <= window):
        return slot
    result = feeds.travel_time(event.get("location"), settings, allow_fetch)
    if not result or result.get("minutes") is None:
        return slot          # unroutable: also silent

    buffer_min = _int(settings.get("travel_buffer_minutes"), 5)
    leave_at = start - timedelta(minutes=result["minutes"] + buffer_min)
    slot.update({
        "visible": True,
        "minutes": result["minutes"],
        "leave_at": leave_at.strftime("%H:%M"),
        "late": leave_at < now,
        "summary": event.get("summary", ""),
        "location": event.get("location", ""),
        "meta": result.get("meta"),
    })
    return slot


def _next_event_with_location(settings, now):
    abfall = (settings.get("abfall_calendar_id") or "").strip()
    rows = database.get_cached_events(
        days=2, exclude_calendar_ids=[abfall] if abfall else None)
    for ev in rows:
        if ev.get("all_day") or not (ev.get("location") or "").strip():
            continue
        start = _parse_event_start(ev)
        if start and start >= now:
            return ev
    return None


def _parse_event_start(ev):
    try:
        raw = str(ev.get("dtstart", "")).replace("Z", "")
        at = datetime.fromisoformat(raw)
        return at.replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# QR companion code
# ---------------------------------------------------------------------------

def _qr(settings):
    vis = visibility(settings, "qr")
    return {
        "slot": "qr",
        # Default dynamic = NEAR only; useless from across the room. The wall
        # applies that, since only it knows the current density.
        "visible": vis != OFF,
        "mode": vis,
        "size": _int(settings.get("qr_size"), 96),
    }


# ---------------------------------------------------------------------------
# Abfall fraction mapping
# ---------------------------------------------------------------------------

def parse_fractions(spec):
    """'rest=Restmüll:#8A8F9C|bio=Bio:#6FAE3F' -> [(needle, label, colour)]."""
    out = []
    for part in str(spec or "").split("|"):
        needle, _, rest = part.partition("=")
        label, _, colour = rest.partition(":")
        if needle.strip() and label.strip():
            out.append((needle.strip().lower(), label.strip(),
                        colour.strip() or "#8A8F9C"))
    return out


def match_fraction(title, fractions):
    """Map an event title onto a fraction. Unknown titles keep their own name
    rather than being dropped — a Kommune this mapping has never seen should
    still put something on the wall."""
    low = str(title or "").lower()
    for needle, label, colour in fractions:
        if needle in low:
            return {"label": label, "color": colour}
    return {"label": str(title or "Abfall").strip(), "color": "#8A8F9C"}


def _hhmm(text):
    try:
        h, _, m = str(text).strip().partition(":")
        return dtime(int(h) % 24, int(m or 0) % 60)
    except (TypeError, ValueError):
        return None


def _int(value, default):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
