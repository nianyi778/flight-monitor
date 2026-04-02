import unittest

from app.matcher import _effective_flex


class TestEffectiveFlex(unittest.TestCase):
    def test_within_30_days(self):
        trip = {"outbound_flex": 3, "return_flex": 2}
        ob, rt = _effective_flex(trip, 15)
        self.assertEqual(ob, 3)
        self.assertEqual(rt, 3)

    def test_30_to_90_days(self):
        trip = {"outbound_flex": 3, "return_flex": 2}
        ob, rt = _effective_flex(trip, 60)
        self.assertEqual(ob, 3)
        self.assertEqual(rt, 2)

    def test_over_90_days(self):
        trip = {"outbound_flex": 3, "return_flex": 2}
        ob, rt = _effective_flex(trip, 100)
        self.assertEqual(ob, 0)
        self.assertEqual(rt, 0)

    def test_defaults_when_none(self):
        trip = {"outbound_flex": None, "return_flex": None}
        ob, rt = _effective_flex(trip, 15)
        self.assertEqual(ob, 0)
        self.assertEqual(rt, 2)


class TestParseHour(unittest.TestCase):
    def test_valid_time(self):
        from app.matcher import _parse_hour

        self.assertEqual(_parse_hour("20:30"), 20)
        self.assertEqual(_parse_hour("08:00"), 8)

    def test_invalid_returns_none(self):
        from app.matcher import _parse_hour

        self.assertIsNone(_parse_hour(""))
        self.assertIsNone(_parse_hour(None))
        self.assertIsNone(_parse_hour("abc"))


if __name__ == "__main__":
    unittest.main()
