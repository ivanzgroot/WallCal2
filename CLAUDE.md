# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A wall-mounted calendar appliance for a Raspberry Pi. A Flask app renders CalDAV
events full-screen in a kiosk browser; an HLK-LD2410C 24 GHz presence radar on
the GPIO header decides when the panel wakes, dims and sleeps. `README.md` is
the user-facing manual and is unusually complete — read it before changing
hardware, install or troubleshooting behaviour, and update it when that
behaviour changes.

## Commands

`wallcal.sh` is the only entry point. It resolves a Python interpreter
(`.venv/bin/python`, else `python3`) with `PYTHONPATH` set to the repo, so
prefer it over invoking Python directly.

```bash
./wallcal.sh selftest              # run every suite in tests/
./wallcal.sh doctor [--fix]        # ~30 checks across hardware, services, kiosk
./wallcal.sh status --json         # services, presence, display, sensor
./wallcal.sh logs presence -f      # watch the daemon decide
./wallcal.sh config list|get|set   # the same settings the web UI writes
./wallcal.sh kiosk diagnose        # why is the panel blank
```

Run a single suite (each is a standalone script, not pytest — no runner, no
fixtures, `sys.exit(1)` on failure):

```bash
python3 tests/test_density.py
```

Dev setup on a non-Pi machine:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install py-mini-racer esprima   # dev extras, commented out in requirements.txt
.venv/bin/python app.py                       # serves on :5005
```

The JS suites (`test_widgets_js.py`, `test_settings_js.py`) hard-fail on import
without `py_mini_racer`; `selftest` warns about that up front. Off a Pi the
Flask app runs normally, and the daemon still starts — `build_sensor()` raises,
`_connect_sensor()` records the error and carries on with `sensor = None`, and
the display backends probe to nothing — so both are exercisable on a laptop,
just blind.

Tests point `WALLCAL_DB_PATH` and `WALLCAL_RUNTIME_DIR` at a tempdir; every
`WALLCAL_*` override lives in `config.py`.

## Architecture

### Two processes that never touch each other's domain

`wallcal.service` runs `app.py` (Flask + the CalDAV poller thread + the prewake
scheduler). `wallcal-presence.service` runs `presence/daemon.py` (radar, display
power, brightness, density). **The web app never switches the panel.** It
publishes intent and the daemon acts on it, so the two can never fight over the
display.

The channel is two JSON files in `/run/wallcal` (tmpfs; falls back to
`data/run/`), written atomically via `presence/runtime.py`:

- `presence.json` — daemon → web: sensor reading, display state, density,
  brightness, thresholds. Written a few times a second; `read_state()` marks the
  daemon dead if the file is older than 15 s.
- `command.json` — web → daemon: `override`, `wake_until`, `pause_until`,
  `reload_seq`, `rescan_seq`, `wake_plan_*`.

Anything time-limited in `command.json` is an **expiry timestamp, not a flag**,
so a tool that dies mid-run cannot wedge the daemon. `pause_until` is how CLI
sensor commands take the UART from the daemon (only one process can hold a
serial port); `request_pause()` / `clear_pause()` and the `sensor_access()`
context manager in `presence/cli.py` are the whole protocol.

### Browser is a renderer, not a decision-maker

`presence.json` reaches `static/js/wall.js` over SSE (`/api/presence/stream`),
with `/api/presence/live` polling as the fallback if the stream never opens.
The browser sets `data-density`, `data-power`, `data-mode` and `--dim-alpha` on
`#app` and lets CSS do the rest.

- **Density (FAR/NEAR)** is decided in `daemon._update_density()` — hysteresis
  (separate enter/exit thresholds) plus a debounce, asymmetric on purpose:
  approaching is ~250 ms, leaving is ~1500 ms. It used to live in the browser
  and was moved because polling was the latency ceiling. `tests/test_density.py`
  drives the real daemon object with a synthetic clock.
- **Brightness** is one perceptual 0–100 value, computed only in
  `presence/panel.py`. Under the `pwm` strategy the hardware is already at that
  level; otherwise the browser applies it as a black scrim. Nothing else in the
  project computes a brightness.
- **Widget visibility** is decided server-side in `widgets.py`, not in JS — the
  rule determines whether a feed is worth fetching at all, and the shapes
  returned by `/api/widgets` are final so a widget can never render half-decided.

### Display power is layered

