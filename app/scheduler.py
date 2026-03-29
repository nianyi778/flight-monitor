"""
调度模块 - 主循环 + 定时检查 + 信号处理
"""

import asyncio
import random
import signal

from app.anti_bot import finalize_result_status, make_result
from app.config import (
    TG_BOT_TOKEN,
    CHECK_INTERVAL,
    PUSH_INTERVAL,
    DATA_DIR,
    now_jst,
    log,
    load_state,
    save_state,
)
from app.db import (
    get_active_trips,
    update_trip_best_price,
    save_to_db,
    already_checked_this_hour,
    cleanup_expired_pending_trips,
)
from app.matcher import find_best_combinations, get_search_urls
from app.notifier import tg_send, format_alert_message, _brief_price
from app.bot import setup_tg_commands, tg_command_listener, force_check_event
from app.source_runtime import (
    choose_proxy,
    ensure_runtime_state,
    finalize_check_metrics,
    force_source_cooldown,
    get_cached_search_result,
    get_source_status_snapshot,
    init_check_metrics,
    penalize_proxy,
    proxy_pool_summary,
    record_check_metric_event,
    record_proxy_outcome,
    record_source_outcome,
    source_in_cooldown,
    store_cached_search_result,
)


# 优雅关闭
shutdown_event = asyncio.Event()


def _make_spring_url(date_str: str, orig: str, dest: str) -> str:
    """构造春秋官网搜索 URL（flights.ch.com 要求日期去前导零，如 2026-4-20）。"""
    parts = (date_str or "").split("-")
    if len(parts) != 3:
        return ""
    try:
        date_no_lz = f"{parts[0]}-{int(parts[1])}-{int(parts[2])}"
    except ValueError:
        return ""
    return (
        f"https://flights.ch.com/{orig.upper()}-{dest.upper()}.html"
        f"?Mtype=0&SType=0&IfRet=false&FDate={date_no_lz}&ActId=&IsNew=1"
    )


def handle_signal(sig, frame):
    log.info(f"收到信号 {sig}，准备优雅关闭...")
    shutdown_event.set()


async def push_until_ack(msg, state):
    """每分钟推送直到确认（通过 bot listener 的 ack_received_event）。
    state 由调用方传入，避免独立 load_state() 导致的 pending_ack 竞态覆盖。
    """
    from app.bot import ack_received_event

    ack_received_event.clear()
    push_count = 1

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
            return 3600  # <30天：每小时
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


def _record_results_for_source(state, source_name, results, searches):
    now_dt = now_jst()
    by_url = {s["url"]: s for s in searches}
    saw_ok = False
    saw_bad = False
    last_reason = None

    for url, result in results.items():
        result["source_runtime"] = source_name
        search = by_url.get(url)
        if search:
            store_cached_search_result(state, search, result, now_dt)
        status = result.get("status", "no_data")
        if status == "ok":
            saw_ok = True
        elif status in {"blocked", "degraded"}:
            saw_bad = True
            last_reason = result.get("block_reason") or result.get("error")
        diagnosis = result.get("diagnosis") or {}
        action = diagnosis.get("action")
        if action == "cooldown":
            status = "blocked"
            saw_bad = True
            last_reason = diagnosis.get("reason") or last_reason
            force_source_cooldown(
                state,
                source_name,
                diagnosis.get("reason") or last_reason,
                now_dt,
                seconds=diagnosis.get("retry_after_seconds") or None,
            )
        elif action == "switch_proxy":
            penalize_proxy(
                state, result.get("proxy_id"), source_name, now_dt, hard=True
            )
        elif action == "raise_alert":
            state.setdefault("runtime_alerts", []).append(
                {
                    "source": source_name,
                    "reason": diagnosis.get("reason") or result.get("error"),
                    "time": now_dt.isoformat(),
                }
            )
            state["runtime_alerts"] = state["runtime_alerts"][-20:]
        record_proxy_outcome(state, result.get("proxy_id"), source_name, status, now_dt)

    if saw_ok:
        record_source_outcome(state, source_name, "ok", None, now_dt)
    elif saw_bad:
        status = next(
            (
                r.get("status")
                for r in results.values()
                if r.get("status") in {"blocked", "degraded"}
            ),
            "degraded",
        )
        record_source_outcome(state, source_name, status, last_reason, now_dt)


