"""SQLite copy of the static feed.

The full zip's stop_times.txt is about 2 GB. Only rail trips (GTFS
route_type 2, which in this feed includes ICE, regional, and S-Bahn)
get stop times. Every trip id is stored so the match rate can count
buses as matched-but-not-drawn.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import zipfile
from datetime import date
from pathlib import Path

from delay_radar.clock import parse_gtfs_time, parse_service_day
from delay_radar.model import Call, TripInfo

FILTER_VERSION = "route-type-2"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def is_rail(route_type: int) -> bool:
    # In germany/free, S-Bahn short names such as S1 are route_type 2.
    return route_type == 2


def database_for(zip_path: Path, cache_dir: Path) -> Path:
    stat = zip_path.stat()
    name = f"schedule-{stat.st_size}-{int(stat.st_mtime)}-{FILTER_VERSION}.sqlite"
    return cache_dir / name


def ensure_database(zip_path: Path, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = database_for(zip_path, cache_dir)
    if dest.exists():
        return dest
    build(zip_path, dest)
    return dest


def build(zip_path: Path, dest: Path) -> None:
    partial = dest.with_suffix(".building")
    if partial.exists():
        partial.unlink()
    conn = sqlite3.connect(partial)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.executescript(
            """
            CREATE TABLE trips (
                trip_id TEXT PRIMARY KEY,
                route_name TEXT NOT NULL,
                route_type INTEGER NOT NULL,
                service_id TEXT NOT NULL,
                rail INTEGER NOT NULL
            );
            CREATE TABLE stops (
                stop_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                lat REAL,
                lon REAL
            );
            CREATE TABLE stop_times (
                trip_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                stop_id TEXT NOT NULL,
                arrival TEXT NOT NULL,
                departure TEXT NOT NULL,
                PRIMARY KEY (trip_id, seq)
            );
            CREATE TABLE calendar (
                service_id TEXT PRIMARY KEY,
                monday INTEGER NOT NULL,
                tuesday INTEGER NOT NULL,
                wednesday INTEGER NOT NULL,
                thursday INTEGER NOT NULL,
                friday INTEGER NOT NULL,
                saturday INTEGER NOT NULL,
                sunday INTEGER NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL
            );
            CREATE TABLE calendar_dates (
                service_id TEXT NOT NULL,
                date TEXT NOT NULL,
                exception_type INTEGER NOT NULL,
                PRIMARY KEY (service_id, date)
            );
            """
        )
        with zipfile.ZipFile(zip_path) as zf:
            _load_routes_trips_calendar(conn, zf)
            _load_stops(conn, zf)
            _load_stop_times(conn, zf)
        conn.execute("CREATE INDEX stop_times_trip ON stop_times(trip_id)")
        conn.commit()
    finally:
        conn.close()
    partial.replace(dest)


def _text(zf: zipfile.ZipFile, name: str):
    return io.TextIOWrapper(zf.open(name), encoding="utf-8-sig", newline="")


def _load_routes_trips_calendar(conn: sqlite3.Connection, zf: zipfile.ZipFile) -> None:
    names: dict[str, str] = {}
    types: dict[str, int] = {}
    for row in csv.DictReader(_text(zf, "routes.txt")):
        names[row["route_id"]] = row.get("route_short_name") or row.get("route_long_name") or row["route_id"]
        types[row["route_id"]] = int(row["route_type"])
    trip_rows = []
    rail_count = 0
    for row in csv.DictReader(_text(zf, "trips.txt")):
        route_type = types.get(row["route_id"], -1)
        rail = is_rail(route_type)
        rail_count += int(rail)
        trip_rows.append(
            (
                row["trip_id"],
                names.get(row["route_id"], row["route_id"]),
                route_type,
                row["service_id"],
                int(rail),
            )
        )
        if len(trip_rows) >= 50000:
            conn.executemany("INSERT INTO trips VALUES (?, ?, ?, ?, ?)", trip_rows)
            trip_rows.clear()
    if trip_rows:
        conn.executemany("INSERT INTO trips VALUES (?, ?, ?, ?, ?)", trip_rows)
    print(f"static trips={conn.execute('SELECT COUNT(*) FROM trips').fetchone()[0]} rail_trips={rail_count}", flush=True)

    cal_rows = []
    for row in csv.DictReader(_text(zf, "calendar.txt")):
        cal_rows.append(tuple(row[column] for column in ("service_id", *WEEKDAYS, "start_date", "end_date")))
    conn.executemany(
        "INSERT INTO calendar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(row[0], *(int(value) for value in row[1:8]), row[8], row[9]) for row in cal_rows],
    )
    date_rows = [
        (row["service_id"], row["date"], int(row["exception_type"]))
        for row in csv.DictReader(_text(zf, "calendar_dates.txt"))
    ]
    conn.executemany("INSERT INTO calendar_dates VALUES (?, ?, ?)", date_rows)


def _load_stops(conn: sqlite3.Connection, zf: zipfile.ZipFile) -> None:
    batch = []
    for row in csv.DictReader(_text(zf, "stops.txt")):
        lat = float(row["stop_lat"]) if row.get("stop_lat") else None
        lon = float(row["stop_lon"]) if row.get("stop_lon") else None
        batch.append((row["stop_id"], row.get("stop_name") or row["stop_id"], lat, lon))
        if len(batch) >= 50000:
            conn.executemany("INSERT OR REPLACE INTO stops VALUES (?, ?, ?, ?)", batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT OR REPLACE INTO stops VALUES (?, ?, ?, ?)", batch)


def _load_stop_times(conn: sqlite3.Connection, zf: zipfile.ZipFile) -> None:
    rail_ids = {row[0] for row in conn.execute("SELECT trip_id FROM trips WHERE rail = 1")}
    print(f"reading stop_times for {len(rail_ids)} rail trips", flush=True)
    kept = 0
    seen = 0
    batch = []
    with zf.open("stop_times.txt") as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        header = text.readline()
        columns = next(csv.reader([header]))
        trip_at = columns.index("trip_id")
        if trip_at != 0:
            raise RuntimeError("stop_times.txt trip_id is not the first column; the fast scan assumes it is")
        seq_at = columns.index("stop_sequence")
        stop_at = columns.index("stop_id")
        arr_at = columns.index("arrival_time")
        dep_at = columns.index("departure_time")
        for line in text:
            seen += 1
            if seen % 5_000_000 == 0:
                print(f"stop_times scanned={seen} kept={kept}", flush=True)
            comma = line.find(",")
            if comma < 1 or line[:comma] not in rail_ids:
                continue
            row = next(csv.reader([line]))
            batch.append((row[trip_at], int(row[seq_at]), row[stop_at], row[arr_at], row[dep_at]))
            kept += 1
            if len(batch) >= 20000:
                conn.executemany("INSERT OR REPLACE INTO stop_times VALUES (?, ?, ?, ?, ?)", batch)
                batch.clear()
    if batch:
        conn.executemany("INSERT OR REPLACE INTO stop_times VALUES (?, ?, ?, ?, ?)", batch)
    print(f"stop_times scanned={seen} kept={kept}", flush=True)


class Schedule:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.calendar = {
            row["service_id"]: row
            for row in self.conn.execute("SELECT * FROM calendar")
        }
        self.exceptions = {
            (row["service_id"], row["date"]): int(row["exception_type"])
            for row in self.conn.execute("SELECT * FROM calendar_dates")
        }

    def close(self) -> None:
        self.conn.close()

    def lookup(self, trip_ids: list[str]) -> dict[str, TripInfo]:
        found: dict[str, TripInfo] = {}
        for start in range(0, len(trip_ids), 4000):
            chunk = trip_ids[start : start + 4000]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT trip_id, route_name, route_type, service_id, rail FROM trips WHERE trip_id IN ({marks})",
                chunk,
            )
            for row in rows:
                found[row["trip_id"]] = TripInfo(
                    trip_id=row["trip_id"],
                    route_name=row["route_name"],
                    route_type=int(row["route_type"]),
                    service_id=row["service_id"],
                    rail=bool(row["rail"]),
                )
        return found

    def calls_for(self, instances: list[tuple[str, date]]) -> dict[tuple[str, str], list[Call]]:
        """Key is (trip_id, YYYYMMDD). One trip id may be running on two service days."""
        by_trip: dict[str, list[date]] = {}
        for trip_id, day in instances:
            by_trip.setdefault(trip_id, []).append(day)
        raw: dict[str, list[tuple]] = {trip_id: [] for trip_id in by_trip}
        trip_ids = list(by_trip)
        for start in range(0, len(trip_ids), 2000):
            chunk = trip_ids[start : start + 2000]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"""
                SELECT st.trip_id, st.seq, st.stop_id, st.arrival, st.departure,
                       s.name, s.lat, s.lon
                FROM stop_times st
                LEFT JOIN stops s ON s.stop_id = st.stop_id
                WHERE st.trip_id IN ({marks})
                ORDER BY st.trip_id, st.seq
                """,
                chunk,
            )
            for row in rows:
                raw[row["trip_id"]].append(row)
        grouped: dict[tuple[str, str], list[Call]] = {}
        for trip_id, days in by_trip.items():
            for day in days:
                key = (trip_id, day.strftime("%Y%m%d"))
                grouped[key] = [
                    Call(
                        seq=int(row["seq"]),
                        stop_id=row["stop_id"],
                        name=row["name"] or row["stop_id"],
                        lat=row["lat"],
                        lon=row["lon"],
                        arrival=parse_gtfs_time(day, row["arrival"]),
                        departure=parse_gtfs_time(day, row["departure"]),
                    )
                    for row in raw.get(trip_id, [])
                ]
        return grouped

    def service_runs(self, service_id: str, day: date) -> bool | None:
        exception = self.exceptions.get((service_id, day.strftime("%Y%m%d")))
        if exception == 1:
            return True
        if exception == 2:
            return False
        row = self.calendar.get(service_id)
        if row is None:
            return None
        start = parse_service_day(row["start_date"])
        end = parse_service_day(row["end_date"])
        if start is None or end is None or day < start or day > end:
            return False
        return int(row[WEEKDAYS[day.weekday()]]) == 1

