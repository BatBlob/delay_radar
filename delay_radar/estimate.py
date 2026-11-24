"""Place a train from scheduled calls plus per-stop delay.

`now` is compared with real timestamps (scheduled + delay), never with a
fraction of the timetable. Stops before the first explicit update stay on
time. Later stops with no update inherit the previous stop's delay.
"""

from __future__ import annotations

import math
from datetime import timedelta

from delay_radar.model import Call, Position, Status, StopPatch


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _fill_absolute_times(calls: list[Call], patches: tuple[StopPatch, ...] | list[StopPatch]) -> list[StopPatch]:
    """Turn a published unix time into a delay when the feed omitted `delay`."""
    by_seq = {call.seq: call for call in calls}
    by_id: dict[str, list[Call]] = {}
    for call in calls:
        by_id.setdefault(call.stop_id, []).append(call)
    filled = []
    for patch in patches:
        target = None
        if patch.stop_sequence is not None and patch.stop_sequence in by_seq:
            target = by_seq[patch.stop_sequence]
        elif patch.stop_id and len(by_id.get(patch.stop_id, [])) == 1:
            target = by_id[patch.stop_id][0]
        arrival, departure = patch.arrival_delay, patch.departure_delay
        if target is not None:
            if arrival is None and patch.arrival_time is not None and target.arrival is not None:
                arrival = int(patch.arrival_time - target.arrival.timestamp())
            if departure is None and patch.departure_time is not None and target.departure is not None:
                departure = int(patch.departure_time - target.departure.timestamp())
        if arrival == patch.arrival_delay and departure == patch.departure_delay:
            filled.append(patch)
        else:
            filled.append(
                StopPatch(
                    patch.stop_sequence,
                    patch.stop_id,
                    arrival,
                    departure,
                    patch.arrival_time,
                    patch.departure_time,
                    patch.skipped,
                )
            )
    return filled


def resolve_patches(calls: list[Call], patches: tuple[StopPatch, ...] | list[StopPatch]) -> tuple[dict[int, tuple[int | None, int | None, bool]], int]:
    """Map updates onto scheduled stop sequences.

    A provided stop_sequence wins, including 0. A stop_id is used only when
    that id occurs once on the trip. Returns (seq -> (arr_delay, dep_delay, skipped), mismatches).
    """
    by_seq = {call.seq: call for call in calls}
    ids: dict[str, list[Call]] = {}
    for call in calls:
        ids.setdefault(call.stop_id, []).append(call)

    resolved: dict[int, tuple[int | None, int | None, bool]] = {}
    mismatches = 0
    for patch in patches:
        target: Call | None = None
        if patch.stop_sequence is not None and patch.stop_sequence in by_seq:
            target = by_seq[patch.stop_sequence]
            if patch.stop_id and target.stop_id != patch.stop_id:
                mismatches += 1
        elif patch.stop_id:
            found = ids.get(patch.stop_id, [])
            if len(found) == 1:
                target = found[0]
            else:
                mismatches += 1
                continue
        else:
            mismatches += 1
            continue
        if target is None:
            mismatches += 1
            continue
        resolved[target.seq] = (patch.arrival_delay, patch.departure_delay, patch.skipped)
    return resolved, mismatches


