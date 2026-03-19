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

        json_match = re.search(r"\{[\s\S]*\}", content)
        if json_match:
            data = json.loads(json_match.group())
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
