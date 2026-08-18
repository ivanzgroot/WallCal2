"""Run the settings page's JavaScript for real.

Catches the class of bug that killed calibration: a handler that fires,
fails, and says nothing.
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


def build(routes=None, status=None):
    ctx = MiniRacer()
    ctx.eval((ROOT / "tests" / "domshim.js").read_text(encoding="utf-8"))
    base = {
        "/api/settings": {"density_mode": "auto", "theme": "dark",
                          "sensor_distance_max_cm": "300", "density_near_cm": "100",
                          "density_far_cm": "140", "widget_transit": "dynamic"},
        "/api/calendars": {"calendars": []},
        "/api/system": {}, "/api/status": {"feeds": []},
        "/api/prewake": {"next": None},
        "/api/presence/live": {"daemon_running": True, "distance_cm": 120},
        "/api/sensor/calibrate/apply": {"ok": True,
            "updated": {"sensor_distance_max_cm": "180"}},
    }
    base.update(routes or {})
    ctx.eval("__routes = " + json.dumps(base) + ";")
    if status:
        ctx.eval("__status = " + json.dumps(status) + ";")
    ctx.eval((ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8"))
    return ctx


def click(ctx, node_id):
    """Fire the click handler registered on a node."""
    return ctx.eval(f"(function(){{ var h=__handlers['{node_id}']; "
                    f"if(!h) return 'NO HANDLER'; h.forEach(function(f){{f.call("
                    f"document.getElementById('{node_id}'), {{preventDefault:function(){{}}}});}}); "
                    f"return 'fired'; }})()")


def text(ctx, node_id):
    return ctx.eval(f"(function(){{var n=document.getElementById('{node_id}');"
                    f"return n.children.map(function(c){{"
                    f"return (c.children||[]).map(function(g){{return g.textContent||'';}}).join(' ')"
                    f" + ' ' + (c.textContent||'');}}).join(' | ');}})()")


print("Calibration apply")
ctx = build()
check("button has a handler", click(ctx, "calibApply"), "fired")
check("success is reported", "Übernommen" in text(ctx, "calibError"), True)
check("says what changed", "180" in text(ctx, "calibError"), True)

print("")
print("Calibration apply — server refuses (e.g. read-only database)")
ctx = build(status={"/api/sensor/calibrate/apply": 500},
            routes={"/api/sensor/calibrate/apply":
                    {"error": "Die Datenbank ist schreibgeschützt. Auf dem Pi: ./wallcal.sh doctor --fix"}})
click(ctx, "calibApply")
body = text(ctx, "calibError")
check("failure is reported", "Konnte nicht übernommen werden" in body, True)
check("says what to do", "doctor --fix" in body, True)

print("")
print("Widget config hides while the widget is off")
ctx = build(routes={"/api/settings": {"widget_transit": "off", "theme": "dark"}})
check("panel hidden", ctx.eval(
    "(function(){var n=0;__detailsNodes.forEach(function(d){if(d.getAttribute('data-needs')"
    "==='widget_transit'&&d.hidden)n++;});return n;})()"), 1)

print("")
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
