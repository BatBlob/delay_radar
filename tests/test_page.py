import unittest

from delay_radar.web import render_page


class PageTests(unittest.TestCase):
    def test_local_page_uses_the_site_root(self):
        html = render_page("")
        self.assertIn('<base href="/">', html)
        self.assertIn('EventSource("api/stream")', html)
        self.assertNotIn('"/api/stream"', html)

    def test_public_page_keeps_the_subpath(self):
        html = render_page("/delay_radar")
        self.assertIn('<base href="/delay_radar/">', html)
        self.assertIn('fetch("api/positions")', html)


if __name__ == "__main__":
    unittest.main()
