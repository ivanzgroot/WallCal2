"""
WallCal — Flask Backend
Serves the calendar frontend, provides REST API for events, settings,
and calendar management. Runs a background CalDAV poller thread.
"""

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory, render_template

import config
import database
from caldav_poller import CalDAVPoller
from presence import runtime as presence_runtime

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("wallcal")

# ---------------------------------------------------------------------------
# App + poller init
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = config.SECRET_KEY

poller = CalDAVPoller()


# ---------------------------------------------------------------------------
# Frontend route
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("calendar.html")


# ---------------------------------------------------------------------------
# Events API
# ---------------------------------------------------------------------------

@app.route("/events")
def get_events():
    """Return events for the next 30 days as JSON."""
    days = request.args.get("days", 30, type=int)
    days = min(days, 90)  # cap at 90

    events = database.get_cached_events(days=days)
    return jsonify({
        "events": events,
        "count": len(events),
        "last_poll": database.get_last_poll_time(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------

@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Return all current settings."""
    settings = database.get_all_settings()
    return jsonify(settings)


@app.route("/api/settings", methods=["POST"])
def update_settings():
    """Update one or more settings."""
    data = request.get_json(force=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    # Sanitize keys — the settings table's own defaults define what exists.
    filtered = {k: v for k, v in data.items() if k in database.SETTABLE_KEYS}
    rejected = sorted(set(data) - set(filtered))
    if not filtered:
        return jsonify({"error": "No valid settings provided",
                        "rejected": rejected}), 400

    database.set_many_settings(filtered)
    logger.info("Settings updated: %s", list(filtered.keys()))

    # Nudge the presence daemon so sensor/display changes apply immediately
    # instead of waiting for its next scheduled reload.
    try:
        presence_runtime.request_reload()
    except OSError as e:
        logger.debug("Could not signal the presence daemon: %s", e)

    return jsonify({"ok": True, "updated": list(filtered.keys()),
                    "rejected": rejected})


# ---------------------------------------------------------------------------
# Calendar management API
# ---------------------------------------------------------------------------

@app.route("/api/calendars", methods=["GET"])
def list_calendars():
    """List all configured calendars (passwords masked)."""
    cals = database.get_all_calendars(include_password=False)
    return jsonify({"calendars": cals})


@app.route("/api/calendars", methods=["POST"])
def add_calendar():
    """Add a new calendar source."""
    data = request.get_json(force=True)
    required = ["name", "caldav_url"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    cal_id = database.add_calendar(
        name=data["name"],
        caldav_url=data["caldav_url"],
        username=data.get("username", ""),
        password=data.get("password", ""),
        provider=data.get("provider", "nextcloud"),
        color=data.get("color", "#00d4aa"),
        enabled=data.get("enabled", True),
        cal_path=data.get("cal_path", ""),
    )
    logger.info("Calendar added: %s (id=%d)", data["name"], cal_id)

    # Trigger an immediate poll for the new calendar
    poller.poll_now()

    return jsonify({"ok": True, "id": cal_id}), 201


@app.route("/api/calendars/<int:cal_id>", methods=["PUT"])
def update_calendar(cal_id):
    """Update a calendar's settings."""
    data = request.get_json(force=True)
    if not database.get_calendar(cal_id):
        return jsonify({"error": "Calendar not found"}), 404

    database.update_calendar(cal_id, **data)
    logger.info("Calendar %d updated: %s", cal_id, list(data.keys()))
    return jsonify({"ok": True})


@app.route("/api/calendars/<int:cal_id>", methods=["DELETE"])
def delete_calendar(cal_id):
    """Delete a calendar and its cached events."""
    if not database.get_calendar(cal_id):
        return jsonify({"error": "Calendar not found"}), 404

    database.delete_calendar(cal_id)
    logger.info("Calendar %d deleted", cal_id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Calendar discovery API
# ---------------------------------------------------------------------------

@app.route("/api/discover", methods=["POST"])
def discover_calendars():
    """Discover calendars on a CalDAV server."""
    data = request.get_json(force=True)
    required = ["caldav_url", "username", "password"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    try:
        cals = poller.discover_calendars(
            caldav_url=data["caldav_url"],
            username=data["username"],
            password=data["password"],
        )
        return jsonify({"calendars": cals, "count": len(cals)})
    except Exception as e:
        logger.error("Discovery failed: %s", e)
        return jsonify({"error": str(e)}), 502


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------

@app.route("/api/test-connection", methods=["POST"])
def test_connection():
    """Test CalDAV connection credentials."""
    data = request.get_json(force=True)
    required = ["caldav_url", "username", "password"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    try:
        import caldav as caldav_lib
        client = caldav_lib.DAVClient(
            url=data["caldav_url"],
            username=data["username"],
            password=data["password"],
        )
        principal = client.principal()
        cals = principal.calendars()
        return jsonify({
            "ok": True,
            "message": f"Connected successfully. Found {len(cals)} calendar(s).",
            "calendar_count": len(cals),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


# ---------------------------------------------------------------------------
# Trigger manual poll
# ---------------------------------------------------------------------------

@app.route("/api/poll", methods=["POST"])
def trigger_poll():
    """Trigger an immediate CalDAV poll."""
    poller.poll_now()
    return jsonify({"ok": True, "message": "Poll triggered"})


# ---------------------------------------------------------------------------
# Presence / display API
#
# The presence daemon is a separate process. The web app never drives the
# panel directly — it publishes intent through the runtime command file and
# lets the daemon act on it, so the two can never fight over the display.
# ---------------------------------------------------------------------------

@app.route("/api/presence")
def get_presence():
    """Live presence, sensor telemetry and display power state."""
    state = presence_runtime.read_state()
    if not state.get("daemon_running"):
        state.setdefault("hint",
                         "Presence daemon not running — "
                         "check: sudo systemctl status wallcal-presence")
    return jsonify(state)


@app.route("/api/presence/override", methods=["POST"])
def set_presence_override():
    """Force the display on/off, or hand control back to the sensor."""
    data = request.get_json(force=True, silent=True) or {}
    mode = str(data.get("mode", "")).lower()
    try:
        presence_runtime.set_override(mode)
    except ValueError:
        return jsonify({"error": "mode must be one of: auto, on, off"}), 400
    logger.info("Display override set to %s", mode)
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/presence/wake", methods=["POST"])
def wake_display():
    """Keep the panel awake for a while — used by on-screen interaction."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        seconds = float(data.get("seconds", 300))
    except (TypeError, ValueError):
        return jsonify({"error": "seconds must be a number"}), 400
    seconds = max(1.0, min(seconds, 86400.0))
    presence_runtime.wake_for(seconds)
    return jsonify({"ok": True, "seconds": seconds})


@app.route("/api/presence/rescan", methods=["POST"])
def rescan_presence():
    """Ask the daemon to re-detect the sensor and the display backend."""
    presence_runtime.request_rescan()
    return jsonify({"ok": True})


@app.route("/api/display")
def get_display():
    """What the display subsystem detected, without touching the hardware."""
    state = presence_runtime.read_state()
    return jsonify({
        "power": state.get("display_on"),
        "reason": state.get("display_reason"),
        "backends": state.get("display_backends", []),
        "output": state.get("display_output"),
        "daemon_running": state.get("daemon_running", False),
    })


@app.route("/api/display/backends")
def list_display_backends():
    """Probe every display power backend. Slow (~1s) — call it on demand."""
    try:
        from presence.display import DisplayController
        return jsonify({"backends": DisplayController.survey()})
    except Exception as e:
        logger.error("Display survey failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/sensor/scan", methods=["POST"])
def scan_sensor():
    """Hunt for the radar across the serial ports and optionally save it."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        from presence import ld2410
        # Keep this well inside the browser's request timeout: probe only the
        # factory baud and the one common reflash. `wallcal.sh sensor scan`
        # sweeps every rate when this is not enough.
        port, baud, reading = ld2410.autodetect(
            bauds=(256000, 115200), seconds=0.6)
    except Exception as e:
        logger.error("Sensor scan failed: %s", e)
        return jsonify({"error": str(e)}), 500

    if not port:
        return jsonify({"found": False,
                        "ports_checked": ld2410.available_ports()}), 200

    if data.get("save"):
        database.set_many_settings({
            "sensor_uart_port": port,
            "sensor_uart_baud": str(baud),
            "sensor_mode": "uart",
        })
        presence_runtime.request_rescan()

    return jsonify({"found": True, "port": port, "baudrate": baud,
                    "reading": reading.to_dict() if reading else None,
                    "saved": bool(data.get("save"))})


# ---------------------------------------------------------------------------
# Sensor calibration
#
# A run takes half a minute, so it happens on a background thread and the
# browser polls for progress. Only one at a time — it takes the serial port
# away from the presence daemon while it runs.
# ---------------------------------------------------------------------------

_calibration_job = None
_calibration_lock = threading.Lock()


@app.route("/api/sensor/calibrate", methods=["GET"])
def calibration_status():
    with _calibration_lock:
        job = _calibration_job
    if job is None:
        return jsonify({"state": "idle", "running": False, "result": None})
    return jsonify(job.status)


@app.route("/api/sensor/calibrate", methods=["POST"])
def calibration_start():
    global _calibration_job
    data = request.get_json(force=True, silent=True) or {}
    try:
        seconds = int(data.get("seconds", 20))
        delay = int(data.get("delay", 8))
    except (TypeError, ValueError):
        return jsonify({"error": "seconds and delay must be whole numbers"}), 400

    from presence.calibration import CalibrationJob

    with _calibration_lock:
        if _calibration_job is not None and _calibration_job.running:
            return jsonify({"error": "A calibration is already running",
                            "status": _calibration_job.status}), 409
        _calibration_job = CalibrationJob(
            seconds=max(3, min(seconds, 300)),
            delay=max(0, min(delay, 120)),
        ).start()
        status = _calibration_job.status

    logger.info("Calibration started (%ss sampling, %ss countdown)", seconds, delay)
    return jsonify(status), 202


@app.route("/api/sensor/calibrate/cancel", methods=["POST"])
def calibration_cancel():
    with _calibration_lock:
        job = _calibration_job
    if job is None or not job.running:
        return jsonify({"ok": True, "message": "nothing running"})
    job.cancel()
    return jsonify({"ok": True})


@app.route("/api/sensor/calibrate/apply", methods=["POST"])
def calibration_apply():
    with _calibration_lock:
        job = _calibration_job
    if job is None or not job.result:
        return jsonify({"error": "No calibration result to apply"}), 400
    try:
        updates = job.apply()
    except Exception as e:
        logger.error("Applying calibration failed: %s", e)
        return jsonify({"error": str(e)}), 500
    logger.info("Calibration applied: %s", updates)
    return jsonify({"ok": True, "updated": updates})


# ---------------------------------------------------------------------------
# System info — for the status panel on the wall display
# ---------------------------------------------------------------------------

def _read_first_line(path, default=""):
    try:
        with open(path) as fh:
            return fh.readline().strip()
    except OSError:
        return default


def _vcgencmd(*args):
    if not shutil.which("vcgencmd"):
        return None
    try:
        out = subprocess.run(["vcgencmd", *args], capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


@app.route("/api/system")
def get_system():
    """Host health: temperature, power, disk, uptime."""
    info = {"hostname": os.uname().nodename if hasattr(os, "uname") else "",
            "server_time": datetime.now(timezone.utc).isoformat()}

    uptime_raw = _read_first_line("/proc/uptime")
    if uptime_raw:
        try:
            info["uptime_seconds"] = int(float(uptime_raw.split()[0]))
        except (ValueError, IndexError):
            pass

    temp_raw = _read_first_line("/sys/class/thermal/thermal_zone0/temp")
    if temp_raw.isdigit():
        info["cpu_temp_c"] = round(int(temp_raw) / 1000.0, 1)

    throttled = _vcgencmd("get_throttled")
    if throttled and "=" in throttled:
        flags = throttled.split("=", 1)[1]
        info["throttled"] = flags
        try:
            bits = int(flags, 16)
            info["under_voltage_now"] = bool(bits & 0x1)
            info["under_voltage_since_boot"] = bool(bits & 0x10000)
        except ValueError:
            pass

    try:
        usage = shutil.disk_usage(config.DATA_DIR)
        info["disk_free_mb"] = round(usage.free / 1048576)
        info["disk_percent_used"] = round(100 * (usage.used / usage.total), 1)
    except OSError:
        pass

    try:
        with open("/proc/meminfo") as fh:
            mem = {}
            for line in fh:
                key, _, value = line.partition(":")
                mem[key] = value.strip()
        total = int(mem.get("MemTotal", "0 kB").split()[0])
        available = int(mem.get("MemAvailable", "0 kB").split()[0])
        if total:
            info["memory_free_mb"] = round(available / 1024)
            info["memory_percent_used"] = round(100 * (1 - available / total), 1)
    except (OSError, ValueError, IndexError):
        pass

    return jsonify(info)


# ---------------------------------------------------------------------------
# Status / health
# ---------------------------------------------------------------------------

@app.route("/api/status")
def get_status():
    """Health check + poller status."""
    presence = presence_runtime.read_state()
    return jsonify({
        "status": "ok",
        "poller": poller.status,
        "last_poll": database.get_last_poll_time(),
        "calendars_configured": len(database.get_all_calendars()),
        "server_time": datetime.now(timezone.utc).isoformat(),
        "presence": {
            "daemon_running": presence.get("daemon_running", False),
            "present": presence.get("present"),
            "display_on": presence.get("display_on"),
        },
    })


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

def shutdown_handler(signum, frame):
    logger.info("Received signal %d — shutting down", signum)
    poller.stop()
    sys.exit(0)


def main():
    # Init DB
    database.init_db()

    # Register signal handlers
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Start CalDAV poller
    poller.start()

    # Start Flask
    logger.info("WallCal starting on %s:%d", config.HOST, config.PORT)
    app.run(
        host=config.HOST,
        port=config.PORT,
        debug=False,
        use_reloader=False,  # we manage our own threads
    )


if __name__ == "__main__":
    main()
