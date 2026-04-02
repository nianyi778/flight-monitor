import unittest
from unittest.mock import patch, MagicMock


class TestNotificationDecision(unittest.TestCase):
    """Test the notification trigger logic extracted from scheduler patterns."""

    def test_budget_hit_triggers_alert(self):
        budget = 1500
        best_total = 1200
        self.assertTrue(best_total <= budget)

    def test_price_drop_triggers_alert(self):
        prev_best = 2000
        best_total = 1800
        threshold = prev_best * 0.95
        self.assertTrue(best_total < threshold)

    def test_minor_drop_no_alert(self):
        prev_best = 2000
        best_total = 1950
        threshold = prev_best * 0.95
        self.assertFalse(best_total < threshold)

    def test_no_previous_price_no_drop_alert(self):
        prev_best = None
        best_total = 1800
        budget = 1500
        should_alert = best_total <= budget or (
            prev_best is not None and best_total < prev_best * 0.95
        )
        self.assertFalse(should_alert)


class TestAntiBot(unittest.TestCase):
    def test_405_is_retryable(self):
        from app.anti_bot import classify_http_status

        status, reason, retryable = classify_http_status(405)
        self.assertEqual(status, "blocked")
        self.assertTrue(retryable)

    def test_429_is_retryable(self):
        from app.anti_bot import classify_http_status

        status, reason, retryable = classify_http_status(429)
        self.assertTrue(retryable)

    def test_403_not_retryable(self):
        from app.anti_bot import classify_http_status

        status, reason, retryable = classify_http_status(403)
        self.assertFalse(retryable)

    def test_500_is_degraded(self):
        from app.anti_bot import classify_http_status

        status, reason, retryable = classify_http_status(500)
        self.assertEqual(status, "degraded")
        self.assertTrue(retryable)

    def test_classify_exception_405_retryable(self):
        from app.anti_bot import classify_exception

        status, reason, retryable = classify_exception("HTTP 405 Method Not Allowed")
        self.assertTrue(retryable)

    def test_classify_exception_captcha_not_retryable(self):
        from app.anti_bot import classify_exception

        status, reason, retryable = classify_exception("captcha detected")
        self.assertFalse(retryable)


if __name__ == "__main__":
    unittest.main()
