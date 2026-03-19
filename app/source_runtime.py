"""
源状态、缓存、代理池、画像池运行时管理
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.config import (
    PROXY_FAIL_DISABLE_THRESHOLD,
    PROXY_POOL,
    PROXY_STICKY_MINUTES,
    PROXY_URL,
    SOURCE_COOLDOWN_SECONDS,
    SOURCE_MAX_CONSECUTIVE_FAILURES,
)
_SOURCE_NAMES = ("spring_api", "ctrip_api", "google_api", "browser_fallback")


def ensure_runtime_state(state: dict) -> dict:
    state.setdefault("source_health", {})
    state.setdefault("query_cache", {})
    state.setdefault("proxy_pool_status", {})
    state.setdefault("profile_assignments", {})
    state.setdefault("last_block_events", [])
    for source in _SOURCE_NAMES:
        state["source_health"].setdefault(source, {
            "status": "healthy",
            "last_success_at": None,
            "last_failure_at": None,
            "consecutive_failures": 0,
            "cooldown_until": None,
            "last_block_reason": None,
        })
    return state


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def get_source_status_snapshot(state: dict) -> dict:
    ensure_runtime_state(state)
    return state["source_health"]


def source_in_cooldown(state: dict, source: str, now_dt: datetime) -> bool:
    ensure_runtime_state(state)
    cooldown_until = _parse_dt(state["source_health"].get(source, {}).get("cooldown_until"))
    return bool(cooldown_until and cooldown_until > now_dt)


def record_source_outcome(state: dict, source: str, status: str, reason: str | None, now_dt: datetime) -> dict:
    ensure_runtime_state(state)
    health = state["source_health"].setdefault(source, {})
    health.setdefault("consecutive_failures", 0)

    if status == "ok":
        health.update({
            "status": "healthy",
            "last_success_at": now_dt.isoformat(),
            "consecutive_failures": 0,
            "cooldown_until": None,
            "last_block_reason": None,
        })
        return health

    health["last_failure_at"] = now_dt.isoformat()
    health["consecutive_failures"] = health.get("consecutive_failures", 0) + 1
    health["last_block_reason"] = reason

    if status == "blocked" or health["consecutive_failures"] >= SOURCE_MAX_CONSECUTIVE_FAILURES:
        health["status"] = "cooldown"
        health["cooldown_until"] = (now_dt + timedelta(seconds=SOURCE_COOLDOWN_SECONDS)).isoformat()
        if status == "blocked":
            state["last_block_events"].append({
                "source": source,
                "reason": reason,
                "time": now_dt.isoformat(),
            })
            state["last_block_events"] = state["last_block_events"][-20:]
    else:
        health["status"] = "degraded" if status == "degraded" else "healthy"
    return health


def force_source_cooldown(state: dict, source: str, reason: str | None, now_dt: datetime, seconds: int | None = None) -> dict:
    ensure_runtime_state(state)
    cooldown_seconds = seconds or SOURCE_COOLDOWN_SECONDS
    health = state["source_health"].setdefault(source, {})
    health.update({
        "status": "cooldown",
        "last_failure_at": now_dt.isoformat(),
        "cooldown_until": (now_dt + timedelta(seconds=cooldown_seconds)).isoformat(),
        "last_block_reason": reason,
        "consecutive_failures": max(health.get("consecutive_failures", 0), SOURCE_MAX_CONSECUTIVE_FAILURES),
    })
    state["last_block_events"].append({
        "source": source,
        "reason": reason,
        "time": now_dt.isoformat(),
        "forced": True,
    })
    state["last_block_events"] = state["last_block_events"][-20:]
    return health


def build_query_cache_key(search: dict) -> str:
    return "|".join([
        search.get("source_type", ""),
        search.get("origin", ""),
        search.get("destination", ""),
        search.get("flight_date", ""),
        "v1",
    ])


def _cache_ttl_seconds(search: dict, status: str) -> int:
    if status == "blocked":
        return SOURCE_COOLDOWN_SECONDS
    try:
        flight_date = datetime.strptime(search.get("flight_date", ""), "%Y-%m-%d").date()
        days = (flight_date - datetime.now().date()).days
    except Exception:
        days = 30

    if days <= 30:
        return 1800
    if days <= 90:
        return 7200
    return 21600


def get_cached_search_result(state: dict, search: dict, now_dt: datetime) -> dict | None:
    ensure_runtime_state(state)
    entry = state["query_cache"].get(build_query_cache_key(search))
    if not entry:
        return None
    expires_at = _parse_dt(entry.get("expires_at"))
    if not expires_at or expires_at <= now_dt:
        return None
    cached = dict(entry["result"])
    cached["from_cache"] = True
    return cached


def store_cached_search_result(state: dict, search: dict, result: dict, now_dt: datetime) -> None:
    ensure_runtime_state(state)
    ttl = _cache_ttl_seconds(search, result.get("status", "no_data"))
    cache_result = dict(result)
    cache_result["from_cache"] = False
    state["query_cache"][build_query_cache_key(search)] = {
        "expires_at": (now_dt + timedelta(seconds=ttl)).isoformat(),
        "result": cache_result,
    }


def get_proxy_choices() -> list[dict]:
    raw = PROXY_POOL or ([PROXY_URL] if PROXY_URL else [])
    if not raw:
        return [{"id": "direct", "url": ""}]
    return [{"id": f"proxy_{idx+1}", "url": url} for idx, url in enumerate(raw)]


def choose_proxy(state: dict, source: str, now_dt: datetime) -> dict:
    ensure_runtime_state(state)
    candidates = []
    for proxy in get_proxy_choices():
        status = state["proxy_pool_status"].setdefault(proxy["id"], {
            "last_used_at": None,
            "success_count": 0,
            "failure_count": 0,
            "blocked_count": 0,
            "disabled_until": None,
            "last_status": "unknown",
            "last_source": None,
        })
        disabled_until = _parse_dt(status.get("disabled_until"))
        if disabled_until and disabled_until > now_dt:
            continue
        score = status["success_count"] - status["failure_count"] - status["blocked_count"] * 2
        last_used = _parse_dt(status.get("last_used_at")) or datetime.min
        candidates.append((score, last_used, proxy))
    if not candidates:
        return {"id": "direct", "url": ""}
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]


def record_proxy_outcome(state: dict, proxy_id: str | None, source: str, status: str, now_dt: datetime) -> None:
    if not proxy_id:
        return
    ensure_runtime_state(state)
    proxy_state = state["proxy_pool_status"].setdefault(proxy_id, {
        "last_used_at": None,
        "success_count": 0,
        "failure_count": 0,
        "blocked_count": 0,
        "disabled_until": None,
        "last_status": "unknown",
        "last_source": None,
    })
    proxy_state["last_used_at"] = now_dt.isoformat()
    proxy_state["last_status"] = status
    proxy_state["last_source"] = source
    if status == "ok":
        proxy_state["success_count"] += 1
        proxy_state["blocked_count"] = 0
    elif status == "blocked":
        proxy_state["failure_count"] += 1
        proxy_state["blocked_count"] += 1
        if proxy_state["blocked_count"] >= PROXY_FAIL_DISABLE_THRESHOLD:
            proxy_state["disabled_until"] = (now_dt + timedelta(minutes=PROXY_STICKY_MINUTES)).isoformat()
    else:
        proxy_state["failure_count"] += 1


def penalize_proxy(state: dict, proxy_id: str | None, source: str, now_dt: datetime, hard: bool = False) -> None:
    if not proxy_id:
        return
    ensure_runtime_state(state)
    proxy_state = state["proxy_pool_status"].setdefault(proxy_id, {
        "last_used_at": None,
        "success_count": 0,
        "failure_count": 0,
        "blocked_count": 0,
        "disabled_until": None,
        "last_status": "unknown",
        "last_source": None,
    })
    proxy_state["last_used_at"] = now_dt.isoformat()
    proxy_state["last_source"] = source
    proxy_state["last_status"] = "blocked" if hard else "degraded"
    proxy_state["failure_count"] += 1
    if hard:
        proxy_state["blocked_count"] += 1
    if proxy_state["blocked_count"] >= PROXY_FAIL_DISABLE_THRESHOLD or hard:
        proxy_state["disabled_until"] = (now_dt + timedelta(minutes=PROXY_STICKY_MINUTES)).isoformat()


def choose_profile_id(state: dict, source: str, available_profiles: list[str], now_dt: datetime) -> str:
    ensure_runtime_state(state)
    assignments = state["profile_assignments"]
    existing = assignments.get(source)
    if existing and existing.get("profile_id") in available_profiles:
        assigned_at = _parse_dt(existing.get("assigned_at"))
        if assigned_at and now_dt - assigned_at < timedelta(minutes=PROXY_STICKY_MINUTES):
            return existing["profile_id"]

    usage = []
    for profile_id in available_profiles:
        last_used = datetime.min
        for item in assignments.values():
            if item.get("profile_id") == profile_id:
                last_used = max(last_used, _parse_dt(item.get("assigned_at")) or datetime.min)
        usage.append((last_used, profile_id))
    usage.sort(key=lambda item: item[0])
    profile_id = usage[0][1]
    assignments[source] = {"profile_id": profile_id, "assigned_at": now_dt.isoformat()}
    return profile_id


def proxy_pool_summary(state: dict) -> dict:
    ensure_runtime_state(state)
    return state["proxy_pool_status"]


def mark_skip_browser_until(state: dict, now_dt: datetime, seconds: int) -> None:
    ensure_runtime_state(state)
    state["browser_skip_until"] = (now_dt + timedelta(seconds=seconds)).isoformat()


def browser_skip_active(state: dict, now_dt: datetime) -> bool:
    ensure_runtime_state(state)
    skip_until = _parse_dt(state.get("browser_skip_until"))
    return bool(skip_until and skip_until > now_dt)
