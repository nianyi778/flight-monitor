"""
告警模块 — 通知推送、确认等待、Spring URL 构造
从 scheduler.py 提取，降低 God Module 复杂度
"""

import asyncio

from app.config import PUSH_INTERVAL, now_jst, log, save_state
from app.notifier import tg_send


def make_spring_url(date_str: str, orig: str, dest: str) -> str:
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


async def push_until_ack(msg, state, shutdown_event):
    from app.bot import ack_received_event

    if ack_received_event.is_set():
        ack_received_event.clear()
        state["pending_ack"] = False
        save_state(state)
        log.info("✅ 确认已预先收到（进入 push_until_ack 前），停止推送")
        tg_send("✅ 已确认收到，停止推送。")
        return
    ack_received_event.clear()
    push_count = 1

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(ack_received_event.wait(), timeout=PUSH_INTERVAL)
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
