"""
Google Flights API 客户端
- 使用 fast-flights 库（protobuf 逆向，纯 HTTP，无需浏览器）
- 价格为 JPY，通过 spring_api.get_exchange_rates() 转换为 CNY
- 兼容 fast-flights v2.x（v2.2+ 接口）
"""

import re

from app.anti_bot import classify_exception, finalize_result_status, make_result
from app.config import log


def _jpy_to_cny(price_jpy):
    """日元 → 人民币，使用 spring_api 的汇率缓存"""
    try:
        from app.spring_api import get_exchange_rates
        _, jpy_cny = get_exchange_rates()
        return round(price_jpy * jpy_cny)
    except Exception:
        return round(price_jpy * 0.048)  # 默认汇率兜底


def _parse_price_str(price_str):
    """
    解析价格字符串 → int JPY
    支持: "¥12,345", "JPY 12,345", "12345", "12,345"
    """
    if not price_str:
        return None
    try:
        cleaned = re.sub(r"[^\d.]", "", str(price_str))
        if cleaned:
            return int(float(cleaned))
    except Exception:
        pass
    return None


def _parse_time_str(time_str: str) -> str:
    """
    解析 fast-flights v2 时间格式 → HH:MM (24h)
    输入: "8:00 PM on Fri, Apr 10"  "7:20 AM on Sat, Apr 11"
    输出: "20:00"  "07:20"
    """
    if not time_str:
        return ""
    # 提取 "H:MM AM/PM" 部分
    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_str, re.IGNORECASE)
    if not m:
        # 已经是 HH:MM 格式则直接返回
        m2 = re.search(r"(\d{2}):(\d{2})", time_str)
        return m2.group(0) if m2 else time_str[:5]
    hour, minute, period = int(m.group(1)), m.group(2), m.group(3).upper()
    if period == "PM" and hour != 12:
        hour += 12
    elif period == "AM" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute}"


def _query_fast_flights(origin, destination, date_str):
    """
    调用 fast-flights v2.2 API
    返回 (Result | None, Exception | None)
    """
    try:
        from fast_flights import FlightQuery, Passengers, create_query, get_flights
        query = create_query(
            flights=[FlightQuery(date=date_str, from_airport=origin, to_airport=destination)],
            trip="one-way",
            passengers=Passengers(adults=1),
            seat="economy",
        )
        result = get_flights(query)
        return result, None
    except Exception as e:
        log.debug(f"  Google fast-flights 失败 {origin}→{destination}: {e}")
        return None, e


def _parse_result(result, origin, destination):
    """
    把 fast-flights v2 Result 对象转换为标准航班列表
    Flight 对象属性（v2.2）: name, departure, arrival, price, stops, duration
    """
    flights = []
    if not result:
        return flights

    raw_flights = getattr(result, "flights", None) or []

    for f in raw_flights:
        try:
            stops = getattr(f, "stops", 0)
            # stops 可能是 int 0 或字符串 "Unknown"
            if stops and str(stops) not in ("0", "Unknown"):
                continue

            name = getattr(f, "name", "") or ""
            dep_raw = str(getattr(f, "departure", "") or "")
            arr_raw = str(getattr(f, "arrival", "") or "")

            dep_time = _parse_time_str(dep_raw)
            arr_time = _parse_time_str(arr_raw)

            price_raw = getattr(f, "price", None)
            if isinstance(price_raw, str) and "unavailable" in price_raw.lower():
                continue
            price_jpy = _parse_price_str(price_raw)
            if not price_jpy:
                continue

            price_cny = _jpy_to_cny(price_jpy)

            flights.append({
                "airline": name,
                "flight_no": "",
                "departure_time": dep_time,
                "arrival_time": arr_time,
                "price_cny": price_cny,
                "origin": origin,
                "destination": destination,
            })
        except Exception:
            continue

    return sorted(flights, key=lambda x: x.get("price_cny", 99999))


def get_google_flights_for_searches(searches, proxy_url=None, proxy_id=None):
    """
    批量查询 Google Flights 航班价格

    Args:
        searches: list of search dicts (each has url, origin, destination, flight_date, name)

    Returns:
        {url: analysis_result}  — 与 analyzer.py 格式兼容
    """
    if not searches:
        return {}

    try:
        import fast_flights  # noqa: F401
    except ImportError:
        log.warning("  ⚠️ fast-flights 未安装，跳过 Google Flights API")
        return {s["url"]: {
            "flights": [], "lowest_price": None,
            "error": "fast-flights 未安装",
            "source": "GoogleAPI", "url": s["url"],
            "flight_date": s.get("flight_date", ""),
            "status": "degraded",
            "block_reason": None,
            "retryable": True,
            "request_mode": "api",
            "proxy_id": proxy_id,
        } for s in searches}

    results = {}

    for s in searches:
        url = s["url"]
        origin = s.get("origin", "")
        destination = s.get("destination", "")
        date_str = s.get("flight_date", "")

        result = make_result(
            source=f"GoogleAPI_{origin}_{destination}",
            url=url,
            flight_date=date_str,
            proxy_id=proxy_id,
            request_mode="api",
        )

        if not (origin and destination and date_str):
            result["error"] = "缺少 origin/destination/date"
            result["status"] = "degraded"
            results[url] = result
            continue

        log.info(f"  🔍 Google Flights API: {origin}→{destination} {date_str}")

        raw, query_error = _query_fast_flights(origin, destination, date_str)
        flights = _parse_result(raw, origin, destination)

        if flights:
            result["flights"] = flights
            result["lowest_price"] = flights[0]["price_cny"]
            log.info(f"  ✓ Google API: {len(flights)} 个航班, 最低 ¥{result['lowest_price']}")
        else:
            if query_error:
                status, reason, retryable = classify_exception(query_error)
                result["status"] = status
                result["block_reason"] = reason
                result["retryable"] = retryable
                result["error"] = f"Google Flights API: {query_error}"
            else:
                result["error"] = "Google Flights API: 无数据（或被限流）"
            log.warning(f"  ⚠️ Google Flights API: 无数据 {origin}→{destination} {date_str}")

        results[url] = finalize_result_status(result)

    return results
