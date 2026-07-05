"""SIM7000C GPS reader.

Opens the AT command port and polls `AT+CGNSINF` once per second. Designed to
avoid pyserial's DTR toggle, which causes some SIM7000C CDC ACM firmwares to
disconnect the port (BrokenPipeError).

Returns a dict per fix with these keys:
    ts (ISO8601 UTC), fix (0/1), run (0/1), utc (raw SIM7000C time string),
    lat, lon, alt, sog_kmh, cog_deg, fix_mode, hdop, pdop, vdop,
    sat_view, sat_used, glonass_used, cn0_max, hpa, vpa.
"""

from __future__ import annotations

import errno
import fcntl
import glob
import logging
import os
import select
import termios
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("gpsd")

DEFAULT_PORT = "auto"        # auto-detect AT port at startup
DEFAULT_BAUD = 115200
POLL_INTERVAL_S = 1.0
RECONNECT_BACKOFF_S = 2.0
DETECT_BACKOFF_S = 5.0       # wait between auto-detect retries
PROBE_TIMEOUT_S = 0.6

# SIM7000C's CGNSINF leaves indices 19/20 (HPA/VPA) empty in current firmware.
# Estimate horizontal accuracy from HDOP × UERE so the UI has a useful number.
# 3.0 m is a conservative UERE for civilian GPS with a reasonable antenna.
GNSS_UERE_M = 3.0


@dataclass
class Fix:
    ts: str          # ISO8601 UTC
    run: int         # GNSS run status
    fix: int         # Fix status: 0 none, 1 fixed
    utc: str         # raw YYYYMMDDHHMMSS.sss
    lat: Optional[float]
    lon: Optional[float]
    alt: Optional[float]
    sog_kmh: Optional[float]
    cog_deg: Optional[float]
    fix_mode: Optional[int]
    hdop: Optional[float]
    pdop: Optional[float]
    vdop: Optional[float]
    sat_view: Optional[int]
    sat_used: Optional[int]
    glonass_used: Optional[int]
    cn0_max: Optional[int]
    hpa: Optional[float]
    vpa: Optional[float]

    def to_dict(self) -> dict:
        return asdict(self)


