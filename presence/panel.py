"""
The panel: one place that owns whether the wall display is lit, and how much.

display.py knows how to stop the Pi driving its output. That is enough for a
monitor or a TV, and nothing here changes it. It is not enough for a bare
driver board whose backlight ignores signal loss, and it cannot dim at all.

So the *strategy* is a setting rather than an assumption:

    hdmi   the display.py backends, still autodetected exactly as before
    pwm    hardware PWM on the panel's backlight line
    css    browser-side dimming only; the panel stays powered
    none   never power off — the screensaver has the panel instead

Strategies combine, using the same comma syntax as display_backend, so
"pwm,hdmi" means ramp the backlight down *and* drop the output.

Ordering is why this is an object rather than a loop over actuators: going
dark, the ramp has to finish before the output goes away or nobody sees it;
coming back, the output has to exist before there is anything to ramp.

Brightness is a single perceptual 0-100 value that every strategy converges
on — duty cycle under pwm, a browser overlay otherwise. _target_brightness()
is its only writer, which is the seam an ambient light sensor plugs into: one
more input to that function, and nothing else in the project changes.
"""

from __future__ import annotations

import logging

from presence.display import DisplayController
from presence import pwm as pwm_mod

logger = logging.getLogger("wallcal.panel")

STRATEGIES = ("hdmi", "pwm", "css", "none")
DEFAULT_STRATEGY = "hdmi"


def parse_strategy(spec: str) -> list:
    """Normalise a strategy spec into an ordered list of known strategies.

    Unknown names are dropped with a warning rather than failing: this comes
    from a settings row that a typo in `wallcal.sh config set` can reach, and
    a dark wall unit is a worse outcome than an ignored word.
    """
    names = []
    for raw in str(spec or "").split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name not in STRATEGIES:
            logger.warning("Unknown off-strategy '%s' — ignoring. Known: %s",
                           name, ", ".join(STRATEGIES))
            continue
        if name not in names:
            names.append(name)
    return names or [DEFAULT_STRATEGY]


