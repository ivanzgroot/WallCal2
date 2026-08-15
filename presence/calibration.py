"""
Presence-sensor calibration, shared by the CLI and the web UI.

Calibration is a walk test: stand where you want the calendar to wake up, let
the radar watch you for a while, and turn what it saw into thresholds.

Two details matter enough to be worth stating:

* The daemon owns the serial port in normal operation, so calibration has to
  ask it to stand down first — two readers on one UART get nothing but errors.
* The daemon also narrows the radar's hardware gates to the *current* wake
  distance. Sampling through those would only ever confirm the setting already
  in place, never discover that you want a longer one, so the gates are opened
  to the sensor's full range for the test and restored afterwards.
"""

from __future__ import annotations

import contextlib
import logging
import os
import statistics
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config                        # noqa: E402
import database                      # noqa: E402
from presence import ld2410          # noqa: E402
from presence import runtime         # noqa: E402

logger = logging.getLogger("wallcal.calibration")

#: How long the daemon is asked to stay away, refreshed while we hold the port.
PAUSE_SECONDS = 60
PAUSE_REFRESH = 20
#: How long to wait for the daemon to confirm it let go.
RELEASE_TIMEOUT = 6.0
#: Fewest usable detections before a result is worth trusting.
MIN_SAMPLES = 5


# ---------------------------------------------------------------------------
# Port arbitration
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def sensor_access(on_message=None):
    """Take the serial port from the presence daemon for the duration.

    The pause is expiry-based and refreshed from a background thread, so if
    this process dies the daemon recovers on its own within a minute.
    """
    def say(message):
        logger.info("%s", message)
        if on_message:
            on_message(message)

    if not runtime.read_state().get("daemon_running"):
        yield True   # nothing is holding the port
        return

    stop = threading.Event()

    def keep_paused():
        while not stop.wait(PAUSE_REFRESH):
            runtime.request_pause(PAUSE_SECONDS)

    runtime.request_pause(PAUSE_SECONDS)
    keeper = threading.Thread(target=keep_paused, daemon=True,
                              name="calibration-pause")
    keeper.start()
    say("Asking the presence daemon to release the sensor…")

    released = False
    deadline = time.monotonic() + RELEASE_TIMEOUT
    while time.monotonic() < deadline:
        if runtime.read_state().get("paused"):
            released = True
            break
        time.sleep(0.2)
    if not released:
        say("The daemon did not confirm it released the port; continuing anyway.")

    try:
        yield released
    finally:
        # Join before clearing, or the keeper can re-arm the pause immediately
        # after and strand the daemon for another minute.
        stop.set()
        keeper.join(timeout=5)
        runtime.clear_pause()


def resolve_target(port=None, baud=None) -> tuple:
    """Work out which port/baud to talk to: explicit, then settings, then scan."""
    settings = database.get_all_settings()
    port = port or settings.get("sensor_uart_port", "auto")
    baud = int(baud or settings.get("sensor_uart_baud",
                                    config.DEFAULT_SENSOR_UART_BAUD))
    if not port or port == "auto":
        found_port, found_baud, _ = ld2410.autodetect()
        if not found_port:
            raise RuntimeError(
                "No LD2410 found on any serial port. Check the wiring, then "
                "run: wallcal.sh sensor scan --save"
            )
        return found_port, found_baud
    return port, baud


# ---------------------------------------------------------------------------
# Turning observations into settings
# ---------------------------------------------------------------------------

def summarise(distances, moving_energy, stationary_energy) -> dict | None:
    """Suggested thresholds from a set of observations, or None if too few."""
    if len(distances) < MIN_SAMPLES:
        return None

    ordered = sorted(distances)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    # Round the wake distance up to the next 10 cm and add a small margin, so
    # standing exactly where you tested still registers.
    suggested = int(round((p95 + 25) / 10.0) * 10)

    def floor_of(values, fraction=0.5, minimum=10):
        if not values:
            return minimum
        return max(minimum, int(statistics.median(values) * fraction))

    return {
        "samples": len(ordered),
        "min_cm": ordered[0],
        "median_cm": int(statistics.median(ordered)),
        "p95_cm": p95,
        "max_cm": ordered[-1],
        "suggested_distance_max_cm": suggested,
        "suggested_moving_energy_min": floor_of(moving_energy),
        "suggested_stationary_energy_min": floor_of(stationary_energy),
    }


def apply_suggestion(result: dict) -> dict:
    """Write a summary's suggestions into the settings table."""
    updates = {
        "sensor_distance_max_cm": str(result["suggested_distance_max_cm"]),
        "sensor_moving_energy_min": str(result["suggested_moving_energy_min"]),
        "sensor_stationary_energy_min": str(result["suggested_stationary_energy_min"]),
    }
    database.set_many_settings(updates)
    runtime.request_reload()
    return updates


# ---------------------------------------------------------------------------
# The job itself
# ---------------------------------------------------------------------------