def _open_serial(path: str, baud: int) -> int:
    """Open serial port without touching DTR/RTS."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0                          # iflag = raw
    attrs[1] = 0                          # oflag = raw
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0                          # lflag = raw
    # baud encoded as B0 < baud < B4000000; pyserial-compatible map is
    # not exposed, so we look it up via termios module attributes.
    baud_const = getattr(termios, f"B{baud}", None)
    if baud_const is None:
        raise ValueError(f"unsupported baud: {baud}")
    attrs[4] = baud_const
    attrs[5] = baud_const
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
    return fd


def _read_until(fd: int, terminator: bytes, timeout: float) -> bytes:
    """Read until terminator seen or timeout. Non-blocking-safe."""
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        rlist, _, _ = select.select([fd], [], [], max(0.05, deadline - time.monotonic()))
        if not rlist:
            continue
        try:
            chunk = os.read(fd, 256)
        except BlockingIOError:
            continue
        if not chunk:
            raise OSError(errno.ENODEV, "serial returned 0 bytes")
        buf.extend(chunk)
        if terminator in buf:
            break
    return bytes(buf)


def _at_command(fd: int, cmd: str, timeout: float = 1.5) -> str:
    os.write(fd, (cmd + "\r\n").encode())
    raw = _read_until(fd, b"OK\r\n", timeout)
    return raw.decode(errors="ignore")


def _probe_at_port(path: str, baud: int = DEFAULT_BAUD) -> bool:
    """Send `AT` to `path` and return True iff it replies OK within the timeout."""
    try:
        fd = _open_serial(path, baud)
    except (FileNotFoundError, OSError, termios.error):
        return False
    try:
        os.write(fd, b"AT\r\n")
        try:
            raw = _read_until(fd, b"OK\r\n", PROBE_TIMEOUT_S)
        except OSError:
            return False
        return b"OK" in raw
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def detect_at_port(baud: int = DEFAULT_BAUD) -> Optional[str]:
    """Scan /dev/ttyUSB* for a SIM7000C AT command port.

    Strategy: prefer any stable symlink under /dev matching `sim7000c*`,
    otherwise probe every ttyUSB in lexical order. Returns the first port
    that replies OK to AT, or None if nothing answered.
    """
    # 1. Symlink short-circuit (set up by the udev rule).
    for path in sorted(glob.glob("/dev/sim7000c*")):
        if os.path.exists(path) and _probe_at_port(path, baud):
            log.info("auto-detect: using symlink %s", path)
            return path

    # 2. Generic probe of every ttyUSB.
    candidates = sorted(glob.glob("/dev/ttyUSB*"))
    for path in candidates:
        if _probe_at_port(path, baud):
            log.info("auto-detect: %s responds to AT", path)
            return path

    log.warning("auto-detect: no /dev/ttyUSB* answered AT")
    return None


def _parse_cgnsinf(line: str) -> Fix:
    """Parse one +CGNSINF: ... line (already stripped)."""
    body = line.split(":", 1)[1].strip()
    parts = [p.strip() for p in body.split(",")]
    # Pad to 21 fields
    while len(parts) < 21:
        parts.append("")

    def f(idx: int) -> Optional[float]:
        try:
            return float(parts[idx]) if parts[idx] else None
        except ValueError:
            return None

    def i(idx: int) -> Optional[int]:
        try:
            return int(parts[idx]) if parts[idx] else None
        except ValueError:
            return None

    run = i(0) or 0
    fix = i(1) or 0
    utc_raw = parts[2] or ""

    # Only surface lat/lon/alt/etc when we actually have a fix.
    has_fix = fix == 1
    lat = f(3) if has_fix else None
    lon = f(4) if has_fix else None
    alt = f(5) if has_fix else None
    sog = f(6) if has_fix else None
    cog = f(7) if has_fix else None
    hdop = f(10)
    hpa_raw = f(19)
    vpa_raw = f(20)
    # Fall back to HDOP × UERE when the module doesn't report HPA directly.
    hpa = hpa_raw if (hpa_raw is not None and hpa_raw > 0) \
          else (hdop * GNSS_UERE_M if hdop is not None else None)
    vpa = vpa_raw if (vpa_raw is not None and vpa_raw > 0) \
          else ((f(12) or 0.0) * GNSS_UERE_M if f(12) is not None else None)

    return Fix(
        ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        run=run,
        fix=fix,
        utc=utc_raw,
        lat=lat,
        lon=lon,
        alt=alt,
        sog_kmh=sog,
        cog_deg=cog,
        fix_mode=i(8),
        hdop=hdop,
        pdop=f(11),
        vdop=f(12),
        sat_view=i(14),
        sat_used=i(15),
        glonass_used=i(16),
        cn0_max=i(18),
        hpa=hpa,
        vpa=vpa,
    )


class GpsReader:
    """Background thread polling SIM7000C. Latest fix is always accessible.

    `port` may be:
      - "/dev/ttyUSB3" — explicit path, used as-is (no detection)
      - "auto"         — call detect_at_port() on each connect attempt
    """

    def __init__(self, port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD):
        self.port = port
        self.baud = baud
        self._latest: Optional[Fix] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._subscribers: list[threading.Event] = []
        self._active_port: Optional[str] = None    # last successfully opened port

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="gpsd", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ---- public --------------------------------------------------------

    def latest(self) -> Optional[Fix]:
        with self._lock:
            return self._latest

    def subscribe(self) -> threading.Event:
        ev = threading.Event()
        self._subscribers.append(ev)
        return ev

    def unsubscribe(self, ev: threading.Event) -> None:
        try:
            self._subscribers.remove(ev)
        except ValueError:
            pass

    def wait_for_next(self, ev: threading.Event, timeout: float) -> Optional[Fix]:
        ev.wait(timeout)
        return self.latest()

    # ---- internals -----------------------------------------------------

    def _publish(self, fix: Fix) -> None:
        with self._lock:
            self._latest = fix
        for ev in list(self._subscribers):
            ev.set()

    def _resolve_port(self) -> Optional[str]:
        """Pick which /dev/ttyUSB* to use this round."""
        if self.port != "auto":
            return self.port
        detected = detect_at_port(self.baud)
        if detected:
            self._active_port = detected
        return detected

    def _run(self) -> None:
        while not self._stop.is_set():
            fd: Optional[int] = None
            target = self._resolve_port()
            if not target:
                log.warning("no AT-capable port found, retrying in %.0fs",
                            DETECT_BACKOFF_S)
                self._stop.wait(DETECT_BACKOFF_S)
                continue
            try:
                log.info("opening %s @ %d", target, self.baud)
                fd = _open_serial(target, self.baud)
                # Ensure GPS is powered on. Module answers OK even if already on.
                _at_command(fd, "AT+CGNSPWR=1", timeout=2.0)
                log.info("GNSS power on, polling CGNSINF")
                while not self._stop.is_set():
                    try:
                        resp = _at_command(fd, "AT+CGNSINF", timeout=2.0)
                    except OSError as e:
                        log.warning("serial read failed: %s", e)
                        break
                    for raw in resp.splitlines():
                        line = raw.strip()
                        if line.startswith("+CGNSINF:"):
                            try:
                                fix = _parse_cgnsinf(line)
                            except Exception:
                                log.exception("bad CGNSINF line: %r", line)
                                continue
                            self._publish(fix)
                    # Pace ourselves; module is happy with 1Hz.
                    self._stop.wait(POLL_INTERVAL_S)
            except FileNotFoundError:
                log.error("serial port %s missing, retrying in %.0fs",
                          target, RECONNECT_BACKOFF_S)
                self._stop.wait(RECONNECT_BACKOFF_S)
            except (OSError, termios.error) as e:
                log.warning("serial error on %s: %s, retrying in %.0fs",
                            target, e, RECONNECT_BACKOFF_S)
                self._stop.wait(RECONNECT_BACKOFF_S)
            except Exception:
                log.exception("unexpected error on %s, retrying in %.0fs",
                              target, RECONNECT_BACKOFF_S)
                self._stop.wait(RECONNECT_BACKOFF_S)
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                # If we were on auto and the explicit path failed, force
                # re-detection next round (the device may have re-enumerated).
                if self.port == "auto":
                    self._active_port = None


def main() -> None:  # CLI smoke test
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    r = GpsReader()
    r.start()
    try:
        for _ in range(5):
            time.sleep(1.1)
            f = r.latest()
            if f:
                print(json.dumps(f.to_dict(), ensure_ascii=False))
    finally:
        r.stop()


if __name__ == "__main__":
    main()