"""
Hardware PWM backlight control.

The wall unit is a laptop panel on an HDMI->eDP driver board. Boards of that
class do not sleep when the HDMI signal goes away — the backlight stays lit
whatever the Pi does with its output — so none of the display.py backends can
switch it off. Driving the board's own backlight dimming line from the Pi is
the way out, and unlike every other backend it gives a *level* rather than a
switch, which is what makes fades and the dim-before-off state possible.

Two signals are worth tapping. Only the first is required:

    BL_PWM   The dimming input on the board's LED driver. Cut the trace (or
             lift the resistor) between the scaler's PWM output and the
             driver's dim pin, then inject the Pi's PWM there.
    BL_EN    Backlight enable. Many LED drivers leak a faint glow at 0% duty;
             a plain GPIO on BL_EN gives a true hard off. Both signals are
             levels, so the arrangement needs no state feedback to stay in
             step — which is the whole reason not to pulse the board's power
             button instead.

Hardware PWM only, through the sysfs interface. Software PWM (RPi.GPIO)
jitters under scheduler load and the flicker is visible on a display somebody
looks at every day; there is deliberately no fallback to it. If the sysfs
interface is unavailable this module reports itself unhealthy and says why,
and the panel keeps whatever control the rest of the strategy gives it.

Requires an overlay in /boot/firmware/config.txt, e.g. for GPIO18:

    dtoverlay=pwm,pin=18,func=2

The func value differs per pin and a wrong one fails silently — see PIN_MAP.
"""

from __future__ import annotations

import glob
import logging
import os
import threading
import time

logger = logging.getLogger("wallcal.pwm")

#: BCM pin -> (pwm channel, device-tree func). The Pi 3B+ exposes two hardware
#: PWM channels on two pins each. Getting func wrong produces no error and no
#: output, which is why it lives in code that can be checked rather than only
#: in the README.
PIN_MAP = {
    12: (0, 4),
    13: (1, 4),
    18: (0, 2),
    19: (1, 2),
}

#: Perceived brightness goes roughly as duty^(1/2.2), so a linear 50% duty
#: reads as about 73% bright and every fade lands wrong. The curve is applied
#: in exactly one place — _perceptual_to_duty — and everything else in the
#: project, settings and state file included, speaks the perceptual value.
DEFAULT_GAMMA = 2.2
DEFAULT_FREQUENCY_HZ = 2000
DEFAULT_MIN_DUTY_PERCENT = 3.0
DEFAULT_FADE_MS = 800

#: Steps per second while fading. 50 is smooth to the eye and costs nothing
#: measurable; the sysfs write is a handful of bytes.
FADE_STEPS_PER_SECOND = 50


class PwmError(RuntimeError):
    """The PWM hardware could not be set up or driven."""


# ---------------------------------------------------------------------------
# Gamma — the single source of truth for perceptual -> duty
# ---------------------------------------------------------------------------

def perceptual_to_duty(percent: float, gamma: float = DEFAULT_GAMMA,
                       floor_percent: float = DEFAULT_MIN_DUTY_PERCENT) -> float:
    """Map a perceptual 0-100 brightness onto a duty fraction 0.0-1.0.

    The floor is a remap rather than a clamp. Many LED drivers flicker or cut
    out below a few percent duty, but clamping would make every perceptual
    value under the floor produce identical output — a dead zone at exactly
    the end of the range the dim states spend their time in. Remapping onto
    [floor, 1] keeps the bottom of the scale useful.

    Zero is special-cased to a true zero: "off" has to mean off, and the
    caller drives BL_EN from that.
    """
    percent = max(0.0, min(100.0, float(percent)))
    if percent <= 0.0:
        return 0.0
    floor = max(0.0, min(0.99, float(floor_percent) / 100.0))
    duty = (percent / 100.0) ** float(gamma)
    return floor + duty * (1.0 - floor)


