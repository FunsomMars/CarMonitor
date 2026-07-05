"""SQLite-backed fix store. Single writer thread; readers come from the API.

History is kept per calendar day (UTC). No automatic GC — the user wants to
look back at past days. A simple space guard keeps the DB from exploding
when the device runs for months: oldest days are dropped beyond a soft cap.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from queue import Queue, Empty
from typing import Iterator, Optional

log = logging.getLogger("store")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two WGS84 points."""
    R = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _td(**kw) -> timedelta:
    """Tiny shim so we can write datetime.now() - _td(days=N) inline."""
    return timedelta(**kw)


def _parse_ts(ts: str) -> Optional[datetime]:
    """Parse an ISO 8601 ts, tolerating the historical T/space mix."""
    if not ts:
        return None
    s = ts.replace(" ", "T")
    try:
        # Allow trailing +00:00 etc.
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# Fast parsers used by the hot loops in dashboard()/day_stats(). Each row goes
# through these once or twice, so we deliberately avoid datetime.fromisoformat
# (which is ~5x slower than the integer-slicing variants below) and the
# sqlite3.Row wrapper (which adds ~1ms per row when bulk-converted to dict).

import calendar as _calendar

_WD_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_EPOCH = datetime(1970, 1, 1)


def _weekday_index(ts_str: str) -> int:
    """Return 0..6 (Mon..Sun) for a ts string `YYYY-MM-DDxHH:MM:SS…`.

    Cheaper than `datetime.fromisoformat(...).weekday()` because we slice
    integers instead of constructing a datetime object.
    """
    return _calendar.weekday(int(ts_str[0:4]), int(ts_str[5:7]), int(ts_str[8:10]))


def _to_epoch_s(ts_str: str) -> int:
    """Return seconds since 1970-01-01 UTC for a ts string.

    Ignores fractional seconds and the +HH:MM tz suffix (all current rows are
    UTC, so the offset cancels out for the gap comparisons we use this for).
    """
    Y, M, D = int(ts_str[0:4]), int(ts_str[5:7]), int(ts_str[8:10])
    h, mn, s = int(ts_str[11:13]), int(ts_str[14:16]), int(ts_str[17:19])
    days = (datetime(Y, M, D) - _EPOCH).days
    return days * 86400 + h * 3600 + mn * 60 + s


def _plus8_date_str(utc_date: str, hour_utc: int) -> str:
    """Return the UTC+8 (Beijing) calendar date for a UTC date + hour.

    UTC+8 day boundary = UTC 16:00. So a UTC hour < 16 stays on the same
    calendar date; >= 16 rolls into the next. Computed from `utc_date`
    ('YYYY-MM-DD') + `hour_utc` (0..23).
    """
    if hour_utc < 16:
        return utc_date
    Y, M, D = int(utc_date[0:4]), int(utc_date[5:7]), int(utc_date[8:10])
    return (datetime(Y, M, D) + _td(days=1)).date().isoformat()

DEFAULT_DB_PATH = "/var/lib/carmonitor/gps.db"
# Soft cap on the number of calendar days to retain. Past this, oldest days
# get pruned on a slow timer. Set high enough for normal use; lower if you
# have a tight disk budget.
MAX_DAYS = 365


@dataclass
class Row:
    ts: str
    lat: float
    lon: float
    alt: Optional[float]
    sog_kmh: Optional[float]
    cog_deg: Optional[float]
    fix: int
    sat_used: Optional[int]
    hdop: Optional[float]
    cn0_max: Optional[int]


class FixStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._queue: "Queue[dict]" = Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ensure_db()
        self._normalise_ts()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="store-writer",
                                       daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def submit(self, fix_dict: dict) -> None:
        # Only queue real fixes; saves space.
        if fix_dict.get("fix") == 1 and fix_dict.get("lat") is not None:
            self._queue.put(fix_dict)

    # ---- reader API ----------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=2.0,
                               detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _connect_raw(self) -> Iterator[sqlite3.Connection]:
        """Same as _connect() but row_factory=None — used by the bulk
        aggregation paths (dashboard / day_stats) that iterate >100k rows.
        Each Row construction costs ~7µs, which is the dominant cost when
        the result set is large; tuple access by index is plenty for those
        code paths."""
        conn = sqlite3.connect(self.db_path, timeout=2.0)
        try:
            yield conn
        finally:
            conn.close()

    def latest(self) -> Optional[Row]:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT ts, lat, lon, alt, sog_kmh, cog_deg, fix, "
                "sat_used, hdop, cn0_max FROM fixes ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return Row(**dict(r)) if r else None

    def history(self, since_s: int = 600, limit: int = 5000) -> list[Row]:
        """Return rows from the last `since_s` seconds, newest first."""
        cutoff = time.time() - since_s
        with self._connect() as conn:
            rs = conn.execute(
                "SELECT ts, lat, lon, alt, sog_kmh, cog_deg, fix, "
                "sat_used, hdop, cn0_max FROM fixes "
                "WHERE ts >= ? ORDER BY id DESC LIMIT ?",
                (datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(),
                 limit),
            ).fetchall()
        return [Row(**dict(r)) for r in rs]

    def list_days(self) -> list[dict]:
        """Return per-day summary, newest first.

        Each entry: {"date": "YYYY-MM-DD", "count": int, "first": iso, "last": iso}.
        """
        with self._connect() as conn:
            rs = conn.execute("""
                SELECT substr(ts, 1, 10) AS day,
                       COUNT(*)           AS n,
                       MIN(ts)            AS first_ts,
                       MAX(ts)            AS last_ts
                FROM fixes
                GROUP BY day
                ORDER BY day DESC
            """).fetchall()

        def norm(ts: str) -> str:
            # Older seed rows used a space between date and time; SQLite's
            # MIN/MAX text comparison happily returns those mixed with the
            # newer T-separated rows. Force a single canonical form so the
            # UI sees consistent timestamps.
            return ts.replace(" ", "T") if ts else ts

        return [
            {"date": r["day"], "count": r["n"],
             "first": norm(r["first_ts"]), "last": norm(r["last_ts"])}
            for r in rs
        ]

    def day_stats(self) -> list[dict]:
        """Per-day stats for the data management page.

        For each UTC calendar day that has fixes, returns:
          {date, count, distance_km, max_speed_kmh, first, last}

        One pass over a single ordered query (no `datetime(ts)` in SQL, so the
        `fixes_ts_idx` index is used). Distance is summed via haversine between
        consecutive points within the same day. All writes enforce fix=1, so
        we don't filter on it here.
        """
        with self._connect_raw() as conn:
            rows = conn.execute(
                "SELECT ts, lat, lon, sog_kmh FROM fixes ORDER BY ts ASC"
            ).fetchall()

        # Hot-loop locals.
        sin = math.sin
        cos = math.cos
        asin = math.asin
        sqrt = math.sqrt
        radians = math.radians
        R = 6371.0088

        per_day: dict[str, dict] = {}
        prev_day: Optional[str] = None
        prev_lat = prev_lon = None
        for ts_str, lat, lon, sog in rows:
            if not ts_str or lat is None or lon is None:
                continue
            ts_canon = ts_str.replace(" ", "T")
            utc_date = ts_str[:10]
            hour_utc = int(ts_str[11:13])
            day = _plus8_date_str(utc_date, hour_utc)
            entry = per_day.get(day)
            if entry is None:
                per_day[day] = {
                    "date": day,
                    "count": 1,
                    "distance_km": 0.0,
                    "max_speed_kmh": float(sog) if sog is not None else 0.0,
                    "first": ts_canon,
                    "last": ts_canon,
                }
            else:
                entry["count"] += 1
                if sog is not None and sog > entry["max_speed_kmh"]:
                    entry["max_speed_kmh"] = float(sog)
                entry["last"] = ts_canon
            if prev_day == day and prev_lat is not None:
                # Inline haversine.
                p1 = radians(prev_lat)
                p2 = radians(lat)
                dphi = radians(lat - prev_lat)
                dlmb = radians(lon - prev_lon)
                hh = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2) ** 2
                per_day[day]["distance_km"] += 2 * R * asin(sqrt(hh))
            prev_day, prev_lat, prev_lon = day, lat, lon

        out = []
        for d in per_day.values():
            d["distance_km"] = round(d["distance_km"], 2)
            d["max_speed_kmh"] = round(d["max_speed_kmh"], 1)
            out.append(d)
        out.sort(key=lambda x: x["date"])
        return out

    def delete_range(self, start: str, end: str) -> int:
        """Delete fixes with start <= ts <= end (inclusive). Returns row count."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM fixes WHERE ts >= ? AND ts <= ?",
                (start, end),
            )
            conn.commit()
            return cur.rowcount

    def delete_days(self, days: list[str]) -> int:
        """Delete all fixes for the given UTC calendar days. Returns row count."""
        if not days:
            return 0
        placeholders = ",".join("?" for _ in days)
        with self._connect() as conn:
            cur = conn.execute(
                f"DELETE FROM fixes WHERE substr(ts, 1, 10) IN ({placeholders})",
                list(days),
            )
            conn.commit()
            return cur.rowcount

    def delete_all(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM fixes")
            conn.commit()
            return cur.rowcount

    def day_history(self, date: str, max_points: int = 8000) -> list[Row]:
        """Return all fixes for the given UTC calendar day.

        For very long drives the row count can exceed what the browser can
        render as a polyline. We downsample to at most `max_points` rows,
        preserving the first and last point of every bucket so the trail
        keeps its shape and the playback timeline is uniformly covered.
        """
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM fixes WHERE substr(ts, 1, 10) = ?",
                (date,),
            ).fetchone()[0]

            if total <= max_points:
                rs = conn.execute(
                    "SELECT ts, lat, lon, alt, sog_kmh, cog_deg, fix, "
                    "sat_used, hdop, cn0_max FROM fixes "
                    "WHERE substr(ts, 1, 10) = ? "
                    "ORDER BY id ASC",
                    (date,),
                ).fetchall()
                return [Row(**dict(r)) for r in rs]

            # Bucket each row into groups of size `step` and keep the first
            # and last row of every bucket. This preserves the trail shape
            # while bounding the output to roughly 2·max_points rows.
            step = max(2, math.ceil(total * 2 / max_points))
            rs = conn.execute(f"""
                WITH numbered AS (
                  SELECT ROW_NUMBER() OVER (ORDER BY id) - 1 AS rn,
                         ts, lat, lon, alt, sog_kmh, cog_deg, fix,
                         sat_used, hdop, cn0_max
                  FROM fixes
                  WHERE substr(ts, 1, 10) = ?
                )
                SELECT ts, lat, lon, alt, sog_kmh, cog_deg, fix,
                       sat_used, hdop, cn0_max
                FROM numbered
                WHERE rn IN (
                  SELECT MIN(rn) FROM numbered GROUP BY (rn / {step})
                  UNION
                  SELECT MAX(rn) FROM numbered GROUP BY (rn / {step})
                )
                ORDER BY rn ASC
            """, (date,)).fetchall()
            return [Row(**dict(r)) for r in rs]

    def range_history(self, start: str, end: str,
                      max_points: int = 8000) -> list[Row]:
        """Return fixes with start <= ts <= end.

        Both bounds are ISO 8601 timestamps (T-separated). Plain TEXT compare
        uses the `fixes_ts_idx` index; assumes the ts column has been
        normalised to T-separated form (see _normalise_ts() at startup).
        """
        where = "ts >= ? AND ts <= ?"
        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM fixes WHERE {where}",
                (start, end),
            ).fetchone()[0]
            if total == 0:
                return []
            if total <= max_points:
                rs = conn.execute(
                    f"SELECT ts, lat, lon, alt, sog_kmh, cog_deg, fix, "
                    f"sat_used, hdop, cn0_max FROM fixes WHERE {where} "
                    f"ORDER BY ts ASC",
                    (start, end),
                ).fetchall()
                return [Row(**dict(r)) for r in rs]
            step = max(2, math.ceil(total * 2 / max_points))
            rs = conn.execute(f"""
                WITH numbered AS (
                  SELECT ROW_NUMBER() OVER (ORDER BY ts) - 1 AS rn,
                         ts, lat, lon, alt, sog_kmh, cog_deg, fix,
                         sat_used, hdop, cn0_max
                  FROM fixes
                  WHERE {where}
                )
                SELECT ts, lat, lon, alt, sog_kmh, cog_deg, fix,
                       sat_used, hdop, cn0_max
                FROM numbered
                WHERE rn IN (
                  SELECT MIN(rn) FROM numbered GROUP BY (rn / {step})
                  UNION
                  SELECT MAX(rn) FROM numbered GROUP BY (rn / {step})
                )
                ORDER BY rn ASC
            """, (start, end)).fetchall()
            return [Row(**dict(r)) for r in rs]

    # ---- dashboard aggregations ---------------------------------------

    # Trip segmentation: a gap longer than this between consecutive fixes
    # marks the end of one trip and the start of the next.
    TRIP_IDLE_S = 300          # 5 min
    TRIP_MOVE_THRESHOLD_KMH = 2.0

    def dashboard(self, days: int = 30) -> dict:
        """Aggregate the last `days` of fixes for the dashboard widgets.

        Single SQL query (no `datetime(ts)` so the index is used), single
        Python pass, raw tuples (no Row / dict wrapper). Signal quality is
        derived from the same pass.

        For very long windows this still materialises all rows in memory;
        with the current 1 Hz cadence and 30 days that's ~2.6M rows upper
        bound. Plenty fast up to ~1M rows on a Pi.
        """
        import collections

        days = max(1, min(int(days), 365))
        now = datetime.now(timezone.utc)
        cutoff = (now - _td(days=days)).isoformat(timespec="seconds")
        sq_cutoff_day = ((now + _td(hours=8)).date()
                       - _td(days=7)).isoformat()

        with self._connect_raw() as conn:
            rows = conn.execute(
                "SELECT ts, lat, lon, sog_kmh, hdop, sat_used "
                "FROM fixes WHERE ts >= ? ORDER BY ts ASC",
                (cutoff,),
            ).fetchall()

        # Pre-bucket per-day epoch-second offset (UTC, used for trip gaps) and
        # weekday index for the UTC+8 dates we display. Building
        # `(date(Y,M,D)-EPOCH).days*86400` for every row is the single
        # biggest cost in the inner loop (~7µs/row vs <1µs/row for a dict
        # lookup), so we compute it once per unique day instead.
        day_keys = {r[0][:10] for r in rows if r[0]}
        day_epoch: dict[str, int] = {
            d: (datetime(int(d[0:4]), int(d[5:7]), int(d[8:10]))
                - _EPOCH).days * 86400
            for d in day_keys
        }
        # UTC+8 (Beijing) calendar date for the <16-UTC and >=16-UTC zones of
        # every UTC date that appears in the window.
        plus8_a: dict[str, str] = {d: d for d in day_keys}
        plus8_b: dict[str, str] = {
            d: _plus8_date_str(d, 16) for d in day_keys
        }
        plus8_dates = set(plus8_a.values()) | set(plus8_b.values())
        day_wd: dict[str, int] = {
            d: _calendar.weekday(int(d[0:4]), int(d[5:7]), int(d[8:10]))
            for d in plus8_dates
        }

        kpis = {
            "total_distance_km": 0.0,
            "max_speed_kmh": 0.0,
            "avg_speed_sum": 0.0,
            "avg_speed_n": 0,
            "trip_count": 0,
            "fix_count": len(rows),
        }
        daily_distance: dict[str, float] = {}
        hourly_heat: list[collections.defaultdict] = [
            collections.defaultdict(float) for _ in range(7)
        ]
        speed_bins = [0] * 12                # 0-10, 10-20, ... 110-120

        # Signal quality: last 7 days, accumulated in-loop (no second query).
        sq_hdop_sum: dict[str, float] = {}
        sq_hdop_n: dict[str, int] = {}
        sq_sat_sum: dict[str, float] = {}
        sq_sat_n: dict[str, int] = {}
        sq_count: dict[str, int] = {}

        # Trip segmentation. We carry over trip state across the whole loop
        # so each "trip" gets its own record when we hit an idle gap.
        trips: list[dict] = []
        cur: Optional[dict] = None
        prev_lat: Optional[float] = None
        prev_lon: Optional[float] = None
        prev_epoch: Optional[int] = None

        # Hot-loop locals (avoid repeated attribute lookups on each iter).
        sin = math.sin
        cos = math.cos
        asin = math.asin
        sqrt = math.sqrt
        radians = math.radians
        R_EARTH = 6371.0088
        TRIP_IDLE_S = self.TRIP_IDLE_S
        TRIP_MOVE_THRESHOLD_KMH = self.TRIP_MOVE_THRESHOLD_KMH
        daily_get = daily_distance.get
        sq_hsum_get = sq_hdop_sum.get
        sq_hn_get = sq_hdop_n.get
        sq_ssum_get = sq_sat_sum.get
        sq_sn_get = sq_sat_n.get
        sq_n_get = sq_count.get

        n_used = 0
        for row in rows:
            ts_str, lat, lon, sog, hdop, sat_used = row
            if not ts_str or lat is None or lon is None:
                continue
            n_used += 1
            utc_date = ts_str[:10]
            hour_utc = int(ts_str[11:13])
            mn = int(ts_str[14:16])
            sec = int(ts_str[17:19])
            # UTC+8 (Beijing) bucketing for display; gap detection stays in
            # UTC epoch so the trip-boundary logic is timezone-agnostic.
            day = plus8_b[utc_date] if hour_utc >= 16 else plus8_a[utc_date]
            hour = (hour_utc + 8) % 24
            epoch = day_epoch[utc_date] + hour_utc * 3600 + mn * 60 + sec

            if prev_lat is not None:
                # Inline haversine (no function-call overhead in the hot loop).
                p1 = radians(prev_lat)
                p2 = radians(lat)
                dphi = radians(lat - prev_lat)
                dlmb = radians(lon - prev_lon)
                hh = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2) ** 2
                d_km = 2 * R_EARTH * asin(sqrt(hh))

                gap_s = (epoch - prev_epoch) if prev_epoch is not None else 0
                daily_distance[day] = daily_get(day, 0.0) + d_km
                wd = day_wd[day]
                hourly_heat[wd][hour] += d_km
                kpis["total_distance_km"] += d_km

                # Trip segmentation: a gap > TRIP_IDLE_S always breaks a trip.
                if gap_s > TRIP_IDLE_S:
                    if cur is not None and cur["n"] >= 2 \
                            and cur["distance_km"] > 0:
                        cur["duration_s"] = (
                            day_epoch[cur["first_day_utc"]]
                            + int(cur["first_ts"][11:13]) * 3600
                            + int(cur["first_ts"][14:16]) * 60
                            + int(cur["first_ts"][17:19])
                        )
                        cur["duration_s"] = (
                            day_epoch[cur["last_day_utc"]]
                            + int(cur["last_ts"][11:13]) * 3600
                            + int(cur["last_ts"][14:16]) * 60
                            + int(cur["last_ts"][17:19])
                        ) - cur["duration_s"]
                        trips.append(cur)
                    cur = None
                if cur is None:
                    cur = {"first_ts": ts_str, "last_ts": ts_str,
                           "first_day": day, "last_day": day,
                           "first_day_utc": utc_date,
                           "last_day_utc": utc_date,
                           "n": 1, "distance_km": 0.0,
                           "max_speed_kmh": 0.0}
                cur["last_ts"] = ts_str
                cur["last_day"] = day
                cur["last_day_utc"] = utc_date
                cur["n"] += 1
                cur["distance_km"] += d_km
                if sog is not None and sog > cur["max_speed_kmh"]:
                    cur["max_speed_kmh"] = sog
                prev_epoch = epoch

            else:
                # First point — open a fresh trip (no distance to add yet).
                cur = {"first_ts": ts_str, "last_ts": ts_str,
                       "first_day": day, "last_day": day,
                       "first_day_utc": utc_date,
                       "last_day_utc": utc_date,
                       "n": 1, "distance_km": 0.0,
                       "max_speed_kmh": float(sog) if sog is not None else 0.0}
                prev_epoch = epoch

            # Speed stats. sog is reported even at rest (often 0); we ignore
            # sub-TRIP_MOVE_THRESHOLD_KMH values for the average.
            if sog is not None:
                if sog > kpis["max_speed_kmh"]:
                    kpis["max_speed_kmh"] = sog
                if sog >= TRIP_MOVE_THRESHOLD_KMH:
                    kpis["avg_speed_sum"] += sog
                    kpis["avg_speed_n"] += 1
                # Histogram uses the raw value (0 included — many 0s are
                # informative about "how often stationary").
                bin_idx = min(11, int(sog // 10))
                speed_bins[bin_idx] += 1

            # Signal-quality accumulation (last 7 days, UTC).
            if day >= sq_cutoff_day:
                if hdop is not None:
                    sq_hdop_sum[day] = sq_hsum_get(day, 0.0) + hdop
                    sq_hdop_n[day] = sq_hn_get(day, 0) + 1
                if sat_used is not None:
                    sq_sat_sum[day] = sq_ssum_get(day, 0.0) + sat_used
                    sq_sat_n[day] = sq_sn_get(day, 0) + 1
                sq_count[day] = sq_n_get(day, 0) + 1

            prev_lat = lat
            prev_lon = lon

        # Close the last in-flight trip.
        if cur is not None and cur["n"] >= 2 and cur["distance_km"] > 0:
            cur["duration_s"] = (
                day_epoch[cur["last_day_utc"]]
                + int(cur["last_ts"][11:13]) * 3600
                + int(cur["last_ts"][14:16]) * 60
                + int(cur["last_ts"][17:19])
            ) - (
                day_epoch[cur["first_day_utc"]]
                + int(cur["first_ts"][11:13]) * 3600
                + int(cur["first_ts"][14:16]) * 60
                + int(cur["first_ts"][17:19])
            )
            trips.append(cur)

        kpis["trip_count"] = len(trips)
        kpis["avg_speed_kmh"] = (kpis["avg_speed_sum"] / kpis["avg_speed_n"]
                                 if kpis["avg_speed_n"] else 0.0)

        # Sort trips by distance desc and keep top 5.
        top_trips = sorted(trips, key=lambda t: t["distance_km"],
                           reverse=True)[:5]
        top_trips_out = [
            {"date": t["first_day"],
             "distance_km": round(t["distance_km"], 2),
             "duration_s": t["duration_s"],
             "max_speed_kmh": round(t["max_speed_kmh"], 1)}
            for t in top_trips
        ]

        # 7×24 matrix in fixed weekday order so the frontend can index it
        # directly. Rows: Mon..Sun; cols: 0..23 (UTC hour).
        heatmap_rows = []
        for wd in range(7):
            row = [round(hourly_heat[wd].get(h, 0.0), 2) for h in range(24)]
            heatmap_rows.append(row)

        daily_out = [{"date": d, "km": round(v, 2)}
                     for d, v in sorted(daily_distance.items())]

        histogram_out = [
            {"lo": i * 10, "hi": (i + 1) * 10, "count": speed_bins[i]}
            for i in range(12)
        ]

        signal_quality = [
            {"date": day,
             "hdop": round(sq_hdop_sum[day] / sq_hdop_n[day], 2)
                     if sq_hdop_n.get(day, 0) else None,
             "sat_used": round(sq_sat_sum[day] / sq_sat_n[day], 1)
                         if sq_sat_n.get(day, 0) else None,
             "fix_count": sq_count.get(day, 0)}
            for day in sorted(sq_count)
        ]

        return {
            "kpis": {
                "total_distance_km": round(kpis["total_distance_km"], 2),
                "max_speed_kmh": round(kpis["max_speed_kmh"], 1),
                "avg_speed_kmh": round(kpis["avg_speed_kmh"], 1),
                "trip_count": kpis["trip_count"],
                "fix_count": n_used,
            },
            "daily_distance": daily_out,
            "hourly_heatmap": heatmap_rows,
            "speed_histogram": histogram_out,
            "top_trips": top_trips_out,
            "signal_quality": signal_quality,
        }

    # ---- internals -----------------------------------------------------

    def _normalise_ts(self) -> None:
        """Rewrite legacy space-separated ts values to the T-separated form.

        Old seed rows (and a handful of historical live rows) stored ts as
        "YYYY-MM-DD HH:MM:SS+00:00". Plain TEXT comparison against a
        T-separated bound ("YYYY-MM-DDTHH:MM:SS…") still sorts cross-day
        correctly, but mixing the two on the same day breaks ordering. After
        normalisation the `fixes_ts_idx` index can be used for range queries.

        Idempotent: a no-op once every row is already T-separated.
        """
        with self._connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM fixes "
                "WHERE ts LIKE '% %' AND ts NOT LIKE '%T%'"
            ).fetchone()[0]
            if n == 0:
                return
            log.info("normalising %d legacy space-separated ts values", n)
            conn.execute(
                "UPDATE fixes SET ts = REPLACE(ts, ' ', 'T') "
                "WHERE ts LIKE '% %' AND ts NOT LIKE '%T%'"
            )
            conn.commit()

    def _ensure_db(self) -> None:
        import os
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fixes (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts      TEXT NOT NULL,
                    lat     REAL NOT NULL,
                    lon     REAL NOT NULL,
                    alt     REAL,
                    sog_kmh REAL,
                    cog_deg REAL,
                    fix     INTEGER NOT NULL,
                    sat_used INTEGER,
                    hdop    REAL,
                    cn0_max INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS fixes_ts_idx ON fixes(ts)")
            conn.commit()

    def _run(self) -> None:
        # Periodic GC.
        last_gc = time.monotonic()
        while not self._stop.is_set():
            try:
                fix = self._queue.get(timeout=0.5)
            except Empty:
                if time.monotonic() - last_gc > 3600:
                    self._gc()
                    last_gc = time.monotonic()
                continue
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO fixes "
                        "(ts, lat, lon, alt, sog_kmh, cog_deg, fix, "
                        "sat_used, hdop, cn0_max) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            fix["ts"],
                            fix["lat"],
                            fix["lon"],
                            fix.get("alt"),
                            fix.get("sog_kmh"),
                            fix.get("cog_deg"),
                            fix["fix"],
                            fix.get("sat_used"),
                            fix.get("hdop"),
                            fix.get("cn0_max"),
                        ),
                    )
                    conn.commit()
            except Exception:
                log.exception("store insert failed")
            if time.monotonic() - last_gc > 3600:
                self._gc()
                last_gc = time.monotonic()

    def _gc(self) -> None:
        """Drop the oldest days so the DB stays bounded."""
        with self._connect() as conn:
            days = conn.execute(
                "SELECT DISTINCT substr(ts, 1, 10) AS d FROM fixes "
                "ORDER BY d DESC LIMIT -1 OFFSET ?",
                (MAX_DAYS,),
            ).fetchall()
            if not days:
                return
            for r in days:
                cur = conn.execute(
                    "DELETE FROM fixes WHERE substr(ts, 1, 10) = ?",
                    (r["d"],),
                )
                if cur.rowcount:
                    log.info("pruned day %s (%d rows)", r["d"], cur.rowcount)
            conn.commit()