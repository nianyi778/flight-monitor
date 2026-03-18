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
from app.scraper import capture_screenshots, capture_screenshots_batch
from app.analyzer import analyze_all_screenshots
from app.matcher import find_best_combinations
from app.notifier import tg_send, format_alert_message, _brief_price
from app.bot import setup_tg_commands, tg_command_listener, force_check_event


# 优雅关闭
shutdown_event = asyncio.Event()


def handle_signal(sig, frame):
    log.info(f"收到信号 {sig}，准备优雅关闭...")
    shutdown_event.set()


async def push_until_ack(msg):
    """每分钟推送直到确认（通过 bot listener 的 ack_received_event）"""
    from app.bot import ack_received_event

    ack_received_event.clear()
    push_count = 1
    state = load_state()

    while not shutdown_event.is_set():
        # 等待 PUSH_INTERVAL 秒，期间如果收到确认立即停止
        try:
            await asyncio.wait_for(ack_received_event.wait(), timeout=PUSH_INTERVAL)
            # event 被 set 了 = 收到确认
            log.info("✅ 收到确认回复，停止推送")
            state["pending_ack"] = False
            save_state(state)
            tg_send("✅ 已确认收到，停止推送。")
            return
        except asyncio.TimeoutError:
            pass

        push_count += 1
        log.info(f"📢 第 {push_count} 次推送...")
        tg_send(f"📢 第{push_count}次提醒\n\n{msg}")

        if push_count >= 60:
            log.warning("达到推送上限，停止")
            state["pending_ack"] = False
            save_state(state)
            break


def _get_check_interval_for_trip(trip):
    """根据出发倒计时决定检查频率（秒）"""
    from datetime import datetime
    try:
        ob_date = datetime.strptime(trip["outbound_date"], "%Y-%m-%d").date()
        days = (ob_date - now_jst().date()).days
        if days <= 0:
            return None  # 已过期
        elif days <= 30:
            return 3600      # <30天：每小时
        elif days <= 90:
            return 3600 * 3  # 30-90天：每3小时
        else:
            return 3600 * 6  # >90天：每6小时
    except:
        return 3600


def _trip_should_check(trip, state):
    """判断该行程本轮是否需要检查"""
    interval = _get_check_interval_for_trip(trip)
    if interval is None:
        return False  # 已过期

    last_key = f"trip_{trip['id']}_last_check"
    last_check = state.get(last_key)
    if not last_check:
        return True

    from datetime import datetime
    try:
        last_dt = datetime.fromisoformat(last_check)
        elapsed = (now_jst() - last_dt).total_seconds()
        return elapsed >= interval
    except:
        return True


def _collect_unique_searches(trips):
    """收集所有行程的搜索URL，按(url)去重，记录每个URL关联的行程"""
    from app.matcher import get_search_urls

    url_map = {}  # url -> {"search": {...}, "trip_ids": []}
    trip_search_map = {}  # trip_id -> [search_keys]

    for trip in trips:
        searches = get_search_urls(trip)
        trip_search_map[trip["id"]] = []
        for s in searches:
            url = s["url"]
            if url not in url_map:
                url_map[url] = {"search": s, "trip_ids": []}
            url_map[url]["trip_ids"].append(trip["id"])
            trip_search_map[trip["id"]].append(url)

    return url_map, trip_search_map


