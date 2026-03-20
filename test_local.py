"""
本地快速测试脚本 - 不依赖 DB / TG / Docker
直接调用 Google Flights + 春秋官网 API，打印价格数据
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

# 创建本地 data 目录，避免 config.py 报错
os.makedirs("data", exist_ok=True)

# 覆盖 DATA_DIR 为本地路径（config.py 里写死了 /app/data）
import pathlib
import app.config as _cfg
_cfg.DATA_DIR = pathlib.Path("data")

from app.config import log

# ─── 测试参数 ─────────────────────────────────────
# 修改这里来测试不同日期
TRIPS = [
    {"outbound_date": "2026-09-18", "return_date": "2026-09-27"},
    {"outbound_date": "2026-10-09", "return_date": "2026-10-13"},
]
# ──────────────────────────────────────────────────


def test_google_flights():
    print("\n" + "="*55)
    print("📡 Google Flights API 测试")
    print("="*55)
    from app.google_flights_api import _query_fast_flights, _parse_result

    routes = [
        ("NRT", "PVG", "2026-09-18"),
        ("PVG", "NRT", "2026-09-27"),
    ]
    for origin, dest, date in routes:
        print(f"\n  🔍 {origin}→{dest} {date}")
        raw, err = _query_fast_flights(origin, dest, date)
        if err:
            print(f"  ❌ 错误: {err}")
            continue
        flights = _parse_result(raw, origin, dest)
        if not flights:
            print("  ⚠️  无直飞航班数据")
            continue
        print(f"  ✓ 共 {len(flights)} 个航班，最低 ¥{flights[0]['price_cny']}")
        for f in flights[:5]:
            print(f"    {f['airline']:25s} {f['departure_time']}→{f['arrival_time']}  ¥{f['price_cny']}")


def test_spring_api():
    print("\n" + "="*55)
    print("🌸 春秋航空官网 API 测试")
    print("="*55)
    from app.spring_api import fetch_spring_prices

    routes = [
        ("NRT", "PVG", "2026-9"),
        ("NRT", "SHA", "2026-9"),
        ("HND", "PVG", "2026-9"),
        ("PVG", "NRT", "2026-9"),
        ("NRT", "PVG", "2026-10"),
        ("PVG", "NRT", "2026-10"),
    ]
    for origin, dest, month in routes:
        print(f"\n  🔍 {origin}→{dest} {month}")
        prices, meta = fetch_spring_prices(origin, dest, month)
        if meta.get("status") != "ok" or not prices:
            print(f"  ⚠️  无数据 ({meta.get('error', meta.get('block_reason', '空'))})")
            continue
        sorted_prices = sorted(prices.items(), key=lambda x: x[1]["price_cny"])
        print(f"  ✓ {len(prices)} 天有价格，最低 ¥{sorted_prices[0][1]['price_cny']} ({sorted_prices[0][0]})")
        for date, p in sorted_prices[:5]:
            print(f"    {date} ({p['day_of_week']:3s})  ${p['price_usd']:.1f} → ¥{p['price_cny']}")


def test_spring_trip():
    print("\n" + "="*55)
    print("🎫 春秋往返最优组合测试")
    print("="*55)
    from app.spring_api import get_spring_price_for_trip

    for trip_dates in TRIPS:
        trip = {
            "id": 1,
            "outbound_date": trip_dates["outbound_date"],
            "return_date": trip_dates["return_date"],
            "budget": 4000,
            "outbound_flex": 2,
            "return_flex": 2,
        }
        print(f"\n  行程: {trip['outbound_date']} → {trip['return_date']}")
        result = get_spring_price_for_trip(trip)
        status = result.get("status")
        if status != "ok":
            print(f"  ⚠️  状态: {status} | {result.get('block_reason') or result.get('error', '无数据')}")
            continue

        ob = result.get("outbound") or {}
        rt = result.get("return") or {}
        total = result.get("total_cny")
        print(f"  去程: {ob.get('route','?')} {ob.get('date','?')}  ¥{ob.get('price_cny','?')}")
        print(f"  回程: {rt.get('route','?')} {rt.get('date','?')}  ¥{rt.get('price_cny','?')}")
        print(f"  合计: ¥{total}")

        best = result.get("best_combo")
        if best and best.get("total_cny") != total:
            print(f"  ★ 最优弹性组合: 去{best['outbound_date']} {best['outbound_route']} ¥{best['outbound_cny']} + "
                  f"回{best['return_date']} {best['return_route']} ¥{best['return_cny']} = ¥{best['total_cny']}")


if __name__ == "__main__":
    test_google_flights()
    test_spring_api()
    test_spring_trip()
    print("\n✅ 测试完成\n")
