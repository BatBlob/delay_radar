import unittest

from delay_radar.estimate import estimate
from delay_radar.model import Status
from delay_radar.railgraph import astar, contract, point_along, routing_graph
from delay_radar.tracks import TrackPaths
from tests.test_estimate import DAY, at, call


def line(a, b):
    return [(a[0], a[1]), (b[0], b[1])]


class RailGraphTests(unittest.TestCase):
    def test_point_along_follows_the_bend(self):
        # 2 km east, then 2 km north. Halfway is the corner, not the diagonal.
        coords = [(13.0, 52.0), (13.028, 52.0), (13.028, 52.018)]
        lat, lon = point_along(coords, 0.5)
        self.assertAlmostEqual(lon, 13.028, places=3)
        self.assertAlmostEqual(lat, 52.0, places=3)

    def test_contract_collapses_a_straight_chain(self):
        raw = {
            1: [(2, line((52.0, 13.0), (52.0, 13.01)))],
            2: [(1, line((52.0, 13.01), (52.0, 13.0))), (3, line((52.0, 13.01), (52.0, 13.02)))],
            3: [(2, line((52.0, 13.02), (52.0, 13.01))), (4, line((52.0, 13.02), (52.1, 13.02)))],
            4: [(3, line((52.1, 13.02), (52.0, 13.02)))],
        }
        edges = contract(raw)
        self.assertEqual(len(edges), 1)
        start, end, points = edges[0]
        self.assertEqual({start, end}, {1, 4})
        self.assertGreaterEqual(len(points), 3)

    def test_route_between_snapped_stops(self):
        edges = [(1, 2, [(52.0, 13.0), (52.0, 13.02), (52.05, 13.02)])]
        snaps = {
            "a": (0, 0.0, 52.0, 13.0),
            "b": (0, 8000.0, 52.05, 13.02),
        }
        adj, coords = routing_graph(edges, snaps)
        path = astar(adj, coords, ("s", "a"), ("s", "b"))
        self.assertIsNotNone(path)
        self.assertEqual(path[0], ("s", "a"))
        self.assertEqual(path[-1], ("s", "b"))

    def test_estimator_uses_the_cached_path(self):
        calls = [call(0, "A", "10:00:00", "10:00:00"), call(1, "B", "11:00:00", "11:00:00")]
        paths = TrackPaths({("s0", "s1"): [(13.0, 52.0), (13.2, 52.0), (13.2, 52.2)]})
        placed = estimate(
            trip_id="t",
            route_name="RE",
            start_date=DAY.isoformat().replace("-", ""),
            calls=calls,
            patches=[],
            now=at("10:30:00"),
            paths=paths,
        )
        self.assertEqual(placed.status, Status.IN_MOTION)
        self.assertFalse(placed.track_fallback)
        self.assertAlmostEqual(placed.lon, 13.2, places=2)
        self.assertGreater(placed.lat, 52.02)
        self.assertLess(placed.lat, 52.18)

    def test_missing_path_stays_on_the_straight_line(self):
        calls = [call(0, "A", "10:00:00", "10:00:00"), call(1, "B", "11:00:00", "11:00:00")]
        straight = estimate(
            trip_id="t",
            route_name="RE",
            start_date="20261123",
            calls=calls,
            patches=[],
            now=at("10:30:00"),
        )
        paths = TrackPaths({})
        placed = estimate(
            trip_id="t",
            route_name="RE",
            start_date="20261123",
            calls=calls,
            patches=[],
            now=at("10:30:00"),
            paths=paths,
        )
        self.assertTrue(placed.track_fallback)
        self.assertAlmostEqual(placed.lat, straight.lat)
        self.assertAlmostEqual(placed.lon, straight.lon)


if __name__ == "__main__":
    unittest.main()
