"""
Command-line tools for the presence subsystem.

``wallcal.sh`` forwards its sensor/display/presence subcommands here so the
shell script stays a control surface and all the logic lives in one place.

    python -m presence.cli sensor monitor
    python -m presence.cli display survey
    python -m presence.cli presence override on
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import statistics
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config                                    # noqa: E402
import database                                  # noqa: E402
from presence import calibration                 # noqa: E402
from presence import ld2410, runtime             # noqa: E402
from presence.display import DisplayController   # noqa: E402


# ---------------------------------------------------------------------------
# Small terminal helpers
# ---------------------------------------------------------------------------

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(t): return _c("1", t)
def green(t): return _c("32", t)
def yellow(t): return _c("33", t)
def red(t): return _c("31", t)
def dim(t): return _c("2", t)
def cyan(t): return _c("36", t)


def emit(payload, as_json: bool, human=None) -> None:
    """Print either JSON or the human rendering of the same data."""
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    elif human is not None:
        human(payload)
    else:
        print(json.dumps(payload, indent=2, default=str))


def bar(value: float, maximum: float, width: int = 28, mark: float | None = None) -> str:
    """A text meter, optionally with a '|' marking the configured threshold."""
    if maximum <= 0:
        return " " * width
    filled = int(max(0.0, min(1.0, value / maximum)) * width)
    cells = ["█" if i < filled else "·" for i in range(width)]
    if mark is not None and 0 <= mark <= maximum:
        pos = min(width - 1, int((mark / maximum) * width))
        cells[pos] = "|" if cells[pos] == "·" else "┃"
    return "".join(cells)


@contextlib.contextmanager
def sensor_access():
    """Take the serial port from the presence daemon, reporting to the terminal."""
    with calibration.sensor_access(
            on_message=lambda m: print(dim(m), file=sys.stderr)) as released:
        yield released


def resolve_sensor_target(args) -> tuple:
    """Work out which port/baud to talk to, honouring settings then autodetect."""
    port = getattr(args, "port", None)
    if not port or port == "auto":
        settings = database.get_all_settings()
        if not (settings.get("sensor_uart_port") or "auto") or \
                settings.get("sensor_uart_port") == "auto":
            print(dim("Autodetecting sensor…"), file=sys.stderr)
    try:
        return calibration.resolve_target(port, getattr(args, "baud", None))
    except RuntimeError as exc:
        raise SystemExit(red(str(exc)))


# ---------------------------------------------------------------------------
# sensor
# ---------------------------------------------------------------------------

def cmd_sensor_scan(args) -> int:
    ports = ld2410.available_ports()
    if not ports:
        print(red("No serial devices found under /dev."))
        print(dim("On a Pi 3B+ you usually need 'enable_uart=1' and "
                  "'dtoverlay=disable-bt' — run: sudo ./wallcal.sh install --only uart"))
        return 1

    print(bold(f"Scanning {len(ports)} serial device(s)…"))
    results = []
    for port in ports:
        for baud in ld2410.CANDIDATE_BAUDS:
            sys.stdout.write(f"  {port} @ {baud:<7} … ")
            sys.stdout.flush()
            reading = ld2410.probe(port, baud, seconds=args.timeout)
            if reading:
                print(green("LD2410 found"))
                results.append({"port": port, "baud": baud,
                                "reading": reading.to_dict()})
                break
            print(dim("no response"))
        if results and not args.all:
            break

    if not results:
        print(red("\nNo sensor responded."))
        print(dim("Checklist: 5V + GND connected, sensor TX -> Pi RXD (BCM15), "
                  "sensor RX -> Pi TXD (BCM14), serial console disabled."))
        return 1

    best = results[0]
    print(green(f"\nSensor on {best['port']} @ {best['baud']}"))
    if args.save:
        database.set_many_settings({
            "sensor_uart_port": best["port"],
            "sensor_uart_baud": str(best["baud"]),
            "sensor_mode": "uart",
        })
        runtime.request_rescan()
        print(green("Saved to settings and asked the daemon to reconnect."))
    else:
        print(dim("Re-run with --save to store this in the settings."))
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    return 0


def cmd_sensor_monitor(args) -> int:
    port, baud = resolve_sensor_target(args)
    settings = database.get_all_settings()
    threshold = int(float(settings.get("sensor_distance_max_cm",
                                       config.DEFAULT_SENSOR_DISTANCE_MAX_CM)))
    span = max(threshold * 2, 600)

    print(bold(f"Live readings from {port} @ {baud}"))
    print(dim(f"threshold {threshold} cm marked with '|' — Ctrl-C to stop\n"))
    print(dim(f"{'state':<18}{'dist':>7}  {'move d/e':>12}  {'still d/e':>12}  meter"))

    deadline = time.monotonic() + args.seconds if args.seconds else None
    try:
        with ld2410.LD2410(port, baud, timeout=0.3) as radar:
            try:
                params = radar.read_parameters()
                print(dim(f"sensor range: {ld2410.gate_to_cm(params.max_moving_gate)} cm "
                          f"moving / {ld2410.gate_to_cm(params.max_stationary_gate)} cm "
                          f"still — nothing beyond that can be reported\n"))
            except ld2410.LD2410Error:
                pass
            if args.engineering:
                try:
                    radar.set_engineering_mode(True)
                except ld2410.LD2410Error as exc:
                    print(yellow(f"Could not enable engineering mode: {exc}"))
            while deadline is None or time.monotonic() < deadline:
                reading = radar.read(max_wait=1.0)
                if reading is None:
                    print(dim("… no frame"))
                    continue
                # With no target the sensor still reports a residual distance;
                # printing it makes an empty room look like a detection.
                distance = reading.distance_cm if reading.has_target else 0
                within = reading.has_target and 0 < distance <= threshold
                state = reading.state_name
                colour = green if within else (yellow if reading.has_target else dim)
                shown = f"{distance:>5} cm" if reading.has_target else "    — cm"
                line = (
                    f"{colour(state):<18}"
                    f"{shown}  "
                    f"{reading.moving_distance_cm:>4}/{reading.moving_energy:<3}    "
                    f"{reading.stationary_distance_cm:>4}/{reading.stationary_energy:<3}    "
                    f"{bar(distance, span, mark=threshold)}"
                )
                print(line)
                if reading.engineering and reading.moving_gate_energy:
                    print(dim("   gates move : " + " ".join(
                        f"{v:>3}" for v in reading.moving_gate_energy)))
                    print(dim("   gates still: " + " ".join(
                        f"{v:>3}" for v in reading.stationary_gate_energy)))
    except KeyboardInterrupt:
        print()
    except ld2410.LD2410Error as exc:
        print(red(str(exc)))
        return 1
    return 0


def cmd_sensor_params(args) -> int:
    port, baud = resolve_sensor_target(args)
    try:
        with ld2410.LD2410(port, baud) as radar:
            firmware = radar.firmware_version()
            params = radar.read_parameters()
    except ld2410.LD2410Error as exc:
        print(red(str(exc)))
        return 1

    payload = params.to_dict()
    payload["firmware"] = firmware
    payload["port"] = port
    payload["baud"] = baud

    def human(p):
        print(bold("Sensor configuration"))
        print(f"  port/baud          {p['port']} @ {p['baud']}")
        print(f"  firmware           {p['firmware']}")
        print(f"  max gate           {p['max_gate']} (~{p['max_gate'] * 75} cm)")
        print(f"  moving range       gate {p['max_moving_gate']} "
              f"(~{p['max_moving_distance_cm']} cm)")
        print(f"  stationary range   gate {p['max_stationary_gate']} "
              f"(~{p['max_stationary_distance_cm']} cm)")
        print(f"  unmanned duration  {p['unmanned_duration_s']} s")
        print(f"  moving sens.       {p['moving_sensitivity']}")
        print(f"  stationary sens.   {p['stationary_sensitivity']}")

    emit(payload, args.json, human)
    return 0


def cmd_sensor_gates(args) -> int:
    port, baud = resolve_sensor_target(args)
    gate = ld2410.cm_to_gate(args.distance_cm)
    try:
        with ld2410.LD2410(port, baud) as radar:
            radar.set_max_gates(gate, gate, args.hold)
    except ld2410.LD2410Error as exc:
        print(red(str(exc)))
        return 1
    print(green(f"Sensor range set to gate {gate} (~{ld2410.gate_to_cm(gate)} cm), "
                f"hold {args.hold} s"))
    print(dim("Gates are 0.75 m wide; WallCal still applies the exact "
              "centimetre threshold in software."))
    return 0


def cmd_sensor_sensitivity(args) -> int:
    port, baud = resolve_sensor_target(args)
    try:
        with ld2410.LD2410(port, baud) as radar:
            radar.set_sensitivity(args.gate, args.moving, args.stationary)
    except ld2410.LD2410Error as exc:
        print(red(str(exc)))
        return 1
    target = "all gates" if args.gate is None else f"gate {args.gate}"
    print(green(f"Sensitivity for {target}: moving {args.moving}, "
                f"stationary {args.stationary}"))
    return 0


def cmd_sensor_reset(args) -> int:
    port, baud = resolve_sensor_target(args)
    try:
        with ld2410.LD2410(port, baud) as radar:
            radar.factory_reset()
            radar.restart()
    except ld2410.LD2410Error as exc:
        print(red(str(exc)))
        return 1
    print(green("Sensor reset to factory defaults and restarted."))
    print(dim("It is back at 256000 baud with a 6 m range."))
    return 0


def cmd_sensor_test(args) -> int:
    """Quick pass/fail: is a sensor there and is it producing sane frames?"""
    settings = database.get_all_settings()
    mode = settings.get("sensor_mode", config.DEFAULT_SENSOR_MODE)
    print(bold(f"Sensor mode: {mode}"))

    if mode in ("uart", "auto"):
        try:
            port, baud = resolve_sensor_target(args)
        except SystemExit as exc:
            print(str(exc))
            if mode == "uart":
                return 1
            port = None
        if port:
            reading = ld2410.probe(port, baud, seconds=2.0)
            if reading:
                print(green(f"  UART OK — {port} @ {baud}, "
                            f"state '{reading.state_name}', {reading.distance_cm} cm"))
                return 0
            print(red(f"  UART FAIL — no frames from {port} @ {baud}"))
            if mode == "uart":
                return 1

    pin = int(float(settings.get("sensor_gpio_pin", config.DEFAULT_SENSOR_GPIO_PIN)))
    try:
        from presence.daemon import _make_gpio_reader
        reader, impl = _make_gpio_reader(pin)
        level = reader()
        closer = getattr(reader, "close", None)
        if callable(closer):
            closer()
        print(green(f"  GPIO OK — BCM{pin} via {impl}, currently "
                    f"{'HIGH (presence)' if level else 'LOW (clear)'}"))
        return 0
    except Exception as exc:
        print(red(f"  GPIO FAIL — {exc}"))
        return 1


def cmd_sensor_calibrate(args) -> int:
    """Walk-test helper: stand where you want the panel to wake, then apply."""
    job = calibration.CalibrationJob(seconds=args.seconds, delay=args.delay,
                                     port=args.port, baud=args.baud)
    print(bold("Calibration"))
    job.start()

    last_state = None
    try:
        while job.running:
            status = job.status
            state = status["state"]

            if state != last_state:
                if state == calibration.CalibrationJob.COUNTDOWN:
                    print(dim("\n  Go and stand at the furthest point where "
                              "the calendar should wake up."))
                elif state == calibration.CalibrationJob.SAMPLING:
                    if status["original_range_cm"]:
                        print(dim(f"  Sensor opened to "
                                  f"{ld2410.gate_to_cm(ld2410.MAX_GATE)} cm for "
                                  f"the test (normally "
                                  f"{status['original_range_cm']} cm)."))
                    print(bold(f"\n  Sampling for {job.seconds}s — hold still.\n"))
                last_state = state

            if state == calibration.CalibrationJob.COUNTDOWN:
                sys.stdout.write(f"\r  Sampling starts in {status['remaining']}s… ")
            elif state == calibration.CalibrationJob.SAMPLING:
                live = (f"{status['current_distance_cm']:>4} cm  "
                        f"{status['current_state']}"
                        if status["current_distance_cm"] else "no target detected")
                sys.stdout.write(
                    f"\r  {status['remaining']:>3}s left — samples: "
                    f"{status['samples']:>4}   now: {live:<28}"
                )
            sys.stdout.flush()
            time.sleep(0.2)
    except KeyboardInterrupt:
        job.cancel()
        job.join(timeout=5)
        print()
        return 130

    job.join(timeout=10)
    status = job.status
    print()

    if status["state"] != calibration.CalibrationJob.DONE:
        print(red(f"\n{status.get('message') or 'Calibration failed'}"))
        print(bold("\nWhat the sensor reported"))
        print(f"  frames received      {status['frames']}")
        print(f"  frames with a target {status['frames_with_target']}")
        if status["furthest_seen_cm"]:
            print(f"  furthest target      {status['furthest_seen_cm']} cm")
        if status["original_range_cm"]:
            print(f"  configured range     {status['original_range_cm']} cm")
        if status.get("error"):
            print()
            print(yellow(status["error"]))
        if status["frames"] and not status["frames_with_target"]:
            # Only relevant on the command line — the web UI shows its own
            # countdown, but over SSH you have to physically walk over.
            print(dim(f"\nSampling starts after the countdown. Run with "
                      f"--delay 20 if you need longer to get into position."))
        return 1

    result = status["result"]
    print(bold("Results"))
    print(f"  samples               {result['samples']}")
    print(f"  distance min/med/max  {result['min_cm']} / "
          f"{result['median_cm']} / {result['max_cm']} cm")
    print(f"  95th percentile       {result['p95_cm']} cm")
    print(bold("\nSuggested settings"))
    print(f"  sensor_distance_max_cm        {result['suggested_distance_max_cm']}")
    print(f"  sensor_moving_energy_min      {result['suggested_moving_energy_min']}")
    print(f"  sensor_stationary_energy_min  {result['suggested_stationary_energy_min']}")

    if args.apply:
        job.apply()
        print(green("\nApplied."))
    else:
        print(dim("\nRe-run with --apply to save these."))

    if args.json:
        print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------------------
# display
# ---------------------------------------------------------------------------

def _controller() -> DisplayController:
    settings = database.get_all_settings()
    return DisplayController(
        backend_spec=settings.get("display_backend", "auto"),
        output=settings.get("display_output", "auto"),
    )


def cmd_display_survey(args) -> int:
    report = DisplayController.survey()

    def human(rows):
        print(bold("Display power backends"))
        for row in rows:
            mark = green("available") if row["available"] else dim("unavailable")
            layout = "" if row["preserves_layout"] else yellow(" (resets layout)")
            print(f"  {row['name']:<12} {mark:<22} {row['description']}{layout}")
            if row["outputs"]:
                print(dim(f"               outputs: {', '.join(row['outputs'])}"))
        usable = [r["name"] for r in rows if r["available"] and r["name"] != "none"]
        print()
        if usable:
            print(f"Autodetect would use: {cyan(usable[0])}")
        else:
            print(yellow("No real backend available — is a desktop session running?"))

    emit(report, args.json, human)
    return 0


def cmd_display_info(args) -> int:
    info = _controller().info()

    def human(i):
        print(bold("Display"))
        print(f"  session       {i['session_type']}")
        print(f"  backends      {', '.join(b['name'] for b in i['backends']) or '-'}")
        print(f"  output        {i['output'] or 'all'}")
        print(f"  available     {', '.join(i['available_outputs']) or '-'}")
        power = i["power"]
        label = green("ON") if power else (red("OFF") if power is False else dim("unknown"))
        print(f"  power         {label}")

    emit(info, args.json, human)
    return 0


def cmd_display_power(args) -> int:
    controller = _controller()
    if args.action == "toggle":
        ok = controller.toggle()
    else:
        ok = controller.set_power(args.action == "on", force=True)
    state = controller.get_power()
    print((green("Display is ON") if state else red("Display is OFF"))
          if state is not None else dim("Command sent"))
    if not ok:
        print(yellow("Backend reported a failure — try 'wallcal.sh display detect'"))
        return 1
    return 0


def _pwm_from_settings():
    """Build a PwmBacklight from the stored settings, without the daemon."""
    from presence import pwm as pwm_mod
    s = database.get_all_settings()

    def num(key, default, cast=int):
        try:
            return cast(float(str(s.get(key, default)).strip()))
        except (TypeError, ValueError):
            return cast(default)

    return pwm_mod.PwmBacklight(
        pin=num("pwm_gpio", config.DEFAULT_PWM_GPIO),
        frequency_hz=num("pwm_frequency_hz", config.DEFAULT_PWM_FREQUENCY_HZ),
        gamma=num("pwm_gamma", config.DEFAULT_PWM_GAMMA, float),
        min_duty_percent=num("pwm_min_duty_percent",
                             config.DEFAULT_PWM_MIN_DUTY_PERCENT, float),
        enable_pin=num("pwm_enable_gpio", config.DEFAULT_PWM_ENABLE_GPIO),
        enable_active_high=str(s.get("pwm_enable_active_high", "true")).lower()
        in ("1", "true", "yes", "on"),
    )


def cmd_display_pwm_status(args) -> int:
    from presence import pwm as pwm_mod
    s = database.get_all_settings()
    report = pwm_mod.survey()
    report["configured"] = {
        "strategy": s.get("display_off_strategy", "hdmi"),
        "pin": s.get("pwm_gpio"),
        "frequency_hz": s.get("pwm_frequency_hz"),
        "gamma": s.get("pwm_gamma"),
        "min_duty_percent": s.get("pwm_min_duty_percent"),
        "fade_ms": s.get("pwm_fade_ms"),
        "enable_pin": s.get("pwm_enable_gpio"),
    }
    try:
        report["overlay_line"] = pwm_mod.overlay_line(int(s.get("pwm_gpio", 18)))
    except Exception as exc:
        report["overlay_line"] = None
        report["overlay_error"] = str(exc)

    def human(r):
        cfg = r["configured"]
        print(bold("PWM backlight"))
        print(f"  strategy      {cfg['strategy']}")
        print(f"  pin           GPIO{cfg['pin']}")
        print(f"  frequency     {cfg['frequency_hz']} Hz")
        print(f"  gamma         {cfg['gamma']}   min duty {cfg['min_duty_percent']}%")
        print(f"  fade          {cfg['fade_ms']} ms")
        enable = cfg["enable_pin"]
        print(f"  BL_EN         {'not wired' if str(enable) in ('-1', '') else 'BCM ' + str(enable)}")
        print()
        if r.get("overlay_line"):
            print(f"  needs         {cyan(r['overlay_line'])}")
        elif r.get("overlay_error"):
            print("  " + red(r["overlay_error"]))
        if not r["available"]:
            print("  " + yellow("no /sys/class/pwm — the overlay is not loaded"))
            print(dim("  add the line above to /boot/firmware/config.txt and reboot"))
        else:
            for chip in r["chips"]:
                print(f"  {chip['path']}  channels: {chip['npwm']}"
                      f"  exported: {', '.join(chip['exported']) or '-'}")

    emit(report, args.json, human)
    return 0 if report["available"] else 1


def cmd_display_pwm_test(args) -> int:
    """Sweep the backlight so the wiring can be proved on its own.

    Takes the channel from the daemon the same way the sensor tools take the
    serial port — otherwise both would be writing duty cycles at each other.
    """
    from presence import pwm as pwm_mod

    backlight = _pwm_from_settings()
    print(dim(f"Sweeping {backlight.describe()} over {args.seconds:.0f}s…"))

    # Same arbitration as the sensor tools. The pause is what makes the daemon
    # hold the panel steady instead of changing power under the sweep — it is
    # named for the sensor because that is what needed it first.
    with sensor_access():
        try:
            backlight.open()
        except pwm_mod.PwmError as exc:
            print(red(str(exc)))
            return 1
        try:
            width = 40

            def show(value):
                filled = int(round(value / 100.0 * width))
                bar = "█" * filled + "·" * (width - filled)
                duty = pwm_mod.perceptual_to_duty(
                    value, backlight.gamma, backlight.min_duty_percent)
                sys.stdout.write(f"\r  {bar} {value:5.1f}%  duty {duty * 100:5.1f}% ")
                sys.stdout.flush()

            backlight.sweep(seconds=args.seconds, on_step=show)
            print()
        finally:
            # Leave it lit rather than dark: whoever ran this is standing in
            # front of the panel looking at it, and the sweep ends at zero.
            backlight.set_brightness(100)

    print(green("Sweep complete — if the panel brightened and dimmed, the tap works"))
    return 0


def cmd_display_strategy(args) -> int:
    from presence.panel import parse_strategy, STRATEGIES
    if not args.value:
        current = database.get_setting("display_off_strategy",
                                       config.DEFAULT_DISPLAY_OFF_STRATEGY)
        print(current)
        return 0
    parsed = parse_strategy(args.value)
    requested = [p.strip().lower() for p in args.value.split(",") if p.strip()]
    unknown = [r for r in requested if r not in STRATEGIES]
    if unknown:
        print(red(f"unknown strategy: {', '.join(unknown)}"))
        print(dim("known: " + ", ".join(STRATEGIES)))
        return 2
    database.set_setting("display_off_strategy", ",".join(parsed))
    runtime.request_rescan()
    print(green(f"Off-strategy set to {','.join(parsed)}"))
    if "pwm" in parsed:
        print(dim("check the wiring with: wallcal.sh display pwm test"))
    return 0


def cmd_presence_density(args) -> int:
    """Watch the near/far decision live, so it can be checked by walking up.

    Prints one line per state-file update: the distance the radar reports,
    whether the wake gate is satisfied, and which layout the daemon has
    settled on. The gate and the layout are deliberately separate — a weak
    stationary signal should not move the layout for somebody standing still.
    """
    s = database.get_all_settings()
    near = int(float(s.get("density_near_cm", 100)))
    far = int(float(s.get("density_far_cm", 140)))
    print(bold("Density"))
    print(f"  mode {s.get('density_mode')}   near <= {near} cm   far >= {far} cm")
    print(dim("  walk towards the panel and back; ctrl-c to stop"))
    print()
    print(f"  {'dist':>6}  {'gate':<6} {'layout':<7} meter")

    width = 40
    scale = 400.0
    last = None
    try:
        while True:
            state = runtime.read_state()
            if not state.get("daemon_running"):
                print(red("  presence daemon is not running"))
                return 1
            reading = state.get("reading") or {}
            distance = reading.get("distance_cm")
            layout = state.get("density", "?")
            gate = "yes" if state.get("present") else "no"

            filled = 0 if not distance else min(width, int(distance / scale * width))
            bar = ["·"] * width
            for i in range(filled):
                bar[i] = "█"
            for mark, cm in ((("n"), near), (("f"), far)):
                pos = min(width - 1, int(cm / scale * width))
                if bar[pos] == "·":
                    bar[pos] = mark
            line = (f"  {str(distance or 0):>6}  {gate:<6} "
                    f"{(green(layout) if layout == 'near' else cyan(layout)):<7} "
                    f"{''.join(bar)}")
            if line != last:
                print(line)
                last = line
            time.sleep(0.2)
    except KeyboardInterrupt:
        print()
        return 0


def cmd_display_autoselect(args) -> int:
    """Pin the best available backend, rather than leaving it to chance.

    The daemon starts before the compositor does, so its own first probe can
    only ever find the session-less backends. Run this once the desktop is up
    and the right answer gets written down.
    """
    report = DisplayController.survey()
    native = [r for r in report if r["available"] and r.get("session_native")]
    other = [r for r in report
             if r["available"] and r["name"] != "none" and not r.get("session_native")]

    if not native:
        print(yellow("No session-native display backend is available."))
        if other:
            print(dim(f"Only {', '.join(r['name'] for r in other)} — these power "
                      "the output down without the compositor knowing, so the "
                      "panel can wake to a blank frame."))
        print(dim("Is the desktop running as this user? Leaving display_backend "
                  "on 'auto'; the daemon keeps re-probing and will upgrade "
                  "itself when a session appears."))
        if args.save:
            database.set_setting("display_backend", "auto")
        return 1

    # survey() preserves BACKEND_ORDER, so the first hit is the best one.
    chosen = native[0]["name"]
    print(green(f"Best available display backend: {chosen}")
          + dim(f" — {native[0]['description']}"))

    if args.save:
        database.set_setting("display_backend", chosen)
        runtime.request_rescan()
        print(green("Pinned in settings; the daemon has been asked to reload."))
    else:
        print(dim("Re-run with --save to pin it."))
    if args.json:
        print(json.dumps({"chosen": chosen, "backends": report}, indent=2))
    return 0


def cmd_display_rotate(args) -> int:
    controller = _controller()
    if controller.set_rotation(args.transform):
        database.set_setting("display_rotate", args.transform)
        print(green(f"Rotation set to {args.transform} (saved for next boot)."))
        return 0
    print(red("Could not rotate the output with the current backend."))
    return 1


# ---------------------------------------------------------------------------
# presence
# ---------------------------------------------------------------------------

def cmd_presence_state(args) -> int:
    state = runtime.read_state()

    def human(s):
        if not s.get("daemon_running"):
            print(red("Presence daemon is not running"))
            if s.get("reason"):
                print(dim(f"  {s['reason']}"))
            if not s.get("updated_at"):
                return
        print(bold("Presence"))
        present = s.get("present")
        cause = s.get("present_cause")
        print(f"  present       {green('yes') if present else dim('no')}"
              + (f"  ({cause})" if present and cause else ""))
        print(f"  idle          {s.get('idle_seconds')} s")
        print(f"  override      {s.get('override', 'auto')}")
        reading = s.get("reading") or {}
        if reading.get("distance_cm") is not None:
            print(f"  distance      {reading.get('distance_cm')} cm "
                  f"({reading.get('state_name', '?')})")
            print(f"  energy        moving {reading.get('moving_energy', '-')}, "
                  f"stationary {reading.get('stationary_energy', '-')}")
        thresholds = s.get("thresholds") or {}
        print(f"  threshold     {thresholds.get('distance_max_cm')} cm, "
              f"off after {thresholds.get('off_timeout')} s")
        print(bold("\nDisplay"))
        on = s.get("display_on")
        print(f"  power         {green('ON') if on else red('OFF')} "
              f"({s.get('display_reason', '')})")
        backends = ', '.join(s.get('display_backends') or []) or '-'
        if s.get("display_backend_is_fallback"):
            backends += yellow("  ← session-less fallback; may wake to a blank frame")
        print(f"  backends      {backends}")
        print(f"  output        {s.get('display_output') or '-'}")
        print(f"  on today      {round((s.get('display_on_seconds_today') or 0) / 60)} min"
              f"   wakes: {s.get('wake_count', 0)}")
        print(bold("\nSensor"))
        print(f"  source        {s.get('sensor_description') or s.get('sensor_kind')}")
        healthy = s.get("sensor_healthy")
        print(f"  health        {green('ok') if healthy else red('unhealthy')}")
        if s.get("sensor_error"):
            print(f"  error         {red(s['sensor_error'])}")
        schedule = s.get("schedule") or {}
        if schedule.get("enabled"):
            active = schedule.get("active_now")
            print(bold("\nSchedule"))
            print(f"  window        {schedule.get('start')} – {schedule.get('end')} "
                  f"({green('inside') if active else yellow('outside')})")

    emit(state, args.json, human)
    return 0 if state.get("daemon_running") else 1


def cmd_presence_override(args) -> int:
    runtime.set_override(args.mode)
    print(green(f"Override set to '{args.mode}'."))
    if args.mode == "auto":
        print(dim("The sensor is back in charge."))
    return 0


def cmd_presence_wake(args) -> int:
    runtime.wake_for(args.seconds)
    print(green(f"Display will stay on for at least {args.seconds}s."))
    return 0


def cmd_presence_rescan(args) -> int:
    runtime.request_rescan()
    print(green("Asked the daemon to re-detect the sensor and display."))
    return 0


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def cmd_settings_list(args) -> int:
    settings = database.get_all_settings()
    if args.json:
        print(json.dumps(settings, indent=2))
        return 0
    width = max(len(k) for k in settings) if settings else 10
    for key in sorted(settings):
        print(f"  {key:<{width}}  {settings[key]}")
    return 0


def cmd_settings_get(args) -> int:
    settings = database.get_all_settings()
    if args.key not in settings:
        print(red(f"Unknown setting '{args.key}'"), file=sys.stderr)
        return 1
    print(settings[args.key])
    return 0


def cmd_settings_set(args) -> int:
    updates = {}
    for pair in args.assignments:
        if "=" not in pair:
            print(red(f"Expected key=value, got '{pair}'"), file=sys.stderr)
            return 2
        key, _, value = pair.partition("=")
        key = key.strip()
        if key not in database.SETTABLE_KEYS:
            print(red(f"Unknown setting '{key}'"), file=sys.stderr)
            print(dim("List valid keys with: wallcal.sh config list"), file=sys.stderr)
            return 1
        updates[key] = value.strip()

    database.set_many_settings(updates)
    runtime.request_reload()
    for key, value in updates.items():
        print(green(f"{key} = {value}"))
    return 0


# ---------------------------------------------------------------------------
# Argument wiring
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="presence.cli",
                                     description="WallCal presence tooling")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="group", required=True)

    # -- sensor ------------------------------------------------------------
    sensor = sub.add_parser("sensor", help="LD2410C radar tools")
    sensor_sub = sensor.add_subparsers(dest="action", required=True)

    def add_port_args(p):
        p.add_argument("--port", help="serial device (default: from settings)")
        p.add_argument("--baud", type=int, help="baud rate (default: from settings)")

    p = sensor_sub.add_parser("scan", help="probe serial ports for a sensor")
    p.add_argument("--timeout", type=float, default=1.0,
                   help="seconds to listen per port/baud combination")
    p.add_argument("--all", action="store_true", help="keep scanning after a hit")
    p.add_argument("--save", action="store_true", help="store the result in settings")
    p.set_defaults(func=cmd_sensor_scan)

    p = sensor_sub.add_parser("monitor", help="live readings")
    add_port_args(p)
    p.add_argument("--seconds", type=float, default=0, help="stop after N seconds")
    p.add_argument("--engineering", action="store_true",
                   help="also show per-gate energies")
    p.set_defaults(func=cmd_sensor_monitor)

    p = sensor_sub.add_parser("params", help="read the sensor's own configuration")
    add_port_args(p)
    p.set_defaults(func=cmd_sensor_params)

    p = sensor_sub.add_parser("gates", help="program the sensor's detection range")
    add_port_args(p)
    p.add_argument("distance_cm", type=int, help="range in cm (rounded up to 75 cm gates)")
    p.add_argument("--hold", type=int, default=5, help="sensor's own unmanned duration")
    p.set_defaults(func=cmd_sensor_gates)

    p = sensor_sub.add_parser("sensitivity", help="set detection sensitivity")
    add_port_args(p)
    p.add_argument("moving", type=int, help="moving sensitivity 0-100")
    p.add_argument("stationary", type=int, help="stationary sensitivity 0-100")
    p.add_argument("--gate", type=int, default=None, help="single gate (default: all)")
    p.set_defaults(func=cmd_sensor_sensitivity)

    p = sensor_sub.add_parser("reset", help="factory-reset the sensor")
    add_port_args(p)
    p.set_defaults(func=cmd_sensor_reset)

    p = sensor_sub.add_parser("test", help="quick sensor health check")
    add_port_args(p)
    p.set_defaults(func=cmd_sensor_test)

    p = sensor_sub.add_parser("calibrate", help="walk-test and suggest thresholds")
    add_port_args(p)
    p.add_argument("--seconds", type=int, default=20, help="sampling window")
    p.add_argument("--delay", type=int, default=8,
                   help="countdown before sampling starts, to walk into position")
    p.add_argument("--apply", action="store_true", help="save the suggestions")
    p.set_defaults(func=cmd_sensor_calibrate)

    # -- display -----------------------------------------------------------
    display = sub.add_parser("display", help="display power tools")
    display_sub = display.add_subparsers(dest="action", required=True)

    display_sub.add_parser("survey", help="probe every backend"
                           ).set_defaults(func=cmd_display_survey)
    display_sub.add_parser("info", help="show the selected backend"
                           ).set_defaults(func=cmd_display_info)
    for action in ("on", "off", "toggle"):
        display_sub.add_parser(action, help=f"switch the display {action}"
                               ).set_defaults(func=cmd_display_power, action=action)

    p = display_sub.add_parser("autoselect",
                               help="pick the best backend and pin it")
    p.add_argument("--save", action="store_true", help="write it to settings")
    p.set_defaults(func=cmd_display_autoselect)

    p = display_sub.add_parser("strategy", help="how the panel is switched off")
    p.add_argument("value", nargs="?",
                   help="hdmi | pwm | css | none, comma-separated; omit to show")
    p.set_defaults(func=cmd_display_strategy)

    pwm_p = display_sub.add_parser("pwm", help="backlight PWM tools")
    pwm_sub = pwm_p.add_subparsers(dest="pwm_action", required=True)
    pwm_sub.add_parser("status", help="configured pins, overlay and sysfs state"
                       ).set_defaults(func=cmd_display_pwm_status)
    p = pwm_sub.add_parser("test", help="sweep 0->100->0 to verify the wiring")
    p.add_argument("--seconds", type=float, default=6.0)
    p.set_defaults(func=cmd_display_pwm_test)

    p = display_sub.add_parser("rotate", help="rotate the output")
    p.add_argument("transform", choices=["normal", "left", "right", "inverted"])
    p.set_defaults(func=cmd_display_rotate)

    # -- presence ----------------------------------------------------------
    pres = sub.add_parser("presence", help="daemon state and overrides")
    pres_sub = pres.add_subparsers(dest="action", required=True)

    pres_sub.add_parser("state", help="live presence + display state"
                        ).set_defaults(func=cmd_presence_state)

    p = pres_sub.add_parser("override", help="force the display on/off")
    p.add_argument("mode", choices=["auto", "on", "off"])
    p.set_defaults(func=cmd_presence_override)

    pres_sub.add_parser("density", help="watch the near/far decision live"
                        ).set_defaults(func=cmd_presence_density)

    p = pres_sub.add_parser("wake", help="keep the display on for a while")
    p.add_argument("seconds", type=float, nargs="?", default=300)
    p.set_defaults(func=cmd_presence_wake)

    pres_sub.add_parser("rescan", help="re-detect sensor and display"
                        ).set_defaults(func=cmd_presence_rescan)

    # -- settings ----------------------------------------------------------
    settings = sub.add_parser("settings", help="read/write stored settings")
    settings_sub = settings.add_subparsers(dest="action", required=True)

    settings_sub.add_parser("list", help="show every setting"
                            ).set_defaults(func=cmd_settings_list)

    p = settings_sub.add_parser("get", help="print one setting")
    p.add_argument("key")
    p.set_defaults(func=cmd_settings_get)

    p = settings_sub.add_parser("set", help="assign one or more settings")
    p.add_argument("assignments", nargs="+", metavar="KEY=VALUE")
    p.set_defaults(func=cmd_settings_set)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    database.init_db()
    try:
        # Every 'sensor' subcommand opens the serial port directly, so it has
        # to take it from the daemon first.
        if args.group == "sensor":
            with sensor_access():
                return args.func(args)
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except ld2410.LD2410Error as exc:
        print(red(str(exc)), file=sys.stderr)
        return 1
    except Exception as exc:
        # A traceback tells the user nothing useful about a loose wire.
        print(red(f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        print(dim("Set WALLCAL_DEBUG=1 for the full traceback."), file=sys.stderr)
        if os.environ.get("WALLCAL_DEBUG"):
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
