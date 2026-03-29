"""
MCP Server - 让其他 AI Agent 发现和操作机票监控系统

暴露工具（Tools）：管理行程、查价、健康检查
暴露资源（Resources）：行程列表、价格数据、系统状态
"""

from datetime import datetime, timedelta
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import now_jst, log, load_state
from app.db import get_db, get_active_trips
from app.source_runtime import (
    ensure_runtime_state,
    get_runtime_metrics,
    get_source_status_snapshot,
    proxy_pool_summary,
)

mcp = FastMCP(
    "Flight Monitor",
    instructions=(
        "东京⇄上海机票价格自动监控系统。"
        "数据源：携程（browser DOM抓取）、Google Flights（fast-flights protobuf）、春秋航空官网（直连API）。"
        "发现低价自动推送 Telegram 通知。支持多行程管理、弹性日期搜索、住宅代理。"
    ),
)


def _format_dt(value) -> str | None:
    if not value:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _normalize_days(days: int) -> int:
    try:
        days_value = int(days)
    except Exception:
        return 7
    return min(max(days_value, 1), 90)


def _validate_trip_fields(payload: dict, existing_trip: dict | None = None) -> dict | None:
    """校验 MCP 写入 trips 的字段。返回错误 dict，成功返回 None。"""
    trip_type = payload.get("trip_type", existing_trip.get("trip_type") if existing_trip else "round_trip") or "round_trip"
    if trip_type not in ("round_trip", "one_way"):
        return {"error": "trip_type 必须是 round_trip 或 one_way"}

    outbound_date = payload.get("outbound_date", existing_trip.get("outbound_date") if existing_trip else None)
    return_date = payload.get("return_date", existing_trip.get("return_date") if existing_trip else None)

    try:
        ob = datetime.strptime(outbound_date, "%Y-%m-%d").date()
    except Exception:
        return {"error": "去程日期格式错误，请用 YYYY-MM-DD"}

    if ob <= now_jst().date():
        return {"error": "去程日期已过期"}

    if trip_type == "round_trip":
        try:
            rt = datetime.strptime(return_date, "%Y-%m-%d").date()
        except Exception:
            return {"error": "往返行程需要回程日期，格式 YYYY-MM-DD"}
        if rt <= ob:
            return {"error": "回程日期必须晚于去程日期"}

    budget = payload.get("budget", existing_trip.get("budget") if existing_trip else 1500)
    try:
        budget_value = int(budget)
    except Exception:
        return {"error": "预算必须是整数"}
    if not (100 <= budget_value <= 50000):
        return {"error": "预算范围 100-50000"}

    hour_fields = {
        "depart_start": payload.get("depart_start", existing_trip.get("depart_start") if existing_trip else 19),
        "depart_end": payload.get("depart_end", existing_trip.get("depart_end") if existing_trip else 23),
    }
    # 单程时 arrive_start/end 用于去程落地时间过滤，往返时用于回程到达
    hour_fields["arrive_start"] = payload.get("arrive_start", existing_trip.get("arrive_start") if existing_trip else 0)
    hour_fields["arrive_end"] = payload.get("arrive_end", existing_trip.get("arrive_end") if existing_trip else 6)
    normalized_hours = {}
    for name, value in hour_fields.items():
        if value is None:
            continue
        try:
            normalized_hours[name] = int(value)
        except Exception:
            return {"error": f"{name} 必须是 0-23 的整数"}
        if not (0 <= normalized_hours[name] <= 23):
            return {"error": f"{name} 必须在 0-23"}
    if normalized_hours.get("depart_start", 0) > normalized_hours.get("depart_end", 23):
        return {"error": "去程时间窗口无效"}
    if "arrive_start" in normalized_hours and "arrive_end" in normalized_hours and normalized_hours["arrive_start"] > normalized_hours["arrive_end"]:
        label = "去程落地" if trip_type == "one_way" else "回程到达"
        return {"error": f"{label}时间窗口无效"}

    flex_fields = {
        "outbound_flex": payload.get("outbound_flex", existing_trip.get("outbound_flex") if existing_trip else 0),
    }
    if trip_type == "round_trip":
        flex_fields["return_flex"] = payload.get("return_flex", existing_trip.get("return_flex") if existing_trip else 1)
    for name, value in flex_fields.items():
        try:
            flex_value = int(value)
        except Exception:
            return {"error": f"{name} 必须是 0-7 的整数"}
        if not (0 <= flex_value <= 7):
            return {"error": f"{name} 必须在 0-7"}

    return None


