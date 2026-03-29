"""
LetsFG 航班搜索集成
- 优先调用 letsfg CLI 的 JSON 输出
- 支持本地模式(search-local)与云模式(search)
- 未安装 letsfg 时自动降级，不影响主系统
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from app.anti_bot import classify_exception, finalize_result_status, make_result
from app.config import log

_LETSFG_BIN = os.getenv("LETSFG_BIN") or shutil.which("letsfg")
_LETSFG_TIMEOUT = int(os.getenv("LETSFG_TIMEOUT") or "200")
_LETSFG_MODE = os.getenv("LETSFG_MODE", "auto").lower()  # auto/local/cloud


def _currency_to_cny(price, currency: str | None):
    currency = (currency or "").upper()
    if price in (None, ""):
        return None
    try:
        value = float(price)
    except Exception:
        return None
    if currency in {"CNY", "RMB", "CNH"}:
        return round(value)
    try:
        from app.spring_api import get_exchange_rates

        usd_cny, jpy_cny = get_exchange_rates()
    except Exception:
        usd_cny, jpy_cny = 7.2, 0.048
    rates = {
        "USD": usd_cny,
        "JPY": jpy_cny,
        "EUR": 7.8,
        "GBP": 9.1,
        "HKD": 0.92,
        "SGD": 5.35,
    }
    rate = rates.get(currency)
    if rate is None:
        return round(value)
    return round(value * rate)


def _pick_cli_mode() -> str:
    if _LETSFG_MODE in {"local", "cloud"}:
        return _LETSFG_MODE
    if os.getenv("LETSFG_API_KEY"):
        return "cloud"
    return "local"


def _run_letsfg(origin: str, destination: str, date_str: str) -> dict:
    """
    直接调用 letsfg Python API（不启动子进程，避免 Chrome 弹窗风暴）。
    有 LETSFG_API_KEY 时激活 Cloud Run 后端（Kiwi/Amadeus/Aviasales 等聚合），
    无需浏览器，单次 HTTP 调用返回结果。
    本地 connector 限制 max_browsers=1 避免同时弹出大量窗口。
    """
    try:
        import asyncio
        from letsfg.local import search_local
    except ImportError:
        raise FileNotFoundError("letsfg 未安装: pip install 'letsfg[cli]'")

    async def _search():
        return await asyncio.wait_for(
            search_local(
                origin=origin,
                destination=destination,
                date_from=date_str,
                adults=1,
                currency="CNY",
                limit=30,
                max_browsers=1,
            ),
            timeout=_LETSFG_TIMEOUT,
        )

    return asyncio.run(_search())


def _normalize_time(value):
    if not value:
        return ""
    text = str(value)
    if "T" in text:
        text = text.split("T", 1)[1]
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.strftime("%H:%M")
    except Exception:
        pass
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return text[:5]


def _extract_segment(offer: dict) -> tuple[str, str, str, str]:
    airline = ""
    flight_no = ""
    dep = ""
    arr = ""
    candidates = []
    for key in ("segments", "legs", "slices", "itinerary", "journeys"):
        value = offer.get(key)
        if isinstance(value, list):
            candidates.extend([v for v in value if isinstance(v, dict)])
    if not candidates and isinstance(offer.get("outbound"), dict):
        ob = offer["outbound"]
        # letsfg cloud format: outbound.segments[]
        ob_segs = ob.get("segments") or ob.get("legs") or []
        if ob_segs and isinstance(ob_segs[0], dict):
            candidates.extend(ob_segs)
        else:
            candidates.append(ob)
    seg = candidates[0] if candidates else offer
    airline = (
        seg.get("airline")
        or seg.get("carrier")
        or seg.get("marketing_carrier")
        or seg.get("name")
        or ""
    )
    if isinstance(airline, dict):
        airline = airline.get("name") or airline.get("code") or ""
    flight_no = (
        seg.get("flight_no")
        or seg.get("flightNumber")
        or seg.get("number")
        or ""
    )
    dep = _normalize_time(
        seg.get("departure_time")
        or seg.get("departure")
        or seg.get("depart_at")
        or seg.get("departureAt")
        or offer.get("departure_time")
    )
    arr = _normalize_time(
        seg.get("arrival_time")
        or seg.get("arrival")
        or seg.get("arrive_at")
        or seg.get("arrivalAt")
        or offer.get("arrival_time")
    )
    return str(airline), str(flight_no), dep, arr


def _parse_offers(payload, origin: str, destination: str) -> list[dict]:
    if isinstance(payload, list):
        offers = payload
        currency = None
    else:
        offers = payload.get("offers") or payload.get("results") or payload.get("data") or []
        currency = payload.get("currency")
    flights = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        price = (
            offer.get("price")
            or offer.get("amount")
            or (offer.get("total") or {}).get("amount")
            or (offer.get("fare") or {}).get("amount")
        )
        offer_currency = (
            offer.get("currency")
            or (offer.get("total") or {}).get("currency")
            or (offer.get("fare") or {}).get("currency")
            or currency
        )
        price_cny = _currency_to_cny(price, offer_currency)
        if price_cny is None:
            continue
        airline, flight_no, dep, arr = _extract_segment(offer)
        flights.append(
            {
                "airline": airline or "LetsFG",
                "flight_no": flight_no,
                "departure_time": dep,
                "arrival_time": arr,
                "price_cny": price_cny,
                "original_price": price,
                "original_currency": offer_currency,
                "origin": origin,
                "destination": destination,
                "via": "",  # LetsFG 不提供中转点信息
            }
        )
    dedup = {}
    for flight in flights:
        key = (
            flight.get("airline"),
            flight.get("flight_no"),
            flight.get("departure_time"),
            flight.get("arrival_time"),
            flight.get("price_cny"),
        )
        dedup[key] = flight
    return sorted(dedup.values(), key=lambda x: x.get("price_cny") or 99999)


def get_letsfg_flights_for_searches(searches, proxy_url=None, proxy_id=None):
    if not searches:
        return {}

    results = {}
    for s in searches:
        url = s["url"]
        origin = s.get("origin", "")
        destination = s.get("destination", "")
        date_str = s.get("flight_date", "")
        result = make_result(
            source=f"LetsFG_{origin}_{destination}",
            url=url,
            flight_date=date_str,
            proxy_id=proxy_id,
            request_mode="letsfg_cli",
            origin=origin,
            destination=destination,
        )

        if not (origin and destination and date_str):
            result["error"] = "缺少 origin/destination/date"
            result["status"] = "degraded"
            results[url] = result
            continue

        try:
            payload = _run_letsfg(origin, destination, date_str)
            flights = _parse_offers(payload, origin, destination)
            if flights:
                result["flights"] = flights
                result["lowest_price"] = flights[0]["price_cny"]
                log.info(f"  ✓ LetsFG: {origin}→{destination} {date_str} {len(flights)} 个航班, 最低 ¥{result['lowest_price']}")
            else:
                result["error"] = "LetsFG 返回空结果"
        except Exception as e:
            status, reason, retryable = classify_exception(e)
            result["status"] = status
            result["block_reason"] = reason
            result["retryable"] = retryable
            result["error"] = f"LetsFG: {e}"
            if "未安装" in result["error"]:
                result["status"] = "degraded"
                result["block_reason"] = None
                result["retryable"] = True
                result["diagnosis"] = {
                    "action": "install_letsfg",
                    "reason": "未检测到 letsfg CLI，可选安装后启用该数据源",
                }
            log.warning(f"  ⚠️ LetsFG 失败 {origin}→{destination} {date_str}: {e}")

        results[url] = finalize_result_status(result)
    return results