class CalibrationJob:
    """A calibration run, executed on a background thread.

    Progress is readable at any time, so a terminal can render a live line and
    a browser can poll the same numbers.
    """

    IDLE = "idle"
    STARTING = "starting"
    COUNTDOWN = "countdown"
    SAMPLING = "sampling"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def __init__(self, seconds: int = 20, delay: int = 8,
                 port: str | None = None, baud: int | None = None):
        self.seconds = max(3, int(seconds))
        self.delay = max(0, int(delay))
        self.port = port
        self.baud = baud

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

        self.state = self.IDLE
        self.message = ""
        self.error: str | None = None
        self.result: dict | None = None
        self.remaining = 0
        self.frames = 0
        self.frames_with_target = 0
        self.furthest_seen = 0
        self.current_distance = 0
        self.current_state = "—"
        self.original_range_cm: int | None = None

        self._distances: list = []
        self._moving: list = []
        self._stationary: list = []

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "CalibrationJob":
        if self.running:
            return self
        self._cancel.clear()
        self.state = self.STARTING
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="calibration")
        self._thread.start()
        return self

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout=None) -> None:
        if self._thread:
            self._thread.join(timeout)

    @property
    def status(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "message": self.message,
                "error": self.error,
                "remaining": self.remaining,
                "samples": len(self._distances),
                "frames": self.frames,
                "frames_with_target": self.frames_with_target,
                "furthest_seen_cm": self.furthest_seen,
                "current_distance_cm": self.current_distance,
                "current_state": self.current_state,
                "original_range_cm": self.original_range_cm,
                "seconds": self.seconds,
                "delay": self.delay,
                "result": self.result,
                "running": self.running,
            }

    def _set(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    # -- the run -----------------------------------------------------------

    def _run(self) -> None:
        try:
            with sensor_access(on_message=lambda m: self._set(message=m)):
                port, baud = resolve_target(self.port, self.baud)
                self._set(message=f"Using {port} @ {baud}")
                with ld2410.LD2410(port, baud, timeout=0.3) as radar:
                    original, widened, fallback_gate = self._open_gates(radar)
                    try:
                        if not self._countdown(radar):
                            return
                        self._sample(radar)
                    finally:
                        self._restore_gates(radar, original, widened, fallback_gate)
            self._finish()
        except Exception as exc:
            logger.exception("Calibration failed")
            self._set(state=self.FAILED, error=str(exc))

    def _open_gates(self, radar):
        settings = database.get_all_settings()
        fallback_gate = ld2410.cm_to_gate(
            int(float(settings.get("sensor_distance_max_cm",
                                   config.DEFAULT_SENSOR_DISTANCE_MAX_CM))))
        original = None
        widened = False
        try:
            try:
                original = radar.read_parameters()
                self._set(original_range_cm=ld2410.gate_to_cm(original.max_moving_gate))
            except ld2410.LD2410Error:
                original = None
            radar.set_max_gates(ld2410.MAX_GATE, ld2410.MAX_GATE, 1)
            widened = True
            self._set(message="Sensor opened to its full range for the test")
        except ld2410.LD2410Error as exc:
            self._set(message=f"Could not widen the sensor range ({exc})")
        return original, widened, fallback_gate

    def _restore_gates(self, radar, original, widened, fallback_gate) -> None:
        # Never leave the radar wide open — the daemon would then wake the
        # panel for anyone anywhere in the room.
        if not widened:
            return
        try:
            if original is not None:
                radar.set_max_gates(original.max_moving_gate,
                                    original.max_stationary_gate,
                                    original.unmanned_duration_s)
            else:
                radar.set_max_gates(fallback_gate, fallback_gate, 1)
        except ld2410.LD2410Error as exc:
            logger.warning("Could not restore the sensor range: %s", exc)

    def _countdown(self, radar) -> bool:
        """Give the user time to walk over. False if cancelled."""
        if self.delay <= 0:
            return True
        self._set(state=self.COUNTDOWN,
                  message="Stand where you want the calendar to wake up")
        end = time.monotonic() + self.delay
        while time.monotonic() < end:
            if self._cancel.is_set():
                self._set(state=self.CANCELLED, message="Cancelled")
                return False
            self._set(remaining=max(0, int(round(end - time.monotonic()))))
            # Keep draining so sampling starts on fresh frames.
            radar.read(max_wait=0.2)
        return True

    def _sample(self, radar) -> None:
        self._set(state=self.SAMPLING, message="Sampling — hold still")
        end = time.monotonic() + self.seconds
        while time.monotonic() < end:
            if self._cancel.is_set():
                self._set(state=self.CANCELLED, message="Cancelled")
                return
            reading = radar.read(max_wait=1.0)
            remaining = max(0, int(round(end - time.monotonic())))
            if reading is None:
                self._set(remaining=remaining)
                continue

            with self._lock:
                self.frames += 1
                self.remaining = remaining
                self.current_state = reading.state_name
                # With no target the radar still reports a residual distance;
                # surfacing it would look like a detection.
                self.current_distance = reading.distance_cm if reading.has_target else 0
                if reading.has_target:
                    self.frames_with_target += 1
                    self.furthest_seen = max(self.furthest_seen, reading.distance_cm)
                    if reading.distance_cm:
                        self._distances.append(reading.distance_cm)
                        self._moving.append(reading.moving_energy)
                        self._stationary.append(reading.stationary_energy)

    def _finish(self) -> None:
        if self.state == self.CANCELLED:
            return
        result = summarise(self._distances, self._moving, self._stationary)
        if result is None:
            self._set(state=self.FAILED, error=self._diagnosis(),
                      message="Not enough detections")
            return
        self._set(state=self.DONE, result=result, remaining=0,
                  message="Calibration complete")

    def _diagnosis(self) -> str:
        """Explain a failed run in terms of what the sensor actually reported."""
        if self.frames == 0:
            return ("No frames at all — the sensor link is down. "
                    "Try a sensor scan.")
        if self.frames_with_target == 0:
            return ("The sensor is talking but never saw a target. Were you in "
                    "front of it? The radar is directional and should face into "
                    "the room. Standing perfectly still can also read as empty — "
                    "try waving an arm, or lower the sensitivity numbers.")
        return (f"Only {len(self._distances)} usable detections — stay in view "
                "for the whole sampling window.")

    def apply(self) -> dict:
        if not self.result:
            raise RuntimeError("no calibration result to apply")
        return apply_suggestion(self.result)
