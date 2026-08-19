# WallCal

A wall-mounted calendar for the Raspberry Pi that only lights up when someone
walks over to read it.

A Flask app pulls events from any CalDAV server (Nextcloud, iCloud, …) and
renders them full-screen in a kiosk browser. An HLK-LD2410C 24 GHz presence
radar on the GPIO header decides when the panel wakes and when it sleeps —
with the detection distance adjustable from the web UI, live, while you stand
in front of it.

Everything is driven from one entry point:

```bash
./wallcal.sh help
```

---

## Quick start

On a fresh Raspberry Pi OS (full desktop) install:

```bash
git clone <your-repo> ~/WallCal        # or copy the folder over
cd ~/WallCal
chmod +x wallcal.sh
sudo ./wallcal.sh install
```

The installer walks through ten steps and prompts before anything
irreversible. When it finishes, reboot; the Pi comes back logged in, running
the calendar full-screen, with the presence daemon controlling the panel.

Then open `http://<pi-address>:5005/settings` from any machine on the network
(or `http://<hostname>.local:5005/settings`) and add your calendar.

The wall display also shows a small QR code pointing at that page, so the usual
way in is to scan it off the wall with a phone.

> **Security note.** That QR code puts an unauthenticated settings URL on a
> wall. WallCal has no authentication by design — it is a LAN-only appliance,
> and the posture is the same one the rest of the project documents. Do not
> expose port 5005 to the internet. Turn the QR widget off under
> **Widgets → QR-Code** if the wall is somewhere public.

> **Editing on Windows?** Shell scripts need Unix line endings or the Pi
> reports `bad interpreter: /bin/bash^M`. The installer normalises them
> automatically, but if `wallcal.sh` itself will not start, run
> `sed -i 's/\r$//' wallcal.sh` first.

---

## Wiring the sensor

The HLK-LD2410C connects to the 40-pin header. Two of the four wires are
optional depending on which mode you use — UART gives distance and signal
strength, the OUT pin gives a bare present/not-present signal.

| Sensor pin | Pi pin | GPIO       | Needed for |
|------------|--------|------------|------------|
| `VCC`      | 4      | 5 V        | always     |
| `GND`      | 6      | GND        | always     |
| `TX`       | 10     | GPIO15 RXD | UART mode  |
| `RX`       | 8      | GPIO14 TXD | UART mode  |
| `OUT`      | 16     | GPIO23     | GPIO mode  |

Wire all five and leave the mode on **auto** — WallCal finds the radar on
serial and falls back to the OUT pin if it cannot.

