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
    """校验行程输入，返回 (parsed_data, error_msg)"""
    parts = text.split()
    if len(parts) < 4:
        return None, (
            "✈️ *添加行程*\n\n"
            "格式: `/trip add 去程 回程 [预算] [去H-H] [回H-H]`\n\n"
            "例:\n"
            "`/trip add 2026-09-18 2026-09-28 1500 去19-23 回0-6`\n"
            "`/trip add 2026-09-18 2026-09-28 1500`\n"
            "`/trip add 2026-12-28 2027-01-05`  (默认¥1500)"
        )

    errors = []
    ob_d, rt_d = parts[2], parts[3]

    # 1. 日期格式校验
    try:
        ob_date = datetime.strptime(ob_d, "%Y-%m-%d").date()
    except ValueError:
        errors.append(f"❌ 去程日期格式错误: `{ob_d}` (应为 YYYY-MM-DD)")

    try:
        rt_date = datetime.strptime(rt_d, "%Y-%m-%d").date()
    except ValueError:
        errors.append(f"❌ 回程日期格式错误: `{rt_d}` (应为 YYYY-MM-DD)")

    if errors:
        return None, "\n".join(errors)

    # 2. 日期逻辑校验
    today = now_jst().date()
    if ob_date <= today:
        errors.append(f"❌ 去程日期 `{ob_d}` 已过期（今天是 {today}）")
    if rt_date <= ob_date:
        errors.append(f"❌ 回程日期 `{rt_d}` 必须晚于去程 `{ob_d}`")
    if (rt_date - ob_date).days > 30:
        errors.append(f"⚠️ 行程跨度 {(rt_date - ob_date).days} 天，超过30天，确认日期是否正确？")

    # 3. 解析可选参数
    bgt = 1500
    ob_start, ob_end = 19, 23
    rt_start, rt_end = 0, 6

    for p in parts[4:]:
        if p.startswith("去"):
            try:
                s, e = [int(x) for x in p.replace("去", "").split("-")]
                if not (0 <= s <= 23 and 0 <= e <= 23):
                    errors.append(f"❌ 去程时间 `{p}` 超出范围 (0-23)")
                elif s > e:
                    errors.append(f"❌ 去程时间 `{p}` 起始应小于结束")
                else:
                    ob_start, ob_end = s, e
            except:
                errors.append(f"❌ 去程时间格式错误: `{p}` (应为 去H-H)")
        elif p.startswith("回"):
            try:
                s, e = [int(x) for x in p.replace("回", "").split("-")]
                if not (0 <= s <= 23 and 0 <= e <= 23):
                    errors.append(f"❌ 回程时间 `{p}` 超出范围 (0-23)")
                else:
                    rt_start, rt_end = s, e
            except:
                errors.append(f"❌ 回程时间格式错误: `{p}` (应为 回H-H)")
        elif p.isdigit():
            bgt = int(p)
            if bgt < 100 or bgt > 50000:
                errors.append(f"❌ 预算 ¥{bgt} 不合理 (范围 100-50000)")
        else:
            errors.append(f"❌ 无法识别参数: `{p}`")

    if errors:
        return None, "\n".join(errors)

    return {
        "ob_d": ob_d, "rt_d": rt_d, "budget": bgt,
        "ob_start": ob_start, "ob_end": ob_end,
        "rt_start": rt_start, "rt_end": rt_end,
    }, None


