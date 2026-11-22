"""Turn estimated positions into GeoJSON for the map."""

from __future__ import annotations

from delay_radar.model import Position

ON_TIME_SECONDS = 5 * 60
MINOR_SECONDS = 15 * 60


def delay_band(delay_seconds: int | None) -> str:
    if delay_seconds is None:
        return "unknown"
    if delay_seconds <= ON_TIME_SECONDS:
        return "on_time"
    if delay_seconds <= MINOR_SECONDS:
        return "minor"
    return "severe"


def feature(position: Position) -> dict | None:
    if position.lat is None or position.lon is None:
        return None
    upcoming = list(position.upcoming)
    next_stop = position.to_name or (upcoming[0] if upcoming else position.stop_name)
    delay_min = None if position.delay_seconds is None else round(position.delay_seconds / 60)
    return {
        "type": "Feature",
        "id": f"{position.trip_id}|{position.start_date}",
        "geometry": {"type": "Point", "coordinates": [position.lon, position.lat]},
        "properties": {
            "id": f"{position.trip_id}|{position.start_date}",
            "name": position.route_name,
            "trip_id": position.trip_id,
            "status": position.status.value,
            "from_stop": position.from_name or position.stop_name,
            "next_stop": next_stop,
            "delay_min": delay_min,
            "delay_explicit": position.delay_explicit,
            "band": delay_band(position.delay_seconds),
            "upcoming": upcoming,
        },
    }


def feature_collection(positions: list[Position]) -> dict:
    features = []
    for position in positions:
        item = feature(position)
        if item is not None:
            features.append(item)
    return {"type": "FeatureCollection", "features": features}