`display_off_strategy` (`hdmi` | `pwm` | `css` | `none`, comma-combinable) picks
the mechanism; `presence/panel.py` fans out to `presence/display.py` (backend
autodetection: wlopm, xset, wlr-randr, xrandr, backlight, vcgencmd, cec, fbcon)
and `presence/pwm.py` (hardware PWM on the backlight line). The daemon starts at
`multi-user.target`, before any compositor exists, so it keeps re-probing while
on a session-less fallback and upgrades itself once a real backend appears.

### Feeds always render from cache

`feeds.py` providers write to SQLite on fetch; the page renders from that cache
and a failed refresh keeps the last good payload with a freshness marker. A
fetch is never in the path of a render — the Pi's WLAN drops and these APIs time
out, and neither should be visible on a wall. Providers sit behind a small
interface (Transitous/db-rest for transit) mirroring `SensorSource` in
`presence/daemon.py`.

`widgets.Refresher` is what enforces that for widgets: it runs `collect()` with
`allow_fetch=True` on its own thread (and stands down while the panel is dark),
while `/api/widgets` runs the same rules with `allow_fetch=False` and is
answered from SQLite alone. Both callers share one copy of the visibility
rules — that is the point of the flag.

### One clock, named in settings

Events are cached as **UTC**; `localtime.py` is the only thing that converts
them back. `localtime.parse_event_start()` returns an aware datetime in the
zone the `timezone` setting names ("auto" = the machine's), and all-day
entries come back as local midnight on the date written rather than as an
instant. Everything that compares a cached event against the clock —
`prewake.py`, `widgets.py`, `app._abfall_payload` — goes through it; they each
used to strip the Z and compare against `datetime.now()`, which put the
anticipatory wake two hours out and inverted the travel widget's window.

Recurrences are the exception that proves it: `caldav_poller._expand_rrule`
expands in the zone the event's own DTSTART named, not the wall's and not UTC,
then converts each occurrence — otherwise a weekly 09:00 shifts to 08:00 the
moment the clocks change. `tests/test_time.py` pins all of this to a real DST
boundary.

## Settings: one key, five places

`_SETTINGS_DEFAULTS` in `database.py` is merged in at **read** time, so an
untouched setting has no row at all. Changing a default in `config.py` therefore
changes behaviour on every existing installation, invisibly. Adding or changing
a setting means:

1. `config.py` — the `DEFAULT_*` constant, with a comment on *why* that value.
2. `database.py` — the entry in `_SETTINGS_DEFAULTS` (which defines
   `SETTABLE_KEYS`, the API's write allowlist).
3. `templates/settings.html` — a `data-setting=` / `data-seg=` control.
4. A `@migration(version, description)` in `database.py` if you changed an
   existing default — pin the old value as a real row for existing installs so
   only fresh ones pick up the new one. `_is_fresh_install()` is resolved once,
   before any migration writes.
5. `presence/daemon.py` `Settings` if the daemon consumes it (and the relevant
   `*_signature` property, which is what triggers a re-open of the sensor,
   display or gates on change).

`tests/test_import.py` enforces that every `data-setting` in the settings page
is in `SETTABLE_KEYS`, and that every `$('id')` reached for by `wall.js` /
`settings.js` exists in the corresponding template. It is the cheapest guard in
the repo — run it after touching any template or its script.

GPIO pins are all settings, checked for collisions through one registry
(`database.pin_usage()` / `pin_conflicts()`), so a pin added later is covered
without touching the checker.

## Conventions

- **No authentication, by design.** LAN-only appliance; the wall shows a QR code
  pointing at an unauthenticated `/settings`. Don't add auth-shaped half
  measures, and don't widen exposure beyond the LAN.
- **CalDAV credentials are encrypted** in SQLite with a key derived from
  `SECRET_KEY`, which ships with a known default. `data/` and `*.db` are
  gitignored.
- **LF line endings are pinned** in `.gitattributes` — a CRLF checkout gives the
  Pi `bad interpreter: /bin/bash^M`.
- **UI copy is German**; code, comments and commit messages are English.
- Static assets are cache-stamped by mtime via the `static_url()` template
  helper — the kiosk browser runs for months behind an aggressive cache with
  nobody there to force-reload.
- Comments here explain *why*, often citing the failure that motivated the code
  (a browned-out sensor, a white kiosk screen, a flickering layout). Match that
  register; don't add comments that restate the line below them.
- Commit subjects are `area: imperative sentence` — `presence:`, `web:`, `db:`,
  `settings:`, `wallcal.sh:`, `docs:`.
- `wallcal.sh` re-execs itself as the owning user for any command that writes
  the database or opens the sensor (`USER_LEVEL_COMMANDS`), so `sudo` never
  leaves root-owned files the services cannot write.
