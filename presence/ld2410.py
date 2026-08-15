"""
HLK-LD2410C 24 GHz mmWave presence radar — UART driver.

The sensor streams "target data" frames continuously at 256000 baud 8N1.
It also accepts a small command set (wrapped in a different frame type) that
lets us read/write the detection gates, per-gate sensitivity and the built-in
unmanned duration.

Frame layout
------------
  report : F4 F3 F2 F1 | len(u16 LE) | payload | F8 F7 F6 F5
  command: FD FC FB FA | len(u16 LE) | payload | 04 03 02 01

Report payload (basic mode, type 0x02)
  0      data type       0x01 engineering / 0x02 basic
  1      head marker     0xAA
  2      target state    0=none 1=moving 2=stationary 3=both
  3..4   moving distance      u16 LE, cm
  5      moving energy        0..100
  6..7   stationary distance  u16 LE, cm
  8      stationary energy    0..100
  9..10  detection distance   u16 LE, cm
  (engineering mode appends per-gate energies, light level and OUT pin state)
  -2     tail marker     0x55
  -1     checksum        0x00

Each "gate" covers 0.75 m, gates 0..8, so the sensor tops out at 6 m.
"""

from __future__ import annotations

import glob
import logging
import struct
import time
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("wallcal.ld2410")

# --- Framing ---------------------------------------------------------------

REPORT_HEADER = b"\xF4\xF3\xF2\xF1"
REPORT_FOOTER = b"\xF8\xF7\xF6\xF5"
COMMAND_HEADER = b"\xFD\xFC\xFB\xFA"
COMMAND_FOOTER = b"\x04\x03\x02\x01"

# --- Commands --------------------------------------------------------------

CMD_ENABLE_CONFIG = 0x00FF
CMD_END_CONFIG = 0x00FE
CMD_SET_MAX_GATES = 0x0060
CMD_READ_PARAMS = 0x0061
CMD_ENGINEERING_ON = 0x0062
CMD_ENGINEERING_OFF = 0x0063
CMD_SET_SENSITIVITY = 0x0064
CMD_READ_FIRMWARE = 0x00A0
CMD_SET_BAUDRATE = 0x00A1
CMD_FACTORY_RESET = 0x00A2
CMD_RESTART = 0x00A3

# --- Target states ---------------------------------------------------------

TARGET_NONE = 0x00
TARGET_MOVING = 0x01
TARGET_STATIONARY = 0x02
TARGET_BOTH = 0x03

TARGET_NAMES = {
    TARGET_NONE: "none",
    TARGET_MOVING: "moving",
    TARGET_STATIONARY: "stationary",
    TARGET_BOTH: "moving+stationary",
}

GATE_SIZE_CM = 75
MAX_GATE = 8

#: Ports worth probing, in the order they are most likely to be the sensor.
#: ttyAMA0 first because that is the real PL011 once Bluetooth is moved aside.
CANDIDATE_PORTS = (
    "/dev/ttyAMA0",
    "/dev/serial0",
    "/dev/ttyS0",
    "/dev/ttyAMA1",
    "/dev/ttyUSB0",
    "/dev/ttyUSB1",
    "/dev/ttyACM0",
)

#: 256000 is the factory default; the rest cover sensors somebody reflashed.
CANDIDATE_BAUDS = (256000, 115200, 460800, 57600, 38400, 19200, 9600)


class LD2410Error(RuntimeError):
    """Raised when the sensor misbehaves or cannot be reached."""


