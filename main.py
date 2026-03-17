"""
✈️ 机票价格自动监控系统 - Docker 版
东京⇄上海 往返机票监控

数据源：携程(NRT/HND) + Google Flights(CN/JP)
分析：GPT-4o 视觉分析截图
通知：Telegram 推送（低于预算持续推送直到确认）
反检测：Playwright stealth + 持久化指纹 + 随机行为模拟
"""

import asyncio
from app.scheduler import main

if __name__ == "__main__":
    asyncio.run(main())
