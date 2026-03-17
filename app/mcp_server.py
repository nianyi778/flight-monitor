"""
MCP Server - 让其他 AI Agent 发现和操作机票监控系统

暴露工具（Tools）：管理行程、查价、健康检查
暴露资源（Resources）：行程列表、价格数据、系统状态
"""

from datetime import datetime
from fastmcp import FastMCP

from app.config import now_jst, log, load_state
from app.db import get_db, get_active_trips

mcp = FastMCP(
    "Flight Monitor",
    description=(
        "东京⇄上海机票价格自动监控系统。"
        "监控多个行程的机票价格，自动抓取携程和Google Flights数据，"
        "通过GPT-4o视觉分析提取价格，发现低价自动通知。"
        "支持多行程管理、弹性日期搜索、住宅代理防检测。"
    ),
)


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
                "outbound_date": t["outbound_date"],
                "return_date": t["return_date"],
                "budget_cny": t["budget"],
                "best_price_cny": t.get("best_price"),
                "depart_window": f"{t['depart_after']}:00-{t['depart_before']}:00",
                "arrive_window": f"{t['arrive_after']}:00-{t['arrive_before']}:00",
                "outbound_flex_days": t.get("outbound_flex", 0),
                "return_flex_days": t.get("return_flex", 1),
            }
            for t in trips
        ],
    }


@mcp.tool()
def add_trip(
    outbound_date: str,
    return_date: str,
    budget: int = 1500,
    depart_start: int = 19,
    depart_end: int = 23,
    arrive_start: int = 0,
    arrive_end: int = 6,
    outbound_flex: int = 0,
    return_flex: int = 1,
) -> dict:
    """
    添加新的机票监控行程。

    Args:
        outbound_date: 去程日期，格式 YYYY-MM-DD（东京出发）
        return_date: 回程日期，格式 YYYY-MM-DD（上海出发）
        budget: 往返预算（人民币），默认1500
        depart_start: 去程最早出发时间（0-23），默认19
        depart_end: 去程最晚出发时间（0-23），默认23
        arrive_start: 回程最早到达时间（0-23），默认0
        arrive_end: 回程最晚到达时间（0-23），默认6
        outbound_flex: 去程弹性天数（向前搜索），默认0
        return_flex: 回程弹性天数（向前搜索），默认1

    Returns:
        新行程的 ID 和详情
    """
    # 校验
    try:
        ob = datetime.strptime(outbound_date, "%Y-%m-%d").date()
        rt = datetime.strptime(return_date, "%Y-%m-%d").date()
    except ValueError:
        return {"error": "日期格式错误，请用 YYYY-MM-DD"}

    if rt <= ob:
        return {"error": "回程日期必须晚于去程日期"}
    if ob <= now_jst().date():
        return {"error": "去程日期已过期"}
    if not (100 <= budget <= 50000):
        return {"error": "预算范围 100-50000"}

    db = get_db()
    c = db.cursor()
    c.execute(
        "INSERT INTO trips (outbound_date, return_date, budget, "
        "outbound_depart_start, outbound_depart_end, return_arrive_start, return_arrive_end, "
        "outbound_flex, return_flex) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (outbound_date, return_date, budget,
         depart_start, depart_end, arrive_start, arrive_end,
         outbound_flex, return_flex)
    )
    db.commit()
    new_id = c.lastrowid
    db.close()

    return {
        "success": True,
        "trip_id": new_id,
        "outbound_date": outbound_date,
        "return_date": return_date,
        "budget_cny": budget,
        "message": f"行程#{new_id}已添加，系统将在下次巡查时开始监控",
    }


@mcp.tool()
def edit_trip(
    trip_id: int,
    outbound_date: str = None,
    return_date: str = None,
    budget: int = None,
    depart_start: int = None,
    depart_end: int = None,
    arrive_start: int = None,
    arrive_end: int = None,
    outbound_flex: int = None,
    return_flex: int = None,
) -> dict:
    """
    编辑已有行程。只需传入要修改的字段，其他保持不变。

    Args:
        trip_id: 行程编号
        outbound_date: 新去程日期 YYYY-MM-DD
        return_date: 新回程日期 YYYY-MM-DD
        budget: 新预算（人民币）
        depart_start: 去程最早出发时间
        depart_end: 去程最晚出发时间
        arrive_start: 回程最早到达时间
        arrive_end: 回程最晚到达时间
        outbound_flex: 去程弹性天数
        return_flex: 回程弹性天数
    """
    updates = {}
    if outbound_date is not None:
        updates["outbound_date"] = outbound_date
    if return_date is not None:
        updates["return_date"] = return_date
    if budget is not None:
        updates["budget"] = budget
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

    if not updates:
        return {"error": "没有要修改的字段"}

    set_clause = ", ".join(f"{k}=%s" for k in updates)
    values = list(updates.values()) + [trip_id]

    db = get_db()
    c = db.cursor()
    c.execute(f"UPDATE trips SET {set_clause} WHERE id=%s AND status='active'", values)
    affected = c.rowcount
    db.commit()
    db.close()

    if affected:
        return {"success": True, "trip_id": trip_id, "updated_fields": list(updates.keys())}
    else:
        return {"error": f"行程#{trip_id}不存在或不是active状态"}


