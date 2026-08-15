"""
WallCal Configuration
All values can be overridden via environment variables prefixed with WALLCAL_.
Runtime settings (CalDAV credentials, calendars, UI preferences) are stored
in SQLite and managed via the web settings panel.
"""

import os

# --- Server ---
PORT = int(os.environ.get("WALLCAL_PORT", "5005"))
HOST = os.environ.get("WALLCAL_HOST", "0.0.0.0")
SECRET_KEY = os.environ.get("WALLCAL_SECRET_KEY", "wallcal-change-me-in-production")

# --- Paths ---
DATA_DIR = os.environ.get("WALLCAL_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
DATABASE_PATH = os.environ.get("WALLCAL_DB_PATH", os.path.join(DATA_DIR, "wallcal.db"))

# --- CalDAV defaults (overridden by settings UI) ---
DEFAULT_POLL_INTERVAL_MINUTES = int(os.environ.get("WALLCAL_POLL_INTERVAL", "5"))

# --- Logging ---
LOG_LEVEL = os.environ.get("WALLCAL_LOG_LEVEL", "INFO")

# --- Presence sensor defaults (for Pi daemon, also settable via UI) ---
# "auto" probes the serial ports for an LD2410 and falls back to the OUT pin.
DEFAULT_SENSOR_MODE = "auto"          # "auto" | "uart" | "gpio" | "none"
# BCM numbering — LD2410C OUT pin (header pin 16). This was GPIO18 until the
# PWM backlight arrived: GPIO18 is one of only four hardware-PWM capable pins
# and the best default for the backlight, whereas OUT can live anywhere. The
# two only ever collide in gpio sensor mode; UART mode leaves OUT unused.
# Existing installs keep GPIO18 — see migration 1 in database.py.
DEFAULT_SENSOR_GPIO_PIN = 23
DEFAULT_SENSOR_GPIO_ACTIVE_HIGH = True
DEFAULT_SENSOR_UART_PORT = "auto"     # or an explicit /dev/tty… path
DEFAULT_SENSOR_UART_BAUD = 256000     # LD2410C factory default
DEFAULT_SENSOR_DISTANCE_MAX_CM = 300  # wake when a target is within 3 m
DEFAULT_SENSOR_DISTANCE_MIN_CM = 0    # ignore targets closer than this
DEFAULT_SENSOR_HYSTERESIS_CM = 40     # extra margin before dropping presence
DEFAULT_SENSOR_MOVING_ENERGY_MIN = 30      # 0-100, radar confidence gate
DEFAULT_SENSOR_STATIONARY_ENERGY_MIN = 25  # 0-100, for a motionless person
DEFAULT_SENSOR_USE_STATIONARY = True  # count a still person as present
DEFAULT_SENSOR_PROGRAM_GATES = True   # also push the range into the sensor

# --- Display power ---
DEFAULT_DISPLAY_OFF_TIMEOUT = 60      # seconds of no presence before display off
DEFAULT_PRESENCE_CONFIRM_MS = 300     # presence must hold this long to wake
DEFAULT_DISPLAY_BACKEND = "auto"      # auto | wlopm | xset | wlr-randr | …
DEFAULT_DISPLAY_OUTPUT = "auto"       # auto | HDMI-A-1 | HDMI-1 | …
DEFAULT_DISPLAY_ROTATE = "normal"     # normal | left | right | inverted

# How the panel is actually switched off. Combines with commas like
# display_backend does, so "pwm,hdmi" ramps the backlight down *and* drops the
# output. "hdmi" keeps the existing autodetected behaviour unchanged.
#   hdmi | pwm | css | none
DEFAULT_DISPLAY_OFF_STRATEGY = "hdmi"

# --- PWM backlight (opt-in; needs a dtoverlay and a wire) ---
# The one documented exception to preferring autodetection: which pin carries
# the backlight signal depends entirely on how the user wired their board, so
# there is nothing to detect. See presence/pwm.py for the tap.
DEFAULT_PWM_GPIO = 18                 # BCM; hardware PWM on 12, 13, 18 or 19
DEFAULT_PWM_FREQUENCY_HZ = 2000       # below ~200 Hz the flicker is visible
DEFAULT_PWM_GAMMA = 2.2               # perceived brightness ≈ duty^(1/2.2)
DEFAULT_PWM_MIN_DUTY_PERCENT = 3      # many LED drivers cut out below ~2–5%
DEFAULT_PWM_FADE_MS = 800             # ramp rather than step
DEFAULT_PWM_ENABLE_GPIO = -1          # BL_EN hard off; -1 means not wired
DEFAULT_PWM_ENABLE_ACTIVE_HIGH = True

# --- Brightness (single perceptual 0–100 value; see presence/panel.py) ---
DEFAULT_BRIGHTNESS = 100              # normal, awake brightness

# --- Dim before off ---
# The panel drops to DIM_LEVEL for DIM_SECONDS after the hold expires, and
# only then goes dark. Any presence during the dim cancels it instantly.
DEFAULT_DIM_SECONDS = 20
DEFAULT_DIM_LEVEL = 25                # perceptual 0–100

# --- Schedule / night mode ---
# schedule_start..schedule_end is the window in which the display behaves
# normally. night_mode decides what happens *outside* it:
#   off         no restriction at all
#   dim_clock   presence wakes a dim clock rather than the full layout
#   never_wake  the panel stays dark however much you wave at it
DEFAULT_SCHEDULE_ENABLED = False      # legacy; migrated into night_mode
DEFAULT_SCHEDULE_START = "06:30"
DEFAULT_SCHEDULE_END = "23:00"
DEFAULT_NIGHT_MODE = "off"            # off | dim_clock | never_wake
DEFAULT_NIGHT_BRIGHTNESS = 15         # perceptual 0–100, for dim_clock

# --- Kiosk ---
KIOSK_URL = os.environ.get("WALLCAL_KIOSK_URL", f"http://localhost:{PORT}/")
# "auto" picks by board: software rendering on a Pi 3 or older, where
# Chromium's GPU path often brings up a window that never paints (a white
# screen with just a cursor), and Chromium's own choice on a Pi 4/5.
# "off" and "on" force it either way.
DEFAULT_KIOSK_GPU = "auto"            # auto | on | off

# --- UI defaults ---
DEFAULT_THEME = "dark"                # "dark" or "light"
DEFAULT_ANIMATIONS_ENABLED = False    # performance first
DEFAULT_CALENDAR_VIEW = "grid"        # "grid" (squares) or "list"
DEFAULT_LOCALE = "en"
