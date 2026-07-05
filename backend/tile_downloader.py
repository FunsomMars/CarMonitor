"""Pre-download 高德 (AMap) raster tiles for a bounding box and zoom range.

Usage:
    python tile_downloader.py --bbox 120.85,30.70,122.10,31.65 --z 3-14 \\
        --out ../frontend/tiles --workers 24

Bounding box format: min_lng,min_lat,max_lng,max_lat (WGS84/GCJ02 — for the
AMap raster layer, GCJ02 is fine because the source tiles are already in that
coordinate system).

Default bbox covers greater Shanghai. Tiles that already exist on disk are
skipped, so re-running resumes an interrupted download.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

log = logging.getLogger("tile_dl")

AMAP_TILE = ("https://webrd0{s}.is.autonavi.com/appmaptile"
             "?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}")


def deg2tile(lat: float, lng: float, z: int) -> tuple[int, int]:
    """Return the (x,y) tile at the given zoom containing this WGS84 point."""
    n = 2 ** z
    x = int((lng + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad))
             / math.pi) / 2.0 * n)
    return x, y


def tiles_for_bbox(bbox: tuple[float, float, float, float],
                   z: int) -> list[tuple[int, int]]:
    """All (x, y) tiles at zoom `z` overlapping the bbox (WGS84)."""
    min_lng, min_lat, max_lng, max_lat = bbox
    x_min, y_max = deg2tile(min_lat, min_lng, z)
    x_max, y_min = deg2tile(max_lat, max_lng, z)
    x_min, x_max = sorted((x_min, x_max))
    y_min, y_max = sorted((y_min, y_max))
    # Clamp to valid tile ranges.
    n = 1 << z
    x_min = max(0, min(n - 1, x_min))
    x_max = max(0, min(n - 1, x_max))
    y_min = max(0, min(n - 1, y_min))
    y_max = max(0, min(n - 1, y_max))
    return [(x, y) for x in range(x_min, x_max + 1)
            for y in range(y_min, y_max + 1)]


def _fetch_one(out_dir: Path, z: int, x: int, y: int,
               timeout: float = 8.0, retries: int = 3) -> tuple[bool, str]:
    path = out_dir / str(z) / str(x) / f"{y}.png"
    if path.exists() and path.stat().st_size > 0:
        return True, "cached"
    path.parent.mkdir(parents=True, exist_ok=True)
    last_err = ""
    for attempt in range(retries):
        sub = (x + y + z + attempt) % 4 + 1   # rotate subdomain on retry
        url = AMAP_TILE.format(s=sub, x=x, y=y, z=z)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "carMonitor/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if len(data) < 200:
                last_err = "empty"
            else:
                path.write_bytes(data)
                return True, "fetched"
        except Exception as e:
            last_err = f"err:{type(e).__name__}"
        time.sleep(0.4 + attempt * 0.3)   # back off before retry
    return False, last_err


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bbox", default="120.85,30.70,122.10,31.65",
                    help="min_lng,min_lat,max_lng,max_lat (default: 大上海)")
    ap.add_argument("-z", "--zoom", default="3-14",
                    help="zoom range, e.g. 3-14 (inclusive)")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parent.parent
                                / "frontend" / "tiles")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--report", type=int, default=50_000,
                    help="progress log every N tiles")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    bbox = tuple(float(x) for x in args.bbox.split(","))  # type: ignore
    if len(bbox) != 4:
        ap.error("--bbox must have 4 comma-separated values")
    z_min, z_max = (int(x) for x in args.zoom.split("-"))
    args.out.mkdir(parents=True, exist_ok=True)

    log.info("bbox=%s zoom=%d-%d out=%s workers=%d",
             bbox, z_min, z_max, args.out, args.workers)

    grand_total = 0
    grand_fetched = 0
    grand_cached = 0
    grand_failed = 0
    t_start = time.monotonic()

    for z in range(z_min, z_max + 1):
        coords = tiles_for_bbox(bbox, z)  # type: ignore
        log.info("zoom %d: %d tiles", z, len(coords))
        if not coords:
            continue

        z_total = len(coords)
        z_fetched = z_cached = z_failed = 0
        z_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {
                ex.submit(_fetch_one, args.out, z, x, y): (x, y)
                for x, y in coords
            }
            for i, fut in enumerate(as_completed(futures), 1):
                ok, status = fut.result()
                if status == "cached":
                    z_cached += 1
                elif ok:
                    z_fetched += 1
                else:
                    z_failed += 1
                if i % args.report == 0:
                    elapsed = time.monotonic() - z_start
                    rate = i / max(elapsed, 0.001)
                    eta = (z_total - i) / max(rate, 0.001)
                    log.info("  z=%d %d/%d (%.0f t/s, ETA %.0fs) "
                             "fetched=%d cached=%d failed=%d",
                             z, i, z_total, rate, eta,
                             z_fetched, z_cached, z_failed)

        z_elapsed = time.monotonic() - z_start
        log.info("zoom %d done in %.1fs: fetched=%d cached=%d failed=%d",
                 z, z_elapsed, z_fetched, z_cached, z_failed)
        grand_total += z_total
        grand_fetched += z_fetched
        grand_cached += z_cached
        grand_failed += z_failed

    log.info("ALL DONE in %.1fs: total=%d fetched=%d cached=%d failed=%d",
             time.monotonic() - t_start,
             grand_total, grand_fetched, grand_cached, grand_failed)
    return 0 if grand_failed * 10 < grand_total else 1


if __name__ == "__main__":
    sys.exit(main())