def duty_to_perceptual(duty: float, gamma: float = DEFAULT_GAMMA,
                       floor_percent: float = DEFAULT_MIN_DUTY_PERCENT) -> float:
    """Inverse of perceptual_to_duty, for reporting what the hardware is at."""
    duty = max(0.0, min(1.0, float(duty)))
    if duty <= 0.0:
        return 0.0
    floor = max(0.0, min(0.99, float(floor_percent) / 100.0))
    if duty <= floor:
        return 0.0
    normalised = (duty - floor) / (1.0 - floor)
    return 100.0 * (normalised ** (1.0 / float(gamma)))


# ---------------------------------------------------------------------------
# sysfs plumbing
# ---------------------------------------------------------------------------

def _write(path: str, value) -> None:
    with open(path, "w") as fh:
        fh.write(str(value))


def _read(path: str, default=None):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def find_chip(channel: int) -> str | None:
    """The pwmchip exposing ``channel``.

    The chip number is not stable across kernels and board revisions, so it is
    discovered rather than hardcoded — the pin is the part the user has to
    tell us, and only because it depends on how they wired it.
    """
    for chip in sorted(glob.glob("/sys/class/pwm/pwmchip*")):
        try:
            count = int(_read(os.path.join(chip, "npwm"), "0") or 0)
        except (TypeError, ValueError):
            continue
        if channel < count:
            return chip
    return None


def overlay_line(pin: int) -> str:
    """The config.txt line this pin needs, so callers never guess ``func``."""
    if pin not in PIN_MAP:
        raise PwmError(
            f"GPIO{pin} has no hardware PWM. Use one of: "
            + ", ".join(f"GPIO{p}" for p in sorted(PIN_MAP))
        )
    return f"dtoverlay=pwm,pin={pin},func={PIN_MAP[pin][1]}"


def survey() -> dict:
    """What the PWM subsystem looks like here — for doctor and the CLI."""
    chips = []
    for chip in sorted(glob.glob("/sys/class/pwm/pwmchip*")):
        chips.append({
            "path": chip,
            "npwm": _read(os.path.join(chip, "npwm")),
            "exported": sorted(
                os.path.basename(p)
                for p in glob.glob(os.path.join(chip, "pwm[0-9]*"))
            ),
        })
    return {
        "available": bool(chips),
        "chips": chips,
        "pins": {p: {"channel": c, "func": f} for p, (c, f) in PIN_MAP.items()},
    }


# ---------------------------------------------------------------------------
# BL_EN — a plain output level, for a true hard off
# ---------------------------------------------------------------------------

def _make_gpio_writer(pin: int):
    """Return ``(callable(bool), implementation_name)`` for a BCM output pin.

    Mirrors _make_gpio_reader in daemon.py: whichever library this Pi OS
    release happens to ship.
    """
    try:
        from gpiozero import DigitalOutputDevice
        device = DigitalOutputDevice(pin, initial_value=False)

        def writer(level):
            device.value = 1 if level else 0
        writer.close = device.close
        return writer, "gpiozero"
    except Exception as exc:
        logger.debug("gpiozero unavailable for BL_EN: %s", exc)

    try:
        import lgpio
        handle = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_output(handle, pin, 0)

        def writer(level):
            lgpio.gpio_write(handle, pin, 1 if level else 0)
        writer.close = lambda: lgpio.gpiochip_close(handle)
        return writer, "lgpio"
    except Exception as exc:
        logger.debug("lgpio unavailable for BL_EN: %s", exc)

    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

        def writer(level):
            GPIO.output(pin, GPIO.HIGH if level else GPIO.LOW)
        writer.close = GPIO.cleanup
        return writer, "RPi.GPIO"
    except Exception as exc:
        logger.debug("RPi.GPIO unavailable for BL_EN: %s", exc)

    raise PwmError(
        "no usable GPIO library for BL_EN — install python3-gpiozero or "
        "python3-lgpio, or clear the BL_EN pin setting"
    )


# ---------------------------------------------------------------------------
# The backlight
# ---------------------------------------------------------------------------

