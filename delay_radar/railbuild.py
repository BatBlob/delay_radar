"""Build rail geometry from the Geofabrik Germany extract.

    .venv\\Scripts\\python -m delay_radar.railbuild

The full pbf is filtered to railway=rail, light_rail and narrow_gauge, then
deleted. Station-to-station routes are cached in data/cache/track-legs.sqlite.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from delay_radar.railgraph import (
    astar,
    closest_on_polyline,
    contract,
    meters,
    path_coords,
    polyline_meters,
    routing_graph,
    simplify,
)

ROOT = Path("data")
OSM_DIR = ROOT / "osm"
PBF = OSM_DIR / "germany-latest.osm.pbf"
RAIL_DB = OSM_DIR / "rail.sqlite"
KEEP = {"rail", "light_rail", "narrow_gauge"}
MINOR = {"yard", "spur", "siding", "crossover"}
SNAP_M = 3000.0
WIDE_SNAP_M = 6000.0


def main() -> None:
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT / "cache").mkdir(parents=True, exist_ok=True)
    if not _rail_ready():
        if not PBF.exists() or PBF.stat().st_size < 1_000_000_000:
            raise SystemExit(f"missing {PBF}. Download the Geofabrik Germany extract there first.")
        print(f"filtering {PBF.stat().st_size / 1e9:.1f} GB", flush=True)
        _filter(PBF, RAIL_DB)
        print(f"deleting {PBF.name}", flush=True)
        PBF.unlink()
    else:
        print(f"reusing {RAIL_DB}", flush=True)
    _route()


def _rail_ready() -> bool:
    if not RAIL_DB.exists():
        return False
    connection = sqlite3.connect(RAIL_DB)
    try:
        ways = connection.execute("SELECT COUNT(*) FROM ways").fetchone()[0]
        located = connection.execute("SELECT COUNT(*) FROM nodes WHERE lat IS NOT NULL").fetchone()[0]
    except sqlite3.Error:
        return False
    finally:
        connection.close()
    return ways > 1000 and located > 1000


def _skip(tags) -> bool:
    railway = tags.get("railway")
    if railway not in KEEP:
        return True
    if tags.get("abandoned") == "yes" or tags.get("disused") == "yes" or tags.get("razed") == "yes":
        return True
    return False


def _filter(pbf: Path, dest: Path) -> None:
    import osmium

    if dest.exists():
        dest.unlink()
    connection = sqlite3.connect(dest)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("CREATE TABLE ways (id INTEGER PRIMARY KEY, railway TEXT, service TEXT, noderefs TEXT)")
    connection.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, lat REAL, lon REAL)")
    needed: set[int] = set()
    pending = []
    seen = 0
    kept = 0

    class Ways(osmium.SimpleHandler):
        def way(self, way):
            nonlocal seen, kept
            seen += 1
            if seen % 2_000_000 == 0:
                print(f"  scanned {seen} ways, kept {kept}", flush=True)
            if _skip(way.tags):
                return
            refs = [node.ref for node in way.nodes]
            if len(refs) < 2:
                return
            kept += 1
            needed.update(refs)
            pending.append((way.id, way.tags.get("railway"), way.tags.get("service") or "", json.dumps(refs)))
            if len(pending) >= 5000:
                connection.executemany("INSERT OR REPLACE INTO ways VALUES (?,?,?,?)", pending)
                pending.clear()

    Ways().apply_file(str(pbf), locations=False)
    if pending:
        connection.executemany("INSERT OR REPLACE INTO ways VALUES (?,?,?,?)", pending)
    connection.commit()
    print(f"  kept {kept} ways, {len(needed)} nodes. locating...", flush=True)

    found = 0
    located = []

    class Nodes(osmium.SimpleHandler):
        def node(self, node):
            nonlocal found
            if node.id not in needed:
                return
            located.append((node.id, node.location.lat, node.location.lon))
            found += 1
            if len(located) >= 20000:
                connection.executemany("INSERT OR REPLACE INTO nodes VALUES (?,?,?)", located)
                located.clear()
            if found % 500_000 == 0:
                connection.commit()
                print(f"  located {found}", flush=True)

    Nodes().apply_file(str(pbf), locations=False)
    if located:
        connection.executemany("INSERT OR REPLACE INTO nodes VALUES (?,?,?)", located)
    connection.commit()
    connection.close()
    print(f"  located {found} nodes", flush=True)


def _schedule() -> Path:
    files = sorted((ROOT / "cache").glob("schedule-*.sqlite"), key=lambda path: path.stat().st_mtime)
    if not files:
        raise SystemExit("no schedule database in data/cache")
    return files[-1]


def _route() -> None:
    started = time.perf_counter()
    print("loading rail graph", flush=True)
    raw, display = _load_raw()
    print(f"  nodes {len(raw)}", flush=True)
    edges = contract(raw)
    print(f"  contracted edges {len(edges)} in {time.perf_counter() - started:.0f}s", flush=True)
    _write_display(display)
    pairs, stops = _pairs()
    used = {stop for pair in pairs for stop in pair}
    stops = {stop_id: point for stop_id, point in stops.items() if stop_id in used}
    print(f"  stop pairs {len(pairs)} stops {len(stops)}", flush=True)
    snaps, far = _snap(edges, stops, SNAP_M)
    if len(far) > max(50, int(0.08 * max(1, len(stops)))):
        print(f"  {len(far)} stops were over {SNAP_M:.0f} m from a track. Widening those to {WIDE_SNAP_M:.0f} m.", flush=True)
        extra, still = _snap(edges, {key: stops[key] for key in far}, WIDE_SNAP_M)
        snaps.update(extra)
        far = still
    print(f"  snapped {len(snaps)} still far {len(far)}", flush=True)
    adj, coords = routing_graph(edges, snaps)
    cache = ROOT / "cache" / "track-legs.sqlite"
    connection = sqlite3.connect(cache)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS legs (
            stop_a TEXT NOT NULL,
            stop_b TEXT NOT NULL,
            coords TEXT,
            reason TEXT,
            PRIMARY KEY (stop_a, stop_b)
        )
        """
    )
    done = {tuple(row) for row in connection.execute("SELECT stop_a, stop_b FROM legs")}
    reasons: dict[str, int] = defaultdict(int)
    routed = 0
    batch = []
    considered = 0
    for index, (stop_a, stop_b) in enumerate(pairs):
        if (stop_a, stop_b) in done:
            continue
        considered += 1
        coords_json, reason = _one(adj, coords, stops, stop_a, stop_b)
        if reason:
            reasons[reason] += 1
        else:
            routed += 1
        batch.append((stop_a, stop_b, coords_json, reason))
        if len(batch) >= 300:
            connection.executemany("INSERT OR REPLACE INTO legs VALUES (?,?,?,?)", batch)
            connection.commit()
            batch.clear()
            print(
                f"  pairs {index + 1}/{len(pairs)} routed {routed} fallback {considered - routed}",
                flush=True,
            )
    if batch:
        connection.executemany("INSERT OR REPLACE INTO legs VALUES (?,?,?,?)", batch)
        connection.commit()
    totals = connection.execute(
        "SELECT COUNT(*), SUM(CASE WHEN coords IS NULL THEN 1 ELSE 0 END) FROM legs"
    ).fetchone()
    connection.close()
    size = cache.stat().st_size
    print(
        f"cache {cache} {size / 1e6:.1f} MB  legs {totals[0]}  fallback {totals[1]}  "
        f"new routed {routed} in {time.perf_counter() - started:.0f}s",
        flush=True,
    )
    for reason, count in sorted(reasons.items(), key=lambda item: -item[1]):
        print(f"  {reason}: {count}", flush=True)