async def run_check(force=False):
    """
    智能巡查：
    1. 按出发倒计时分频（<30天每小时，30-90天每3小时，>90天每6小时）
    2. 相同日期的搜索去重（多行程共享截图+分析结果）
    3. 结果分发到各行程
    """
    import app.bot as bot_module

    if not force and already_checked_this_hour():
        log.info("⏭ 本小时已检查过，跳过（重启不重复查询）")
        return

    all_trips = get_active_trips()
    if not all_trips:
        log.warning("没有 active 行程，跳过检查")
        return

    bot_module.checking_in_progress = True

    state = load_state()
    check_count = state.get("check_count", 0) + 1
    state["check_count"] = check_count

    # 1. 筛选本轮需要检查的行程
    if force:
        due_trips = all_trips
    else:
        due_trips = [t for t in all_trips if _trip_should_check(t, state)]

    skipped = len(all_trips) - len(due_trips)
    if not due_trips:
        log.info(f"⏭ {len(all_trips)} 个行程均未到检查时间")
        save_state(state)
        bot_module.checking_in_progress = False
        return

    if skipped > 0:
        log.info(f"📋 本轮检查 {len(due_trips)}/{len(all_trips)} 个行程（{skipped}个未到频率）")

    # 2. 收集去重后的搜索URL
    url_map, trip_search_map = _collect_unique_searches(due_trips)
    total_unique = len(url_map)
    total_raw = sum(len(v) for v in trip_search_map.values())
    saved = total_raw - total_unique

    log.info(f"🔍 搜索去重: {total_raw}个 → {total_unique}个（节省{saved}次抓取）")

    # 3. 统一抓取去重后的截图
    from app.matcher import get_search_urls
    unique_searches = [v["search"] for v in url_map.values()]

    # 用第一个行程的 trip 对象启动浏览器（只需要 browser profile）
    screenshots = await capture_screenshots_batch(unique_searches)
    if not screenshots:
        log.error("未获取到任何截图")
        bot_module.checking_in_progress = False
        return

    log.info(f"获取到 {len(screenshots)} 张截图")

    # 4. 统一 LLM 分析（并行）
    from app.analyzer import analyze_screenshot
    from concurrent.futures import ThreadPoolExecutor, as_completed

    all_analysis = {}  # url -> analysis_result

    def _analyze(ss):
        analysis = analyze_screenshot(ss)
        analysis["source"] = ss["name"]
        analysis["url"] = ss["url"]
        analysis["flight_date"] = ss.get("flight_date", "")
        return ss["url"], analysis

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_analyze, ss): ss for ss in screenshots}
        for future in as_completed(futures):
            url, analysis = future.result()
            all_analysis[url] = analysis
            if analysis.get("error"):
                log.warning(f"  ⚠️ {analysis['error']}")
            elif analysis.get("flights"):
                log.info(f"  ✓ {len(analysis['flights'])} 个航班, 最低 ¥{analysis.get('lowest_price', '?')}")

    # 5. 分发结果到各行程
    brief_lines = [f"🕐 *{now_jst().strftime('%H:%M')} 巡查报告* (第{check_count}次 | {len(due_trips)}个行程 {total_unique}次抓取)\n"]

    for trip in due_trips:
        # 重组该行程的 results
        results = {"outbound": [], "return": [], "timestamp": now_jst().isoformat()}
        for url in trip_search_map.get(trip["id"], []):
            if url in all_analysis:
                a = all_analysis[url]
                direction = "outbound" if any(
                    s["url"] == url and s["direction"] == "outbound"
                    for s in get_search_urls(trip)
                ) else "return"
                results[direction].append(a)

        # 春秋官网直销价（零成本、100%准确）
        from app.spring_api import get_spring_price_for_trip
        spring = get_spring_price_for_trip(trip)
        spring_best = spring.get("best_combo")

        combos = find_best_combinations(results, trip)
        save_to_db(results, combos, trip)

        # 如果春秋官网比携程/Google更便宜，插入到 combos 最前面
        if spring_best and spring_best.get("total_cny"):
            ota_best = combos[0]["total"] if combos else 99999
            if spring_best["total_cny"] < ota_best:
                log.info(f"  🌸 春秋官网更便宜: ¥{spring_best['total_cny']} vs OTA ¥{ota_best}")
                combos.insert(0, {
                    "outbound": {
                        "airline": "春秋航空(官网直销)",
                        "departure_time": "", "arrival_time": "",
                        "price_cny": spring_best["outbound_cny"],
                        "original_currency": "USD",
                        "_source": "春秋官网",
                        "_url": f"https://en.ch.com/NRT-PVG/?date={spring_best['outbound_date']}",
                        "_flight_date": spring_best["outbound_date"],
                    },
                    "return": {
                        "airline": "春秋航空(官网直销)",
                        "departure_time": "", "arrival_time": "",
                        "price_cny": spring_best["return_cny"],
                        "original_currency": "USD",
                        "_source": "春秋官网",
                        "_url": f"https://en.ch.com/PVG-NRT/?date={spring_best['return_date']}",
                        "_flight_date": spring_best["return_date"],
                    },
                    "total": spring_best["total_cny"],
                    "within_budget": spring_best["total_cny"] <= trip["budget"],
                })

        # 更新检查时间
        state[f"trip_{trip['id']}_last_check"] = now_jst().isoformat()

        best_total = combos[0]["total"] if combos else None
        prev_best = trip.get("best_price")
        budget = trip["budget"]

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

            best_ob = combos[0]["outbound"] if combos else {}
            best_rt = combos[0]["return"] if combos else {}

            ob_date_tag = f" [{best_ob.get('_flight_date', '')}]" if best_ob.get('_flight_date') and best_ob.get('_flight_date') != trip['outbound_date'] else ""
            rt_date_tag = f" [{best_rt.get('_flight_date', '')}]" if best_rt.get('_flight_date') and best_rt.get('_flight_date') != trip['return_date'] else ""
            ob_info = f"{best_ob.get('airline', '?')} {best_ob.get('departure_time', '')}→{best_ob.get('arrival_time', '')} {_brief_price(best_ob)} ({best_ob.get('_source', '')}){ob_date_tag}" if best_ob else "无数据"
            rt_info = f"{best_rt.get('airline', '?')} {best_rt.get('departure_time', '')}→{best_rt.get('arrival_time', '')} {_brief_price(best_rt)} ({best_rt.get('_source', '')}){rt_date_tag}" if best_rt else "无数据"

            interval = _get_check_interval_for_trip(trip)
            freq = f"{interval//3600}h" if interval else "?"

            brief_lines.append(
                f"✈️ *#{trip['id']}* {trip['outbound_date']}→{trip['return_date']} (¥{budget} 频率{freq})\n"
                f"  最低: ¥{best_total or '?'}{trend} | 差预算: {diff}\n"
                f"  去: {ob_info}\n"
                f"  回: {rt_info}\n"
                f"  历史最低: ¥{new_best if new_best < 99999 else '?'}"
            )

    save_state(state)

    # 发送汇总简报
    s = load_state()
    if not s.get("pending_ack") and len(brief_lines) > 1:
        tg_send("\n\n".join(brief_lines))

    bot_module.checking_in_progress = False


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

    # 启动 TG 命令监听（后台，必须在 push_until_ack 之前）
    tg_listener_task = asyncio.create_task(tg_command_listener())

    # 首次检查是否有未确认的通知（重启后继续推送）
    if state.get("pending_ack") and state.get("last_alert_msg"):
        log.info("发现未确认的通知，继续推送...")
        await push_until_ack(state["last_alert_msg"])

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
