"""
携程内部 API 客户端
- curl_cffi 模拟 Chrome 131 TLS 指纹（JA3），绕过携程 WAF
- 优先: GET lowestPrice（轻量，快）
- 备选: POST products（完整航班列表）
- Session 预热: GET 首页拿 WAF cookie
"""

import json
import random
import time

from app.anti_bot import classify_exception, finalize_result_status, make_result
from app.config import PROXY_URL, now_jst, log

_BASE_URL = "https://flights.ctrip.com"

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# 城市名映射（Ctrip products API 需要城市名）
_CITY_NAMES = {
    "NRT": "Tokyo(Narita)",
    "HND": "Tokyo(Haneda)",
    "PVG": "Shanghai Pudong",
    "SHA": "Shanghai Hongqiao",
}


def _make_session(proxy_url=None):
    """创建 Session，优先用 curl_cffi 模拟 Chrome 131 TLS 指纹"""
    proxy_server = proxy_url if proxy_url is not None else PROXY_URL
    proxies = {"https": proxy_server, "http": proxy_server} if proxy_server else None
    ua = random.choice(_UA_POOL)
    headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://flights.ctrip.com/",
        "Origin": "https://flights.ctrip.com",
    }

    try:
        from curl_cffi import requests as curl_requests
        session = curl_requests.Session(impersonate="chrome131")
        if proxies:
            session.proxies = proxies
        session.headers.update(headers)
        log.debug("  携程: 使用 curl_cffi Chrome131 TLS 指纹")
        return session
    except ImportError:
        import requests
        session = requests.Session()
        if proxies:
            session.proxies = proxies
        session.headers.update(headers)
        log.debug("  携程: curl_cffi 未安装，使用普通 requests（TLS 伪装不可用）")
        return session


def _warmup_session(session):
    """访问首页拿 WAF cookie（acw_tc 等）"""
    try:
        session.get(f"{_BASE_URL}/", timeout=8)
    except Exception as e:
        log.debug(f"  携程 Session 预热失败（不影响主流程）: {e}")


def _fetch_lowest_price(session, origin, destination, date_str):
    """
    GET lowestPrice：获取某日最低价（轻量端点，无需 token）
    返回 Ctrip 日历价格 JSON，失败返回 None
    """
    url = f"{_BASE_URL}/itinerary/api/12808/lowestPrice"
    params = {
        "dcity": origin,
        "acity": destination,
        "depdate": date_str,
        "direct": "true",
        "classType": "Economy",
    }
    try:
        resp = session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == 0 or "data" in data:
            return data
        return None
    except Exception as e:
        log.debug(f"  携程 lowestPrice 失败 {origin}→{destination}: {e}")
        return None


def _fetch_products(session, origin, destination, date_str):
    """
    POST products：获取完整航班列表（含时刻、舱位、价格）
    返回 Ctrip 航班 JSON，失败返回 None
    """
    url = f"{_BASE_URL}/itinerary/api/12808/products"
    payload = {
        "flightWay": "Oneway",
        "classType": "Economy",
        "hasChild": False,
        "hasBaby": False,
        "searchIndex": 1,
        "airportParams": [
            {
                "dcity": origin,
                "acity": destination,
                "dcityname": _CITY_NAMES.get(origin, origin),
                "acityname": _CITY_NAMES.get(destination, destination),
                "date": date_str,
            }
        ],
    }
    try:
        resp = session.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json;charset=UTF-8"},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.debug(f"  携程 products 失败 {origin}→{destination}: {e}")
        return None