def _load_raw():
    connection = sqlite3.connect(RAIL_DB)
    nodes = {
        row[0]: (row[1], row[2])
        for row in connection.execute("SELECT id, lat, lon FROM nodes WHERE lat IS NOT NULL")
    }
    raw = defaultdict(list)
    display = []
    ways = 0
    for railway, service, noderefs in connection.execute("SELECT railway, service, noderefs FROM ways"):
        ways += 1
        if ways % 100_000 == 0:
            print(f"  ways read {ways}", flush=True)
        refs = [int(item) for item in json.loads(noderefs)]
        points = [nodes[ref] for ref in refs if ref in nodes]
        ids = [ref for ref in refs if ref in nodes]
        if len(points) < 2:
            continue
        minor = 1 if service in MINOR else 0
        display.append((points, minor))
        for index in range(1, len(ids)):
            a, b = ids[index - 1], ids[index]
            if a == b:
                continue
            segment = [nodes[a], nodes[b]]
            raw[a].append((b, segment))
            raw[b].append((a, list(reversed(segment))))
    connection.close()
    return raw, display


def _merge_display(display: list, minor: int) -> list:
    """Join ways that meet at their endpoints so the map file is one line per corridor."""
    raw = defaultdict(list)
    for points, flag in display:
        if flag != minor or len(points) < 2:
            continue
        start = (round(points[0][0], 5), round(points[0][1], 5))
        end = (round(points[-1][0], 5), round(points[-1][1], 5))
        if start == end:
            continue
        raw[start].append((end, points))
        raw[end].append((start, list(reversed(points))))
    return [points for _start, _end, points in contract(raw)]


