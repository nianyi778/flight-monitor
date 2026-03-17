"""
Telegram Bot 命令监听模块
支持 Inline Keyboard 按钮交互，减少手动输入
"""

import asyncio
import json
from datetime import datetime

import requests

from app.config import (
    TG_BOT_TOKEN, TG_CHAT_ID, ACK_KEYWORD, CHECK_INTERVAL,
    now_jst, log, load_state, save_state,
)
from app.db import get_db, get_active_trips
from app.notifier import tg_send


# 用于 /check 命令触发立即检查
force_check_event = asyncio.Event()


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
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "SELECT id, outbound_date, return_date, budget, best_price, "
            "outbound_depart_start, outbound_depart_end, return_arrive_start, return_arrive_end, status "
            "FROM trips WHERE status IN ('active', 'paused') ORDER BY outbound_date"
        )
        rows = cur.fetchall()
        db.close()
        return [
            {"id": r[0], "outbound_date": str(r[1]), "return_date": str(r[2]),
             "budget": r[3], "best_price": r[4],
             "depart_after": r[5] or 19, "depart_before": r[6] or 23,
             "arrive_after": r[7] or 0, "arrive_before": r[8] or 6,
             "status": r[9]}
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
        da, db_ = t["depart_after"], t["depart_before"]
        aa, ab = t["arrive_after"], t["arrive_before"]

        lines.append(f"🟢 *#{tid}* {t['outbound_date']} → {t['return_date']}  {countdown}")
        lines.append(f"  💰 ¥{budget:,}  🛫{da}-{db_}点  🛬{aa}-{ab}点")
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
            lines.append(f"⏸ #{t['id']} {t['outbound_date']}→{t['return_date']}")

    # 生成每个行程的操作按钮
    buttons = []
    for t in active:
        tid = t["id"]
        buttons.append([
            {"text": f"⏸ 暂停#{tid}", "callback_data": f"trip_pause_{tid}"},
            {"text": f"💰 改预算#{tid}", "callback_data": f"trip_budget_guide_{tid}"},
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


def _handle_trip_add(text):
    # /trip add 去程 回程 [预算] [去HH-HH] [回HH-HH]
    parts = text.split()
    if len(parts) < 4:
        tg_send(
            "✈️ *添加行程*\n\n"
            "格式: `/trip add 去程 回程 [预算] [去H-H] [回H-H]`\n\n"
            "例:\n"
            "`/trip add 2026-09-18 2026-09-27 1500 去19-23 回0-6`\n"
            "`/trip add 2026-09-18 2026-09-27 1500`\n"
            "`/trip add 2026-12-28 2027-01-05`  (默认¥1500 去19-23 回0-6)"
        )
        return

    try:
        ob_d, rt_d = parts[2], parts[3]
        datetime.strptime(ob_d, "%Y-%m-%d")
        datetime.strptime(rt_d, "%Y-%m-%d")

        # 解析可选参数（预算、时间窗口，顺序灵活）
        bgt = 1500
        ob_start, ob_end = 19, 23
        rt_start, rt_end = 0, 6

        for p in parts[4:]:
            if p.startswith("去"):
                s, e = [int(x) for x in p.replace("去", "").split("-")]
                ob_start, ob_end = s, e
            elif p.startswith("回"):
                s, e = [int(x) for x in p.replace("回", "").split("-")]
                rt_start, rt_end = s, e
            elif p.isdigit():
                bgt = int(p)

        db = get_db()
        c = db.cursor()
        c.execute(
            "INSERT INTO trips (outbound_date, return_date, budget, "
            "outbound_depart_start, outbound_depart_end, return_arrive_start, return_arrive_end) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (ob_d, rt_d, bgt, ob_start, ob_end, rt_start, rt_end)
        )
        db.commit()
        new_id = c.lastrowid
        db.close()

        countdown = _days_until(ob_d)
        tg_send_with_buttons(
            f"✅ *行程已添加!*\n\n"
            f"🆔 #{new_id}  {countdown}\n"
            f"📅 {ob_d} → {rt_d}\n"
            f"💰 ¥{bgt:,}(CNY)\n"
            f"🛫 去程出发: {ob_start}:00-{ob_end}:00\n"
            f"🛬 回程到达: {rt_start}:00-{rt_end}:00",
            [
                [{"text": "⏰ 调整时间", "callback_data": f"trip_time_guide_{new_id}"},
                 {"text": "💰 调整预算", "callback_data": f"trip_budget_guide_{new_id}"}],
                [{"text": "🔍 立即查价", "callback_data": "do_check"}],
            ]
        )
    except ValueError:
        tg_send("❌ 格式错误\n日期: YYYY-MM-DD  预算: 数字  时间: 去H-H 回H-H")
    except Exception as e:
        tg_send(f"❌ 添加失败: {e}")


def _handle_status():
    s = load_state()
    trips = get_active_trips()
    lines = [
        "📊 *系统状态*\n",
        f"🔄 启动次数: {s.get('boot_count', 0)}",
        f"🔍 已巡查: {s.get('check_count', 0)} 次",
        f"✈️ 监控行程: {len(trips)} 个",
        f"⏰ 间隔: ~{CHECK_INTERVAL // 60} 分钟",
        f"🕐 {now_jst().strftime('%Y-%m-%d %H:%M')} JST",
    ]
    tg_send_with_buttons("\n".join(lines), [
        [{"text": "🔍 立即查价", "callback_data": "do_check"},
         {"text": "✈️ 行程管理", "callback_data": "show_trips"}],
    ])


def _handle_history():
    try:
        db = get_db()
        c = db.cursor()
        c.execute(
            "SELECT cs.check_time, cs.best_total, cs.outbound_lowest, cs.return_lowest, "
            "cs.best_outbound_airline, cs.best_return_airline, cs.trip_id "
            "FROM check_summary cs ORDER BY cs.check_time DESC LIMIT 10"
        )
        rows = c.fetchall()
        db.close()

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
        "只有添加行程需要输入日期：\n"
        "`/trip add 去程 回程 预算`\n\n"
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
        tg_answer_callback(callback_id, "🔍 开始查价...")
        tg_send("🔍 收到！正在立即查价...")
        force_check_event.set()

    elif data == "show_trips":
        tg_answer_callback(callback_id)
        _handle_trips()

    elif data == "show_status":
        tg_answer_callback(callback_id)
        _handle_status()

    elif data == "show_history":
        tg_answer_callback(callback_id)
        _handle_history()

    elif data == "trip_add_guide":
        tg_answer_callback(callback_id)
        tg_send(
            "✈️ *添加行程*\n\n"
            "请输入（复制修改日期即可）：\n\n"
            "`/trip add 2026-09-18 2026-09-27 1500`\n\n"
            "格式: 去程日期 回程日期 预算(CNY)"
        )

    elif data.startswith("trip_pause_"):
        tid = int(data.split("_")[-1])
        try:
            db = get_db()
            c = db.cursor()
            c.execute("UPDATE trips SET status='paused' WHERE id=%s AND status='active'", (tid,))
            db.commit()
            db.close()
            tg_answer_callback(callback_id, f"⏸ 行程#{tid}已暂停")
            _handle_trips()  # 刷新列表
        except Exception as e:
            tg_answer_callback(callback_id, f"失败: {e}")

    elif data.startswith("trip_resume_"):
        tid = int(data.split("_")[-1])
        try:
            db = get_db()
            c = db.cursor()
            c.execute("UPDATE trips SET status='active' WHERE id=%s AND status='paused'", (tid,))
            db.commit()
            db.close()
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
            db = get_db()
            c = db.cursor()
            c.execute("UPDATE trips SET status='deleted' WHERE id=%s", (tid,))
            db.commit()
            db.close()
            tg_answer_callback(callback_id, f"🗑 已删除#{tid}")
            _handle_trips()
        except Exception as e:
            tg_answer_callback(callback_id, f"失败: {e}")

    elif data.startswith("trip_budget_guide_"):
        tid = data.split("_")[-1]
        tg_answer_callback(callback_id)
        tg_send(f"💰 修改预算，请输入：\n\n`/trip budget {tid} 新金额`\n\n例: `/trip budget {tid} 2000`")

    elif data.startswith("trip_time_guide_"):
        tid = data.split("_")[-1]
        tg_answer_callback(callback_id)
        tg_send(
            f"⏰ 修改时间窗口，请输入：\n\n"
            f"`/trip time {tid} 去HH-HH 回HH-HH`\n\n"
            f"例: `/trip time {tid} 去19-23 回0-6`"
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
            resp = requests.get(
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
                    if chat_id == str(TG_CHAT_ID):
                        _handle_callback(cb_id, cb_data, cb_msg_id)
                    continue

                # 处理文字命令
                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id != str(TG_CHAT_ID):
                    continue

                if text == "/check":
                    tg_send("🔍 收到！正在立即查价...")
                    force_check_event.set()
                elif text == "/status":
                    _handle_status()
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
                            db = get_db()
                            c = db.cursor()
                            c.execute("UPDATE trips SET status='deleted' WHERE id=%s", (tid,))
                            db.commit()
                            db.close()
                            tg_send(f"🗑 行程 #{tid} 已删除")
                        except Exception as e:
                            tg_send(f"❌ {e}")
                elif text.startswith("/trip pause"):
                    parts = text.split()
                    if len(parts) >= 3:
                        try:
                            tid = int(parts[2])
                            db = get_db()
                            c = db.cursor()
                            c.execute("UPDATE trips SET status='paused' WHERE id=%s", (tid,))
                            db.commit()
                            db.close()
                            tg_send(f"⏸ 行程 #{tid} 已暂停")
                        except Exception as e:
                            tg_send(f"❌ {e}")
                elif text.startswith("/trip resume"):
                    parts = text.split()
                    if len(parts) >= 3:
                        try:
                            tid = int(parts[2])
                            db = get_db()
                            c = db.cursor()
                            c.execute("UPDATE trips SET status='active' WHERE id=%s", (tid,))
                            db.commit()
                            db.close()
                            tg_send(f"▶️ 行程 #{tid} 已恢复")
                        except Exception as e:
                            tg_send(f"❌ {e}")
                elif text.startswith("/trip budget"):
                    parts = text.split()
                    if len(parts) >= 4:
                        try:
                            tid, new_b = int(parts[2]), int(parts[3])
                            db = get_db()
                            c = db.cursor()
                            c.execute("UPDATE trips SET budget=%s WHERE id=%s", (new_b, tid))
                            db.commit()
                            db.close()
                            tg_send(f"💰 行程 #{tid} 预算已改为 ¥{new_b:,}(CNY)")
                        except Exception as e:
                            tg_send(f"❌ {e}")
                    else:
                        tg_send("格式: `/trip budget 编号 金额`")
                elif text.startswith("/trip time"):
                    parts = text.split()
                    if len(parts) >= 5:
                        try:
                            tid = int(parts[2])
                            ob_s, ob_e = [int(x) for x in parts[3].replace("去", "").split("-")]
                            rt_s, rt_e = [int(x) for x in parts[4].replace("回", "").split("-")]
                            db = get_db()
                            c = db.cursor()
                            c.execute(
                                "UPDATE trips SET outbound_depart_start=%s, outbound_depart_end=%s, "
                                "return_arrive_start=%s, return_arrive_end=%s WHERE id=%s",
                                (ob_s, ob_e, rt_s, rt_e, tid))
                            db.commit()
                            db.close()
                            tg_send(f"⏰ 行程 #{tid} 时间已更新\n🛫 去程{ob_s}-{ob_e}点 🛬 回程{rt_s}-{rt_e}点")
                        except Exception as e:
                            tg_send(f"❌ {e}")
                    else:
                        tg_send("格式: `/trip time 编号 去HH-HH 回HH-HH`")
                elif text == "/help":
                    _handle_help()
                elif ACK_KEYWORD in text:
                    pass

            state = load_state()
            state["last_tg_update_id"] = last_update_id
            save_state(state)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"TG 命令监听异常: {e}")
            await asyncio.sleep(10)

        await asyncio.sleep(1)
