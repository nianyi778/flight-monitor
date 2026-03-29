"""
数据库模块 - TiDB 连接 & CRUD
"""

from contextlib import contextmanager
from datetime import datetime, timedelta

from app.config import (
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME,
    now_jst, log,
)


@contextmanager
def get_db():
    """获取 TiDB 数据库连接（上下文管理器，异常时自动回滚并关闭）"""
    import pymysql
    conn = pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME,
        ssl={"ca": None}, ssl_verify_cert=False,
        ssl_verify_identity=False, charset="utf8mb4",
    )
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_active_trips():
    """从数据库读取所有 active 行程"""
    try:
        with get_db() as db:
            cur = db.cursor()
            cur.execute(
                "SELECT id, outbound_date, return_date, budget, best_price, "
                "ob_depart_start, ob_depart_end, ob_arrive_start, ob_arrive_end, "
                "rt_depart_start, rt_depart_end, rt_arrive_start, rt_arrive_end, "
                "ob_flex, rt_flex, trip_type, max_stops, throwaway, "
                "origin, destination "
                "FROM trips WHERE status='active'"
            )
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "outbound_date": str(r[1]),
                "return_date": str(r[2]) if r[2] else None,
                "budget": r[3],
                "best_price": r[4],
                # 去程时间窗（NULL = 不过滤该维度）
                "ob_depart_start": r[5],
                "ob_depart_end":   r[6],
                "ob_arrive_start": r[7],
                "ob_arrive_end":   r[8],
                # 回程时间窗（NULL = 不过滤）
                "rt_depart_start": r[9],
                "rt_depart_end":   r[10],
                "rt_arrive_start": r[11],
                "rt_arrive_end":   r[12],
                "outbound_flex":   r[13] if r[13] is not None else 0,
                "return_flex":     r[14],  # None for one_way
                "trip_type":       r[15] or "round_trip",
                "max_stops":       r[16],  # None=不限, 0=直飞, 1=最多1转
                "throwaway":       bool(r[17]),
                "origin":          r[18] or "TYO",
                "destination":     r[19] or "PVG",
            }
            for r in rows
        ]
    except Exception as e:
        log.error(f"读取行程失败: {e}")
        return []


def update_trip_best_price(trip_id, best_price):
    """更新行程历史最低价"""
    try:
        with get_db() as db:
            cur = db.cursor()
            cur.execute("UPDATE trips SET best_price = LEAST(COALESCE(best_price, 99999), %s) WHERE id = %s",
                        (best_price, trip_id))
            db.commit()
    except Exception as e:
        log.error(f"更新行程最低价失败: {e}")


def save_to_db(results, combos, trip):
    """将所有航班数据和巡查汇总写入 TiDB"""
    now = now_jst()
    trip_id = trip["id"]

    try:
        with get_db() as conn:
            cur = conn.cursor()

            flights_count = 0
            skipped_count = 0
            is_one_way = trip.get("trip_type") == "one_way"
            directions = ["outbound"] if is_one_way else ["outbound", "return"]
            for direction in directions:
                for src in results[direction]:
                    source_flight_date = src.get("flight_date") or (
                        trip["outbound_date"] if direction == "outbound" else trip.get("return_date")
                    )
                    for f in src.get("flights", []):
                        try:
                            cur.execute(
                                """INSERT INTO flight_prices
                                (trip_id, check_time, direction, source, airline, flight_no,
                                 departure_time, arrival_time, origin, destination,
                                 price_cny, original_price, original_currency, stops, flight_date, via)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                (trip_id, now, direction,
                                 src.get("source", "")[:30],
                                 f.get("airline", "")[:50], f.get("flight_no", "")[:20],
                                 f.get("departure_time", "")[:10], f.get("arrival_time", "")[:10],
                                 f.get("origin", "")[:5], f.get("destination", "")[:5],
                                 f.get("price_cny"), f.get("original_price"),
                                 f.get("original_currency", "CNY")[:5],
                                 f.get("stops", 0),
                                 source_flight_date,
                                 f.get("via", "")[:50] or None)
                            )
                            flights_count += 1
                        except Exception as row_err:
                            skipped_count += 1
                            log.warning(f"跳过异常行 ({src.get('source', '')}): {row_err}")

            best = combos[0] if combos else {}
            ob_lowest = min((s.get("lowest_price") or 99999 for s in results["outbound"]), default=None)
            if is_one_way:
                rt_lowest = None
                best_total = best.get("total") or (ob_lowest if ob_lowest and ob_lowest != 99999 else None)
                best_return_airline = ""
            else:
                rt_lowest = min((s.get("lowest_price") or 99999 for s in results["return"]), default=None)
                best_total = best.get("total")
                best_return_airline = (best.get("return") or {}).get("airline", "")[:50]

            cur.execute(
                """INSERT INTO check_summary
                (trip_id, check_time, best_total, outbound_lowest, return_lowest,
                 best_outbound_airline, best_return_airline, flights_found)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (trip_id, now,
                 best_total,
                 ob_lowest if ob_lowest and ob_lowest != 99999 else None,
                 rt_lowest if rt_lowest and rt_lowest != 99999 else None,
                 (best.get("outbound") or {}).get("airline", "")[:50],
                 best_return_airline,
                 flights_count)
            )

            conn.commit()
            if skipped_count:
                log.warning(f"💾 已入库: {flights_count} 条航班 + 1 条汇总 (跳过 {skipped_count} 条异常行)")
            else:
                log.info(f"💾 已入库: {flights_count} 条航班 + 1 条汇总")

    except Exception as e:
        log.error(f"数据库写入失败: {e}")


