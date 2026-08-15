"""
WallCal CalDAV Poller
Background thread that periodically fetches events from configured CalDAV
servers (Nextcloud, iCloud, or any standard CalDAV). Supports auto-discovery
of calendars and recurring event expansion.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, date, timezone

import caldav
from icalendar import Calendar as iCalendar
from dateutil.rrule import rrulestr
from dateutil.tz import tzutc, gettz

import database

logger = logging.getLogger(__name__)

# Timezone for display — events stored as UTC internally
LOCAL_TZ = gettz("Europe/Berlin")


class CalDAVPoller:
    """Polls CalDAV servers in a background thread."""

    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._last_error = None
        self._last_poll = None
        self._polling = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the background polling thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Poller already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="caldav-poller")
        self._thread.start()
        logger.info("CalDAV poller started")

    def stop(self):
        """Signal the poller to stop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("CalDAV poller stopped")

    @property
    def status(self):
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "polling": self._polling,
            "last_poll": self._last_poll,
            "last_error": self._last_error,
        }

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _run(self):
        # Initial poll immediately
        self._poll_all()

        while not self._stop_event.is_set():
            interval = int(database.get_setting("poll_interval_minutes", "5"))
            # Sleep in small increments so we can stop quickly
            for _ in range(interval * 60):
                if self._stop_event.is_set():
                    return
                time.sleep(1)
            self._poll_all()

    def _poll_all(self):
        """Poll all enabled calendars."""
        self._polling = True
        calendars = database.get_enabled_calendars()
        if not calendars:
            logger.debug("No enabled calendars configured — skipping poll")
            self._polling = False
            return

        for cal_cfg in calendars:
            try:
                self._poll_calendar(cal_cfg)
            except Exception as e:
                self._last_error = f"Calendar '{cal_cfg['name']}': {e}"
                logger.error("Error polling calendar %s: %s",
                             cal_cfg["name"], e, exc_info=True)

        self._last_poll = datetime.now(timezone.utc).isoformat()
        self._polling = False
        logger.info("Poll complete for %d calendar(s)", len(calendars))

    # ------------------------------------------------------------------
    # Per-calendar polling
    # ------------------------------------------------------------------

    def _poll_calendar(self, cal_cfg):
        """Fetch events from one calendar and cache them."""
        client = caldav.DAVClient(
            url=cal_cfg["caldav_url"],
            username=cal_cfg["username"],
            password=cal_cfg["password"],
        )

        # If cal_path is set, use it directly; otherwise search via principal
        if cal_cfg.get("cal_path"):
            calendar = client.calendar(url=cal_cfg["cal_path"])
        else:
            principal = client.principal()
            cals = principal.calendars()
            if not cals:
                logger.warning("No calendars found for %s", cal_cfg["name"])
                return
            # Try to match by name
            calendar = None
            for c in cals:
                if c.name == cal_cfg["name"]:
                    calendar = c
                    break
            if calendar is None:
                calendar = cals[0]
                logger.info("Using first calendar: %s", calendar.name)

        # Fetch events for the next 30 days
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
        end = start + timedelta(days=30)

        try:
            results = calendar.date_search(start=start, end=end, expand=True)
        except Exception:
            # Some servers don't support expand, try without
            logger.debug("date_search with expand failed, retrying without")
            results = calendar.date_search(start=start, end=end, expand=False)

        events = []
        for cal_obj in results:
            try:
                parsed = self._parse_event(cal_obj, cal_cfg, start, end)
                events.extend(parsed)
            except Exception as e:
                logger.warning("Failed to parse event: %s", e)

        # Verify calendar still exists (may have been deleted during poll)
        if not database.get_calendar(cal_cfg["id"]):
            logger.info("Calendar '%s' was deleted during poll — skipping cache",
                        cal_cfg["name"])
            return

        database.cache_events(cal_cfg["id"], events)
        logger.debug("Fetched %d events from '%s'", len(events), cal_cfg["name"])

    # ------------------------------------------------------------------
    # Event parsing
    # ------------------------------------------------------------------

    def _parse_event(self, cal_obj, cal_cfg, window_start, window_end):
        """Parse a CalDAV object into one or more event dicts.

        Returns a list because recurring events expand into multiple instances.
        """
        ical = iCalendar.from_ical(cal_obj.data)
        events = []

        for component in ical.walk():
            if component.name != "VEVENT":
                continue

            uid = str(component.get("UID", ""))
            summary = str(component.get("SUMMARY", "No title"))
            description = str(component.get("DESCRIPTION", ""))
            location = str(component.get("LOCATION", ""))

            dtstart_prop = component.get("DTSTART")
            dtend_prop = component.get("DTEND")
            duration_prop = component.get("DURATION")

            if dtstart_prop is None:
                continue

            dtstart = dtstart_prop.dt
            all_day = isinstance(dtstart, date) and not isinstance(dtstart, datetime)

            if all_day:
                dtstart_dt = datetime(dtstart.year, dtstart.month, dtstart.day,
                                      tzinfo=tzutc())
                if dtend_prop:
                    dtend = dtend_prop.dt
                    dtend_dt = datetime(dtend.year, dtend.month, dtend.day,
                                        tzinfo=tzutc())
                else:
                    dtend_dt = dtstart_dt + timedelta(days=1)
            else:
                dtstart_dt = self._ensure_utc(dtstart)
                if dtend_prop:
                    dtend_dt = self._ensure_utc(dtend_prop.dt)
                elif duration_prop:
                    dtend_dt = dtstart_dt + duration_prop.dt
                else:
                    dtend_dt = dtstart_dt + timedelta(hours=1)

            # Handle recurring events
            rrule_prop = component.get("RRULE")
            if rrule_prop:
                occurrences = self._expand_rrule(
                    component, dtstart_dt, dtend_dt - dtstart_dt,
                    window_start, window_end, all_day
                )
                for occ_start, occ_end in occurrences:
                    events.append({
                        "uid": uid,
                        "summary": summary,
                        "description": description,
                        "location": location,
                        "dtstart": occ_start.strftime("%Y-%m-%dT%H:%M:%S"),
                        "dtend": occ_end.strftime("%Y-%m-%dT%H:%M:%S"),
                        "all_day": all_day,
                        "color": cal_cfg["color"],
                        "recurrence_id": occ_start.strftime("%Y%m%dT%H%M%S"),
                    })
            else:
                events.append({
                    "uid": uid,
                    "summary": summary,
                    "description": description,
                    "location": location,
                    "dtstart": dtstart_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "dtend": dtend_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "all_day": all_day,
                    "color": cal_cfg["color"],
                    "recurrence_id": "",
                })

        return events

    def _expand_rrule(self, component, dtstart, duration,
                      window_start, window_end, all_day):
        """Expand a RRULE into individual occurrences within the window."""
        rrule_prop = component.get("RRULE")
        if not rrule_prop:
            return []

        # Build the rrule string
        rrule_str = rrule_prop.to_ical().decode("utf-8")

        try:
            if hasattr(dtstart, 'tzinfo') and dtstart.tzinfo:
                rule = rrulestr("RRULE:" + rrule_str, dtstart=dtstart)
            else:
                rule = rrulestr("RRULE:" + rrule_str,
                                dtstart=dtstart.replace(tzinfo=tzutc()))

            # Ensure window bounds are timezone-aware
            ws = window_start if hasattr(window_start, 'tzinfo') and window_start.tzinfo \
                else window_start.replace(tzinfo=tzutc())
            we = window_end if hasattr(window_end, 'tzinfo') and window_end.tzinfo \
                else window_end.replace(tzinfo=tzutc())

            occurrences = []
            for occ in rule.between(ws, we, inc=True):
                occ_start = occ if occ.tzinfo else occ.replace(tzinfo=tzutc())
                occ_end = occ_start + duration
                occurrences.append((occ_start, occ_end))
                if len(occurrences) >= 200:  # safety limit
                    break
            return occurrences

        except Exception as e:
            logger.warning("Failed to expand RRULE: %s", e)
            return []

    @staticmethod
    def _ensure_utc(dt):
        """Convert a datetime to UTC. Naive datetimes assumed UTC."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=tzutc())
        return dt.astimezone(tzutc()).replace(tzinfo=tzutc())

    # ------------------------------------------------------------------
    # Calendar discovery
    # ------------------------------------------------------------------

    def discover_calendars(self, caldav_url, username, password):
        """Connect to a CalDAV server and return available calendars.

        Returns list of dicts: {name, path, color (if available)}.
        """
        client = caldav.DAVClient(
            url=caldav_url,
            username=username,
            password=password,
        )
        principal = client.principal()
        calendars = principal.calendars()

        result = []
        for cal in calendars:
            # Try to get color from calendar properties
            color = "#00d4aa"
            try:
                props = cal.get_properties([caldav.dav.DisplayName()])
            except Exception:
                pass
            try:
                # Some servers provide calendar-color
                if hasattr(cal, 'get_property'):
                    raw_color = cal.get_property(
                        caldav.elements.ical.CalendarColor()
                    )
                    if raw_color and raw_color.startswith("#"):
                        color = raw_color[:7]  # strip alpha if present
            except Exception:
                pass

            result.append({
                "name": cal.name or "Unnamed",
                "path": str(cal.url),
                "color": color,
            })

        return result

    def poll_now(self):
        """Trigger an immediate poll (called from API)."""
        threading.Thread(target=self._poll_all, daemon=True,
                         name="caldav-poll-now").start()
