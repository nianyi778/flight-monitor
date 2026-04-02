"""
搜索引擎模块 — 搜索去重、缓存、API 调用、结果记录
从 scheduler.py 提取，降低 God Module 复杂度
"""

import asyncio

from app.config import now_jst, log
from app.matcher import get_search_urls
from app.source_runtime import (
    choose_proxy,
    force_source_cooldown,
    get_cached_search_result,
    get_source_status_snapshot,
    penalize_proxy,
    record_check_metric_event,
    record_proxy_outcome,
    record_source_outcome,
    source_in_cooldown,
    store_cached_search_result,
)


def collect_unique_searches(trips):
    url_map = {}
    trip_search_map = {}

    for trip in trips:
        searches = get_search_urls(trip)
        trip_search_map[trip["id"]] = []
        for s in searches:
            url = s["url"]
            if url not in url_map:
                url_map[url] = {"search": s, "trip_ids": []}
            url_map[url]["trip_ids"].append(trip["id"])
            trip_search_map[trip["id"]].append(url)

    return url_map, trip_search_map


def load_cached_results(state, searches):
    now_dt = now_jst()
    cached = {}
    remaining = []
    source_name_map = {
        "kiwi": "kiwi_api",
        "google": "google_api",
        "spring": "spring_api",
    }
    for search in searches:
        hit = get_cached_search_result(state, search, now_dt)
        if hit:
            hit["source_runtime"] = source_name_map.get(
                search.get("source_type"), hit.get("source_runtime", "unknown")
            )
            cached[search["url"]] = hit
        else:
            remaining.append(search)
    return cached, remaining


def record_results_for_source(state, source_name, results, searches):
    now_dt = now_jst()
    by_url = {s["url"]: s for s in searches}
    saw_ok = False
    saw_bad = False
    last_reason = None

    for url, result in results.items():
        result["source_runtime"] = source_name
        search = by_url.get(url)
        if search:
            store_cached_search_result(state, search, result, now_dt)
        status = result.get("status", "no_data")
        if status == "ok":
            saw_ok = True
        elif status in {"blocked", "degraded"}:
            saw_bad = True
            last_reason = result.get("block_reason") or result.get("error")
        diagnosis = result.get("diagnosis") or {}
        action = diagnosis.get("action")
        if action == "cooldown":
            saw_bad = True
            last_reason = diagnosis.get("reason") or last_reason
            force_source_cooldown(
                state,
                source_name,
                diagnosis.get("reason") or last_reason,
                now_dt,
                seconds=diagnosis.get("retry_after_seconds") or None,
            )
        elif action == "switch_proxy":
            penalize_proxy(
                state, result.get("proxy_id"), source_name, now_dt, hard=True
            )
        elif action == "raise_alert":
            state.setdefault("runtime_alerts", []).append(
                {
                    "source": source_name,
                    "reason": diagnosis.get("reason") or result.get("error"),
                    "time": now_dt.isoformat(),
                }
            )
            state["runtime_alerts"] = state["runtime_alerts"][-20:]
        record_proxy_outcome(state, result.get("proxy_id"), source_name, status, now_dt)

    if saw_ok:
        record_source_outcome(state, source_name, "ok", None, now_dt)
    elif saw_bad:
        status = next(
            (
                r.get("status")
                for r in results.values()
                if r.get("status") in {"blocked", "degraded"}
            ),
            "degraded",
        )
        record_source_outcome(state, source_name, status, last_reason, now_dt)


def log_request_result(result, trip_ids=None):
    trip_ids = trip_ids or []
    log.info(
        "source=%s mode=%s route=%s-%s date=%s status=%s block=%s cache=%s proxy=%s profile=%s flights=%s trips=%s",
        result.get("source", ""),
        result.get("request_mode", ""),
        result.get("origin", "") or "",
        result.get("destination", "") or "",
        result.get("flight_date", ""),
        result.get("status", ""),
        result.get("block_reason", ""),
        result.get("from_cache", False),
        result.get("proxy_id", ""),
        result.get("profile_id", ""),
        len(result.get("flights", [])),
        ",".join(str(t) for t in trip_ids),
    )


async def execute_api_searches(state, kiwi_searches, google_searches):
    """Execute Kiwi + Google API searches, return merged {url: result} dict."""
    from app.notifier import tg_send

    all_analysis = {}

    if kiwi_searches:
        from app.kiwi_api import get_kiwi_flights_for_searches

        cached, remaining = load_cached_results(state, kiwi_searches)
        all_analysis.update(cached)
        if not source_in_cooldown(state, "kiwi_api", now_jst()) and remaining:
            proxy = choose_proxy(state, "kiwi_api", now_jst())
            fetched = await asyncio.to_thread(
                get_kiwi_flights_for_searches,
                remaining,
                proxy_url=proxy.get("url"),
                proxy_id=proxy.get("id"),
            )
            all_analysis.update(fetched)
            record_results_for_source(state, "kiwi_api", fetched, remaining)

    if google_searches:
        from app.google_flights_api import get_google_flights_for_searches

        cached, remaining = load_cached_results(state, google_searches)
        all_analysis.update(cached)
        if not source_in_cooldown(state, "google_api", now_jst()) and remaining:
            proxy = choose_proxy(state, "google_api", now_jst())
            fetched = await asyncio.to_thread(
                get_google_flights_for_searches,
                remaining,
                proxy_url=proxy.get("url"),
                proxy_id=proxy.get("id"),
            )
            all_analysis.update(fetched)
            record_results_for_source(state, "google_api", fetched, remaining)

        google_health = get_source_status_snapshot(state).get("google_api", {})
        if google_health.get("status") in ("cooldown", "degraded"):
            from datetime import datetime as _dt

            last_alert_str = state.get("_google_coverage_alert_at")
            last_alert = _dt.fromisoformat(last_alert_str) if last_alert_str else None
            if last_alert is None or (now_jst() - last_alert).total_seconds() > 3600:
                reason = google_health.get("last_block_reason") or "Chrome/CDP 连接失败"
                tg_send(
                    "⚠️ *Google Flights 覆盖降级*\n"
                    "Chrome 容器不可用，以下航司价格暂时无法监控：\n"
                    "• JAL (JL)\n• Peach Aviation (MM)\n• Jetstar Japan (GK)\n\n"
                    f"原因: `{reason}`\n"
                    "Spring + Kiwi 渠道仍正常运行。"
                )
                state["_google_coverage_alert_at"] = now_jst().isoformat()
                log.warning(f"⚠️ Google 覆盖降级告警已推送: {reason}")

    return all_analysis
