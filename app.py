"""
WallCal — Flask Backend
Serves the calendar frontend, provides REST API for events, settings,
and calendar management. Runs a background CalDAV poller thread.
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from flask import (Flask, Response, jsonify, request, send_from_directory,
                   render_template, url_for)

import config
import database
import feeds
import widgets
import prewake
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
prewake_scheduler = prewake.PrewakeScheduler()
widget_refresher = widgets.Refresher()


# ---------------------------------------------------------------------------
# Frontend route
# ---------------------------------------------------------------------------

@app.context_processor
def _asset_helpers():
    """``static_url('css/wall.css')`` — a static URL stamped with the file's
    mtime.

    The kiosk browser starts once and runs for months behind an aggressive
    cache, with nobody in front of it to press ctrl-shift-R. Without the stamp,
    an update leaves the wall showing the old stylesheet until something
    evicts it.
    """
    def static_url(filename):
        path = os.path.join(app.static_folder or "static", filename)
        try:
            stamp = int(os.path.getmtime(path))
        except OSError:
            stamp = 0
        return url_for("static", filename=filename, v=stamp)
    return {"static_url": static_url}


@app.route("/")
def index():
    return render_template("calendar.html")


@app.route("/settings")
def settings_page():
    """The service surface, on its own URL.

    Separate from the wall display because the two have different jobs and
    different viewports: the wall is pinned to 1920 and has no pointer, this
    is opened on a phone by scanning the QR code off it.
    """
    return render_template("settings.html")


# ---------------------------------------------------------------------------
# Events API
# ---------------------------------------------------------------------------

@app.route("/events")
def get_events():
    """Return events for the next 30 days as JSON."""
    days = request.args.get("days", 30, type=int)
    days = min(days, 90)  # cap at 90

    settings = database.get_all_settings()
    abfall_id = (settings.get("abfall_calendar_id") or "").strip()

    # Read the window once and split it here. Asking the database a second
    # time for the Abfall calendar meant scanning and materialising the same
    # rows twice on every poll from the wall.
    rows = database.get_cached_events(days=days)

    # The Abfall source is a CalDAV calendar like any other, but its entries
    # belong to their own widget rather than the day's agenda.
    events = ([e for e in rows if str(e.get("calendar_id")) != abfall_id]
              if abfall_id else rows)
    return jsonify({
        "events": events,
        "count": len(events),
        "abfall": _abfall_payload(settings, rows),
        "last_poll": database.get_last_poll_time(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


def _abfall_payload(settings, rows):
    """Upcoming bin collections, already mapped to fraction and colour.

    Multiple fractions on one day are kept as a list rather than collapsed,
    so the wall renders them together instead of one overwriting the other.

    ``rows`` is the event window the agenda was already built from, so this
    follows the caller's horizon rather than querying its own 30 days. The
    output is capped at the next eight collection days either way.
    """
    cal_id = (settings.get("abfall_calendar_id") or "").strip()
    if not cal_id.isdigit():
        return None
    fractions = widgets.parse_fractions(settings.get("abfall_fractions", ""))

    by_day = {}
    for ev in rows:
        if str(ev.get("calendar_id")) != cal_id:
            continue
        day = str(ev.get("dtstart", ""))[:10]
        if not day:
            continue
        match = widgets.match_fraction(ev.get("summary", ""), fractions)
        by_day.setdefault(day, []).append({
            "label": match["label"],
            "color": match["color"],
            "summary": ev.get("summary", ""),
        })
    return {"days": [{"date": d, "fractions": by_day[d]}
                     for d in sorted(by_day)][:8],
            "from_hour": settings.get("abfall_from_hour", "16:00"),
            "until_hour": settings.get("abfall_until_hour", "10:00")}


# ---------------------------------------------------------------------------
# Widget feeds
#
# One endpoint, because the wall display needs them together and three
# requests every minute from a Pi 3B+ is three times the wakeups. Everything
# here reads the cache; a fetch only ever happens when a TTL has expired.
# ---------------------------------------------------------------------------

@app.route("/api/widgets")
def get_widgets():
    """Answered from SQLite, always.

    widgets.Refresher owns the fetching, on its own thread. This handler runs
    the same rules with allow_fetch off, so it cannot block on a network the
    Pi may not currently have.
    """
    settings = database.get_all_settings()
    return jsonify(widgets.collect(settings, allow_fetch=False))


@app.route("/api/transit/search")
def transit_search():
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify({"stations": []})
    try:
        provider = feeds.transit_provider(request.args.get("provider"))
        return jsonify({"stations": provider.search(query),
                        "provider": provider.name})
    except Exception as e:
        logger.info("Station search failed: %s", e)
        return jsonify({
            "stations": [],
            "error": "Die Haltestellensuche ist gerade nicht erreichbar. "
                     "Prüfe die Netzwerkverbindung und versuche es erneut.",
        }), 200


@app.route("/api/geocode")
def geocode_place():
    """Resolve a place name to coordinates — used by the weather and home
    location pickers, so nobody has to type latitude by hand."""
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify({"places": []})
    try:
        return jsonify({"places": feeds.geocode_search(query)})
    except Exception as e:
        logger.info("Geocode failed: %s", e)
        return jsonify({"places": [],
                        "error": "Die Ortssuche ist gerade nicht erreichbar."}), 200


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

    try:
        database.set_many_settings(filtered)
    except Exception as exc:
        # Almost always a root-owned database from an installer that did not
        # drop privileges. The raw SQLite text says nothing a user can act on.
        detail = str(exc)
        if "readonly" in detail.lower() or "permission" in detail.lower():
            detail = ("Die Datenbank ist schreibgeschützt. Auf dem Pi: "
                      "./wallcal.sh doctor --fix")
        logger.error("Settings write failed: %s", exc)
        return jsonify({"error": detail}), 500

    logger.info("Settings updated: %s", list(filtered.keys()))

    # A changed station or location invalidates its cache; leaving it would
    # keep the old departures on the wall until the TTL happened to lapse.
    invalidated = False
    if {"transit_station_id", "transit_provider"} & set(filtered):
        database.forget_feed("transit:")
        invalidated = True
    if {"weather_lat", "weather_lon", "weather_units"} & set(filtered):
        database.forget_feed("weather")
        invalidated = True
    if {"home_lat", "home_lon"} & set(filtered):
        database.forget_feed("travel:")
        invalidated = True
    if any(k.startswith("prewake_") for k in filtered):
        prewake_scheduler.poke()

    # A cleared cache has nothing to serve until the refresher runs again, and
    # somebody is sitting on the settings page waiting to see it work.
    if invalidated or any(k.startswith("widget_") for k in filtered):
        widget_refresher.poke()

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

    # The Abfall widget names its source by id. Left dangling, the widget
    # simply goes quiet with nothing anywhere saying why.
    if (database.get_setting("abfall_calendar_id", "") or "").strip() == str(cal_id):
        database.set_setting("abfall_calendar_id", "")
        logger.info("Cleared abfall_calendar_id — calendar %d was deleted", cal_id)

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


@app.route("/api/kiosk/restart", methods=["POST"])
def restart_kiosk():
    """Restart the kiosk browser on the wall."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wallcal.sh")
    try:
        subprocess.Popen([script, "kiosk", "restart"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/sensor/reset", methods=["POST"])
def reset_sensor():
    """Factory-reset the radar. Destructive: the UI confirms first."""
    try:
        from presence import calibration, ld2410
        with calibration.sensor_access():
            port, baud = calibration.resolve_target()
            with ld2410.LD2410(port, baud, timeout=0.5) as radar:
                radar.factory_reset()
        presence_runtime.request_rescan()
        return jsonify({"ok": True})
    except Exception as e:
        logger.error("Radar reset failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


#: Keyed on (url, size). Both are stable, so this is effectively permanent.
_QR_CACHE = {}


@app.route("/api/qr.svg")
def qr_svg():
    """The companion QR code, as SVG so it scales without artefacts.

    Encodes the settings URL. Note this puts an unauthenticated settings link
    on a wall — see the security note in the README. It is LAN-only, and the
    same posture the rest of the project already documents.
    """
    import io
    import segno

    target = request.args.get("url") or _settings_url()
    try:
        size = max(48, min(int(request.args.get("size", 96)), 512))
    except (TypeError, ValueError):
        size = 96

    cached = _QR_CACHE.get((target, size))
    if cached is not None:
        return cached, 200, {
            "Content-Type": "image/svg+xml",
            "Cache-Control": "public, max-age=3600",
        }

    code = segno.make(target, error="m")
    buf = io.BytesIO()
    # light=None leaves the background transparent, so the code sits on the
    # wall's own ground rather than in a white box.
    code.save(buf, kind="svg", scale=max(1, size // (code.symbol_size()[0] or 1)),
              dark="#7E8598", light=None, xmldecl=False, svgns=True)

    # The target and size change about never, so encoding it once is enough.
    # Reed-Solomon on a Pi 3B+ is not free, and this sits in the path of
    # somebody who has just walked up.
    if len(_QR_CACHE) > 8:
        _QR_CACHE.clear()
    _QR_CACHE[(target, size)] = buf.getvalue()

    return buf.getvalue(), 200, {
        "Content-Type": "image/svg+xml",
        "Cache-Control": "public, max-age=3600",
    }


def lan_address():
    """The address a phone on the same network can actually reach.

    request.host is wrong here: the kiosk browser loads the wall display from
    http://localhost:5005/, so a QR code built from it sends the phone to its
    own loopback. The Pi's LAN address has to be discovered instead.

    Opening a UDP socket toward a documentation address sends no packets — it
    only makes the kernel pick the interface it would route through, which is
    the one the phone is on.
    """
    override = (database.get_setting("public_host", "") or "").strip()
    if override:
        return override

    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 1))     # TEST-NET-1, never routed
        address = probe.getsockname()[0]
        if address and not address.startswith("127."):
            return address
    except OSError:
        pass
    finally:
        probe.close()

    # Fall back to mDNS, which the installer enables. Android resolves .local
    # unreliably, which is why it is second rather than first.
    try:
        return socket.gethostname() + ".local"
    except OSError:
        return request.host.split(":")[0]


def _settings_url():
    return f"http://{lan_address()}:{config.PORT}/settings"


@app.route("/api/prewake")
def get_prewake():
    """What the next calendar-driven wake is, and when."""
    plan = prewake.next_wake()
    if plan is None:
        return jsonify({"enabled": database.get_setting("prewake_enabled") == "true",
                        "next": None})
    start, end, label = plan
    return jsonify({"enabled": True, "next": {
        "from": start.isoformat(timespec="minutes"),
        "until": end.isoformat(timespec="minutes"),
        "label": label,
    }})


@app.route("/api/presence/live")
def get_presence_live():
    """The same fields /api/presence/stream pushes, as a plain request.

    The wall uses the stream; this is its fallback for when the stream will
    not open or has dropped. /api/presence returns the full sensor telemetry —
    several kB of radar frame that nothing on the wall reads — so this is the
    same tmpfs file filtered down to about 100 bytes.
    """
    state = presence_runtime.read_state()
    reading = state.get("reading") or {}
    return jsonify({
        "present": state.get("present"),
        "display_on": state.get("display_on"),
        "display_mode": state.get("display_mode", "normal"),
        "density": state.get("density", "far"),
        "screensaver": state.get("screensaver") or {},
        "brightness": state.get("brightness", 100),
        "brightness_source": state.get("brightness_source", "css"),
        # None in gpio and none sensor modes, which have no distance at all —
        # the wall display falls back to a single layout when it sees that.
        "distance_cm": reading.get("distance_cm"),
        # The settings page paints its manual-control buttons from this.
        # Without it the page could only ever show the override it had just
        # set itself, so a display forced on stayed forced on while the UI
        # went on claiming "automatisch".
        "override": state.get("override", "auto"),
        "daemon_running": state.get("daemon_running", False),
    })


#: How often the stream re-checks the state file. The daemon writes at most
#: every 0.3 s, so this bounds how long a transition somebody is standing
#: there watching can go unseen.
POLL_SECONDS = 0.1

#: How long an unchanged stream stays silent before a keepalive. Long enough
#: that an idle wall costs nothing, short enough that proxies and the browser
#: keep the connection open.
KEEPALIVE_SECONDS = 15.0


@app.route("/api/presence/stream")
def presence_stream():
    """Push panel state to the wall the moment it changes.

    This reverses an earlier decision. The argument against SSE was that a
    stream held open for months pins a worker thread — true, but there is
    exactly one client, the kiosk browser. One resident thread is cheaper
    than 345,000 requests a day, each of which spawns its own thread, and
    polling put a fixed interval of lag in front of every transition
    somebody is standing there watching.

    The generator watches the daemon's state file on tmpfs and only writes
    when something the wall cares about actually differs, so an idle wall
    costs one stat per tick and nothing on the wire.
    """
    def watch():
        previous = None
        sep = chr(10) * 2          # SSE frames end on a blank line
        # A comment line opens the stream so the browser fires onopen even
        # when the first real change is minutes away.
        yield ': connected' + sep
        last_write = time.monotonic()
        while True:
            state = presence_runtime.read_state()
            payload = {
                'density': state.get('density', 'far'),
                'display_on': state.get('display_on'),
                'display_mode': state.get('display_mode', 'normal'),
                'brightness': state.get('brightness', 100),
                'brightness_source': state.get('brightness_source', 'css'),
                'screensaver': state.get('screensaver') or {},
                'daemon_running': state.get('daemon_running', False),
            }
            now = time.monotonic()
            if payload != previous:
                previous = payload
                last_write = now
                yield 'data: ' + json.dumps(payload) + sep
            elif now - last_write >= KEEPALIVE_SECONDS:
                # Keeps proxies and the browser from closing an idle stream,
                # and is what surfaces a vanished client as a broken pipe.
                # Emitting it every tick instead cost 864,000 frames a day,
                # each one waking the kiosk browser to discard a comment.
                last_write = now
                yield ': keepalive' + sep
            time.sleep(POLL_SECONDS)

    return Response(watch(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


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
        # The single perceptual 0–100 value. The wall display applies it as an
        # overlay whenever brightness_source is "css" — that is every strategy
        # except pwm, where the hardware is already at this level.
        "brightness": state.get("brightness", 100),
        "brightness_source": state.get("brightness_source", "css"),
        "off_strategy": state.get("off_strategy", []),
        "pwm_error": state.get("pwm_error"),
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
        detail = str(e)
        if "readonly" in detail.lower() or "permission" in detail.lower():
            detail = ("Die Datenbank ist schreibgeschützt. Auf dem Pi: "
                      "./wallcal.sh doctor --fix")
        return jsonify({"error": detail}), 500
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
        # Per-feed freshness, so a widget quietly serving week-old data is
        # visible to status and doctor even though the wall never says so.
        "feeds": database.feed_freshness(),
    })


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

def shutdown_handler(signum, frame):
    logger.info("Received signal %d — shutting down", signum)
    poller.stop()
    prewake_scheduler.stop()
    widget_refresher.stop()
    sys.exit(0)


def main():
    # Init DB
    database.init_db()

    # Register signal handlers
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Start CalDAV poller
    poller.start()

    # Publish the anticipatory wake window for the daemon to act on.
    prewake_scheduler.start()

    # Keep the widget feed cache warm off the request path.
    widget_refresher.start()

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
