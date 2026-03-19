"""
LLM 视觉分析模块
- 全部数据源：gpt-4o-mini + detail:low（经测试，mini 在此场景比 4o 更稳定，成本低95%）
- 携程截图：短 prompt，直接提取 CNY 价格
- Google JP 截图：带日元换算 prompt
"""

import base64
import json
import re
import time

import requests

from app.anti_bot import infer_block_reason, make_result
from app.config import (
    LLM_BASE_URL, LLM_API_KEY, LLM_MODEL,
    now_jst, log,
)


def image_to_base64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


# 携程用的简洁 prompt（gpt-4o-mini 够用）
_CTRIP_PROMPT = """提取截图中所有直达航班，严格返回JSON（无其他文字）：
{"flights":[{"airline":"航空公司","flight_no":"航班号","departure_time":"HH:MM","arrival_time":"HH:MM","price_cny":1234,"origin":"NRT","destination":"PVG"}],"lowest_price":1234,"error":null}
price_cny 为人民币整数。无结果返回 {"flights":[],"lowest_price":null,"error":"原因"}"""

_PAGE_CLASSIFIER_PROMPT = """你在做机票抓取页面分类。请严格返回 JSON：
{"page_state":"normal|blocked|login_wall|captcha|empty|partial","reason":"简短原因","confidence":0.0}

判定规则：
- normal: 正常航班结果页，能继续提取价格
- blocked: 明显被风控/拒绝访问
- login_wall: 要求登录后才能继续
- captcha: 人机验证、验证码
- empty: 页面无航班结果或空白异常
- partial: 页面在加载中、信息不完整、可稍后重试
不要输出任何额外文字。"""

_FAILURE_DIAGNOSER_PROMPT = """你在为机票抓取系统诊断失败原因。请严格返回 JSON：
{"action":"retry|cooldown|switch_proxy|skip_browser|raise_alert","reason":"简短原因","retry_after_seconds":300}

决策规则：
- retry: 短暂网络问题，可快速重试
- cooldown: 明显风控/限流/登录墙，需要暂停
- switch_proxy: 更像出口质量问题
- skip_browser: 页面不适合继续截图，但其他数据源仍可用
- raise_alert: 页面结构变化或程序逻辑异常，需要开发者关注
不要输出任何额外文字。"""


def _google_jp_prompt():
    """动态生成 Google JP prompt，使用当天实时汇率（每日刷新一次）。"""
    from app.spring_api import get_exchange_rates
    _, jpy_cny = get_exchange_rates()
    return (
        "提取截图中所有直达航班，严格返回JSON（无其他文字）：\n"
        '{"flights":[{"airline":"航空公司","flight_no":"","departure_time":"HH:MM","arrival_time":"HH:MM",'
        '"price_cny":1234,"original_price":20000,"original_currency":"JPY","origin":"NRT","destination":"PVG"}],'
        '"lowest_price":1234,"error":null}\n'
        f"注意：截图为日文，价格为日元(JPY)。price_cny按1JPY={jpy_cny:.5f}CNY换算为人民币整数。original_price保留日元原价。\n"
        '无结果返回 {"flights":[],"lowest_price":null,"error":"原因"}'
    )


def _call_llm(prompt, img_b64, model, detail="low", max_retries=3):
    """调用 LLM 视觉 API（带重试）"""
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {
                                "url": f"data:image/png;base64,{img_b64}",
                                "detail": detail,
                            }},
                        ],
                    }],
                    "max_tokens": 1000,
                    "temperature": 0,
                },
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

        except Exception as e:
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 5
                log.warning(f"  LLM 第{attempt+1}次失败，{wait}s 后重试: {e}")
                time.sleep(wait)
            else:
                raise


def _extract_json(content):
    json_match = re.search(r"\{[\s\S]*\}", content)
    if not json_match:
        return None
    try:
        return json.loads(json_match.group())
    except Exception:
        return None


def classify_screenshot_page(screenshot_info):
    """先判断页面状态，避免把风控页送进价格提取。"""
    source = screenshot_info.get("name", "")
    heuristic_text = " ".join(
        str(screenshot_info.get(key, "") or "")
        for key in ("title", "body_text", "error")
    ).lower()

    if heuristic_text:
        if any(token in heuristic_text for token in ("captcha", "验证码", "verify", "人机验证")):
            return {"page_state": "captcha", "reason": "规则识别到验证码", "confidence": 0.99}
        if any(token in heuristic_text for token in ("登录", "login", "sign in")):
            return {"page_state": "login_wall", "reason": "规则识别到登录墙", "confidence": 0.99}
        if any(token in heuristic_text for token in ("forbidden", "access denied", "拒绝访问", "waf", "blocked")):
            return {"page_state": "blocked", "reason": "规则识别到风控拦截", "confidence": 0.99}

    if not screenshot_info.get("path") or not LLM_API_KEY:
        return {"page_state": "normal", "reason": "无截图或未配置LLM，走默认流程", "confidence": 0.5}

    img_b64 = image_to_base64(screenshot_info["path"])
    try:
        content = _call_llm(_PAGE_CLASSIFIER_PROMPT, img_b64, LLM_MODEL, detail="low", max_retries=2)
        data = _extract_json(content)
        if data and data.get("page_state"):
            return data
    except Exception as e:
        log.warning(f"页面分类失败，回退规则: {e}")

    return {"page_state": "normal", "reason": "分类失败，保守放行", "confidence": 0.3}


