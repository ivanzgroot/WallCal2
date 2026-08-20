"""The wall's clock: one zone, chosen in settings, applied everywhere.

Events are cached as UTC. Three consumers used to strip the Z and compare the
result against a local clock, which in Berlin is an hour out in winter and two
in summer. The symptoms were not subtle — the anticipatory wake fired two
hours early and was dark again by the time the meeting began, and the travel
widget skipped every appointment nearer than the offset while showing ones
three hours away. Recurring events drifted an hour at every clock change.

Everything here is pinned to real dates around a real DST boundary
(Germany 2026-10-25), so it cannot pass by accident on a machine that happens
to sit on UTC.
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tmp = tempfile.mkdtemp()
os.environ["WALLCAL_DB_PATH"] = os.path.join(tmp, "t.db")
os.environ["WALLCAL_RUNTIME_DIR"] = os.path.join(tmp, "run")

from dateutil.tz import gettz, tzutc      # noqa: E402

import database                           # noqa: E402
import localtime                          # noqa: E402
import prewake                            # noqa: E402
import widgets                            # noqa: E402
from caldav_poller import CalDAVPoller    # noqa: E402

database.init_db()
fails = []
BERLIN = gettz("Europe/Berlin")


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


#: Tomorrow, here. The cache query filters against the real clock, so an
#: event pinned to a fixed date drops out of the window as that date passes —
#: a test that passes in the morning and fails after lunch. The times below
#: are fixed; only the day rides along.
DAY = (datetime.now(BERLIN) + timedelta(days=1)).strftime("%Y-%m-%d")


def utc_of(clock, zone="Europe/Berlin"):
    """A local wall-clock time tomorrow, as the cache would store it."""
    at = datetime.fromisoformat(f"{DAY}T{clock}").replace(tzinfo=gettz(zone))
    return at.astimezone(tzutc()).strftime("%Y-%m-%dT%H:%M:%S")


def local_at(clock):
    """The same instant, as an aware local datetime, for use as `now`."""
    return datetime.fromisoformat(f"{DAY}T{clock}").replace(tzinfo=BERLIN)


print("Resolving the zone")
check("a named zone is used", str(localtime.zone("Europe/Berlin")), "Europe/Berlin")
check("nonsense falls back to the machine, never to UTC",
      localtime.zone("Not/AZone") is not None, True)
check("the picker has something to offer", len(localtime.available()) > 100, True)

print("")
print("Reading a cached event")
# 07:00 UTC is 09:00 in Berlin in summer, 08:00 in winter — the same stored
# string has to come back as two different wall clocks.
summer = {"dtstart": "2026-08-19T07:00:00", "all_day": False}
winter = {"dtstart": "2026-12-19T07:00:00", "all_day": False}
berlin = {"timezone": "Europe/Berlin"}
check("summer: +02:00 applied",
      localtime.parse_event_start(summer, berlin).strftime("%H:%M"), "09:00")
check("winter: +01:00 applied",
      localtime.parse_event_start(winter, berlin).strftime("%H:%M"), "08:00")
check("the setting is what decides",
      localtime.parse_event_start(summer, {"timezone": "UTC"}).strftime("%H:%M"), "07:00")
check("and a different one gives a different answer",
      localtime.parse_event_start(summer, {"timezone": "Asia/Tokyo"}).strftime("%H:%M"),
      "16:00")

# An all-day entry is a date, not an instant. Read as one it lands a day early
# for anyone west of Greenwich.
allday = {"dtstart": "2026-08-19T00:00:00", "all_day": True}
check("all-day keeps its date in Berlin",
      localtime.event_day(allday, berlin), "2026-08-19")
check("and in New York", localtime.event_day(allday, {"timezone": "America/New_York"}),
      "2026-08-19")

print("")
print("Anticipatory wake fires a lead time before the event, not an offset")
cal = database.add_calendar("Arbeit", "https://example.invalid/dav")
database.set_many_settings({"prewake_enabled": "true", "prewake_lead_minutes": "35",
                            "timezone": "Europe/Berlin"})
database.cache_events(cal, [{
    "uid": "m1", "summary": "Standup", "dtstart": utc_of("09:00:00"),
    "dtend": utc_of("10:00:00"), "all_day": False, "color": "#00d4aa"}])

settings = database.get_all_settings()
now = local_at("06:00:00")
plan = prewake.next_wake(settings, now)
check("a plan exists", plan is not None, True)
if plan:
    start, end, label = plan
    check("wakes 35 min before the meeting",
          start.astimezone(BERLIN).strftime("%H:%M"), "08:25")
    check("and holds past the start",
          end.astimezone(BERLIN).strftime("%H:%M"), "09:05")
    check("names it", label, "Standup")

# The window used to have closed before the event it was for.
check("still planned five minutes before the meeting",
      prewake.next_wake(settings, local_at("08:55:00")) is not None, True)
check("gone once it is over",
      prewake.next_wake(settings, local_at("09:30:00")) is None, True)

print("")
print("Travel time shows for the appointment you are about to leave for")
database.set_many_settings({"widget_travel": "dynamic", "home_lat": "49.02",
                            "home_lon": "12.10", "travel_window_minutes": "90"})
now = local_at("08:00:00")
for label, at, want in [("in 30 min", "08:30:00", True),
                        ("in 60 min", "09:00:00", True),
                        ("in 3 hours", "11:00:00", False)]:
    database.cache_events(cal, [{
        "uid": "t", "summary": "Termin", "location": "Domplatz 1, Regensburg",
        "dtstart": utc_of(at), "dtend": utc_of(at), "all_day": False,
        "color": "#00d4aa"}])
    settings = database.get_all_settings()
    event = widgets._next_event_with_location(settings, now)
    minutes = None
    if event:
        minutes = (widgets._parse_event_start(event, settings) - now).total_seconds() / 60
    inside = minutes is not None and 0 <= minutes <= 90
    check(f"{label}: inside the 90 min window", inside, want)

print("")
print("Recurring events hold their wall clock across a DST change")


class Obj:
    data = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//wallcal test//
BEGIN:VEVENT
UID:weekly-standup
SUMMARY:Standup
DTSTART;TZID=Europe/Berlin:20261001T090000
DTEND;TZID=Europe/Berlin:20261001T093000
RRULE:FREQ=WEEKLY;BYDAY=TH
END:VEVENT
END:VCALENDAR
"""


