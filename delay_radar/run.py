"""Live report, snapshot recording, and replay."""

from __future__ import annotations

import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from delay_radar.clock import BERLIN
from delay_radar.feeds import (
    ensure_static,
    fetch_realtime,
    is_realtime_stale,
)
from delay_radar.join import place
from delay_radar.model import Status
from delay_radar.realtime import parse_feed
from delay_radar.snapshot import load, record, resolve
from delay_radar.store import Schedule, ensure_database
from delay_radar.tracks import load_paths


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    data = Path("data")
    command = args[0] if args else "live"
    if command == "serve":
        replay = None
        if "--replay" in args:
            index = args.index("--replay")
            replay = args[index + 1] if index + 1 < len(args) else "latest"
        from delay_radar.web import serve

        serve(replay=replay)
        return 0
    if command not in {"live", "record", "replay"}:
        print("usage: python -m delay_radar [serve|live|record|replay latest|<snapshot>]")
        return 2

    problems: list[str] = []
    replay = command == "replay"
    if replay:
        name = args[1] if len(args) > 1 else "latest"
        try:
            folder = resolve(data, name)
        except FileNotFoundError as exc:
            print(exc)
            return 1
        static_path, payload, meta = load(folder)
        print(f"replay {folder.name}  static {meta.get('static_url')}")
        fallback = bool(meta.get("fallback"))
    else:
        static, static_problems = ensure_static(data / "cache")
        problems.extend(static_problems)
        fetched = fetch_realtime(data / "cache")
        if fetched.banner:
            problems.append(fetched.banner)
        if fetched.payload is None:
            print(fetched.banner or "realtime download failed")
            return 1
        payload = fetched.payload
        static_path = static.path
        fallback = static.fallback
        print(f"static {static.url}  cached={static.from_cache}  fallback={static.fallback}")
        if command == "record":
            folder = record(
                data,
                static.path,
                payload,
                {
                    "static_url": static.url,
                    "fallback": static.fallback,
                    "last_modified": None if static.last_modified is None else static.last_modified.isoformat(),
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            print(f"recorded {folder.name}")

    feed = parse_feed(payload)
    if feed.header_time is None:
        problems.append("realtime feed has no header timestamp.")
        now = datetime.now(BERLIN)
    elif replay:
        now = feed.header_time.astimezone(BERLIN)
        print(f"clock frozen at {now.isoformat()}")
    else:
        now = datetime.now(BERLIN)
        if is_realtime_stale(feed.header_time):
            age = datetime.now(timezone.utc) - feed.header_time
            problems.append(f"realtime header is {int(age.total_seconds())}s old.")
        else:
            age = datetime.now(timezone.utc) - feed.header_time
            print(f"realtime header age {int(age.total_seconds())}s")

    if fallback:
        problems.append("using fv_free, so only long-distance trip ids can match.")
    paths = load_paths(data / "cache")
    with zipfile.ZipFile(static_path) as archive:
        if paths is None and "shapes.txt" not in archive.namelist():
            problems.append("static feed has no shapes.txt. In-motion coordinates are straight lines between stops.")

    db_path = ensure_database(static_path, data / "cache")
    schedule = Schedule(db_path)
    try:
        summary = place(schedule, feed, now, paths=paths)
    finally:
        schedule.close()

    rate = summary.match_rate
    print(
        f"match rate: {summary.matched}/{summary.trip_updates} "
        f"({rate:.1%}) trip updates"
    )
    print(f"trains in motion: {len(summary.in_motion)}")
    counts = ", ".join(
        f"{status.value}={summary.by_status[status]}"
        for status in Status
        if summary.by_status[status]
    )
    if counts:
        print(f"rail outcomes: {counts}")
    shown = sorted(summary.in_motion, key=lambda item: abs(item.delay_seconds or 0), reverse=True)[:3]
    for item in shown:
        delay = "no delay" if item.delay_seconds is None else f"{item.delay_seconds / 60:+.0f} min"
        print(
            f"  {item.route_name}  {item.trip_id}  {item.from_name} -> {item.to_name}  "
            f"{(item.fraction or 0) * 100:.0f}%  {delay}"
        )
    print("problems:")
    seen = problems + summary.problems
    if not seen:
        print("  none")
    for problem in seen:
        print(f"  - {problem}")
    return 0
