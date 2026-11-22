"""Decode a GTFS-RT FeedMessage into plain trip updates."""

from __future__ import annotations

from datetime import datetime, timezone

from google.transit import gtfs_realtime_pb2

from delay_radar.model import ParsedFeed, RealtimeTrip, StopPatch

CANCELED = gtfs_realtime_pb2.TripDescriptor.ScheduleRelationship.CANCELED
SKIPPED = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.ScheduleRelationship.SKIPPED


def _event(event) -> tuple[int | None, int | None]:
    if event is None:
        return None, None
    delay = int(event.delay) if event.HasField("delay") else None
    when = int(event.time) if event.HasField("time") else None
    return delay, when


def parse_feed(payload: bytes) -> ParsedFeed:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(payload)
    header_time = None
    if feed.header.HasField("timestamp") and feed.header.timestamp:
        header_time = datetime.fromtimestamp(feed.header.timestamp, tz=timezone.utc)
    trips = []
    vehicles = 0
    alerts = 0
    for entity in feed.entity:
        if entity.HasField("vehicle"):
            vehicles += 1
        if entity.HasField("alert"):
            alerts += 1
        if not entity.HasField("trip_update"):
            continue
        update = entity.trip_update
        trip = update.trip
        stops = []
        for stu in update.stop_time_update:
            sequence = int(stu.stop_sequence) if stu.HasField("stop_sequence") else None
            arrival_delay, arrival_time = _event(stu.arrival if stu.HasField("arrival") else None)
            departure_delay, departure_time = _event(stu.departure if stu.HasField("departure") else None)
            stops.append(
                StopPatch(
                    stop_sequence=sequence,
                    stop_id=stu.stop_id or None,
                    arrival_delay=arrival_delay,
                    departure_delay=departure_delay,
                    arrival_time=arrival_time,
                    departure_time=departure_time,
                    skipped=stu.schedule_relationship == SKIPPED,
                )
            )
        trips.append(
            RealtimeTrip(
                trip_id=trip.trip_id,
                start_date=trip.start_date or "",
                route_id=trip.route_id or "",
                canceled=trip.schedule_relationship == CANCELED,
                stops=tuple(stops),
            )
        )
    return ParsedFeed(
        header_time=header_time,
        trips=tuple(trips),
        vehicle_count=vehicles,
        alert_count=alerts,
    )
