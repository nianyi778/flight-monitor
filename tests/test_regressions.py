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
from app.analyzer import classify_screenshot_page, diagnose_failure_context
from app.matcher import find_best_combinations
from app.mcp_server import get_metrics_history
from app.source_runtime import (
    browser_skip_active,
    ensure_runtime_state,
    finalize_check_metrics,
    force_source_cooldown,
    get_runtime_metrics,
    init_check_metrics,
    mark_skip_browser_until,
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


class AnalyzerAssistTests(unittest.TestCase):
    def test_page_classifier_uses_heuristic_for_login_wall(self):
        result = classify_screenshot_page({
            "name": "携程_NRT_PVG",
            "title": "请先登录继续查看价格",
            "body_text": "login required",
        })
        self.assertEqual(result["page_state"], "login_wall")

    def test_failure_diagnoser_recommends_cooldown_for_waf(self):
        diagnosis = diagnose_failure_context({
            "status": "blocked",
            "block_reason": "waf",
            "error": "Access denied by WAF",
            "request_mode": "browser",
        })
        self.assertEqual(diagnosis["action"], "cooldown")


class RuntimeControlTests(unittest.TestCase):
    def test_force_source_cooldown_marks_source_unavailable(self):
        state = {}
        ensure_runtime_state(state)
        now_dt = now_jst()
        force_source_cooldown(state, "browser_fallback", "captcha", now_dt, seconds=600)
        self.assertTrue(source_in_cooldown(state, "browser_fallback", now_dt))

    def test_mark_skip_browser_until_activates_guard(self):
        state = {}
        ensure_runtime_state(state)
        now_dt = now_jst()
        mark_skip_browser_until(state, now_dt, 300)
        self.assertTrue(browser_skip_active(state, now_dt))

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


if __name__ == "__main__":
    unittest.main()
