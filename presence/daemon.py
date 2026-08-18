"""
WallCal presence daemon.

Reads the HLK-LD2410C (over UART, or its OUT pin over GPIO), decides whether
somebody is standing in front of the wall calendar, and switches the display
on or off accordingly.

Everything is live-configurable: the daemon re-reads its settings from the
same SQLite database the web UI writes to, so changing the detection distance
in the browser takes effect within a couple of seconds without a restart.

Run with:  python -m presence.daemon
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, time as dtime

# Allow "python -m presence.daemon" and "python presence/daemon.py" alike.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config                                    # noqa: E402
import database                                  # noqa: E402
from presence import runtime                     # noqa: E402
from presence.panel import PanelController       # noqa: E402
from presence import ld2410                      # noqa: E402

logger = logging.getLogger("wallcal.presence")

SETTINGS_RELOAD_INTERVAL = 3.0      # seconds between settings re-reads
#: The wall polls this file for the distance that drives near/far switching,
#: so a one-second write interval put a whole second of lag in front of a
#: transition somebody is watching. It is a small JSON blob on tmpfs — the SD
#: card never sees it — so writing it more often costs effectively nothing.
STATE_WRITE_INTERVAL = 0.3          # seconds between routine state writes
DISPLAY_REDETECT_INTERVAL = 15.0    # while no real backend is available
SENSOR_RETRY_INTERVAL = 10.0        # seconds between reconnect attempts

#: The radar's own "unmanned duration": how long it keeps reporting a target
#: after the person has actually left. Keep this as short as the hardware
#: allows. WallCal times the idle period itself, so anything the sensor holds
#: on to is *added* to display_off_timeout rather than hidden inside it — a
#: sensor hold of 60s plus a 60s timeout means a two-minute wait, which reads
#: as "the screen never turns off".
SENSOR_HOLD_SECONDS = 1

#: What the lit panel is currently showing. Carried instead of a boolean
#: because brightness, and what the browser renders, both follow from it —
#: and because the screensaver becomes one more value rather than one more
#: flag to keep in step with the others.
MODE_NORMAL = "normal"
MODE_DIMMING = "dimming"
MODE_NIGHT = "night"
MODE_SAVER = "screensaver"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _as_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value, default=0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _as_float(value, default=0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


class Settings:
    """Typed snapshot of the presence-relevant settings."""

    def __init__(self, raw: dict):
        self.raw = raw
        g = raw.get

        self.sensor_mode = str(g("sensor_mode", "auto") or "auto").lower()
        self.gpio_pin = _as_int(g("sensor_gpio_pin"), config.DEFAULT_SENSOR_GPIO_PIN)
        self.gpio_active_high = _as_bool(g("sensor_gpio_active_high"), True)
        self.uart_port = str(g("sensor_uart_port") or config.DEFAULT_SENSOR_UART_PORT)
        self.uart_baud = _as_int(g("sensor_uart_baud"), config.DEFAULT_SENSOR_UART_BAUD)

        self.distance_max_cm = _as_int(g("sensor_distance_max_cm"),
                                       config.DEFAULT_SENSOR_DISTANCE_MAX_CM)
        self.distance_min_cm = _as_int(g("sensor_distance_min_cm"), 0)
        self.hysteresis_cm = _as_int(g("sensor_hysteresis_cm"),
                                     config.DEFAULT_SENSOR_HYSTERESIS_CM)
        self.moving_energy_min = _as_int(g("sensor_moving_energy_min"),
                                         config.DEFAULT_SENSOR_MOVING_ENERGY_MIN)
        self.stationary_energy_min = _as_int(g("sensor_stationary_energy_min"),
                                             config.DEFAULT_SENSOR_STATIONARY_ENERGY_MIN)
        self.use_stationary = _as_bool(g("sensor_use_stationary"), True)
        self.program_gates = _as_bool(g("sensor_program_gates"),
                                      config.DEFAULT_SENSOR_PROGRAM_GATES)

        self.off_timeout = max(1, _as_int(g("display_off_timeout"),
                                          config.DEFAULT_DISPLAY_OFF_TIMEOUT))
        self.confirm_ms = max(0, _as_int(g("presence_confirm_ms"),
                                         config.DEFAULT_PRESENCE_CONFIRM_MS))
        self.display_backend = str(g("display_backend", "auto") or "auto")
        self.display_output = str(g("display_output", "auto") or "auto")
        self.off_strategy = str(g("display_off_strategy")
                                or config.DEFAULT_DISPLAY_OFF_STRATEGY)

        self.pwm_gpio = _as_int(g("pwm_gpio"), config.DEFAULT_PWM_GPIO)
        self.pwm_frequency_hz = _as_int(g("pwm_frequency_hz"),
                                        config.DEFAULT_PWM_FREQUENCY_HZ)
        self.pwm_gamma = _as_float(g("pwm_gamma"), config.DEFAULT_PWM_GAMMA)
        self.pwm_min_duty_percent = _as_float(g("pwm_min_duty_percent"),
                                              config.DEFAULT_PWM_MIN_DUTY_PERCENT)
        self.pwm_fade_ms = max(0, _as_int(g("pwm_fade_ms"),
                                          config.DEFAULT_PWM_FADE_MS))
        self.pwm_enable_gpio = _as_int(g("pwm_enable_gpio"),
                                       config.DEFAULT_PWM_ENABLE_GPIO)
        self.pwm_enable_active_high = _as_bool(g("pwm_enable_active_high"), True)

        self.brightness = max(0, min(100, _as_int(g("brightness"),
                                                  config.DEFAULT_BRIGHTNESS)))
        self.dim_seconds = max(0, _as_int(g("dim_seconds"),
                                          config.DEFAULT_DIM_SECONDS))
        self.dim_level = max(0, min(100, _as_int(g("dim_level"),
                                                 config.DEFAULT_DIM_LEVEL)))

        self.schedule_start = str(g("schedule_start") or config.DEFAULT_SCHEDULE_START)
        self.schedule_end = str(g("schedule_end") or config.DEFAULT_SCHEDULE_END)
        self.night_mode = str(g("night_mode") or config.DEFAULT_NIGHT_MODE).lower()
        if self.night_mode not in ("off", "dim_clock", "never_wake"):
            self.night_mode = "off"
        self.night_brightness = max(0, min(100, _as_int(
            g("night_brightness"), config.DEFAULT_NIGHT_BRIGHTNESS)))

        self.density_mode = str(g("density_mode") or "auto").lower()
        self.density_near_cm = _as_int(g("density_near_cm"), 100)
        self.density_far_cm = _as_int(g("density_far_cm"), 140)
        self.density_min_band_cm = _as_int(g("density_min_band_cm"), 80)
        self.density_enter_ms = max(0, _as_int(g("density_enter_ms"), 250))
        self.density_leave_ms = max(0, _as_int(g("density_debounce_ms"), 1500))

        self.screensaver_style = str(g("screensaver_style")
                                     or config.DEFAULT_SCREENSAVER_STYLE).lower()
        if self.screensaver_style not in ("dim_dashboard", "clock", "blank"):
            self.screensaver_style = "dim_dashboard"
        self.screensaver_idle = max(0, _as_int(
            g("screensaver_idle_seconds"), config.DEFAULT_SCREENSAVER_IDLE_SECONDS))
        self.screensaver_brightness = max(0, min(100, _as_int(
            g("screensaver_brightness"), config.DEFAULT_SCREENSAVER_BRIGHTNESS)))

    @property
    def never_off(self) -> bool:
        """True when the panel never sleeps, so it needs something to show."""
        return "none" in [p.strip().lower()
                          for p in str(self.off_strategy).split(",")]

    @property
    def schedule_enabled(self) -> bool:
        """Whether the window is enforced at all. Kept for the state file."""
        return self.night_mode != "off"

    # Changing any of these means the sensor connection must be rebuilt.
    def sensor_signature(self) -> tuple:
        return (self.sensor_mode, self.gpio_pin, self.gpio_active_high,
                self.uart_port, self.uart_baud)

    # Changing any of these means the panel must be rebuilt — a new strategy,
    # a different backend, or PWM hardware that has to be re-exported.
    def display_signature(self) -> tuple:
        return (self.display_backend, self.display_output, self.off_strategy,
                self.pwm_gpio, self.pwm_frequency_hz, self.pwm_gamma,
                self.pwm_min_duty_percent, self.pwm_enable_gpio,
                self.pwm_enable_active_high)

    def pwm_kwargs(self) -> dict:
        return {
            "pin": self.pwm_gpio,
            "frequency_hz": self.pwm_frequency_hz,
            "gamma": self.pwm_gamma,
            "min_duty_percent": self.pwm_min_duty_percent,
            "enable_pin": self.pwm_enable_gpio,
            "enable_active_high": self.pwm_enable_active_high,
        }

    # The sensor-side hold is a constant now, so only the range matters here.
    def gate_signature(self) -> tuple:
        return (self.program_gates, self.distance_max_cm)

    @classmethod
    def load(cls) -> "Settings":
        try:
            return cls(database.get_all_settings())
        except Exception as exc:
            logger.error("Could not read settings (%s) — using defaults", exc)
            return cls({})

    def in_schedule(self, now: datetime | None = None) -> bool:
        """True when the current time is inside the normal display window.

        Note the sense: the window is the *permitted* period, and night mode
        governs what happens outside it. The README has always described it
        that way; only the setting that switches it on has changed shape.
        """
        if not self.schedule_enabled:
            return True
        start = _parse_hhmm(self.schedule_start, dtime(0, 0))
        end = _parse_hhmm(self.schedule_end, dtime(23, 59))
        current = (now or datetime.now()).time()
        if start == end:
            return True
        if start < end:
            return start <= current < end
        # Window wraps past midnight, e.g. 22:00 -> 06:00.
        return current >= start or current < end


def _parse_hhmm(text: str, fallback: dtime) -> dtime:
    try:
        hour, _, minute = str(text).partition(":")
        return dtime(int(hour) % 24, int(minute or 0) % 60)
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------------------
# Sensor sources
# ---------------------------------------------------------------------------

class SensorSource:
    kind = "none"

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def sample(self, timeout: float = 0.5):
        """Return a dict describing the latest observation, or None."""
        raise NotImplementedError

    @property
    def healthy(self) -> bool:
        return True

    def describe(self) -> str:
        return self.kind


class UartSensor(SensorSource):
    """Full LD2410C telemetry: state, distance and energy per target type."""

    kind = "uart"

    def __init__(self, port: str, baud: int):
        self.port = port
        self.baud = baud
        self._radar = None
        self._last_ok = 0.0
        self._frames = 0

    def open(self) -> None:
        self._radar = ld2410.LD2410(self.port, self.baud, timeout=0.3).open()
        self._last_ok = time.monotonic()
        logger.info("LD2410 connected on %s @ %d", self.port, self.baud)

    def close(self) -> None:
        if self._radar is not None:
            self._radar.close()
            self._radar = None

    def sample(self, timeout: float = 0.5):
        if self._radar is None:
            return None
        reading = self._radar.read(max_wait=timeout)
        if reading is None:
            return None
        self._last_ok = time.monotonic()
        self._frames += 1
        return {
            "source": "uart",
            "target_state": reading.target_state,
            "state_name": reading.state_name,
            "moving_distance_cm": reading.moving_distance_cm,
            "moving_energy": reading.moving_energy,
            "stationary_distance_cm": reading.stationary_distance_cm,
            "stationary_energy": reading.stationary_energy,
            "detection_distance_cm": reading.detection_distance_cm,
            "distance_cm": reading.distance_cm,
            "light": reading.light,
            "out_pin": reading.out_pin,
            "frames": self._frames,
        }

    @property
    def healthy(self) -> bool:
        return self._radar is not None and (time.monotonic() - self._last_ok) < 5.0

    def describe(self) -> str:
        return f"UART {self.port} @ {self.baud}"

    def program_gates(self, max_distance_cm: int, unmanned_duration_s: int) -> bool:
        """Push the distance threshold into the sensor's own gate config.

        Gates are 0.75 m wide so this is coarse; the daemon still applies the
        exact centimetre threshold in software. Doing both means the sensor
        stops reporting far-away targets at all, which cuts false wakes.
        """
        if self._radar is None:
            return False
        gate = ld2410.cm_to_gate(max_distance_cm)
        try:
            self._radar.set_max_gates(gate, gate, unmanned_duration_s)
            logger.info("Sensor gates set to %d (~%d cm), hold %ds",
                        gate, ld2410.gate_to_cm(gate), unmanned_duration_s)
            return True
        except ld2410.LD2410Error as exc:
            logger.warning("Could not program sensor gates: %s", exc)
            return False


class GpioSensor(SensorSource):
    """The LD2410C OUT pin — a plain presence/no-presence level.

    No distance information is available in this mode, so the distance
    threshold is enforced by the sensor's own gate configuration instead
    (see 'wallcal.sh sensor gates').
    """

    kind = "gpio"

    def __init__(self, pin: int, active_high: bool = True):
        self.pin = int(pin)
        self.active_high = bool(active_high)
        self._reader = None
        self._impl = "none"

    def open(self) -> None:
        self._reader, self._impl = _make_gpio_reader(self.pin)
        logger.info("GPIO presence input on BCM %d via %s", self.pin, self._impl)

    def close(self) -> None:
        closer = getattr(self._reader, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
        self._reader = None

    def sample(self, timeout: float = 0.5):
        if self._reader is None:
            return None
        try:
            level = bool(self._reader())
        except Exception as exc:
            logger.warning("GPIO read failed: %s", exc)
            return None
        detected = level if self.active_high else not level
        time.sleep(min(timeout, 0.1))  # OUT is a level, no need to spin
        return {
            "source": "gpio",
            "target_state": 1 if detected else 0,
            "state_name": "detected" if detected else "none",
            "gpio_level": level,
            "distance_cm": None,
            "moving_energy": 100 if detected else 0,
            "stationary_energy": 0,
        }

    @property
    def healthy(self) -> bool:
        return self._reader is not None

    def describe(self) -> str:
        return f"GPIO BCM{self.pin} ({self._impl})"


class NullSensor(SensorSource):
    """No sensor: the display simply stays on."""

    kind = "none"

    def sample(self, timeout: float = 0.5):
        time.sleep(min(timeout, 0.5))
        return {"source": "none", "target_state": 1, "state_name": "always-on",
                "distance_cm": None}

    def describe(self) -> str:
        return "no sensor (display always on)"


def _make_gpio_reader(pin: int):
    """Return ``(callable -> bool, implementation_name)`` for a BCM pin.

    Tries gpiozero, then lgpio, then RPi.GPIO — whichever this Pi OS release
    happens to ship. Each returns a zero-argument callable.
    """
    try:
        from gpiozero import DigitalInputDevice
        device = DigitalInputDevice(pin, pull_up=False)
        reader = lambda: device.value == 1  # noqa: E731
        reader.close = device.close
        return reader, "gpiozero"
    except Exception as exc:
        logger.debug("gpiozero unavailable: %s", exc)

    try:
        import lgpio
        handle = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_input(handle, pin)
        reader = lambda: lgpio.gpio_read(handle, pin) == 1  # noqa: E731
        reader.close = lambda: lgpio.gpiochip_close(handle)
        return reader, "lgpio"
    except Exception as exc:
        logger.debug("lgpio unavailable: %s", exc)

    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(pin, GPIO.IN)
        reader = lambda: GPIO.input(pin) == 1  # noqa: E731
        reader.close = GPIO.cleanup
        return reader, "RPi.GPIO"
    except Exception as exc:
        logger.debug("RPi.GPIO unavailable: %s", exc)

    raise RuntimeError(
        "no usable GPIO library found — install python3-gpiozero or python3-lgpio"
    )


def build_sensor(settings: Settings) -> SensorSource:
    """Create and open the sensor source described by the settings.

    ``sensor_mode`` may be ``uart``, ``gpio``, ``none`` or ``auto``; ``auto``
    looks for a radar on the serial ports first and falls back to the OUT pin.
    """
    mode = settings.sensor_mode

    def _gpio() -> SensorSource:
        sensor = GpioSensor(settings.gpio_pin, settings.gpio_active_high)
        sensor.open()
        return sensor

    if mode == "none":
        return NullSensor()

    if mode in ("uart", "auto"):
        port, baud = settings.uart_port, settings.uart_baud
        if mode == "auto" or not port or port == "auto":
            found_port, found_baud, _ = ld2410.autodetect(seconds=0.8)
            if found_port:
                port, baud = found_port, found_baud
            elif mode == "auto":
                logger.info("No LD2410 on serial — falling back to GPIO mode")
                return _gpio()
        sensor = UartSensor(port, baud)
        try:
            sensor.open()
            return sensor
        except Exception as exc:
            if mode == "auto":
                logger.warning("UART sensor failed (%s) — trying GPIO", exc)
                return _gpio()
            raise

    return _gpio()


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class PresenceDaemon:
    def __init__(self):
        self.settings = Settings.load()
        self.sensor: SensorSource | None = None
        self.display: PanelController | None = None

        self._stop = threading.Event()
        self._started_at = time.time()
        self._last_settings_load = 0.0
        self._last_state_write = 0.0
        self._last_sensor_attempt = 0.0
        self._last_display_detect = 0.0
        self._sensor_error: str | None = None

        self._paused = False
        self._present = False
        self._mode = MODE_NORMAL
        self._density = "far"
        self._density_pending: str | None = None
        self._density_pending_since = 0.0
        self._present_cause: str | None = None
        self._present_since: float | None = None
        self._last_present_at: float | None = None
        self._last_reading: dict = {}
        self._display_on: bool | None = None
        self._display_on_since: float | None = None
        self._on_seconds_today = 0.0
        self._on_seconds_day = datetime.now().date()
        self._wake_count = 0
        self._command_seq = -1
        self._reload_seq = 0
        self._rescan_seq = 0
        self._gate_signature: tuple | None = None

    # -- lifecycle ---------------------------------------------------------

    def stop(self, *_args) -> None:
        self._stop.set()

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        database.init_db()
        self._connect_display(force=True)
        self._connect_sensor()

        # Start awake: whoever just booted or restarted the service wants to
        # see the calendar, not a black panel.
        self._apply_display(True, reason="startup")

        logger.info("Presence daemon ready — sensor: %s | display: %s",
                    self.sensor.describe() if self.sensor else "none",
                    ",".join(self.display.backend_names) if self.display else "none")

        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                logger.exception("Unhandled error in main loop: %s", exc)
                self._stop.wait(2.0)

        self._shutdown()
        return 0

    def _shutdown(self) -> None:
        logger.info("Shutting down — leaving the display switched on")
        try:
            self._apply_display(True, reason="shutdown", force=True)
        except Exception:
            pass
        if self.sensor:
            self.sensor.close()
        self._write_state(final=True)

    # -- wiring ------------------------------------------------------------

    def _connect_display(self, force: bool = False) -> None:
        signature = self.settings.display_signature()
        if self.display is None or force or getattr(self, "_display_sig", None) != signature:
            if self.display is not None:
                self.display.close()
            self.display = PanelController(
                strategy=self.settings.off_strategy,
                backend_spec=self.settings.display_backend,
                output=self.settings.display_output,
                pwm_settings=self.settings.pwm_kwargs(),
            )
            self.display.set_brightness(self.settings.brightness)
            self._display_sig = signature
            self._display_on = None
        self._last_display_detect = time.monotonic()

    def _connect_sensor(self) -> None:
        self._last_sensor_attempt = time.monotonic()
        if self.sensor is not None:
            self.sensor.close()
            self.sensor = None
        try:
            self.sensor = build_sensor(self.settings)
            self._sensor_error = None
            self._gate_signature = None
        except Exception as exc:
            self._sensor_error = str(exc)
            logger.error("Sensor unavailable: %s", exc)
            self.sensor = None

    def _maybe_program_gates(self) -> None:
        """Keep the sensor's own gate config in step with the UI setting."""
        if not isinstance(self.sensor, UartSensor) or not self.settings.program_gates:
            return
        signature = self.settings.gate_signature()
        if signature == self._gate_signature:
            return
        self._gate_signature = signature
        self.sensor.program_gates(self.settings.distance_max_cm, SENSOR_HOLD_SECONDS)

    # -- main loop ---------------------------------------------------------

    def _tick(self) -> None:
        now = time.monotonic()

        self._poll_commands()

        # A command-line tool wants the sensor. Let go of it — two readers on
        # one UART produce nothing but errors for both.
        if runtime.pause_active(getattr(self, "_command", None)):
            just_paused = not self._paused
            if just_paused:
                logger.info("Paused by a command-line tool — releasing the sensor")
                self._paused = True
            if self.sensor is not None:
                self.sensor.close()
                self.sensor = None
            self._decide_display()
            # Publish the transition immediately rather than on the next
            # throttled write — the waiting tool is watching for this flag
            # before it opens the port.
            self._write_state(force=just_paused)
            self._stop.wait(0.5)
            return

        if self._paused:
            self._paused = False
            logger.info("Resuming — reconnecting the sensor")
            self._connect_sensor()
            self._write_state(force=True)

        if now - self._last_settings_load >= SETTINGS_RELOAD_INTERVAL:
            self._reload_settings()

        # This service starts at multi-user.target, well before the compositor.
        # Anything session-less (vcgencmd, backlight, fbcon) is already
        # answering by then and would otherwise be kept for good — so keep
        # re-probing until a session-native backend becomes available.
        if self.display is not None and self.display.using_fallback \
                and now - self._last_display_detect >= DISPLAY_REDETECT_INTERVAL:
            previous_backends = list(self.display.backend_names)
            previous_state = self._display_on
            self._connect_display(force=True)
            if self.display.backend_names != previous_backends:
                logger.info("Display backend upgraded: %s -> %s",
                            ", ".join(previous_backends) or "none",
                            ", ".join(self.display.backend_names))
            if previous_state is not None:
                self._apply_display(previous_state, reason="redetect", force=True)

        if self.sensor is None or not self.sensor.healthy:
            if now - self._last_sensor_attempt >= SENSOR_RETRY_INTERVAL:
                logger.info("Reconnecting sensor…")
                self._connect_sensor()

        reading = None
        if self.sensor is not None:
            try:
                reading = self.sensor.sample(timeout=0.5)
            except Exception as exc:
                self._sensor_error = str(exc)
                logger.warning("Sensor sample failed: %s", exc)
                self.sensor.close()
                self.sensor = None
        else:
            self._stop.wait(0.5)

        self._maybe_program_gates()

        if reading is not None:
            self._last_reading = reading
            self._evaluate(reading)

        self._update_density()
        self._decide_display()
        self._write_state()

    def _poll_commands(self) -> None:
        command = runtime.read_command()
        seq = int(command.get("seq", 0))
        self._command = command
        if seq == self._command_seq:
            return
        self._command_seq = seq

        reload_seq = int(command.get("reload_seq", 0))
        if reload_seq != self._reload_seq:
            self._reload_seq = reload_seq
            self._reload_settings(force=True)

        rescan_seq = int(command.get("rescan_seq", 0))
        if rescan_seq != self._rescan_seq:
            self._rescan_seq = rescan_seq
            logger.info("Rescan requested — re-detecting sensor and display")
            self._connect_display(force=True)
            self._connect_sensor()

    def _reload_settings(self, force: bool = False) -> None:
        self._last_settings_load = time.monotonic()
        previous = self.settings
        self.settings = Settings.load()

        if force or previous.sensor_signature() != self.settings.sensor_signature():
            if not force:
                logger.info("Sensor settings changed — reconnecting")
            self._connect_sensor()
        if previous.display_signature() != self.settings.display_signature():
            logger.info("Display settings changed — re-detecting")
            self._connect_display(force=True)
        elif previous.brightness != self.settings.brightness:
            # Deliberately not part of display_signature: brightness changes
            # while somebody drags a slider, and rebuilding the panel — which
            # re-exports the PWM channel — on every step would flicker.
            self._apply_brightness(reason="setting")

    # -- presence logic ----------------------------------------------------

    def _evaluate(self, reading: dict) -> None:
        now = time.monotonic()
        raw = self._raw_present(reading)

        if raw:
            if self._present_since is None:
                self._present_since = now
            self._last_present_at = now
            # Only count as present once it has held for the confirm window;
            # this filters single-frame radar glitches.
            if (now - self._present_since) * 1000.0 >= self.settings.confirm_ms:
                self._present = True
        else:
            self._present_since = None
            self._present = False

    def _raw_present(self, reading: dict) -> bool:
        """Decide presence, recording which rule fired.

        The cause is published in the state file: "the screen never sleeps" is
        almost always a stationary-target false positive, and knowing that
        without a logic trace saves a lot of guessing.
        """
        settings = self.settings
        self._present_cause = None

        if reading.get("source") == "none":
            self._present_cause = "no-sensor"
            return True
        if reading.get("source") == "gpio":
            if reading.get("target_state") == 1:
                self._present_cause = "gpio-pin"
                return True
            return False

        state = reading.get("target_state", 0)
        if state == ld2410.TARGET_NONE:
            return False

        # While already present, tolerate an extra margin before dropping out
        # so somebody hovering at the threshold does not make it flicker.
        limit = settings.distance_max_cm + (settings.hysteresis_cm if self._present else 0)
        floor = settings.distance_min_cm

        def within(distance):
            return distance and floor <= distance <= limit

        if state in (ld2410.TARGET_MOVING, ld2410.TARGET_BOTH):
            if within(reading.get("moving_distance_cm")) and \
                    reading.get("moving_energy", 0) >= settings.moving_energy_min:
                self._present_cause = "moving"
                return True

        if settings.use_stationary and state in (ld2410.TARGET_STATIONARY,
                                                 ld2410.TARGET_BOTH):
            if within(reading.get("stationary_distance_cm")) and \
                    reading.get("stationary_energy", 0) >= settings.stationary_energy_min:
                self._present_cause = "stationary"
                return True

        return False

    def _resolve_display(self) -> tuple:
        """Work out (power, mode, reason) for right now.

        Deciding these together rather than in scattered early-returns is
        deliberate: with a separate "am I dimming?" flag set along the way,
        every path that means "fully awake" has to remember to clear it, and
        the one that forgets leaves the panel stuck dim.

        Power of ``None`` means "hold whatever the panel already is". The mode
        is what the browser renders and what brightness resolves from.
        """
        command = getattr(self, "_command", runtime.DEFAULT_COMMAND)
        override = str(command.get("override", "auto"))
        wake_until = float(command.get("wake_until", 0) or 0)

        if override == "on":
            return True, MODE_NORMAL, "override:on"
        if override == "off":
            return False, MODE_NORMAL, "override:off"

        if wake_until and time.time() < wake_until:
            return True, MODE_NORMAL, "manual-wake"

        # The web app publishes this; the daemon has no calendar access and
        # deliberately no database dependency beyond its own settings.
        if runtime.wake_plan_active(command):
            return True, MODE_NORMAL, "calendar-wake"

        # While a tool holds the sensor we have no presence data, so keep the
        # panel on rather than blanking it under whoever is calibrating.
        if self._paused:
            return True, MODE_NORMAL, "paused-for-tools"

        night = not self.settings.in_schedule()
        if night and self.settings.night_mode == "never_wake":
            return False, MODE_NORMAL, "outside-schedule"

        # At night with dim_clock the panel still answers to presence, just
        # dimly and with the clock only. Everything below is the same state
        # machine; only the mode differs, which is the whole point of carrying
        # it rather than a boolean.
        awake_mode = MODE_NIGHT if night else MODE_NORMAL

        if self._present:
            return True, awake_mode, "presence"

        if self._last_present_at is None:
            # Nothing has ever been seen — hold whatever we already are, but
            # default to off once the grace period after startup has passed.
            if time.time() - self._started_at > self.settings.off_timeout:
                return False, MODE_NORMAL, "no-presence"
            return None, MODE_NORMAL, ""

        # ON -> (hold expires) -> DIMMING -> (dim_seconds) -> OFF
        # Any target during DIMMING takes the "presence" branch above, which
        # cancels the dim and ramps straight back up.
        idle = time.monotonic() - self._last_present_at
        hold = self.settings.off_timeout
        dim_for = self.settings.dim_seconds

        if idle < hold:
            return True, awake_mode, "presence-hold"

        # With the "none" strategy the panel cannot go dark, so the terminal
        # idle state is a screensaver rather than off. Between the hold
        # expiring and the screensaver engaging it sits dim — there is
        # nothing to go dark to, and leaving the full layout up is what burns
        # a panel in.
        if self.settings.never_off:
            if idle >= hold + self.settings.screensaver_idle:
                return True, MODE_SAVER, "screensaver"
            return (True, MODE_DIMMING if dim_for else awake_mode,
                    "screensaver-wait")

        if dim_for and idle < hold + dim_for:
            # Still lit, just dimmer — it should read as the display
            # considering rather than deciding, and give whoever is there a
            # chance to move before it commits. At night it is already dim, so
            # there is nothing to step down to.
            if awake_mode == MODE_NIGHT:
                return True, MODE_NIGHT, "night-hold"
            return True, MODE_DIMMING, "dimming"
        return False, MODE_NORMAL, "idle"

    def _decide_display(self) -> None:
        on, mode, reason = self._resolve_display()
        if on is None:
            return
        if on:
            self._set_mode(mode, reason=reason)
        else:
            # Going dark: no point ramping back to full on the way out, since
            # _apply_display is about to take it to zero regardless.
            self._mode = MODE_NORMAL
        self._density = "far"
        self._density_pending: str | None = None
        self._density_pending_since = 0.0
        self._apply_display(on, reason=reason)

    def _apply_display(self, on: bool, reason: str = "", force: bool = False) -> None:
        if self.display is None:
            return
        if self._display_on == on and not force:
            return

        # Wake instantly, sleep slowly. A ramp on the way up delays the thing
        # somebody walked over to read; on the way down it reads as the
        # display considering rather than deciding.
        self.display.set_power(
            on, force=force,
            fade_ms=0 if on else self.settings.pwm_fade_ms,
            # Waking returns to the level the *current* state wants, which is
            # the configured brightness normally but the dim level if the
            # panel is coming back up inside the dim window.
            brightness=self.display.target_brightness(
                self.settings.brightness,
                self._mode_level(self._mode),
            ) if on else None,
        )
        now = time.monotonic()

        if self._display_on is True and self._display_on_since is not None:
            self._accumulate_on_time(now - self._display_on_since)
        if on:
            self._display_on_since = now
            if self._display_on is False:
                self._wake_count += 1
        else:
            self._display_on_since = None

        if self._display_on != on:
            logger.info("Display %s (%s)", "ON" if on else "OFF", reason)
        self._display_on = on
        self._display_reason = reason
        self._write_state(force=True)

    # -- density ----------------------------------------------------------
    #
    # Decided here rather than in the browser. The daemon sees every radar
    # frame at about 10 Hz; the browser used to poll a number four times a
    # second purely to compare it against a threshold the daemon already
    # knows. Moving it removes a whole round trip from something a person is
    # standing there watching.

    def _density_enabled(self) -> bool:
        mode = self.settings.density_mode
        if mode == "off":
            return False
        if mode == "on":
            return True
        if self._last_reading.get("distance_cm") is None:
            return False        # gpio/none sensor modes have no distance
        # The usable FAR band is what makes switching worth doing at all.
        near = self.settings.density_near_cm
        if self.settings.never_off:
            band = 600 - near
        else:
            band = self.settings.distance_max_cm - near
        return band >= self.settings.density_min_band_cm

    def _update_density(self) -> None:
        if not self._density_enabled():
            # NEAR is the safe fallback: it is the layout that still makes
            # sense when somebody is standing right in front of the panel.
            self._density = "near" if self._last_reading else self._density
            self._density_pending = None
            return

        distance = self._last_reading.get("distance_cm")
        empty = not self._present or not distance or distance <= 0

        target = None
        if empty:
            target = "far"          # the at-rest layout for an empty room
        elif distance <= self.settings.density_near_cm:
            target = "near"
        elif distance >= self.settings.density_far_cm:
            target = "far"
        # Between the thresholds a frame argues for nothing. That gap is the
        # hysteresis, and a frame landing in it must not count either way.

        if target == self._density:
            self._density_pending = None
            return

        now = time.monotonic()
        if target is not None and self._density_pending != target:
            self._density_pending = target
            self._density_pending_since = now
            return

        if self._density_pending is None:
            return

        # Asymmetric, like presence itself: arriving is confirmed almost at
        # once, leaving waits out the jitter. Hysteresis already guards the
        # way back, so a slow entry protects nothing.
        wait = (self.settings.density_enter_ms if self._density_pending == "near"
                else self.settings.density_leave_ms) / 1000.0
        if now - self._density_pending_since >= wait:
            if self._density != self._density_pending:
                logger.debug("Density -> %s (%s cm)", self._density_pending, distance)
                self._density = self._density_pending
                # Publish immediately: this is the transition being watched.
                self._write_state(force=True)
            self._density_pending = None

    def _apply_brightness(self, dim_to: float | None = None, fade_ms: int = 0,
                          reason: str = "") -> None:
        """Drive the panel to the resolved brightness for the current state.

        Everything that wants the panel dimmer — the dim-before-off state,
        night mode, the screensaver — passes a level here rather than
        computing one of its own. One value, one place that resolves it.
        """
        if self.display is None:
            return
        target = self.display.target_brightness(self.settings.brightness, dim_to)
        if abs(target - self.display.brightness) < 0.5:
            return
        self.display.set_brightness(target, fade_ms=fade_ms)
        logger.info("Brightness -> %.0f%%%s", target, f" ({reason})" if reason else "")

    def _mode_level(self, mode: str):
        """The brightness a mode wants, or None for the configured level."""
        if mode == MODE_DIMMING:
            return self.settings.dim_level
        if mode == MODE_NIGHT:
            return self.settings.night_brightness
        if mode == MODE_SAVER:
            return self.settings.screensaver_brightness
        return None

    def _set_mode(self, mode: str, reason: str = "") -> None:
        """Move the lit panel into a display mode and light it accordingly.

        Wake instantly, sleep slowly: the ramp into DIMMING takes dim_seconds
        so there is time to notice it and move, while leaving it snaps back at
        once. A slow ramp *up* would delay exactly the person it is for.
        """
        if self._mode == mode:
            return
        previous, self._mode = self._mode, mode
        fade_ms = self.settings.dim_seconds * 1000 if mode == MODE_DIMMING else 0
        self._apply_brightness(dim_to=self._mode_level(mode), fade_ms=fade_ms,
                               reason=reason or f"{previous}->{mode}")

    def _accumulate_on_time(self, seconds: float) -> None:
        today = datetime.now().date()
        if today != self._on_seconds_day:
            self._on_seconds_day = today
            self._on_seconds_today = 0.0
        self._on_seconds_today += max(0.0, seconds)

    # -- state publishing --------------------------------------------------

    def _write_state(self, force: bool = False, final: bool = False) -> None:
        now = time.monotonic()
        if not force and not final and (now - self._last_state_write) < STATE_WRITE_INTERVAL:
            return
        self._last_state_write = now

        on_today = self._on_seconds_today
        if self._display_on and self._display_on_since is not None:
            on_today += now - self._display_on_since

        idle = None
        if self._last_present_at is not None:
            idle = round(now - self._last_present_at, 1)

        state = {
            "present": self._present,
            "present_cause": self._present_cause,
            "display_mode": self._mode,
            "density": self._density,
            "dimming": self._mode == MODE_DIMMING,
            "screensaver": {
                "active": self._mode == MODE_SAVER,
                "style": self.settings.screensaver_style,
                "idle_seconds": self.settings.screensaver_idle,
            },
            "paused": self._paused,
            "idle_seconds": idle,
            "display_on": self._display_on,
            "display_reason": getattr(self, "_display_reason", ""),
            "display_backends": self.display.backend_names if self.display else [],
            # getattr: state publishing sits in the main loop and must never
            # be the thing that raises.
            "display_backend_is_fallback": getattr(
                self.display, "using_fallback", True),
            "display_output": self.display.output if self.display else None,
            # The single brightness value (§1.3). In pwm mode the hardware is
            # already at this level; otherwise the browser applies it as an
            # overlay, which is why it is published either way.
            "brightness": self.display.brightness if self.display else 100.0,
            "brightness_source": ("pwm" if (self.display and self.display.dimmable)
                                  else "css"),
            "off_strategy": list(self.display.strategy) if self.display else [],
            "pwm_error": self.display.pwm_error if self.display else None,
            "sensor_kind": self.sensor.kind if self.sensor else "disconnected",
            "sensor_description": self.sensor.describe() if self.sensor else "",
            "sensor_healthy": bool(self.sensor and self.sensor.healthy),
            "sensor_error": self._sensor_error,
            "reading": self._last_reading,
            "thresholds": {
                "distance_max_cm": self.settings.distance_max_cm,
                "distance_min_cm": self.settings.distance_min_cm,
                "hysteresis_cm": self.settings.hysteresis_cm,
                "off_timeout": self.settings.off_timeout,
                "moving_energy_min": self.settings.moving_energy_min,
                "stationary_energy_min": self.settings.stationary_energy_min,
            },
            "schedule": {
                "enabled": self.settings.schedule_enabled,
                "start": self.settings.schedule_start,
                "end": self.settings.schedule_end,
                "active_now": self.settings.in_schedule(),
                "night_mode": self.settings.night_mode,
                "night_brightness": self.settings.night_brightness,
            },
            "override": str(getattr(self, "_command", {}).get("override", "auto")),
            "wake_plan": {
                "label": str(getattr(self, "_command", {}).get("wake_plan_label", "")),
                "from": float(getattr(self, "_command", {}).get("wake_plan_from", 0) or 0),
                "until": float(getattr(self, "_command", {}).get("wake_plan_until", 0) or 0),
                "active": runtime.wake_plan_active(getattr(self, "_command", None)),
            },
            "wake_count": self._wake_count,
            "display_on_seconds_today": round(on_today, 1),
            "uptime_seconds": round(time.time() - self._started_at, 1),
            "stopped": final,
        }
        try:
            runtime.write_state(state)
        except OSError as exc:
            logger.debug("Could not write state file: %s", exc)


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return PresenceDaemon().run()


if __name__ == "__main__":
    sys.exit(main())
