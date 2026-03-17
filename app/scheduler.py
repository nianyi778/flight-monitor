"""
调度模块 - 主循环 + 定时检查 + 信号处理
"""

import asyncio
import random
import signal

from app.config import (
    LLM_API_KEY, TG_BOT_TOKEN, CHECK_INTERVAL, PUSH_INTERVAL,
    DATA_DIR, now_jst, log, load_state, save_state,
)
from app.db import (
    get_active_trips, update_trip_best_price, save_to_db,
    already_checked_this_hour,
)
from app.scraper import capture_screenshots
from app.analyzer import analyze_all_screenshots
from app.matcher import find_best_combinations
from app.notifier import tg_send, tg_check_ack, format_alert_message, _brief_price
from app.bot import setup_tg_commands, tg_command_listener, force_check_event


# 优雅关闭
shutdown_event = asyncio.Event()


def handle_signal(sig, frame):
    log.info(f"收到信号 {sig}，准备优雅关闭...")
    shutdown_event.set()


async def push_until_ack(msg):
    """每分钟推送直到确认"""
    push_count = 1
    state = load_state()

    while not shutdown_event.is_set():
        await asyncio.sleep(PUSH_INTERVAL)

        if tg_check_ack():
            log.info("✅ 收到确认回复，停止推送")
            state["pending_ack"] = False
            save_state(state)
            tg_send("✅ 已确认收到，停止推送。")
            break

        push_count += 1
        log.info(f"📢 第 {push_count} 次推送...")
        tg_send(f"📢 *第{push_count}次提醒*\n\n{msg}")

        if push_count >= 60:
            log.warning("达到推送上限，停止")
            state["pending_ack"] = False
            save_state(state)
            break


async def run_check(force=False):
    """执行一次完整的价格检查（遍历所有 active 行程）"""
    if not force and already_checked_this_hour():
        log.info("⏭ 本小时已检查过，跳过（重启不重复查询）")
        return

    trips = get_active_trips()
    if not trips:
        log.warning("没有 active 行程，跳过检查")
        return

    state = load_state()
    check_count = state.get("check_count", 0) + 1
    state["check_count"] = check_count
    save_state(state)

    brief_lines = [f"🕐 *{now_jst().strftime('%H:%M')} 巡查报告* (第{check_count}次)\n"]

    for trip in trips:
        log.info("=" * 50)
        log.info(f"✈️ 检查行程#{trip['id']}: {trip['outbound_date']} → {trip['return_date']} (预算¥{trip['budget']})")

        screenshots = await capture_screenshots(trip)
        if not screenshots:
            log.error(f"行程#{trip['id']} 未获取到截图")
            brief_lines.append(f"❌ 行程#{trip['id']} 抓取失败")
            continue

        results = analyze_all_screenshots(screenshots)
        combos = find_best_combinations(results, trip)
        save_to_db(results, combos, trip)

        best_total = combos[0]["total"] if combos else None
        prev_best = trip.get("best_price")
        budget = trip["budget"]

        # 更新历史最低
        if best_total:
            update_trip_best_price(trip["id"], best_total)

        hit_budget = best_total and best_total <= budget
        price_dropped = best_total and prev_best and best_total < prev_best * 0.95

        if hit_budget or price_dropped:
            msg = format_alert_message(combos, results, trip)
            log.info(f"\n{msg}")
            tg_send(msg)
            s = load_state()
            s["pending_ack"] = True
            s["last_alert_msg"] = msg
            save_state(s)
            await push_until_ack(msg)
        else:
            # 简报
            trend = ""
            if prev_best and best_total:
                if best_total < prev_best:
                    trend = f" 📉↓¥{prev_best - best_total}"
                elif best_total > prev_best:
                    trend = f" 📈↑¥{best_total - prev_best}"
                else:
                    trend = " ➡️持平"

            diff = f"¥{best_total - budget}" if best_total else "?"
            new_best = min(best_total or 99999, prev_best or 99999)

            # 提取去程/回程最优航班详情
            best_ob = combos[0]["outbound"] if combos else {}
            best_rt = combos[0]["return"] if combos else {}

            ob_info = f"{best_ob.get('airline', '?')} {best_ob.get('departure_time', '')}→{best_ob.get('arrival_time', '')} {_brief_price(best_ob)} ({best_ob.get('_source', '')})" if best_ob else "无数据"
            rt_info = f"{best_rt.get('airline', '?')} {best_rt.get('departure_time', '')}→{best_rt.get('arrival_time', '')} {_brief_price(best_rt)} ({best_rt.get('_source', '')})" if best_rt else "无数据"

            brief_lines.append(
                f"✈️ *#{trip['id']}* {trip['outbound_date']}→{trip['return_date']} (预算¥{budget})\n"
                f"  最低往返: ¥{best_total or '?'}(CNY){trend} | 差预算: {diff}\n"
                f"  去程: {ob_info}\n"
                f"  回程: {rt_info}\n"
                f"  历史最低: ¥{new_best if new_best < 99999 else '?'}(CNY)"
            )

    # 发送汇总简报（没有触发好价推送时）
    state = load_state()
    if not state.get("pending_ack") and len(brief_lines) > 1:
        tg_send("\n\n".join(brief_lines))


