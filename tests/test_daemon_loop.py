"""Drive the daemon's real _tick() with a fake sensor.

Every earlier density suite called _update_density() directly, which is why
they all passed while the wall stayed on one layout: a second copy of the
density initialisation had been pasted into _decide_display(), so the pending
state was wiped on every pass through the loop. Testing the function proved
nothing about the loop that calls it.
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

import logging                                # noqa: E402
logging.basicConfig(level=logging.CRITICAL)
import database                               # noqa: E402
database.init_db()
import presence.daemon as pd                  # noqa: E402
from presence import runtime                  # noqa: E402

fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


class FakeSensor(pd.SensorSource):
    kind = "uart"

    def __init__(self, cm=250):
        self.cm = cm
        self.state = 1          # TARGET_MOVING

    def sample(self, timeout=0.5):
        return {"source": "uart", "target_state": self.state if self.cm else 0,
                "moving_distance_cm": self.cm, "moving_energy": 70,
                "stationary_distance_cm": 0, "stationary_energy": 0,
                "distance_cm": self.cm}

    @property
    def healthy(self):
        return True

    def describe(self):
        return "fake"


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


clock = Clock()
pd.time.monotonic = clock

database.set_many_settings({
    "display_off_strategy": "css", "density_mode": "auto",
    "density_near_cm": "100", "density_far_cm": "140",
    "density_enter_ms": "250", "density_debounce_ms": "1500",
    "sensor_distance_max_cm": "300", "sensor_mode": "none",
    "display_off_timeout": "600",
})

daemon = pd.PresenceDaemon()
daemon.settings = pd.Settings.load()
daemon._connect_display(force=True)
daemon.sensor = FakeSensor()


def hold(cm, seconds=3.0, step=0.1):
    """Stand at ``cm`` for a while, one radar frame per step."""
    daemon.sensor.cm = cm
    for _ in range(int(seconds / step)):
        clock.t += step
        daemon._tick()
    return daemon._density


print("Through the real loop, not the function alone")
check("far across the room", hold(250), "far")
check("near when close", hold(80), "near")
check("stays near while close", hold(60), "near")
check("far again after walking away", hold(250), "far")
check("near again on return", hold(70), "near")

print("")
print("The published state agrees with the daemon")
published = runtime.read_state()
check("state file carries it", published.get("density"), daemon._density)

print("")
print("Nothing else in the loop resets it")
hold(80)
before = daemon._density
for _ in range(50):                      # settings reload, redetect, state write
    clock.t += 0.1
    daemon._tick()
check("survives 5 s of loop", daemon._density, before)

print("")
print("ALL PASS" if not fails else "%d FAILED: %s" % (len(fails), fails))
sys.exit(1 if fails else 0)
