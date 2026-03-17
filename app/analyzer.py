"""
LLM 视觉分析模块 - GPT-4o 截图分析 + 价格校验
"""

import base64
import json
import re

import requests

from app.config import (
    LLM_BASE_URL, LLM_API_KEY, LLM_MODEL,
    now_jst, log,
)


def image_to_base64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def analyze_screenshot(screenshot_info):
    """用 GPT-4o 分析截图，提取结构化航班数据"""
    img_b64 = image_to_base64(screenshot_info["path"])
    direction = "去程（东京→上海）" if screenshot_info["direction"] == "outbound" else "回程（上海→东京）"

    prompt = f"""分析这张机票搜索截图，提取所有航班信息。

搜索条件：{direction}
平台：{screenshot_info['name']}

请严格按以下 JSON 格式返回（不要返回其他内容）：
{{
  "flights": [
    {{
      "airline": "航空公司名",
      "flight_no": "航班号（如有）",
      "departure_time": "HH:MM",
      "arrival_time": "HH:MM",
      "duration": "Xh Xm",
      "stops": 0,
      "price_cny": 1234,
      "original_price": 20000,
      "original_currency": "JPY",
      "origin": "NRT/HND/PVG/SHA",
      "destination": "PVG/SHA/NRT/HND",
      "booking_note": "平台备注"
    }}
  ],
  "lowest_price": 1234,
  "currency": "CNY",
  "error": null
}}

如果截图中没有航班结果，返回：
{{"flights": [], "lowest_price": null, "currency": "CNY", "error": "描述问题"}}

重要注意事项：
- 仔细读取截图上完整的价格数字，不要遗漏任何数位（如 ¥1,064 → 1064）
- 币种判断规则：
  - 携程/去哪儿/同程 → 人民币(CNY)
  - Google Flights URL含 curr=CNY → 人民币(CNY)
  - Google Flights URL含 curr=JPY 或日本站 → 日元(JPY)
  - 日元价格在数万级别（如 ¥15,000~¥50,000）
- price_cny 统一换算为人民币整数（日元按 1 JPY = 0.048 CNY 换算）
- original_price 和 original_currency 保留原始价格和币种
- price_cny 合理范围：单程 300-5000
- 如果价格标注为"往返价"，请在 booking_note 中注明，price_cny 填往返总价的一半
- 只提取直达航班"""

    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/png;base64,{img_b64}",
                            "detail": "high",
                        }},
                    ],
                }],
                "max_tokens": 2000,
                "temperature": 0,
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

        json_match = re.search(r"\{[\s\S]*\}", content)
        if json_match:
            data = json.loads(json_match.group())
            # 价格校验：换算后 CNY 单程 200-8000 合理
            valid_flights = []
            for f in data.get("flights", []):
                price = f.get("price_cny")
                if price and 200 <= price <= 8000:
                    valid_flights.append(f)
                elif price:
                    log.warning(f"  过滤异常价格: ¥{price} ({f.get('airline', '')})")
            data["flights"] = valid_flights
            data["lowest_price"] = min((f["price_cny"] for f in valid_flights), default=None)
            return data
        else:
            return {"flights": [], "lowest_price": None, "error": f"LLM返回非JSON: {content[:200]}"}

    except Exception as e:
        log.error(f"LLM 分析失败: {e}")
        return {"flights": [], "lowest_price": None, "error": str(e)}


def analyze_all_screenshots(screenshots):
    """分析所有截图"""
    results = {"outbound": [], "return": [], "timestamp": now_jst().isoformat()}

    for ss in screenshots:
        log.info(f"🤖 分析: {ss['name']} {ss['label']}")
        analysis = analyze_screenshot(ss)
        analysis["source"] = ss["name"]
        analysis["url"] = ss["url"]

        if ss["direction"] == "outbound":
            results["outbound"].append(analysis)
        else:
            results["return"].append(analysis)

        if analysis.get("error"):
            log.warning(f"  ⚠️ {analysis['error']}")
        elif analysis.get("flights"):
            log.info(f"  ✓ {len(analysis['flights'])} 个航班, 最低 ¥{analysis.get('lowest_price', '?')}")

    return results