occurrences = CalDAVPoller()._parse_event(
    Obj(), {"id": cal, "color": "#00d4aa", "name": "Arbeit"},
    datetime(2026, 10, 1, tzinfo=tzutc()), datetime(2026, 11, 15, tzinfo=tzutc()))
shown = [datetime.fromisoformat(ev["dtstart"]).replace(tzinfo=tzutc())
         .astimezone(BERLIN) for ev in occurrences]
check("several weeks expanded", len(shown) >= 6, True)
check("every one of them reads 09:00",
      sorted({at.strftime("%H:%M") for at in shown}), ["09:00"])
# The offset in the stored value has to move, which is the actual mechanism.
stored = {ev["dtstart"][11:16] for ev in occurrences}
check("by storing two different UTC times", sorted(stored), ["07:00", "08:00"])

print("")
print("A departure window may run past midnight")
# The daemon's quiet hours have always read a wrapping window this way; the
# ÖPNV windows compared them as a plain range instead, so 22:00-02:00 matched
# nothing at all and the last-train board simply never appeared.
NIGHT = "mon=22:00-02:00|tue=|wed=|thu=|fri=|sat=|sun="
DAY = "mon=06:30-09:00,16:00-18:30|tue=|wed=|thu=|fri=|sat=|sun="
for spec, when, want, label in [
        (NIGHT, "2026-08-17T23:30", True, "Monday night, inside it"),
        (NIGHT, "2026-08-18T01:00", True, "carries into Tuesday morning"),
        (NIGHT, "2026-08-18T03:00", False, "and closes there"),
        (NIGHT, "2026-08-17T21:59", False, "not before it opens"),
        (NIGHT, "2026-08-18T23:30", False, "Tuesday night is not configured"),
        (DAY, "2026-08-17T07:00", True, "an ordinary window still matches"),
        (DAY, "2026-08-17T09:00", False, "and still ends"),
        (DAY, "2026-08-18T07:00", False, "and stays on its own weekday")]:
    check(label, widgets._in_any_window(spec, datetime.fromisoformat(when)), want)

print("")
print("ALL PASS" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