def _handle_trip_add(text):
    """校验 → 预览确认 → 等用户点按钮才写入数据库"""
    data, error = _validate_trip_input(text)
    if error:
        tg_send_with_buttons(
            f"{error}\n\n💡 正确格式:\n`/trip add 2026-09-18 2026-09-28 1500 去19-23 回0-6`",
            [[{"text": "📖 查看帮助", "callback_data": "trip_add_guide"}]]
        )
        return

    # 生成预览卡片，不写入数据库
    countdown = _days_until(data["ob_d"])
    days = (datetime.strptime(data["rt_d"], "%Y-%m-%d").date() -
            datetime.strptime(data["ob_d"], "%Y-%m-%d").date()).days

    # 把校验通过的数据编码到 callback_data 里
    cb_data = (f"trip_confirm_{data['ob_d']}_{data['rt_d']}_{data['budget']}_"
               f"{data['ob_start']}-{data['ob_end']}_{data['rt_start']}-{data['rt_end']}")

    tg_send_with_buttons(
        f"✈️ *请确认行程信息*\n\n"
        f"📅 去程: {data['ob_d']}  ({countdown})\n"
        f"📅 回程: {data['rt_d']}  (共{days}天)\n"
        f"💰 预算: ¥{data['budget']:,}(CNY)\n"
        f"🛫 去程出发: {data['ob_start']}:00-{data['ob_end']}:00\n"
        f"🛬 回程到达: {data['rt_start']}:00-{data['rt_end']}:00\n\n"
        f"信息正确吗？",
        [
            [{"text": "✅ 确认添加", "callback_data": cb_data},
             {"text": "❌ 取消", "callback_data": "cancel_add"}],
        ]
    )


