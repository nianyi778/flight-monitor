import unittest
import types
import sys
from datetime import timedelta
from unittest.mock import patch

sys.modules.setdefault("requests", types.SimpleNamespace())


class _FakeFastMCP:
    def __init__(self, *args, **kwargs):
        pass

    def tool(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator

    def resource(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator

    def custom_route(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator


sys.modules.setdefault("fastmcp", types.SimpleNamespace(FastMCP=_FakeFastMCP))
sys.modules.setdefault("starlette.requests", types.SimpleNamespace(Request=object))
sys.modules.setdefault("starlette.responses", types.SimpleNamespace(JSONResponse=object))

from app.bot import (
    _parse_flex_arg,
    _parse_window_arg,
    _validate_budget_value,
    _validate_date_pair,
)
from app.matcher import find_best_combinations
from app.mcp_server import get_metrics_history
from app.source_runtime import (
    ensure_runtime_state,
    finalize_check_metrics,
    force_source_cooldown,
    get_runtime_metrics,
    init_check_metrics,
    record_check_metric_event,
    source_in_cooldown,
)
from app.config import now_jst


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


class RuntimeControlTests(unittest.TestCase):
    def test_force_source_cooldown_marks_source_unavailable(self):
        state = {}
        ensure_runtime_state(state)
        now_dt = now_jst()
        force_source_cooldown(state, "ctrip_api", "captcha", now_dt, seconds=600)
        self.assertTrue(source_in_cooldown(state, "ctrip_api", now_dt))

    def test_runtime_metrics_accumulate_check_events(self):
        state = {}
        ensure_runtime_state(state)
        now_dt = now_jst()
        metrics = init_check_metrics(check_id=3, due_trips=2, total_searches=5, started_at=now_dt)

        record_check_metric_event(
            metrics,
            "ctrip_api",
            from_cache=False,
            status="ok",
            has_flights=True,
            request_mode="api",
        )
        record_check_metric_event(
            metrics,
            "google_api",
            from_cache=True,
            status="blocked",
            has_flights=False,
            request_mode="api",
        )

        finalize_check_metrics(state, metrics, now_dt + timedelta(seconds=2))
        runtime_metrics = get_runtime_metrics(state)

        self.assertEqual(runtime_metrics["totals"]["checks"], 1)
        self.assertEqual(runtime_metrics["totals"]["real_requests"], 1)
        self.assertEqual(runtime_metrics["totals"]["cache_hits"], 1)
        self.assertEqual(runtime_metrics["totals"]["blocked_results"], 1)
        self.assertEqual(runtime_metrics["totals"]["valid_results"], 1)
        self.assertEqual(runtime_metrics["recent_checks"][-1]["duration_ms"], 2000)


class FakeCursor:
    def __init__(self):
        self._result = []

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        if "GROUP BY DATE(check_time) ORDER BY day DESC" in normalized:
            self._result = [
                ("2026-03-20", 4, 3, 12.5, 980, 1120.0),
                ("2026-03-19", 2, 1, 6.0, 1200, 1250.0),
            ]
        elif "GROUP BY DATE(check_time), source" in normalized:
            self._result = [
                ("2026-03-20", "ctrip_api", 14, 3, 980, 1088.0),
                ("2026-03-20", "google_api", 9, 2, 1010, 1115.5),
            ]
        elif "GROUP BY DATE(check_time), direction" in normalized:
            self._result = [
                ("2026-03-20", "outbound", 12, 2, 980, 1090.0),
                ("2026-03-20", "return", 11, 2, 999, 1108.0),
            ]
        else:
            self._result = [(6, 4, 10.3333, 980, 1163.3)]

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0]


class FakeDB:
    def cursor(self):
        return FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class MCPMetricsHistoryTests(unittest.TestCase):
    @patch("app.mcp_server.get_db", return_value=FakeDB())
    def test_get_metrics_history_returns_tidb_aggregates(self, _mock_get_db):
        result = get_metrics_history(days=7, trip_id=12)

        self.assertEqual(result["range"]["days"], 7)
        self.assertEqual(result["range"]["trip_id"], 12)
        self.assertEqual(result["summary"]["checks"], 6)
        self.assertEqual(result["summary"]["checks_with_result"], 4)
        self.assertAlmostEqual(result["summary"]["result_rate"], 0.6667, places=4)
        self.assertEqual(result["daily_checks"][0]["day"], "2026-03-20")
        self.assertEqual(result["source_coverage"][0]["source"], "ctrip_api")
        self.assertEqual(result["direction_coverage"][0]["direction"], "outbound")


class DbSaveRegressionTests(unittest.TestCase):
    """验证 save_to_db 的行级隔离、列截断和春秋写入行为"""

    def _make_trip(self):
        return {
            "id": 1,
            "outbound_date": "2026-09-18",
            "return_date": "2026-09-28",
            "budget": 3000,
        }

    def _make_results(self, flights_ob, flights_rt):
        return {
            "outbound": [{"source": "test_ob", "flights": flights_ob}],
            "return": [{"source": "test_rt", "flights": flights_rt}],
        }

    def _run_save(self, results, trip=None):
        """执行 save_to_db，收集所有 INSERT 参数，返回 inserted_rows"""
        from app.db import save_to_db
        trip = trip or self._make_trip()
        inserted_rows = []

        class FakeCur:
            def execute(self_, query, params=None):
                if params and "INSERT INTO flight_prices" in query:
                    inserted_rows.append(params)

        class FakeConn:
            def cursor(self_): return FakeCur()
            def commit(self_): pass
            def rollback(self_): pass
            def close(self_): pass
            def __enter__(self_): return self_
            def __exit__(self_, *a): pass

        with patch("app.db.get_db", return_value=FakeConn()):
            save_to_db(results, [], trip)
        return inserted_rows

    def test_normal_flight_is_inserted(self):
        flights = [{"airline": "ANA", "flight_no": "NH101", "departure_time": "20:00",
                    "arrival_time": "22:00", "origin": "NRT", "destination": "PVG",
                    "price_cny": 1500, "original_price": 210, "original_currency": "USD", "stops": 0}]
        rows = self._run_save(self._make_results(flights, flights))
        self.assertEqual(len(rows), 2)
        # flight_no 字段在 params 的索引 5
        self.assertEqual(rows[0][5], "NH101")

    def test_uuid_flight_no_is_truncated_to_20_chars(self):
        """Critical #1 回归：UUID 不应再触发 VARCHAR(20) 溢出"""
        uuid_flight_no = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
        flights = [{"airline": "X", "flight_no": uuid_flight_no, "departure_time": "20:00",
                    "arrival_time": "22:00", "origin": "NRT", "destination": "PVG",
                    "price_cny": 999, "original_price": None, "original_currency": "CNY", "stops": 0}]
        rows = self._run_save(self._make_results(flights, []))
        self.assertEqual(len(rows), 1)
        stored_flight_no = rows[0][5]
        self.assertLessEqual(len(stored_flight_no), 20)

    def test_long_airline_name_is_truncated_to_50_chars(self):
        long_airline = "A" * 80
        flights = [{"airline": long_airline, "flight_no": "AA1", "departure_time": "20:00",
                    "arrival_time": "22:00", "origin": "NRT", "destination": "PVG",
                    "price_cny": 999, "original_price": None, "original_currency": "CNY", "stops": 0}]
        rows = self._run_save(self._make_results(flights, []))
        stored_airline = rows[0][4]
        self.assertLessEqual(len(stored_airline), 50)

    def test_long_source_is_truncated_to_30_chars(self):
        long_source = "S" * 60
        results = {
            "outbound": [{"source": long_source, "flights": [
                {"airline": "X", "flight_no": "X1", "departure_time": "20:00",
                 "arrival_time": "22:00", "origin": "NRT", "destination": "PVG",
                 "price_cny": 999, "original_price": None, "original_currency": "CNY", "stops": 0}
            ]}],
            "return": [],
        }
        rows = self._run_save(results)
        stored_source = rows[0][3]
        self.assertLessEqual(len(stored_source), 30)

    def test_bad_row_does_not_abort_batch(self):
        """Critical #2 回归：一行抛异常，其余行仍应写入"""
        from app.db import save_to_db
        trip = self._make_trip()
        good_flight = {"airline": "ANA", "flight_no": "NH101", "departure_time": "20:00",
                       "arrival_time": "22:00", "origin": "NRT", "destination": "PVG",
                       "price_cny": 1500, "original_price": None, "original_currency": "CNY", "stops": 0}
        results = {
            "outbound": [{"source": "s", "flights": [good_flight, good_flight]}],
            "return": [],
        }

        call_count = [0]

        class FakeCurWithError:
            def execute(self_, query, params=None):
                if "INSERT INTO flight_prices" in query:
                    call_count[0] += 1
                    if call_count[0] == 1:
                        raise Exception("simulated DB error on row 1")

        class FakeConnWithError:
            def cursor(self_): return FakeCurWithError()
            def commit(self_): pass
            def rollback(self_): pass
            def close(self_): pass
            def __enter__(self_): return self_
            def __exit__(self_, *a): pass

        with patch("app.db.get_db", return_value=FakeConnWithError()):
            with self.assertLogs("flight_monitor", level="WARNING"):
                save_to_db(results, [], trip)

        # 第 2 行应仍被处理（call_count == 2）
        self.assertEqual(call_count[0], 2)

    def test_letsfg_extract_segment_no_uuid_fallback(self):
        """Critical #1 根本修复验证：_extract_segment 不应把 id 当 flight_no"""
        from app.letsfg_api import _extract_segment
        offer = {
            "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "airline": "ANA",
            "departure_time": "20:00",
            "arrival_time": "22:00",
        }
        _, flight_no, _, _ = _extract_segment(offer)
        self.assertNotEqual(flight_no, "f47ac10b-58cc-4372-a567-0e02b2c3d479")
        self.assertEqual(flight_no, "")


if __name__ == "__main__":
    unittest.main()
