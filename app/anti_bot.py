"""
反作弊识别与统一结果元数据
"""

from __future__ import annotations

from typing import Any


_BLOCK_PATTERNS = {
    "captcha": ["captcha", "验证码", "verify you are human", "验证您是真人"],
    "login_wall": ["登录", "login", "sign in", "请先登录"],
    "rate_limit": ["访问过于频繁", "too many requests", "rate limit", "频繁"],
    "waf": ["access denied", "forbidden", "waf", "blocked", "拒绝访问"],
    "empty_page": ["暂无结果", "no flights", "no result", "没有找到"],
}


def make_result(source: str, url: str, flight_date: str = "", **kwargs: Any) -> dict:
    result = {
        "flights": [],
        "lowest_price": None,
        "error": None,
        "source": source,
        "url": url,
        "flight_date": flight_date,
        "status": "no_data",
        "block_reason": None,
        "retryable": True,
        "request_mode": "api",
        "from_cache": False,
        "proxy_id": None,
        "profile_id": None,
        "cooldown_recommended_seconds": None,
    }
    result.update(kwargs)
    return result


def finalize_result_status(result: dict) -> dict:
    if result.get("flights"):
        result["status"] = "ok"
        result["retryable"] = True
        result["block_reason"] = None
    elif result.get("status") not in {"blocked", "degraded"}:
        error_text = str(result.get("error") or "").lower()
        if any(
            token in error_text
            for token in (
                "captcha",
                "验证码",
                "login",
                "forbidden",
                "rate limit",
                "waf",
                "access denied",
            )
        ):
            result["status"] = "blocked"
            result["retryable"] = False
            result["block_reason"] = infer_block_reason(error_text)
        else:
            result["status"] = "no_data"
            result["retryable"] = True
    return result


def infer_block_reason(text: str) -> str:
    lowered = (text or "").lower()
    for reason, patterns in _BLOCK_PATTERNS.items():
        if any(p.lower() in lowered for p in patterns):
            return reason
    return "unknown"


def classify_http_status(status_code: int) -> tuple[str, str | None, bool]:
    if status_code == 403:
        return "blocked", "waf", False
    if status_code == 405:
        # 405 often retryable after WAF warmup (e.g., Spring Airlines)
        return "blocked", "waf", True
    if status_code == 429:
        return "blocked", "rate_limit", True  # rate limit is transient
    if status_code >= 500:
        return "degraded", "network", True
    return "no_data", None, True


def classify_exception(exc: Exception | str) -> tuple[str, str, bool]:
    text = str(exc).lower()
    # Permanent blocks (captcha, explicit denial)
    if any(token in text for token in ("captcha", "forbidden", "access denied")):
        return "blocked", infer_block_reason(text), False
    # Retryable blocks (WAF, rate limit, 405)
    if any(token in text for token in ("405", "429", "waf", "rate limit")):
        return "blocked", infer_block_reason(text), True
    # 403 is ambiguous — mark blocked but retryable for proxy rotation
    if "403" in text:
        return "blocked", infer_block_reason(text), True
    return "degraded", "network", True


def inspect_browser_page(
    text: str, title: str = "", url: str = ""
) -> tuple[str, str | None]:
    haystack = " ".join(part for part in [text, title, url] if part).lower()
    for reason, patterns in _BLOCK_PATTERNS.items():
        if reason == "empty_page":
            continue
        if any(p.lower() in haystack for p in patterns):
            return "blocked", reason
    if not haystack.strip():
        return "blocked", "empty_page"
    return "ok", None
