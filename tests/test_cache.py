"""The event cache is only rewritten when the calendar actually changed.

A DELETE plus N INSERTs on every poll is 288 rewrites a day onto an SD card
for a calendar that usually comes back identical — the same wear the tmpfs
IPC exists to avoid. Skipping the write has one trap: the wall reads
"when did we last poll" out of this table, and a wall that thinks the poller
died lights its staleness dot.
"""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tmp = tempfile.mkdtemp()
os.environ["WALLCAL_DB_PATH"] = os.path.join(tmp, "t.db")
os.environ["WALLCAL_RUNTIME_DIR"] = os.path.join(tmp, "run")

import database                      # noqa: E402

database.init_db()
fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


cal_id = database.add_calendar("Familie", "https://example.invalid/dav/")

EVENTS = [
    {"uid": "a", "summary": "Zahnarzt", "dtstart": "2026-08-19T09:00:00",
     "dtend": "2026-08-19T10:00:00", "all_day": False, "color": "#00d4aa"},
    {"uid": "b", "summary": "Urlaub", "dtstart": "2026-08-21",
     "dtend": "2026-08-28", "all_day": True, "color": "#00d4aa"},
]


def stored():
    with database.get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM events_cache WHERE calendar_id = ?",
            (cal_id,)).fetchone()["c"]


def written_at():
    with database.get_db() as conn:
        return conn.execute(
            "SELECT MAX(cached_at) t FROM events_cache WHERE calendar_id = ?",
            (cal_id,)).fetchone()["t"]


print("Writing only when something moved")
check("first poll writes", database.cache_events(cal_id, EVENTS), True)
check("rows stored", stored(), 2)

check("an identical poll does not", database.cache_events(cal_id, EVENTS), False)
check("rows untouched", stored(), 2)

# The order a CalDAV server returns objects in is not guaranteed and means
# nothing — reordering must not read as a change.
check("reordering is not a change",
      database.cache_events(cal_id, list(reversed(EVENTS))), False)

# The write is an INSERT OR REPLACE, so a repeated uid collapses to one row.
# A fingerprint that counted both would never compare equal to what it wrote.
check("a repeated uid collapses like the insert does",
      database.cache_events(cal_id, EVENTS + [EVENTS[0]]), False)

changed = [dict(EVENTS[0], summary="Zahnarzt (verschoben)"), EVENTS[1]]
check("an edited summary writes", database.cache_events(cal_id, changed), True)
check("still two rows", stored(), 2)

check("a new event writes", database.cache_events(
    cal_id, changed + [{"uid": "c", "summary": "Elternabend",
                        "dtstart": "2026-08-22T19:00:00",
                        "dtend": "2026-08-22T20:30:00", "all_day": False,
                        "color": "#00d4aa"}]), True)
check("three rows", stored(), 3)

check("a removal writes", database.cache_events(cal_id, changed), True)
check("back to two", stored(), 2)

check("dropping everything writes", database.cache_events(cal_id, []), True)
check("empty", stored(), 0)
check("and staying empty does not", database.cache_events(cal_id, []), False)

print("")
print("The wall can still tell a quiet calendar from a dead poller")
database.cache_events(cal_id, EVENTS)
first = database.get_last_poll_time()
check("a poll time is available", bool(first), True)

database.record_poll_time()
recorded = database.get_last_poll_time()
check("recording a cycle sets it", bool(recorded), True)

# The point of the whole exercise: a poll that changed nothing still counts
# as a poll, so the staleness dot stays dark on a calendar nobody edited.
with database.get_db() as conn:
    conn.execute("UPDATE events_cache SET cached_at = '2020-01-01 00:00:00' "
                 "WHERE calendar_id = ?", (cal_id,))
check("the row timestamps are old now", written_at(), "2020-01-01 00:00:00")
check("but the poll time is not", database.get_last_poll_time() != written_at(), True)
check("unchanged poll still writes nothing",
      database.cache_events(cal_id, EVENTS), False)

print("")
print("ALL PASS" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
