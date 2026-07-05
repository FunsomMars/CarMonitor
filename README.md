# carMonitor

A real-time GPS web dashboard for a **SIM7000C** GNSS module connected to a
**Raspberry Pi 4B** over USB.

```
   SIM7000C  ──USB──>  Pi 4B
                       ├── /dev/ttyUSB2  AT commands (1Hz, CGNSINF)
                       └── backend/app.py  FastAPI + WebSocket
                              └── browser: Leaflet dark map, live trail
```

## Files

| Path | Purpose |
|---|---|
| `backend/gpsd.py` | SIM7000C serial reader, AT+CGNSINF poller, parser |
| `backend/store.py` | SQLite history (24h rolling window) |
| `backend/app.py` | FastAPI HTTP + WebSocket server |
| `frontend/index.html` | Leaflet map, stats panel, controls |
| `systemd/carmonitor.service` | systemd unit (auto-start on boot) |
| `requirements.txt` | Python deps |

## API

- `GET /` — dashboard
- `GET /api/status` — latest fix as JSON
- `GET /api/history?since=600` — trail from last N seconds
- `WS /ws` — live JSON stream, ~1 Hz

## Setup on Pi

```bash
sudo apt-get install -y python3-venv sqlite3
sudo mkdir -p /var/lib/carmonitor && sudo chown mars:mars /var/lib/carmonitor
cd ~ && git clone <this repo> carMonitor
cd carMonitor && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Smoke test (Ctrl-C after a few seconds)
.venv/bin/python backend/gpsd.py

# Start the server
.venv/bin/uvicorn --app-dir backend app:app --host 0.0.0.0 --port 8000

# As a service:
sudo cp systemd/carmonitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now carmonitor
sudo journalctl -fu carmonitor
```

Browse to `http://<pi-address>:8000/`.

## Troubleshooting

- **`/dev/ttyUSB2` missing** — `lsusb` should show `1e0e:9001 SimTech SIM7000`.
  Unplug/replug. The board presents 5 serial ports (ttyUSB0..4); we use **ttyUSB2**
  for AT commands and **ttyUSB1** carries raw NMEA if you want it.
- **Fix never reaches 1** — indoors the GPS can't see enough sky. Take the Pi
  outside or near a window; cold start can take a few minutes.
- **pyserial BrokenPipeError on open** — this is why we use raw `os.open` +
  `termios`; don't replace the serial layer with pyserial's default path.
- **Wrong port mapping** — set `CARMONITOR_PORT=/dev/ttyUSB1` (or whichever
  answered `AT\r\n` with `OK`) before launching the server.