"""One-off: configure SIM7000C for China Telecom NB-IoT and watch registration."""

import os
import sys
import time
import select

sys.path.insert(0, os.path.dirname(__file__))
from gpsd import _open_serial


def q(fd, c, w=2.0):
    """Send AT command, read until OK/ERROR or timeout."""
    while True:
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            break
        try:
            os.read(fd, 4096)
        except Exception:
            break
    os.write(fd, (c + "\r\n").encode())
    time.sleep(w)
    buf = b""
    deadline = time.time() + w
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                buf += os.read(fd, 4096)
            except Exception:
                break
        if b"OK\r\n" in buf or b"ERROR" in buf:
            break
    return buf.decode(errors="ignore")


def drain(fd, t):
    buf = b""
    while True:
        r, _, _ = select.select([fd], [], [], t)
        if not r:
            break
        try:
            buf += os.read(fd, 4096)
        except Exception:
            break
    return buf


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB3"
    fd = _open_serial(port, 115200)
    print(f"=== probe {port} ===\n")

    print("--- initial state ---")
    for c in [
        "AT+CCID", "AT+CIMI", "AT+CSQ", "AT+CREG?", "AT+CEREG?",
        "AT+COPS?", "AT+CPSI?", "AT+CGDCONT?", "AT+CNMP?", "AT+CMNB?",
    ]:
        print(f"{c} => {q(fd, c).strip()}")
    print()

    print("--- configure for China Telecom NB-IoT ---")
    for c in [
        "AT+CNMP=38",                # LTE Cat-M1 + NB-IoT
        "AT+CMNB=2",                 # NB-IoT preferred
        'AT+CGDCONT=1,"IP","ctwing"',
        "AT+CFUN=0", "AT+CFUN=1",
        "AT+COPS=0",
    ]:
        print(f"{c} => {q(fd, c).strip()}")
    print()

    print("--- 60s monitoring ---")
    for i in range(20):
        time.sleep(3)
        urc = drain(fd, 0.2).decode(errors="ignore").replace("\r", "").strip()
        os.write(fd, b"AT+CSQ;+CEREG?;+CPSI?;+COPS?;+CGATT?\r\n")
        cmd = drain(fd, 0.6).decode(errors="ignore").replace("\r\n", " | ").strip()
        print(f"[{(i + 1) * 3:>2}s] urc=[{urc[:60]}]  cmd=[{cmd}]")
    print()

    print("--- final state ---")
    for c in [
        "AT+CSQ", "AT+CEREG?", "AT+CPSI?", "AT+CGATT?",
        "AT+CGPADDR=1", "AT+CGCONTRDP=1",
    ]:
        print(f"{c} => {q(fd, c, 3.0).strip()}")

    os.close(fd)


if __name__ == "__main__":
    main()