def cm_to_gate(cm: int) -> int:
    """Convert a distance in cm to the smallest gate that covers it."""
    gate = int((int(cm) + GATE_SIZE_CM - 1) // GATE_SIZE_CM)
    return max(1, min(MAX_GATE, gate))


def gate_to_cm(gate: int) -> int:
    return int(gate) * GATE_SIZE_CM


@dataclass
class Reading:
    """One decoded target-data frame."""

    target_state: int = TARGET_NONE
    moving_distance_cm: int = 0
    moving_energy: int = 0
    stationary_distance_cm: int = 0
    stationary_energy: int = 0
    detection_distance_cm: int = 0
    engineering: bool = False
    moving_gate_energy: list = field(default_factory=list)
    stationary_gate_energy: list = field(default_factory=list)
    light: int | None = None
    out_pin: bool | None = None
    timestamp: float = field(default_factory=time.time)

    @property
    def has_target(self) -> bool:
        return self.target_state != TARGET_NONE

    @property
    def state_name(self) -> str:
        return TARGET_NAMES.get(self.target_state, "unknown")

    @property
    def distance_cm(self) -> int:
        """Best single distance estimate for the closest detected target.

        A stationary target reported at 0 cm means "not detected", so only
        non-zero candidates are considered.
        """
        candidates = []
        if self.target_state in (TARGET_MOVING, TARGET_BOTH) and self.moving_distance_cm:
            candidates.append(self.moving_distance_cm)
        if self.target_state in (TARGET_STATIONARY, TARGET_BOTH) and self.stationary_distance_cm:
            candidates.append(self.stationary_distance_cm)
        if candidates:
            return min(candidates)
        return self.detection_distance_cm

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state_name"] = self.state_name
        d["distance_cm"] = self.distance_cm
        d["has_target"] = self.has_target
        return d


@dataclass
class Parameters:
    """Result of a "read configuration" command."""

    max_gate: int = MAX_GATE
    max_moving_gate: int = MAX_GATE
    max_stationary_gate: int = MAX_GATE
    moving_sensitivity: list = field(default_factory=list)
    stationary_sensitivity: list = field(default_factory=list)
    unmanned_duration_s: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["max_moving_distance_cm"] = gate_to_cm(self.max_moving_gate)
        d["max_stationary_distance_cm"] = gate_to_cm(self.max_stationary_gate)
        return d


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def parse_report_payload(payload: bytes) -> Reading | None:
    """Decode a report payload (the bytes between length and footer)."""
    if len(payload) < 13:
        return None
    data_type = payload[0]
    if payload[1] != 0xAA:
        return None

    r = Reading(
        target_state=payload[2],
        moving_distance_cm=_u16(payload, 3),
        moving_energy=payload[5],
        stationary_distance_cm=_u16(payload, 6),
        stationary_energy=payload[8],
        detection_distance_cm=_u16(payload, 9),
        engineering=(data_type == 0x01),
    )

    if r.engineering and len(payload) >= 13:
        # 11: max moving gate, 12: max stationary gate, then two energy arrays.
        try:
            max_moving_gate = payload[11]
            max_static_gate = payload[12]
            pos = 13
            n_move = max_moving_gate + 1
            n_static = max_static_gate + 1
            r.moving_gate_energy = list(payload[pos:pos + n_move])
            pos += n_move
            r.stationary_gate_energy = list(payload[pos:pos + n_static])
            pos += n_static
            # LD2410C appends a photosensitive value and the OUT pin level
            # before the 0x55/0x00 trailer. Older firmware omits them.
            if len(payload) - pos >= 4:
                r.light = payload[pos]
                r.out_pin = bool(payload[pos + 1])
        except IndexError:
            logger.debug("Truncated engineering frame (%d bytes)", len(payload))

    return r


class LD2410:
    """Serial connection to an LD2410C.

    Usable as a context manager::

        with LD2410("/dev/ttyAMA0") as radar:
            for reading in radar.stream():
                print(reading.distance_cm)
    """

    def __init__(self, port: str, baudrate: int = 256000, timeout: float = 1.0):
        self.port = port
        self.baudrate = int(baudrate)
        self.timeout = timeout
        self._serial = None
        self._buffer = bytearray()
        self._in_config = False

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> "LD2410":
        try:
            import serial  # pyserial
        except ImportError as exc:  # pragma: no cover - install-time problem
            raise LD2410Error(
                "pyserial is not installed — run 'pip install pyserial'"
            ) from exc

        try:
            self._serial = serial.Serial(
                self.port,
                self.baudrate,
                timeout=self.timeout,
                write_timeout=2.0,
            )
        except Exception as exc:
            raise LD2410Error(f"cannot open {self.port} @ {self.baudrate}: {exc}") from exc

        self._buffer.clear()
        self._serial.reset_input_buffer()
        logger.debug("Opened %s @ %d", self.port, self.baudrate)
        return self

    def close(self) -> None:
        if self._serial is not None:
            try:
                if self._in_config:
                    self.end_config()
            except Exception:
                pass
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    def __enter__(self) -> "LD2410":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    # -- report stream -----------------------------------------------------

    def read(self, max_wait: float = 1.0) -> Reading | None:
        """Return the next decoded report frame, or None on timeout."""
        if self._serial is None:
            raise LD2410Error("port is not open")

        # The sensor reports faster than callers consume, so the buffer often
        # already holds a whole frame — check it before waiting on the port.
        reading = self._extract_report()
        if reading is not None:
            return reading

        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            chunk = self._read_bytes()
            if not chunk:
                time.sleep(0.005)
                continue
            self._buffer.extend(chunk)
            # Keep the buffer from growing without bound if the wiring is
            # noisy and we never find a valid frame.
            if len(self._buffer) > 4096:
                del self._buffer[:-2048]
            reading = self._extract_report()
            if reading is not None:
                return reading
        return None

    def _read_bytes(self) -> bytes:
        """Read whatever is waiting, translating driver errors.

        pyserial raises SerialException when the fd reports itself readable
        but yields nothing — a sensor that was unplugged, or another process
        holding the same port. Callers only ever want to see LD2410Error.
        """
        try:
            waiting = self._serial.in_waiting
            return self._serial.read(max(1, waiting or 1))
        except Exception as exc:
            raise LD2410Error(f"{self.port}: {exc}") from exc

    def stream(self, max_wait: float = 1.0):
        """Yield readings forever; yields None whenever a read times out."""
        while True:
            yield self.read(max_wait=max_wait)

    def _extract_report(self) -> Reading | None:
        while True:
            start = self._buffer.find(REPORT_HEADER)
            if start < 0:
                # Nothing usable; keep the last 3 bytes in case a header
                # straddles the chunk boundary.
                if len(self._buffer) > 3:
                    del self._buffer[:-3]
                return None
            if start > 0:
                del self._buffer[:start]
            if len(self._buffer) < 6:
                return None

            length = _u16(bytes(self._buffer), 4)
            total = 4 + 2 + length + 4
            if length > 512:
                # Bogus length — resync past this header.
                del self._buffer[:4]
                continue
            if len(self._buffer) < total:
                return None

            frame = bytes(self._buffer[:total])
            del self._buffer[:total]
            if not frame.endswith(REPORT_FOOTER):
                continue

            reading = parse_report_payload(frame[6:6 + length])
            if reading is not None:
                return reading

    # -- command channel ---------------------------------------------------

    def _send_command(self, command: int, payload: bytes = b"",
                      expect_ack: bool = True, timeout: float = 1.5):
        if self._serial is None:
            raise LD2410Error("port is not open")

        body = struct.pack("<H", command) + payload
        frame = COMMAND_HEADER + struct.pack("<H", len(body)) + body + COMMAND_FOOTER
        try:
            self._serial.write(frame)
            self._serial.flush()
        except Exception as exc:
            raise LD2410Error(f"{self.port}: write failed: {exc}") from exc
        if not expect_ack:
            return None
        return self._await_ack(command, timeout=timeout)

    def _await_ack(self, command: int, timeout: float = 1.5) -> bytes:
        """Wait for the ACK matching ``command``; returns the ACK payload."""
        expected = command | 0x0100
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self._read_bytes()
            if chunk:
                self._buffer.extend(chunk)
            ack = self._extract_ack(expected)
            if ack is not None:
                status = _u16(ack, 2)
                if status != 0:
                    raise LD2410Error(
                        f"command 0x{command:04X} rejected (status {status})"
                    )
                return ack[4:]
        raise LD2410Error(f"timed out waiting for ACK of command 0x{command:04X}")

    def _extract_ack(self, expected: int) -> bytes | None:
        search_from = 0
        while True:
            start = self._buffer.find(COMMAND_HEADER, search_from)
            if start < 0:
                return None
            if len(self._buffer) < start + 6:
                return None
            length = _u16(bytes(self._buffer), start + 4)
            total = start + 6 + length + 4
            if length > 512:
                search_from = start + 4
                continue
            if len(self._buffer) < total:
                return None

            frame = bytes(self._buffer[start:total])
            payload = frame[6:6 + length]
            if frame.endswith(COMMAND_FOOTER) and len(payload) >= 4 \
                    and _u16(payload, 0) == expected:
                del self._buffer[:total]
                return payload
            search_from = start + 4

    # -- configuration mode ------------------------------------------------

    def enable_config(self) -> None:
        self._send_command(CMD_ENABLE_CONFIG, struct.pack("<H", 0x0001))
        self._in_config = True

    def end_config(self) -> None:
        try:
            self._send_command(CMD_END_CONFIG)
        finally:
            self._in_config = False

    def _configured(self, fn, *args, **kwargs):
        """Run ``fn`` inside an enable/end config bracket."""
        already = self._in_config
        if not already:
            self.enable_config()
        try:
            return fn(*args, **kwargs)
        finally:
            if not already:
                self.end_config()

    # -- high level operations --------------------------------------------

    def firmware_version(self) -> str:
        def _do():
            payload = self._send_command(CMD_READ_FIRMWARE)
            if len(payload) < 6:
                return "unknown"
            major = payload[3]
            minor = payload[2]
            build = struct.unpack_from("<I", payload, 4)[0]
            return f"{major}.{minor:02d}.{build:08X}"
        return self._configured(_do)

    def read_parameters(self) -> Parameters:
        def _do():
            payload = self._send_command(CMD_READ_PARAMS)
            if len(payload) < 2 or payload[0] != 0xAA:
                raise LD2410Error("malformed parameter response")
            max_gate = payload[1]
            max_moving = payload[2]
            max_static = payload[3]
            n = max_gate + 1
            moving = list(payload[4:4 + n])
            static = list(payload[4 + n:4 + 2 * n])
            duration = _u16(payload, 4 + 2 * n) if len(payload) >= 6 + 2 * n else 0
            return Parameters(
                max_gate=max_gate,
                max_moving_gate=max_moving,
                max_stationary_gate=max_static,
                moving_sensitivity=moving,
                stationary_sensitivity=static,
                unmanned_duration_s=duration,
            )
        return self._configured(_do)

    def set_max_gates(self, moving_gate: int, stationary_gate: int,
                      unmanned_duration_s: int) -> None:
        """Program the sensor's own distance gates and hold time.

        Gates are coarse (0.75 m each); WallCal still applies an exact
        centimetre threshold in software on top of this.
        """
        moving_gate = max(1, min(MAX_GATE, int(moving_gate)))
        stationary_gate = max(1, min(MAX_GATE, int(stationary_gate)))
        unmanned_duration_s = max(0, min(65535, int(unmanned_duration_s)))

        payload = (
            struct.pack("<HI", 0x0000, moving_gate)
            + struct.pack("<HI", 0x0001, stationary_gate)
            + struct.pack("<HI", 0x0002, unmanned_duration_s)
        )
        self._configured(self._send_command, CMD_SET_MAX_GATES, payload)

    def set_sensitivity(self, gate: int | None, moving: int, stationary: int) -> None:
        """Set per-gate sensitivity. ``gate=None`` applies to every gate."""
        gate_value = 0xFFFF if gate is None else max(0, min(MAX_GATE, int(gate)))
        payload = (
            struct.pack("<HI", 0x0000, gate_value)
            + struct.pack("<HI", 0x0001, max(0, min(100, int(moving))))
            + struct.pack("<HI", 0x0002, max(0, min(100, int(stationary))))
        )
        self._configured(self._send_command, CMD_SET_SENSITIVITY, payload)

    def set_engineering_mode(self, enabled: bool) -> None:
        cmd = CMD_ENGINEERING_ON if enabled else CMD_ENGINEERING_OFF
        self._configured(self._send_command, cmd)

    def restart(self) -> None:
        self._configured(self._send_command, CMD_RESTART, expect_ack=False)
        self._in_config = False

    def factory_reset(self) -> None:
        self._configured(self._send_command, CMD_FACTORY_RESET)


# ---------------------------------------------------------------------------
# Autodetection
# ---------------------------------------------------------------------------

def available_ports() -> list:
    """Serial devices present on this machine, best candidates first.

    /dev/serial0 is a symlink to whichever UART is on the header, so paths are
    resolved and de-duplicated — otherwise a scan probes the same device twice.
    """
    import os

    found, seen = [], set()

    def add(path):
        if not os.path.exists(path):
            return
        real = os.path.realpath(path)
        if real in seen:
            return
        seen.add(real)
        found.append(real)

    for path in CANDIDATE_PORTS:
        add(path)
    for path in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
                       + glob.glob("/dev/ttyAMA*")):
        add(path)
    return found


