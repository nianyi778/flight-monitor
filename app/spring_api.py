"""
春秋航空官网 API 直连模块
- 零截图、零 LLM、100% 准确
- 直接拿每日最低价（USD）
- 支持 NRT/PVG 双向
"""

import requests
from app.config import now_jst, log

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://en.ch.com/",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}

_API_URL = "https://en.ch.com/Flights/MinPriceTrends"

# 机场名映射（春秋 API 用英文全名）
_AIRPORT_NAMES = {
    "NRT": "Tokyo(Narita)",
    "HND": "Tokyo(Haneda)",
    "PVG": "Shanghaipudong",
    "SHA": "Shanghaihongqiao",
}

# USD → CNY 汇率（近似）
USD_TO_CNY = 7.2


def fetch_spring_prices(origin, destination, year_month):
    """
    从春秋官网API获取某月每日最低价

    Args:
        origin: 出发机场代码 (NRT/HND)
        destination: 到达机场代码 (PVG/SHA)
        year_month: 年月字符串 "2026-9" 或 "2026-09"

    Returns:
        {
            "2026-09-18": {"price_usd": 275.2, "price_cny": 1981, "day_of_week": "Fri"},
            ...
        }
        失败返回 {}
    """
    parts = year_month.replace("-0", "-").split("-")
    dep_date = f"{parts[0]}-{parts[1]}-1"

    dep_name = _AIRPORT_NAMES.get(origin, origin)
    arr_name = _AIRPORT_NAMES.get(destination, destination)

    data = {
        "Currency": "1",
        "DepartureDate": dep_date,
        "Departure": dep_name,
        "Arrival": arr_name,
        "Days": "0",
        "IfRet": "false",
        "SType": "0",
        "IsIJFlight": "false",
        "ActId": "",
        "IsReturn": "false",
        "IsShowTaxprice": "false",
    }

    try:
        resp = requests.post(_API_URL, headers=_HEADERS, data=data, timeout=15)
        resp.raise_for_status()
        result = resp.json()

        trends = result.get("PriceTrends") or []
        prices = {}
        for t in trends:
            date = t.get("Date", "")
            price_usd = t.get("Price")
            if date and price_usd and price_usd > 0:
                prices[date] = {
                    "price_usd": round(price_usd, 2),
                    "price_cny": round(price_usd * USD_TO_CNY),
                    "day_of_week": t.get("DayOfWeek", ""),
                }

        log.info(f"  🌸 春秋API {origin}→{destination} {year_month}: {len(prices)} 天有价格")
        return prices

    except Exception as e:
        log.error(f"  🌸 春秋API失败 {origin}→{destination}: {e}")
        return {}


def get_spring_price_for_trip(trip):
    """
    获取某行程的春秋官网直销价

    Returns:
        {
            "outbound": {"date": "2026-09-18", "price_usd": 275.2, "price_cny": 1981, ...},
            "return": {"date": "2026-09-27", "price_usd": 274.2, "price_cny": 1974, ...},
            "total_cny": 3955,
            "outbound_flex": {日期: 价格, ...},  # 弹性日期的价格
            "return_flex": {日期: 价格, ...},
        }
    """
    ob_date = trip["outbound_date"]  # "2026-09-18"
    rt_date = trip["return_date"]    # "2026-09-27"
    ob_month = ob_date[:7]           # "2026-09"
    rt_month = rt_date[:7]

    ob_flex = trip.get("outbound_flex", 0)
    rt_flex = trip.get("return_flex", 1)

    result = {
        "outbound": None, "return": None, "total_cny": None,
        "outbound_flex": {}, "return_flex": {},
        "source": "春秋官网",
    }

    # 去程价格
    ob_prices = fetch_spring_prices("NRT", "PVG", ob_month)
    if ob_date in ob_prices:
        result["outbound"] = {"date": ob_date, **ob_prices[ob_date]}

    # 去程弹性日期
    from datetime import datetime, timedelta
    for i in range(1, ob_flex + 1):
        flex_date = str((datetime.strptime(ob_date, "%Y-%m-%d") - timedelta(days=i)).date())
        if flex_date in ob_prices:
            result["outbound_flex"][flex_date] = ob_prices[flex_date]

    # 回程价格
    rt_prices = fetch_spring_prices("PVG", "NRT", rt_month)
    if rt_date in rt_prices:
        result["return"] = {"date": rt_date, **rt_prices[rt_date]}

    # 回程弹性日期
    for i in range(1, rt_flex + 1):
        flex_date = str((datetime.strptime(rt_date, "%Y-%m-%d") - timedelta(days=i)).date())
        if flex_date in rt_prices:
            result["return_flex"][flex_date] = rt_prices[flex_date]

    # 找最优组合（含弹性日期）
    all_ob = {}
    if result["outbound"]:
        all_ob[ob_date] = result["outbound"]["price_cny"]
    for d, p in result["outbound_flex"].items():
        all_ob[d] = p["price_cny"]

    all_rt = {}
    if result["return"]:
        all_rt[rt_date] = result["return"]["price_cny"]
    for d, p in result["return_flex"].items():
        all_rt[d] = p["price_cny"]

    if all_ob and all_rt:
        best_ob_date = min(all_ob, key=all_ob.get)
        best_rt_date = min(all_rt, key=all_rt.get)
        result["best_combo"] = {
            "outbound_date": best_ob_date,
            "outbound_cny": all_ob[best_ob_date],
            "return_date": best_rt_date,
            "return_cny": all_rt[best_rt_date],
            "total_cny": all_ob[best_ob_date] + all_rt[best_rt_date],
        }

    if result["outbound"] and result["return"]:
        result["total_cny"] = result["outbound"]["price_cny"] + result["return"]["price_cny"]

    return result
