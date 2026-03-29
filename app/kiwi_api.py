"""
Kiwi.com 航班搜索（通过 letsfg Kiwi connector）
- 使用 Kiwi GraphQL API，零认证，无需 API key
- 覆盖 NRT→PVG 主要航司：MU/CA/NH/9C/HO/FM/GK/IJ
- 纯 HTTP，无浏览器，单次调用 10~30s
"""

from __future__ import annotations

import asyncio
from datetime import date as dt_date

from app.anti_bot import classify_exception, finalize_result_status, make_result
from app.config import log

_KIWI_TIMEOUT = 45  # Kiwi GraphQL 通常 10-30s


def _parse_offers(offers, origin: str, destination: str) -> list[dict]:
    flights = []
    for offer in offers:
        try:
            price_cny = offer.price
            if not price_cny:
                continue

            outbound = offer.outbound
            if not outbound or not outbound.segments:
                continue

            seg = outbound.segments[0]
            airline = seg.airline_name or seg.airline or ""
            flight_no = seg.flight_no or ""
            dep = seg.departure.strftime("%H:%M") if seg.departure else ""
            arr = seg.arrival.strftime("%H:%M") if seg.arrival else ""

            flights.append({
                "airline": airline or "Kiwi",
                "flight_no": flight_no,
                "departure_time": dep,
                "arrival_time": arr,
                "price_cny": round(price_cny),
                "original_price": price_cny,
                "original_currency": offer.currency or "CNY",
                "origin": seg.origin or origin,
                "destination": seg.destination or destination,
                "via": "",
                "stops": outbound.stopovers or 0,
            })
        except Exception:
            continue

    # 去重：同航班不同价格保留最低
    dedup: dict[tuple, dict] = {}
    for f in flights:
        key = (f["airline"], f["flight_no"], f["departure_time"], f["arrival_time"])
        if key not in dedup or f["price_cny"] < dedup[key]["price_cny"]:
            dedup[key] = f

    return sorted(dedup.values(), key=lambda x: x["price_cny"])


def _run_kiwi(origin: str, destination: str, date_str: str) -> list[dict]:
    try:
        from letsfg.connectors.kiwi import KiwiConnectorClient
        from letsfg.models.flights import FlightSearchRequest
    except ImportError:
        raise FileNotFoundError("letsfg 未安装: pip install 'letsfg[cli]'")

    flight_date = dt_date.fromisoformat(date_str)

    async def _search():
        client = KiwiConnectorClient(timeout=_KIWI_TIMEOUT)
        try:
            req = FlightSearchRequest(
                origin=origin.upper(),
                destination=destination.upper(),
                date_from=flight_date,
                adults=1,
                currency="CNY",
                limit=50,
                max_stopovers=0,  # 仅直飞
            )
            resp = await asyncio.wait_for(
                client.search_flights(req),
                timeout=_KIWI_TIMEOUT,
            )
            return resp.offers or []
        finally:
            await client.close()

    return asyncio.run(_search())


def get_kiwi_flights_for_searches(searches, proxy_url=None, proxy_id=None):
    if not searches:
        return {}

    results = {}
    for s in searches:
        url = s["url"]
        origin = s.get("origin", "")
        destination = s.get("destination", "")
        date_str = s.get("flight_date", "")

        result = make_result(
            source=f"Kiwi_{origin}_{destination}",
            url=url,
            flight_date=date_str,
            proxy_id=proxy_id,
            request_mode="kiwi_graphql",
            origin=origin,
            destination=destination,
        )

        if not (origin and destination and date_str):
            result["error"] = "缺少 origin/destination/date"
            result["status"] = "degraded"
            results[url] = result
            continue

        try:
            offers = _run_kiwi(origin, destination, date_str)
            flights = _parse_offers(offers, origin, destination)
            if flights:
                result["flights"] = flights
                result["lowest_price"] = flights[0]["price_cny"]
                log.info(
                    f"  ✓ Kiwi: {origin}→{destination} {date_str} "
                    f"{len(flights)} 个航班, 最低 ¥{result['lowest_price']}"
                )
            else:
                result["error"] = "Kiwi 返回空结果"
        except Exception as e:
            status, reason, retryable = classify_exception(e)
            result["status"] = status
            result["block_reason"] = reason
            result["retryable"] = retryable
            result["error"] = f"Kiwi: {e}"
            if "未安装" in result["error"]:
                result["status"] = "degraded"
                result["block_reason"] = None
                result["retryable"] = True
                result["diagnosis"] = {
                    "action": "install_letsfg",
                    "reason": "未检测到 letsfg，可选安装后启用 Kiwi 数据源",
                }
            log.warning(f"  ⚠️ Kiwi 失败 {origin}→{destination} {date_str}: {e}")

        results[url] = finalize_result_status(result)
    return results