def _get_trip_for_update(trip_id: int) -> dict | None:
    with get_db() as db:
        c = db.cursor()
        c.execute(
            "SELECT outbound_date, return_date, budget, "
            "outbound_depart_start, outbound_depart_end, return_arrive_start, return_arrive_end, "
            "outbound_flex, return_flex, status, trip_type "
            "FROM trips WHERE id=%s",
            (trip_id,),
        )
        row = c.fetchone()
    if not row:
        return None
    return {
        "outbound_date": str(row[0]),
        "return_date": str(row[1]) if row[1] else None,
        "budget": row[2],
        "depart_start": row[3],
        "depart_end": row[4],
        "arrive_start": row[5],
        "arrive_end": row[6],
        "outbound_flex": row[7] or 0,
        "return_flex": row[8] if row[8] is not None else 1,
        "status": row[9],
        "trip_type": row[10] or "round_trip",
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tools（A 机器人可以调用的操作）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.tool()
def list_trips() -> dict:
    """查看所有监控中的行程。返回行程列表，包含日期、预算、时间窗口、历史最低价等信息。"""
    trips = get_active_trips()
    return {
        "count": len(trips),
        "trips": [
            {
                "id": t["id"],
                "trip_type": t.get("trip_type", "round_trip"),
                "outbound_date": t["outbound_date"],
                "return_date": t.get("return_date"),
                "budget_cny": t["budget"],
                "best_price_cny": t.get("best_price"),
                "depart_window": f"{t['depart_after']}:00-{t['depart_before']}:00",
                "arrive_window": f"{t['arrive_after']}:00-{t['arrive_before']}:00" if t.get("trip_type") != "one_way" else None,
                "outbound_flex_days": t.get("outbound_flex", 0),
                "return_flex_days": t.get("return_flex", 1) if t.get("trip_type") != "one_way" else None,
            }
            for t in trips
        ],
    }


@mcp.tool()
def add_trip(
    outbound_date: str,
    return_date: str = None,
    budget: int = 1500,
    trip_type: str = "round_trip",
    depart_start: int = 19,
    depart_end: int = 23,
    arrive_start: int = 0,
    arrive_end: int = 6,
    outbound_flex: int = 0,
    return_flex: int = 1,
    direct_only: bool = False,
) -> dict:
    """
    添加新的机票监控行程。

    Args:
        outbound_date: 去程日期，格式 YYYY-MM-DD（东京出发）
        return_date: 回程日期，格式 YYYY-MM-DD（上海出发）；单程时可不填
        budget: 预算（人民币），往返总价或单程价，默认1500
        trip_type: 行程类型，round_trip（往返，默认）或 one_way（单程）
        depart_start: 去程最早出发时间（0-23），默认19
        depart_end: 去程最晚出发时间（0-23），默认23
        arrive_start: 往返回程最早到达时间（0-23），默认0；单程时为去程落地最早时间
        arrive_end: 往返回程最晚到达时间（0-23），默认6；单程时为去程落地最晚时间
        outbound_flex: 去程弹性天数（向前搜索），默认0
        return_flex: 回程弹性天数（向前搜索），默认1（单程时忽略）
        direct_only: 只看直飞（不含经停），默认False

    Returns:
        新行程的 ID 和详情
    """
    error = _validate_trip_fields({
        "outbound_date": outbound_date,
        "return_date": return_date,
        "budget": budget,
        "trip_type": trip_type,
        "depart_start": depart_start,
        "depart_end": depart_end,
        "arrive_start": arrive_start,
        "arrive_end": arrive_end,
        "outbound_flex": outbound_flex,
        "return_flex": return_flex,
    })
    if error:
        return error

    is_one_way = trip_type == "one_way"
    with get_db() as db:
        c = db.cursor()
        c.execute(
            "INSERT INTO trips (outbound_date, return_date, budget, trip_type, "
            "outbound_depart_start, outbound_depart_end, return_arrive_start, return_arrive_end, "
            "outbound_flex, return_flex, direct_only) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (outbound_date, None if is_one_way else return_date, budget, trip_type,
             depart_start, depart_end,
             arrive_start,
             arrive_end,
             outbound_flex,
             None if is_one_way else return_flex,
             1 if direct_only else 0)
        )
        db.commit()
        new_id = c.lastrowid

    type_label = "单程" if is_one_way else "往返"
    return {
        "success": True,
        "trip_id": new_id,
        "trip_type": trip_type,
        "outbound_date": outbound_date,
        "return_date": None if is_one_way else return_date,
        "budget_cny": budget,
        "message": f"{type_label}行程#{new_id}已添加，系统将在下次巡查时开始监控",
    }


