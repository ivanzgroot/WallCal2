"""
Anticipatory wake: light the panel shortly before an event starts.

The daemon has no calendar access and deliberately no database dependency
beyond its own settings, so the web app computes the next wake window and
publishes it through /run/wallcal — the same intent-publishing boundary
everything else uses.

Recomputed on a timer rather than on every request: the answer changes when
the calendar changes or a window elapses, not when somebody loads a page.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

import database
from presence import runtime

logger = logging.getLogger("wallcal.prewake")

RECOMPUTE_INTERVAL = 60.0


def _int(value, default):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _parse_start(ev):
    try:
        return datetime.fromisoformat(str(ev.get("dtstart", "")).replace("Z", ""))
    except (ValueError, AttributeError):
        return None


def _hhmm(text, fallback=(7, 0)):
    try:
        h, _, m = str(text).partition(":")
        return int(h) % 24, int(m or 0) % 60
    except (TypeError, ValueError):
        return fallback


def next_wake(settings=None, now=None):
    """The next (start, until, label) wake window, or None.

    All-day events have no meaningful start time. Left alone they would fire
    a wake at 00:00, so they are excluded by default; when included they use
    a configured time of day instead.
    """
    settings = settings or database.get_all_settings()
    if not _bool(settings.get("prewake_enabled")):
        return None

    now = now or datetime.now()
    lead = timedelta(minutes=_int(settings.get("prewake_lead_minutes"), 35))
    hold = timedelta(minutes=_int(settings.get("prewake_hold_minutes"), 5))
    timed_only = _bool(settings.get("prewake_timed_only"), True)
    allday_h, allday_m = _hhmm(settings.get("prewake_allday_at"), (7, 0))

    wanted = {c.strip() for c in str(settings.get("prewake_calendars") or "").split(",")
              if c.strip()}
    abfall = (settings.get("abfall_calendar_id") or "").strip()

    events = database.get_cached_events(
        days=3, exclude_calendar_ids=[abfall] if abfall else None)

    best = None
    for ev in events:
        if wanted and str(ev.get("calendar_id")) not in wanted:
            continue

        start = _parse_start(ev)
        if start is None:
            continue

        if ev.get("all_day"):
            if timed_only:
                continue
            start = start.replace(hour=allday_h, minute=allday_m,
                                  second=0, microsecond=0)

        window_start = start - lead
        window_end = start + hold
        if window_end <= now:
            continue                      # already gone by
        if best is None or window_start < best[0]:
            best = (window_start, window_end, ev.get("summary", ""))

    return best


def publish(settings=None):
    """Compute and publish the next wake window. Returns what was published."""
    try:
        plan = next_wake(settings)
    except Exception as exc:
        logger.warning("Could not compute the wake plan: %s", exc)
        return None

    if plan is None:
        runtime.clear_wake_plan()
        return None
    start, end, label = plan
    runtime.set_wake_plan(start.timestamp(), end.timestamp(), label)
    return {"from": start.isoformat(timespec="minutes"),
            "until": end.isoformat(timespec="minutes"), "label": label}


class PrewakeScheduler:
    """Keeps the published wake plan in step with the calendar."""

    def __init__(self, interval=RECOMPUTE_INTERVAL):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.last = None

    def start(self):
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="prewake")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def poke(self):
        """Recompute now — after a settings change or a fresh CalDAV poll."""
        self.last = publish()
        return self.last

    def _run(self):
        while not self._stop.is_set():
            try:
                self.last = publish()
            except Exception:
                logger.exception("prewake loop")
            self._stop.wait(self.interval)
