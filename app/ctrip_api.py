"""
携程航班抓取
- 优先走会话驱动的 batchSearch 接口（需要 data/ctrip_batch_profile.json）
- 次级尝试旧版 products 接口（接口已逐步下线，但仍可作为轻量探测）
- 再降级到 m.ctrip 最低价接口（只能拿最低价，通常没有完整时刻）
- 浏览器 DOM 仅作为可选兜底，默认关闭
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shlex
import socket
import shutil
import subprocess
import time
from copy import deepcopy
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

try:
    from curl_cffi import requests as requests

    _IMPERSONATE = {"impersonate": "chrome131"}
except ImportError:
    import requests  # type: ignore

    _IMPERSONATE = {}

from app.anti_bot import classify_exception, finalize_result_status, make_result
from app.config import log

_BASE_URL = "https://flights.ctrip.com"
_MOBILE_LOWEST_PRICE_URL = "https://m.ctrip.com/restapi/soa2/15380/bjjson/FlightIntlAndInlandLowestPriceSearch"
_BATCH_SEARCH_URL = f"{_BASE_URL}/international/search/api/search/batchSearch"
_PRODUCTS_URL = f"{_BASE_URL}/itinerary/api/12808/products"
_BROWSER_SESSION = "ctrip-api-live"
_CDP_PORT = os.getenv("CTRIP_CDP_PORT", "")
_ENABLE_BROWSER_FALLBACK = os.getenv("CTRIP_ENABLE_BROWSER_FALLBACK", "1") == "1"
_PROFILE_PATH = Path(os.getenv("CTRIP_PROFILE_PATH") or "/app/data/ctrip_batch_profile.json")
if not _PROFILE_PATH.exists():
    _PROFILE_PATH = Path(__file__).resolve().parents[1] / "data" / "ctrip_batch_profile.json"

def _normalize_cdp_target(value: str) -> str:
    target = (value or "").strip()
    if not target:
        return ""
    if target.isdigit():
        return target
    if "://" in target:
        return target
    host, sep, port = target.partition(":")
    if sep and host and host not in {"localhost", "127.0.0.1"}:
        try:
            host = socket.gethostbyname(host)
        except Exception:
            pass
        target = f"{host}:{port}" if port else host
    return f"http://{target}"


_CITY_META = {
    "NRT": {
        "city_code": "TYO", "city_name": "东京", "country_id": 78,
        "country_code": "JP", "country_name": "日本", "province_id": 0,
        "city_id": 228, "timezone": 540,
        "airport_name": "成田国际机场", "airport_name_en": "Tokyo(Narita)",
    },
    "HND": {
        "city_code": "TYO", "city_name": "东京", "country_id": 78,
        "country_code": "JP", "country_name": "日本", "province_id": 0,
        "city_id": 228, "timezone": 540,
        "airport_name": "羽田机场", "airport_name_en": "Tokyo(Haneda)",
    },
    "PVG": {
        "city_code": "SHA", "city_name": "上海", "country_id": 1,
        "country_code": "CN", "country_name": "中国", "province_id": 2,
        "city_id": 2, "timezone": 480,
        "airport_name": "浦东国际机场", "airport_name_en": "Shanghai Pudong",
    },
    "SHA": {
        "city_code": "SHA", "city_name": "上海", "country_id": 1,
        "country_code": "CN", "country_name": "中国", "province_id": 2,
        "city_id": 2, "timezone": 480,
        "airport_name": "虹桥国际机场", "airport_name_en": "Shanghai Hongqiao",
    },
    # ── 甩尾延伸目的地 ───────────────────────────────
    "KIX": {
        "city_code": "OSA", "city_name": "大阪", "country_id": 78,
        "country_code": "JP", "country_name": "日本", "province_id": 0,
        "city_id": 231, "timezone": 540,
        "airport_name": "关西国际机场", "airport_name_en": "Osaka(Kansai)",
    },
    "ITM": {
        "city_code": "OSA", "city_name": "大阪", "country_id": 78,
        "country_code": "JP", "country_name": "日本", "province_id": 0,
        "city_id": 231, "timezone": 540,
        "airport_name": "大阪伊丹机场", "airport_name_en": "Osaka(Itami)",
    },
    "CTS": {
        "city_code": "CTS", "city_name": "札幌", "country_id": 78,
        "country_code": "JP", "country_name": "日本", "province_id": 0,
        "city_id": 680, "timezone": 540,
        "airport_name": "新千岁机场", "airport_name_en": "Sapporo(Chitose)",
    },
    "FUK": {
        "city_code": "FUK", "city_name": "福冈", "country_id": 78,
        "country_code": "JP", "country_name": "日本", "province_id": 0,
        "city_id": 229, "timezone": 540,
        "airport_name": "福冈机场", "airport_name_en": "Fukuoka",
    },
    "OKA": {
        "city_code": "OKA", "city_name": "冲绳", "country_id": 78,
        "country_code": "JP", "country_name": "日本", "province_id": 0,
        "city_id": 682, "timezone": 540,
        "airport_name": "那霸机场", "airport_name_en": "Okinawa(Naha)",
    },
    "ICN": {
        "city_code": "SEL", "city_name": "首尔", "country_id": 116,
        "country_code": "KR", "country_name": "韩国", "province_id": 0,
        "city_id": 160, "timezone": 540,
        "airport_name": "仁川国际机场", "airport_name_en": "Seoul(Incheon)",
    },
    "SYD": {
        "city_code": "SYD", "city_name": "悉尼", "country_id": 10,
        "country_code": "AU", "country_name": "澳大利亚", "province_id": 0,
        "city_id": 45, "timezone": 660,
        "airport_name": "悉尼机场", "airport_name_en": "Sydney",
    },
    "CAN": {
        "city_code": "CAN", "city_name": "广州", "country_id": 1,
        "country_code": "CN", "country_name": "中国", "province_id": 19,
        "city_id": 103, "timezone": 480,
        "airport_name": "白云国际机场", "airport_name_en": "Guangzhou Baiyun",
    },
    "CTU": {
        "city_code": "CTU", "city_name": "成都", "country_id": 1,
        "country_code": "CN", "country_name": "中国", "province_id": 23,
        "city_id": 105, "timezone": 480,
        "airport_name": "天府国际机场", "airport_name_en": "Chengdu Tianfu",
    },
    "HKG": {
        "city_code": "HKG", "city_name": "香港", "country_id": 1,
        "country_code": "CN", "country_name": "中国", "province_id": 30,
        "city_id": 57, "timezone": 480,
        "airport_name": "香港国际机场", "airport_name_en": "Hong Kong",
    },
    "SIN": {
        "city_code": "SIN", "city_name": "新加坡", "country_id": 154,
        "country_code": "SG", "country_name": "新加坡", "province_id": 0,
        "city_id": 165, "timezone": 480,
        "airport_name": "樟宜机场", "airport_name_en": "Singapore Changi",
    },
}

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _make_session(proxy_url=None):
    session = requests.Session(**_IMPERSONATE)
    proxy_server = proxy_url or ""
    if proxy_server:
        session.proxies = {"http": proxy_server, "https": proxy_server}
    session.headers.update(
        {
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
            "Referer": f"{_BASE_URL}/",
            "Origin": _BASE_URL,
        }
    )
    return session


def _load_profile() -> dict | None:
    try:
        if _PROFILE_PATH.exists():
            data = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        log.warning(f"  ⚠️ 读取携程 profile 失败: {e}")
    return None


def _build_criteria(origin: str, destination: str, date_str: str, profile: dict | None) -> dict:
    origin_meta = _CITY_META[origin]
    destination_meta = _CITY_META[destination]
    criteria = deepcopy((profile or {}).get("criteria") or {})
    if not criteria:
        criteria = {
            "adultCount": 1,
            "childCount": 0,
            "infantCount": 0,
            "flightWay": "S",
            "cabin": "Y_S",
            "scope": "i",
            "extensionAttributes": {"LoggingSampling": False, "isFlightIntlNewUser": False},
            "segmentNo": 1,
            "directFlight": False,
            "extGlobalSwitches": {"useAllRecommendSwitch": True, "unfoldPriceListSwitch": True},
            "noRecommend": False,
            "flightWayEnum": "OW",
            "cabinEnum": "Y_S",
            "isMultiplePassengerType": 0,
        }

    criteria["adultCount"] = 1
    criteria["childCount"] = 0
    criteria["infantCount"] = 0
    criteria["flightWay"] = criteria.get("flightWay") or "S"
    criteria["scope"] = criteria.get("scope") or "i"
    criteria["cabin"] = criteria.get("cabin") or "Y_S"
    criteria["flightWayEnum"] = criteria.get("flightWayEnum") or "OW"
    criteria["cabinEnum"] = criteria.get("cabinEnum") or criteria["cabin"]
    criteria["segmentNo"] = 1
    criteria["isMultiplePassengerType"] = 0
    criteria["directFlight"] = False
    criteria["noRecommend"] = False
    criteria.setdefault("extensionAttributes", {"LoggingSampling": False, "isFlightIntlNewUser": False})
    criteria.setdefault("extGlobalSwitches", {"useAllRecommendSwitch": True, "unfoldPriceListSwitch": True})
    criteria["transactionID"] = criteria.get("transactionID") or f"codex-{int(time.time() * 1000)}"
    criteria["departCountryName"] = origin_meta["country_name"]
    criteria["departProvinceId"] = origin_meta["province_id"]
    criteria["departureCityId"] = origin_meta["city_id"]
    criteria["arrivalCountryName"] = destination_meta["country_name"]
    criteria["arrivalProvinceId"] = destination_meta["province_id"]
    criteria["arrivalCityId"] = destination_meta["city_id"]
    criteria["flightSegments"] = [
        {
            "departureCityCode": origin_meta["city_code"],
            "arrivalCityCode": destination_meta["city_code"],
            "departureAirportCode": origin,
            "arrivalAirportCode": destination,
            "departureCityName": origin_meta["city_name"],
            "arrivalCityName": destination_meta["city_name"],
            "departureDate": date_str,
            "departureCountryId": origin_meta["country_id"],
            "departureCountryName": origin_meta["country_name"],
            "departureCountryCode": origin_meta["country_code"],
            "departureProvinceId": origin_meta["province_id"],
            "departureCityId": origin_meta["city_id"],
            "arrivalCountryId": destination_meta["country_id"],
            "arrivalCountryName": destination_meta["country_name"],
            "arrivalCountryCode": destination_meta["country_code"],
            "arrivalProvinceId": destination_meta["province_id"],
            "arrivalCityId": destination_meta["city_id"],
            "departureAirportName": origin_meta["airport_name"],
            "arrivalAirportName": destination_meta["airport_name"],
            "departureCityTimeZone": origin_meta["timezone"],
            "arrivalCityTimeZone": destination_meta["timezone"],
            "timeZone": origin_meta["timezone"],
        }
    ]
    return criteria


def _profile_headers(profile: dict | None) -> dict:
    headers = dict((profile or {}).get("headers") or {})
    headers.pop("content-length", None)
    return headers


def _canonical_search_url(origin: str, destination: str, date_str: str) -> str:
    ob = _CITY_META.get(origin, {})
    rt = _CITY_META.get(destination, {})
    pair = f"{ob.get('city_code', origin).lower()}-{rt.get('city_code', destination).lower()}"
    return (
        f"{_BASE_URL}/online/list/oneway-{pair}"
        f"?depdate={date_str}&cabin=y_s&adult=1&child=0&infant=0&containstax=1"
    )


def _extract_price(value):
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, dict):
        adult_price = _extract_price(value.get("adultPrice"))
        adult_tax = _extract_price(value.get("adultTax"))
        if adult_price is not None:
            return adult_price + (adult_tax or 0)
        for key in (
            "salePrice",
            "cabinPrice",
            "price",
            "lowestPrice",
            "minPrice",
            "ticketPrice",
            "amount",
        ):
            price = _extract_price(value.get(key))
            if price is not None:
                return price
        return None
    if isinstance(value, (list, tuple)):
        prices = [p for p in (_extract_price(v) for v in value) if p is not None]
        return min(prices) if prices else None
    try:
        return int(float(str(value).replace(",", "")))
    except Exception:
        return None


def _normalize_time(value):
    if not value:
        return ""
    text = str(value).strip()
    if "T" in text:
        text = text.split("T", 1)[1]
    elif " " in text:
        text = text.rsplit(" ", 1)[-1]
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return text[:5]


def _parse_flights_from_pull_response(payload: dict, origin: str, destination: str) -> list[dict]:
    itineraries = (((payload or {}).get("data") or {}).get("flightItineraryList") or [])
    flights = []
    for idx, item in enumerate(itineraries):
        if not isinstance(item, dict):
            continue
        segments = item.get("flightSegments") or []
        if not segments:
            continue
        first_segment = segments[0] if isinstance(segments[0], dict) else {}
        last_segment = segments[-1] if isinstance(segments[-1], dict) else {}
        first_legs = first_segment.get("flightList") or []
        last_legs = last_segment.get("flightList") or []
        first_leg = first_legs[0] if first_legs and isinstance(first_legs[0], dict) else {}
        last_leg = last_legs[-1] if last_legs and isinstance(last_legs[-1], dict) else first_leg
        if not first_leg or not last_leg:
            continue

        prices = [_extract_price(price) for price in (item.get("priceList") or [])]
        prices = [price for price in prices if price is not None]
        if not prices:
            continue

        flight_numbers = []
        for segment in segments:
            for leg in (segment.get("flightList") or []):
                if not isinstance(leg, dict):
                    continue
                flight_no = leg.get("marketFlightNo") or leg.get("flightNo") or ""
                if flight_no and flight_no not in flight_numbers:
                    flight_numbers.append(flight_no)

        transfer_count = first_segment.get("transferCount")
        if transfer_count is None:
            transfer_count = max(len(flight_numbers) - 1, 0)

        # 提取中转机场（用于甩尾检测）
        via_airports = []
        for seg in segments:
            legs = seg.get("flightList") or []
            for i, leg in enumerate(legs):
                if not isinstance(leg, dict):
                    continue
                # 非最后一段的到达机场即为中转点
                if i < len(legs) - 1:
                    arr_code = (
                        leg.get("arrivalAirportCode")
                        or leg.get("arrivalAirport")
                        or leg.get("toAirportCode")
                        or leg.get("arrCode")
                        or ""
                    )
                    if arr_code and arr_code not in via_airports:
                        via_airports.append(arr_code.upper())
            # 多 segment 时，segment 之间的转机点
            if len(segments) > 1:
                arr_code = (
                    (first_leg.get("arrivalAirportCode") or "")
                    if seg is segments[0] else ""
                )
                if arr_code and arr_code not in via_airports:
                    via_airports.append(arr_code.upper())

        flights.append(
            {
                "airline": first_leg.get("marketAirlineName") or first_leg.get("airlineName") or item.get("airlineName") or "携程",
                "flight_no": "/".join(flight_numbers[:4]),
                "departure_time": _normalize_time(first_leg.get("departureDateTime") or first_leg.get("departureTime") or ""),
                "arrival_time": _normalize_time(last_leg.get("arrivalDateTime") or last_leg.get("arrivalTime") or ""),
                "price_cny": min(prices),
                "origin": origin,
                "destination": destination,
                "stops": transfer_count,
                "via": ",".join(via_airports),  # 中转机场列表，用于甩尾检测
                "is_direct": transfer_count == 0,
                "raw_order": idx,
            }
        )

    dedup = {}
    for flight in flights:
        key = (
            flight.get("flight_no", ""),
            flight.get("departure_time", ""),
            flight.get("arrival_time", ""),
            flight.get("price_cny"),
        )
        if key not in dedup:
            dedup[key] = flight
    return sorted(dedup.values(), key=lambda x: x.get("raw_order", 99999))


async def _capture_pull_response_via_playwright(search_url: str) -> dict | None:
    if async_playwright is None or not _CDP_PORT:
        return None

    cdp_target = _normalize_cdp_target(_CDP_PORT)
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_target)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        payloads = []

        async def on_response(response):
            if "/international/search/api/search/pull/" not in response.url:
                return
            try:
                data = await response.json()
            except Exception:
                return
            if isinstance(data, dict):
                payloads.append(data)

        page.on("response", on_response)
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=90000)
            for _ in range(12):
                await page.wait_for_timeout(1000)
                if payloads and (((payloads[-1].get("data") or {}).get("context") or {}).get("finished") is True):
                    break
        finally:
            await page.close()
            await browser.close()

    best = None
    best_count = -1
    for payload in payloads:
        count = len((((payload or {}).get("data") or {}).get("flightItineraryList") or []))
        if count > best_count:
            best = payload
            best_count = count
    return best


def _capture_pull_response(search_url: str) -> dict | None:
    if async_playwright is None or not _CDP_PORT:
        return None
    try:
        return asyncio.run(_capture_pull_response_via_playwright(search_url))
    except Exception as e:
        log.debug(f"  Playwright 抓取 pull 响应失败: {e}")
        return None


def _parse_flights_from_body_text(text: str, origin: str, destination: str) -> list[dict]:
    import re

    if not text:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    start = 0
    for i, line in enumerate(lines):
        if line == "低价提醒":
            start = i + 1
            break
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i] in {"在线客服", "旅游资讯", "宾馆索引", "关于携程"}:
            end = i
            break
    lines = lines[start:end]

    flights = []
    chunk = []
    price_re = re.compile(r"¥\s*(\d+)")
    time_re = re.compile(r"^\d{2}:\d{2}$")
    flight_re = re.compile(r"\b([A-Z0-9]{2}\d{2,4})\b")
    skip_values = {"含税价", "订票", "航班详情", "直飞优先", "低价优先", "起飞时间早-晚", "耗时短优先", "更多排序"}

    def flush(block):
        if not block:
            return
        joined = "\n".join(block)
        price_m = price_re.search(joined)
        times = [line for line in block if time_re.match(line)]
        if not price_m or len(times) < 2:
            return
        price = int(price_m.group(1))
        airline = ""
        flight_no = ""
        for line in block:
            if line in skip_values or price_re.search(line) or time_re.match(line):
                continue
            m = flight_re.search(line)
            if m and not flight_no:
                flight_no = m.group(1)
                candidate = line.replace(flight_no, "").strip()
                if candidate and not airline:
                    airline = candidate
                continue
            if not airline and "机场" not in line and "小时" not in line and "周" not in line and "¥" not in line:
                airline = line
        if not airline:
            airline = "携程"
        flights.append({
            "airline": airline,
            "flight_no": flight_no,
            "departure_time": times[0],
            "arrival_time": times[1],
            "price_cny": price,
            "origin": origin,
            "destination": destination,
        })

    for line in lines:
        if line in skip_values:
            continue
        chunk.append(line)
        if price_re.search(line):
            flush(chunk)
            chunk = []
    flush(chunk)

    dedup = {}
    for flight in flights:
        key = (
            flight.get("airline", ""),
            flight.get("flight_no", ""),
            flight.get("departure_time", ""),
            flight.get("arrival_time", ""),
            flight.get("price_cny"),
        )
        dedup[key] = flight
    return sorted(dedup.values(), key=lambda x: x.get("price_cny") or 99999)


def _extract_flights_from_state(state_obj, origin: str, destination: str) -> list[dict]:
    if not isinstance(state_obj, (dict, list)):
        return []

    best = []

    def _walk(obj, depth=0):
        nonlocal best
        if depth > 12:
            return
        if isinstance(obj, list):
            local = []
            for item in obj:
                if not isinstance(item, dict):
                    _walk(item, depth + 1)
                    continue
                seg = item.get("flightSegments") or item.get("flightList") or item.get("segments") or []
                seg0 = seg[0] if seg and isinstance(seg[0], dict) else item
                airline = (
                    seg0.get("marketAirlineName")
                    or seg0.get("airlineName")
                    or seg0.get("operatingAirlineName")
                    or item.get("airlineName")
                    or item.get("airline")
                    or ""
                )
                flight_no = (
                    seg0.get("flightNumber")
                    or seg0.get("marketFlightNo")
                    or seg0.get("flightNo")
                    or item.get("flightNo")
                    or item.get("flightNumber")
                    or ""
                )
                dep = (
                    seg0.get("departureDateTime")
                    or seg0.get("departureTime")
                    or item.get("departureDateTime")
                    or item.get("departureTime")
                    or item.get("dep")
                    or item.get("depTime")
                    or ""
                )
                arr = (
                    seg0.get("arrivalDateTime")
                    or seg0.get("arrivalTime")
                    or item.get("arrivalDateTime")
                    or item.get("arrivalTime")
                    or item.get("arr")
                    or item.get("arrTime")
                    or ""
                )
                price = _extract_price(item.get("priceList") or item.get("priceInfo") or item.get("prices") or item)
                dep_time = _normalize_time(dep)
                arr_time = _normalize_time(arr)
                if dep_time and arr_time and price:
                    local.append(
                        {
                            "airline": airline,
                            "flight_no": flight_no,
                            "departure_time": dep_time,
                            "arrival_time": arr_time,
                            "price_cny": price,
                            "origin": origin,
                            "destination": destination,
                        }
                    )
                _walk(item, depth + 1)
            if len(local) > len(best):
                best = local
            return
        if isinstance(obj, dict):
            for value in obj.values():
                _walk(value, depth + 1)

    _walk(state_obj)
    dedup = {}
    for flight in best:
        key = (
            flight.get("flight_no", ""),
            flight.get("departure_time", ""),
            flight.get("arrival_time", ""),
            flight.get("price_cny"),
        )
        dedup[key] = flight
    return sorted(dedup.values(), key=lambda x: x.get("price_cny") or 99999)


def _fetch_batch_search(session, criteria: dict, profile: dict | None):
    headers = _profile_headers(profile)
    resp = session.post(
        _BATCH_SEARCH_URL,
        json=criteria,
        headers=headers,
        timeout=20,
        **_IMPERSONATE,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_products(session, origin: str, destination: str, date_str: str):
    origin_meta = _CITY_META[origin]
    destination_meta = _CITY_META[destination]
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
                "dcityname": origin_meta["airport_name_en"],
                "acityname": destination_meta["airport_name_en"],
                "date": date_str,
            }
        ],
    }
    resp = session.post(
        _PRODUCTS_URL,
        json=payload,
        headers={"Content-Type": "application/json;charset=UTF-8"},
        timeout=20,
        **_IMPERSONATE,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_mobile_lowest_price(session, criteria: dict, profile: dict | None):
    headers = _profile_headers(profile)
    headers.setdefault("content-type", "application/json;charset=UTF-8")
    headers.setdefault("accept", "application/json")
    resp = session.post(
        f"{_MOBILE_LOWEST_PRICE_URL}?v={random.random()}",
        json=criteria,
        headers=headers,
        timeout=20,
        **_IMPERSONATE,
    )
    resp.raise_for_status()
    return resp.json()


def _parse_mobile_lowest_price(data: dict, origin: str, destination: str) -> list[dict]:
    flights = []
    for item in data.get("priceList") or []:
        price = _extract_price(item)
        if price is None:
            continue
        flights.append(
            {
                "airline": item.get("airlineName") or "携程最低价",
                "flight_no": item.get("flightNo") or item.get("flightNumber") or "",
                "departure_time": _normalize_time(item.get("departureTime") or item.get("depTime") or ""),
                "arrival_time": _normalize_time(item.get("arrivalTime") or item.get("arrTime") or ""),
                "price_cny": price,
                "origin": origin,
                "destination": destination,
            }
        )
    if flights:
        return sorted(flights, key=lambda x: x.get("price_cny") or 99999)

    price = _extract_price(data.get("lowestPrice") or data.get("minPrice") or data)
    if price is None:
        return []
    return [
        {
            "airline": "携程最低价",
            "flight_no": "",
            "departure_time": "",
            "arrival_time": "",
            "price_cny": price,
            "origin": origin,
            "destination": destination,
        }
    ]


def _run_agent_browser(args: list[str]) -> str:
    if _CDP_PORT:
        cdp_target = _normalize_cdp_target(_CDP_PORT)
        ab = shutil.which("agent-browser")
        if ab:
            parts = [ab, "--cdp", cdp_target, *args]
        else:
            parts = ["npx", "-y", "agent-browser", "--cdp", cdp_target, *args]
    else:
        parts = ["npx", "-y", "agent-browser", "--session", _BROWSER_SESSION, *args]
    cmd = " ".join(shlex.quote(part) for part in parts)
    env = dict(os.environ)
    agent_browser_home = Path(env.get("AGENT_BROWSER_HOME") or "/tmp/agent-browser-home")
    agent_browser_home.mkdir(parents=True, exist_ok=True)
    env["AGENT_BROWSER_HOME"] = str(agent_browser_home)
    env["HOME"] = str(agent_browser_home)
    shell = "/bin/zsh" if Path("/bin/zsh").exists() else "/bin/sh"
    proc = subprocess.run(
        [shell, "-lc", cmd],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"agent-browser 失败: {cmd}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return (proc.stdout or "").strip()


def _extract_last_json_blob(text: str):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("{") or line.startswith("[") or line.startswith('"'):
            try:
                return json.loads(line)
            except Exception:
                continue
    raise ValueError(f"未找到可解析 JSON 输出: {text[:500]}")


def _normalize_json_object(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _browser_dom_scrape_flights(search_url: str, origin: str, destination: str) -> list[dict]:
    pull_payload = _capture_pull_response(search_url)
    if pull_payload:
        flights = _parse_flights_from_pull_response(pull_payload, origin, destination)
        if flights:
            return flights

    if not (shutil.which("agent-browser") or shutil.which("npx")):
        raise RuntimeError("agent-browser / npx 未安装，无法执行 DOM 抓取")
    _run_agent_browser(["open", search_url])
    _run_agent_browser(["wait", "10000"])

    js_state = r"""(() => {
      const candidates = [window.__NEXT_DATA__, window.__INITIAL_STATE__, window.__serverData, window.FlightSearchData, window.GlobalFlightList];
      for (const c of candidates) {
        if (c && typeof c === 'object') {
          const s = JSON.stringify(c);
          if (s.includes('flightNo') || s.includes('price')) return s;
        }
      }
      for (const sel of ['#__NEXT_DATA__', 'script[type="application/json"]']) {
        const el = document.querySelector(sel);
        if (el && el.textContent.includes('flightNo')) return el.textContent;
      }
      return null;
    })()"""

    def collect_once() -> list[dict]:
        try:
            raw_state = _extract_last_json_blob(_run_agent_browser(["eval", js_state]))
            state_obj = _normalize_json_object(raw_state)
            flights = _extract_flights_from_state(state_obj, origin, destination)
            if flights:
                return flights
        except Exception as e:
            log.debug(f"  DOM window state 失败: {e}")

        try:
            body_text = _run_agent_browser(["get", "text", "body"])
            flights = _parse_flights_from_body_text(body_text, origin, destination)
            if flights:
                return flights
        except Exception as e:
            log.debug(f"  DOM body text 失败: {e}")
        return []

    dedup = {}
    stagnant_rounds = 0
    last_scroll_y = None

    for _ in range(8):
        for flight in collect_once():
            key = (
                flight.get("airline", ""),
                flight.get("flight_no", ""),
                flight.get("departure_time", ""),
                flight.get("arrival_time", ""),
                flight.get("price_cny"),
            )
            dedup[key] = flight

        scroll_raw = _run_agent_browser(["eval", "String(window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0)"])
        try:
            scroll_y = int(str(_normalize_json_object(_extract_last_json_blob(scroll_raw))).strip())
        except Exception:
            try:
                scroll_y = int(str(scroll_raw).splitlines()[-1].strip().strip('"'))
            except Exception:
                scroll_y = None

        before = len(dedup)
        _run_agent_browser(["scroll", "down", "2200"])
        _run_agent_browser(["wait", "1800"])

        for flight in collect_once():
            key = (
                flight.get("airline", ""),
                flight.get("flight_no", ""),
                flight.get("departure_time", ""),
                flight.get("arrival_time", ""),
                flight.get("price_cny"),
            )
            dedup[key] = flight

        after = len(dedup)
        if after == before or (scroll_y is not None and last_scroll_y == scroll_y):
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        last_scroll_y = scroll_y
        if stagnant_rounds >= 2:
            break

    return sorted(dedup.values(), key=lambda x: x.get("price_cny") or 99999)


def get_ctrip_flights_for_searches(searches, proxy_url=None, proxy_id=None):
    if not searches:
        return {}

    results = {}
    profile = _load_profile()
    session = _make_session(proxy_url=proxy_url)

    try:
        session.get(f"{_BASE_URL}/", timeout=8, **_IMPERSONATE)
    except Exception as e:
        log.debug(f"  携程 warmup 失败: {e}")

    for s in searches:
        url = s["url"]
        origin = s.get("origin", "")
        destination = s.get("destination", "")
        date_str = s.get("flight_date", "")
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

        if origin not in _CITY_META or destination not in _CITY_META:
            result["error"] = f"暂不支持路线 {origin}-{destination}"
            result["status"] = "degraded"
            results[url] = result
            continue

        criteria = _build_criteria(origin, destination, date_str, profile)
        errors = []
        auth_blocked = False
        flights = []

        log.info(f"  🏷️ 携程API: {origin}→{destination} {date_str}")

        if profile:
            try:
                batch_data = _fetch_batch_search(session, criteria, profile)
                flights = _extract_flights_from_state(batch_data, origin, destination)
                context = ((batch_data or {}).get("data") or {}).get("context") or {}
                if not flights and context.get("showAuthCode"):
                    auth_blocked = True
                    errors.append("batchSearch 返回 showAuthCode")
                    log.warning(f"  ⚠️ 携程 batchSearch 触发验证码 {origin}→{destination} {date_str}")
                elif flights:
                    log.info(f"  ✓ 携程 batchSearch: {len(flights)} 个航班")
            except Exception as e:
                errors.append(f"batchSearch: {e}")
                log.debug(f"  携程 batchSearch 失败 {origin}→{destination}: {e}")
        else:
            errors.append("未找到 ctrip_batch_profile.json")

        if not flights:
            try:
                products_data = _fetch_products(session, origin, destination, date_str)
                if ((products_data.get("data") or {}).get("error") or {}).get("msg") == "接口下线":
                    errors.append("products 接口下线")
                else:
                    flights = _extract_flights_from_state(products_data, origin, destination)
                    if flights:
                        log.info(f"  ✓ 携程 products: {len(flights)} 个航班")
            except Exception as e:
                errors.append(f"products: {e}")
                log.debug(f"  携程 products 失败 {origin}→{destination}: {e}")

        if not flights and profile:
            try:
                mobile_data = _fetch_mobile_lowest_price(session, criteria, profile)
                flights = _parse_mobile_lowest_price(mobile_data, origin, destination)
                if flights:
                    log.info(f"  ✓ 携程 m.ctrip 最低价: {len(flights)} 条")
                else:
                    errors.append("m.ctrip lowest price 返回空 priceList")
            except Exception as e:
                errors.append(f"mobile_lowest_price: {e}")
                log.debug(f"  携程 m.ctrip 最低价失败 {origin}→{destination}: {e}")

        dom_fallback_used = False
        if not flights and (_ENABLE_BROWSER_FALLBACK or _CDP_PORT):
            try:
                flights = _browser_dom_scrape_flights(_canonical_search_url(origin, destination, date_str), origin, destination)
                if flights:
                    dom_fallback_used = True
                    result["request_mode"] = "browser_dom"
                    log.info(f"  ✓ 携程 DOM fallback: {len(flights)} 个航班")
            except Exception as e:
                errors.append(f"browser_dom: {e}")
                log.debug(f"  携程 DOM fallback 失败 {origin}→{destination}: {e}")

        if flights:
            result["flights"] = flights
            result["lowest_price"] = min(f.get("price_cny") or 99999 for f in flights)
            if dom_fallback_used:
                result["diagnosis"] = {
                    "action": "refresh_profile" if auth_blocked else "keep_dom_fallback",
                    "reason": "当前结果来自 DOM fallback；如需恢复更稳的 API 结果，请刷新真实 Chrome profile",
                }
            elif auth_blocked and not any(f.get("departure_time") and f.get("arrival_time") for f in flights):
                result["diagnosis"] = {
                    "action": "refresh_profile",
                    "reason": "batchSearch 已触发验证码，当前结果来自降级接口，仅供参考",
                }
        else:
            result["error"] = " | ".join(errors) if errors else "携程 API 无有效数据"
            if auth_blocked:
                result["status"] = "blocked"
                result["block_reason"] = "captcha"
                result["retryable"] = False
                result["diagnosis"] = {
                    "action": "capture_real_chrome_profile",
                    "reason": "当前 profile 已触发验证码，需用真实 Chrome 重新捕获 session",
                }
            else:
                status, reason, retryable = classify_exception(result["error"])
                result["status"] = status
                result["block_reason"] = reason
                result["retryable"] = retryable
                if not profile:
                    result["diagnosis"] = {
                        "action": "capture_profile",
                        "reason": f"缺少 profile 文件: {_PROFILE_PATH}",
                    }
            log.warning(f"  ⚠️ 携程 API 失败 {origin}→{destination} {date_str}: {result['error']}")

        results[url] = finalize_result_status(result)
        time.sleep(random.uniform(0.3, 0.8))

    try:
        session.close()
    except Exception:
        pass
    return results
