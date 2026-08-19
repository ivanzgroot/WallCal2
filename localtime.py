"""One place that knows what time it is on this wall.

Events are cached as UTC — `caldav_poller` writes them that way and
`wall.js` reads them that way, appending the Z before handing them to the
browser. Three consumers on the Python side did not: they stripped the Z,
kept the naive value and compared it against `datetime.now()`, which is
local. In Berlin that is an hour out in winter and two in summer, and the
symptoms were not subtle — the anticipatory wake lit the panel two hours
early and was dark again by the time the meeting started, and the travel
widget skipped every appointment closer than the offset while showing ones
three hours away.

So the conversion lives here, once, and everything that reads a cached
event goes through it.

The zone comes from the `timezone` setting. "auto" means the machine's own,
which is what a wall on a shelf in one room should do; naming a zone matters
when the Pi's clock is on UTC, which several images ship as the default.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone, tzinfo

logger = logging.getLogger("wallcal.localtime")

try:                                    # 3.9+, and Raspberry Pi OS has tzdata
    from zoneinfo import ZoneInfo, available_timezones
except ImportError:                     # pragma: no cover - very old runtimes
    ZoneInfo = None
    available_timezones = None

AUTO = "auto"

#: Resolving a zone reads the system tz database, so remember the answer.
#: Cleared whenever the setting changes.
_zone_cache: dict = {}


def zone(name: str | None = None) -> tzinfo:
    """The wall's timezone. Falls back to the machine's own, never to UTC.

    A wrong-but-plausible local zone shows a clock that is off by an hour.
    Falling back to UTC would show one that is off by however far the Pi is
    from Greenwich, on a device whose entire job is telling you the time.
    """
    if name is None:
        name = AUTO
    name = str(name).strip() or AUTO
    if name in _zone_cache:
        return _zone_cache[name]

    resolved = None
    if name.lower() != AUTO and ZoneInfo is not None:
        try:
            resolved = ZoneInfo(name)
        except Exception as exc:
            logger.warning("Unknown timezone %r (%s) — using the system zone",
                           name, exc)
    if resolved is None:
        # astimezone() on a naive datetime attaches whatever the machine is
        # set to, which is what "auto" has always meant here.
        resolved = datetime.now().astimezone().tzinfo or timezone.utc

    _zone_cache[name] = resolved
    return resolved


def forget() -> None:
    """Drop the cache — the setting changed, or the machine's zone did."""
    _zone_cache.clear()


def zone_name(settings=None) -> str:
    settings = settings if settings is not None else _settings()
    return str(settings.get("timezone") or AUTO)


def _settings():
    import database
    return database.get_all_settings()


def now(settings=None) -> datetime:
    """Now, as an aware datetime in the wall's zone."""
    return datetime.now(zone(zone_name(settings)))


def to_local(moment: datetime, settings=None) -> datetime:
    """Any datetime into the wall's zone. Naive input is read as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(zone(zone_name(settings)))


def parse_event_start(event, settings=None) -> datetime | None:
    """The start of a cached event, aware, in the wall's zone.

    All-day entries are dates wearing a datetime's clothes: the cache stores
    them at UTC midnight, and reading that as an instant puts them on the
    wrong day for anyone west of Greenwich. They come back as local midnight
    on the date that was written, which is what "all day" means.
    """
    raw = str((event or {}).get("dtstart") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

    if (event or {}).get("all_day"):
        return datetime(parsed.year, parsed.month, parsed.day,
                        tzinfo=zone(zone_name(settings)))
    return to_local(parsed, settings)


def event_day(event, settings=None) -> str:
    """The calendar day a cached event falls on here, as YYYY-MM-DD."""
    start = parse_event_start(event, settings)
    return start.strftime("%Y-%m-%d") if start else ""


def available() -> list:
    """Every zone this machine knows, sorted, for the settings picker."""
    if available_timezones is None:
        return list(FALLBACK_ZONES)
    try:
        return sorted(available_timezones())
    except Exception as exc:
        logger.warning("Could not list timezones (%s)", exc)
        return list(FALLBACK_ZONES)


#: Enough to get a wall configured when the tz database is unreadable.
FALLBACK_ZONES = (
    "Europe/Berlin", "Europe/Vienna", "Europe/Zurich", "Europe/London",
    "Europe/Paris", "Europe/Madrid", "Europe/Rome", "Europe/Warsaw",
    "Europe/Amsterdam", "Europe/Prague", "Europe/Stockholm", "UTC",
)