def _load_cached_results(state, searches):
    now_dt = now_jst()
    cached = {}
    remaining = []
    source_name_map = {
        "kiwi": "kiwi_api",
        "google": "google_api",
        "spring": "spring_api",
    }
    for search in searches:
        hit = get_cached_search_result(state, search, now_dt)
        if hit:
            hit["source_runtime"] = source_name_map.get(
                search.get("source_type"), hit.get("source_runtime", "unknown")
            )
            cached[search["url"]] = hit
        else:
            remaining.append(search)
    return cached, remaining


def _log_request_result(result, trip_ids=None):
    trip_ids = trip_ids or []
    log.info(
        "source=%s mode=%s route=%s-%s date=%s status=%s block=%s cache=%s proxy=%s profile=%s flights=%s trips=%s",
        result.get("source", ""),
        result.get("request_mode", ""),
        result.get("origin", "") or "",
        result.get("destination", "") or "",
        result.get("flight_date", ""),
        result.get("status", ""),
        result.get("block_reason", ""),
        result.get("from_cache", False),
        result.get("proxy_id", ""),
        result.get("profile_id", ""),
        len(result.get("flights", [])),
        ",".join(str(t) for t in trip_ids),
    )


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

    cleanup_expired_pending_trips()
    all_trips = get_active_trips()
    if not all_trips:
        log.warning("没有 active 行程，跳过检查")
        return

    bot_module.checking_in_progress = True
    try:
        await _run_check_inner(force, all_trips, bot_module)
    finally:
        bot_module.checking_in_progress = False


