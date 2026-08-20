"""The two processes must converge on one IPC directory.

Both services can start before systemd has created /run/wallcal. Whichever
loses that race used to keep its fallback for its whole lifetime, so the
daemon wrote to /run/wallcal while the web app read data/run/ and reported
the daemon dead — for days, with "sensor not detected" as the only symptom
and every service reporting healthy.
"""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tmp = tempfile.mkdtemp()
os.environ["WALLCAL_DB_PATH"] = os.path.join(tmp, "t.db")

from presence import runtime          # noqa: E402

fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


# The preferred directory is blocked by a plain file, so makedirs raises the
# way it does when /run is not writable by this user. Removing the blocker is
# systemd creating RuntimeDirectory a moment later.
blocker = os.path.join(tmp, "blocked")
preferred = os.path.join(blocker, "wallcal")
fallback = os.path.join(tmp, "fallback")
os.makedirs(fallback, exist_ok=True)
runtime._candidates = lambda: [preferred, fallback]


def block():
    if os.path.isdir(preferred):
        os.rmdir(preferred)
    if os.path.isdir(blocker):
        os.rmdir(blocker)
    with open(blocker, "w") as fh:
        fh.write("in the way")


def unblock():
    if os.path.isfile(blocker):
        os.unlink(blocker)
    os.makedirs(preferred, exist_ok=True)


def reset():
    runtime._runtime_dir_cache = None
    runtime._runtime_dir_preferred = False
    runtime._last_upgrade_check = 0.0
    runtime._parse_cache.clear()


print("Falling back, then upgrading when the preferred one appears")
block()
reset()
check("starts on the fallback", runtime.runtime_dir(), fallback)
check("and knows it is a fallback", runtime._runtime_dir_preferred, False)
check("repeat calls are stable", runtime.runtime_dir(), fallback)

unblock()
runtime._last_upgrade_check = 0.0          # the throttle window has passed
check("upgrades itself once the preferred one exists",
      runtime.runtime_dir(), preferred)
check("and stops looking", runtime._runtime_dir_preferred, True)

print("")
print("The upgrade check is throttled, not run on every read")
block()
runtime._runtime_dir_cache = fallback
runtime._runtime_dir_preferred = False
runtime._last_upgrade_check = 1e9          # a check just happened
check("a recent check is not repeated", runtime.runtime_dir(), fallback)

print("")
print("A writable preferred directory is taken straight away")
unblock()
reset()
check("no fallback needed", runtime.runtime_dir(), preferred)
check("and it stops looking", runtime._runtime_dir_preferred, True)

print("")
print("Once converged, a write on one side is read on the other")
reset()
runtime.write_state({"present": True, "reading": {"distance_cm": 41}})
state = runtime.read_state()
check("the reader sees it", (state.get("reading") or {}).get("distance_cm"), 41)
check("and calls the daemon alive", state.get("daemon_running"), True)

print("")
print("ALL PASS" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
