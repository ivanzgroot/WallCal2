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
                "code": 63, "is_day": True, "feels_like": 20,
                "hourly": [{"at": "13", "temp": 18, "rain": 5, "code": 2, "is_day": True},
                           {"at": "14", "temp": 19, "rain": 20, "code": 3, "is_day": True},
                           {"at": "15", "temp": 18, "rain": 75, "code": 63, "is_day": True},
                           {"at": "16", "temp": 17, "rain": 60, "code": 61, "is_day": True},
                           {"at": "17", "temp": 16, "rain": 30, "code": 3, "is_day": True}],
                "tomorrow": {"min": 12, "max": 21, "rain": 55, "code": 0}},
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
cells = ctx.eval(
    "(function(){var n=document.getElementById('nearWeather');"
    "var c=n.children.filter(function(x){return String(x.className).indexOf('wx-hours')>=0;})[0];"
    "return c ? c.children.length : 0;})()")
check("five hours shown", cells, 5)
icons = ctx.eval(
    "(function(){var n=document.getElementById('nearWeather');"
    "var c=n.children.filter(function(x){return String(x.className).indexOf('wx-hours')>=0;})[0];"
    "return c.children.map(function(cell){"
    "  var svg=cell.children.filter(function(x){return x.tagName==='SVG';})[0];"
    "  return svg ? svg.children[0].getAttribute('href') : '?';}).join(',');})()")
print("      Stundensymbole:", icons)
check("rainy hour gets the rain symbol", icons.split(",")[2], "#i-rain")
check("dry hour does not", icons.split(",")[0] in ("#i-partly", "#i-sun", "#i-cloud"), True)
wet = ctx.eval(
    "(function(){var n=document.getElementById('nearWeather');"
    "var c=n.children.filter(function(x){return String(x.className).indexOf('wx-hours')>=0;})[0];"
    "return c.children.filter(function(x){return String(x.className).indexOf('wet')>=0;}).length;})()")
check("wet hours flagged", wet, 2)

print("")
print("Nothing to say — the widget withdraws")
ctx.eval("__routes['/api/widgets'] = " + json.dumps(
    {"transit": {"visible": False}, "weather": {"visible": False},
     "travel": {"visible": False}, "qr": {"visible": False}, "feeds": []}) + ";")
ctx.eval("__tick(30000);")
check("board hidden", hidden("transitBoard"), True)
check("weather hidden", hidden("nearWeather"), True)

print("")


print("")
print("Weather symbols follow the WMO code")


def symbol_for(code, is_day=True):
    """Render a payload with this code and read back the symbol used."""
    ctx.eval("__routes['/api/widgets'] = " + json.dumps(dict(
        WIDGETS, weather=dict(WIDGETS["weather"], code=code, is_day=is_day))) + ";")
    ctx.eval("__tick(30000);")
    return ctx.eval(
        "(function(){var n=document.getElementById('nearWeather');"
        "var now=n.children[0];"
        "var svg=now.children.filter(function(x){return x.tagName==='SVG';})[0];"
        "return svg ? svg.children[0].getAttribute('href') : '?';})()")


for code, day, want in [(0, True, "#i-sun"), (0, False, "#i-moon"),
                        (2, True, "#i-partly"), (2, False, "#i-partly-night"),
                        (3, True, "#i-cloud"), (45, True, "#i-fog"),
                        (53, True, "#i-drizzle"), (63, True, "#i-rain"),
                        (73, True, "#i-snow"), (81, True, "#i-rain"),
                        (95, True, "#i-storm"), (None, True, "#i-cloud")]:
    check("code %s%s" % (code, "" if day else " nachts"), symbol_for(code, day), want)

print("")
print("Cards reflow instead of leaving a hole")
# Everything visible, then transit withdraws: the remaining cards must stay
# in the flow and grow, which means they must not be display:none.
ctx.eval("__routes['/api/widgets'] = " + json.dumps(WIDGETS) + ";")
ctx.eval("__tick(30000);")
check("transit card present", hidden("transitBoard"), False)
ctx.eval("__routes['/api/widgets'] = " + json.dumps(
    dict(WIDGETS, transit={"visible": False, "departures": []})) + ";")
ctx.eval("__tick(30000);")
check("transit card withdrawn", hidden("transitBoard"), True)
check("weather still there to take the space", hidden("nearWeather"), False)

css = (ROOT / "static" / "css" / "wall.css").read_text(encoding="utf-8")
check("hidden cards stay in the flow", "display: flex;" in
      css.split(".box[hidden] {")[1].split("}")[0], True)
check("cards grow into free space", "flex: 1 1 auto" in css, True)
check("collapse is animated", "max-height var(--reflow)" in css, True)
check("margin collapses too, not just height",
      "margin-bottom: 0;" in css.split(".box[hidden] {")[1].split("}")[0], True)

print("")
print("Abfall is the loud one")
check("uses the fraction colour", "--fraction" in css, True)
check("has its own card style", ".box.abfall" in css, True)

print("")


print("")
print("QR reacts to the density, not to the widget poll")
ctx.eval("__routes['/api/widgets'] = " + json.dumps(dict(
    WIDGETS, qr={"visible": True, "mode": "dynamic", "size": 96})) + ";")
ctx.eval("document.getElementById('app').setAttribute('data-density','far');")
ctx.eval("__tick(30000);")
check("hidden while far away", hidden("qrBox"), True)

# The image has to exist before it is needed: building it lazily meant the
# first appearance also paid for a request and a Reed-Solomon encode.
built = ctx.eval("document.getElementById('qrBox').children.length")
check("image already built while hidden", built, 1)
check("and points at the endpoint", ctx.eval(
    "document.getElementById('qrBox').children[0].src"), "/api/qr.svg?size=96")

# No widget poll in between — only the density changes.
ctx.eval("__requests.length = 0;")
ctx.eval("(function(){ __setDensityProbe(); })();")
check("visible immediately on walking up", hidden("qrBox"), False)
check("without waiting for a widget fetch", ctx.eval(
    "__requests.filter(function(u){return u.indexOf('/api/widgets')===0;}).length"), 0)

ctx.eval("__routes['/api/widgets'] = " + json.dumps(dict(
    WIDGETS, qr={"visible": True, "mode": "always", "size": 96})) + ";")
ctx.eval("__tick(30000);")
ctx.eval("document.getElementById('app').setAttribute('data-density','far');")
ctx.eval("(function(){ __setDensityProbe('far'); })();")
check("always mode ignores the density", hidden("qrBox"), False)

print("")
print("ALL PASS" if not fails else "%d FAILED: %s" % (len(fails), fails))
sys.exit(1 if fails else 0)
