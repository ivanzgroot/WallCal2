"""Render the widgets for real against realistic payloads."""
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
         "delay": 0, "cancelled": False, "mode": "SUBURBAN"},
        {"line": "6", "direction": "Burgweinting", "when": "in 11 min", "minutes": 11,
         "delay": 3, "cancelled": False, "mode": "BUS"},
        {"line": "U2", "direction": "Garching", "when": "15:42", "minutes": 34,
         "delay": 0, "cancelled": True, "mode": "SUBWAY"}]},
    "weather": {"visible": True, "temperature": 18, "units": "°",
                "headline": "Regen ab 15:00", "place": "Regensburg",
                "hourly": [{"at": "13", "temp": 18, "rain": 5},
                           {"at": "14", "temp": 19, "rain": 20},
                           {"at": "15", "temp": 18, "rain": 75},
                           {"at": "16", "temp": 17, "rain": 60},
                           {"at": "17", "temp": 16, "rain": 30}],
                "tomorrow": {"min": 12, "max": 21, "rain": 55}},
    "travel": {"visible": False}, "qr": {"visible": False}, "feeds": [],
}

ctx = MiniRacer()
ctx.eval((ROOT / "tests" / "domshim.js").read_text(encoding="utf-8"))
ctx.eval("__routes = " + json.dumps({
    "/api/settings": {"theme": "dark", "density_mode": "auto"},
    "/events": {"events": [], "abfall": None},
    "/api/widgets": WIDGETS,
    "/api/presence/live": {"daemon_running": True, "density": "near"},
}) + ";")
ctx.eval((ROOT / "static" / "js" / "wall.js").read_text(encoding="utf-8"))


def dump(node_id):
    return ctx.eval(
        "(function(){var n=document.getElementById('%s');"
        "function walk(x){return (x.children||[]).map(function(c){"
        "return (c.textContent||'') + ' ' + walk(c);}).join(' ');}"
        "return (n.textContent||'') + ' ' + walk(n);})()" % node_id)


def hidden(node_id):
    return ctx.eval("document.getElementById('%s').hidden" % node_id)


print("Departure board")
check("board is shown", hidden("transitBoard"), False)
board = dump("transitBoard")
check("station named", "Regensburg Hbf" in board, True)
check("lines listed", "S1" in board and "U2" in board, True)
check("relative time kept", "in 4 min" in board, True)
check("delay marked", "+3" in board, True)
check("cancellation said plainly", "fällt aus" in board, True)
check("imminent departure flagged", ctx.eval(
    "(function(){var n=document.getElementById('transitBoard');"
    "return n.children.filter(function(c){return (c._cls&&0)||"
    "String(c.className||'').indexOf('soon')>=0;}).length;})()"), 1)
check("S-Bahn badge coloured", ctx.eval(
    "(function(){var n=document.getElementById('transitBoard');"
    "var r=n.children[1]; return r.children[0].style.background;})()"), "#3B8A3F")

print("")
print("Weather")
check("panel is shown", hidden("nearWeather"), False)
wx = dump("nearWeather")
check("temperature shown", "18°" in wx, True)
check("headline shown", "Regen ab 15:00" in wx, True)
check("tomorrow shown", "Morgen 12° bis 21°" in wx, True)
check("rain flagged for tomorrow", "Regen" in wx, True)
bars = ctx.eval(
    "(function(){var n=document.getElementById('nearWeather');"
    "var c=n.children.filter(function(x){return String(x.className).indexOf('wx-chart')>=0;})[0];"
    "return c.children.map(function(col){return col.children[0].style.height;}).join(',');})()"
).split(",")
print("      bar heights:", bars)
check("dry hours keep a visible baseline", bars[0], "6%")
check("wet hour scales with probability", bars[2], "75%")

print("")
print("Nothing to say — the widget withdraws")
ctx.eval("__routes['/api/widgets'] = " + json.dumps(
    {"transit": {"visible": False}, "weather": {"visible": False},
     "travel": {"visible": False}, "qr": {"visible": False}, "feeds": []}) + ";")
ctx.eval("__tick(30000);")
check("board hidden", hidden("transitBoard"), True)
check("weather hidden", hidden("nearWeather"), True)

print("")
print("ALL PASS" if not fails else "%d FAILED: %s" % (len(fails), fails))
sys.exit(1 if fails else 0)