class PanelController:
    """Owns display power and brightness for whatever strategy is configured.

    Exposes set_power/backend_names/output compatibly with DisplayController
    so the daemon can hold one of these where it used to hold the other.
    """

    def __init__(self, strategy: str = DEFAULT_STRATEGY,
                 backend_spec: str = "auto", output: str = "auto",
                 pwm_settings: dict | None = None):
        self.strategy = parse_strategy(strategy)
        self.display: DisplayController | None = None
        self.pwm: pwm_mod.PwmBacklight | None = None
        self.pwm_error: str | None = None

        self._brightness = 100.0
        self._power: bool | None = None

        if "hdmi" in self.strategy:
            self.display = DisplayController(backend_spec=backend_spec,
                                             output=output)
        if "pwm" in self.strategy:
            self._open_pwm(pwm_settings or {})

        logger.info("Panel strategy: %s", ", ".join(self.strategy))

    def _open_pwm(self, settings: dict) -> None:
        try:
            self.pwm = pwm_mod.PwmBacklight(
                pin=settings.get("pin", 18),
                frequency_hz=settings.get("frequency_hz", pwm_mod.DEFAULT_FREQUENCY_HZ),
                gamma=settings.get("gamma", pwm_mod.DEFAULT_GAMMA),
                min_duty_percent=settings.get("min_duty_percent",
                                              pwm_mod.DEFAULT_MIN_DUTY_PERCENT),
                enable_pin=settings.get("enable_pin"),
                enable_active_high=settings.get("enable_active_high", True),
            ).open()
            self.pwm_error = None
        except Exception as exc:
            # Loud, but not fatal. "Fail loudly" cannot mean leaving the wall
            # unit dark because one actuator is missing — the error is logged,
            # published in the state file and checked by doctor, while
            # whatever else is in the strategy keeps the panel working.
            self.pwm_error = str(exc)
            self.pwm = None
            logger.error("PWM backlight unavailable: %s", exc)

    # -- introspection -----------------------------------------------------

    @property
    def backend_names(self) -> list:
        """What is actually driving the panel, for the state file and status."""
        names = []
        if self.display is not None:
            names.extend(self.display.backend_names)
        if self.pwm is not None:
            names.append("pwm")
        for soft in ("css", "none"):
            if soft in self.strategy:
                names.append(soft)
        return names

    @property
    def output(self):
        return self.display.output if self.display is not None else None

    @property
    def using_fallback(self) -> bool:
        """Whether the daemon should keep re-probing for a better backend.

        Only meaningful for the hdmi strategy — a PWM channel does not get
        better when a compositor starts.
        """
        if self.display is None:
            return False
        return self.display.using_fallback

    @property
    def brightness(self) -> float:
        return self._brightness

    @property
    def dimmable(self) -> bool:
        """True when brightness reaches real hardware rather than a CSS scrim."""
        return self.pwm is not None and self.pwm.healthy

    def info(self) -> dict:
        return {
            "strategy": list(self.strategy),
            "brightness": round(self._brightness, 1),
            "dimmable": self.dimmable,
            "power": self._power,
            "pwm": {
                "active": self.pwm is not None,
                "error": self.pwm_error,
                "describe": self.pwm.describe() if self.pwm else None,
                "fading": self.pwm.fading if self.pwm else False,
            },
            "display": self.display.info() if self.display else None,
        }

    # -- control -----------------------------------------------------------

    def set_brightness(self, percent: float, fade_ms: int = 0) -> float:
        """Set the single perceptual brightness value. Returns what was set.

        Under css/hdmi/none this only records the number — the browser reads
        it out of the state file and applies the overlay, because there is no
        hardware lever to pull.
        """
        percent = max(0.0, min(100.0, float(percent)))
        self._brightness = percent
        if self.pwm is not None:
            self.pwm.set_brightness(percent, fade_ms=fade_ms)
        return percent

    def target_brightness(self, base: float, scale: float = 1.0,
                          ambient: float | None = None) -> float:
        """Resolve the one brightness value everything converges on.

        This is the only place the target is computed, and the only reason it
        takes an ``ambient`` argument it currently never receives: adding a
        light sensor (BH1750 over I2C) means feeding this parameter and
        changing nothing else in the project. Everything downstream — PWM duty,
        the browser overlay, the state file, the CLI — already reads the
        result rather than deriving its own.

        ``scale`` is what the presence states use: 1.0 awake, lower while
        dimming or in night mode.
        """
        value = max(0.0, min(100.0, float(base))) * max(0.0, min(1.0, float(scale)))
        if ambient is not None:
            value = max(0.0, min(100.0, float(ambient)))
        return round(value, 1)

    def set_power(self, on: bool, force: bool = False, fade_ms: int = 0,
                  brightness: float | None = None) -> bool:
        """Light the panel or put it out, through every configured strategy.

        ``brightness`` sets the level the panel comes back at without driving
        it early: waking has to put the output up before the backlight, or the
        first thing on screen is a lit panel showing whatever the board had
        last.
        """
        if brightness is not None:
            self._brightness = max(0.0, min(100.0, float(brightness)))
        if self._power == on and not force:
            return True

        if "none" in self.strategy and not on:
            # The panel never sleeps under this strategy; the screensaver has
            # it instead. Brightness still applies, so it is not "ignore".
            logger.debug("Off requested but strategy is 'none' — staying lit")
            self._power = True
            return True

        ok = True
        if on:
            # Output first: ramping a panel the Pi is not driving yet means
            # the first frame arrives at whatever the board was last at.
            if self.display is not None:
                ok = self.display.set_power(True, force=force) and ok
            if self.pwm is not None:
                self.pwm.set_brightness(self._brightness, fade_ms=fade_ms)
        else:
            # Ramp first, then drop the output — the other order makes the
            # fade invisible, because the panel is already dark when it runs.
            if self.pwm is not None:
                self.pwm.set_brightness(0.0, fade_ms=fade_ms)
                if fade_ms > 0:
                    self.pwm.cancel_fade()   # settle at 0 before the output goes
                    self.pwm.set_brightness(0.0)
            if self.display is not None:
                ok = self.display.set_power(False, force=force) and ok

        self._power = on
        return ok

    def on(self) -> bool:
        return self.set_power(True)

    def off(self) -> bool:
        return self.set_power(False)

    def get_power(self):
        if self.display is not None:
            return self.display.get_power()
        return self._power

    def set_rotation(self, transform: str) -> bool:
        if self.display is None:
            return False
        return self.display.set_rotation(transform)

    def close(self) -> None:
        if self.pwm is not None:
            self.pwm.close()
            self.pwm = None
