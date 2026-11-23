import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from delay_radar.board import sleep_seconds
from delay_radar.feeds import (
    PRIMARY_STATIC_URL,
    FAILURE_BACKOFF,
    ensure_static,
    fetch_realtime,
)


def http_error(code: int, retry_after: str) -> urllib.error.HTTPError:
    headers = Message()
    headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://example.test", code, "no", headers, BytesIO(b""))


class FeedCacheTests(unittest.TestCase):
    def test_static_check_waits_a_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "germany-free.zip").write_bytes(b"PK")
            later = datetime.now(timezone.utc) + timedelta(hours=2)
            (cache / "germany-free.meta.json").write_text(
                json.dumps(
                    {
                        "url": PRIMARY_STATIC_URL,
                        "last_modified": "Sat, 21 Nov 2026 09:42:12 GMT",
                        "next_check": later.isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            with patch("delay_radar.feeds.urllib.request.urlopen") as urlopen:
                static, _problems = ensure_static(cache)
            urlopen.assert_not_called()
            self.assertTrue(static.from_cache)
            self.assertIsNone(static.notice)

    def test_static_download_failure_keeps_the_saved_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "germany-free.zip").write_bytes(b"PK")
            (cache / "germany-free.meta.json").write_text(
                json.dumps({"url": PRIMARY_STATIC_URL, "last_modified": "Sat, 21 Nov 2026 09:42:12 GMT"}),
                encoding="utf-8",
            )
            with patch("delay_radar.feeds.urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
                static, problems = ensure_static(cache)
            self.assertTrue(static.from_cache)
            self.assertEqual(static.notice, "Could not check the timetable. Using the saved copy.")
            self.assertIn(static.notice, problems)
            meta = json.loads((cache / "germany-free.meta.json").read_text(encoding="utf-8"))
            nxt = datetime.fromisoformat(meta["next_check"])
            self.assertLess(nxt - datetime.now(timezone.utc), FAILURE_BACKOFF + timedelta(minutes=1))

    def test_realtime_failure_uses_the_saved_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "realtime.pb").write_bytes(b"saved")
            fetched_at = datetime(2026, 11, 23, 5, 44, tzinfo=timezone.utc)
            (cache / "realtime.meta.json").write_text(
                json.dumps({"fetched_at": fetched_at.isoformat()}),
                encoding="utf-8",
            )
            with patch("delay_radar.feeds.urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
                fetched = fetch_realtime(cache)
            self.assertEqual(fetched.payload, b"saved")
            self.assertTrue(fetched.from_cache)
            self.assertIn("05:44 UTC", fetched.banner)

    def test_rate_limit_is_remembered(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "realtime.pb").write_bytes(b"saved")
            with patch("delay_radar.feeds.urllib.request.urlopen", side_effect=http_error(429, "90")) as first:
                limited = fetch_realtime(cache)
            first.assert_called_once()
            self.assertEqual(limited.payload, b"saved")
            self.assertIn("rate limit", limited.banner)
            self.assertGreaterEqual(limited.retry_after, 90)
            with patch("delay_radar.feeds.urllib.request.urlopen") as second:
                again = fetch_realtime(cache)
            second.assert_not_called()
            self.assertEqual(again.payload, b"saved")

    def test_rate_limit_without_a_saved_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("delay_radar.feeds.urllib.request.urlopen", side_effect=http_error(429, "30")):
                fetched = fetch_realtime(Path(tmp))
            self.assertIsNone(fetched.payload)
            self.assertIn("No saved delay feed yet", fetched.banner)

    def test_update_button_does_not_fetch_twice_in_a_row(self):
        from delay_radar.board import Board

        board = Board(Path("."))
        calls = []
        board.refresh = lambda: calls.append(1)
        board.request_refresh()
        board.request_refresh()
        self.assertEqual(calls, [1])

    def test_sleep_backs_off_after_a_rate_limit(self):
        self.assertEqual(sleep_seconds(None), 180)
        self.assertEqual(sleep_seconds(90), 180)
        self.assertEqual(sleep_seconds(5), 180)
        self.assertEqual(sleep_seconds(240), 240)