def _write_collection(dest: Path, lines: list, tolerance_m: float, minor: int) -> None:
    features = []
    for points in lines:
        simple = simplify(points, tolerance_m)
        if len(simple) < 2:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {"minor": minor},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[round(lon, 4), round(lat, 4)] for lat, lon in simple],
                },
            }
        )
    text = json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":"))
    dest.write_text(text, encoding="utf-8")
    print(f"  {dest.name} {len(text) / 1e6:.1f} MB features {len(features)} at {tolerance_m:.0f} m", flush=True)


def _write_display(display: list) -> None:
    cache = ROOT / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    main = _merge_display(display, 0)
    minor = _merge_display(display, 1)
    print(f"  display lines {len(display)} main {len(main)} sidings {len(minor)}", flush=True)
    _write_collection(cache / "tracks.geojson", main, 45, 0)
    _write_collection(cache / "tracks-minor.geojson", minor, 70, 1)


def _pairs() -> tuple[list[tuple[str, str]], dict[str, tuple[float, float]]]:
    connection = sqlite3.connect(_schedule())
    stops = {
        row[0]: (row[1], row[2])
        for row in connection.execute("SELECT stop_id, lat, lon FROM stops WHERE lat IS NOT NULL AND lon IS NOT NULL")
    }
    found = set()
    hops = 0
    for stop_a, stop_b in connection.execute(
        """
        SELECT a.stop_id, b.stop_id
        FROM stop_times a
        JOIN stop_times b ON a.trip_id = b.trip_id AND b.seq = a.seq + 1
        """
    ):
        hops += 1
        if hops % 1_000_000 == 0:
            print(f"  hops {hops} unique {len(found)}", flush=True)
        if stop_a != stop_b:
            found.add((stop_a, stop_b))
    connection.close()
    print(f"  hops {hops}", flush=True)
    return list(found), stops


def _components(edges) -> tuple[list[int], dict[int, int]]:
    """Union-find over edge endpoints. Returns (root per edge, edge-count per root)."""
    parent: dict = {}

    def find(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left, right):
        ra, rb = find(left), find(right)
        if ra != rb:
            parent[rb] = ra

    for start, end, _points in edges:
        union(start, end)
    roots = []
    counts: dict[int, int] = defaultdict(int)
    for start, _end, _points in edges:
        root = find(start)
        roots.append(root)
        counts[root] += 1
    return roots, counts


