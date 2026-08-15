"""
Display power control with autodetection.

Raspberry Pi OS has shipped three different display stacks in recent years
(X11+Openbox, Wayfire, labwc) and a wall-mounted panel may only respond to
HDMI-CEC or a backlight file. Rather than hard-coding one, this module probes
everything available and picks the first backend that actually works, in an
order that prefers "true DPMS" (output stays configured, the browser layout
survives) over "disable the output entirely".

Backends, best first:
    wlopm      Wayland, wlr-output-power-management — real DPMS
    wlr-randr  Wayland, disables/enables the output
    xset       X11 DPMS — real DPMS
    xrandr     X11, disables/enables the output
    backlight  /sys/class/backlight/*/bl_power — DSI panels, official screen
    vcgencmd   Broadcom firmware display_power
    cec        HDMI-CEC standby/image-view-on — for TVs and smart monitors
    fbcon      /sys/class/graphics/fb0/blank — console only, needs root
    none       no-op, so the daemon still runs on a dev machine
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import time

logger = logging.getLogger("wallcal.display")

_COMPOSITOR_HINTS = (
    "labwc", "wayfire", "Xorg", "X", "lxsession", "openbox", "mutter",
    "weston", "sway", "wayvnc", "lxpanel", "pcmanfm", "chromium",
)

_ENV_KEYS = (
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS",
)

_env_cache: dict = {}
_env_cache_time: float = 0.0
_ENV_CACHE_TTL = 30.0


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

def session_env(refresh: bool = False) -> dict:
    """Best guess at the graphical session's environment.

    Scans /proc for a process owned by us that looks like a compositor or the
    kiosk browser and borrows its DISPLAY / WAYLAND_DISPLAY / XAUTHORITY.
    Falls back to conventional defaults when nothing is running yet.
    """
    global _env_cache, _env_cache_time
    now = time.monotonic()
    if _env_cache and not refresh and (now - _env_cache_time) < _ENV_CACHE_TTL:
        return dict(_env_cache)

    env = {k: v for k, v in os.environ.items()}
    harvested = _harvest_from_proc()
    for key, value in harvested.items():
        env.setdefault(key, value)
        if key in _ENV_KEYS:
            env[key] = value

    uid = os.getuid() if hasattr(os, "getuid") else 0
    runtime_dir = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    if os.path.isdir(runtime_dir):
        env["XDG_RUNTIME_DIR"] = runtime_dir

    if "WAYLAND_DISPLAY" not in env:
        sockets = sorted(
            p for p in glob.glob(os.path.join(runtime_dir, "wayland-*"))
            if not p.endswith(".lock")
        )
        if sockets:
            env["WAYLAND_DISPLAY"] = os.path.basename(sockets[0])

    if "DISPLAY" not in env and _x_server_running():
        env["DISPLAY"] = ":0"

    if env.get("DISPLAY") and "XAUTHORITY" not in env:
        for candidate in (
            os.path.expanduser("~/.Xauthority"),
            os.path.join(runtime_dir, "gdm", "Xauthority"),
        ):
            if os.path.exists(candidate):
                env["XAUTHORITY"] = candidate
                break

    _env_cache = env
    _env_cache_time = now
    return dict(env)


def _harvest_from_proc() -> dict:
    """Read session variables out of a running compositor/browser process."""
    found: dict = {}
    try:
        uid = os.getuid()
    except AttributeError:
        return found

    for pid_dir in glob.glob("/proc/[0-9]*"):
        try:
            if os.stat(pid_dir).st_uid != uid:
                continue
            with open(os.path.join(pid_dir, "comm"), "r") as fh:
                comm = fh.read().strip()
            if not any(hint in comm for hint in _COMPOSITOR_HINTS):
                continue
            with open(os.path.join(pid_dir, "environ"), "rb") as fh:
                raw = fh.read()
        except (OSError, PermissionError):
            continue

        for entry in raw.split(b"\0"):
            if b"=" not in entry:
                continue
            key, _, value = entry.decode("utf-8", "replace").partition("=")
            if key in _ENV_KEYS and value:
                found.setdefault(key, value)
        if "WAYLAND_DISPLAY" in found or "DISPLAY" in found:
            break
    return found


def _x_server_running() -> bool:
    return bool(glob.glob("/tmp/.X11-unix/X*"))


def session_type() -> str:
    """'wayland', 'x11' or 'none'."""
    env = session_env()
    if env.get("WAYLAND_DISPLAY"):
        return "wayland"
    if env.get("DISPLAY"):
        return "x11"
    return "none"


def _run(cmd, env=None, timeout=8):
    """Run a command with the session env; returns (rc, stdout+stderr)."""
    merged = session_env()
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(
            cmd, env=merged, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out"
    except Exception as exc:  # pragma: no cover
        return 1, str(exc)


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class Backend:
    name = "base"
    description = ""
    #: True when the output keeps its mode/geometry while powered down, so the
    #: kiosk browser does not get resized or reshuffled.
    preserves_layout = True
    #: True when this backend drives the graphical session. Those cannot be
    #: probed before a compositor exists — and the daemon starts at
    #: multi-user.target, long before one does. The session-less backends
    #: (vcgencmd, backlight, fbcon) answer immediately at boot, so without
    #: this flag the first probe would settle on one of them permanently.
    session_native = False

    def probe(self) -> bool:
        raise NotImplementedError

    def outputs(self) -> list:
        return []

    def set_power(self, on: bool, output: str | None = None) -> bool:
        raise NotImplementedError

    def get_power(self, output: str | None = None):
        return None

    def prepare(self) -> None:
        """Optional one-time setup once this backend is selected."""


class WlopmBackend(Backend):
    name = "wlopm"
    description = "Wayland output power management (true DPMS)"
    session_native = True

    def probe(self) -> bool:
        if not _have("wlopm") or session_type() != "wayland":
            return False
        rc, _ = _run(["wlopm"])
        return rc == 0

    def outputs(self) -> list:
        rc, out = _run(["wlopm"])
        if rc != 0:
            return []
        return [line.split()[0] for line in out.splitlines() if line.strip()]

    def set_power(self, on: bool, output=None) -> bool:
        targets = [output] if output else (self.outputs() or [None])
        ok = False
        for target in targets:
            cmd = ["wlopm", "--on" if on else "--off"]
            if target:
                cmd.append(target)
            rc, out = _run(cmd)
            ok = ok or rc == 0
            if rc != 0:
                logger.debug("wlopm failed: %s", out.strip())
        return ok

    def get_power(self, output=None):
        rc, out = _run(["wlopm"])
        if rc != 0:
            return None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and (output is None or parts[0] == output):
                return parts[1].strip().lower() == "on"
        return None


class WlrRandrBackend(Backend):
    name = "wlr-randr"
    description = "Wayland output enable/disable"
    session_native = True
    preserves_layout = False

    def probe(self) -> bool:
        if not _have("wlr-randr") or session_type() != "wayland":
            return False
        rc, _ = _run(["wlr-randr"])
        return rc == 0

    def outputs(self) -> list:
        rc, out = _run(["wlr-randr"])
        if rc != 0:
            return []
        names = []
        for line in out.splitlines():
            if line and not line[0].isspace():
                names.append(line.split()[0])
        return names

    def set_power(self, on: bool, output=None) -> bool:
        targets = [output] if output else self.outputs()
        ok = False
        for target in targets:
            rc, out = _run(["wlr-randr", "--output", target,
                            "--on" if on else "--off"])
            ok = ok or rc == 0
            if rc != 0:
                logger.debug("wlr-randr failed: %s", out.strip())
        return ok

    def get_power(self, output=None):
        rc, out = _run(["wlr-randr"])
        if rc != 0:
            return None
        current = None
        for line in out.splitlines():
            if line and not line[0].isspace():
                current = line.split()[0]
            elif current and (output is None or current == output):
                if "Enabled:" in line:
                    return "yes" in line.lower()
        return None

    def set_transform(self, transform: str, output=None) -> bool:
        targets = [output] if output else self.outputs()
        ok = False
        for target in targets:
            rc, _ = _run(["wlr-randr", "--output", target,
                          "--transform", transform])
            ok = ok or rc == 0
        return ok


class XsetBackend(Backend):
    name = "xset"
    description = "X11 DPMS (true DPMS)"
    session_native = True

    def probe(self) -> bool:
        if not _have("xset") or session_type() != "x11":
            return False
        rc, out = _run(["xset", "q"])
        return rc == 0 and "DPMS" in out

    def prepare(self) -> None:
        # Enable DPMS but disable every automatic timeout — WallCal decides
        # when the panel sleeps, nothing else.
        _run(["xset", "s", "off"])
        _run(["xset", "s", "noblank"])
        _run(["xset", "+dpms"])
        _run(["xset", "dpms", "0", "0", "0"])

    def outputs(self) -> list:
        if not _have("xrandr"):
            return []
        return XrandrBackend().outputs()

    def set_power(self, on: bool, output=None) -> bool:
        if on:
            _run(["xset", "dpms", "force", "on"])
            # Nudge the screensaver too, some drivers latch on it.
            _run(["xset", "s", "reset"])
            return True
        _run(["xset", "+dpms"])
        rc, _ = _run(["xset", "dpms", "force", "off"])
        return rc == 0

    def get_power(self, output=None):
        rc, out = _run(["xset", "q"])
        if rc != 0:
            return None
        match = re.search(r"Monitor is (\w+)", out)
        if match:
            return match.group(1).lower() == "on"
        return None


class XrandrBackend(Backend):
    name = "xrandr"
    description = "X11 output enable/disable"
    session_native = True
    preserves_layout = False

    def probe(self) -> bool:
        if not _have("xrandr") or session_type() != "x11":
            return False
        rc, _ = _run(["xrandr", "--query"])
        return rc == 0

    def outputs(self) -> list:
        rc, out = _run(["xrandr", "--query"])
        if rc != 0:
            return []
        return [line.split()[0] for line in out.splitlines()
                if " connected" in line]

    def set_power(self, on: bool, output=None) -> bool:
        targets = [output] if output else self.outputs()
        ok = False
        for target in targets:
            rc, _ = _run(["xrandr", "--output", target,
                          "--auto" if on else "--off"])
            ok = ok or rc == 0
        return ok

    def get_power(self, output=None):
        rc, out = _run(["xrandr", "--query"])
        if rc != 0:
            return None
        for line in out.splitlines():
            if " connected" in line:
                name = line.split()[0]
                if output and name != output:
                    continue
                # A resolution in the connected line means the output is live.
                return bool(re.search(r"\d+x\d+\+\d+\+\d+", line))
        return None

    def set_transform(self, transform: str, output=None) -> bool:
        mapping = {"normal": "normal", "90": "left", "180": "inverted",
                   "270": "right", "left": "left", "right": "right",
                   "inverted": "inverted"}
        rotation = mapping.get(str(transform), "normal")
        targets = [output] if output else self.outputs()
        ok = False
        for target in targets:
            rc, _ = _run(["xrandr", "--output", target, "--rotate", rotation])
            ok = ok or rc == 0
        return ok


class BacklightBackend(Backend):
    name = "backlight"
    description = "sysfs backlight (DSI / official touch display)"

    def _devices(self) -> list:
        return sorted(glob.glob("/sys/class/backlight/*"))

    def probe(self) -> bool:
        for dev in self._devices():
            path = os.path.join(dev, "bl_power")
            if os.path.exists(path) and os.access(path, os.W_OK):
                return True
        return False

    def outputs(self) -> list:
        return [os.path.basename(d) for d in self._devices()]

    def set_power(self, on: bool, output=None) -> bool:
        ok = False
        for dev in self._devices():
            if output and os.path.basename(dev) != output:
                continue
            try:
                with open(os.path.join(dev, "bl_power"), "w") as fh:
                    fh.write("0" if on else "4")
                ok = True
            except OSError as exc:
                logger.debug("backlight write failed: %s", exc)
        return ok

    def get_power(self, output=None):
        for dev in self._devices():
            if output and os.path.basename(dev) != output:
                continue
            try:
                with open(os.path.join(dev, "bl_power")) as fh:
                    return fh.read().strip() == "0"
            except OSError:
                continue
        return None


class VcgencmdBackend(Backend):
    name = "vcgencmd"
    description = "Broadcom firmware display_power"

    def probe(self) -> bool:
        if not _have("vcgencmd"):
            return False
        rc, out = _run(["vcgencmd", "display_power"])
        return rc == 0 and "display_power=" in out and "error" not in out.lower()

    def set_power(self, on: bool, output=None) -> bool:
        cmd = ["vcgencmd", "display_power", "1" if on else "0"]
        if output and output.isdigit():
            cmd.append(output)
        rc, out = _run(cmd)
        return rc == 0 and "error" not in out.lower()

    def get_power(self, output=None):
        rc, out = _run(["vcgencmd", "display_power"])
        if rc != 0:
            return None
        match = re.search(r"display_power=(\d)", out)
        return match.group(1) == "1" if match else None


class CecBackend(Backend):
    name = "cec"
    description = "HDMI-CEC standby / image-view-on (TVs & smart monitors)"

    def probe(self) -> bool:
        return bool(glob.glob("/dev/cec*")) and (_have("cec-ctl") or _have("cec-client"))

    def set_power(self, on: bool, output=None) -> bool:
        if _have("cec-ctl"):
            arg = "--image-view-on" if on else "--standby"
            rc, _ = _run(["cec-ctl", "-s", "--to", "0", arg])
            return rc == 0
        if _have("cec-client"):
            payload = "on 0" if on else "standby 0"
            try:
                proc = subprocess.run(
                    ["cec-client", "-s", "-d", "1"],
                    input=payload.encode(), timeout=15,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
                return proc.returncode == 0
            except Exception:
                return False
        return False

    def get_power(self, output=None):
        # CEC power status polling is slow and unreliable across vendors;
        # the controller tracks the last commanded state instead.
        return None


class FbconBackend(Backend):
    name = "fbcon"
    description = "framebuffer console blanking (needs root)"

    def _devices(self) -> list:
        return sorted(glob.glob("/sys/class/graphics/fb*/blank"))

    def probe(self) -> bool:
        return any(os.access(p, os.W_OK) for p in self._devices())

    def set_power(self, on: bool, output=None) -> bool:
        ok = False
        for path in self._devices():
            try:
                with open(path, "w") as fh:
                    fh.write("0" if on else "4")
                ok = True
            except OSError:
                continue
        return ok

    def get_power(self, output=None):
        for path in self._devices():
            try:
                with open(path) as fh:
                    return fh.read().strip() == "0"
            except OSError:
                continue
        return None


class NullBackend(Backend):
    name = "none"
    description = "no display control available (no-op)"

    def probe(self) -> bool:
        return True

    def set_power(self, on: bool, output=None) -> bool:
        logger.debug("null backend: display would go %s", "on" if on else "off")
        return True


#: Probe order. Layout-preserving backends first, then the blunter ones.
BACKEND_ORDER = (
    WlopmBackend, XsetBackend, WlrRandrBackend, XrandrBackend,
    BacklightBackend, VcgencmdBackend, CecBackend, FbconBackend, NullBackend,
)

BACKENDS_BY_NAME = {cls.name: cls for cls in BACKEND_ORDER}


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class DisplayController:
    """Picks a backend (or an explicit chain of them) and drives the panel.

    ``backend_spec`` is ``"auto"`` or a comma-separated list of backend names.
    Listing several is useful in practice, e.g. ``"xset,cec"`` turns the Pi's
    output off *and* asks the TV to go to standby.
    """

    def __init__(self, backend_spec: str = "auto", output: str = "auto"):
        self.backend_spec = (backend_spec or "auto").strip()
        self._output_is_auto = not output or output == "auto"
        self.output = None if self._output_is_auto else output
        self._backends: list = []
        self._last_known: bool | None = None
        self._detected_at = 0.0
        self.detect()

    # -- selection ---------------------------------------------------------

    def detect(self) -> list:
        """(Re)select backends. Returns the list of chosen backend names."""
        chosen: list = []
        self._pin_failed = False
        if self.backend_spec and self.backend_spec != "auto":
            usable = 0
            for name in (n.strip() for n in self.backend_spec.split(",")):
                cls = BACKENDS_BY_NAME.get(name)
                if cls is None:
                    logger.warning("Unknown display backend '%s' — ignoring", name)
                    continue
                backend = cls()
                if backend.probe() or name == "none":
                    usable += 1
                else:
                    logger.warning("Display backend '%s' is not usable here", name)
                chosen.append(backend)
            # A pin that cannot work would otherwise leave the panel with no
            # control at all. Fall back to autodetection and keep re-probing,
            # so a compositor starting later still gets picked up.
            if chosen and usable == 0:
                logger.warning("Pinned backend(s) '%s' unusable — falling back "
                               "to autodetection", self.backend_spec)
                self._pin_failed = True
                chosen = []

        if not chosen:
            for cls in BACKEND_ORDER:
                backend = cls()
                try:
                    if backend.probe():
                        chosen.append(backend)
                        break
                except Exception as exc:
                    logger.debug("probe %s failed: %s", cls.name, exc)

        if not chosen:
            chosen = [NullBackend()]

        for backend in chosen:
            try:
                backend.prepare()
            except Exception as exc:
                logger.debug("prepare %s failed: %s", backend.name, exc)

        self._backends = chosen
        self._detected_at = time.monotonic()
        # Re-resolve on every detect: a session change from X11 to Wayland
        # also renames the connector (HDMI-1 becomes HDMI-A-1).
        if self._output_is_auto:
            self.output = self._auto_output()
        logger.info("Display backend(s): %s | output: %s",
                    ", ".join(b.name for b in chosen), self.output or "all")
        return [b.name for b in chosen]

    def _auto_output(self):
        for backend in self._backends:
            try:
                names = backend.outputs()
            except Exception:
                continue
            if names:
                # Prefer a connected HDMI connector when there is a choice.
                for name in names:
                    if "HDMI" in name.upper():
                        return name
                return names[0]
        return None

    # -- introspection -----------------------------------------------------

    @property
    def backend_names(self) -> list:
        return [b.name for b in self._backends]

    @property
    def using_fallback(self) -> bool:
        """True when we settled for a backend that ignores the session.

        The daemon starts before the desktop does, and vcgencmd/backlight/fbcon
        all answer happily with no compositor running — so the first probe
        picks one of them and, without re-probing, would keep it forever even
        once labwc is up and wlopm is available. An explicitly pinned backend
        is never treated as a fallback: that is the user's decision to keep.
        """
        if self.backend_spec and self.backend_spec != "auto" \
                and not getattr(self, "_pin_failed", False):
            return False
        return not any(b.session_native for b in self._backends)

    def info(self) -> dict:
        return {
            "session_type": session_type(),
            "backend_spec": self.backend_spec,
            "backends": [
                {"name": b.name, "description": b.description,
                 "preserves_layout": b.preserves_layout}
                for b in self._backends
            ],
            "output": self.output,
            "available_outputs": self._backends[0].outputs() if self._backends else [],
            "power": self.get_power(),
            "last_known": self._last_known,
        }

    @staticmethod
    def survey() -> list:
        """Probe every backend — used by 'wallcal.sh display detect'."""
        report = []
        for cls in BACKEND_ORDER:
            backend = cls()
            try:
                usable = backend.probe()
            except Exception as exc:
                usable = False
                logger.debug("survey %s: %s", cls.name, exc)
            entry = {
                "name": backend.name,
                "description": backend.description,
                "available": usable,
                "preserves_layout": backend.preserves_layout,
                "session_native": backend.session_native,
                "outputs": [],
            }
            if usable:
                try:
                    entry["outputs"] = backend.outputs()
                except Exception:
                    pass
            report.append(entry)
        return report

    # -- control -----------------------------------------------------------

    def set_power(self, on: bool, force: bool = False) -> bool:
        if not force and self._last_known is not None and self._last_known == on:
            return True
        ok = False
        for backend in self._backends:
            try:
                ok = backend.set_power(on, self.output) or ok
            except Exception as exc:
                logger.warning("%s.set_power failed: %s", backend.name, exc)
        if ok:
            self._last_known = on
            logger.info("Display -> %s", "ON" if on else "OFF")
        else:
            logger.error("Failed to switch display %s via %s",
                         "on" if on else "off", ", ".join(self.backend_names))
        return ok

    def on(self) -> bool:
        return self.set_power(True)

    def off(self) -> bool:
        return self.set_power(False)

    def toggle(self) -> bool:
        current = self.get_power()
        return self.set_power(not (current if current is not None else True))

    def get_power(self):
        for backend in self._backends:
            try:
                state = backend.get_power(self.output)
            except Exception:
                state = None
            if state is not None:
                return state
        return self._last_known

    def set_rotation(self, transform: str) -> bool:
        """Rotate the output — 'normal', 'left', 'right' or 'inverted'."""
        for backend in self._backends + [WlrRandrBackend(), XrandrBackend()]:
            setter = getattr(backend, "set_transform", None)
            if setter is None:
                continue
            try:
                if backend.probe() and setter(transform, self.output):
                    return True
            except Exception:
                continue
        return False


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import json
    import sys

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
    action = sys.argv[1] if len(sys.argv) > 1 else "detect"
    ctl = DisplayController()
    if action == "on":
        ctl.set_power(True, force=True)
    elif action == "off":
        ctl.set_power(False, force=True)
    elif action == "toggle":
        ctl.toggle()
    elif action == "survey":
        print(json.dumps(DisplayController.survey(), indent=2))
    else:
        print(json.dumps(ctl.info(), indent=2))
