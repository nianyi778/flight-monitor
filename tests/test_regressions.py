import unittest
import types
import sys

sys.modules.setdefault("requests", types.SimpleNamespace())

from app.bot import (
    _parse_flex_arg,
    _parse_window_arg,
    _validate_budget_value,
    _validate_date_pair,
)
from app.matcher import find_best_combinations


class MatcherRegressionTests(unittest.TestCase):
    def test_time_windows_filter_out_invalid_flights(self):
        trip = {
            "outbound_date": "2026-09-18",
            "return_date": "2026-09-28",
            "budget": 1500,
            "depart_after": 19,
            "depart_before": 23,
            "arrive_after": 0,
            "arrive_before": 6,
        }
        results = {
            "outbound": [{
                "source": "test-ob",
                "url": "ob",
                "flight_date": "2026-09-18",
                "flights": [
                    {"airline": "TooEarly", "departure_time": "18:30", "arrival_time": "21:00", "price_cny": 100, "origin": "NRT", "destination": "PVG"},
                    {"airline": "ValidOB", "departure_time": "20:30", "arrival_time": "23:10", "price_cny": 200, "origin": "NRT", "destination": "PVG"},
                    {"airline": "TooLate", "departure_time": "17:30", "arrival_time": "20:00", "price_cny": 50, "origin": "NRT", "destination": "PVG"},
                ],
            }],
            "return": [{
                "source": "test-rt",
                "url": "rt",
                "flight_date": "2026-09-28",
                "flights": [
                    {"airline": "BadRT", "departure_time": "10:00", "arrival_time": "08:30", "price_cny": 100, "origin": "PVG", "destination": "NRT"},
                    {"airline": "ValidRT", "departure_time": "12:00", "arrival_time": "05:45", "price_cny": 300, "origin": "PVG", "destination": "NRT"},
                ],
            }],
        }

        combos = find_best_combinations(results, trip)

        self.assertEqual(len(combos), 1)
        self.assertEqual(combos[0]["outbound"]["airline"], "ValidOB")
        self.assertEqual(combos[0]["return"]["airline"], "ValidRT")
        self.assertEqual(combos[0]["total"], 500)


class BotValidationTests(unittest.TestCase):
    def test_budget_validation_rejects_out_of_range(self):
        value, error = _validate_budget_value("99")
        self.assertIsNone(value)
        self.assertIn("预算范围", error)

    def test_date_validation_rejects_past_departure(self):
        pair, error = _validate_date_pair("2026-03-18", "2026-03-25")
        self.assertIsNone(pair)
        self.assertIn("已过期", error)

    def test_window_validation_rejects_reversed_range(self):
        window, error = _parse_window_arg("去23-19", "去", "去程时间")
        self.assertIsNone(window)
        self.assertIn("起始应小于等于结束", error)

    def test_flex_validation_rejects_out_of_range(self):
        value, error = _parse_flex_arg("回8", "回", "回程弹性")
        self.assertIsNone(value)
        self.assertIn("范围 0-7", error)


if __name__ == "__main__":
    unittest.main()