async def main():
    """主循环：定时执行价格检查"""
    # 确保数据目录存在
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 注册信号处理
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    trips = get_active_trips()
    log.info("=" * 55)
    log.info("✈️ 机票价格监控系统启动 (Docker)")
    log.info(f"   监控行程: {len(trips)} 个")
    log.info(f"   检查间隔: ~{CHECK_INTERVAL}s (随机抖动)")
    log.info(f"   TG通知: {'已配置' if TG_BOT_TOKEN else '⚠️ 未配置'}")
    log.info("=" * 55)

    # 检查必要配置
    if not LLM_API_KEY:
        log.error("❌ LLM_API_KEY 未配置，退出")
        return

    # 设置 TG Bot 菜单命令
    setup_tg_commands()

    # 🟢 启动打招呼（健康检查）
    state = load_state()
    boot_count = state.get("boot_count", 0) + 1
    state["boot_count"] = boot_count
    save_state(state)
    tg_send(
        f"🟢 *机票监控系统已上线* (第{boot_count}次启动)\n\n"
        f"📊 监控行程: {len(get_active_trips())} 个\n"
        f"⏰ 约每 {CHECK_INTERVAL//60} 分钟巡查（随机抖动防检测）\n\n"
        f"💡 可用命令:\n"
        f"/check - 立即查价\n"
        f"/status - 系统状态\n"
        f"/history - 价格趋势\n"
        f"/trips - 查看所有行程\n"
        f"/trip add 去程 回程 预算 - 添加行程\n"
        f"/trip del 编号 - 删除行程"
    )

    # 首次检查是否有未确认的通知
    if state.get("pending_ack") and state.get("last_alert_msg"):
        log.info("发现未确认的通知，继续推送...")
        if tg_check_ack():
            state["pending_ack"] = False
            save_state(state)
        else:
            await push_until_ack(state["last_alert_msg"])

    # 启动 TG 命令监听（后台）
    tg_listener_task = asyncio.create_task(tg_command_listener())

    # 主循环
    is_force = False
    while not shutdown_event.is_set():
        try:
            await run_check(force=is_force)
        except Exception as e:
            log.error(f"检查异常: {e}", exc_info=True)
            tg_send(f"⚠️ 机票监控异常: {e}")

        is_force = False
        # 等待下一次检查（可被 shutdown 或 /check 命令中断）
        # 随机间隔：CHECK_INTERVAL ± 30%，防止被目标站点识别为定时爬虫
        jitter = random.uniform(0.7, 1.3)
        wait_time = int(CHECK_INTERVAL * jitter)
        log.info(f"⏰ 下次检查: {wait_time}s 后 (随机抖动)")
        force_check_event.clear()
        done, _ = await asyncio.wait(
            [asyncio.create_task(shutdown_event.wait()),
             asyncio.create_task(force_check_event.wait())],
            timeout=wait_time,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if force_check_event.is_set():
            log.info("📢 收到 /check 命令，立即执行检查")
            is_force = True

    tg_listener_task.cancel()
    log.info("🛑 监控系统已停止")
