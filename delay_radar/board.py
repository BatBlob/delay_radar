"""Keep the latest train positions and refresh them off the request path."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from delay_radar.clock import BERLIN
from delay_radar.feeds import ensure_static, fetch_realtime
from delay_radar.geo import feature_collection
from delay_radar.join import place
from delay_radar.realtime import parse_feed
from delay_radar.snapshot import load, resolve
from delay_radar.store import Schedule, ensure_database

REFRESH_SECONDS = 180
MANUAL_MIN_SECONDS = 15


def sleep_seconds(retry_after: float | None) -> float:
    if retry_after is None:
        return REFRESH_SECONDS
    return max(REFRESH_SECONDS, min(retry_after, 600))


class Board:
    def __init__(self, data_dir: Path, replay: str | None = None):
        self.data_dir = data_dir
        self.replay = replay
        self._lock = threading.Lock()
        self.positions: dict | None = None
        self.status: dict | None = None
        self._wait = REFRESH_SECONDS
        self._refresh_lock = threading.Lock()
        self._wake = threading.Event()
        self._last_refresh = 0.0
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        thread = threading.Thread(target=self._loop, name="delay-radar-refresh", daemon=True)
        thread.start()

    def _loop(self) -> None:
        while True:
            self._run_refresh()
            self._sleep_until_next()

    def _run_refresh(self) -> None:
        with self._refresh_lock:
            self._refresh_holding_lock()

    def _refresh_holding_lock(self) -> None:
        try:
            self.refresh()
        except Exception as exc:
            print(f"refresh failed: {exc}")
            self._fail("The live delay feed did not load. Showing the last good data.")
        self._last_refresh = time.monotonic()

    def request_refresh(self) -> None:
        """Fetch now. A second request within 15 seconds reuses that result."""
        with self._refresh_lock:
            if time.monotonic() - self._last_refresh < MANUAL_MIN_SECONDS:
                return
            self._refresh_holding_lock()
        self._wake.set()

    def _sleep_until_next(self) -> None:
        end = time.monotonic() + self._wait
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            if self._wake.wait(remaining):
                self._wake.clear()
                end = time.monotonic() + self._wait

    def snapshot(self) -> tuple[dict | None, dict | None]:
        with self._lock:
            return self.positions, self.status

    def refresh(self) -> None:
        banner = None
        if self.replay:
            folder = resolve(self.data_dir, self.replay)
            static_path, payload, meta = load(folder)
            modified = _parse_iso(meta.get("last_modified"))
            source = "snapshot"
            replay = True
            self._wait = REFRESH_SECONDS
        else:
            cache = self.data_dir / "cache"
            static, _problems = ensure_static(cache)
            fetched = fetch_realtime(cache)
            self._wait = sleep_seconds(fetched.retry_after)
            if fetched.payload is None:
                self._fail(fetched.banner or "The live delay feed did not load.")
                return
            payload = fetched.payload
            static_path = static.path
            modified = static.last_modified
            source = "live"
            replay = False
            banner = fetched.banner or static.notice

        feed = parse_feed(payload)
        now = feed.header_time.astimezone(BERLIN) if replay and feed.header_time else datetime.now(BERLIN)
        # A new connection each refresh: SQLite connections cannot cross threads.
        schedule = Schedule(ensure_database(static_path, self.data_dir / "cache"))
        try:
            summary = place(schedule, feed, now)
        finally:
            schedule.close()
        positions = feature_collection([*summary.in_motion, *summary.dwelling])
        wall = datetime.now(timezone.utc)
        feed_age = None if feed.header_time is None else int((wall - feed.header_time).total_seconds())
        schedule_age = None if modified is None else int((wall - modified).total_seconds())
        status = {
            "source": source,
            "replay": replay,
            "as_of": now.isoformat(),
            "feed_time": None if feed.header_time is None else feed.header_time.isoformat(),
            "feed_age_seconds": feed_age,
            "schedule_time": None if modified is None else modified.isoformat(),
            "schedule_age_seconds": schedule_age,
            "matched": summary.matched,
            "trip_updates": summary.trip_updates,
            "in_motion": len(summary.in_motion),
            "dwelling": len(summary.dwelling),
            "on_map": len(positions["features"]),
            "estimated": True,
            "banner": None if replay else banner,
            "error": None,
        }
        with self._lock:
            self.positions = positions
            self.status = status

    def _fail(self, banner: str) -> None:
        with self._lock:
            if self.positions is None and "last good" in banner:
                banner = "The live delay feed did not load. No saved delay feed yet."
            base = self.status or {
                "source": "live",
                "replay": False,
                "as_of": None,
                "feed_time": None,
                "feed_age_seconds": None,
                "schedule_time": None,
                "schedule_age_seconds": None,
                "matched": 0,
                "trip_updates": 0,
                "in_motion": 0,
                "dwelling": 0,
                "on_map": 0,
                "estimated": True,
            }
            self.status = {**base, "banner": banner, "error": None}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