> **`OUT` moved from GPIO18 to GPIO23.** GPIO18 is one of only four pins with
> hardware PWM and is the default for the [backlight](#backlight-pwm); `OUT`
> can live anywhere. Existing installations keep GPIO18 — the setting is
> migrated on upgrade, so nothing needs rewiring. It only matters if you use
> **GPIO sensor mode *and* the PWM backlight**, in which case move the `OUT`
> wire to pin 16. In UART mode, the default, `OUT` is unused and the two never
> collide.
>
> The pin is a setting either way: `./wallcal.sh config set sensor_gpio_pin=23`.
> `doctor` checks every configured pin against every other and tells you if two
> of them end up on the same one.

### Why the installer offers to disable Bluetooth

The LD2410C talks at **256000 baud**. On a Pi 3B+ the header pins are wired to
the *mini* UART by default, whose baud rate is tied to the core clock and
cannot hold that rate reliably; the good PL011 is reserved for the on-board
Bluetooth. The installer sets `dtoverlay=disable-bt` so the PL011 serves the
header instead.

If you need Bluetooth, run `sudo ./wallcal.sh install --keep-bluetooth` — that
uses `dtoverlay=miniuart-bt` to swap them around instead. Or skip the UART step
entirely (`--skip uart`) and use GPIO mode, which works fine at any clock.

Both changes are written into `/boot/firmware/config.txt` inside a marked
block, with a `.wallcal.bak` alongside, and are removed by
`./wallcal.sh uninstall --revert-boot`.

---

## Tuning the detection

The fastest way to get the distance right is to stand where you want the
calendar to wake up and let it measure. Either from the web UI —
**⚙ → Sensor → Calibrate by standing there**, which counts you down, shows what
the radar sees live, and offers the result for one click — or from the
terminal:

```bash
./wallcal.sh sensor calibrate --apply
```

It counts down first so you can walk over from wherever you typed the command
(`--delay 15` if the Pi is further away), then samples for 20 seconds and
writes the suggested distance and sensitivity gates.

For the duration it opens the radar to its full 6 m range and puts the setting
back afterwards — otherwise calibration could only ever confirm the threshold
already configured, never discover that you want a longer one.

To watch what the radar sees in real time:

```bash
./wallcal.sh sensor monitor
```

```
state              dist    move d/e     still d/e   meter
moving              118 cm   118/72       0/0        ██████·····|·············
moving+stationary    95 cm    95/64     210/38       █████······|·············
none                  0 cm      0/0        0/0       ···········|·············
```

The `|` marks the configured wake threshold. Everything is also live in the
web UI under **⚙ → Sensor**, which shows the same meter, the current display
state, and sliders for every threshold — changes apply within a couple of
seconds without restarting anything.

### What each setting does

| Setting | Effect |
|---|---|
| **Wake distance** | Someone closer than this wakes the panel. |
| **Display off timeout** | How long the panel stays on after the last detection. |
| **Count a motionless person as present** | Off means only movement counts — fewer false wakes, but it sleeps while you stand still reading. |
| **Sensitivity gates** | Minimum radar signal energy (0–100). Lower is more sensitive. |
| **Release margin** | Extra distance tolerated before presence drops, so someone hovering at the edge of range does not make it flicker. |
| **Confirm delay** | Presence must hold this long before waking — filters single-frame radar glitches. |
| **Dim before off** | The panel drops to a dim level for a while before going dark, so you get a chance to move and cancel it. Set the duration to 0 to switch off outright. |
| **Nah/Fern-Modus** | Every value names the layout you get: `auto` switches by distance when the usable band is wide enough, `on` always switches, `near` and `far` pin one layout. (`off` is the old spelling of `near` and still works.) |
| **Zeitzone** | The clock, event times, quiet hours, the ÖPNV windows and the Abfall banner all read from this one setting. `auto` follows the Pi's own zone — worth naming explicitly, since several Pi OS images ship set to UTC. |
| **Quiet hours** | A window in which the display behaves normally. **Night mode** decides what happens outside it: nothing (`off`), presence wakes a dim clock only (`dim clock`), or the panel never wakes at all (`never wake`). |

When the sensor is on UART, WallCal also programs the threshold into the
radar's own detection gates, so it stops reporting distant targets at all.
Gates are 0.75 m wide, so the exact centimetre threshold is still applied in
software on top.

---

## Display power

### Choosing how the panel goes off

Not every panel can be switched off by the Pi. A monitor or TV honours DPMS or
CEC; a bare HDMI→eDP driver board of the RTD2556 class does not — its backlight
stays lit whatever the Pi does with its output. So *how* the display goes dark
is a setting rather than an assumption:

| Strategy | Mechanism | Suits |
|---|---|---|
| `hdmi` | The backends below, autodetected as always | Monitors and TVs that honour DPMS/CEC |
| `pwm` | Hardware PWM on the panel's backlight line | Bare driver boards |
| `css` | Browser-side dimming; the panel stays powered | Anything, with no wiring at all |
| `none` | Never power off — show a screensaver instead | Always-on installations |

`hdmi` is the default and behaves exactly as it always has. Strategies combine
with commas, the same way display backends do:

```bash
./wallcal.sh display strategy            # what is set now
./wallcal.sh display strategy pwm,hdmi   # ramp the backlight down AND drop the output
./wallcal.sh display strategy css        # no wiring; dim in the browser
```

All four converge on **one brightness value**, 0–100, perceptual. Under `pwm`
it is a duty cycle; otherwise the browser applies it as an overlay. Nothing
else in the project computes a brightness of its own.

### Backlight PWM

Only needed for `pwm`. It is the one thing here that is *not* autodetected,
because which pin carries the signal depends entirely on how you wired it.

Two signals are worth tapping — the first is required, the second is better:

- **`BL_PWM`** — the dimming input on the board's LED driver. Cut the trace (or
  lift the resistor) between the scaler's PWM output and the driver's dim pin,
  then inject the Pi's PWM there.
