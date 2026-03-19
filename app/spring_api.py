"""
春秋航空官网 API 直连模块
- 零截图、零 LLM、100% 准确
- 直接拿每日最低价（USD）
- 支持 NRT/PVG 双向
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from curl_cffi import requests as requests
    _IMPERSONATE = {"impersonate": "chrome"}
except ImportError:
    import requests  # type: ignore
    _IMPERSONATE = {}

from app.anti_bot import classify_exception
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

# USD/JPY → CNY 汇率（启动时为默认值，每日从免费API刷新）
USD_TO_CNY = 7.2
JPY_TO_CNY = 0.048

_rate_cache = {"date": None, "usd_cny": USD_TO_CNY, "jpy_cny": JPY_TO_CNY}


def get_exchange_rates():
    """返回最新汇率 (usd_cny, jpy_cny)。每日从 Frankfurter API 刷新一次，失败时沿用上次缓存值。"""
    from datetime import date
    today = date.today()
    if _rate_cache["date"] == today:
        return _rate_cache["usd_cny"], _rate_cache["jpy_cny"]
    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "CNY,JPY"},
            timeout=8,
            **_IMPERSONATE,
        )
        resp.raise_for_status()
        rates = resp.json().get("rates", {})
        usd_cny = rates.get("CNY", _rate_cache["usd_cny"])
        usd_jpy = rates.get("JPY", None)
        jpy_cny = round(usd_cny / usd_jpy, 6) if usd_jpy else _rate_cache["jpy_cny"]
        _rate_cache.update({"date": today, "usd_cny": usd_cny, "jpy_cny": jpy_cny})
        log.info(f"💱 汇率更新: 1USD={usd_cny:.4f}CNY, 1JPY={jpy_cny:.5f}CNY")
    except Exception as e:
        log.warning(f"💱 汇率获取失败，沿用缓存值: {e}")
    return _rate_cache["usd_cny"], _rate_cache["jpy_cny"]


def fetch_spring_prices(origin, destination, year_month, session=None):
    """
    从春秋官网API获取某月每日最低价

    Args:
        origin: 出发机场代码 (NRT/HND)
        destination: 到达机场代码 (PVG/SHA)
        year_month: 年月字符串 "2026-9" 或 "2026-09"
        session: 可选 requests.Session，用于共享 WAF cookie

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

    requester = session or requests
    try:
        resp = requester.post(_API_URL, headers=_HEADERS, data=data, timeout=15,
                              **_IMPERSONATE)
        resp.raise_for_status()
        result = resp.json()

        usd_cny, _ = get_exchange_rates()
        trends = result.get("PriceTrends") or []
        prices = {}
        for t in trends:
            date = t.get("Date", "")
            price_usd = t.get("Price")
            if date and price_usd and price_usd > 0:
                prices[date] = {
                    "price_usd": round(price_usd, 2),
                    "price_cny": round(price_usd * usd_cny),
                    "day_of_week": t.get("DayOfWeek", ""),
                }

        log.info(f"  🌸 春秋API {origin}→{destination} {year_month}: {len(prices)} 天有价格")
        return prices, {"status": "ok", "block_reason": None, "retryable": True}

    except Exception as e:
        log.error(f"  🌸 春秋API失败 {origin}→{destination}: {e}")
        status, reason, retryable = classify_exception(e)
        return {}, {"status": status, "block_reason": reason, "retryable": retryable, "error": str(e)}


