"""
WallCal Database Layer
SQLite-based persistence for settings, calendar configs, and cached events.
All data survives reboots and container restarts via the mounted data volume.
"""

import sqlite3
import json
import os
import logging
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from cryptography.fernet import Fernet

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Encryption helpers for CalDAV passwords
# ---------------------------------------------------------------------------

def _get_fernet():
    """Derive a Fernet key from the app secret. Stable across restarts."""
    import hashlib
    import base64
    key = hashlib.sha256(config.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_value(plaintext):
    """Encrypt a string value for safe storage."""
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext):
    """Decrypt a stored value. Returns empty string on failure."""
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except Exception:
        logger.warning("Failed to decrypt value — returning empty string")
        return ""


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def _ensure_data_dir():
    os.makedirs(os.path.dirname(config.DATABASE_PATH), exist_ok=True)


@contextmanager
def get_db():
    """Context manager yielding a SQLite connection with row_factory."""
    _ensure_data_dir()
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------

def init_db():
    """Create tables if they don't exist."""
    _ensure_data_dir()
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS calendars (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                caldav_url  TEXT NOT NULL,
                username    TEXT NOT NULL DEFAULT '',
                password    TEXT NOT NULL DEFAULT '',
                provider    TEXT NOT NULL DEFAULT 'nextcloud',
                color       TEXT NOT NULL DEFAULT '#00d4aa',
                enabled     INTEGER NOT NULL DEFAULT 1,
                discovered  INTEGER NOT NULL DEFAULT 0,
                cal_path    TEXT NOT NULL DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS events_cache (
                uid           TEXT NOT NULL,
                calendar_id   INTEGER NOT NULL,
                summary       TEXT NOT NULL DEFAULT '',
                description   TEXT DEFAULT '',
                location      TEXT DEFAULT '',
                dtstart       TEXT NOT NULL,
                dtend         TEXT,
                all_day       INTEGER DEFAULT 0,
                color         TEXT DEFAULT '#00d4aa',
                recurrence_id TEXT DEFAULT '',
                cached_at     TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (uid, calendar_id, recurrence_id),
                FOREIGN KEY (calendar_id) REFERENCES calendars(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_events_dtstart
                ON events_cache(dtstart);
            CREATE INDEX IF NOT EXISTS idx_events_calendar
                ON events_cache(calendar_id);
        """)
        _run_migrations(conn)
    logger.info("Database initialized at %s", config.DATABASE_PATH)


# ---------------------------------------------------------------------------
# Schema migrations
#
# These exist because of how _SETTINGS_DEFAULTS works: it is merged in at
# *read* time rather than written out at install, so a setting nobody has
# touched has no row at all. Changing a default therefore changes behaviour on
# every existing installation the moment the new code lands — silently, and
# invisibly in a diff that only touches config.py.
#
# A migration's job is usually to pin the old value down as a real row first,
# so only fresh installs pick up the new default.
# ---------------------------------------------------------------------------

_MIGRATIONS = []


def migration(version, description):
    """Register a schema migration. Applied once, in version order.

    The wrapped function is called as ``fn(conn, fresh_install)``.
    """
    def register(fn):
        _MIGRATIONS.append((int(version), str(description), fn))
        return fn
    return register


def schema_version(conn=None) -> int:
    """Highest migration applied to this database. 0 means none."""
    if conn is None:
        with get_db() as own:
            return schema_version(own)
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'schema_version'"
    ).fetchone()
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


def _is_fresh_install(conn) -> bool:
    """True when this database has never been used.

    Both tables are empty only on a genuinely new install. The distinction
    matters for any migration that changes a default: an upgrade must keep the
    behaviour it already had, a new install should get the new value.
    """
    for table in ("settings", "calendars"):
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            return False
    return True


def _run_migrations(conn) -> None:
    current = schema_version(conn)
    pending = sorted(m for m in _MIGRATIONS if m[0] > current)
    if not pending:
        return

    # Resolved once, before anything writes: the first migration to store a
    # row makes the settings table non-empty, and the answer would then flip
    # for every migration after it.
    fresh = _is_fresh_install(conn)
    logger.info("Migrating database from schema %d (%s install)",
                current, "new" if fresh else "existing")

    for version, description, fn in pending:
        fn(conn, fresh)
        conn.execute("""
            INSERT INTO settings (key, value, updated_at)
            VALUES ('schema_version', ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
        """, (str(version),))
        logger.info("  applied %d: %s", version, description)


def _has_setting(conn, key) -> bool:
    """True when ``key`` has a stored row, as opposed to inheriting a default."""
    return conn.execute(
        "SELECT 1 FROM settings WHERE key = ?", (key,)
    ).fetchone() is not None


def _put_setting(conn, key, value) -> None:
    """Write a setting from inside a migration, reusing its transaction."""
    conn.execute("""
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at = excluded.updated_at
    """, (key, str(value)))


#: The pin the sensor's OUT line used to default to, before the backlight
#: claimed GPIO18. Pinned here rather than read from config so that a later
#: change to the default cannot rewrite what this migration means.
_LEGACY_SENSOR_GPIO_PIN = 18


@migration(1, "keep GPIO18 for the sensor on existing installs")
def _m1_sensor_gpio_pin(conn, fresh):
    """Stop the GPIO18 -> GPIO23 default change from moving anyone's wiring.

    An install that never opened the sensor settings has no sensor_gpio_pin
    row, so it inherits whatever config.py says. Changing that default would
    silently point the daemon at a pin with no wire on it. Writing the old
    value down as a real row leaves existing hardware working and lets only
    new installs pick up GPIO23.
    """
    if fresh or _has_setting(conn, "sensor_gpio_pin"):
        return
    _put_setting(conn, "sensor_gpio_pin", _LEGACY_SENSOR_GPIO_PIN)
    logger.info("  sensor OUT pin pinned to GPIO%d (was the old default)",
                _LEGACY_SENSOR_GPIO_PIN)


# ---------------------------------------------------------------------------
# Settings CRUD
# ---------------------------------------------------------------------------

_SETTINGS_DEFAULTS = {
    "poll_interval_minutes": str(config.DEFAULT_POLL_INTERVAL_MINUTES),
    "theme": config.DEFAULT_THEME,
    "animations_enabled": str(config.DEFAULT_ANIMATIONS_ENABLED).lower(),
    "calendar_view": config.DEFAULT_CALENDAR_VIEW,
    "locale": config.DEFAULT_LOCALE,
    # Sensor
    "sensor_mode": config.DEFAULT_SENSOR_MODE,
    "sensor_gpio_pin": str(config.DEFAULT_SENSOR_GPIO_PIN),
    "sensor_gpio_active_high": str(config.DEFAULT_SENSOR_GPIO_ACTIVE_HIGH).lower(),
    "sensor_uart_port": config.DEFAULT_SENSOR_UART_PORT,
    "sensor_uart_baud": str(config.DEFAULT_SENSOR_UART_BAUD),
    "sensor_distance_max_cm": str(config.DEFAULT_SENSOR_DISTANCE_MAX_CM),
    "sensor_distance_min_cm": str(config.DEFAULT_SENSOR_DISTANCE_MIN_CM),
    "sensor_hysteresis_cm": str(config.DEFAULT_SENSOR_HYSTERESIS_CM),
    "sensor_moving_energy_min": str(config.DEFAULT_SENSOR_MOVING_ENERGY_MIN),
    "sensor_stationary_energy_min": str(config.DEFAULT_SENSOR_STATIONARY_ENERGY_MIN),
    "sensor_use_stationary": str(config.DEFAULT_SENSOR_USE_STATIONARY).lower(),
    "sensor_program_gates": str(config.DEFAULT_SENSOR_PROGRAM_GATES).lower(),
    # Display power
    "display_off_timeout": str(config.DEFAULT_DISPLAY_OFF_TIMEOUT),
    "presence_confirm_ms": str(config.DEFAULT_PRESENCE_CONFIRM_MS),
    "display_backend": config.DEFAULT_DISPLAY_BACKEND,
    "display_output": config.DEFAULT_DISPLAY_OUTPUT,
    "display_rotate": config.DEFAULT_DISPLAY_ROTATE,
    "display_off_strategy": config.DEFAULT_DISPLAY_OFF_STRATEGY,
    "kiosk_gpu": config.DEFAULT_KIOSK_GPU,
    # PWM backlight
    "pwm_gpio": str(config.DEFAULT_PWM_GPIO),
    "pwm_frequency_hz": str(config.DEFAULT_PWM_FREQUENCY_HZ),
    "pwm_gamma": str(config.DEFAULT_PWM_GAMMA),
    "pwm_min_duty_percent": str(config.DEFAULT_PWM_MIN_DUTY_PERCENT),
    "pwm_fade_ms": str(config.DEFAULT_PWM_FADE_MS),
    "pwm_enable_gpio": str(config.DEFAULT_PWM_ENABLE_GPIO),
    "pwm_enable_active_high": str(config.DEFAULT_PWM_ENABLE_ACTIVE_HIGH).lower(),
    # Brightness
    "brightness": str(config.DEFAULT_BRIGHTNESS),
    # Schedule
    "schedule_enabled": str(config.DEFAULT_SCHEDULE_ENABLED).lower(),
    "schedule_start": config.DEFAULT_SCHEDULE_START,
    "schedule_end": config.DEFAULT_SCHEDULE_END,
}

#: Every setting the API is willing to write. Kept next to the defaults so
#: the two never drift apart.
SETTABLE_KEYS = frozenset(_SETTINGS_DEFAULTS)


# ---------------------------------------------------------------------------
# GPIO pin registry
#
# Every pin the project drives is a setting, and collisions are checked
# through this one registry rather than by knowing about any particular pair.
# A pin setting added later is covered without touching the checker.
# ---------------------------------------------------------------------------

PinUse = namedtuple("PinUse", "key label pin state")

#: state is "on" (definitely driven), "maybe" (driven only in some
#: configurations) or "off". The distinction is what stops doctor shouting
#: about the sensor OUT pin colliding with the backlight on the UART setups
#: where OUT is not connected to anything at all.
def _strategy_has(settings, name):
    spec = str(settings.get("display_off_strategy", "hdmi") or "hdmi")
    return name in [p.strip().lower() for p in spec.split(",")]


_PIN_REGISTRY = (
    ("sensor_gpio_pin", "Sensor OUT",
     lambda s: {"gpio": "on", "auto": "maybe"}.get(
         str(s.get("sensor_mode", "auto")).lower(), "off")),
    ("pwm_gpio", "Backlight PWM",
     lambda s: "on" if _strategy_has(s, "pwm") else "off"),
    ("pwm_enable_gpio", "Backlight BL_EN",
     lambda s: "on" if _strategy_has(s, "pwm") else "off"),
)


def pin_usage(settings=None):
    """Every configured GPIO pin, with whether it is actually in use."""
    settings = settings if settings is not None else get_all_settings()
    used = []
    for key, label, state_of in _PIN_REGISTRY:
        try:
            pin = int(float(str(settings.get(key, "")).strip()))
        except (TypeError, ValueError):
            continue
        if pin < 0:
            continue          # negative means "not wired", the opt-out value
        used.append(PinUse(key, label, pin, state_of(settings)))
    return used


def pin_conflicts(settings=None):
    """Settings pointing at the same BCM pin, worst first.

    Severity is "error" when both pins are definitely driven and "warning"
    when at least one is only conditional — sharing a pin with something that
    is not currently wired up is worth mentioning, not worth failing over.
    """
    entries = [p for p in pin_usage(settings) if p.state != "off"]
    conflicts = []
    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            if a.pin != b.pin:
                continue
            severity = "error" if a.state == b.state == "on" else "warning"
            conflicts.append((severity, a, b))
    conflicts.sort(key=lambda c: c[0] != "error")
    return conflicts


def get_setting(key, default=None):
    """Get a single setting value. Falls back to built-in defaults."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    if row:
        return row["value"]
    if default is not None:
        return default
    return _SETTINGS_DEFAULTS.get(key, "")


def set_setting(key, value):
    """Upsert a setting."""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
        """, (key, str(value)))


def get_all_settings():
    """Return all settings merged with defaults."""
    settings = dict(_SETTINGS_DEFAULTS)
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    for row in rows:
        settings[row["key"]] = row["value"]
    return settings


def set_many_settings(data):
    """Bulk upsert settings from a dict."""
    with get_db() as conn:
        for key, value in data.items():
            conn.execute("""
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at
            """, (key, str(value)))


# ---------------------------------------------------------------------------
# Calendars CRUD
# ---------------------------------------------------------------------------

def add_calendar(name, caldav_url, username="", password="",
                 provider="nextcloud", color="#00d4aa", enabled=True,
                 cal_path="", discovered=False):
    """Add a new calendar source. Password is stored encrypted."""
    enc_pw = encrypt_value(password) if password else ""
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO calendars (name, caldav_url, username, password,
                                   provider, color, enabled, cal_path, discovered)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, caldav_url, username, enc_pw, provider, color,
              1 if enabled else 0, cal_path, 1 if discovered else 0))
        return cursor.lastrowid


def update_calendar(cal_id, **kwargs):
    """Update calendar fields. Encrypts password if provided."""
    if "password" in kwargs and kwargs["password"]:
        kwargs["password"] = encrypt_value(kwargs["password"])
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()

    allowed = {"name", "caldav_url", "username", "password", "provider",
               "color", "enabled", "cal_path", "discovered", "updated_at"}
    filtered = {k: v for k, v in kwargs.items() if k in allowed}
    if not filtered:
        return

    set_clause = ", ".join(f"{k} = ?" for k in filtered)
    values = list(filtered.values()) + [cal_id]

    with get_db() as conn:
        conn.execute(
            f"UPDATE calendars SET {set_clause} WHERE id = ?", values
        )


def delete_calendar(cal_id):
    """Delete a calendar and its cached events."""
    with get_db() as conn:
        conn.execute("DELETE FROM calendars WHERE id = ?", (cal_id,))


def get_calendar(cal_id):
    """Get a single calendar by ID, decrypting password."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM calendars WHERE id = ?", (cal_id,)
        ).fetchone()
    if row:
        return _calendar_row_to_dict(row)
    return None


def get_all_calendars(include_password=False):
    """Get all calendars. Passwords masked unless explicitly requested."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM calendars ORDER BY id"
        ).fetchall()
    result = []
    for row in rows:
        d = _calendar_row_to_dict(row)
        if not include_password:
            d["password"] = "••••••••" if d["password"] else ""
        result.append(d)
    return result


def get_enabled_calendars():
    """Get calendars that are enabled, with decrypted passwords."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM calendars WHERE enabled = 1 ORDER BY id"
        ).fetchall()
    return [_calendar_row_to_dict(row) for row in rows]


def _calendar_row_to_dict(row):
    d = dict(row)
    d["password"] = decrypt_value(d["password"])
    d["enabled"] = bool(d["enabled"])
    d["discovered"] = bool(d["discovered"])
    return d


# ---------------------------------------------------------------------------
# Events cache
# ---------------------------------------------------------------------------

def cache_events(calendar_id, events):
    """Replace cached events for a calendar with a fresh list.

    Each event dict should have: uid, summary, dtstart, dtend,
    all_day, description, location, color, recurrence_id (optional).
    """
    with get_db() as conn:
        conn.execute(
            "DELETE FROM events_cache WHERE calendar_id = ?", (calendar_id,)
        )
        for ev in events:
            conn.execute("""
                INSERT OR REPLACE INTO events_cache
                    (uid, calendar_id, summary, description, location,
                     dtstart, dtend, all_day, color, recurrence_id, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (
                ev.get("uid", ""),
                calendar_id,
                ev.get("summary", ""),
                ev.get("description", ""),
                ev.get("location", ""),
                ev.get("dtstart", ""),
                ev.get("dtend", ""),
                1 if ev.get("all_day") else 0,
                ev.get("color", "#00d4aa"),
                ev.get("recurrence_id", ""),
            ))
    logger.debug("Cached %d events for calendar %d", len(events), calendar_id)


def get_cached_events(days=30):
    """Return all cached events within the next N days from enabled calendars."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    end = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

    with get_db() as conn:
        rows = conn.execute("""
            SELECT e.*, c.name as calendar_name, c.color as calendar_color
            FROM events_cache e
            JOIN calendars c ON e.calendar_id = c.id
            WHERE c.enabled = 1
              AND e.dtstart <= ?
              AND (e.dtend >= ? OR e.dtend IS NULL OR e.dtend = '')
            ORDER BY e.dtstart ASC
        """, (end, now)).fetchall()

    # Also get all-day events that start today or later
    with get_db() as conn:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        end_date = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
        allday_rows = conn.execute("""
            SELECT e.*, c.name as calendar_name, c.color as calendar_color
            FROM events_cache e
            JOIN calendars c ON e.calendar_id = c.id
            WHERE c.enabled = 1
              AND e.all_day = 1
              AND e.dtstart >= ?
              AND e.dtstart <= ?
            ORDER BY e.dtstart ASC
        """, (today, end_date)).fetchall()

    # Merge and deduplicate
    seen = set()
    result = []
    for row in list(rows) + list(allday_rows):
        key = (row["uid"], row["calendar_id"], row["recurrence_id"])
        if key not in seen:
            seen.add(key)
            d = dict(row)
            d["all_day"] = bool(d["all_day"])
            # Use the live calendar color (from JOIN), not the cached snapshot
            if d.get("calendar_color"):
                d["color"] = d["calendar_color"]
            result.append(d)

    result.sort(key=lambda x: x["dtstart"])
    return result


def get_last_poll_time():
    """Get timestamp of most recent cache entry."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(cached_at) as last_poll FROM events_cache"
        ).fetchone()
    if row and row["last_poll"]:
        return row["last_poll"]
    return None