def already_checked_this_hour():
    """检查本小时是否已有查询记录"""
    try:
        with get_db() as db:
            cur = db.cursor()
            hour_start = now_jst().replace(minute=0, second=0, microsecond=0)
            cur.execute("SELECT COUNT(*) FROM check_summary WHERE check_time >= %s", (hour_start,))
            count = cur.fetchone()[0]
        return count > 0
    except Exception:
        return False


# ─── Pending Trip（Bot 确认流程用）────────────────────────────────────────────

def create_pending_trip(fields: dict) -> int:
    """
    插入一条 status='pending' 的行程，返回 id。
    expires_at = 1小时后，由定期清理任务删除未确认的行程。
    """
    expires_at = datetime.utcnow() + timedelta(hours=1)
    with get_db() as db:
        cur = db.cursor()
        cur.execute(
            """INSERT INTO trips
            (origin, destination, outbound_date, return_date, budget, trip_type, status,
             ob_depart_start, ob_depart_end, ob_arrive_start, ob_arrive_end,
             rt_depart_start, rt_depart_end, rt_arrive_start, rt_arrive_end,
             ob_flex, rt_flex, max_stops, throwaway, expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,'pending',
                    %s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s)""",
            (
                fields.get("origin", "TYO"),
                fields.get("destination", "PVG"),
                fields["outbound_date"],
                fields.get("return_date"),
                fields.get("budget", 1500),
                fields.get("trip_type", "round_trip"),
                fields.get("ob_depart_start"), fields.get("ob_depart_end"),
                fields.get("ob_arrive_start"), fields.get("ob_arrive_end"),
                fields.get("rt_depart_start"), fields.get("rt_depart_end"),
                fields.get("rt_arrive_start"), fields.get("rt_arrive_end"),
                fields.get("outbound_flex", 0),
                fields.get("return_flex"),
                fields.get("max_stops"),
                1 if fields.get("throwaway") else 0,
                expires_at,
            )
        )
        db.commit()
        return cur.lastrowid


def activate_pending_trip(pending_id: int) -> bool:
    """将 pending 行程激活为 active，返回是否成功。"""
    with get_db() as db:
        cur = db.cursor()
        cur.execute(
            "UPDATE trips SET status='active', expires_at=NULL WHERE id=%s AND status='pending'",
            (pending_id,)
        )
        affected = cur.rowcount
        db.commit()
    return affected > 0


def cleanup_expired_pending_trips():
    """删除超时未确认的 pending 行程（调度器定期调用）。"""
    try:
        with get_db() as db:
            cur = db.cursor()
            cur.execute(
                "DELETE FROM trips WHERE status='pending' AND expires_at < UTC_TIMESTAMP()"
            )
            deleted = cur.rowcount
            db.commit()
        if deleted:
            log.info(f"🗑 清理过期 pending 行程: {deleted} 条")
    except Exception as e:
        log.warning(f"清理 pending 行程失败: {e}")
