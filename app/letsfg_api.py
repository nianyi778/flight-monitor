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
_LETSFG_TIMEOUT = int(os.getenv("LETSFG_TIMEOUT") or "90")
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
    if not _LETSFG_BIN:
        raise FileNotFoundError("letsfg CLI 未安装")
    mode = _pick_cli_mode()
    subcommand = "search" if mode == "cloud" else "search-local"
    cmd = [_LETSFG_BIN, subcommand, origin, destination, date_str, "--json"]
    env = dict(os.environ)
    env.setdefault("CHROME_PATH", "/root/.cache/ms-playwright/chromium-1208/chrome-linux/chrome")
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
        timeout=_LETSFG_TIMEOUT,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "letsfg failed").strip())
    output = (proc.stdout or "").strip()
    json_starts = [idx for idx in (output.find("{"), output.find("[")) if idx != -1]
    if json_starts:
        return json.loads(output[min(json_starts):])
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        if line.startswith("{") or line.startswith("["):
            return json.loads(line)
    return json.loads(output)


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
        candidates.append(offer["outbound"])
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
