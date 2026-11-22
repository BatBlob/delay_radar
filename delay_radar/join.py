"""Join realtime trip updates to static trips and estimate positions."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from delay_radar.clock import parse_service_day
from delay_radar.estimate import estimate
from delay_radar.model import ParsedFeed, Position, RealtimeTrip, Status
from delay_radar.store import Schedule


@dataclass
class Summary:
    trip_updates: int
    matched: int
    duplicate_extras: int
    not_rail: int
    off_calendar: int
    missing_start_date: int
    stop_mismatches: int
    missing_coords: int
    long_gaps: int
    empty_route_id: int
    vehicle_count: int
    by_status: Counter = field(default_factory=Counter)
    in_motion: list[Position] = field(default_factory=list)
    dwelling: list[Position] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        if not self.trip_updates:
            return 0.0
        return self.matched / self.trip_updates


def dedupe(trips: tuple[RealtimeTrip, ...] | list[RealtimeTrip]) -> tuple[list[RealtimeTrip], int]:
    """Keep one update per (trip_id, start_date). The copy with more stops wins."""
    chosen: dict[tuple[str, str], RealtimeTrip] = {}
    order: list[tuple[str, str]] = []
    extras = 0
    for trip in trips:
        key = (trip.trip_id, trip.start_date)
        current = chosen.get(key)
        if current is None:
            chosen[key] = trip
            order.append(key)
            continue
        extras += 1
        if _richer(trip, current):
            chosen[key] = trip
    return [chosen[key] for key in order], extras


def _richer(new: RealtimeTrip, old: RealtimeTrip) -> bool:
    if len(new.stops) != len(old.stops):
        return len(new.stops) > len(old.stops)
    if new.canceled != old.canceled:
        return not new.canceled
    return False


def place(schedule: Schedule, feed: ParsedFeed, now: datetime) -> Summary:
    summary = Summary(
        trip_updates=len(feed.trips),
        matched=0,
        duplicate_extras=0,
        not_rail=0,
        off_calendar=0,
        missing_start_date=0,
        stop_mismatches=0,
        missing_coords=0,
        long_gaps=0,
        empty_route_id=sum(1 for trip in feed.trips if not trip.route_id),
        vehicle_count=feed.vehicle_count,
    )
    unique, extras = dedupe(feed.trips)
    summary.duplicate_extras = extras
    infos = schedule.lookup([trip.trip_id for trip in unique])
    # Match rate counts raw entities, before duplicate collapsing.
    summary.matched = sum(1 for trip in feed.trips if trip.trip_id in infos)

    runnable: list[RealtimeTrip] = []
    instances: list[tuple[str, object]] = []
    for trip in unique:
        info = infos.get(trip.trip_id)
        if info is None:
            continue
        if not info.rail:
            summary.not_rail += 1
            continue
        day = parse_service_day(trip.start_date)
        if day is None:
            summary.missing_start_date += 1
            continue
        runs = schedule.service_runs(info.service_id, day)
        if runs is False:
            summary.off_calendar += 1
            continue
        instances.append((trip.trip_id, day))
        runnable.append(trip)

    calls = schedule.calls_for(instances)
    unmatched_samples = []
    for trip in unique:
        if trip.trip_id not in infos and len(unmatched_samples) < 8:
            unmatched_samples.append(trip.trip_id or "<empty>")
    for trip in runnable:
        info = infos[trip.trip_id]
        position = estimate(
            trip_id=trip.trip_id,
            route_name=info.route_name,
            start_date=trip.start_date,
            calls=calls.get((trip.trip_id, trip.start_date), []),
            patches=trip.stops,
            now=now,
            canceled=trip.canceled,
        )
        summary.by_status[position.status] += 1
        summary.stop_mismatches += position.stop_mismatches
        if position.status == Status.IN_MOTION:
            if position.lat is None:
                summary.missing_coords += 1
            elif position.gap_km is not None and position.gap_km > 40:
                summary.long_gaps += 1
            summary.in_motion.append(position)
        elif position.status == Status.DWELLING and position.lat is not None:
            summary.dwelling.append(position)

    if feed.vehicle_count == 0:
        summary.problems.append("realtime feed has no VehiclePosition entities. Positions are estimated, not GPS.")
    if summary.trip_updates and summary.matched < summary.trip_updates:
        summary.problems.append(
            f"{summary.trip_updates - summary.matched} trip updates are not in this static file. "
            f"Sample trip_ids: {', '.join(unmatched_samples) or 'none'}."
        )
    if summary.duplicate_extras:
        summary.problems.append(
            f"{summary.duplicate_extras} extra copies of a (trip_id, start_date) were collapsed to one update."
        )
    if summary.off_calendar:
        summary.problems.append(f"{summary.off_calendar} rail trips are not scheduled on their realtime start_date.")
    if summary.missing_start_date:
        summary.problems.append(f"{summary.missing_start_date} rail trips have no usable start_date.")
    if summary.stop_mismatches:
        summary.problems.append(
            f"{summary.stop_mismatches} StopTimeUpdates cited a stop_sequence or stop_id that does not agree with the static trip."
        )
    if summary.missing_coords:
        summary.problems.append(f"{summary.missing_coords} in-motion trains are missing stop coordinates.")
    if summary.long_gaps:
        summary.problems.append(
            f"{summary.long_gaps} in-motion trains span more than 40 km between consecutive stops. "
            "The straight line leaves the railway because this feed has no shapes.txt."
        )
    if summary.empty_route_id:
        summary.problems.append(
            f"{summary.empty_route_id} trip updates have an empty route_id. The line name comes from static routes.txt."
        )
    if summary.not_rail:
        summary.problems.append(
            f"{summary.not_rail} matched trips are buses, trams, metros or ferries (route_type other than 2) and are not placed. "
            "S-Bahn in this feed is route_type 2 and is included."
        )
    return summary
