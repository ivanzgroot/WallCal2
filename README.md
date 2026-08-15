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

Then open `http://<pi-address>:5005/` from any machine on the network (or
`http://<hostname>.local:5005/`) and add your calendar under **⚙ → Calendars**.

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
| `OUT`      | 12     | GPIO18     | GPIO mode  |

Wire all five and leave the mode on **auto** — WallCal finds the radar on
serial and falls back to the OUT pin if it cannot.

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
| **Quiet hours** | A window outside which the panel never wakes, however much you wave at it. |

When the sensor is on UART, WallCal also programs the threshold into the
radar's own detection gates, so it stops reporting distant targets at all.
Gates are 0.75 m wide, so the exact centimetre threshold is still applied in
software on top.

---

## Display power

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
database.py             SQLite persistence (settings, calendars, event cache)
config.py               Defaults, all overridable via WALLCAL_* env vars
presence/
  ld2410.py             HLK-LD2410C UART protocol driver
  display.py            Display power backends with autodetection
  daemon.py             Presence state machine
  runtime.py            IPC between the daemon and the web app
  cli.py                Sensor/display/presence tooling
scripts/kiosk.sh        Session-agnostic kiosk launcher with supervision
templates/calendar.html The calendar UI
```

The web app never touches the display directly — it publishes intent through a
small JSON file in `/run/wallcal`, and the daemon acts on it. That way the two
processes can never fight over the panel.
