"""
Telegram Bot 命令监听模块
支持 Inline Keyboard 按钮交互，减少手动输入
"""

import asyncio
import json
from datetime import datetime

import requests

from app.config import (
    TG_BOT_TOKEN, TG_CHAT_ID, TG_ALLOWED_CHATS, ACK_KEYWORD, CHECK_INTERVAL,
    now_jst, log, load_state, save_state,
)
from app.db import get_db, get_active_trips
from app.notifier import tg_send


# 用于 /check 命令触发立即检查
force_check_event = asyncio.Event()
# 用于"确认收到"停止推送
ack_received_event = asyncio.Event()
# 标记是否正在查价中
checking_in_progress = False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TG API 工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def tg_api(method, **kwargs):
    """调用 Telegram Bot API"""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}",
            json=kwargs, timeout=10,
        )
        return resp.json()
    except Exception as e:
        log.error(f"TG API {method} 失败: {e}")
        return {}


def tg_send_with_buttons(text, buttons, parse_mode="Markdown"):
    """发送带 Inline Keyboard 的消息"""
    tg_api("sendMessage",
        chat_id=TG_CHAT_ID, text=text, parse_mode=parse_mode,
        disable_web_page_preview=True,
        reply_markup={"inline_keyboard": buttons},
    )


def tg_answer_callback(callback_id, text=""):
    """回答 callback query（消除按钮加载状态）"""
    tg_api("answerCallbackQuery", callback_query_id=callback_id, text=text)


