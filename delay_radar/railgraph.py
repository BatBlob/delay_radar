"""Rail graph helpers: snap a station to a track and walk a path.

The heavy OpenStreetMap load lives in railbuild. These functions are pure
so the estimator and the tests can use them without a map extract.
"""

from __future__ import annotations

import heapq
import math

EARTH_M = 6371000.0


def meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_M * math.asin(min(1.0, math.sqrt(h)))


def polyline_meters(points: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(1, len(points)):
        total += meters(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])
    return total


def point_along(coords: list[tuple[float, float]], fraction: float) -> tuple[float, float]:
    """coords are (lon, lat). Returns (lat, lon) at a fraction of the path length."""
    if not coords:
        raise ValueError("empty path")
    if fraction <= 0 or len(coords) == 1:
        return coords[0][1], coords[0][0]
    if fraction >= 1:
        return coords[-1][1], coords[-1][0]
    lengths = []
    total = 0.0
    for i in range(1, len(coords)):
        step = meters(coords[i - 1][1], coords[i - 1][0], coords[i][1], coords[i][0])
        lengths.append(step)
        total += step
    if total == 0:
        return coords[0][1], coords[0][0]
    target = total * fraction
    walked = 0.0
    for index, step in enumerate(lengths):
        if walked + step >= target and step > 0:
            t = (target - walked) / step
            lon = coords[index][0] + (coords[index + 1][0] - coords[index][0]) * t
            lat = coords[index][1] + (coords[index + 1][1] - coords[index][1]) * t
            return lat, lon
        walked += step
    return coords[-1][1], coords[-1][0]


def _project(lat: float, lon: float, a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float, float]:
    """Return (lat, lon, t) of the closest point on segment a-b. Points are (lat, lon)."""
    lat_scale = 110540.0
    lon_scale = 111320.0 * math.cos(math.radians(lat))
    ax, ay = a[1] * lon_scale, a[0] * lat_scale
    bx, by = b[1] * lon_scale, b[0] * lat_scale
    px, py = lon * lon_scale, lat * lat_scale
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return a[0], a[1], 0.0
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return (ay + t * dy) / lat_scale, (ax + t * dx) / lon_scale, t


def closest_on_polyline(
    lat: float, lon: float, points: list[tuple[float, float]]
) -> tuple[float, float, float, float]:
    """points are (lat, lon). Returns (lat, lon, distance_m, along_m)."""
    best = (points[0][0], points[0][1], meters(lat, lon, points[0][0], points[0][1]), 0.0)
    walked = 0.0
    for index in range(1, len(points)):
        a, b = points[index - 1], points[index]
        plat, plon, t = _project(lat, lon, a, b)
        dist = meters(lat, lon, plat, plon)
        if dist < best[2]:
            along = walked + meters(a[0], a[1], plat, plon)
            best = (plat, plon, dist, along)
        walked += meters(a[0], a[1], b[0], b[1])
    return best


def simplify(points: list[tuple[float, float]], tolerance_m: float) -> list[tuple[float, float]]:
    """Douglas-Peucker. points are (lat, lon)."""
    if len(points) < 3:
        return list(points)

    def keep(start: int, end: int, out: list[tuple[float, float]]) -> None:
        if end <= start + 1:
            return
        ax, ay = points[start]
        bx, by = points[end]
        farthest = start
        farthest_d = 0.0
        for index in range(start + 1, end):
            plat, plon, _t = _project(points[index][0], points[index][1], (ax, ay), (bx, by))
            dist = meters(points[index][0], points[index][1], plat, plon)
            if dist > farthest_d:
                farthest = index
                farthest_d = dist
        if farthest_d > tolerance_m:
            keep(start, farthest, out)
            out.append(points[farthest])
            keep(farthest, end, out)

    kept = [points[0]]
    keep(0, len(points) - 1, kept)
    kept.append(points[-1])
    return kept


def astar(
    adj: dict,
    coords: dict,
    start,
    goal,
    expansion_limit: int = 80000,
) -> list | None:
    """Shortest path on an undirected graph. adj[node] = [(neighbor, meters, polyline)]."""
    if start == goal:
        return [start]
    if start not in coords or goal not in coords:
        return None
    goal_lat, goal_lon = coords[goal]
    sequence = 0
    open_heap = [(0.0, 0.0, sequence, start)]
    came: dict = {}
    best = {start: 0.0}
    seen = 0
    while open_heap:
        _rank, cost, _sequence, node = heapq.heappop(open_heap)
        if cost > best.get(node, cost) + 0.01:
            continue
        if node == goal:
            path = [goal]
            while path[-1] != start:
                path.append(came[path[-1]])
            path.reverse()
            return path
        seen += 1
        if seen > expansion_limit:
            return None
        for neighbor, step, _line in adj.get(node, ()):
            nxt = cost + step
            if nxt < best.get(neighbor, float("inf")):
                best[neighbor] = nxt
                came[neighbor] = node
                nlat, nlon = coords[neighbor]
                rank = nxt + meters(nlat, nlon, goal_lat, goal_lon)
                sequence += 1
                heapq.heappush(open_heap, (rank, nxt, sequence, neighbor))
    return None