def probe(port: str, baudrate: int = 256000, seconds: float = 1.2) -> Reading | None:
    """Return a Reading if a live LD2410 answers on this port/baud.

    Never raises: a port that cannot be opened, is held by another process or
    dies mid-read is simply "not the sensor", so a scan moves on to the next.
    """
    try:
        radar = LD2410(port, baudrate, timeout=0.3).open()
    except LD2410Error as exc:
        logger.debug("probe %s@%d: %s", port, baudrate, exc)
        return None
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            reading = radar.read(max_wait=0.4)
            if reading is not None:
                return reading
    except LD2410Error as exc:
        logger.debug("probe %s@%d: %s", port, baudrate, exc)
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("probe %s@%d failed unexpectedly: %s", port, baudrate, exc)
        return None
    finally:
        radar.close()
    return None


def autodetect(ports=None, bauds=None, seconds: float = 1.2):
    """Scan ports/bauds for a sensor.

    Returns ``(port, baudrate, Reading)`` or ``(None, None, None)``.
    """
    ports = list(ports) if ports else available_ports()
    bauds = list(bauds) if bauds else list(CANDIDATE_BAUDS)
    for port in ports:
        for baud in bauds:
            reading = probe(port, baud, seconds=seconds)
            if reading is not None:
                logger.info("LD2410 found on %s @ %d", port, baud)
                return port, baud, reading
    return None, None, None
