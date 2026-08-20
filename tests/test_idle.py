"""The wall stops working behind a dark panel.

Every timer here is invisible work while the panel is off: a clock nobody
reads, a 1920px grid nobody sees, and two XHRs a minute on a Pi that would
rather be idle. The panel is dark most of the day, so this is most of the
wall's lifetime.

The live stream is the exception — it is how the wall learns the panel came
back, so it is never stopped.
"""
import json
import pathlib
import sys

from py_mini_racer import MiniRacer

ROOT = pathlib.Path(__file__).resolve().parent.parent
fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


WIDGETS = {
    "transit": {"visible": True, "station": "Regensburg Hbf", "departures": [
        {"line": "S1", "direction": "Hauptbahnhof", "when": "in 4 min", "minutes": 4,
         "delay": 0, "cancelled": False, "mode": "SUBURBAN"}]},
    "weather": {"visible": False}, "travel": {"visible": False},
    "qr": {"visible": False}, "feeds": [],
}
EVENTS = {"events": [{"uid": "a", "summary": "Zahnarzt", "dtstart": "2026-08-19T09:00:00",
                      "dtend": "2026-08-19T10:00:00", "all_day": False,
                      "color": "#00d4aa", "calendar_id": 1}],
          "abfall": None, "last_poll": "2026-08-19 08:00:00",
          "generated_at": "2026-08-19T08:00:00+00:00"}

ctx = MiniRacer()
ctx.eval((ROOT / "tests" / "domshim.js").read_text(encoding="utf-8"))
ctx.eval("__routes = " + json.dumps({
    "/api/settings": {"theme": "dark", "density_mode": "auto"},
    "/events": EVENTS,
    "/api/widgets": WIDGETS,
    "/api/presence/live": {"daemon_running": True, "density": "near",
                           "display_on": True, "display_mode": "normal",
                           "brightness": 100, "brightness_source": "css",
                           "screensaver": {}},
}) + ";")
ctx.eval((ROOT / "static" / "js" / "wall.js").read_text(encoding="utf-8"))


def periods():
    """The periods of every timer still live, sorted."""
    return sorted(ctx.eval(
        "__liveTimers().map(function(t){return t.ms;}).join(',')").split(","))


def requests():
    return ctx.eval("__requests.length")


def writes():
    return ctx.eval("localStorage._writes")


def set_live(**fields):
    base = {"daemon_running": True, "density": "near", "display_mode": "normal",
            "brightness": 100, "brightness_source": "css", "screensaver": {}}
    base.update(fields)
    ctx.eval("__routes['/api/presence/live'] = " + json.dumps(base) + ";")
    ctx.eval("__tick(2000);")          # the live-state poll


print("Awake: the wall is doing its job")
awake = periods()
print("      live timer periods:", awake)
check("clock runs", "1000" in awake, True)
check("settings re-read", "20000" in awake, True)
check("events polled", "300000" in awake, True)
check("widgets polled", "30000" in awake, True)
check("sync status checked", "60000" in awake, True)
check("live state watched", "2000" in awake, True)

print("")
print("Dark: everything but the stream stands down")
set_live(display_on=False)
dark = periods()
print("      live timer periods:", dark)
check("only the live stream is left", dark, ["2000"])

before = requests()
ctx.eval("__tick(1000); __tick(30000); __tick(300000); __tick(60000);")
check("no work is done behind a dark panel", requests(), before)

print("")
print("Walking up: it catches up rather than waiting for the next tick")
before = requests()
set_live(display_on=True)
check("timers are back", sorted(periods()), sorted(awake))
check("fetched immediately, not on the next 30 s poll", requests() > before, True)

print("")
print("A dead daemon must not leave the wall frozen")
set_live(display_on=False)
check("stopped", periods(), ["2000"])
ctx.eval("__routes['/api/presence/live'] = " + json.dumps(
    {"daemon_running": False, "density": "far"}) + ";")
ctx.eval("__tick(2000);")
check("nothing is driving the panel, so the wall drives itself",
      sorted(periods()), sorted(awake))

print("")
print("The minute re-render cannot be stepped over")
# The old check was `getSeconds() === 0` on a drifting 1 s interval: one late
# tick steps 59 -> 1 and the fortnight keeps yesterday's date until the next
# poll repaints it. This tick lands at an arbitrary second, so the old code
# would have skipped it 59 times out of 60.
set_live(display_on=True)
ctx.eval("document.getElementById('transitBoard').children = [];")
minute_before = ctx.eval("new Date().getMinutes()")
ctx.eval("__tick(1000);")
check("renders on the first tick, whatever second it is",
      ctx.eval("document.getElementById('transitBoard').children.length") > 0, True)