def diagnose_failure_context(result, screenshot_info=None):
    """对失败结果做动作诊断；LLM 失败时退回规则诊断。"""
    error = str(result.get("error") or "")
    status = result.get("status", "")
    block_reason = result.get("block_reason", "")
    combined = " ".join(filter(None, [status, block_reason, error])).lower()

    if any(token in combined for token in ("captcha", "login", "waf", "rate_limit", "forbidden", "access denied")):
        return {"action": "cooldown", "reason": block_reason or infer_block_reason(combined), "retry_after_seconds": 1800}
    if any(token in combined for token in ("timeout", "network", "connection reset", "temporarily")):
        return {"action": "retry", "reason": "疑似短暂网络问题", "retry_after_seconds": 300}
    if any(token in combined for token in ("selector", "json", "schema", "parse", "非json", "结构")):
        return {"action": "raise_alert", "reason": "页面结构或解析逻辑异常", "retry_after_seconds": 0}
    if result.get("request_mode") == "browser" and status in {"blocked", "degraded"}:
        return {"action": "switch_proxy", "reason": "浏览器链路异常，优先切换代理", "retry_after_seconds": 900}

    if not LLM_API_KEY:
        return {"action": "retry", "reason": "默认短重试", "retry_after_seconds": 300}

    try:
        context = {
            "source": result.get("source"),
            "status": status,
            "block_reason": block_reason,
            "error": error[:300],
            "request_mode": result.get("request_mode"),
            "profile_id": result.get("profile_id"),
            "proxy_id": result.get("proxy_id"),
            "page_hint": {
                "title": screenshot_info.get("title") if screenshot_info else "",
                "body_text": (screenshot_info.get("body_text", "")[:500] if screenshot_info else ""),
            },
        }
        content = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": f"{_FAILURE_DIAGNOSER_PROMPT}\n\n上下文:\n{json.dumps(context, ensure_ascii=False)}"}],
                "max_tokens": 200,
                "temperature": 0,
            },
            timeout=30,
        )
        content = content.json()["choices"][0]["message"]["content"]
        data = _extract_json(content)
        if data and data.get("action"):
            return data
    except Exception as e:
        log.warning(f"失败诊断LLM失败，回退规则: {e}")

    return {"action": "retry", "reason": "默认短重试", "retry_after_seconds": 300}


def analyze_screenshot(screenshot_info):
    """根据数据源自动选择模型和prompt"""
    img_b64 = image_to_base64(screenshot_info["path"])
    source = screenshot_info.get("name", "")

    # 全部用 mini + low detail（经测试，mini 比 full 更稳定）
    if "Google" in source or "google" in source:
        model = "gpt-4o-mini"
        prompt = _google_jp_prompt()
        detail = "low"
    else:
        model = "gpt-4o-mini"
        prompt = _CTRIP_PROMPT
        detail = "low"

    try:
        content = _call_llm(prompt, img_b64, model, detail)
        data = _extract_json(content)
        if data:
            # 价格校验
            valid_flights = []
            for f in data.get("flights", []):
                price = f.get("price_cny")
                if price and 200 <= price <= 8000:
                    valid_flights.append(f)
                elif price:
                    log.warning(f"  过滤异常价格: ¥{price} ({f.get('airline', '')})")
            data["flights"] = valid_flights
            data["lowest_price"] = min((f["price_cny"] for f in valid_flights), default=None)
            data["_model"] = model
            return data
        else:
            return {"flights": [], "lowest_price": None, "error": f"LLM返回非JSON: {content[:200]}"}

    except Exception as e:
        log.error(f"LLM 分析失败（{model}）: {e}")
        return {"flights": [], "lowest_price": None, "error": str(e)}


def analyze_all_screenshots(screenshots):
    """分析所有截图"""
    results = {"outbound": [], "return": [], "timestamp": now_jst().isoformat()}

    for ss in screenshots:
        log.info(f"🤖 分析: {ss['name']} {ss['label']}")
        analysis = analyze_screenshot(ss)
        analysis["source"] = ss["name"]
        analysis["url"] = ss["url"]
        analysis["flight_date"] = ss.get("flight_date", "")

        if ss["direction"] == "outbound":
            results["outbound"].append(analysis)
        else:
            results["return"].append(analysis)

        if analysis.get("error"):
            log.warning(f"  ⚠️ {analysis['error']}")
        elif analysis.get("flights"):
            model_tag = analysis.get("_model", "?")
            log.info(f"  ✓ {len(analysis['flights'])} 个航班, 最低 ¥{analysis.get('lowest_price', '?')} ({model_tag})")

    return results
