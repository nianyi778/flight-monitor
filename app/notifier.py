"""
Telegram 通知模块 - 消息发送 + 格式化
"""

import requests

from app.config import (
    TG_BOT_TOKEN, TG_CHAT_ID, ACK_KEYWORD,
    now_jst, log,
)


def tg_send(text, parse_mode="Markdown"):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram 未配置，跳过通知")
        log.info(f"[TG预览]\n{text}")
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info("TG 通知已发送")
        return True
    except Exception as e:
        log.error(f"TG 发送失败: {e}")
        return False



def _price_str(f):
    op = f.get("original_price")
    oc = f.get("original_currency", "")
    cny = f.get("price_cny", "?")
    if oc == "JPY" and op:
        return f"¥{cny}(≈{op:,}円)"
    return f"¥{cny}"


def _brief_price(f):
    oc = f.get("original_currency", "CNY")
    op = f.get("original_price")
    cny = f.get("price_cny", "?")
    flag = "🇨🇳" if oc == "CNY" else "🇯🇵"
    if oc == "JPY" and op:
        return f"¥{cny}({flag}{op:,}円)"
    return f"¥{cny}({flag}CNY)"


def format_alert_message(combos, results, trip=None):
    ts = now_jst().strftime("%Y-%m-%d %H:%M")
    ob_date = trip["outbound_date"] if trip else "?"
    rt_date = trip["return_date"] if trip else "?"
    budget = trip["budget"] if trip else 1500
    trip_id = trip["id"] if trip else "?"

    lines = [f"✈️ *机票价格更新* ({ts}) 行程#{trip_id}\n"]
    lines.append(f"📅 去程: {ob_date} 东京→上海")
    lines.append(f"📅 回程: {rt_date} 上海→东京")
    lines.append(f"💰 预算: ¥{budget}(CNY) 往返\n")

    if combos:
        best = combos[0]
        ob = best["outbound"]
        rt = best["return"]
        emoji = "🎉" if best["within_budget"] else "📊"

        lines.append(f"{emoji} *最优组合: ¥{best['total']}*")
        lines.append(f"{'✅ 低于预算!' if best['within_budget'] else '⚠️ 超出预算'}\n")

        lines.append(f"*去程* {ob.get('airline', '')} {ob.get('flight_no', '')}")
        lines.append(f"  {ob.get('departure_time', '')}→{ob.get('arrival_time', '')} {_price_str(ob)} ({ob.get('_source', '')})")

        lines.append(f"*回程* {rt.get('airline', '')} {rt.get('flight_no', '')}")
        lines.append(f"  {rt.get('departure_time', '')}→{rt.get('arrival_time', '')} {_price_str(rt)} ({rt.get('_source', '')})")

        lines.append(f"\n🔗 *购买链接:*")
        lines.append(f"去程: {ob.get('_url', '')}")
        lines.append(f"回程: {rt.get('_url', '')}")

        if len(combos) > 1:
            lines.append(f"\n📋 *其他组合 (前5):*")
            for i, c in enumerate(combos[1:5], 2):
                o, r = c["outbound"], c["return"]
                lines.append(
                    f"{i}. ¥{c['total']} | "
                    f"{o.get('airline', '?')} {o.get('departure_time', '')} + "
                    f"{r.get('airline', '?')} {r.get('departure_time', '')}"
                )
    else:
        lines.append("⚠️ 未能组合出符合时间要求的航班\n")
        lines.append("*各平台最低价:*")
        for direction, label in [("outbound", "去程"), ("return", "回程")]:
            for src in results[direction]:
                lp = src.get("lowest_price")
                if lp:
                    lines.append(f"  {label} {src['source']}: ¥{lp}")

    lines.append(f"\n💬 回复「{ACK_KEYWORD}」停止推送")
    return "\n".join(lines)