ctx.eval("document.getElementById('transitBoard').children = [];")
ctx.eval("__tick(1000);")
if ctx.eval("new Date().getMinutes()") == minute_before:
    check("and not again within the same minute",
          ctx.eval("document.getElementById('transitBoard').children.length"), 0)
else:
    print("  SKIP  same-minute check: the clock rolled over mid-test")

print("")
print("The calendar is not rewritten to flash on every poll")
before = writes()
ctx.eval("__tick(300000);")            # another /events poll, same payload
check("an unchanged calendar writes nothing", writes(), before)

changed = dict(EVENTS)
changed["events"] = EVENTS["events"] + [
    {"uid": "b", "summary": "Elternabend", "dtstart": "2026-08-20T19:00:00",
     "dtend": "2026-08-20T20:30:00", "all_day": False, "color": "#00d4aa",
     "calendar_id": 1}]
changed["generated_at"] = "2026-08-19T08:05:00+00:00"
ctx.eval("__routes['/events'] = " + json.dumps(changed) + ";")
ctx.eval("__tick(300000);")
check("a changed calendar still writes", writes() > before, True)

before = writes()
ctx.eval("__routes['/events'] = " + json.dumps(
    dict(changed, generated_at="2026-08-19T08:10:00+00:00")) + ";")
ctx.eval("__tick(300000);")
check("a new generated_at alone is not a change", writes(), before)

print("")
print("The staleness check runs instead of throwing")
# num() was called here and defined nowhere: updateSyncStatus raised a
# ReferenceError every sixty seconds, so a wall quietly serving week-old data
# looked exactly like a healthy one.
ctx.eval("__routes['/events'] = " + json.dumps(
    dict(changed, last_poll="2025-08-12 10:00:00")) + ";")
ctx.eval("__tick(300000);")            # take the stale last_poll
ctx.eval("__tick(60000);")             # updateSyncStatus
check("a wall serving old data says so",
      ctx.eval("document.getElementById('app').getAttribute('data-stale')"), "1")

ctx.eval("__routes['/events'] = " + json.dumps(
    dict(changed, last_poll="2025-08-12 12:39:00")) + ";")
ctx.eval("__tick(300000);")
ctx.eval("__tick(60000);")
check("a healthy one does not",
      ctx.eval("document.getElementById('app').getAttribute('data-stale')"), "0")

print("")
print("Changing the view actually changes the view")
# near_view reached the wall once at load and never again, so switching
# between Monat, zwei Wochen and Liste in settings did nothing at all until
# the kiosk browser was restarted. Nothing here could see that: the suites
# checked the timers, not what got drawn.


def view(name):
    """Load the wall with a near_view and report what calGrid became."""
    ctx = MiniRacer()
    ctx.eval((ROOT / "tests" / "domshim.js").read_text(encoding="utf-8"))
    ctx.eval("__routes = " + json.dumps({
        "/api/settings": {"theme": "dark", "near_view": name},
        "/events": EVENTS, "/api/widgets": WIDGETS,
        "/api/presence/live": {"daemon_running": True, "density": "near",
                               "display_on": True, "display_mode": "normal",
                               "brightness": 100, "brightness_source": "css",
                               "screensaver": {}},
    }) + ";")
    ctx.eval((ROOT / "static" / "js" / "wall.js").read_text(encoding="utf-8"))
    return ctx, ctx.eval("document.getElementById('calGrid').className"), \
        ctx.eval("document.getElementById('calGrid').children.length")


_, fortnight_cls, fortnight_cells = view("fortnight")
_, month_cls, month_cells = view("month")
_, agenda_cls, agenda_cells = view("agenda")
print(f"      fortnight: {fortnight_cls!r} {fortnight_cells} cells")
print(f"      month:     {month_cls!r} {month_cells} cells")
print(f"      agenda:    {agenda_cls!r} {agenda_cells} rows")
check("fortnight lays out two rows", "rows-2" in fortnight_cls, True)
check("and draws a fortnight of them", fortnight_cells, 14)
check("month lays out six", "rows-6" in month_cls, True)
check("and draws more than a fortnight", month_cells > 14, True)
check("agenda is neither", "rows-" not in agenda_cls, True)

# The live path: change the setting on the server and let the wall notice,
# without reloading it.
ctx, _, _ = view("fortnight")
ctx.eval("__routes['/api/settings'] = " + json.dumps(
    {"theme": "dark", "near_view": "month"}) + ";")
ctx.eval("__tick(20000);")          # the settings re-read
ctx.eval("__tick(1000);")           # ...and the clock that runs beside it
check("a changed setting is picked up without a restart",
      "rows-6" in ctx.eval("document.getElementById('calGrid').className"), True)

print("")
print("ALL PASS" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