@mcp.tool()
def edit_trip(
    trip_id: int,
    outbound_date: str = None,
    return_date: str = None,
    budget: int = None,
    trip_type: str = None,
    depart_start: int = None,
    depart_end: int = None,
    arrive_start: int = None,
    arrive_end: int = None,
    outbound_flex: int = None,
    return_flex: int = None,
    direct_only: bool = None,
) -> dict:
    """
    编辑已有行程。只需传入要修改的字段，其他保持不变。

    Args:
        trip_id: 行程编号
        outbound_date: 新去程日期 YYYY-MM-DD
        return_date: 新回程日期 YYYY-MM-DD（切换为单程时会被清空）
        budget: 新预算（人民币）
        trip_type: 行程类型 round_trip 或 one_way
        depart_start: 去程最早出发时间
        depart_end: 去程最晚出发时间
        arrive_start: 往返回程/单程去程的最早落地时间
        arrive_end: 往返回程/单程去程的最晚落地时间
        outbound_flex: 去程弹性天数
        return_flex: 回程弹性天数
        direct_only: True=只看直飞，False=不限（经停也纳入）
    """
    updates = {}
    if outbound_date is not None:
        updates["outbound_date"] = outbound_date
    if return_date is not None:
        updates["return_date"] = return_date
    if budget is not None:
        updates["budget"] = budget
    if trip_type is not None:
        updates["trip_type"] = trip_type
    if depart_start is not None:
        updates["outbound_depart_start"] = depart_start
    if depart_end is not None:
        updates["outbound_depart_end"] = depart_end
    if arrive_start is not None:
        updates["return_arrive_start"] = arrive_start
    if arrive_end is not None:
        updates["return_arrive_end"] = arrive_end
    if outbound_flex is not None:
        updates["outbound_flex"] = outbound_flex
    if return_flex is not None:
        updates["return_flex"] = return_flex
    if direct_only is not None:
        updates["direct_only"] = 1 if direct_only else 0

    if not updates:
        return {"error": "没有要修改的字段"}

    existing_trip = _get_trip_for_update(trip_id)
    if not existing_trip:
        return {"error": f"行程#{trip_id}不存在"}
    if existing_trip["status"] != "active":
        return {"error": f"行程#{trip_id}不存在或不是active状态"}

    # 仅在从 round_trip 切换为 one_way 时才清空回程相关字段
    # 已经是 one_way 的行程编辑其他字段时不重复写 NULL，避免 NOT NULL 约束问题
    effective_type = trip_type or existing_trip.get("trip_type", "round_trip")
    switching_to_one_way = (effective_type == "one_way" and existing_trip.get("trip_type") != "one_way")
    switching_to_round_trip = (effective_type == "round_trip" and existing_trip.get("trip_type") == "one_way")
    if switching_to_round_trip and not return_date:
        return {"error": "切换为往返行程时必须同时提供回程日期 (return_date)"}
    if switching_to_one_way:
        updates["return_date"] = None
        updates["return_arrive_start"] = None
        updates["return_arrive_end"] = None
        updates["return_flex"] = None

    # 只把实际传入的字段放进 payload，None 表示"未传入"（由 existing_trip 回填）
    # 注意：dict.get(key, default) 在 key 存在但值为 None 时返回 None，不返回 default
    validate_payload = {"trip_type": effective_type}
    if outbound_date is not None:
        validate_payload["outbound_date"] = outbound_date
    if return_date is not None:
        validate_payload["return_date"] = return_date
    if budget is not None:
        validate_payload["budget"] = budget
    if depart_start is not None:
        validate_payload["depart_start"] = depart_start
    if depart_end is not None:
        validate_payload["depart_end"] = depart_end
    if arrive_start is not None:
        validate_payload["arrive_start"] = arrive_start
    if arrive_end is not None:
        validate_payload["arrive_end"] = arrive_end
    if outbound_flex is not None:
        validate_payload["outbound_flex"] = outbound_flex
    if return_flex is not None:
        validate_payload["return_flex"] = return_flex

    error = _validate_trip_fields(validate_payload, existing_trip=existing_trip)
    if error:
        return error

    set_clause = ", ".join(f"{k}=%s" for k in updates)
    values = list(updates.values()) + [trip_id]

    with get_db() as db:
        c = db.cursor()
        c.execute(f"UPDATE trips SET {set_clause} WHERE id=%s AND status='active'", values)
        affected = c.rowcount
        db.commit()

    if affected:
        return {"success": True, "trip_id": trip_id, "updated_fields": list(updates.keys())}
    else:
        return {"error": f"行程#{trip_id}不存在或不是active状态"}


