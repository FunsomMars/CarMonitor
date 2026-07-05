"""FastAPI app exposing GPS data over HTTP and WebSocket.

GET  /                    -> frontend/index.html
GET  /api/status          -> module status + last fix (compact)
GET  /api/history         -> recent trail (?since=600) | single day (?date=) |
                             free range (?start=&end= in ISO 8601 UTC)
GET  /api/days            -> per-day summary for the playback picker (legacy)
GET  /api/dashboard       -> KPIs / daily / heatmap / histogram / top trips
GET  /api/data/days       -> per-day stats (count, distance, max speed)
POST /api/data/delete     -> delete by range / days / all
WS   /ws                  -> stream of fix dicts as JSON, ~1 Hz
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from gpsd import GpsReader
from store import FixStore

log = logging.getLogger("app")

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DB_PATH = os.environ.get("CARMONITOR_DB", "/var/lib/carmonitor/gps.db")
SERIAL_PORT = os.environ.get("CARMONITOR_PORT", "auto")

reader: Optional[GpsReader] = None
store: Optional[FixStore] = None
clients: set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global reader, store
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    reader = GpsReader(port=SERIAL_PORT)
    store = FixStore(db_path=DB_PATH)
    reader.start()
    store.start()
    asyncio.create_task(broadcast_loop())
    log.info("carmonitor started")
    try:
        yield
    finally:
        log.info("carmonitor stopping")
        if reader:
            reader.stop()
        if store:
            store.stop()


app = FastAPI(title="carMonitor", lifespan=lifespan)


# ---- broadcast -----------------------------------------------------------

async def broadcast_loop() -> None:
    """Push every new fix to all connected WebSocket clients.

    The GPS reader updates at ~1 Hz, so we wake on a fast tick (50 ms) but
    only broadcast when the latest fix timestamp changes — otherwise we'd
    spam the same point 20x per second to every client.
    """
    assert reader is not None and store is not None
    last_broadcast_ts: str | None = None
    while True:
        await asyncio.sleep(0.05)
        latest = reader.latest()
        if latest is None:
            continue
        # Persist before pushing so REST sees freshest data.
        store.submit(latest.to_dict())
        if not clients:
            last_broadcast_ts = latest.ts
            continue
        if latest.ts == last_broadcast_ts:
            continue
        last_broadcast_ts = latest.ts
        payload = json.dumps(latest.to_dict(), ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in list(clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.discard(ws)


# ---- HTTP ----------------------------------------------------------------

@app.get("/api/status")
async def api_status():
    if reader is None:
        return JSONResponse({"error": "not started"}, status_code=503)
    fix = reader.latest()
    if fix is None:
        return {"fix": None, "subscribers": len(clients)}
    d = fix.to_dict()
    d["subscribers"] = len(clients)
    return d


@app.get("/api/history")
async def api_history(
    since: int | None = Query(None, ge=1, le=86400),
    date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    start: str | None = Query(None, description="ISO 8601 UTC, inclusive"),
    end: str | None = Query(None, description="ISO 8601 UTC, inclusive"),
    max_points: int = Query(8000, ge=100, le=50000),
):
    if store is None:
        return JSONResponse({"error": "not started"}, status_code=503)
    if start is not None or end is not None:
        # Free time range. Default missing end to "now".
        s = start or "1970-01-01T00:00:00Z"
        e = end or datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = store.range_history(start=s, end=e, max_points=max_points)
        return {"mode": "range", "start": s, "end": e,
                "count": len(rows), "rows": [_row(r) for r in rows]}
    if date is not None:
        rows = store.day_history(date=date, max_points=max_points)
        return {"mode": "day", "date": date, "count": len(rows),
                "rows": [_row(r) for r in rows]}
    if since is None:
        since = 600
    rows = store.history(since_s=since, limit=max_points)
    return {"mode": "recent", "since_s": since, "count": len(rows),
            "rows": [_row(r) for r in rows]}


@app.get("/api/days")
async def api_days():
    if store is None:
        return JSONResponse({"error": "not started"}, status_code=503)
    return {"days": store.list_days()}


@app.get("/api/dashboard")
async def api_dashboard(days: int = Query(30, ge=1, le=365)):
    """Aggregated stats for the dashboard sidebar.

    Returns KPIs (total km, max/avg speed, trip count, fix count), per-day
    distance, 7×24 hour-of-week heatmap, speed histogram, top 5 trips, and
    a 7-day signal quality summary. Heavy: runs in-process and iterates
    every row in the window — fine for a single user at typical 1 Hz density.
    """
    if store is None:
        return JSONResponse({"error": "not started"}, status_code=503)
    return store.dashboard(days=days)


@app.get("/api/data/days")
async def api_data_days():
    """Per-day stats for the data management page.

    Each entry: {date, count, distance_km, max_speed_kmh, first, last}.
    """
    if store is None:
        return JSONResponse({"error": "not started"}, status_code=503)
    return {"days": store.day_stats()}


@app.post("/api/data/delete")
async def api_data_delete(body: dict):
    """Delete fixes by range, specific days, or all. Returns row count.

    Body shape (any ONE of these):
      {"all": true}
      {"days": ["2026-07-03", "2026-07-04"]}
      {"start": "2026-07-03T00:00:00Z", "end": "2026-07-04T23:59:59Z"}
    """
    if store is None:
        return JSONResponse({"error": "not started"}, status_code=503)
    if body.get("all"):
        n = store.delete_all()
        return {"deleted": n, "mode": "all"}
    days = body.get("days")
    if days:
        n = store.delete_days(days)
        return {"deleted": n, "mode": "days", "days": days}
    start = body.get("start")
    end = body.get("end")
    if start and end:
        n = store.delete_range(start, end)
        return {"deleted": n, "mode": "range",
                "start": start, "end": end}
    return JSONResponse(
        {"error": "must specify all=true, days=[…], or start+end"},
        status_code=400)


def _row(r) -> dict:
    return {
        "ts": r.ts,
        "lat": r.lat,
        "lon": r.lon,
        "alt": r.alt,
        "sog_kmh": r.sog_kmh,
        "cog_deg": r.cog_deg,
        "sat_used": r.sat_used,
        "hdop": r.hdop,
        "cn0_max": r.cn0_max,
    }


# ---- WebSocket -----------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    log.info("ws connected, total=%d", len(clients))
    # Send the latest fix immediately on connect so the map can centre.
    if reader and reader.latest() is not None:
        try:
            await ws.send_text(json.dumps(reader.latest().to_dict(),
                                          ensure_ascii=False))
        except Exception:
            pass
    try:
        while True:
            # Client can send pings; we don't need anything else.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        log.info("ws disconnected, total=%d", len(clients))


# ---- tile proxy / cache --------------------------------------------------

TILES_DIR = BASE_DIR / "frontend" / "tiles"
TILES_DIR.mkdir(parents=True, exist_ok=True)
AMAP_TILE = ("https://webrd0{s}.is.autonavi.com/appmaptile"
             "?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}")


def _tile_path(z: int, x: int, y: int) -> Path:
    return TILES_DIR / str(z) / str(x) / f"{y}.png"


@app.get("/tiles/{z:int}/{x:int}/{y:int}.png")
async def tile(z: int, x: int, y: int):
    """Serve a map tile. Reads from local cache; falls back to 高德 online and
    writes the response to cache so next request is fast (and offline-capable
    after a warmup pass)."""
    if not (0 <= z <= 19 and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
        return JSONResponse({"error": "tile out of range"}, status_code=404)

    cached = _tile_path(z, x, y)
    if cached.exists() and cached.stat().st_size > 500:
        return FileResponse(cached, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})
    # Stale or missing → refetch (also cleans up <500B polluted entries).

    # Cache miss → proxy 高德
    import urllib.request
    sub = (x + y + z) % 4 + 1   # webrd01..04 round-robin to spread load
    url = AMAP_TILE.format(s=sub, x=x, y=y, z=z)
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = r.read()
    except Exception as e:
        log.warning("tile fetch failed z=%d x=%d y=%d: %s", z, x, y, e)
        # Fallback: a 1×1 transparent png so the map doesn't show pink tiles.
        transparent = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                       b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
                       b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00"
                       b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00"
                       b"\x00IEND\xaeB`\x82")
        return Response(content=transparent, media_type="image/png")

    # Don't cache empty/error responses — they'll fill the disk with junk.
    if len(data) < 500:
        transparent = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                       b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
                       b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00"
                       b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00"
                       b"\x00IEND\xaeB`\x82")
        return Response(content=transparent, media_type="image/png")

    # Best-effort cache write; ignore errors so a slow disk doesn't 5xx the map.
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    except OSError as e:
        log.debug("cache write failed: %s", e)

    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


# ---- static --------------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
async def index():
    html = FRONTEND_DIR / "index.html"
    if not html.exists():
        return JSONResponse({"error": "frontend not built"}, status_code=404)
    return FileResponse(html)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()