- **`BL_EN`** — backlight enable. Many LED drivers leak a faint glow at 0% duty;
  a plain GPIO here gives a true hard off. Both signals are levels, so the
  arrangement needs no feedback to stay in step.

> **Do not drive the board's front-panel power button instead.** It is a toggle
> with no deterministic state, it needs the power LED sensed to stay in sync,
> and it cannot dim. PWM is a level and needs none of that.

Hardware PWM is available on four pins, and the device-tree `func` value
differs per pin — a wrong one produces no output and no error:

| GPIO | Channel | Overlay line |
|---|---|---|
| 12 | PWM0 | `dtoverlay=pwm,pin=12,func=4` |
| 13 | PWM1 | `dtoverlay=pwm,pin=13,func=4` |
| **18** | PWM0 | `dtoverlay=pwm,pin=18,func=2` ← default |
| 19 | PWM1 | `dtoverlay=pwm,pin=19,func=2` |

The installer writes the right line for whichever pin is configured. To set it
up and prove the wiring:

```bash
./wallcal.sh display strategy pwm,hdmi
sudo ./wallcal.sh install --only uart    # rewrites the boot config block
sudo reboot
./wallcal.sh display pwm status          # overlay, sysfs, configured pins
./wallcal.sh display pwm test            # sweep 0 -> 100 -> 0
```

`display pwm test` works with the daemon running — it asks it to stand down for
the duration, the same way the sensor tools do.

Everything is tunable: frequency (default 2 kHz — below roughly 200 Hz the
flicker is perceptible), the gamma curve, the minimum duty floor (default 3%,
since many drivers cut out below a few percent) and the fade duration.

**Software PWM is deliberately not supported.** `RPi.GPIO`'s software PWM
jitters under scheduler load and the flicker is visible on a display you look
at every day. If the overlay is missing WallCal says so rather than quietly
falling back to something worse — and the rest of your strategy keeps working
in the meantime.

### Backends for the `hdmi` strategy

The panel is switched by whichever mechanism the Pi actually has. WallCal
probes them all at startup and picks the best available, preferring the ones
that leave the output configured (so the browser layout survives a sleep):

| Backend | Used when |
|---|---|
| `wlopm` | Wayland (labwc/wayfire) — true DPMS |
| `xset` | X11 — true DPMS |
| `wlr-randr` / `xrandr` | fallback; disables the output entirely |
| `backlight` | DSI panels and the official touch display |
| `vcgencmd` | Broadcom firmware display power |
| `cec` | HDMI-CEC standby — for TVs and smart monitors |
| `fbcon` | console framebuffer blanking |

The installer picks the best one available and writes it down, rather than
leaving it to chance: the presence daemon starts at `multi-user.target`, before
any compositor exists, so its own first probe can only ever find the
session-less backends. If the desktop was not up during installation, run it
again afterwards:

```bash
./wallcal.sh display autoselect
```

The daemon also keeps re-probing while it is on a session-less fallback, and
upgrades itself once a real one appears.

```bash
./wallcal.sh display detect     # what is available here
./wallcal.sh display test       # blink the panel to prove it works
./wallcal.sh display backend cec        # pin one
./wallcal.sh display backend xset,cec   # or several at once
./wallcal.sh display rotate left        # portrait wall mount
```

If your monitor ignores DPMS and stays backlit, `xset,cec` is usually the
combination that works: the Pi stops driving the output *and* asks the screen
to go to standby over HDMI.

---

## Day-to-day

```bash
./wallcal.sh status              # services, presence, display, sensor at a glance
./wallcal.sh doctor              # ~30 checks across hardware, services and kiosk
./wallcal.sh doctor --fix        # …and repair what it can
./wallcal.sh logs presence -f    # watch the daemon decide
./wallcal.sh presence on         # force the panel on (until you set it back)
./wallcal.sh presence auto       # hand control back to the sensor
./wallcal.sh kiosk restart       # restart just the browser
./wallcal.sh backup              # snapshot the database
./wallcal.sh update              # pull, reinstall deps, restart
```