@mcp.tool()
def delete_trip(trip_id: int) -> dict:
    """删除（停止监控）一个行程。"""
    with get_db() as db:
        c = db.cursor()
        c.execute("UPDATE trips SET status='deleted' WHERE id=%s", (trip_id,))
        affected = c.rowcount
        db.commit()
    if affected:
        return {"success": True, "message": f"行程#{trip_id}已删除"}
    return {"error": f"行程#{trip_id}不存在"}


@mcp.tool()
def pause_trip(trip_id: int) -> dict:
    """暂停监控一个行程。"""
    with get_db() as db:
        c = db.cursor()
        c.execute("UPDATE trips SET status='paused' WHERE id=%s AND status='active'", (trip_id,))
        affected = c.rowcount
        db.commit()
    if affected:
        return {"success": True, "message": f"行程#{trip_id}已暂停"}
    return {"error": f"行程#{trip_id}不存在或不是active状态"}


@mcp.tool()
def resume_trip(trip_id: int) -> dict:
    """恢复监控一个已暂停的行程。"""
    with get_db() as db:
        c = db.cursor()
        c.execute("UPDATE trips SET status='active' WHERE id=%s AND status='paused'", (trip_id,))
        affected = c.rowcount
        db.commit()
    if affected:
        return {"success": True, "message": f"行程#{trip_id}已恢复"}
    return {"error": f"行程#{trip_id}不存在或不是paused状态"}


@mcp.tool()
def get_price_history(trip_id: int = None, limit: int = 20) -> dict:
    """
    查询价格历史趋势。

    Args:
        trip_id: 指定行程编号（可选，不传则查全部）
        limit: 返回最近N条记录，默认20
    """
    with get_db() as db:
        c = db.cursor()
        if trip_id:
            c.execute(
                "SELECT check_time, best_total, outbound_lowest, return_lowest, "
                "best_outbound_airline, best_return_airline "
                "FROM check_summary WHERE trip_id=%s ORDER BY check_time DESC LIMIT %s",
                (trip_id, limit)
            )
        else:
            c.execute(
                "SELECT check_time, best_total, outbound_lowest, return_lowest, "
                "best_outbound_airline, best_return_airline, trip_id "
                "FROM check_summary ORDER BY check_time DESC LIMIT %s",
                (limit,)
            )
        rows = c.fetchall()

    records = []
    for r in reversed(rows):
        rec = {
            "time": r[0].strftime("%Y-%m-%d %H:%M") if r[0] else None,
            "best_total_cny": r[1],
            "outbound_lowest_cny": r[2],
            "return_lowest_cny": r[3],
            "best_outbound_airline": r[4],
            "best_return_airline": r[5],
        }
        if not trip_id and len(r) > 6:
            rec["trip_id"] = r[6]
        records.append(rec)

    return {"count": len(records), "records": records}


