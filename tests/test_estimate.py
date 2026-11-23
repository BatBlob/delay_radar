import unittest
from datetime import date, datetime

from delay_radar.clock import BERLIN, parse_gtfs_time
from delay_radar.estimate import estimate
from delay_radar.join import dedupe, place
from delay_radar.model import Call, ParsedFeed, RealtimeTrip, Status, StopPatch, TripInfo

DAY = date(2026, 11, 23)


def at(hhmmss: str, day: date = DAY) -> datetime:
    parsed = parse_gtfs_time(day, hhmmss)
    assert parsed is not None
    return parsed


def call(seq: int, name: str, arr: str, dep: str, lat: float = 52.0, lon: float = 13.0, day: date = DAY) -> Call:
    return Call(seq, f"s{seq}", name, lat + seq * 0.1, lon + seq * 0.1, at(arr, day), at(dep, day))


def patch(seq: int, arr=None, dep=None, stop_id=None, skipped=False, arrival_time=None) -> StopPatch:
    return StopPatch(seq, stop_id, arr, dep, arrival_time, None, skipped)


class ClockTests(unittest.TestCase):
    def test_hours_past_24_roll_into_the_next_day(self):
        self.assertEqual(parse_gtfs_time(date(2026, 11, 22), "25:10:00"), at("01:10:00"))
        self.assertEqual(parse_gtfs_time(date(2026, 11, 22), "49:00:00"), at("01:00:00", date(2026, 11, 24)))

    def test_blank_time_is_missing(self):
        self.assertIsNone(parse_gtfs_time(DAY, ""))


class EstimateTests(unittest.TestCase):
    def test_unstarted_trip_has_no_position(self):
        calls = [call(0, "A", "10:00:00", "10:00:00"), call(1, "B", "11:00:00", "11:00:00")]
        position = estimate(
            trip_id="t", route_name="ICE 1", start_date="20261123",
            calls=calls, patches=[], now=at("09:30:00"),
        )
        self.assertEqual(position.status, Status.NOT_STARTED)
        self.assertIsNone(position.lat)

    def test_dwelling_uses_the_stop_coordinate(self):
        calls = [call(0, "A", "10:00:00", "10:10:00"), call(1, "B", "11:00:00", "11:00:00")]
        position = estimate(
            trip_id="t", route_name="ICE 1", start_date="20261123",
            calls=calls, patches=[], now=at("10:05:00"),
        )
        self.assertEqual(position.status, Status.DWELLING)
        self.assertEqual(position.stop_name, "A")
        self.assertEqual(position.lat, calls[0].lat)

    def test_delay_moves_the_train_back_a_leg(self):
        calls = [call(0, "A", "10:00:00", "10:00:00"), call(1, "B", "10:20:00", "10:20:00"), call(2, "C", "11:00:00", "11:00:00")]
        # Timetable at 10:30 would already be past B. A 20 minute delay keeps it between A and B.
        patches = [patch(0, dep=1200), patch(1, arr=1200)]
        position = estimate(
            trip_id="t", route_name="ICE 1", start_date="20261123",
            calls=calls, patches=patches, now=at("10:30:00"),
        )
        self.assertEqual(position.status, Status.IN_MOTION)
        self.assertEqual((position.from_name, position.to_name), ("A", "B"))
        self.assertEqual(position.upcoming, ("B", "C"))
        self.assertAlmostEqual(position.fraction, 0.5)
        self.assertEqual(position.delay_seconds, 1200)
        self.assertTrue(position.delay_explicit)

    def test_delay_carries_forward_and_does_not_apply_backward(self):
        calls = [
            call(0, "A", "10:00:00", "10:00:00"),
            call(1, "B", "11:00:00", "11:00:00"),
            call(2, "C", "12:00:00", "12:00:00"),
        ]
        patches = [patch(0, dep=600)]
        position = estimate(
            trip_id="t", route_name="RE 1", start_date="20261123",
            calls=calls, patches=patches, now=at("11:30:00"),
        )
        self.assertEqual((position.from_name, position.to_name), ("B", "C"))
        self.assertEqual(position.delay_seconds, 600)
        self.assertFalse(position.delay_explicit)
        # 10:10 is after the scheduled departure, and A's delay must not be invented from B.
        early = estimate(
            trip_id="t", route_name="RE 1", start_date="20261123",
            calls=calls, patches=[patch(1, arr=1200)], now=at("10:10:00"),
        )
        self.assertEqual(early.status, Status.IN_MOTION)
        self.assertEqual(early.from_name, "A")

    def test_sequence_zero_is_a_real_stop(self):
        calls = [call(0, "A", "10:00:00", "10:00:00"), call(1, "B", "11:00:00", "11:00:00")]
        position = estimate(
            trip_id="t", route_name="S1", start_date="20261123",
            calls=calls, patches=[patch(0, dep=3600)], now=at("10:30:00"),
        )
        # Arrival stays 10:00 and the delayed departure is 11:00, so the train is still at A.
        self.assertEqual(position.status, Status.DWELLING)
        self.assertEqual(position.stop_name, "A")

    def test_skipped_stop_is_removed_from_the_chain(self):
        calls = [
            call(0, "A", "10:00:00", "10:00:00"),
            call(1, "B", "10:20:00", "10:20:00"),
            call(2, "C", "11:00:00", "11:00:00"),
        ]
        position = estimate(
            trip_id="t", route_name="RB 1", start_date="20261123",
            calls=calls, patches=[patch(1, skipped=True)], now=at("10:30:00"),
        )
        self.assertEqual((position.from_name, position.to_name), ("A", "C"))

    def test_absolute_time_becomes_a_delay(self):
        calls = [call(0, "A", "10:00:00", "10:00:00"), call(1, "B", "11:00:00", "11:00:00")]
        when = int((at("11:30:00")).timestamp())
        position = estimate(
            trip_id="t", route_name="ICE 2", start_date="20261123",
            calls=calls,
            patches=[StopPatch(1, None, None, None, when, None, False)],
            now=at("10:15:00"),
        )
        self.assertEqual(position.delay_seconds, 1800)
        self.assertTrue(position.delay_explicit)

    def test_canceled_and_too_few_stops(self):
        calls = [call(0, "A", "10:00:00", "10:00:00")]
        self.assertEqual(
            estimate(trip_id="t", route_name="ICE", start_date="20261123", calls=calls, patches=[], now=at("10:00:00"), canceled=True).status,
            Status.CANCELED,
        )
        self.assertEqual(
            estimate(trip_id="t", route_name="ICE", start_date="20261123", calls=calls, patches=[], now=at("10:00:00")).status,
            Status.TOO_FEW_STOPS,
        )

    def test_service_day_past_midnight(self):
        yesterday = date(2026, 11, 22)
        calls = [
            call(0, "A", "23:00:00", "23:00:00", day=yesterday),
            call(1, "B", "25:00:00", "25:00:00", day=yesterday),
        ]
        position = estimate(
            trip_id="t", route_name="NJ", start_date="20261122",
            calls=calls, patches=[], now=at("00:00:00"),
        )
        self.assertEqual(position.status, Status.IN_MOTION)
        self.assertAlmostEqual(position.fraction, 0.5)


