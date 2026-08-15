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
DEFAULT_SENSOR_GPIO_PIN = 18          # BCM numbering — LD2410C OUT pin
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

# --- Schedule (display stays dark outside this window when enabled) ---
DEFAULT_SCHEDULE_ENABLED = False
DEFAULT_SCHEDULE_START = "06:30"
DEFAULT_SCHEDULE_END = "23:00"

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