def estimate(
    *,
    trip_id: str,
    route_name: str,
    start_date: str,
    calls: list[Call],
    patches: tuple[StopPatch, ...] | list[StopPatch],
    now,
    canceled: bool = False,
    paths=None,
) -> Position:
    base = dict(trip_id=trip_id, route_name=route_name, start_date=start_date)
    if canceled:
        return Position(status=Status.CANCELED, **base)
    if not calls:
        return Position(status=Status.NO_CALLS, **base)

    patches = _fill_absolute_times(calls, patches)
    resolved, mismatches = resolve_patches(calls, patches)
    carried: int | None = None
    timed: list[tuple[Call, object, object]] = []
    for call in calls:
        arr_delay, dep_delay, skipped = resolved.get(call.seq, (None, None, False))
        if arr_delay is not None:
            carried = arr_delay
        use_arr = arr_delay if arr_delay is not None else (0 if carried is None else carried)
        if dep_delay is not None:
            carried = dep_delay
        use_dep = dep_delay if dep_delay is not None else (0 if carried is None else carried)
        if skipped:
            continue
        arrival = call.arrival + timedelta(seconds=use_arr) if call.arrival is not None else None
        departure = call.departure + timedelta(seconds=use_dep) if call.departure is not None else None
        timed.append((call, arrival, departure))

    delays = _applied_delays(calls, resolved)

    if len(timed) < 2:
        return Position(status=Status.TOO_FEW_STOPS, stop_mismatches=mismatches, **base)

    origin = _classify(timed, now)
    kind = origin[0]
    if kind == "not_started":
        return Position(status=Status.NOT_STARTED, stop_mismatches=mismatches, **base)
    if kind == "finished":
        return Position(status=Status.FINISHED, stop_mismatches=mismatches, **base)
    if kind == "dwelling":
        call = origin[1]
        lat, lon = call.lat, call.lon
        delay, explicit = delays.get(call.seq, (0, False))
        index = next(i for i, item in enumerate(timed) if item[0] is call)
        return Position(
            status=Status.DWELLING,
            stop_name=call.name,
            lat=lat,
            lon=lon,
            delay_seconds=delay,
            delay_explicit=explicit,
            stop_mismatches=mismatches,
            upcoming=_names_after(timed, index),
            **base,
        )

    index, fraction = origin[1], origin[2]
    start, end = timed[index][0], timed[index + 1][0]
    lat, lon, gap, fallback = _locate(start, end, fraction, paths)
    delay, explicit = delays.get(end.seq, (0, False))
    return Position(
        status=Status.IN_MOTION,
        from_name=start.name,
        to_name=end.name,
        fraction=fraction,
        lat=lat,
        lon=lon,
        delay_seconds=delay,
        delay_explicit=explicit,
        stop_mismatches=mismatches,
        gap_km=gap,
        upcoming=_names_after(timed, index),
        track_fallback=fallback,
        **base,
    )


def _locate(start: Call, end: Call, fraction: float, paths):
    if None in (start.lat, start.lon, end.lat, end.lon):
        return None, None, None, False
    lat = start.lat + (end.lat - start.lat) * fraction
    lon = start.lon + (end.lon - start.lon) * fraction
    gap = _km(start.lat, start.lon, end.lat, end.lon)
    if paths is None:
        return lat, lon, gap, False
    lat, lon, fell_back = paths.place(start.stop_id, end.stop_id, fraction, (lat, lon))
    return lat, lon, gap, fell_back


def _names_after(timed, index: int, limit: int = 6) -> tuple[str, ...]:
    names = []
    for call, _arrival, _departure in timed[index + 1 :]:
        names.append(call.name)
        if len(names) >= limit:
            break
    return tuple(names)


def _applied_delays(calls: list[Call], resolved: dict[int, tuple[int | None, int | None, bool]]) -> dict[int, tuple[int, bool]]:
    """seq -> (delay seconds used for the arrival at that stop, explicit?)."""
    carried: int | None = None
    out: dict[int, tuple[int, bool]] = {}
    for call in calls:
        arr_delay, dep_delay, _skipped = resolved.get(call.seq, (None, None, False))
        if arr_delay is not None:
            carried = arr_delay
            out[call.seq] = (arr_delay, True)
        else:
            out[call.seq] = (0 if carried is None else carried, False)
        if dep_delay is not None:
            carried = dep_delay
    return out


def _classify(timed, now):
    first = timed[0]
    first_arrival, first_departure = first[1], first[2]
    if first_arrival is not None and first_departure is not None and first_arrival <= now < first_departure:
        return ("dwelling", first[0])
    first_dep = first_departure or first_arrival
    if first_dep is not None and now < first_dep:
        return ("not_started",)
    last = timed[-1]
    last_arr = last[1] or last[2]
    if last_arr is not None and now >= last_arr:
        return ("finished",)
    for index in range(len(timed) - 1):
        depart = timed[index][2] or timed[index][1]
        arrive = timed[index + 1][1] or timed[index + 1][2]
        if depart is None or arrive is None or arrive <= depart:
            continue
        if depart <= now < arrive:
            return ("in_motion", index, (now - depart) / (arrive - depart))
    for call, arrival, departure in timed:
        if arrival is not None and departure is not None and arrival <= now < departure:
            return ("dwelling", call)
    return ("finished",)