def get_spring_price_for_trip(trip, proxy_url=None, proxy_id=None):
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
        "status": "no_data",
        "block_reason": None,
        "retryable": True,
        "proxy_id": proxy_id,
    }

    from datetime import datetime, timedelta

    # 搜索所有机场组合，取最便宜的
    # 去程: NRT→PVG, NRT→SHA, HND→PVG, HND→SHA
    ob_routes = [("NRT", "PVG"), ("NRT", "SHA"), ("HND", "PVG"), ("HND", "SHA")]
    # 回程: PVG→NRT, SHA→NRT, PVG→HND, SHA→HND
    rt_routes = [("PVG", "NRT"), ("SHA", "NRT"), ("PVG", "HND"), ("SHA", "HND")]

    all_ob = {}  # {(date, origin, dest): price_cny}
    all_rt = {}

    # 用共享 Session，先访问主页拿到阿里云 WAF 的 acw_tc cookie，避免后续 POST 被 405
    session = requests.Session()
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    try:
        session.get("https://en.ch.com/", headers=_HEADERS, timeout=10, **_IMPERSONATE)
    except Exception:
        pass  # warm-up 失败不影响主流程

    # 并行请求所有路线（8次串行 → 8并发，最坏耗时从120s降至15s）
    route_tasks = (
        [(o, d, ob_month, "ob") for o, d in ob_routes] +
        [(o, d, rt_month, "rt") for o, d in rt_routes]
    )

    def _fetch(origin, dest, month, direction):
        prices, meta = fetch_spring_prices(origin, dest, month, session)
        return origin, dest, month, direction, prices, meta

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch, o, d, m, dir_): (o, d, dir_)
                   for o, d, m, dir_ in route_tasks}
        for future in as_completed(futures):
            try:
                origin, dest, month, direction, prices, meta = future.result()
            except Exception as e:
                o, d, dir_ = futures[future]
                log.error(f"  🌸 春秋并行请求失败 {o}→{d}: {e}")
                continue
            if meta.get("status") == "blocked":
                result["status"] = "blocked"
                result["block_reason"] = meta.get("block_reason")
                result["retryable"] = meta.get("retryable", False)
            elif meta.get("status") == "ok" and result.get("status") != "blocked":
                result["status"] = "ok"

            if direction == "ob":
                if ob_date in prices:
                    key = (ob_date, origin, dest)
                    all_ob[key] = prices[ob_date]["price_cny"]
                    if result["outbound"] is None or prices[ob_date]["price_cny"] < result["outbound"].get("price_cny", 99999):
                        result["outbound"] = {"date": ob_date, "route": f"{origin}→{dest}", **prices[ob_date]}
                for i in range(1, ob_flex + 1):
                    flex_date = str((datetime.strptime(ob_date, "%Y-%m-%d") - timedelta(days=i)).date())
                    if flex_date in prices:
                        key = (flex_date, origin, dest)
                        all_ob[key] = prices[flex_date]["price_cny"]
                        result["outbound_flex"][f"{flex_date}_{origin}_{dest}"] = {
                            "route": f"{origin}→{dest}", **prices[flex_date]
                        }
            else:
                if rt_date in prices:
                    key = (rt_date, origin, dest)
                    all_rt[key] = prices[rt_date]["price_cny"]
                    if result["return"] is None or prices[rt_date]["price_cny"] < result["return"].get("price_cny", 99999):
                        result["return"] = {"date": rt_date, "route": f"{origin}→{dest}", **prices[rt_date]}
                for i in range(1, rt_flex + 1):
                    flex_date = str((datetime.strptime(rt_date, "%Y-%m-%d") - timedelta(days=i)).date())
                    if flex_date in prices:
                        key = (flex_date, origin, dest)
                        all_rt[key] = prices[flex_date]["price_cny"]
                        result["return_flex"][f"{flex_date}_{origin}_{dest}"] = {
                            "route": f"{origin}→{dest}", **prices[flex_date]
                        }

    session.close()

    # 找全局最优组合
    if all_ob and all_rt:
        best_ob_key = min(all_ob, key=all_ob.get)
        best_rt_key = min(all_rt, key=all_rt.get)
        result["best_combo"] = {
            "outbound_date": best_ob_key[0],
            "outbound_route": f"{best_ob_key[1]}→{best_ob_key[2]}",
            "outbound_cny": all_ob[best_ob_key],
            "return_date": best_rt_key[0],
            "return_route": f"{best_rt_key[1]}→{best_rt_key[2]}",
            "return_cny": all_rt[best_rt_key],
            "total_cny": all_ob[best_ob_key] + all_rt[best_rt_key],
        }

    if result["outbound"] and result["return"]:
        result["total_cny"] = result["outbound"]["price_cny"] + result["return"]["price_cny"]
        result["status"] = "ok"

    return result