@mcp.tool()
def get_cheapest_flights(trip_id: int = None, direction: str = "outbound", limit: int = 10) -> dict:
    """
    查询最便宜的航班记录。

    Args:
        trip_id: 行程编号（可选）
        direction: outbound（去程）或 return（回程）
        limit: 返回条数
    """
    where = "WHERE direction=%s"
    params = [direction]
    if trip_id:
        where += " AND trip_id=%s"
        params.append(trip_id)
    params.append(limit)

    with get_db() as db:
        c = db.cursor()
        c.execute(
            f"SELECT check_time, airline, flight_no, departure_time, arrival_time, "
            f"price_cny, original_price, original_currency, origin, destination, flight_date "
            f"FROM flight_prices {where} ORDER BY price_cny ASC LIMIT %s",
            params
        )
        rows = c.fetchall()

    return {
        "direction": direction,
        "count": len(rows),
        "flights": [
            {
                "check_time": r[0].strftime("%Y-%m-%d %H:%M") if r[0] else None,
                "airline": r[1],
                "flight_no": r[2],
                "departure": r[3],
                "arrival": r[4],
                "price_cny": r[5],
                "original_price": r[6],
                "original_currency": r[7],
                "origin": r[8],
                "destination": r[9],
                "flight_date": str(r[10]) if r[10] else None,
            }
            for r in rows
        ],
    }


@mcp.tool()
def health_check() -> dict:
    """检查系统各组件健康状态：数据库、代理、TG Bot。"""
    from app.config import PROXY_URL, TG_BOT_TOKEN
    import requests

    checks = {}

    # DB
    try:
        with get_db() as db:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM trips WHERE status='active'")
            count = c.fetchone()[0]
        checks["database"] = {"status": "ok", "active_trips": count}
    except Exception as e:
        checks["database"] = {"status": "error", "error": str(e)}

    # Proxy
    if PROXY_URL:
        try:
            r = requests.get("https://httpbin.org/ip",
                proxies={"https": PROXY_URL}, timeout=10)
            checks["proxy"] = {"status": "ok", "exit_ip": r.json().get("origin")}
        except Exception as e:
            checks["proxy"] = {"status": "error", "error": str(e)}
    else:
        checks["proxy"] = {"status": "not_configured"}

    # TG
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getMe", timeout=5)
        checks["telegram"] = {"status": "ok"}
    except Exception as e:
        checks["telegram"] = {"status": "error", "error": str(e)}

    # State
    state = load_state()
    ensure_runtime_state(state)
    checks["monitor"] = {
        "boot_count": state.get("boot_count", 0),
        "check_count": state.get("check_count", 0),
        "server_time_jst": now_jst().strftime("%Y-%m-%d %H:%M:%S"),
        "source_status": get_source_status_snapshot(state),
        "proxy_pool": proxy_pool_summary(state),
        "recent_alerts": state.get("runtime_alerts", [])[-5:],
    }

    return checks


