"""Plain records shared by the parser, the joiner, and the estimator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Status(Enum):
    IN_MOTION = "in_motion"
    DWELLING = "dwelling"
    NOT_STARTED = "not_started"
    FINISHED = "finished"
    TOO_FEW_STOPS = "too_few_stops"
    CANCELED = "canceled"
    NO_CALLS = "no_calls"


@dataclass(frozen=True)
class Call:
    seq: int
    stop_id: str
    name: str
    lat: float | None
    lon: float | None
    arrival: datetime | None
    departure: datetime | None


@dataclass(frozen=True)
class StopPatch:
    """One StopTimeUpdate, already decoded. Sequence is None when the feed omitted it."""

    stop_sequence: int | None
    stop_id: str | None
    arrival_delay: int | None
    departure_delay: int | None
    arrival_time: int | None = None
    departure_time: int | None = None
    skipped: bool = False


@dataclass(frozen=True)
class RealtimeTrip:
    trip_id: str
    start_date: str
    route_id: str
    canceled: bool
    stops: tuple[StopPatch, ...]


@dataclass(frozen=True)
class ParsedFeed:
    header_time: datetime | None
    trips: tuple[RealtimeTrip, ...]
    vehicle_count: int
    alert_count: int


@dataclass(frozen=True)
class TripInfo:
    trip_id: str
    route_name: str
    route_type: int
    service_id: str
    rail: bool


@dataclass(frozen=True)
class Position:
    status: Status
    trip_id: str
    route_name: str
    start_date: str
    from_name: str | None = None
    to_name: str | None = None
    stop_name: str | None = None
    fraction: float | None = None
    lat: float | None = None
    lon: float | None = None
    delay_seconds: int | None = None
    delay_explicit: bool = False
    stop_mismatches: int = 0
    gap_km: float | None = None
    upcoming: tuple[str, ...] = ()