Tab completion:

```bash
./wallcal.sh completion | sudo tee /etc/bash_completion.d/wallcal >/dev/null
```

---

## What gets installed

| Component | Purpose |
|---|---|
| `wallcal.service` | The Flask app and CalDAV poller |
| `wallcal-presence.service` | Sensor reading and display power |
| `wallcal-watchdog.timer` | Every 2 min: restarts anything wedged |
| `wallcal-maintenance.timer` | Nightly: backup, vacuum, log trim |
| `~/.config/autostart/wallcal-kiosk.desktop` | Launches the kiosk browser |
| `/etc/X11/xorg.conf.d/10-wallcal-blanking.conf` | Stops X blanking on its own |
| `/etc/systemd/journald.conf.d/wallcal.conf` | Caps the journal at 64 MB |

Raspberry Pi OS has used LXDE/Openbox, Wayfire and labwc as its session across
recent releases, so the installer hooks every autostart mechanism it finds. The
kiosk launcher takes a lock, so only the first one to fire actually starts a
browser.

Removal:

```bash
./wallcal.sh uninstall                  # services and hooks, data kept
./wallcal.sh uninstall --purge --revert-boot   # everything, including the UART changes
```

---

## Troubleshooting

**The screen never wakes.** `./wallcal.sh presence status` — if the daemon is
offline, `./wallcal.sh logs presence -n 50`. If it is running but never sees
you, `./wallcal.sh sensor monitor` and walk in front of the sensor: it prints
the target state per frame, so you can tell detection from silence.

**"Not enough detections" when calibrating.** The failure report tells you
which of these it is — how many frames arrived, how many contained a target,
and what range the sensor is configured for. Usually one of:

- You were not in front of it. Sampling begins after the countdown; use
  `--delay 15` if you are running this over SSH from another room.
- The radar is directional. It should face into the room, not along a wall,
  and not through metal.
- Standing perfectly still can read as an empty room. Wave an arm, or raise
  the sensitivity with `./wallcal.sh sensor sensitivity 20 20` (lower numbers
  are more sensitive).

**The screen never sleeps.** Check the override is `auto`
(`./wallcal.sh presence auto`), then `./wallcal.sh display test` — if the panel
does not blink, no backend has real control and you probably want `cec`.

If the panel is a bare driver board, no backend will ever work: those keep the
backlight lit whatever happens to the HDMI signal. Switch strategy instead —
`./wallcal.sh display strategy css` dims in the browser with no wiring, and
`pwm` genuinely switches the backlight once you have tapped `BL_PWM`.

**The backlight PWM does nothing.** `./wallcal.sh display pwm status` first —
it reports whether the overlay is loaded and whether the pin matches. The usual
causes, in order:

- *No reboot since adding the overlay.* `/sys/class/pwm` does not appear until
  the kernel has loaded it.
- *The wrong `func` for the pin.* `func=2` is for GPIO18/19 and `func=4` for
  GPIO12/13; the wrong one loads without complaint and drives nothing.
- *`display_off_strategy` does not include `pwm`.* Setting the pins alone does
  not enable it.
- *The trace was not cut.* The scaler's own PWM output is still driving the dim
  pin and fighting the Pi's.

**No sensor found.** `./wallcal.sh sensor scan --save` sweeps every port and
baud rate. If there are no serial devices at all, the UART step has not run or
the Pi has not rebooted since: `sudo ./wallcal.sh install --only uart`.

If the scan finds nothing but the wiring looks right, check the sensor's TX
goes to the Pi's **RX** (they cross over), and that the module has 5 V — the
LD2410C browns out and stops transmitting on a sagging supply.

**Permission denied on the serial port.** The user needs the `dialout` group;
group changes only take effect after a full logout: `sudo reboot`.

**Under-voltage warnings from `doctor`.** Take these seriously. A Pi 3B+ with a
sagging supply corrupts SD cards, and the 256000-baud sensor link is one of the
first things to become unreliable. Use a 5 V/3 A supply and a short, thick
cable — most "the sensor works intermittently" reports are really this.