def _health_check():
    """健康检查：LLM / 数据库 / 代理 / TG"""
    from app.config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, PROXY_URL
    import requests as req

    checks = {}

    # 1. LLM
    try:
        r = req.post(f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
            json={"model": LLM_MODEL, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5},
            timeout=10)
        checks["llm"] = "✅" if r.ok else f"❌ {r.status_code}"
    except Exception as e:
        checks["llm"] = f"❌ {e}"

    # 2. 数据库
    try:
        db = get_db()
        c = db.cursor()
        c.execute("SELECT 1")
        db.close()
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
    from app.config import LLM_BASE_URL, LLM_MODEL, PROXY_URL

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
        f"🤖 模型: `{LLM_MODEL}`",
        f"🔗 API: `{LLM_BASE_URL.split('//')[1][:30]}`",
        f"🏠 代理: `{PROXY_URL or '未配置'}`",
    ]

    # 行程摘要
    if trips:
        lines.append("")
        lines.append("━━━ 行程 ━━━")
        for t in trips:
            best = t.get("best_price")
            best_str = f"¥{best:,}" if best else "暂无"
            lines.append(f"#{t['id']} {t['outbound_date']}→{t['return_date']} 预算¥{t['budget']:,} 最低{best_str}")

    tg_send_with_buttons("\n".join(lines), [
        [{"text": "🩺 健康检查", "callback_data": "health_check"},
         {"text": "🔍 立即查价", "callback_data": "do_check"}],
        [{"text": "✈️ 行程管理", "callback_data": "show_trips"}],
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
        lines.append(f"🤖 LLM: {checks['llm']}")
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
            "请输入（复制修改日期即可）：\n\n"
            "`/trip add 2026-09-18 2026-09-28 1500 去19-23 回0-6`\n\n"
            "格式: 去程 回程 [预算] [去H-H] [回H-H]\n"
            "预算默认¥1500，时间默认去19-23 回0-6"
        )

    elif data == "cancel_add":
        tg_answer_callback(callback_id, "已取消")
        tg_edit_message(message_id, "❌ 已取消添加")

    elif data.startswith("trip_confirm_"):
        # trip_confirm_2026-09-18_2026-09-28_1500_19-23_0-6
        try:
            _, _, ob_d, rt_d, bgt_str, ob_time, rt_time = data.split("_", 6)
            bgt = int(bgt_str)
            ob_s, ob_e = [int(x) for x in ob_time.split("-")]
            rt_s, rt_e = [int(x) for x in rt_time.split("-")]

            db = get_db()
            c = db.cursor()
            c.execute(
                "INSERT INTO trips (outbound_date, return_date, budget, "
                "outbound_depart_start, outbound_depart_end, return_arrive_start, return_arrive_end) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (ob_d, rt_d, bgt, ob_s, ob_e, rt_s, rt_e)
            )
            db.commit()
            new_id = c.lastrowid
            db.close()

            tg_answer_callback(callback_id, f"✅ 行程#{new_id}已添加")
            countdown = _days_until(ob_d)
            tg_edit_message(message_id,
                f"✅ *行程#{new_id} 已添加!*\n\n"
                f"📅 {ob_d} → {rt_d}  ({countdown})\n"
                f"💰 ¥{bgt:,}(CNY)\n"
                f"🛫 {ob_s}:00-{ob_e}:00  🛬 {rt_s}:00-{rt_e}:00\n\n"
                f"系统将在下次巡查时开始监控此行程",
                [[{"text": "🔍 立即查价", "callback_data": "do_check"},
                  {"text": "✈️ 查看行程", "callback_data": "show_trips"}]]
            )
        except Exception as e:
            tg_answer_callback(callback_id, f"添加失败")
            tg_send(f"❌ 添加失败: {e}")

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

    elif data.startswith("trip_edit_"):
        tid = data.split("_")[-1]
        tg_answer_callback(callback_id)
        # 查当前值显示
        try:
            db = get_db()
            c = db.cursor()
            c.execute("SELECT outbound_date, return_date, budget, outbound_depart_start, outbound_depart_end, return_arrive_start, return_arrive_end, outbound_flex, return_flex FROM trips WHERE id=%s", (tid,))
            r = c.fetchone()
            db.close()
            if r:
                ob_flex = r[7] or 0
                rt_flex = r[8] if r[8] is not None else 1
                tg_send_with_buttons(
                    f"✏️ *编辑行程 #{tid}*\n\n"
                    f"📅 去程: `{r[0]}` (弹性±{ob_flex}天)\n"
                    f"📅 回程: `{r[1]}` (弹性±{rt_flex}天)\n"
                    f"💰 预算: `¥{r[2]:,}`\n"
                    f"🛫 去程出发: `{r[3]}-{r[4]}点`\n"
                    f"🛬 回程到达: `{r[5]}-{r[6]}点`\n\n"
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
                    lines.append(f"🤖 LLM: {checks['llm']}")
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
                elif text.startswith("/trip date"):
                    parts = text.split()
                    if len(parts) >= 5:
                        try:
                            tid = int(parts[2])
                            ob_d, rt_d = parts[3], parts[4]
                            # 校验
                            ob_date = datetime.strptime(ob_d, "%Y-%m-%d").date()
                            rt_date = datetime.strptime(rt_d, "%Y-%m-%d").date()
                            if rt_date <= ob_date:
                                tg_send("❌ 回程日期必须晚于去程")
                            else:
                                db = get_db()
                                c = db.cursor()
                                c.execute("UPDATE trips SET outbound_date=%s, return_date=%s WHERE id=%s",
                                          (ob_d, rt_d, tid))
                                db.commit()
                                db.close()
                                tg_send(f"📅 行程 #{tid} 日期已更新\n去程: {ob_d} → 回程: {rt_d}")
                        except ValueError:
                            tg_send("❌ 日期格式错误，请用 YYYY-MM-DD")
                        except Exception as e:
                            tg_send(f"❌ {e}")
                    else:
                        tg_send("格式: `/trip date 编号 去程 回程`\n例: `/trip date 1 2026-09-18 2026-09-28`")
                elif text.startswith("/trip flex"):
                    parts = text.split()
                    if len(parts) >= 5:
                        try:
                            tid = int(parts[2])
                            ob_flex = int(parts[3].replace("去", ""))
                            rt_flex = int(parts[4].replace("回", ""))
                            if ob_flex < 0 or rt_flex < 0 or ob_flex > 7 or rt_flex > 7:
                                tg_send("❌ 弹性天数范围 0-7")
                            else:
                                db = get_db()
                                c = db.cursor()
                                c.execute("UPDATE trips SET outbound_flex=%s, return_flex=%s WHERE id=%s",
                                          (ob_flex, rt_flex, tid))
                                db.commit()
                                db.close()
                                tg_send(f"📆 行程 #{tid} 弹性已更新\n去程±{ob_flex}天 回程±{rt_flex}天")
                        except ValueError:
                            tg_send("❌ 格式: `/trip flex 编号 去N 回N`")
                        except Exception as e:
                            tg_send(f"❌ {e}")
                    else:
                        tg_send("格式: `/trip flex 编号 去N 回N`\n例: `/trip flex 1 去0 回1`")
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