def tg_edit_message(message_id, text, buttons=None, parse_mode="Markdown"):
    """编辑已有消息（更新按钮状态）"""
    kwargs = dict(
        chat_id=TG_CHAT_ID, message_id=message_id,
        text=text, parse_mode=parse_mode, disable_web_page_preview=True,
    )
    if buttons is not None:
        kwargs["reply_markup"] = {"inline_keyboard": buttons}
    tg_api("editMessageText", **kwargs)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 菜单注册
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def setup_tg_commands():
    if not TG_BOT_TOKEN:
        return
    try:
        tg_api("setMyCommands", commands=[
            {"command": "check", "description": "🔍 立即查价"},
            {"command": "trips", "description": "✈️ 行程管理"},
            {"command": "status", "description": "📊 系统状态"},
            {"command": "history", "description": "📈 价格趋势"},
            {"command": "health", "description": "🩺 健康检查"},
            {"command": "help", "description": "💡 使用帮助"},
        ])
        log.info("TG 菜单命令已注册")
    except Exception as e:
        log.error(f"TG 菜单注册失败: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _days_until(date_str):
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        delta = (target - now_jst().date()).days
        if delta > 0:
            return f"还有{delta}天"
        elif delta == 0:
            return "就是今天!"
        else:
            return f"已过{-delta}天"
    except:
        return ""


def _progress_bar(current, target, width=10):
    if not current or not target or current <= 0:
        return ""
    ratio = min(target / current, 1.0)
    filled = int(ratio * width)
    bar = "🟩" * filled + "⬜" * (width - filled)
    return f"{bar} {int(ratio * 100)}%"


def _get_all_trips():
    try:
        with get_db() as db:
            cur = db.cursor()
            cur.execute(
                "SELECT id, outbound_date, return_date, budget, best_price, "
                "ob_depart_start, ob_depart_end, ob_arrive_start, ob_arrive_end, "
                "rt_depart_start, rt_depart_end, rt_arrive_start, rt_arrive_end, "
                "status, trip_type, origin, destination, max_stops, throwaway "
                "FROM trips WHERE status IN ('active', 'paused') ORDER BY outbound_date"
            )
            rows = cur.fetchall()
        return [
            {
                "id": r[0], "outbound_date": str(r[1]),
                "return_date": str(r[2]) if r[2] else None,
                "budget": r[3], "best_price": r[4],
                "ob_depart_start": r[5], "ob_depart_end": r[6],
                "ob_arrive_start": r[7], "ob_arrive_end": r[8],
                "rt_depart_start": r[9], "rt_depart_end": r[10],
                "rt_arrive_start": r[11], "rt_arrive_end": r[12],
                "status": r[13], "trip_type": r[14] or "round_trip",
                "origin": r[15] or "TYO", "destination": r[16] or "PVG",
                "max_stops": r[17], "throwaway": bool(r[18]),
            }
            for r in rows
        ]
    except Exception as e:
        log.error(f"读取行程失败: {e}")
        return []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 命令处理
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _handle_trips():
    trips = _get_all_trips()
    if not trips:
        tg_send_with_buttons(
            "✈️ *行程管理*\n\n暂无行程，点击下方添加：",
            [[{"text": "➕ 添加行程", "callback_data": "trip_add_guide"}]]
        )
        return

    active = [t for t in trips if t["status"] == "active"]
    paused = [t for t in trips if t["status"] == "paused"]

    lines = [f"✈️ *行程管理* ({len(active)}个监控中)\n"]

    for t in active:
        tid = t["id"]
        countdown = _days_until(t["outbound_date"])
        best = t["best_price"]
        budget = t["budget"]
        is_one_way = t.get("trip_type") == "one_way"
        type_tag = " [单程]" if is_one_way else ""
        route = f"{t.get('origin', 'TYO')}→{t.get('destination', 'PVG')}"
        date_display = t["outbound_date"] if is_one_way else f"{t['outbound_date']} → {t.get('return_date', '?')}"

        # Time window summary
        ods, ode = t.get("ob_depart_start"), t.get("ob_depart_end")
        oas, oae = t.get("ob_arrive_start"), t.get("ob_arrive_end")
        ob_dep_str = f"🛫去{ods}-{ode}" if ods is not None else ""
        ob_arr_str = f"🛬去{oas}-{oae}" if oas is not None else ""
        stops_str = ""
        if t.get("max_stops") is not None:
            stops_str = "直飞" if t["max_stops"] == 0 else f"≤{t['max_stops']}转"
        ta_str = "🎫甩尾" if t.get("throwaway") else ""

        lines.append(f"🟢 *#{tid}* {route} {date_display}{type_tag}  {countdown}")
        info_parts = [f"💰 ¥{budget:,}", ob_dep_str, ob_arr_str, stops_str, ta_str]
        lines.append("  " + "  ".join(p for p in info_parts if p))
        if best:
            diff = best - budget
            sign = f"+¥{diff:,}" if diff > 0 else f"¥{diff:,} ✅"
            lines.append(f"  📉 最低¥{best:,} ({sign})")
            lines.append(f"  {_progress_bar(best, budget)}")
        else:
            lines.append(f"  📉 暂无数据")
        lines.append("")

    if paused:
        for t in paused:
            is_one_way = t.get("trip_type") == "one_way"
            date_display = t["outbound_date"] if is_one_way else f"{t['outbound_date']}→{t.get('return_date', '?')}"
            type_tag = "[单程]" if is_one_way else ""
            lines.append(f"⏸ #{t['id']} {date_display} {type_tag}")

    # 生成每个行程的操作按钮
    buttons = []
    for t in active:
        tid = t["id"]
        buttons.append([
            {"text": f"✏️ 编辑#{tid}", "callback_data": f"trip_edit_{tid}"},
            {"text": f"⏸ 暂停#{tid}", "callback_data": f"trip_pause_{tid}"},
            {"text": f"🗑 删除#{tid}", "callback_data": f"trip_del_confirm_{tid}"},
        ])
    for t in paused:
        tid = t["id"]
        buttons.append([
            {"text": f"▶️ 恢复#{tid}", "callback_data": f"trip_resume_{tid}"},
            {"text": f"🗑 删除#{tid}", "callback_data": f"trip_del_confirm_{tid}"},
        ])

    buttons.append([{"text": "➕ 添加新行程", "callback_data": "trip_add_guide"}])

    tg_send_with_buttons("\n".join(lines), buttons)


def _validate_trip_input(text):
    """校验行程输入，返回 (parsed_data, error_msg)

    格式: /trip add 出发 目的 去程 [回程|单程] [预算]
          [ob-dep H-H] [ob-arr H-H] [rt-dep H-H] [rt-arr H-H]
          [直飞|转机N] [甩尾]
    例:
      /trip add TYO PVG 2026-09-18 2026-09-28 1500 ob-dep 19-23 rt-arr 0-6
      /trip add NRT PVG 2026-09-18 单程 800 ob-dep 19-23 直飞
    """
    parts = text.split()
    USAGE = (
        "✈️ *添加行程*\n\n"
        "格式: `/trip add 出发 目的 去程 [回程|单程] [预算]`\n"
        "时间: `ob-dep H-H` `ob-arr H-H` `rt-dep H-H` `rt-arr H-H`\n"
        "过滤: `直飞` `转机1` `甩尾`\n\n"
        "例:\n"
        "`/trip add TYO PVG 2026-09-18 2026-09-28 1500 ob-dep 19-23 rt-arr 0-6`\n"
        "`/trip add NRT SHA 2026-09-18 单程 800 ob-dep 19-23 直飞`\n"
        "`/trip add PVG TYO 2026-09-18 2026-09-28`  (默认¥1500)"
    )

    if len(parts) < 5:
        return None, USAGE

    errors = []
    origin = parts[2].upper()
    destination = parts[3].upper()
    ob_d = parts[4]
    fifth_arg = parts[5] if len(parts) > 5 else None

    # Detect one-way vs round-trip
    is_one_way = fifth_arg in ("单程", "单", "one_way", "oneway") if fifth_arg else False

    try:
        ob_date = datetime.strptime(ob_d, "%Y-%m-%d").date()
    except ValueError:
        return None, f"❌ 去程日期格式错误: `{ob_d}` (应为 YYYY-MM-DD)"

    today = now_jst().date()
    if ob_date <= today:
        errors.append(f"❌ 去程日期 `{ob_d}` 已过期（今天是 {today}）")

    rt_d = None
    extra_start = 6
    if is_one_way:
        extra_start = 6
    else:
        if fifth_arg is None:
            return None, USAGE
        # Check if fifth_arg is a date
        try:
            rt_date = datetime.strptime(fifth_arg, "%Y-%m-%d").date()
            rt_d = fifth_arg
            if rt_date <= ob_date:
                errors.append(f"❌ 回程日期 `{rt_d}` 必须晚于去程 `{ob_d}`")
            extra_start = 6
        except ValueError:
            return None, f"❌ 回程日期格式错误: `{fifth_arg}` (应为 YYYY-MM-DD 或 '单程')"

    bgt = 1500
    ob_dep_s = ob_dep_e = ob_arr_s = ob_arr_e = None
    rt_dep_s = rt_dep_e = rt_arr_s = rt_arr_e = None
    max_stops = None
    throwaway = False

    i = extra_start
    remaining = parts[6:]  # after origin dest ob_d rt_d/单程
    # Re-collect remaining tokens after mandatory args
    # parts: [/trip, add, origin, dest, ob_d, fifth_arg, ...]
    remaining = parts[6:]
    # Handle budget as first remaining token if it's a digit
    if remaining and remaining[0].isdigit():
        try:
            bgt = int(remaining[0])
            if not (100 <= bgt <= 50000):
                errors.append(f"❌ 预算 ¥{bgt} 不合理 (范围 100-50000)")
            remaining = remaining[1:]
        except Exception:
            pass

    idx = 0
    while idx < len(remaining):
        token = remaining[idx]
        # Named time window: ob-dep, ob-arr, rt-dep, rt-arr
        if token in ("ob-dep", "ob-arr", "rt-dep", "rt-arr"):
            if idx + 1 >= len(remaining):
                errors.append(f"❌ {token} 后面需要时间范围，如 `{token} 19-23`")
                idx += 1
                continue
            time_val = remaining[idx + 1]
            try:
                s, e = [int(x) for x in time_val.split("-")]
                if not (0 <= s <= 23 and 0 <= e <= 23 and s <= e):
                    errors.append(f"❌ {token} 时间范围无效: `{time_val}` (0-23, 起始≤结束)")
                else:
                    if token == "ob-dep":
                        ob_dep_s, ob_dep_e = s, e
                    elif token == "ob-arr":
                        ob_arr_s, ob_arr_e = s, e
                    elif token == "rt-dep":
                        rt_dep_s, rt_dep_e = s, e
                    elif token == "rt-arr":
                        rt_arr_s, rt_arr_e = s, e
            except Exception:
                errors.append(f"❌ {token} 时间格式错误: `{time_val}` (应为 H-H)")
            idx += 2
            continue
        # Legacy aliases
        if token.startswith("去") and "-" in token:
            try:
                s, e = [int(x) for x in token.replace("去", "").split("-")]
                ob_dep_s, ob_dep_e = s, e
            except Exception:
                errors.append(f"❌ 去程时间格式错误: `{token}`")
            idx += 1
            continue
        if token.startswith("回") and "-" in token and not is_one_way:
            try:
                s, e = [int(x) for x in token.replace("回", "").split("-")]
                rt_arr_s, rt_arr_e = s, e
            except Exception:
                errors.append(f"❌ 回程时间格式错误: `{token}`")
            idx += 1
            continue
        if token in ("直飞", "直"):
            max_stops = 0
            idx += 1
            continue
        if token.startswith("转机"):
            try:
                max_stops = int(token.replace("转机", "") or "1")
            except Exception:
                max_stops = 1
            idx += 1
            continue
        if token in ("甩尾", "throwaway"):
            throwaway = True
            idx += 1
            continue
        if token.isdigit():
            try:
                bgt = int(token)
                if not (100 <= bgt <= 50000):
                    errors.append(f"❌ 预算 ¥{bgt} 不合理 (范围 100-50000)")
            except Exception:
                pass
            idx += 1
            continue
        errors.append(f"❌ 无法识别参数: `{token}`")
        idx += 1

    if errors:
        return None, "\n".join(errors)

    return {
        "origin": origin, "destination": destination,
        "ob_d": ob_d, "rt_d": rt_d, "budget": bgt,
        "trip_type": "one_way" if is_one_way else "round_trip",
        "ob_depart_start": ob_dep_s, "ob_depart_end": ob_dep_e,
        "ob_arrive_start": ob_arr_s, "ob_arrive_end": ob_arr_e,
        "rt_depart_start": rt_dep_s, "rt_depart_end": rt_dep_e,
        "rt_arrive_start": rt_arr_s, "rt_arrive_end": rt_arr_e,
        "max_stops": max_stops, "throwaway": throwaway,
    }, None


def _validate_budget_value(value):
    try:
        budget = int(value)
    except Exception:
        return None, "❌ 预算必须是整数"
    if not (100 <= budget <= 50000):
        return None, "❌ 预算范围 100-50000"
    return budget, None


def _validate_date_pair(ob_d, rt_d):
    try:
        ob_date = datetime.strptime(ob_d, "%Y-%m-%d").date()
        rt_date = datetime.strptime(rt_d, "%Y-%m-%d").date()
    except ValueError:
        return None, "❌ 日期格式错误，请用 YYYY-MM-DD"

    today = now_jst().date()
    if ob_date <= today:
        return None, f"❌ 去程日期 `{ob_d}` 已过期（今天是 {today}）"
    if rt_date <= ob_date:
        return None, "❌ 回程日期必须晚于去程"
    return (ob_d, rt_d), None


def _parse_window_arg(raw, prefix, label):
    if not raw.startswith(prefix):
        return None, f"❌ {label}格式错误，应为 `{prefix}HH-HH`"
    try:
        start, end = [int(x) for x in raw.replace(prefix, "").split("-")]
    except Exception:
        return None, f"❌ {label}格式错误，应为 `{prefix}HH-HH`"
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return None, f"❌ {label}超出范围 (0-23)"
    if start > end:
        return None, f"❌ {label}起始应小于等于结束"
    return (start, end), None


def _parse_flex_arg(raw, prefix, label):
    if not raw.startswith(prefix):
        return None, f"❌ {label}格式错误，应为 `{prefix}N`"
    try:
        value = int(raw.replace(prefix, ""))
    except Exception:
        return None, f"❌ {label}格式错误，应为 `{prefix}N`"
    if not (0 <= value <= 7):
        return None, f"❌ {label}范围 0-7"
    return value, None


def _handle_trip_add(text):
    """校验 → 写入 pending → 预览确认 → 激活"""
    from app.db import create_pending_trip

    data, error = _validate_trip_input(text)
    if error:
        tg_send_with_buttons(
            f"{error}\n\n💡 示例:\n`/trip add TYO PVG 2026-09-18 2026-09-28 1500 ob-dep 19-23 rt-arr 0-6`",
            [[{"text": "📖 查看帮助", "callback_data": "trip_add_guide"}]]
        )
        return

    is_one_way = data.get("trip_type") == "one_way"
    countdown = _days_until(data["ob_d"])
    rt_d = data.get("rt_d") or ""
    type_label = "单程" if is_one_way else "往返"

    # 写入 pending 状态（1小时后自动清理）
    pending_id = create_pending_trip({
        "origin": data["origin"],
        "destination": data["destination"],
        "outbound_date": data["ob_d"],
        "return_date": rt_d or None,
        "budget": data["budget"],
        "trip_type": data["trip_type"],
        "ob_depart_start": data.get("ob_depart_start"),
        "ob_depart_end": data.get("ob_depart_end"),
        "ob_arrive_start": data.get("ob_arrive_start"),
        "ob_arrive_end": data.get("ob_arrive_end"),
        "rt_depart_start": data.get("rt_depart_start") if not is_one_way else None,
        "rt_depart_end": data.get("rt_depart_end") if not is_one_way else None,
        "rt_arrive_start": data.get("rt_arrive_start") if not is_one_way else None,
        "rt_arrive_end": data.get("rt_arrive_end") if not is_one_way else None,
        "outbound_flex": 0,
        "return_flex": 1 if not is_one_way else None,
        "max_stops": data.get("max_stops"),
        "throwaway": data.get("throwaway", False),
    })

    preview_lines = [
        f"✈️ *请确认行程信息* [{type_label}]\n",
        f"🗺️ 路线: {data['origin']}→{data['destination']}",
        f"📅 去程: {data['ob_d']}  ({countdown})",
    ]
    if not is_one_way and rt_d:
        days = (datetime.strptime(rt_d, "%Y-%m-%d").date() -
                datetime.strptime(data["ob_d"], "%Y-%m-%d").date()).days
        preview_lines.append(f"📅 回程: {rt_d}  (共{days}天)")
    preview_lines.append(f"💰 预算: ¥{data['budget']:,}(CNY) {type_label}")
    if data.get("ob_depart_start") is not None:
        preview_lines.append(f"🛫 去程出发: {data['ob_depart_start']}:00-{data['ob_depart_end']}:00")
    if data.get("ob_arrive_start") is not None:
        preview_lines.append(f"🛬 去程落地: {data['ob_arrive_start']}:00-{data['ob_arrive_end']}:00")
    if not is_one_way and data.get("rt_arrive_start") is not None:
        preview_lines.append(f"🛬 回程落地: {data['rt_arrive_start']}:00-{data['rt_arrive_end']}:00")
    if data.get("max_stops") is not None:
        stops_str = "直飞" if data["max_stops"] == 0 else f"≤{data['max_stops']}转"
        preview_lines.append(f"✈️ 经停: {stops_str}")
    if data.get("throwaway"):
        preview_lines.append("🎫 甩尾票监控: 已启用")
    preview_lines.append("\n信息正确吗？")

    tg_send_with_buttons(
        "\n".join(preview_lines),
        [
            [{"text": "✅ 确认添加", "callback_data": f"trip_confirm_{pending_id}"},
             {"text": "❌ 取消", "callback_data": f"trip_cancel_pending_{pending_id}"}],
        ]
    )


def _health_check():
    """健康检查：数据库 / 代理 / TG"""
    from app.config import PROXY_URL
    import requests as req

    checks = {}

    # 1. 数据库
    try:
        with get_db() as db:
            c = db.cursor()
            c.execute("SELECT 1")
        checks["db"] = "✅"
    except Exception as e:
        checks["db"] = f"❌ {e}"

    # 3. 代理
    if PROXY_URL:
        try:
            r = req.get("https://httpbin.org/ip",
                proxies={"https": PROXY_URL, "http": PROXY_URL}, timeout=10)
            ip = r.json().get("origin", "?")
            checks["proxy"] = f"✅ {ip}"
        except Exception as e:
            checks["proxy"] = f"❌ {e}"
    else:
        checks["proxy"] = "⚠️ 未配置"

    # 4. TG Bot
    try:
        r = req.get(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getMe", timeout=5)
        checks["tg"] = "✅" if r.ok else f"❌ {r.status_code}"
    except Exception as e:
        checks["tg"] = f"❌ {e}"

    return checks


def _handle_status():
    from app.config import PROXY_URL

    s = load_state()
    trips = get_active_trips()

    lines = [
        "📊 *系统状态*\n",
        f"🔄 启动次数: {s.get('boot_count', 0)}",
        f"🔍 已巡查: {s.get('check_count', 0)} 次",
        f"✈️ 监控行程: {len(trips)} 个",
        f"⏰ 间隔: ~{CHECK_INTERVAL // 60} 分钟",
        f"🕐 {now_jst().strftime('%Y-%m-%d %H:%M')} JST",
        "",
        "━━━ 配置 ━━━",
        f"🏠 代理: `{PROXY_URL or '未配置'}`",
    ]

    # 行程摘要
    if trips:
        lines.append("")
        lines.append("━━━ 行程 ━━━")
        for t in trips:
            best = t.get("best_price")
            best_str = f"¥{best:,}" if best else "暂无"
            date_display = t["outbound_date"] if not t.get("return_date") else f"{t['outbound_date']}→{t['return_date']}"
            lines.append(f"#{t['id']} {date_display} 预算¥{t['budget']:,} 最低{best_str}")

    tg_send_with_buttons("\n".join(lines), [
        [{"text": "🩺 健康检查", "callback_data": "health_check"},
         {"text": "🔍 立即查价", "callback_data": "do_check"}],
        [{"text": "✈️ 行程管理", "callback_data": "show_trips"}],
    ])


def _handle_history():
    try:
        with get_db() as db:
            c = db.cursor()
            c.execute(
                "SELECT cs.check_time, cs.best_total, cs.outbound_lowest, cs.return_lowest, "
                "cs.best_outbound_airline, cs.best_return_airline, cs.trip_id "
                "FROM check_summary cs ORDER BY cs.check_time DESC LIMIT 10"
            )
            rows = c.fetchall()

        if not rows:
            tg_send("📈 暂无历史数据")
            return

        lines = [f"📈 *价格趋势* (最近{len(rows)}次)\n"]
        for r in reversed(rows):
            ts = r[0].strftime("%m-%d %H:%M")
            total = r[1] or "?"
            ob, rt = r[2] or "?", r[3] or "?"
            ob_air, rt_air = r[4] or "", r[5] or ""
            tid = r[6] or "?"
            air = f" {ob_air}+{rt_air}" if ob_air else ""
            lines.append(f"  {ts} #{tid} ¥{total} (去¥{ob}+回¥{rt}){air}")

        tg_send("\n".join(lines))
    except Exception as e:
        tg_send(f"📈 查询失败: {e}")


def _handle_help():
    tg_send_with_buttons(
        "💡 *使用帮助*\n\n"
        "点击下方按钮或使用菜单命令：\n\n"
        "大部分操作可以通过按钮完成，\n"
        "添加行程需要输入命令：\n"
        "`/trip add TYO PVG 去程 回程 预算`\n"
        "`/trip add NRT SHA 去程 单程 预算 ob-dep 19-23 直飞`\n\n"
        f"🇨🇳=人民币  🇯🇵=日元\n"
        f"回复「{ACK_KEYWORD}」停止好价推送",
        [
            [{"text": "🔍 立即查价", "callback_data": "do_check"},
             {"text": "✈️ 行程管理", "callback_data": "show_trips"}],
            [{"text": "📊 系统状态", "callback_data": "show_status"},
             {"text": "📈 价格趋势", "callback_data": "show_history"}],
        ]
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Callback Query 处理（按钮点击）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _handle_callback(callback_id, data, message_id):
    """处理 Inline Keyboard 按钮回调"""

    if data == "do_check":
        if checking_in_progress:
            tg_answer_callback(callback_id, "⏳ 正在查价中，请稍候...")
        else:
            tg_answer_callback(callback_id, "🔍 开始查价...")
            tg_send("🔍 收到！正在立即查价...")
            force_check_event.set()

    elif data == "show_trips":
        tg_answer_callback(callback_id)
        _handle_trips()

    elif data == "show_status":
        tg_answer_callback(callback_id)
        _handle_status()

    elif data == "health_check":
        tg_answer_callback(callback_id, "🩺 检查中...")
        checks = _health_check()
        lines = ["🩺 *健康检查*\n"]
        lines.append(f"💾 数据库: {checks['db']}")
        lines.append(f"🏠 代理: {checks['proxy']}")
        lines.append(f"📱 TG Bot: {checks['tg']}")

        all_ok = all("✅" in str(v) for v in checks.values())
        lines.append(f"\n{'✅ 全部正常' if all_ok else '⚠️ 部分异常，请检查'}")
        tg_send("\n".join(lines))

    elif data == "show_history":
        tg_answer_callback(callback_id)
        _handle_history()

    elif data == "trip_add_guide":
        tg_answer_callback(callback_id)
        tg_send(
            "✈️ *添加行程*\n\n"
            "请输入命令（复制修改即可）：\n\n"
            "`/trip add TYO PVG 2026-09-18 2026-09-28 1500 ob-dep 19-23 rt-arr 0-6`\n\n"
            "格式: `出发 目的 去程 [回程|单程] [预算]`\n"
            "时间窗: `ob-dep H-H` `ob-arr H-H` `rt-dep H-H` `rt-arr H-H`\n"
            "经停: `直飞` `转机1` | 甩尾: `甩尾`\n\n"
            "默认预算¥1500，所有时间窗均为可选"
        )

    elif data == "cancel_add":
        tg_answer_callback(callback_id, "已取消")
        tg_edit_message(message_id, "❌ 已取消添加")

    elif data.startswith("trip_cancel_pending_"):
        # Cancel a pending trip
        try:
            pending_id = int(data.split("_")[-1])
            with get_db() as db:
                c = db.cursor()
                c.execute("DELETE FROM trips WHERE id=%s AND status='pending'", (pending_id,))
                db.commit()
        except Exception:
            pass
        tg_answer_callback(callback_id, "已取消")
        tg_edit_message(message_id, "❌ 已取消添加")

    elif data.startswith("trip_confirm_"):
        # trip_confirm_{pending_id}
        try:
            from app.db import activate_pending_trip
            pending_id = int(data.split("_")[-1])
            ok = activate_pending_trip(pending_id)
            if not ok:
                tg_answer_callback(callback_id, "确认失败（可能已超时）")
                tg_edit_message(message_id, "❌ 确认失败，请重新添加行程")
                return

            # Fetch the newly activated trip for display
            with get_db() as db:
                c = db.cursor()
                c.execute(
                    "SELECT id, origin, destination, outbound_date, return_date, budget, trip_type "
                    "FROM trips WHERE id=%s",
                    (pending_id,)
                )
                row = c.fetchone()

            new_id = row[0]
            route = f"{row[1]}→{row[2]}"
            ob_d = str(row[3])
            rt_d = str(row[4]) if row[4] else None
            bgt = row[5]
            trip_type = row[6] or "round_trip"
            is_one_way = trip_type == "one_way"
            type_label = "单程" if is_one_way else "往返"
            countdown = _days_until(ob_d)
            date_line = ob_d if is_one_way else f"{ob_d} → {rt_d}"

            tg_answer_callback(callback_id, f"✅ 行程#{new_id}已添加")
            tg_edit_message(message_id,
                f"✅ *行程#{new_id} 已添加!* [{type_label}]\n\n"
                f"🗺️ 路线: {route}\n"
                f"📅 {date_line}  ({countdown})\n"
                f"💰 ¥{bgt:,}(CNY)\n\n"
                f"系统将在下次巡查时开始监控此行程",
                [[{"text": "🔍 立即查价", "callback_data": "do_check"},
                  {"text": "✈️ 查看行程", "callback_data": "show_trips"}]]
            )
        except Exception as e:
            tg_answer_callback(callback_id, "添加失败")
            tg_send(f"❌ 添加失败: {e}")

    elif data.startswith("trip_pause_"):
        tid = int(data.split("_")[-1])
        try:
            with get_db() as db:
                c = db.cursor()
                c.execute("UPDATE trips SET status='paused' WHERE id=%s AND status='active'", (tid,))
                db.commit()
            tg_answer_callback(callback_id, f"⏸ 行程#{tid}已暂停")
            _handle_trips()  # 刷新列表
        except Exception as e:
            tg_answer_callback(callback_id, f"失败: {e}")

    elif data.startswith("trip_resume_"):
        tid = int(data.split("_")[-1])
        try:
            with get_db() as db:
                c = db.cursor()
                c.execute("UPDATE trips SET status='active' WHERE id=%s AND status='paused'", (tid,))
                db.commit()
            tg_answer_callback(callback_id, f"▶️ 行程#{tid}已恢复")
            _handle_trips()
        except Exception as e:
            tg_answer_callback(callback_id, f"失败: {e}")

    elif data.startswith("trip_del_confirm_"):
        tid = int(data.split("_")[-1])
        tg_answer_callback(callback_id)
        tg_send_with_buttons(
            f"⚠️ 确认删除行程 *#{tid}*？",
            [[{"text": "✅ 确认删除", "callback_data": f"trip_del_yes_{tid}"},
              {"text": "❌ 取消", "callback_data": "show_trips"}]]
        )

    elif data.startswith("trip_del_yes_"):
        tid = int(data.split("_")[-1])
        try:
            with get_db() as db:
                c = db.cursor()
                c.execute("UPDATE trips SET status='deleted' WHERE id=%s", (tid,))
                db.commit()
            tg_answer_callback(callback_id, f"🗑 已删除#{tid}")
            _handle_trips()
        except Exception as e:
            tg_answer_callback(callback_id, f"失败: {e}")

    elif data.startswith("trip_edit_"):
        tid = data.split("_")[-1]
        tg_answer_callback(callback_id)
        # 查当前值显示
        try:
            with get_db() as db:
                c = db.cursor()
                c.execute(
                    "SELECT outbound_date, return_date, budget, "
                    "ob_depart_start, ob_depart_end, ob_arrive_start, ob_arrive_end, "
                    "rt_depart_start, rt_depart_end, rt_arrive_start, rt_arrive_end, "
                    "ob_flex, rt_flex FROM trips WHERE id=%s", (tid,))
                r = c.fetchone()
            if r:
                ob_flex = r[11] or 0
                rt_flex = r[12] if r[12] is not None else 1
                def _fmt_win(s, e): return f"`{s}-{e}点`" if s is not None else "`不限`"
                tg_send_with_buttons(
                    f"✏️ *编辑行程 #{tid}*\n\n"
                    f"📅 去程: `{r[0]}` (弹性±{ob_flex}天)\n"
                    f"📅 回程: `{r[1]}` (弹性±{rt_flex}天)\n"
                    f"💰 预算: `¥{r[2]:,}`\n"
                    f"🛫 去程出发: {_fmt_win(r[3], r[4])}  到达: {_fmt_win(r[5], r[6])}\n"
                    f"🛬 回程出发: {_fmt_win(r[7], r[8])}  到达: {_fmt_win(r[9], r[10])}\n\n"
                    f"点击要修改的项目：",
                    [
                        [{"text": "📅 改日期", "callback_data": f"trip_date_guide_{tid}"},
                         {"text": "💰 改预算", "callback_data": f"trip_budget_guide_{tid}"}],
                        [{"text": "⏰ 改时间窗口", "callback_data": f"trip_time_guide_{tid}"},
                         {"text": "📆 改弹性天数", "callback_data": f"trip_flex_guide_{tid}"}],
                        [{"text": "↩️ 返回", "callback_data": "show_trips"}],
                    ]
                )
            else:
                tg_send(f"❌ 行程 #{tid} 不存在")
        except Exception as e:
            tg_send(f"❌ {e}")

    elif data.startswith("trip_date_guide_"):
        tid = data.split("_")[-1]
        tg_answer_callback(callback_id)
        tg_send(
            f"📅 修改日期，请输入：\n\n"
            f"`/trip date {tid} 去程日期 回程日期`\n\n"
            f"例: `/trip date {tid} 2026-09-18 2026-09-28`"
        )

    elif data.startswith("trip_budget_guide_"):
        tid = data.split("_")[-1]
        tg_answer_callback(callback_id)
        tg_send(f"💰 修改预算，请输入：\n\n`/trip budget {tid} 新金额`\n\n例: `/trip budget {tid} 2000`")

    elif data.startswith("trip_flex_guide_"):
        tid = data.split("_")[-1]
        tg_answer_callback(callback_id)
        tg_send(
            f"📆 修改弹性天数，请输入：\n\n"
            f"`/trip flex {tid} 去N 回N`\n\n"
            f"例: `/trip flex {tid} 去0 回1`\n"
            f"含义: 去程不弹性，回程向前搜1天\n\n"
            f"回1 = 回程日期前1天也会搜索\n"
            f"如 回程9/28 + 回1 → 搜9/27和9/28"
        )

    elif data.startswith("trip_time_guide_"):
        tid = data.split("_")[-1]
        tg_answer_callback(callback_id)
        tg_send(
            f"⏰ 修改时间窗口，请输入（可组合任意窗口）：\n\n"
            f"`/trip time {tid} ob-dep HH-HH`  去程出发\n"
            f"`/trip time {tid} ob-arr HH-HH`  去程到达\n"
            f"`/trip time {tid} rt-dep HH-HH`  回程出发\n"
            f"`/trip time {tid} rt-arr HH-HH`  回程到达\n\n"
            f"例: `/trip time {tid} ob-dep 19-23 rt-arr 0-6`"
        )

    else:
        tg_answer_callback(callback_id, "未知操作")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主监听循环
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def tg_command_listener():
    """后台监听 TG 命令和按钮回调"""
    from app.scheduler import shutdown_event

    state = load_state()
    last_update_id = state.get("last_tg_update_id", 0)

    while not shutdown_event.is_set():
        try:
            resp = await asyncio.to_thread(
                requests.get,
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates",
                params={"offset": last_update_id + 1, "timeout": 10},
                timeout=15,
            )
            if not resp.ok:
                await asyncio.sleep(5)
                continue

            updates = resp.json().get("result", [])
            for update in updates:
                last_update_id = update["update_id"]

                # 处理按钮回调
                callback = update.get("callback_query")
                if callback:
                    cb_id = callback["id"]
                    cb_data = callback.get("data", "")
                    cb_msg_id = callback.get("message", {}).get("message_id")
                    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
                    if chat_id in TG_ALLOWED_CHATS:
                        _handle_callback(cb_id, cb_data, cb_msg_id)
                    continue

                # 处理文字命令
                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id not in TG_ALLOWED_CHATS:
                    continue

                if text == "/check":
                    if checking_in_progress:
                        tg_send("⏳ 正在查价中，请稍候...")
                    else:
                        tg_send("🔍 收到！正在立即查价...")
                        force_check_event.set()
                elif text == "/status":
                    _handle_status()
                elif text == "/health":
                    checks = _health_check()
                    lines = ["🩺 *健康检查*\n"]
                    lines.append(f"💾 数据库: {checks['db']}")
                    lines.append(f"🏠 代理: {checks['proxy']}")
                    lines.append(f"📱 TG Bot: {checks['tg']}")
                    all_ok = all("✅" in str(v) for v in checks.values())
                    lines.append(f"\n{'✅ 全部正常' if all_ok else '⚠️ 部分异常'}")
                    tg_send("\n".join(lines))
                elif text == "/history":
                    _handle_history()
                elif text in ("/trips", "/trip list", "/trip", "/budget", "/start"):
                    _handle_trips()
                elif text.startswith("/trip add"):
                    _handle_trip_add(text)
                elif text.startswith("/trip del"):
                    # 文字方式也支持
                    parts = text.split()
                    if len(parts) >= 3:
                        try:
                            tid = int(parts[2])
                            with get_db() as db:
                                c = db.cursor()
                                c.execute("UPDATE trips SET status='deleted' WHERE id=%s", (tid,))
                                db.commit()
                            tg_send(f"🗑 行程 #{tid} 已删除")
                        except Exception as e:
                            tg_send(f"❌ {e}")
                elif text.startswith("/trip pause"):
                    parts = text.split()
                    if len(parts) >= 3:
                        try:
                            tid = int(parts[2])
                            with get_db() as db:
                                c = db.cursor()
                                c.execute("UPDATE trips SET status='paused' WHERE id=%s", (tid,))
                                db.commit()
                            tg_send(f"⏸ 行程 #{tid} 已暂停")
                        except Exception as e:
                            tg_send(f"❌ {e}")
                elif text.startswith("/trip resume"):
                    parts = text.split()
                    if len(parts) >= 3:
                        try:
                            tid = int(parts[2])
                            with get_db() as db:
                                c = db.cursor()
                                c.execute("UPDATE trips SET status='active' WHERE id=%s", (tid,))
                                db.commit()
                            tg_send(f"▶️ 行程 #{tid} 已恢复")
                        except Exception as e:
                            tg_send(f"❌ {e}")
                elif text.startswith("/trip budget"):
                    parts = text.split()
                    if len(parts) >= 4:
                        try:
                            tid = int(parts[2])
                            new_b, error = _validate_budget_value(parts[3])
                            if error:
                                tg_send(error)
                                continue
                            with get_db() as db:
                                c = db.cursor()
                                c.execute("UPDATE trips SET budget=%s WHERE id=%s", (new_b, tid))
                                db.commit()
                            tg_send(f"💰 行程 #{tid} 预算已改为 ¥{new_b:,}(CNY)")
                        except Exception as e:
                            tg_send(f"❌ {e}")
                    else:
                        tg_send("格式: `/trip budget 编号 金额`")
                elif text.startswith("/trip date"):
                    parts = text.split()
                    if len(parts) >= 5:
                        try:
                            tid = int(parts[2])
                            ob_d, rt_d = parts[3], parts[4]
                            _, error = _validate_date_pair(ob_d, rt_d)
                            if error:
                                tg_send(error)
                                continue
                            with get_db() as db:
                                c = db.cursor()
                                c.execute("UPDATE trips SET outbound_date=%s, return_date=%s WHERE id=%s",
                                          (ob_d, rt_d, tid))
                                db.commit()
                            tg_send(f"📅 行程 #{tid} 日期已更新\n去程: {ob_d} → 回程: {rt_d}")
                        except ValueError:
                            tg_send("❌ 编号必须是整数")
                        except Exception as e:
                            tg_send(f"❌ {e}")
                    else:
                        tg_send("格式: `/trip date 编号 去程 回程`\n例: `/trip date 1 2026-09-18 2026-09-28`")
                elif text.startswith("/trip flex"):
                    parts = text.split()
                    if len(parts) >= 5:
                        try:
                            tid = int(parts[2])
                            ob_flex, error = _parse_flex_arg(parts[3], "去", "去程弹性")
                            if error:
                                tg_send(error)
                                continue
                            rt_flex, error = _parse_flex_arg(parts[4], "回", "回程弹性")
                            if error:
                                tg_send(error)
                                continue
                            with get_db() as db:
                                c = db.cursor()
                                c.execute("UPDATE trips SET ob_flex=%s, rt_flex=%s WHERE id=%s",
                                          (ob_flex, rt_flex, tid))
                                db.commit()
                            tg_send(f"📆 行程 #{tid} 弹性已更新\n去程±{ob_flex}天 回程±{rt_flex}天")
                        except ValueError:
                            tg_send("❌ 编号必须是整数")
                        except Exception as e:
                            tg_send(f"❌ {e}")
                    else:
                        tg_send("格式: `/trip flex 编号 去N 回N`\n例: `/trip flex 1 去0 回1`")
                elif text.startswith("/trip time"):
                    parts = text.split()
                    if len(parts) >= 4:
                        try:
                            tid = int(parts[2])
                            tokens = parts[3:]
                            windows = {}
                            key_map = {
                                "ob-dep": ("ob_depart_start", "ob_depart_end"),
                                "ob-arr": ("ob_arrive_start", "ob_arrive_end"),
                                "rt-dep": ("rt_depart_start", "rt_depart_end"),
                                "rt-arr": ("rt_arrive_start", "rt_arrive_end"),
                            }
                            err = None
                            i = 0
                            while i < len(tokens):
                                tok = tokens[i]
                                if tok in key_map:
                                    if i + 1 >= len(tokens):
                                        err = f"❌ {tok} 后面需要时间范围，如 `{tok} 19-23`"
                                        break
                                    try:
                                        s, e = [int(x) for x in tokens[i + 1].split("-")]
                                        if not (0 <= s <= 23 and 0 <= e <= 23 and s <= e):
                                            err = f"❌ {tok} 时间范围无效 (0-23，起始≤结束)"
                                            break
                                        windows[tok] = (s, e)
                                        i += 2
                                    except Exception:
                                        err = f"❌ {tok} 格式错误，应为 `{tok} HH-HH`"
                                        break
                                else:
                                    err = f"❌ 未知参数 `{tok}`，支持: ob-dep ob-arr rt-dep rt-arr"
                                    break
                            if err:
                                tg_send(err)
                                continue
                            if not windows:
                                tg_send("格式: `/trip time 编号 ob-dep HH-HH [ob-arr HH-HH] [rt-dep HH-HH] [rt-arr HH-HH]`")
                                continue
                            set_parts, vals = [], []
                            for key, (col_s, col_e) in key_map.items():
                                if key in windows:
                                    set_parts += [f"{col_s}=%s", f"{col_e}=%s"]
                                    vals += list(windows[key])
                            vals.append(tid)
                            with get_db() as db:
                                c = db.cursor()
                                c.execute(f"UPDATE trips SET {', '.join(set_parts)} WHERE id=%s", vals)
                                db.commit()
                            labels = {"ob-dep": "去出发", "ob-arr": "去到达", "rt-dep": "回出发", "rt-arr": "回到达"}
                            summary = "  ".join(f"{labels[k]}: {v[0]}-{v[1]}点" for k, v in windows.items())
                            tg_send(f"⏰ 行程 #{tid} 时间窗已更新\n{summary}")
                        except ValueError:
                            tg_send("❌ 编号必须是整数")
                        except Exception as e:
                            tg_send(f"❌ {e}")
                    else:
                        tg_send("格式: `/trip time 编号 ob-dep HH-HH [ob-arr HH-HH] [rt-dep HH-HH] [rt-arr HH-HH]`")
                elif text == "/help":
                    _handle_help()
                elif ACK_KEYWORD in text:
                    ack_received_event.set()
                    log.info("📨 收到确认回复（via bot listener）")

            state = load_state()
            state["last_tg_update_id"] = last_update_id
            save_state(state)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"TG 命令监听异常: {e}")
            await asyncio.sleep(10)

        await asyncio.sleep(1)
