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

    def _post(pm):
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }
        if pm:
            payload["parse_mode"] = pm
        return requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )

    try:
        resp = _post(parse_mode)
        if resp.status_code == 400 and parse_mode:
            # Markdown 解析失败（通常是 URL 含特殊字符），降级为纯文本重试
            log.warning("TG Markdown 解析失败，降级纯文本重试")
            resp = _post(None)
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
    rt_date = trip.get("return_date") if trip else None
    budget = trip["budget"] if trip else 1500
    trip_id = trip["id"] if trip else "?"
    is_one_way = (trip.get("trip_type") == "one_way") if trip else False

    origin = trip.get("origin", "TYO") if trip else "TYO"
    destination = trip.get("destination", "PVG") if trip else "PVG"

    lines = [f"✈️ *机票价格更新* ({ts}) 行程#{trip_id}\n"]
    lines.append(f"📅 去程: {ob_date} {origin}→{destination}")
    if not is_one_way and rt_date:
        lines.append(f"📅 回程: {rt_date} {destination}→{origin}")
    type_label = "单程" if is_one_way else "往返"
    lines.append(f"💰 预算: ¥{budget}(CNY) {type_label}\n")

    if combos:
        best = combos[0]
        ob = best["outbound"]
        rt = best.get("return")
        emoji = "🎉" if best["within_budget"] else "📊"

        lines.append(f"{emoji} *最优{'单程' if is_one_way else '组合'}: ¥{best['total']}*")
        lines.append(f"{'✅ 低于预算!' if best['within_budget'] else '⚠️ 超出预算'}\n")

        lines.append(f"*去程* {ob.get('airline', '')} {ob.get('flight_no', '')}")
        lines.append(f"  {ob.get('departure_time', '')}→{ob.get('arrival_time', '')} {_price_str(ob)} ({ob.get('_source', '')})")

        if not is_one_way and rt:
            lines.append(f"*回程* {rt.get('airline', '')} {rt.get('flight_no', '')}")
            lines.append(f"  {rt.get('departure_time', '')}→{rt.get('arrival_time', '')} {_price_str(rt)} ({rt.get('_source', '')})")

        if best.get("throwaway"):
            via = ob.get("via", "") or destination
            lines.append(
                f"\n🎫 *甩尾票提示*: 购买 {origin}→终点 的机票，"
                f"在 *{via}* 下机即可，无需乘坐后续航段"
            )

        lines.append(f"\n🔗 *购买链接:*")
        lines.append(f"去程: {ob.get('_url', '')}")
        if not is_one_way and rt:
            lines.append(f"回程: {rt.get('_url', '')}")

        if len(combos) > 1:
            lines.append(f"\n📋 *其他选项 (前5):*")
            for i, c in enumerate(combos[1:5], 2):
                o = c["outbound"]
                r = c.get("return")
                if is_one_way or not r:
                    lines.append(
                        f"{i}. ¥{c['total']} | "
                        f"{o.get('airline', '?')} {o.get('departure_time', '')}"
                    )
                else:
                    lines.append(
                        f"{i}. ¥{c['total']} | "
                        f"{o.get('airline', '?')} {o.get('departure_time', '')} + "
                        f"{r.get('airline', '?')} {r.get('departure_time', '')}"
                    )
    else:
        lines.append("⚠️ 未能找到符合时间要求的航班\n")
        lines.append("*各平台最低价:*")
        directions = [("outbound", "去程")] if is_one_way else [("outbound", "去程"), ("return", "回程")]
        for direction, label in directions:
            for src in results.get(direction, []):
                lp = src.get("lowest_price")
                if lp:
                    lines.append(f"  {label} {src['source']}: ¥{lp}")

    lines.append(f"\n💬 回复「{ACK_KEYWORD}」停止推送")
    return "\n".join(lines)
