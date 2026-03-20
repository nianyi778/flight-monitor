#!/usr/bin/env python3
"""
本地测试脚本：查询携程航班价格
用法: python test_ctrip.py [日期 YYYY-MM-DD] [出发机场] [到达机场]
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))

LOCAL_DATA = PROJECT_DIR / "data"
LOCAL_DATA.mkdir(exist_ok=True)
os.environ.setdefault("DATA_DIR_OVERRIDE", str(LOCAL_DATA))

import app.config as _cfg

_cfg.DATA_DIR = LOCAL_DATA
_cfg.SCREENSHOT_DIR = LOCAL_DATA / "screenshots"
_cfg.PRICE_LOG = LOCAL_DATA / "price_log.jsonl"
_cfg.STATE_FILE = LOCAL_DATA / "state.json"
_cfg.BROWSER_PROFILE = LOCAL_DATA / "browser_profile"

from app.ctrip_api import _canonical_search_url, get_ctrip_flights_for_searches

flight_date = sys.argv[1] if len(sys.argv) > 1 else (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")
origin = sys.argv[2].upper() if len(sys.argv) > 2 else "NRT"
destination = sys.argv[3].upper() if len(sys.argv) > 3 else "PVG"

url = _canonical_search_url(origin, destination, flight_date)
searches = [
    {
        "url": url,
        "origin": origin,
        "destination": destination,
        "flight_date": flight_date,
        "name": f"{origin}→{destination}",
    }
]

print(f"\n{'=' * 60}")
print(f"  携程航班查询: {origin} → {destination}")
print(f"  日期: {flight_date}")
print(f"  URL: {url}")
print(f"{'=' * 60}\n")

results = get_ctrip_flights_for_searches(searches)
result = results.get(url, {})
status = result.get("status", "unknown")

print(f"\n{'=' * 60}")
print(f"  状态: {status}")
if result.get("lowest_price"):
    print(f"  最低价: ¥{result['lowest_price']}")
if result.get("error"):
    print(f"  错误: {result['error']}")
if result.get("block_reason"):
    print(f"  风控原因: {result['block_reason']}")
if result.get("diagnosis"):
    print(f"  诊断: {result['diagnosis'].get('action')} / {result['diagnosis'].get('reason')}")

flights = result.get("flights", [])
if flights:
    print(f"\n  共 {len(flights)} 个航班:\n")
    for i, f in enumerate(flights[:10], 1):
        airline = f.get("airline", "?")
        fno = f.get("flight_no", "")
        dep = f.get("departure_time", "")
        arr = f.get("arrival_time", "")
        price = f.get("price_cny", "?")
        print(f"  {i:2}. {airline} {fno:8}  {dep} → {arr}  ¥{price}")
    if len(flights) > 10:
        print(f"  ... 还有 {len(flights) - 10} 个航班")
else:
    print("\n  未获取到航班数据")

print(f"\n{'=' * 60}")
print("\n原始结果 (JSON):")
print(json.dumps(result, ensure_ascii=False, indent=2))
