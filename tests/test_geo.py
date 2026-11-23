import unittest

from delay_radar.geo import delay_band, feature, feature_collection
from delay_radar.model import Position, Status


def position(**kwargs) -> Position:
    base = dict(
        status=Status.IN_MOTION,
        trip_id="t1",
        route_name="ICE 42",
        start_date="20261123",
        from_name="Essen Hbf",
        to_name="Bochum Hbf",
        lat=51.45,
        lon=7.01,
        delay_seconds=16 * 60,
        delay_explicit=True,
        upcoming=("Bochum Hbf", "Dortmund Hbf"),
    )
    base.update(kwargs)
    return Position(**base)


class GeoTests(unittest.TestCase):
    def test_delay_bands(self):
        self.assertEqual(delay_band(0), "on_time")
        self.assertEqual(delay_band(-120), "on_time")
        self.assertEqual(delay_band(5 * 60), "on_time")
        self.assertEqual(delay_band(6 * 60), "minor")
        self.assertEqual(delay_band(15 * 60), "minor")
        self.assertEqual(delay_band(16 * 60), "severe")
        self.assertEqual(delay_band(None), "unknown")

    def test_feature_is_a_point_with_the_next_stop(self):
        item = feature(position())
        self.assertEqual(item["geometry"]["coordinates"], [7.01, 51.45])
        self.assertEqual(item["properties"]["name"], "ICE 42")
        self.assertEqual(item["properties"]["next_stop"], "Bochum Hbf")
        self.assertEqual(item["properties"]["band"], "severe")
        self.assertEqual(item["properties"]["delay_min"], 16)
        self.assertEqual(item["properties"]["upcoming"], ["Bochum Hbf", "Dortmund Hbf"])

    def test_missing_coordinates_are_left_off_the_map(self):
        collection = feature_collection([position(lat=None, lon=None), position()])
        self.assertEqual(len(collection["features"]), 1)


if __name__ == "__main__":
    unittest.main()
