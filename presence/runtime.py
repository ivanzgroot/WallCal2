"""
Shared runtime state between the presence daemon and the Flask app.

The two run as separate processes, so they talk through two small JSON files:

    presence.json   daemon -> web   live sensor + display state
    command.json    web -> daemon   override, manual wake, rescan requests

These live in /run/wallcal (tmpfs) when it exists so the several-times-a-second
state updates never touch the SD card. If /run is unavailable — a dev machine,
or the daemon running before systemd created the RuntimeDirectory — they fall
back to the project's data directory.
"""

from __future__ import annotations

import json
import os
import tempfile
import time

_FALLBACK_SUBDIR = "run"
_runtime_dir_cache: str | None = None


def runtime_dir() -> str:
    """Directory holding the IPC files, created on first use."""
    global _runtime_dir_cache
    if _runtime_dir_cache and os.path.isdir(_runtime_dir_cache):
        return _runtime_dir_cache

    candidates = [os.environ.get("WALLCAL_RUNTIME_DIR"), "/run/wallcal"]
    try:
        import config
        candidates.append(os.path.join(config.DATA_DIR, _FALLBACK_SUBDIR))
    except Exception:
        candidates.append(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", _FALLBACK_SUBDIR,
        ))

    for candidate in candidates:
        if not candidate:
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".write-test")
            with open(probe, "w") as fh:
                fh.write("1")
            os.unlink(probe)
        except OSError:
            continue
        _runtime_dir_cache = candidate
        return candidate

    return tempfile.gettempdir()


def state_path() -> str:
    return os.path.join(runtime_dir(), "presence.json")


def command_path() -> str:
    return os.path.join(runtime_dir(), "command.json")


def _write_json(path: str, payload: dict) -> None:
    """Atomically replace ``path`` so readers never see a half-written file."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o664)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


#: path -> ((inode, size, mtime_ns), parsed). Reading is on two hot paths:
#: the SSE generator watches the state file ten times a second, and the daemon
#: reads the command file on every radar frame. Both almost always find it
#: unchanged, so the parse happens only when the file actually moved — a stat
#: instead of a full json.load, which is an order of magnitude cheaper.
_parse_cache: dict = {}


def _read_json(path: str) -> dict:
    """Parse ``path``, reusing the last parse while the file is untouched.

    The returned dict is a shallow copy, so callers may add top-level keys
    (``read_state`` annotates liveness, /api/presence adds a hint) without
    poisoning the cache. Nested values are shared and must be treated as
    read-only — nothing in the project mutates them.
    """
    try:
        stat = os.stat(path)
    except OSError:
        _parse_cache.pop(path, None)
        return {}

    # The inode is in the stamp because writes land via os.replace, which
    # swaps the file rather than rewriting it in place.
    stamp = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    cached = _parse_cache.get(path)
    if cached is not None and cached[0] == stamp:
        return dict(cached[1])

    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        _parse_cache.pop(path, None)
        return {}

    if not isinstance(data, dict):
        _parse_cache.pop(path, None)
        return {}

    # A write racing the stat above only ever caches newer content under an
    # older stamp, which costs one redundant parse on the next call.
    _parse_cache[path] = (stamp, data)
    return dict(data)


# ---------------------------------------------------------------------------
# Daemon -> web
# ---------------------------------------------------------------------------

def write_state(state: dict) -> None:
    payload = dict(state)
    payload["updated_at"] = time.time()
    _write_json(state_path(), payload)


def read_state(max_age: float = 15.0) -> dict:
    """Live state, annotated with whether the daemon still looks alive."""
    state = _read_json(state_path())
    if not state:
        return {"daemon_running": False, "reason": "no state file"}
    age = time.time() - float(state.get("updated_at", 0))
    state["age_seconds"] = round(age, 2)
    state["daemon_running"] = age <= max_age
    return state


# ---------------------------------------------------------------------------
# Web -> daemon
# ---------------------------------------------------------------------------

DEFAULT_COMMAND = {
    "override": "auto",   # "auto" | "on" | "off"
    "wake_until": 0.0,    # epoch seconds; keeps the panel awake until then
    "pause_until": 0.0,   # epoch seconds; daemon releases the sensor until then
    "reload_seq": 0,      # bump to make the daemon re-read settings now
    "rescan_seq": 0,      # bump to make the daemon re-detect sensor + display
    # Anticipatory wake: the web app owns the calendar, so it computes the
    # next wake window and publishes it here rather than giving the daemon a
    # database dependency. Separate from wake_until because a manual override
    # must not silently cancel a calendar wake, or vice versa.
    "wake_plan_from": 0.0,   # epoch seconds; panel comes up at this time
    "wake_plan_until": 0.0,  # ...and is held until this one
    "wake_plan_label": "",
    "seq": 0,
}


def read_command() -> dict:
    command = dict(DEFAULT_COMMAND)
    command.update(_read_json(command_path()))
    return command


def push_command(**changes) -> dict:
    """Merge ``changes`` into the command file and bump its sequence number."""
    command = read_command()
    command.update(changes)
    command["seq"] = int(command.get("seq", 0)) + 1
    command["updated_at"] = time.time()
    _write_json(command_path(), command)
    return command


def set_override(mode: str) -> dict:
    if mode not in ("auto", "on", "off"):
        raise ValueError("override must be auto, on or off")
    return push_command(override=mode, wake_until=0.0)


def wake_for(seconds: float) -> dict:
    """Keep the display on for at least ``seconds`` from now."""
    return push_command(wake_until=time.time() + max(1.0, float(seconds)))


def set_wake_plan(start_epoch: float, until_epoch: float, label: str = "") -> dict:
    """Publish the next calendar-driven wake window."""
    return push_command(wake_plan_from=float(start_epoch or 0),
                        wake_plan_until=float(until_epoch or 0),
                        wake_plan_label=str(label or "")[:120])


def clear_wake_plan() -> dict:
    return set_wake_plan(0, 0, "")


def wake_plan_active(command=None) -> bool:
    command = command if command is not None else read_command()
    try:
        now = time.time()
        return (float(command.get("wake_plan_from", 0) or 0) <= now
                < float(command.get("wake_plan_until", 0) or 0))
    except (TypeError, ValueError):
        return False


def request_reload() -> dict:
    return push_command(reload_seq=int(read_command().get("reload_seq", 0)) + 1)


def request_rescan() -> dict:
    return push_command(rescan_seq=int(read_command().get("rescan_seq", 0)) + 1)


# ---------------------------------------------------------------------------
# Sensor arbitration
#
# Only one process can hold a serial port. The daemon owns it in normal
# operation, so command-line tools ask it to stand down first rather than
# racing it — "device reports readiness to read but returned no data" is what
# two readers on one UART looks like.
# ---------------------------------------------------------------------------

def request_pause(seconds: float) -> dict:
    """Ask the daemon to release the sensor for the next ``seconds``.

    Deliberately expiry-based rather than a flag: if the tool that requested
    the pause is killed, the daemon resumes on its own.
    """
    return push_command(pause_until=time.time() + max(0.0, float(seconds)))


def clear_pause() -> dict:
    return push_command(pause_until=0.0)


def pause_active(command=None) -> bool:
    command = command if command is not None else read_command()
    try:
        return float(command.get("pause_until", 0) or 0) > time.time()
    except (TypeError, ValueError):
        return False