def contract(raw: dict) -> list[tuple[object, object, list[tuple[float, float]]]]:
    """Collapse degree-2 chains. raw[node] = [(neighbor, polyline from node to neighbor)].

    Polylines are (lat, lon). Returns one edge per direction, (a, b, polyline from a to b).
    """
    degree = {node: len(links) for node, links in raw.items()}
    used: set[tuple] = set()
    seen: set[tuple] = set()
    edges = []

    def follow(start, nxt, line):
        path = list(line)
        steps = [(start, nxt)]
        prev, cur = start, nxt
        guard = 0
        while degree.get(cur, 0) == 2 and cur != start and guard < 100000:
            guard += 1
            links = raw[cur]
            if len(links) != 2:
                break
            chosen = links[0] if links[1][0] == prev else links[1]
            nxt_node, nxt_line = chosen
            if (cur, nxt_node) in used or (cur, nxt_node) in steps:
                break
            steps.append((cur, nxt_node))
            path.extend(nxt_line[1:])
            prev, cur = cur, nxt_node
        return cur, path, steps

    def emit_from(start):
        for nxt, line in raw.get(start, ()):
            if (start, nxt) in used:
                continue
            end, path, steps = follow(start, nxt, line)
            if len(path) < 2:
                continue
            signature = tuple((round(point[0], 5), round(point[1], 5)) for point in path)
            reverse = signature[::-1]
            for step in steps:
                used.add(step)
            if signature in seen or reverse in seen:
                continue
            seen.add(signature)
            edges.append((start, end, path))

    for node, deg in degree.items():
        if deg != 2:
            emit_from(node)
    for node, deg in degree.items():
        if deg == 2 and all((node, nxt) not in used for nxt, _line in raw.get(node, ())):
            emit_from(node)
    return edges


def routing_graph(edges, snaps: dict):
    """Split edges at station snaps.

    edges: (a, b, polyline of (lat, lon))
    snaps: stop_id -> (edge_index, along_m, lat, lon)
    Returns (adj, coords) where stop nodes are ('s', stop_id).
    """
    grouped: dict[int, list] = {}
    for stop_id, (index, along, lat, lon) in snaps.items():
        grouped.setdefault(index, []).append((along, stop_id, lat, lon))

    adj: dict = {}
    coords: dict = {}

    def link(left, right, line):
        if len(line) < 2 or left == right:
            return
        step = polyline_meters(line)
        adj.setdefault(left, []).append((right, step, line))
        adj.setdefault(right, []).append((left, step, list(reversed(line))))
        coords.setdefault(left, line[0])
        coords.setdefault(right, line[-1])

    for index, (a, b, points) in enumerate(edges):
        cumulative = [0.0]
        for i in range(1, len(points)):
            cumulative.append(cumulative[-1] + meters(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1]))
        total = cumulative[-1]
        cuts = [(0.0, a, points[0][0], points[0][1])]
        for along, stop_id, lat, lon in sorted(grouped.get(index, ())):
            cuts.append((max(0.0, min(total, along)), ("s", stop_id), lat, lon))
        cuts.append((total, b, points[-1][0], points[-1][1]))
        cuts.sort(key=lambda item: item[0])
        for left, right in zip(cuts, cuts[1:]):
            piece = _slice(points, cumulative, left[0], right[0])
            if piece and piece[0] != (left[2], left[3]):
                piece = [(left[2], left[3]), *piece]
            if piece and piece[-1] != (right[2], right[3]):
                piece = [*piece, (right[2], right[3])]
            if not piece:
                piece = [(left[2], left[3]), (right[2], right[3])]
            link(left[1], right[1], piece)
        coords.setdefault(a, points[0])
        coords.setdefault(b, points[-1])
    return adj, coords


def _slice(points, cumulative, start_m: float, end_m: float) -> list[tuple[float, float]]:
    if end_m < start_m:
        start_m, end_m = end_m, start_m
        flipped = True
    else:
        flipped = False
    if not points:
        return []
    out = [_at(points, cumulative, start_m)]
    for index in range(1, len(points)):
        if cumulative[index] <= start_m or cumulative[index - 1] >= end_m:
            continue
        if start_m < cumulative[index] < end_m:
            out.append(points[index])
    end = _at(points, cumulative, end_m)
    if not out or out[-1] != end:
        out.append(end)
    if flipped:
        out.reverse()
    return out


def _at(points, cumulative, distance: float) -> tuple[float, float]:
    if distance <= 0:
        return points[0]
    if distance >= cumulative[-1]:
        return points[-1]
    for index in range(1, len(points)):
        if cumulative[index] >= distance:
            span = cumulative[index] - cumulative[index - 1]
            t = 0 if span == 0 else (distance - cumulative[index - 1]) / span
            lat = points[index - 1][0] + (points[index][0] - points[index - 1][0]) * t
            lon = points[index - 1][1] + (points[index][1] - points[index - 1][1]) * t
            return (lat, lon)
    return points[-1]


def path_coords(adj: dict, nodes: list) -> list[tuple[float, float]]:
    """Join edge polylines. Each polyline is (lat, lon) and includes both ends."""
    if len(nodes) == 1:
        return []
    coords: list[tuple[float, float]] = []
    for left, right in zip(nodes, nodes[1:]):
        line = None
        for neighbor, _step, poly in adj.get(left, ()):
            if neighbor == right:
                line = poly
                break
        if not line:
            continue
        if coords and line and coords[-1] == line[0]:
            coords.extend(line[1:])
        else:
            coords.extend(line)
    return coords