@mcp.tool()
def delete_trip(trip_id: int) -> dict:
    """删除（停止监控）一个行程。"""
    db = get_db()
    c = db.cursor()
    c.execute("UPDATE trips SET status='deleted' WHERE id=%s", (trip_id,))
    affected = c.rowcount
    db.commit()
    db.close()
    if affected:
        return {"success": True, "message": f"行程#{trip_id}已删除"}
    return {"error": f"行程#{trip_id}不存在"}


@mcp.tool()
def pause_trip(trip_id: int) -> dict:
    """暂停监控一个行程。"""
    db = get_db()
    c = db.cursor()
    c.execute("UPDATE trips SET status='paused' WHERE id=%s AND status='active'", (trip_id,))
    affected = c.rowcount
    db.commit()
    db.close()
    if affected:
        return {"success": True, "message": f"行程#{trip_id}已暂停"}
    return {"error": f"行程#{trip_id}不存在或不是active状态"}


@mcp.tool()
def resume_trip(trip_id: int) -> dict:
    """恢复监控一个已暂停的行程。"""
    db = get_db()
    c = db.cursor()
    c.execute("UPDATE trips SET status='active' WHERE id=%s AND status='paused'", (trip_id,))
    affected = c.rowcount
    db.commit()
    db.close()
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
    db = get_db()
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
    db.close()

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
    db = get_db()
    c = db.cursor()
    where = "WHERE direction=%s"
    params = [direction]
    if trip_id:
        where += " AND trip_id=%s"
        params.append(trip_id)
    params.append(limit)

    c.execute(
        f"SELECT check_time, airline, flight_no, departure_time, arrival_time, "
        f"price_cny, original_price, original_currency, origin, destination, flight_date "
        f"FROM flight_prices {where} ORDER BY price_cny ASC LIMIT %s",
        params
    )
    rows = c.fetchall()
    db.close()

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
    """检查系统各组件健康状态：LLM连通性、数据库、代理、TG Bot。"""
    from app.config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, PROXY_URL, TG_BOT_TOKEN
    import requests

    checks = {}

    # LLM
    try:
        r = requests.post(f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={"model": LLM_MODEL, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5},
            timeout=10)
        checks["llm"] = {"status": "ok", "model": LLM_MODEL}
    except Exception as e:
        checks["llm"] = {"status": "error", "error": str(e)}

    # DB
    try:
        db = get_db()
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM trips WHERE status='active'")
        count = c.fetchone()[0]
        db.close()
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
    checks["monitor"] = {
        "boot_count": state.get("boot_count", 0),
        "check_count": state.get("check_count", 0),
        "server_time_jst": now_jst().strftime("%Y-%m-%d %H:%M:%S"),
    }

    return checks


@mcp.tool()
def get_system_info() -> dict:
    """获取系统配置和运行信息。"""
    from app.config import LLM_MODEL, LLM_BASE_URL, CHECK_INTERVAL, PROXY_URL

    state = load_state()
    trips = get_active_trips()

    return {
        "name": "Flight Monitor",
        "description": "东京⇄上海机票价格自动监控系统",
        "config": {
            "llm_model": LLM_MODEL,
            "llm_api": LLM_BASE_URL.split("//")[1] if "//" in LLM_BASE_URL else LLM_BASE_URL,
            "check_interval_seconds": CHECK_INTERVAL,
            "proxy": PROXY_URL or "not configured",
        },
        "status": {
            "boot_count": state.get("boot_count", 0),
            "check_count": state.get("check_count", 0),
            "active_trips": len(trips),
            "server_time_jst": now_jst().strftime("%Y-%m-%d %H:%M:%S"),
        },
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
        lines.append(
            f"行程#{t['id']}: {t['outbound_date']} → {t['return_date']} "
            f"预算¥{t['budget']:,} 最低{best}"
        )
    return "\n".join(lines)


@mcp.resource("system://status")
def resource_system_status() -> str:
    """系统运行状态摘要"""
    from app.config import LLM_MODEL, CHECK_INTERVAL

    state = load_state()
    trips = get_active_trips()
    return (
        f"机票监控系统运行中\n"
        f"模型: {LLM_MODEL}\n"
        f"已巡查: {state.get('check_count', 0)}次\n"
        f"监控行程: {len(trips)}个\n"
        f"检查间隔: ~{CHECK_INTERVAL // 60}分钟\n"
        f"时间: {now_jst().strftime('%Y-%m-%d %H:%M')} JST"
    )
