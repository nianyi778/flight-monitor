"""
Telegram Bot 命令监听模块
"""

import asyncio

import requests

from app.config import (
    TG_BOT_TOKEN, TG_CHAT_ID, ACK_KEYWORD, CHECK_INTERVAL,
    log, load_state, save_state,
)
from app.db import get_db, get_active_trips
from app.notifier import tg_send


# 用于 /check 命令触发立即检查
force_check_event = asyncio.Event()


def setup_tg_commands():
    """注册 TG Bot 菜单命令"""
    if not TG_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setMyCommands",
            json={"commands": [
                {"command": "check", "description": "立即查价"},
                {"command": "status", "description": "系统状态"},
                {"command": "history", "description": "价格趋势"},
                {"command": "trips", "description": "查看所有行程"},
            ]},
            timeout=10,
        )
        log.info("TG 菜单命令已注册")
    except Exception as e:
        log.error(f"TG 菜单注册失败: {e}")


async def tg_command_listener():
    """后台监听 TG 命令"""
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
                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id != str(TG_CHAT_ID):
                    continue

                if text == "/check":
                    tg_send("🔍 收到！正在立即查价...")
                    force_check_event.set()

                elif text == "/status":
                    s = load_state()
                    uptime_checks = s.get("check_count", 0)
                    best = s.get("best_price", "?")
                    boot = s.get("boot_count", 0)
                    trips = get_active_trips()
                    tg_send(
                        f"📊 *系统状态*\n\n"
                        f"启动次数: {boot}\n"
                        f"已巡查: {uptime_checks} 次\n"
                        f"监控行程: {len(trips)} 个\n"
                        f"检查间隔: ~{CHECK_INTERVAL//60} 分钟"
                    )

                elif text == "/history":
                    try:
                        db = get_db()
                        c = db.cursor()
                        c.execute(
                            "SELECT check_time, best_total, outbound_lowest, return_lowest "
                            "FROM check_summary ORDER BY check_time DESC LIMIT 10"
                        )
                        rows = c.fetchall()
                        db.close()
                        if rows:
                            lines_h = []
                            for r in reversed(rows):
                                ts = r[0].strftime("%m-%d %H:%M")
                                total = r[1] or "?"
                                ob = r[2] or "?"
                                rt = r[3] or "?"
                                lines_h.append(f"  {ts} | 往返¥{total} (去¥{ob}+回¥{rt})")
                            tg_send(f"📈 *价格趋势* (最近{len(lines_h)}次)\n\n" + "\n".join(lines_h))
                        else:
                            tg_send("📈 暂无历史数据")
                    except Exception as e:
                        tg_send(f"📈 查询失败: {e}")

                elif text == "/budget" or text == "/trip list" or text == "/trips":
                    trips = get_active_trips()
                    if trips:
                        lines_t = ["📋 *监控中的行程*\n"]
                        for t in trips:
                            lines_t.append(
                                f"*#{t['id']}* {t['outbound_date']} → {t['return_date']}\n"
                                f"  预算: ¥{t['budget']}(CNY) | 去程: {t['depart_after']}:00-{t['depart_before']}:00\n"
                                f"  回程到达: {t['arrive_after']}:00-{t['arrive_before']}:00\n"
                                f"  历史最低: ¥{t['best_price'] or '暂无'}"
                            )
                        lines_t.append(f"\n💡 /trip add 去程 回程 预算")
                        lines_t.append(f"💡 /trip del 编号")
                        tg_send("\n".join(lines_t))
                    else:
                        tg_send("📋 暂无监控行程\n\n用 /trip add 2026-09-18 2026-09-27 1500 添加")

                elif text.startswith("/trip add"):
                    # /trip add 2026-09-18 2026-09-27 1500
                    parts = text.split()
                    if len(parts) >= 4:
                        try:
                            ob_d = parts[2]
                            rt_d = parts[3]
                            bgt = int(parts[4]) if len(parts) > 4 else 1500
                            db = get_db()
                            c = db.cursor()
                            c.execute(
                                "INSERT INTO trips (outbound_date, return_date, budget) VALUES (%s, %s, %s)",
                                (ob_d, rt_d, bgt)
                            )
                            db.commit()
                            new_id = c.lastrowid
                            db.close()
                            tg_send(f"✅ 行程#{new_id} 已添加\n{ob_d} → {rt_d} 预算¥{bgt}(CNY)")
                        except Exception as e:
                            tg_send(f"❌ 添加失败: {e}")
                    else:
                        tg_send("格式: /trip add 去程日期 回程日期 预算\n例: /trip add 2026-12-28 2027-01-05 2000")

                elif text.startswith("/trip del"):
                    parts = text.split()
                    if len(parts) >= 3:
                        try:
                            tid = int(parts[2])
                            db = get_db()
                            c = db.cursor()
                            c.execute("UPDATE trips SET status='deleted' WHERE id=%s", (tid,))
                            db.commit()
                            db.close()
                            tg_send(f"🗑 行程#{tid} 已删除")
                        except Exception as e:
                            tg_send(f"❌ 删除失败: {e}")

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
                            tg_send(f"⏸ 行程#{tid} 已暂停")
                        except Exception as e:
                            tg_send(f"❌ 暂停失败: {e}")

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
                            tg_send(f"▶️ 行程#{tid} 已恢复")
                        except Exception as e:
                            tg_send(f"❌ 恢复失败: {e}")

                elif ACK_KEYWORD in text:
                    pass  # tg_check_ack 会处理

            # 保存 offset
            state = load_state()
            state["last_tg_update_id"] = last_update_id
            save_state(state)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"TG 命令监听异常: {e}")
            await asyncio.sleep(10)

        await asyncio.sleep(1)
