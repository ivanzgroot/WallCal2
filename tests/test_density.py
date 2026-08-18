"""Drive the wall display's density switching for real, in a JS engine.

The layout swap is the one piece of behaviour that only exists in the
browser, so reading it is not enough — this runs it.
"""
import json
import pathlib
import sys

from py_mini_racer import MiniRacer

ROOT = pathlib.Path(__file__).resolve().parent.parent


def build(settings=None, presence=None):
    ctx = MiniRacer()
    ctx.eval((ROOT / "tests" / "domshim.js").read_text(encoding="utf-8"))
    ctx.eval("__routes = " + json.dumps({
        "/api/settings": settings or {},
        "/events": {"events": [], "abfall": None, "last_poll": None},
        "/api/widgets": {"transit": {"visible": False, "departures": []},
                         "weather": {"visible": False}, "travel": {"visible": False},
                         "qr": {"visible": False}, "feeds": []},
        "/api/presence/live": presence or {},
    }) + ";")
    ctx.eval((ROOT / "static" / "js" / "wall.js").read_text(encoding="utf-8"))
    return ctx


def walk(ctx, distances, present=True, step_ms=250):
    """Feed a sequence of distances at the real poll cadence."""
    out = []
    for d in distances:
        ctx.eval("__routes['/api/presence/live'] = " + json.dumps({
            "daemon_running": True, "present": present, "display_on": True,
            "display_mode": "normal", "brightness": 100,
            "brightness_source": "css", "distance_cm": d,
        }) + ";")
        ctx.eval("__tick(%d);" % step_ms)
        out.append((d, ctx.eval("__density()")))
    return out


SETTINGS = {
    "density_mode": "auto", "density_near_cm": "100", "density_far_cm": "140",
    "density_min_band_cm": "80", "density_debounce_ms": "1500",
    "density_enter_ms": "250",
    "sensor_distance_max_cm": "300", "display_off_strategy": "hdmi",
    "near_view": "fortnight", "locale": "de-DE", "timezone": "Europe/Berlin",
    "drift_enabled": "true", "crossfade_ms": "400", "theme": "dark",
    "poll_interval_minutes": "5",
}

fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got}" + ("" if ok else f"  (want {want})"))
    if not ok:
        fails.append(label)


print("A. approach from across the room, then walk up")
ctx = build(SETTINGS)
seq = walk(ctx, [250, 250, 250, 250, 250, 250, 90, 90, 90, 90, 90])
print("     ", seq)
check("far while distant", seq[5][1], "far")
check("near once close", seq[-1][1], "near")

print("\nB. hysteresis — drifting around the boundary must not oscillate")
ctx = build(SETTINGS)
walk(ctx, [250] * 6)
seq = walk(ctx, [95, 105, 95, 110, 120, 130, 118, 125, 132, 99, 121])
print("     ", seq)
settled = [x[1] for x in seq[4:]]
check("commits to near despite jitter", seq[-1][1], "near")
check("no oscillation once settled", set(settled), {"near"})

print("")
print("B2. crossing the exit threshold does switch back")
ctx = build(SETTINGS)
walk(ctx, [80] * 6)
seq = walk(ctx, [150, 155, 160, 152, 158, 151, 156, 149, 160])
print("     ", seq)
check("far again above 140", seq[-1][1], "far")

print("\nC. nobody there — the sensor stops reporting a target")
ctx = build(SETTINGS)
walk(ctx, [80] * 6)
check("near while present", ctx.eval("__density()"), "near")
seq = walk(ctx, [0, 0, 0, 0, 0, 0, 0, 0], present=False)
print("     ", seq)
check("returns to far when the room empties", seq[-1][1], "far")

print("\nD. density_mode=off pins one layout")
ctx = build(dict(SETTINGS, density_mode="off"))
seq = walk(ctx, [250, 250, 250, 250, 80, 80])
print("     ", seq)
check("never switches", set(x[1] for x in seq), {"near"})

print("\nE. no distance source (gpio/none sensor mode)")
ctx = build(SETTINGS)
seq = walk(ctx, [None] * 6)
print("     ", seq)
check("falls back to near", seq[-1][1], "near")

def latency(ctx, distances, want, step_ms=250):
    """Poll frames until the layout commits. Returns milliseconds."""
    for i, d in enumerate(distances):
        ctx.eval("__routes['/api/presence/live'] = " + json.dumps({
            "daemon_running": True, "present": True, "display_on": True,
            "display_mode": "normal", "brightness": 100,
            "brightness_source": "css", "distance_cm": d}) + ";")
        ctx.eval("__tick(%d);" % step_ms)
        if ctx.eval("__density()") == want:
            return (i + 1) * step_ms
    return None


print("")
print("F. how long the switch actually takes")
ctx = build(SETTINGS)
walk(ctx, [250] * 8)
approach = latency(ctx, [80] * 20, "near")
print("      far -> near: %s ms of polling" % approach)
check("approaching commits within 500 ms", approach is not None and approach <= 500, True)

ctx = build(SETTINGS)
walk(ctx, [80] * 12)
leaving = latency(ctx, [200] * 20, "far")
print("      near -> far: %s ms of polling" % leaving)
check("leaving still waits out the jitter", leaving is not None and leaving >= 1500, True)

print("")
print("G. a single bogus frame must not flip the layout")
ctx = build(SETTINGS)
walk(ctx, [250] * 8)
seq = walk(ctx, [80, 250, 250, 250, 250])
print("     ", seq)
check("one stray close reading is ignored", seq[-1][1], "far")

print("")
print("ALL PASS" if not fails else "%d FAILED: %s" % (len(fails), fails))
sys.exit(1 if fails else 0)
