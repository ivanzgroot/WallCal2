"""Density switching, now decided in the daemon.

The logic moved out of the browser, so this drives the real daemon object
with a synthetic clock instead of a JS engine.
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

import database                       # noqa: E402
database.init_db()
import presence.daemon as pd          # noqa: E402

fails = []
SETTINGS = {
    "display_off_strategy": "css", "density_mode": "auto",
    "density_near_cm": "100", "density_far_cm": "140",
    "density_min_band_cm": "80", "density_enter_ms": "250",
    "density_debounce_ms": "1500", "sensor_distance_max_cm": "300",
}


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


class Clock:
    """Replaces time.monotonic so frames can be fed at an exact cadence."""
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, ms): self.t += ms / 1000.0


def daemon(**overrides):
    database.set_many_settings(dict(SETTINGS, **overrides))
    d = pd.PresenceDaemon()
    d.settings = pd.Settings.load()
    d._write_state = lambda **kw: None      # no IPC in the test
    return d


def feed(d, clock, distances, present=True, step_ms=100):
    """One radar frame per step, at the sensor's real ~10 Hz."""
    out = []
    for cm in distances:
        # A radar-shaped reading: no target when the distance is zero, which
        # is what an empty room actually looks like on the wire.
        d._last_reading = {"distance_cm": cm, "source": "uart",
                           "target_state": 1 if cm else 0}
        d._present = present and bool(cm)
        clock.advance(step_ms)
        d._update_density()
        out.append((cm, d._density))
    return out


def commit_ms(d, clock, distances, want, step_ms=100):
    for i, cm in enumerate(distances):
        d._last_reading = {"distance_cm": cm, "source": "uart",
                           "target_state": 1 if cm else 0}
        d._present = bool(cm)
        clock.advance(step_ms)
        d._update_density()
        if d._density == want:
            return (i + 1) * step_ms
    return None


clock = Clock()
pd.time.monotonic = clock

print("A. walking up, then away")
d = daemon()
feed(d, clock, [250] * 25)
check("far while distant", d._density, "far")
feed(d, clock, [80] * 8)
check("near once close", d._density, "near")

print("")
print("B. hysteresis holds between the thresholds")
d = daemon()
feed(d, clock, [80] * 8)
seq = feed(d, clock, [105, 120, 135, 118, 99, 130, 125])
print("     ", seq)
check("no oscillation in the gap", set(x[1] for x in seq), {"near"})

print("")
print("C. an empty room resolves to far")
d = daemon()
feed(d, clock, [80] * 8)
check("near while present", d._density, "near")
feed(d, clock, [0] * 25, present=False)
check("far once nobody is there", d._density, "far")

print("")
print("D. how long it takes")
d = daemon()
feed(d, clock, [250] * 25)
approach = commit_ms(d, clock, [80] * 40, "near")
print(f"      far -> near: {approach} ms")
check("arriving commits within 400 ms", approach is not None and approach <= 400, True)

d = daemon()
feed(d, clock, [80] * 8)
leaving = commit_ms(d, clock, [220] * 40, "far")
print(f"      near -> far: {leaving} ms")
check("leaving waits out the jitter", leaving is not None and leaving >= 1500, True)

print("")
print("E. a single stray frame changes nothing")
d = daemon()
feed(d, clock, [250] * 25)
seq = feed(d, clock, [80, 250, 250, 250])
print("     ", seq)
check("one close reading ignored", seq[-1][1], "far")

print("")
print("F. modes")
# "off" pins the near layout — the one that makes sense standing in front
# of the panel — and stays there whatever the distance does.
d = daemon(density_mode="off")
seq = feed(d, clock, [250, 80, 300, 50, 400, 90])
check("off pins near", set(x[1] for x in seq), {"near"})
d = daemon()
d._last_reading = {"distance_cm": None}
check("no distance source disables auto", d._density_enabled(), False)
d = daemon(density_near_cm="260")
check("band too narrow disables auto", d._density_enabled(), False)

# ---------------------------------------------------------------------------
# Driving _evaluate() with radar-shaped frames rather than setting _present by
# hand. Bypassing it is what let the real bug through: density was keyed off
# the wake decision, so a weak stationary signal threw the layout back to FAR
# while somebody was standing right in front of the wall reading it.
# ---------------------------------------------------------------------------

def frame(state, mv_cm=0, mv_e=0, st_cm=0, st_e=0):
    candidates = [x for x in (mv_cm if state in (1, 3) else 0,
                              st_cm if state in (2, 3) else 0) if x]
    return {"source": "uart", "target_state": state,
            "moving_distance_cm": mv_cm, "moving_energy": mv_e,
            "stationary_distance_cm": st_cm, "stationary_energy": st_e,
            "distance_cm": min(candidates) if candidates else 0}


def radar(d, clock, reading, ticks=20, step_ms=100):
    for _ in range(ticks):
        clock.advance(step_ms)
        d._last_reading = reading
        d._evaluate(reading)
        d._update_density()
    return d._density


print("")
print("G. real radar frames, through the presence evaluator")
d = daemon(presence_confirm_ms="300", sensor_stationary_energy_min="25")
check("moving, far away", radar(d, clock, frame(1, mv_cm=250, mv_e=60)), "far")
check("moving, close", radar(d, clock, frame(1, mv_cm=80, mv_e=70)), "near")
check("standing still, good signal",
      radar(d, clock, frame(2, st_cm=80, st_e=40)), "near")
# The regression: energy below the wake gate must not move the layout.
check("standing still, signal under the wake gate",
      radar(d, clock, frame(2, st_cm=80, st_e=20)), "near")
check("presence itself still says no", d._present, False)
check("target gone entirely", radar(d, clock, frame(0)), "far")

print("")
print("ALL PASS" if not fails else "%d FAILED: %s" % (len(fails), fails))
sys.exit(1 if fails else 0)
