"""
Google Flights API 客户端
- 使用 fast-flights 库（protobuf 逆向，纯 HTTP，无需浏览器）
- 价格为 JPY，通过 spring_api.get_exchange_rates() 转换为 CNY
- fetch_mode="fallback" 在 API 被限流时用备用端点
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
        # 去掉货币符号和逗号，提取数字
        cleaned = re.sub(r"[^\d.]", "", str(price_str))
        if cleaned:
            return int(float(cleaned))
    except Exception:
        pass
    return None


def _get_flights_v1(origin, destination, date_str):
    """
    fast-flights v1.x API: FlightData + Passengers
    """
    from fast_flights import FlightData, Passengers, get_flights
    result = get_flights(
        FlightData(date=date_str, from_airport=origin, to_airport=destination),
        passengers=Passengers(adults=1),
        trip="one-way",
        currency="JPY",
        fetch_mode="fallback",
    )
    return result


def _get_flights_v2(origin, destination, date_str):
    """
    fast-flights v2.x API: create_filter (如果 v1 API 不存在时尝试)
    """
    from fast_flights import create_filter, get_flights
    f = create_filter(
        flight_data=[{"date": date_str, "from_airport": origin, "to_airport": destination}],
        trip="one-way",
        passengers={"adults": 1},
        currency="JPY",
    )
    return get_flights(f, fetch_mode="fallback")


def _query_fast_flights(origin, destination, date_str):
    """
    调用 fast-flights，兼容 v1/v2 API
    返回 fast-flights Result 对象，失败返回 None
    """
    try:
        return _get_flights_v1(origin, destination, date_str), None
    except (ImportError, TypeError, AttributeError):
        pass
    try:
        return _get_flights_v2(origin, destination, date_str), None
    except Exception as e:
        log.debug(f"  Google fast-flights 失败 {origin}→{destination}: {e}")
        return None, e


def _parse_result(result, origin, destination):
    """
    把 fast-flights Result 对象转换为标准航班列表
    Flight 对象属性：airline, departure_airport, arrival_airport,
                     departure_time, arrival_time, price, stops
    """
    flights = []
    if not result:
        return flights

    raw_flights = getattr(result, "flights", None) or []

    for f in raw_flights:
        try:
            # 只取直飞（stops == 0）
            stops = getattr(f, "stops", 0)
            if stops and stops > 0:
                continue

            airline = getattr(f, "airline", "") or ""
            dep_raw = str(getattr(f, "departure_time", "") or "")
            arr_raw = str(getattr(f, "arrival_time", "") or "")

            # 标准化时间格式 HH:MM
            dep_time = dep_raw[:5] if len(dep_raw) >= 5 else dep_raw
            arr_time = arr_raw[:5] if len(arr_raw) >= 5 else arr_raw

            # 价格（JPY 字符串 → int → CNY）
            price_raw = getattr(f, "price", None) or getattr(f, "price_str", None)
            price_jpy = _parse_price_str(price_raw)
            if not price_jpy:
                continue

            price_cny = _jpy_to_cny(price_jpy)

            # 航班号（部分版本有）
            flight_no = str(getattr(f, "flight_number", "") or "")

            flights.append({
                "airline": airline,
                "flight_no": flight_no,
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

    # 检查 fast-flights 是否可用
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
            log.info(f"  ✓ Google API: {len(flights)} 个直飞, 最低 ¥{result['lowest_price']}")
        else:
            if query_error:
                status, reason, retryable = classify_exception(query_error)
                result["status"] = status
                result["block_reason"] = reason
                result["retryable"] = retryable
                result["error"] = f"Google Flights API: {query_error}"
            else:
                result["error"] = "Google Flights API: 无直飞数据（或被限流）"
            log.warning(f"  ⚠️ Google Flights API: 无数据 {origin}→{destination} {date_str}")

        results[url] = finalize_result_status(result)

    return results