**"multiple access on port".** Only one process can hold a UART. The presence
daemon owns it, so the `sensor` commands ask it to stand down first and hand it
back when they finish; you do not need to stop the service by hand. If a tool
is killed mid-run the daemon takes the port back on its own within a minute.

Sensor and config commands do not need `sudo` — run them as your normal user.
If you do use `sudo`, the script re-executes itself as the owning user anyway,
so the database never ends up root-owned.

**The panel is white / blank.** `./wallcal.sh kiosk diagnose` walks the whole
chain — session type, browser, page size, external resources, processes and the
last 20 log lines. The two usual causes:

- *No internet.* Anything render-blocking loaded from an external host leaves
  the screen white until the request times out. WallCal loads its webfont
  asynchronously and falls back to the fonts already on the Pi, so this should
  not happen — but `kiosk diagnose` will say so if a build regresses.
- *Chromium's GPU path.* On a Pi 3 under Wayland the window can come up and
  never paint. `./wallcal.sh kiosk gpu off` switches to software rendering,
  which is more than enough for a calendar that repaints once a minute.

**The mouse pointer is visible.** The installer builds a transparent cursor
theme and points the compositor at it (`unclutter` is X11-only and does nothing
under labwc). If it is still showing, the session needs a restart to pick up
`~/.config/labwc/environment` — reboot, or `./wallcal.sh install --only kiosk`
then reboot.

**Anything else.** `./wallcal.sh doctor` checks the whole chain — power supply,
disk, dependencies, database integrity, services, HTTP, boot config, groups,
serial devices, display backends and kiosk autostart — and tells you the exact
command to fix each failure.

---

## Layout

```
wallcal.sh              Entry point: install, services, kiosk, sensor, display
app.py                  Flask app and REST API
caldav_poller.py        Background CalDAV sync
database.py             SQLite persistence, migrations, feed cache
config.py               Defaults, all overridable via WALLCAL_* env vars
feeds.py                Transit, weather and travel-time providers
widgets.py              Widget visibility rules, shaping, and feed refresh
prewake.py              Anticipatory wake: publishes the next wake window
localtime.py            The wall's timezone, and reading cached events in it
presence/
  ld2410.py             HLK-LD2410C UART protocol driver
  display.py            Display power backends with autodetection
  pwm.py                Hardware PWM backlight (BL_PWM / BL_EN)
  panel.py              Off-strategy and the single brightness value
  daemon.py             Presence state machine
  runtime.py            IPC between the daemon and the web app
  cli.py                Sensor/display/presence tooling
scripts/kiosk.sh        Session-agnostic kiosk launcher with supervision
templates/
  calendar.html         The wall display
  settings.html         The settings page (mobile-first)
static/
  css/tokens.css        Palette and type scale
  css/wall.css          The wall display
  css/settings-page.css The settings page
  js/wall.js            Wall rendering, density, widgets
  js/settings.js        Settings behaviour
```

The web app never touches the display directly — it publishes intent through a
small JSON file in `/run/wallcal`, and the daemon acts on it. That way the two
processes can never fight over the panel.


---

## Widgets

Every widget has one visibility setting — **always**, **dynamic** or **off** —
and its own rule for what "dynamic" means. Slots keep their place in the grid
whether or not the widget is showing, so nothing reshuffles when a bus becomes
due.

| Widget | `dynamic` means |
|---|---|
| **Abfall** | From a configurable time the day before collection until one on the day itself |
| **ÖPNV** | Only inside the configured per-weekday windows |
| **Wetter** | Only when there is something actionable — rain starting, frost, wind |
| **Fahrzeit** | Only within a configurable window before an event that has an address |
| **QR-Code** | Only in the near layout; it is useless from across the room |

**Abfall** is one of your existing CalDAV sources, not a new poller. Mark it
under **Kalender → Abfallkalender** and it drops out of the normal agenda.
Title substrings map to a fraction name and colour, editable in settings,
because every Kommune names them differently. Several fractions on one day
render together.