class JoinTests(unittest.TestCase):
    def test_duplicate_trip_keeps_the_richer_update_once(self):
        kept, extras = dedupe(
            [
                RealtimeTrip("t", "20261123", "", False, ()),
                RealtimeTrip("t", "20261123", "", False, (patch(0, dep=60),)),
            ]
        )
        self.assertEqual(extras, 1)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(kept[0].stops), 1)

    def test_place_counts_match_rate_and_one_moving_train(self):
        calls = [call(0, "A", "10:00:00", "10:00:00"), call(1, "B", "11:00:00", "11:00:00")]
        info = TripInfo("t", "ICE 1", 2, "svc", True)
        bus = TripInfo("bus", "2", 3, "svc", False)

        class Fake:
            def lookup(self, ids):
                return {trip_id: info if trip_id == "t" else bus for trip_id in ids if trip_id in {"t", "bus"}}

            def calls_for(self, instances):
                return {("t", "20261123"): calls}

            def service_runs(self, service_id, day):
                return True

        feed = ParsedFeed(
            header_time=at("10:30:00"),
            trips=(
                RealtimeTrip("t", "20261123", "", False, ()),
                RealtimeTrip("t", "20261123", "", False, ()),
                RealtimeTrip("bus", "20261123", "", False, ()),
                RealtimeTrip("missing", "20261123", "", False, ()),
            ),
            vehicle_count=0,
            alert_count=0,
        )
        summary = place(Fake(), feed, at("10:30:00"))
        self.assertEqual(summary.matched, 3)
        self.assertEqual(summary.trip_updates, 4)
        self.assertEqual(len(summary.in_motion), 1)
        self.assertEqual(summary.duplicate_extras, 1)
        self.assertEqual(summary.not_rail, 1)


if __name__ == "__main__":
    unittest.main()
