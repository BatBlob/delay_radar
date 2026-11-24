"""Cached rail geometry for one stop pair, loaded once and reused."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from delay_radar.railgraph import point_along

CACHE_NAME = "track-legs.sqlite"
DISPLAY_NAME = "tracks.geojson"
DISPLAY_MINOR_NAME = "tracks-minor.geojson"


def cache_path(cache_dir: Path) -> Path:
    return cache_dir / CACHE_NAME


def display_path(cache_dir: Path) -> Path:
    return cache_dir / DISPLAY_NAME


def display_minor_path(cache_dir: Path) -> Path:
    return cache_dir / DISPLAY_MINOR_NAME


class TrackPaths:
    def __init__(self, legs: dict[tuple[str, str], list[tuple[float, float]] | None]):
        self._legs = legs
        self.routed = sum(1 for line in legs.values() if line)
        self.fallback = sum(1 for line in legs.values() if not line)

    def line(self, stop_a: str, stop_b: str) -> list[tuple[float, float]] | None:
        return self._legs.get((stop_a, stop_b))

    def place(self, stop_a: str, stop_b: str, fraction: float, straight: tuple[float, float]) -> tuple[float, float, bool]:
        """Return (lat, lon, fell_back)."""
        line = self.line(stop_a, stop_b)
        if not line:
            return straight[0], straight[1], True
        lat, lon = point_along(line, fraction)
        return lat, lon, False


def load_paths(cache_dir: Path) -> TrackPaths | None:
    path = cache_path(cache_dir)
    if not path.exists():
        return None
    connection = sqlite3.connect(path)
    try:
        legs: dict[tuple[str, str], list[tuple[float, float]] | None] = {}
        for stop_a, stop_b, coords in connection.execute("SELECT stop_a, stop_b, coords FROM legs"):
            legs[(stop_a, stop_b)] = None if not coords else [tuple(pair) for pair in json.loads(coords)]
    finally:
        connection.close()
    return TrackPaths(legs)