def _parse_flights(data, origin, destination):
    """
    解析 products API 返回的航班数据为标准格式
    Ctrip 返回结构可能有 flightItineraryList 或 routeList，这里兼容多种
    """
    flights = []
    if not data:
        return flights

    raw = data.get("data") or data

    # 尝试 flightItineraryList 结构（常见）
    itinerary_list = (
        raw.get("flightItineraryList") or
        raw.get("routeList") or
        []
    )

    for item in itinerary_list:
        try:
            segments = (
                item.get("flightSegments") or
                item.get("flightList") or
                []
            )
            if not segments:
                continue
            seg = segments[0]

            airline = (
                seg.get("marketAirlineName") or
                seg.get("airlineName") or
                seg.get("operatingAirlineName") or
                ""
            )
            flight_no = (
                seg.get("flightNumber") or
                seg.get("marketFlightNo") or
                seg.get("flightNo") or
                ""
            )
            dep_raw = seg.get("departureDateTime") or seg.get("departureTime") or ""
            arr_raw = seg.get("arrivalDateTime") or seg.get("arrivalTime") or ""

            # "2026-09-18T20:00:00" → "20:00"
            dep_time = dep_raw.split("T")[1][:5] if "T" in dep_raw else dep_raw[:5]
            arr_time = arr_raw.split("T")[1][:5] if "T" in arr_raw else arr_raw[:5]

            # 价格：从 priceList 取最低
            price_cny = None
            price_list = item.get("priceList") or []
            for p in price_list:
                candidate = (
                    p.get("adultPrice") or
                    p.get("salePrice") or
                    p.get("cabinPrice") or
                    p.get("price")
                )
                if candidate:
                    v = int(float(candidate))
                    if price_cny is None or v < price_cny:
                        price_cny = v

            if airline and price_cny:
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


def get_ctrip_flights_for_searches(searches, proxy_url=None, proxy_id=None):
    """
    批量查询携程航班价格

    Args:
        searches: list of search dicts (each has url, origin, destination, flight_date, name)

    Returns:
        {url: analysis_result}  — 与 analyzer.py 格式兼容
    """
    if not searches:
        return {}

    results = {}
    session = _make_session(proxy_url=proxy_url)
    _warmup_session(session)

    for s in searches:
        url = s["url"]
        origin = s.get("origin", "")
        destination = s.get("destination", "")
        date_str = s.get("flight_date", "")
        name = s.get("name", f"{origin}_{destination}")

        result = make_result(
            source=f"携程API_{origin}_{destination}",
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

        log.info(f"  🏷️ 携程API: {origin}→{destination} {date_str}")

        # 1. 先调 products（完整列表）
        prod_error = None
        low_error = None
        prod_data = _fetch_products(session, origin, destination, date_str)
        flights = _parse_flights(prod_data, origin, destination)

        if flights:
            result["flights"] = flights
            result["lowest_price"] = flights[0]["price_cny"]
            log.info(f"  ✓ 携程API products: {len(flights)} 个航班, 最低 ¥{result['lowest_price']}")
        else:
            # 2. 降级: lowestPrice（仅返回最低价，无时刻）
            low_data = _fetch_lowest_price(session, origin, destination, date_str)
            price = None
            if low_data:
                d = low_data.get("data") or low_data
                # 尝试多种字段名
                price = (
                    d.get("price") or
                    d.get("lowestPrice") or
                    d.get("minPrice")
                )
                # 也尝试日历格式：{"2026-09-18": {"price": 980}}
                if not price and isinstance(d, dict):
                    day_entry = d.get(date_str)
                    if day_entry and isinstance(day_entry, dict):
                        price = day_entry.get("price") or day_entry.get("adultPrice")

            if price:
                result["lowest_price"] = int(float(price))
                result["flights"] = [{
                    "airline": "携程(最低价)",
                    "flight_no": "",
                    "departure_time": "",
                    "arrival_time": "",
                    "price_cny": int(float(price)),
                    "origin": origin,
                    "destination": destination,
                }]
                log.info(f"  ✓ 携程API lowestPrice: ¥{price}")
            else:
                result["error"] = "携程API: 无有效数据（WAF拦截或接口变更）"
                result["status"] = "blocked"
                result["block_reason"] = "waf"
                result["retryable"] = False
                log.warning(f"  ⚠️ 携程API: 无有效数据 {origin}→{destination} {date_str}")

        results[url] = finalize_result_status(result)

        # 轻微延迟防止速率限制
        time.sleep(random.uniform(0.3, 0.8))

    try:
        session.close()
    except Exception:
        pass
    return results
