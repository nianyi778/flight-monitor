"""
集成测试：直接调用真实 API，验证返回结果格式正确
运行: PYTHONPATH=. python -m pytest tests/test_integration.py -v -s
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from datetime import date, timedelta


def _future_date(days=180) -> str:
    return str(date.today() + timedelta(days=days))


class GoogleFlightsIntegrationTest(unittest.TestCase):
    def test_query_returns_flights(self):
        from app.google_flights_api import get_google_flights_for_searches

        dep = _future_date(180)
        search = {
            "url": f"https://www.google.com/travel/flights#stub/{dep}",
            "origin": "NRT",
            "destination": "PVG",
            "flight_date": dep,
            "name": "GoogleAPI_NRT_PVG",
        }
        results = get_google_flights_for_searches([search])
        r = results[search["url"]]

        print(f"\n  Google Flights: status={r['status']} flights={len(r.get('flights', []))} lowest={r.get('lowest_price')}")
        if r.get("error"):
            print(f"  error: {r['error']}")

        # 允许被限流(degraded)，但不允许代码崩溃
        self.assertIn(r["status"], ("ok", "degraded", "blocked"))
        if r["status"] == "ok":
            self.assertGreater(len(r["flights"]), 0)
            self.assertIsNotNone(r["lowest_price"])
            f0 = r["flights"][0]
            self.assertIn("airline", f0)
            self.assertIn("departure_time", f0)
            self.assertIn("price_cny", f0)
            self.assertGreater(f0["price_cny"], 0)


class SpringAirlinesIntegrationTest(unittest.TestCase):
    def test_query_returns_prices(self):
        from app.spring_api import fetch_spring_prices

        ym = _future_date(180)[:7]  # "YYYY-MM"
        prices, meta = fetch_spring_prices("NRT", "PVG", ym)

        print(f"\n  春秋 NRT→PVG {ym}: status={meta['status']} days={len(prices)}")
        if meta.get("error"):
            print(f"  error: {meta['error']}")

        # 允许 WAF 拦截(blocked)，但不允许代码崩溃
        self.assertIn(meta["status"], ("ok", "blocked", "degraded"))
        if meta["status"] == "ok":
            self.assertGreater(len(prices), 0)
            # 验证价格结构
            sample = next(iter(prices.values()))
            self.assertIn("price_usd", sample)
            self.assertIn("price_cny", sample)
            self.assertGreater(sample["price_cny"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
