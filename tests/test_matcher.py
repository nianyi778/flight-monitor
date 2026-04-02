import unittest
from datetime import datetime, timedelta

from app.matcher import (
    _date_range,
    _in_time_window,
    _parse_hour,
    _flight_passes_filters,
    find_best_combinations,
)


class TestDateRange(unittest.TestCase):
    def test_both_direction_generates_before_and_after(self):
        dates = _date_range("2026-09-18", 2, "both")
        self.assertIn("2026-09-18", dates)
        self.assertIn("2026-09-16", dates)
        self.assertIn("2026-09-17", dates)
        self.assertIn("2026-09-19", dates)
        self.assertIn("2026-09-20", dates)
        self.assertEqual(len(dates), 5)

    def test_before_only(self):
        dates = _date_range("2026-09-18", 2, "before")
        self.assertIn("2026-09-18", dates)
        self.assertIn("2026-09-16", dates)
        self.assertIn("2026-09-17", dates)
        self.assertNotIn("2026-09-19", dates)

    def test_after_only(self):
        dates = _date_range("2026-09-18", 2, "after")
        self.assertIn("2026-09-18", dates)
        self.assertIn("2026-09-19", dates)
        self.assertIn("2026-09-20", dates)
        self.assertNotIn("2026-09-17", dates)

    def test_zero_flex(self):
        dates = _date_range("2026-09-18", 0, "both")
        self.assertEqual(dates, ["2026-09-18"])

    def test_no_duplicates(self):
        dates = _date_range("2026-09-18", 3, "both")
        self.assertEqual(len(dates), len(set(dates)))


class TestTimeWindow(unittest.TestCase):
    def test_normal_window(self):
        self.assertTrue(_in_time_window(20, 19, 23))
        self.assertFalse(_in_time_window(18, 19, 23))

    def test_cross_midnight_window(self):
        self.assertTrue(_in_time_window(23, 22, 2))
        self.assertTrue(_in_time_window(1, 22, 2))
        self.assertFalse(_in_time_window(10, 22, 2))

    def test_exact_boundaries(self):
        self.assertTrue(_in_time_window(19, 19, 23))
        self.assertTrue(_in_time_window(23, 19, 23))


class TestFlightPassesFilters(unittest.TestCase):
    def test_passes_with_no_filters(self):
        f = {"departure_time": "20:00", "arrival_time": "23:00", "stops": 0}
        self.assertTrue(_flight_passes_filters(f, None, None, None, None, None))

    def test_filtered_by_departure_window(self):
        f = {"departure_time": "08:00", "arrival_time": "11:00", "stops": 0}
        self.assertFalse(_flight_passes_filters(f, 19, 23, None, None, None))

    def test_filtered_by_max_stops(self):
        f = {"departure_time": "20:00", "arrival_time": "23:00", "stops": 2}
        self.assertFalse(_flight_passes_filters(f, None, None, None, None, 1))
        self.assertTrue(_flight_passes_filters(f, None, None, None, None, 2))

    def test_missing_time_passes_filter(self):
        f = {"stops": 0}
        self.assertTrue(_flight_passes_filters(f, 19, 23, None, None, None))


class TestDedupWithRoute(unittest.TestCase):
    def test_same_flight_different_routes_kept(self):
        results = {
            "outbound": [
                {
                    "flights": [
                        {
                            "airline": "9C",
                            "flight_no": "8515",
                            "departure_time": "20:00",
                            "arrival_time": "23:00",
                            "price_cny": 500,
                            "origin": "NRT",
                            "destination": "PVG",
                            "stops": 0,
                            "via": "",
                        },
                        {
                            "airline": "9C",
                            "flight_no": "8515",
                            "departure_time": "20:00",
                            "arrival_time": "23:00",
                            "price_cny": 600,
                            "origin": "HND",
                            "destination": "PVG",
                            "stops": 0,
                            "via": "",
                        },
                    ],
                    "source": "Kiwi",
                    "url": "test",
                    "flight_date": "2026-09-18",
                }
            ],
            "return": [],
        }
        trip = {
            "id": 1,
            "outbound_date": "2026-09-18",
            "budget": 2000,
            "trip_type": "one_way",
            "origin": "TYO",
            "destination": "PVG",
            "max_stops": None,
            "ob_depart_start": None,
            "ob_depart_end": None,
            "ob_arrive_start": None,
            "ob_arrive_end": None,
            "rt_depart_start": None,
            "rt_depart_end": None,
            "rt_arrive_start": None,
            "rt_arrive_end": None,
            "throwaway": False,
        }
        combos = find_best_combinations(results, trip)
        self.assertEqual(len(combos), 2)
        origins = {c["outbound"]["origin"] for c in combos}
        self.assertEqual(origins, {"NRT", "HND"})


class TestCartesianLimit(unittest.TestCase):
    def test_uses_top_15(self):
        ob_flights = [
            {
                "airline": f"A{i}",
                "flight_no": f"F{i}",
                "departure_time": "10:00",
                "arrival_time": "13:00",
                "price_cny": 100 + i,
                "origin": "NRT",
                "destination": "PVG",
                "stops": 0,
                "via": "",
            }
            for i in range(20)
        ]
        rt_flights = [
            {
                "airline": f"B{i}",
                "flight_no": f"R{i}",
                "departure_time": "10:00",
                "arrival_time": "13:00",
                "price_cny": 100 + i,
                "origin": "PVG",
                "destination": "NRT",
                "stops": 0,
                "via": "",
            }
            for i in range(20)
        ]
        results = {
            "outbound": [
                {
                    "flights": ob_flights,
                    "source": "Kiwi",
                    "url": "t",
                    "flight_date": "2026-09-18",
                }
            ],
            "return": [
                {
                    "flights": rt_flights,
                    "source": "Kiwi",
                    "url": "t",
                    "flight_date": "2026-09-28",
                }
            ],
        }
        trip = {
            "id": 1,
            "outbound_date": "2026-09-18",
            "return_date": "2026-09-28",
            "budget": 5000,
            "trip_type": "round_trip",
            "origin": "TYO",
            "destination": "PVG",
            "max_stops": None,
            "throwaway": False,
            "ob_depart_start": None,
            "ob_depart_end": None,
            "ob_arrive_start": None,
            "ob_arrive_end": None,
            "rt_depart_start": None,
            "rt_depart_end": None,
            "rt_arrive_start": None,
            "rt_arrive_end": None,
        }
        combos = find_best_combinations(results, trip)
        self.assertGreater(len(combos), 0)
        self.assertEqual(combos[0]["total"], 200)


if __name__ == "__main__":
    unittest.main()