class PwmBacklight:
    """A dimmable backlight on a hardware PWM channel.

    Deliberately not a display.py Backend. Backend.set_power() is binary, and
    a level with a ramp is the entire reason this exists; and BACKEND_ORDER is
    a probe list that autodetection walks, which is the last place something
    that depends on how the user wired their panel should be discoverable.
    """

    def __init__(self, pin: int = 18, frequency_hz: int = DEFAULT_FREQUENCY_HZ,
                 gamma: float = DEFAULT_GAMMA,
                 min_duty_percent: float = DEFAULT_MIN_DUTY_PERCENT,
                 enable_pin: int | None = None, enable_active_high: bool = True):
        self.pin = int(pin)
        self.frequency_hz = max(1, int(frequency_hz))
        self.gamma = max(1.0, float(gamma))
        self.min_duty_percent = max(0.0, min(50.0, float(min_duty_percent)))
        self.enable_pin = int(enable_pin) if enable_pin is not None and int(enable_pin) >= 0 else None
        self.enable_active_high = bool(enable_active_high)

        self.error: str | None = None
        self._chip: str | None = None
        self._path: str | None = None
        self._period_ns = 0
        self._brightness = 0.0          # perceptual 0-100, what we last set
        self._enable_writer = None
        self._enable_impl = "none"

        self._lock = threading.Lock()
        self._fade_thread: threading.Thread | None = None
        self._fade_cancel = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> "PwmBacklight":
        if self.pin not in PIN_MAP:
            raise PwmError(
                f"GPIO{self.pin} has no hardware PWM channel. Hardware PWM is "
                f"available on GPIO " + ", ".join(str(p) for p in sorted(PIN_MAP))
                + ". Software PWM is not used here: it flickers visibly under load."
            )
        channel, _func = PIN_MAP[self.pin]

        chip = find_chip(channel)
        if chip is None:
            raise PwmError(
                "no PWM chip exposes channel %d. The overlay is probably "
                "missing — add '%s' to /boot/firmware/config.txt and reboot."
                % (channel, overlay_line(self.pin))
            )

        path = os.path.join(chip, f"pwm{channel}")
        if not os.path.isdir(path):
            try:
                _write(os.path.join(chip, "export"), channel)
            except OSError as exc:
                raise PwmError(
                    f"could not export PWM channel {channel} on {chip}: {exc}. "
                    "The wallcal user usually needs to be in the 'gpio' group."
                ) from exc
            # udev renames and re-chowns the new directory; give it a moment
            # rather than racing it on the first write.
            for _ in range(20):
                if os.path.isdir(path):
                    break
                time.sleep(0.05)

        if not os.path.isdir(path):
            raise PwmError(f"PWM channel {channel} did not appear at {path}")

        self._chip, self._path = chip, path
        self._apply_period()
        self._open_enable()
        logger.info("PWM backlight on GPIO%d (%s, %d Hz)",
                    self.pin, path, self.frequency_hz)
        self.error = None
        return self

    def _apply_period(self) -> None:
        period_ns = max(1, int(round(1_000_000_000 / self.frequency_hz)))
        try:
            # Duty must never exceed period, and the old duty may be larger
            # than the new period when the frequency goes up.
            _write(os.path.join(self._path, "duty_cycle"), 0)
            _write(os.path.join(self._path, "period"), period_ns)
        except OSError as exc:
            raise PwmError(
                f"could not set PWM period to {period_ns} ns "
                f"({self.frequency_hz} Hz): {exc}"
            ) from exc
        self._period_ns = period_ns

    def _open_enable(self) -> None:
        if self.enable_pin is None:
            return
        try:
            self._enable_writer, self._enable_impl = _make_gpio_writer(self.enable_pin)
            logger.info("BL_EN on BCM %d via %s", self.enable_pin, self._enable_impl)
        except PwmError as exc:
            # A missing BL_EN is a degradation, not a failure: PWM alone still
            # dims, it just cannot guarantee a black panel at zero.
            logger.warning("BL_EN unavailable (%s) — continuing without it", exc)
            self._enable_writer = None

    def close(self) -> None:
        self.cancel_fade()
        if self._path:
            try:
                _write(os.path.join(self._path, "enable"), 0)
            except OSError:
                pass
        closer = getattr(self._enable_writer, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
        self._enable_writer = None
        self._path = None

    @property
    def healthy(self) -> bool:
        return self._path is not None and self.error is None

    @property
    def brightness(self) -> float:
        """The perceptual 0-100 value currently driven."""
        return self._brightness

    def describe(self) -> str:
        bits = [f"GPIO{self.pin}", f"{self.frequency_hz} Hz"]
        if self.enable_pin is not None:
            bits.append(f"BL_EN BCM{self.enable_pin}")
        return "PWM " + ", ".join(bits)

    # -- driving -----------------------------------------------------------

    def set_brightness(self, percent: float, fade_ms: int = 0) -> bool:
        """Drive to a perceptual brightness, optionally ramping there.

        Cancels any fade already running. Returning immediately with the fade
        continuing on its own thread is deliberate: the daemon's main loop
        must not block for the better part of a second while the panel ramps,
        or presence stops being sampled exactly when somebody is walking up.
        """
        percent = max(0.0, min(100.0, float(percent)))
        self.cancel_fade()

        if fade_ms <= 0 or abs(percent - self._brightness) < 0.5:
            return self._drive(percent)

        start = self._brightness
        steps = max(1, int(round(fade_ms / 1000.0 * FADE_STEPS_PER_SECOND)))
        interval = (fade_ms / 1000.0) / steps
        cancel = self._fade_cancel
        cancel.clear()

        def ramp():
            for step in range(1, steps + 1):
                if cancel.is_set():
                    return
                self._drive(start + (percent - start) * (step / steps))
                if cancel.wait(interval):
                    return

        self._fade_thread = threading.Thread(target=ramp, daemon=True,
                                             name="pwm-fade")
        self._fade_thread.start()
        return True

    def cancel_fade(self) -> None:
        """Stop any ramp in progress. Wake has to be instant."""
        self._fade_cancel.set()
        thread = self._fade_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.5)
        self._fade_thread = None

    @property
    def fading(self) -> bool:
        thread = self._fade_thread
        return thread is not None and thread.is_alive()

    def _drive(self, percent: float) -> bool:
        """Write one brightness level to the hardware."""
        if self._path is None:
            return False
        duty = perceptual_to_duty(percent, self.gamma, self.min_duty_percent)

        with self._lock:
            try:
                if duty <= 0.0:
                    # Below the floor means off, not "as dim as the driver
                    # manages". BL_EN makes that a genuine black panel; without
                    # it, zero duty is the best available and some drivers
                    # still leak a glow.
                    self._set_enable(False)
                    _write(os.path.join(self._path, "duty_cycle"), 0)
                    _write(os.path.join(self._path, "enable"), 0)
                else:
                    _write(os.path.join(self._path, "duty_cycle"),
                           int(round(duty * self._period_ns)))
                    _write(os.path.join(self._path, "enable"), 1)
                    self._set_enable(True)
            except OSError as exc:
                self.error = str(exc)
                logger.warning("PWM write failed: %s", exc)
                return False

        self.error = None
        self._brightness = percent
        return True

    def _set_enable(self, on: bool) -> None:
        if self._enable_writer is None:
            return
        try:
            self._enable_writer(on if self.enable_active_high else not on)
        except Exception as exc:
            logger.debug("BL_EN write failed: %s", exc)

    # -- verification ------------------------------------------------------

    def sweep(self, seconds: float = 6.0, on_step=None) -> None:
        """Ramp 0 -> 100 -> 0 so the wiring can be proved without the daemon."""
        half = max(0.5, seconds / 2.0)
        steps = max(2, int(half * FADE_STEPS_PER_SECOND))
        for direction in (1, -1):
            for step in range(steps + 1):
                fraction = step / steps
                value = 100.0 * (fraction if direction > 0 else 1.0 - fraction)
                self._drive(value)
                if on_step:
                    on_step(value)
                time.sleep(half / steps)