def _snap(edges, stops, limit: float) -> tuple[dict, list]:
    # A station can sit 1 m from a yard track that does not join the main line,
    # with the through track a couple of metres further. Prefer the large component.
    large_edges = 250
    slack_m = 40.0
    cell = 0.05
    grid = defaultdict(set)
    boxes = []
    roots, counts = _components(edges)
    for index, (_a, _b, points) in enumerate(edges):
        lats = [point[0] for point in points]
        lons = [point[1] for point in points]
        boxes.append((min(lats), max(lats), min(lons), max(lons)))
        for point_index in range(len(points) - 1):
            a, b = points[point_index], points[point_index + 1]
            lat0, lat1 = (a[0], b[0]) if a[0] <= b[0] else (b[0], a[0])
            lon0, lon1 = (a[1], b[1]) if a[1] <= b[1] else (b[1], a[1])
            for lat_i in range(int(lat0 / cell), int(lat1 / cell) + 1):
                for lon_i in range(int(lon0 / cell), int(lon1 / cell) + 1):
                    grid[(lat_i, lon_i)].add(index)
    print(f"  snap index {len(edges)} edges, {len(stops)} stops, limit {limit:.0f} m", flush=True)
    snaps = {}
    far = []
    pad = limit / 110_000
    for count, (stop_id, (lat, lon)) in enumerate(stops.items(), start=1):
        lat_i, lon_i = int(lat / cell), int(lon / cell)
        best = None
        best_large = None
        seen = set()
        for dlat in (-2, -1, 0, 1, 2):
            for dlon in (-2, -1, 0, 1, 2):
                for index in grid.get((lat_i + dlat, lon_i + dlon), ()):
                    if index in seen:
                        continue
                    seen.add(index)
                    south, north, west, east = boxes[index]
                    if lat < south - pad or lat > north + pad or lon < west - pad or lon > east + pad:
                        continue
                    _a, _b, points = edges[index]
                    plat, plon, dist, along = closest_on_polyline(lat, lon, points)
                    size = counts[roots[index]]
                    if best is None or dist < best[0]:
                        best = (dist, index, along, plat, plon, size)
                    if size >= large_edges and (best_large is None or dist < best_large[0]):
                        best_large = (dist, index, along, plat, plon, size)
        chosen = best
        if (
            best is not None
            and best_large is not None
            and best[5] < large_edges
            and best_large[0] <= best[0] + slack_m
        ):
            chosen = best_large
        if chosen is None or chosen[0] > limit:
            far.append(stop_id)
        else:
            _dist, index, along, plat, plon, _size = chosen
            snaps[stop_id] = (index, along, plat, plon)
        if count % 2000 == 0:
            print(f"  snapped {count}/{len(stops)}", flush=True)
    return snaps, far


def _one(adj, coords, stops, stop_a, stop_b):
    if stop_a not in stops or stop_b not in stops:
        return None, "missing_coord"
    start = ("s", stop_a)
    goal = ("s", stop_b)
    if start not in coords or goal not in coords:
        return None, "far_from_track"
    nodes = astar(adj, coords, start, goal)
    if not nodes:
        return None, "no_path"
    line = path_coords(adj, nodes)
    if len(line) < 2:
        return None, "no_path"
    straight = meters(stops[stop_a][0], stops[stop_a][1], stops[stop_b][0], stops[stop_b][1])
    length = polyline_meters(line)
    if straight > 500 and length > max(straight * 3.5, straight + 40000):
        return None, "detour"
    slim = simplify([(lat, lon) for lat, lon in line], 25)
    encoded = [[round(lon, 5), round(lat, 5)] for lat, lon in slim]
    return json.dumps(encoded, separators=(",", ":")), None


if __name__ == "__main__":
    main()
