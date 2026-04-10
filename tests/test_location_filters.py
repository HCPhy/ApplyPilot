from __future__ import annotations

import unittest

from applypilot.discovery.location_filters import US_WIDE_SENTINEL, location_ok


class LocationFiltersTest(unittest.TestCase):
    def test_us_wide_accepts_positive_us_signals(self) -> None:
        accept = [US_WIDE_SENTINEL]
        reject: list[str] = []

        self.assertTrue(location_ok("Chicago, IL", accept, reject))
        self.assertTrue(location_ok("US, CA, Santa Clara", accept, reject))
        self.assertTrue(location_ok("San Jose, California, United States of America", accept, reject))
        self.assertTrue(location_ok("USA.VA.Reston", accept, reject))
        self.assertTrue(location_ok("California - San Francisco", accept, reject))
        self.assertTrue(location_ok("Los Gatos", accept, reject))

    def test_us_wide_rejects_non_us_and_ambiguous_locations(self) -> None:
        accept = [US_WIDE_SENTINEL]
        reject: list[str] = []

        self.assertFalse(location_ok("Toronto, ON, CAN", accept, reject))
        self.assertFalse(location_ok("London", accept, reject))
        self.assertFalse(location_ok("Bengaluru Millenia", accept, reject))
        self.assertFalse(location_ok("San Jose, Costa Rica", accept, reject))
        self.assertFalse(location_ok("Remote", accept, reject))
        self.assertFalse(location_ok("2 Locations", accept, reject))
        self.assertFalse(location_ok(None, accept, reject))

    def test_custom_accept_patterns_keep_existing_behavior(self) -> None:
        accept = ["Boston"]
        reject = ["Canada"]

        self.assertTrue(location_ok("Boston, MA", accept, reject))
        self.assertTrue(location_ok("Remote", accept, reject))
        self.assertFalse(location_ok("Toronto, ON, Canada", accept, reject))


if __name__ == "__main__":
    unittest.main()
