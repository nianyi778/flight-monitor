"""
数据库模块 - TiDB 连接 & CRUD
"""

from contextlib import contextmanager

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
                "outbound_depart_start, outbound_depart_end, return_arrive_start, return_arrive_end, "
                "outbound_flex, return_flex "
                "FROM trips WHERE status='active'"
            )
            rows = cur.fetchall()
        return [
            {"id": r[0], "outbound_date": str(r[1]), "return_date": str(r[2]),
             "budget": r[3], "best_price": r[4],
             "depart_after": r[5] or 19, "depart_before": r[6] or 23,
             "arrive_after": r[7] or 0, "arrive_before": r[8] or 6,
             "outbound_flex": r[9] or 0, "return_flex": r[10] if r[10] is not None else 1}
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

            # 写入每条航班记录
            flights_count = 0
            for direction in ["outbound", "return"]:
                flight_date = trip["outbound_date"] if direction == "outbound" else trip["return_date"]
                for src in results[direction]:
                    for f in src.get("flights", []):
                        cur.execute(
                            """INSERT INTO flight_prices
                            (trip_id, check_time, direction, source, airline, flight_no,
                             departure_time, arrival_time, origin, destination,
                             price_cny, original_price, original_currency, stops, flight_date)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (trip_id, now, direction, src.get("source", ""),
                             f.get("airline", ""), f.get("flight_no", ""),
                             f.get("departure_time", ""), f.get("arrival_time", ""),
                             f.get("origin", ""), f.get("destination", ""),
                             f.get("price_cny"), f.get("original_price"),
                             f.get("original_currency", "CNY"), f.get("stops", 0),
                             flight_date)
                        )
                        flights_count += 1

            # 写入巡查汇总
            best = combos[0] if combos else {}
            ob_lowest = min((s.get("lowest_price") or 99999 for s in results["outbound"]), default=None)
            rt_lowest = min((s.get("lowest_price") or 99999 for s in results["return"]), default=None)

            cur.execute(
                """INSERT INTO check_summary
                (trip_id, check_time, best_total, outbound_lowest, return_lowest,
                 best_outbound_airline, best_return_airline, flights_found)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (trip_id, now,
                 best.get("total"),
                 ob_lowest if ob_lowest != 99999 else None,
                 rt_lowest if rt_lowest != 99999 else None,
                 best.get("outbound", {}).get("airline", ""),
                 best.get("return", {}).get("airline", ""),
                 flights_count)
            )

            conn.commit()
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
    except:
        return False
