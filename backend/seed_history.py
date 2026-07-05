"""Seed synthetic day-histories for playback testing.

Inserts a plausible 2-hour loop drive around Nanjing (118.78, 32.04) on
multiple past days. Run once on the Pi with the same DB path as the server.

Usage (on the Pi):
    /home/mars/carMonitor/.venv/bin/python seed_history.py --days 3
"""

from __future__ import annotations

import argparse
import math
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

DB = "/home/mars/carMonitor/data/gps.db"

# Start point: somewhere in Nanjing
ORIGIN_LAT = 32.041
ORIGIN_LON = 118.778


def fake_trail(duration_min: int = 120, step_s: int = 1,
               origin_lat: float = ORIGIN_LAT,
               origin_lon: float = ORIGIN_LON):
    """A loose random walk of `duration_min` around the origin, 1 Hz."""
    lat = origin_lat
    lon = origin_lon
    heading = random.uniform(0, 360)
    speed_kmh = random.uniform(15, 45)
    out = []
    t0 = datetime.now(timezone.utc).replace(microsecond=0)
    for s in range(duration_min * 60):
        # Occasionally tweak heading; occasionally slow for traffic lights.
        if random.random() < 0.01:
            heading += random.uniform(-30, 30)
        if random.random() < 0.005:
            speed_kmh = max(0.0, speed_kmh + random.uniform(-15, 0))
        elif random.random() < 0.01:
            speed_kmh = min(60.0, speed_kmh + random.uniform(0, 10))
        # Convert speed to lat/lon delta (rough).
        # 1 degree lat ≈ 111 km; lon varies with cos(lat).
        d_km = speed_kmh * (step_s / 3600.0)
        d_lat = (d_km / 111.0) * math.cos(math.radians(heading))
        d_lon = (d_km / (111.0 * math.cos(math.radians(lat)))) * math.sin(math.radians(heading))
        lat += d_lat
        lon += d_lon
        cog = (heading + random.uniform(-3, 3)) % 360
        out.append((
            (t0 + timedelta(seconds=s)).isoformat(timespec="milliseconds"),
            round(lat, 6), round(lon, 6),
            round(20 + random.gauss(0, 5), 1),
            round(speed_kmh + random.gauss(0, 1.5), 1),
            round(cog, 0),
            1, 14, round(random.uniform(0.8, 1.5), 2), random.randint(38, 48),
        ))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3,
                    help="how many past days to seed")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("""CREATE TABLE IF NOT EXISTS fixes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        lat REAL NOT NULL, lon REAL NOT NULL,
        alt REAL, sog_kmh REAL, cog_deg REAL,
        fix INTEGER NOT NULL,
        sat_used INTEGER, hdop REAL, cn0_max INTEGER
    )""")
    conn.commit()

    today = datetime.now(timezone.utc).replace(microsecond=0)
    total = 0
    for d in range(args.days):
        # Stagger over past days.
        base = today - timedelta(days=d, hours=2)
        rows = fake_trail(duration_min=random.randint(60, 180))
        rows = [(base + timedelta(seconds=i), *r[1:]) for i, r in enumerate(rows)]
        conn.executemany(
            "INSERT INTO fixes (ts, lat, lon, alt, sog_kmh, cog_deg, fix, "
            "sat_used, hdop, cn0_max) VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        total += len(rows)
        print(f"  seeded {len(rows)} fixes for {(base).date()}")
    conn.commit()

    # Quick sanity.
    rs = conn.execute(
        "SELECT substr(ts,1,10) AS day, COUNT(*) FROM fixes GROUP BY day"
    ).fetchall()
    print(f"days now: {rs}")
    print(f"inserted total: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())