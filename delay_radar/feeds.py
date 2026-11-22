"""Download the static GTFS zip and the GTFS-RT feed, with a disk cache."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

USER_AGENT = "delay-radar/0.1 (+https://gtfs.de)"
PRIMARY_STATIC_URL = "https://download.gtfs.de/germany/free/latest.zip"
FALLBACK_STATIC_URL = "https://download.gtfs.de/germany/fv_free/latest.zip"
REALTIME_URL = "https://realtime.gtfs.de/realtime-free.pb"

STATIC_STALE_AFTER = timedelta(hours=26)
STATIC_REFRESH_EVERY = timedelta(hours=24)
FAILURE_BACKOFF = timedelta(minutes=15)
REALTIME_STALE_AFTER = timedelta(seconds=120)
RATE_LIMIT_CAP_SECONDS = 600


class RateLimited(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"rate limited, retry after {retry_after:.0f}s")


@dataclass(frozen=True)
class StaticFile:
    path: Path
    url: str
    last_modified: datetime | None
    from_cache: bool
    fallback: bool
    notice: str | None = None


@dataclass(frozen=True)
class RealtimeFetch:
    payload: bytes | None
    last_modified: datetime | None
    fetched_at: datetime | None
    from_cache: bool
    banner: str | None
    retry_after: float | None


def _request(url: str, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})


def _last_modified(header: str | None) -> datetime | None:
    if not header:
        return None
    try:
        parsed = parsedate_to_datetime(header)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _urlopen(request: urllib.request.Request, timeout: int):
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimited(_retry_after(exc)) from exc
        raise


def _retry_after(exc: urllib.error.HTTPError) -> float:
    header = exc.headers.get("Retry-After") if exc.headers else None
    if header and str(header).strip().isdigit():
        return min(float(header), RATE_LIMIT_CAP_SECONDS)
    if header:
        when = _last_modified(str(header))
        if when is not None:
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
            return min(max(seconds, 1), RATE_LIMIT_CAP_SECONDS)
    return 60


def head_last_modified(url: str) -> datetime | None:
    try:
        with _urlopen(_request(url, "HEAD"), timeout=30) as response:
            return _last_modified(response.headers.get("Last-Modified"))
    except RateLimited:
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def _download(url: str, dest: Path) -> datetime | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    with _urlopen(_request(url), timeout=120) as response, partial.open("wb") as handle:
        modified = _last_modified(response.headers.get("Last-Modified"))
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    partial.replace(dest)
    return modified


def _read_meta(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_meta(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def static_age(modified: datetime | None, now: datetime | None = None) -> timedelta | None:
    if modified is None:
        return None
    clock = now or datetime.now(timezone.utc)
    return clock - modified


def is_static_stale(modified: datetime | None, now: datetime | None = None) -> bool:
    age = static_age(modified, now)
    return age is not None and age > STATIC_STALE_AFTER


def is_realtime_stale(header_time: datetime | None, now: datetime | None = None) -> bool:
    if header_time is None:
        return True
    clock = now or datetime.now(timezone.utc)
    return clock - header_time > REALTIME_STALE_AFTER


def _due(meta: dict, now: datetime) -> bool:
    nxt = _parse_time(meta.get("next_check"))
    return nxt is None or now >= nxt


def _postpone(meta_path: Path, meta: dict, now: datetime, seconds: float) -> None:
    updated = dict(meta)
    updated["next_check"] = (now + timedelta(seconds=seconds)).isoformat()
    _write_meta(meta_path, updated)


def _cached_static(dest: Path, modified: datetime | None, notice: str | None) -> StaticFile:
    return StaticFile(dest, PRIMARY_STATIC_URL, modified, True, False, notice)


def _has_full_cache(dest: Path, meta: dict) -> bool:
    return dest.exists() and meta.get("url") == PRIMARY_STATIC_URL


def ensure_static(cache_dir: Path, now: datetime | None = None) -> tuple[StaticFile, list[str]]:
    """Return the full-Germany zip.

    The remote file is checked at most once a day. A failed download or a
    rate limit keeps the cached zip and tries again after a short backoff.
    fv_free is used only when the full zip cannot be downloaded and no full
    copy is already cached.
    """
    clock = now or datetime.now(timezone.utc)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / "germany-free.zip"
    meta_path = cache_dir / "germany-free.meta.json"
    meta = _read_meta(meta_path)
    problems: list[str] = []
    cached_modified = _last_modified(meta.get("last_modified"))

    if _has_full_cache(dest, meta) and not _due(meta, clock):
        return _with_age(_cached_static(dest, cached_modified, None), problems)

    try:
        remote_modified = head_last_modified(PRIMARY_STATIC_URL)
    except RateLimited as exc:
        if _has_full_cache(dest, meta):
            _postpone(meta_path, meta, clock, exc.retry_after)
            notice = "gtfs.de rate limit. Using the saved timetable."
            problems.append(notice)
            return _with_age(_cached_static(dest, cached_modified, notice), problems)
        raise

    if _has_full_cache(dest, meta) and remote_modified is not None and cached_modified == remote_modified:
        _postpone(meta_path, meta, clock, STATIC_REFRESH_EVERY.total_seconds())
        return _with_age(_cached_static(dest, cached_modified, None), problems)

    if _has_full_cache(dest, meta) and remote_modified is None:
        _postpone(meta_path, meta, clock, FAILURE_BACKOFF.total_seconds())
        notice = "Could not check the timetable. Using the saved copy."
        problems.append(notice)
        return _with_age(_cached_static(dest, cached_modified, notice), problems)

    try:
        modified = _download(PRIMARY_STATIC_URL, dest) or remote_modified
        _write_meta(
            meta_path,
            {
                "url": PRIMARY_STATIC_URL,
                "last_modified": None if modified is None else modified.strftime("%a, %d %b %Y %H:%M:%S GMT"),
                "next_check": (clock + STATIC_REFRESH_EVERY).isoformat(),
            },
        )
        static = StaticFile(dest, PRIMARY_STATIC_URL, modified, False, False)
    except RateLimited as exc:
        if _has_full_cache(dest, meta):
            _postpone(meta_path, meta, clock, exc.retry_after)
            notice = "gtfs.de rate limit. Using the saved timetable."
            problems.append(notice)
            static = _cached_static(dest, cached_modified, notice)
        else:
            raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if _has_full_cache(dest, meta):
            _postpone(meta_path, meta, clock, FAILURE_BACKOFF.total_seconds())
            notice = "The timetable download failed. Using the saved copy."
            problems.append(notice)
            static = _cached_static(dest, cached_modified, notice)
        else:
            problems.append(f"full GTFS download failed ({exc}); falling back to fv_free.")
            fallback = cache_dir / "fv-free.zip"
            modified = _download(FALLBACK_STATIC_URL, fallback)
            static = StaticFile(fallback, FALLBACK_STATIC_URL, modified, False, True)
    return _with_age(static, problems)


def _with_age(static: StaticFile, problems: list[str]) -> tuple[StaticFile, list[str]]:
    if static.fallback:
        problems.append("static feed is fv_free (long-distance only). Regional and S-Bahn trip ids will not match.")
    if is_static_stale(static.last_modified):
        age = static_age(static.last_modified)
        hours = 0 if age is None else int(age.total_seconds() // 3600)
        problems.append(
            f"static Last-Modified is {hours}h old"
            + (f" ({static.last_modified:%Y-%m-%d %H:%M} UTC)." if static.last_modified else ".")
            + " The publisher describes the file as generated daily."
        )
    return static, problems


def fetch_realtime(cache_dir: Path, now: datetime | None = None) -> RealtimeFetch:
    """Download the delay feed. On failure, return the last saved copy."""
    clock = now or datetime.now(timezone.utc)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / "realtime.pb"
    meta_path = cache_dir / "realtime.meta.json"
    meta = _read_meta(meta_path)
    retry_until = _parse_time(meta.get("retry_until"))
    if retry_until is not None and clock < retry_until:
        return _cached_realtime(
            dest,
            meta,
            "gtfs.de rate limit. Showing the last good delay feed",
            (retry_until - clock).total_seconds(),
        )

    try:
        with _urlopen(_request(REALTIME_URL), timeout=90) as response:
            payload = response.read()
            modified = _last_modified(response.headers.get("Last-Modified"))
    except RateLimited as exc:
        _postpone_retry(meta_path, meta, clock, exc.retry_after)
        return _cached_realtime(dest, meta, "gtfs.de rate limit. Showing the last good delay feed", exc.retry_after)
    except (urllib.error.URLError, TimeoutError, OSError):
        return _cached_realtime(dest, meta, "The live delay feed did not load. Showing the last good data", None)

    _write_meta(
        meta_path,
        {
            "fetched_at": clock.isoformat(),
            "last_modified": None if modified is None else modified.isoformat(),
        },
    )
    dest.write_bytes(payload)
    return RealtimeFetch(payload, modified, clock, False, None, None)


def download_realtime(cache_dir: Path | None = None) -> tuple[bytes, datetime | None]:
    fetched = fetch_realtime(cache_dir or Path("data") / "cache")
    if fetched.payload is None:
        raise RuntimeError(fetched.banner or "realtime download failed")
    return fetched.payload, fetched.last_modified


def _cached_realtime(dest: Path, meta: dict, banner: str, retry_after: float | None) -> RealtimeFetch:
    fetched_at = _parse_time(meta.get("fetched_at"))
    modified = _parse_time(meta.get("last_modified"))
    if not dest.exists():
        if "rate limit" in banner:
            missing = "gtfs.de rate limit. No saved delay feed yet."
        else:
            missing = "The live delay feed did not load. No saved delay feed yet."
        return RealtimeFetch(None, modified, fetched_at, False, missing, retry_after)
    when = "" if fetched_at is None else " from " + fetched_at.strftime("%H:%M UTC")
    return RealtimeFetch(dest.read_bytes(), modified, fetched_at, True, banner + when + ".", retry_after)


def _postpone_retry(meta_path: Path, meta: dict, now: datetime, seconds: float) -> None:
    updated = dict(meta)
    updated["retry_until"] = (now + timedelta(seconds=seconds)).isoformat()
    _write_meta(meta_path, updated)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return _last_modified(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