@mcp.tool()
def get_system_info() -> dict:
    """获取系统配置和运行信息。"""
    from app.config import CHECK_INTERVAL, PROXY_URL

    state = load_state()
    ensure_runtime_state(state)
    trips = get_active_trips()

    return {
        "name": "Flight Monitor",
        "description": "东京⇄上海机票价格自动监控系统",
        "config": {
            "check_interval_seconds": CHECK_INTERVAL,
            "proxy": PROXY_URL or "not configured",
        },
        "status": {
            "boot_count": state.get("boot_count", 0),
            "check_count": state.get("check_count", 0),
            "active_trips": len(trips),
            "server_time_jst": now_jst().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "source_status": get_source_status_snapshot(state),
        "proxy_pool": proxy_pool_summary(state),
        "data_sources": [
            "携程 NRT⇄PVG (CNY)",
            "携程 HND⇄PVG (CNY)",
            "Google Flights JP NRT⇄PVG (JPY)",
        ],
        "covered_airlines": [
            "春秋航空(9C/IJ)", "捷星(GK)", "乐桃(MM)",
            "东航(MU)", "国航(CA)", "吉祥(HO)",
            "ANA(NH)", "JAL(JL)", "上航(FM)",
        ],
    }


@mcp.tool()
def get_runtime_metrics_snapshot(recent_limit: int = 10) -> dict:
    """
    查询运行时 metrics 快照。

    Args:
        recent_limit: 返回最近多少轮巡查指标，默认10，最大20
    """
    state = load_state()
    ensure_runtime_state(state)
    metrics = get_runtime_metrics(state)
    limit = min(max(int(recent_limit or 10), 1), 20)

    return {
        "generated_at": now_jst().strftime("%Y-%m-%d %H:%M:%S"),
        "totals": metrics.get("totals", {}),
        "last_check": metrics.get("last_check"),
        "recent_checks": metrics.get("recent_checks", [])[-limit:],
        "source_stats": metrics.get("source_stats", {}),
        "source_status": get_source_status_snapshot(state),
        "proxy_pool": proxy_pool_summary(state),
        "recent_alerts": state.get("runtime_alerts", [])[-5:],
    }


@mcp.tool()
def get_metrics_history(days: int = 7, trip_id: int = None) -> dict:
    """
    查询历史指标聚合，直接基于 TiDB 的 check_summary / flight_prices。

    Args:
        days: 查询最近 N 天，默认 7，范围 1-90
        trip_id: 可选，指定单个行程编号
    """
    days_value = _normalize_days(days)
    end_dt = now_jst()
    start_dt = end_dt - timedelta(days=days_value)

    trip_filter_summary = ""
    trip_filter_prices = ""
    summary_params = [start_dt]
    price_params = [start_dt]

    if trip_id is not None:
        trip_filter_summary = " AND trip_id=%s"
        trip_filter_prices = " AND trip_id=%s"
        summary_params.append(trip_id)
        price_params.append(trip_id)

    with get_db() as db:
        c = db.cursor()

        c.execute(
            "SELECT DATE(check_time) AS day, "
            "COUNT(*) AS checks, "
            "SUM(CASE WHEN best_total IS NOT NULL THEN 1 ELSE 0 END) AS checks_with_result, "
            "AVG(flights_found) AS avg_flights_found, "
            "MIN(best_total) AS min_best_total, "
            "AVG(best_total) AS avg_best_total "
            "FROM check_summary "
            "WHERE check_time >= %s"
            f"{trip_filter_summary} "
            "GROUP BY DATE(check_time) "
            "ORDER BY day DESC",
            tuple(summary_params),
        )
        daily_checks_rows = c.fetchall()

        c.execute(
            "SELECT DATE(check_time) AS day, source, "
            "COUNT(*) AS flight_rows, "
            "COUNT(DISTINCT trip_id) AS trips, "
            "MIN(price_cny) AS min_price, "
            "AVG(price_cny) AS avg_price "
            "FROM flight_prices "
            "WHERE check_time >= %s"
            f"{trip_filter_prices} "
            "GROUP BY DATE(check_time), source "
            "ORDER BY day DESC, source ASC",
            tuple(price_params),
        )
        source_rows = c.fetchall()

        c.execute(
            "SELECT DATE(check_time) AS day, direction, "
            "COUNT(*) AS flight_rows, "
            "COUNT(DISTINCT source) AS source_count, "
            "MIN(price_cny) AS min_price, "
            "AVG(price_cny) AS avg_price "
            "FROM flight_prices "
            "WHERE check_time >= %s"
            f"{trip_filter_prices} "
            "GROUP BY DATE(check_time), direction "
            "ORDER BY day DESC, direction ASC",
            tuple(price_params),
        )
        direction_rows = c.fetchall()

        c.execute(
            "SELECT COUNT(*) AS checks, "
            "SUM(CASE WHEN best_total IS NOT NULL THEN 1 ELSE 0 END) AS checks_with_result, "
            "AVG(flights_found) AS avg_flights_found, "
            "MIN(best_total) AS min_best_total, "
            "AVG(best_total) AS avg_best_total "
            "FROM check_summary "
            "WHERE check_time >= %s"
            f"{trip_filter_summary}",
            tuple(summary_params),
        )
        summary_totals = c.fetchone()

    daily_checks = []
    for row in daily_checks_rows:
        checks = int(row[1] or 0)
        checks_with_result = int(row[2] or 0)
        daily_checks.append({
            "day": str(row[0]),
            "checks": checks,
            "checks_with_result": checks_with_result,
            "result_rate": round(checks_with_result / checks, 4) if checks else 0,
            "avg_flights_found": float(row[3]) if row[3] is not None else None,
            "min_best_total": row[4],
            "avg_best_total": float(row[5]) if row[5] is not None else None,
        })

    source_coverage = [
        {
            "day": str(row[0]),
            "source": row[1],
            "flight_rows": int(row[2] or 0),
            "trip_count": int(row[3] or 0),
            "min_price": row[4],
            "avg_price": float(row[5]) if row[5] is not None else None,
        }
        for row in source_rows
    ]

    direction_coverage = [
        {
            "day": str(row[0]),
            "direction": row[1],
            "flight_rows": int(row[2] or 0),
            "source_count": int(row[3] or 0),
            "min_price": row[4],
            "avg_price": float(row[5]) if row[5] is not None else None,
        }
        for row in direction_rows
    ]

    total_checks = int((summary_totals[0] if summary_totals else 0) or 0)
    total_checks_with_result = int((summary_totals[1] if summary_totals else 0) or 0)

    return {
        "generated_at": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "range": {
            "days": days_value,
            "trip_id": trip_id,
            "start": _format_dt(start_dt),
            "end": _format_dt(end_dt),
        },
        "summary": {
            "checks": total_checks,
            "checks_with_result": total_checks_with_result,
            "result_rate": round(total_checks_with_result / total_checks, 4) if total_checks else 0,
            "avg_flights_found": float(summary_totals[2]) if summary_totals and summary_totals[2] is not None else None,
            "min_best_total": summary_totals[3] if summary_totals else None,
            "avg_best_total": float(summary_totals[4]) if summary_totals and summary_totals[4] is not None else None,
        },
        "daily_checks": daily_checks,
        "source_coverage": source_coverage,
        "direction_coverage": direction_coverage,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Resources（A 机器人可以读取的数据）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@mcp.resource("trips://active")
def resource_active_trips() -> str:
    """当前所有活跃监控行程的概览"""
    trips = get_active_trips()
    if not trips:
        return "当前没有活跃的监控行程"

    lines = []
    for t in trips:
        best = f"¥{t['best_price']:,}" if t.get("best_price") else "暂无数据"
        is_one_way = t.get("trip_type") == "one_way"
        type_tag = "[单程]" if is_one_way else ""
        date_str = t["outbound_date"] if is_one_way else f"{t['outbound_date']} → {t.get('return_date', '?')}"
        lines.append(
            f"行程#{t['id']}{type_tag}: {date_str} "
            f"预算¥{t['budget']:,} 最低{best}"
        )
    return "\n".join(lines)


@mcp.resource("system://status")
def resource_system_status() -> str:
    """系统运行状态摘要"""
    from app.config import CHECK_INTERVAL

    state = load_state()
    trips = get_active_trips()
    return (
        f"机票监控系统运行中\n"
        f"已巡查: {state.get('check_count', 0)}次\n"
        f"监控行程: {len(trips)}个\n"
        f"检查间隔: ~{CHECK_INTERVAL // 60}分钟\n"
        f"时间: {now_jst().strftime('%Y-%m-%d %H:%M')} JST"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HTTP /health 端点（供外部监控轮询）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@mcp.custom_route("/health", methods=["GET"])
async def http_health(request: Request) -> JSONResponse:
    """
    轻量健康检查，不调用 LLM/代理（避免每次轮询产生费用）。
    只检查 DB 连通性 + 系统状态。

    返回示例：
    {
      "status": "ok",          // ok / degraded
      "database": "ok",        // ok / error: <msg>
      "active_trips": 2,
      "check_count": 48,
      "server_time_jst": "2026-03-18 15:30:00"
    }
    """
    from app.config import CHECK_INTERVAL

    result: dict = {}

    # DB 检查
    try:
        with get_db() as db:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM trips WHERE status='active'")
            active_trips = c.fetchone()[0]
        result["database"] = "ok"
        result["active_trips"] = active_trips
    except Exception as e:
        result["database"] = f"error: {e}"
        result["active_trips"] = None

    # 系统状态
    state = load_state()
    ensure_runtime_state(state)
    result["check_count"] = state.get("check_count", 0)
    result["boot_count"] = state.get("boot_count", 0)
    result["server_time_jst"] = now_jst().strftime("%Y-%m-%d %H:%M:%S")
    result["check_interval_s"] = CHECK_INTERVAL
    result["source_status"] = get_source_status_snapshot(state)
    result["proxy_pool"] = proxy_pool_summary(state)
    result["recent_alerts"] = state.get("runtime_alerts", [])[-5:]

    # 整体状态
    result["status"] = "ok" if result["database"] == "ok" else "degraded"

    status_code = 200 if result["status"] == "ok" else 503
    return JSONResponse(result, status_code=status_code)