**ÖPNV** uses [Transitous](https://transitous.org/), a community MOTIS instance
covering all German operators through DELFI and the regional feeds. No API key.
It was chosen over `v6.db.transport.rest` because that wraps Deutsche Bahn
alone, its HAFAS backend was shut off permanently, and its data endpoints were
returning 503 during development — a `db-rest` provider is still included
behind the same interface and selectable with `transit_provider`.

**Wetter** uses Open-Meteo, no key. FAR shows one actionable line; NEAR is laid
out the way a phone does it — temperature, the one thing that matters, the
condition as a symbol, then the coming hours. Symbols are inline SVG mapped
from the WMO weather codes, with a moon instead of a sun after dark. Nothing is
fetched, so they still render when the Pi has no internet.

Still not a seven-day grid: those are not read on a wall.

**Fahrzeit** reuses the same MOTIS instance for routing, so it needs no second
service. It is transit time; there is no car routing. Events without a
parseable address are skipped silently.

### Layout

NEAR groups each region onto its own card. FAR stays frameless — from four
metres a border is just a line, and the clock needs the room more than the
grouping does.

Cards share the rail and grow into whatever space is free, so a widget
withdrawing does not leave a hole behind it. The collapse is animated rather
than instant: the height runs to zero over `--reflow` and the neighbours
expand as it does.

The Abfall card is the exception to the quiet-by-default rule. It only appears
in a narrow window around collection day, and its whole job is to be seen on
the way past, so it takes the top of the rail and carries the fraction's own
colour.

### Failure behaviour

Every feed caches to SQLite on fetch and the page always renders from that
cache. There is no spinner and no error state: a failed refresh keeps the last
good data and marks it with a small dot. The Pi's WLAN will drop and these APIs
will time out, and neither should be visible on the wall.

```bash
./wallcal.sh doctor        # includes per-feed freshness
curl -s localhost:5005/api/status | python3 -m json.tool
```

---

## Settings

Six sections, one scrolling page with a sticky jumper: **Kalender**,
**Widgets**, **Anzeige**, **Präsenz**, **System**, **Erweitert**. Changes apply
live within a couple of seconds — there is no save button.

**Erweitert** holds anything hardware-specific or footgun-adjacent: the
off-strategy, display backend pinning, PWM pins and parameters, sensor mode and
port, and the radar factory reset. A user who never opens it gets sensible
autodetected behaviour.

Everything is also reachable from the command line:

```bash
./wallcal.sh config list
./wallcal.sh config set near_view=month
./wallcal.sh config set widget_transit=always
```

| Setting group | Keys |
|---|---|
| Density | `density_mode` (`auto` · `on` · `near` · `far`) `density_near_cm` `density_far_cm` `density_min_band_cm` `density_enter_ms` `density_debounce_ms` `crossfade_ms` |
| Layout | `near_view` `drift_enabled` `timezone` `locale` `timeofday_*` |
| Brightness | `brightness` `dim_seconds` `dim_level` `night_mode` `night_brightness` |
| Screensaver | `screensaver_style` `screensaver_idle_seconds` `screensaver_brightness` |
| Anticipatory wake | `prewake_enabled` `prewake_lead_minutes` `prewake_timed_only` `prewake_allday_at` `prewake_hold_minutes` |
| Widgets | `widget_*` `abfall_*` `transit_*` `weather_*` `home_*` `travel_*` `qr_size` |
| Backlight | `display_off_strategy` `pwm_*` |

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /` | The wall display |
| `GET /settings` | The settings page |
| `GET /events` | Cached events, plus the Abfall payload |
| `GET /api/widgets` | Transit, weather, travel and QR, already decided |
| `GET /api/presence/stream` | Server-sent events: panel state pushed on change |
| `GET /api/presence/live` | The same fields polled, used as the fallback |
| `GET /api/presence` | Full sensor telemetry |
| `GET /api/display` | Power, brightness, off-strategy |
| `GET /api/prewake` | The next calendar-driven wake |
| `GET /api/status` | Health, poller state, per-feed freshness |
| `GET /api/qr.svg` | The companion QR code |
| `GET /api/transit/search?q=` | Station search |
| `GET /api/geocode?q=` | Place search for the location pickers |
| `GET /api/timezones?q=` | Zone search for the timezone picker |
| `GET/POST /api/settings` | Read and write settings |