async def _run_check_inner(force, all_trips, bot_module):
    """run_check 的实际逻辑，由 run_check 负责重置 checking_in_progress 标志"""
    state = load_state()
    ensure_runtime_state(state)
    started_at = now_jst()
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
        return

    if skipped > 0:
        log.info(
            f"📋 本轮检查 {len(due_trips)}/{len(all_trips)} 个行程（{skipped}个未到频率）"
        )

    # 2. 收集去重后的搜索URL
    url_map, trip_search_map = _collect_unique_searches(due_trips)
    total_unique = len(url_map)
    total_raw = sum(len(v) for v in trip_search_map.values())
    saved = total_raw - total_unique
    check_metrics = init_check_metrics(
        check_id=check_count,
        due_trips=len(due_trips),
        total_searches=total_unique,
        started_at=started_at,
    )

    log.info(f"🔍 搜索去重: {total_raw}个 → {total_unique}个（节省{saved}次抓取）")

    # 3. API 瀑布调用（Kiwi → Google → 春秋官网）
    unique_searches = [v["search"] for v in url_map.values()]

    kiwi_searches = [s for s in unique_searches if s.get("source_type") == "kiwi"]
    google_searches = [s for s in unique_searches if s.get("source_type") == "google"]

    all_analysis = {}  # url -> analysis_result

    # — Kiwi.com GraphQL API（零认证，覆盖 MU/CA/NH/9C/HO 等全航司）—
    if kiwi_searches:
        from app.kiwi_api import get_kiwi_flights_for_searches

        cached, remaining = _load_cached_results(state, kiwi_searches)
        all_analysis.update(cached)
        if not source_in_cooldown(state, "kiwi_api", now_jst()) and remaining:
            proxy = choose_proxy(state, "kiwi_api", now_jst())
            fetched = await asyncio.to_thread(
                get_kiwi_flights_for_searches,
                remaining, proxy_url=proxy.get("url"), proxy_id=proxy.get("id"),
            )
            all_analysis.update(fetched)
            _record_results_for_source(state, "kiwi_api", fetched, remaining)

    # — Google Flights API —
    if google_searches:
        from app.google_flights_api import get_google_flights_for_searches

        cached, remaining = _load_cached_results(state, google_searches)
        all_analysis.update(cached)
        if not source_in_cooldown(state, "google_api", now_jst()) and remaining:
            proxy = choose_proxy(state, "google_api", now_jst())
            fetched = await asyncio.to_thread(
                get_google_flights_for_searches,
                remaining, proxy_url=proxy.get("url"), proxy_id=proxy.get("id"),
            )
            all_analysis.update(fetched)
            _record_results_for_source(state, "google_api", fetched, remaining)

    # 汇总日志
    got = sum(1 for a in all_analysis.values() if a.get("flights"))
    log.info(f"  📊 API结果: {got}/{len(unique_searches)} 个搜索有航班数据")
    for url, a in all_analysis.items():
        record_check_metric_event(
            check_metrics,
            a.get(
                "source_runtime",
                a.get("source_type")
                or a.get("request_mode")
                or a.get("source")
                or "unknown",
            ),
            from_cache=bool(a.get("from_cache")),
            status=a.get("status", "no_data"),
            has_flights=bool(a.get("flights")),
            request_mode=a.get("request_mode", "api"),
        )
        _log_request_result(a, trip_ids=url_map.get(url, {}).get("trip_ids", []))
        if a.get("error") and not a.get("flights"):
            log.warning(f"  ⚠️ {a['error']}")
            if a.get("diagnosis"):
                log.warning(
                    f"  🤖 诊断: {a['diagnosis'].get('action')} / {a['diagnosis'].get('reason')}"
                )
        elif a.get("flights"):
            log.info(
                f"  ✓ {a.get('source', '')} {len(a['flights'])} 个航班, 最低 ¥{a.get('lowest_price', '?')}"
            )

    if state.get("runtime_alerts"):
        latest = state["runtime_alerts"][-1]
        log.warning(f"🚨 最近告警: {latest.get('source')} / {latest.get('reason')}")
    check_metrics["alerts"] = len(state.get("runtime_alerts", []))

    # 5. 分发结果到各行程
    brief_lines = [
        f"🕐 *{now_jst().strftime('%H:%M')} 巡查报告* (第{check_count}次 | {len(due_trips)}个行程 {total_unique}次抓取)\n"
    ]

    # 本轮所有行程共享春秋价格缓存 {(origin, dest, month): prices}
    # 相同路线+相同月份只发一次 HTTP 请求，其余从缓存返回
    spring_price_cache: dict = {}

    for trip in due_trips:
        try:
            # 重组该行程的 results
            results = {"outbound": [], "return": [], "timestamp": now_jst().isoformat()}
            for url in trip_search_map.get(trip["id"], []):
                if url in all_analysis:
                    a = all_analysis[url]
                    direction = (
                        "outbound"
                        if any(
                            s["url"] == url and s["direction"] == "outbound"
                            for s in get_search_urls(trip)
                        )
                        else "return"
                    )
                    results[direction].append(a)

            is_one_way = trip.get("trip_type") == "one_way"

            # 春秋官网直销价（零成本、100%准确）
            from app.spring_api import get_spring_price_for_trip

            spring_proxy = choose_proxy(state, "spring_api", now_jst())
            spring = await asyncio.to_thread(
                get_spring_price_for_trip,
                trip, proxy_url=spring_proxy.get("url"), proxy_id=spring_proxy.get("id"),
                price_cache=spring_price_cache,
            )
            record_source_outcome(
                state,
                "spring_api",
                spring.get("status", "no_data"),
                spring.get("block_reason"),
                now_jst(),
            )
            record_proxy_outcome(
                state,
                spring.get("proxy_id"),
                "spring_api",
                spring.get("status", "no_data"),
                now_jst(),
            )
            spring_best = spring.get("best_combo")

            # 将春秋直销价加入 results，确保写入 flight_prices 历史记录
            spring_directions = [("outbound", "outbound")] if is_one_way else [("outbound", "outbound"), ("return", "return")]
            for direction, key in spring_directions:
                spring_leg = spring.get(key)
                if spring_leg and spring_leg.get("price_cny"):
                    route = spring_leg.get("route", "")
                    split = route.split("→")
                    parts = split if len(split) == 2 else ["", ""]
                    results[direction].append({
                        "source": f"春秋{parts[0]}{parts[1]}",
                        "url": _make_spring_url(spring_leg.get("date") or "", parts[0], parts[1]),
                        "flight_date": spring_leg.get("date"),
                        "lowest_price": spring_leg.get("price_cny"),
                        "flights": [{
                            "airline": "春秋航空",
                            "flight_no": "",
                            "departure_time": "",
                            "arrival_time": "",
                            "origin": parts[0],
                            "destination": parts[1],
                            "price_cny": spring_leg.get("price_cny"),
                            "original_price": None,
                            "original_currency": "CNY",
                            "stops": 0,
                            "via": "",
                        }],
                    })

            combos = find_best_combinations(results, trip)
            save_to_db(results, combos, trip)

            # 如果春秋官网比携程/LetsFG/Google 更便宜，插入到 combos 最前面
            if spring_best and spring_best.get("total_cny") and not is_one_way:
                ota_best = combos[0]["total"] if combos else 99999
                if spring_best["total_cny"] < ota_best:
                    log.info(
                        f"  🌸 春秋官网更便宜: ¥{spring_best['total_cny']} vs OTA ¥{ota_best}"
                    )
                    combos.insert(
                        0,
                        {
                            "outbound": {
                                "airline": "春秋航空(官网直销)",
                                "departure_time": "",
                                "arrival_time": "",
                                "price_cny": spring_best["outbound_cny"],
                                "original_currency": "USD",
                                "_source": "春秋官网",
                                "_url": _make_spring_url(spring_best['outbound_date'] or '', *spring_best['outbound_route'].split('→')),
                                "_flight_date": spring_best["outbound_date"],
                            },
                            "return": {
                                "airline": "春秋航空(官网直销)",
                                "departure_time": "",
                                "arrival_time": "",
                                "price_cny": spring_best["return_cny"],
                                "original_currency": "USD",
                                "_source": "春秋官网",
                                "_url": _make_spring_url(spring_best['return_date'] or '', *spring_best['return_route'].split('→')),
                                "_flight_date": spring_best["return_date"],
                            },
                            "total": spring_best["total_cny"],
                            "within_budget": spring_best["total_cny"] <= trip["budget"],
                        },
                    )

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
                # 使用同一个 state dict，避免独立 load/save 相互覆盖 pending_ack
                state["pending_ack"] = True
                state["last_alert_msg"] = msg
                save_state(state)
                # 检查已完成，在等待用户确认期间放开 checking_in_progress
                # 否则"立即查价"按钮会永远显示"查价中"直到用户发送"确认收到"
                bot_module.checking_in_progress = False
                await push_until_ack(msg, state)
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
                best_rt = (combos[0]["return"] if combos else None) or {}

                ob_date_tag = (
                    f" [{best_ob.get('_flight_date', '')}]"
                    if best_ob.get("_flight_date")
                    and best_ob.get("_flight_date") != trip["outbound_date"]
                    else ""
                )
                rt_date_tag = (
                    f" [{best_rt.get('_flight_date', '')}]"
                    if best_rt.get("_flight_date")
                    and best_rt.get("_flight_date") != trip.get("return_date")
                    else ""
                )
                ob_info = (
                    f"{best_ob.get('airline', '?')} {best_ob.get('departure_time', '')}→{best_ob.get('arrival_time', '')} {_brief_price(best_ob)} ({best_ob.get('_source', '')}){ob_date_tag}"
                    if best_ob
                    else "无数据"
                )

                interval = _get_check_interval_for_trip(trip)
                freq = f"{interval // 3600}h" if interval else "?"
                type_tag = "单程" if is_one_way else ""
                date_range = trip["outbound_date"] if is_one_way else f"{trip['outbound_date']}→{trip.get('return_date', '?')}"

                if is_one_way:
                    brief_lines.append(
                        f"✈️ *#{trip['id']}* {date_range} {type_tag} (¥{budget} 频率{freq})\n"
                        f"  最低: ¥{best_total or '?'}{trend} | 差预算: {diff}\n"
                        f"  去: {ob_info}\n"
                        f"  历史最低: ¥{new_best if new_best < 99999 else '?'}"
                    )
                else:
                    rt_info = (
                        f"{best_rt.get('airline', '?')} {best_rt.get('departure_time', '')}→{best_rt.get('arrival_time', '')} {_brief_price(best_rt)} ({best_rt.get('_source', '')}){rt_date_tag}"
                        if best_rt
                        else "无数据"
                    )
                    brief_lines.append(
                        f"✈️ *#{trip['id']}* {date_range} (¥{budget} 频率{freq})\n"
                        f"  最低: ¥{best_total or '?'}{trend} | 差预算: {diff}\n"
                        f"  去: {ob_info}\n"
                        f"  回: {rt_info}\n"
                        f"  历史最低: ¥{new_best if new_best < 99999 else '?'}"
                    )
        except Exception as e:
            log.error(f"行程 #{trip['id']} 处理失败: {e}", exc_info=True)
            brief_lines.append(f"✈️ *#{trip['id']}* 处理失败: {e}")

    finalized_metrics = finalize_check_metrics(state, check_metrics, now_jst())
    log.info(
        "📈 check=%s trips=%s searches=%s real=%s cache=%s browser=%s blocked=%s valid=%s cooldown=%s duration_ms=%s",
        finalized_metrics.get("check_id"),
        finalized_metrics.get("due_trips"),
        finalized_metrics.get("searches"),
        finalized_metrics.get("real_requests"),
        finalized_metrics.get("cache_hits"),
        finalized_metrics.get("browser_fallbacks"),
        finalized_metrics.get("blocked_results"),
        finalized_metrics.get("valid_results"),
        finalized_metrics.get("cooldown_active_sources"),
        finalized_metrics.get("duration_ms"),
    )

    save_state(state)

    # 发送汇总简报
    s = load_state()
    if not s.get("pending_ack") and len(brief_lines) > 1:
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
    from app.version import VERSION
    log.info(f"✈️ 机票价格监控系统启动 {VERSION} (Docker)")
    log.info(f"   监控行程: {len(trips)} 个")
    log.info(f"   检查间隔: ~{CHECK_INTERVAL}s (随机抖动)")
    log.info(f"   TG通知: {'已配置' if TG_BOT_TOKEN else '⚠️ 未配置'}")
    log.info("=" * 55)

    # 设置 TG Bot 菜单命令
    setup_tg_commands()

    # 🟢 启动打招呼（健康检查）
    state = load_state()
    boot_count = state.get("boot_count", 0) + 1
    state["boot_count"] = boot_count
    save_state(state)
    tg_send(
        f"🟢 *机票监控系统 {VERSION} 已上线* (第{boot_count}次启动)\n\n"
        f"📊 监控行程: {len(get_active_trips())} 个\n"
        f"⏰ 约每 {CHECK_INTERVAL // 60} 分钟巡查（随机抖动防检测）\n\n"
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
        await push_until_ack(state["last_alert_msg"], state)

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
        done, pending = await asyncio.wait(
            [
                asyncio.create_task(shutdown_event.wait()),
                asyncio.create_task(force_check_event.wait()),
            ],
            timeout=wait_time,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if force_check_event.is_set():
            log.info("📢 收到 /check 命令，立即执行检查")
            is_force = True

    tg_listener_task.cancel()
    log.info("🛑 监控系统已停止")
