"""
✈️ 机票价格自动监控系统 - Docker 版

数据源：携程(NRT/HND) + Google Flights JP
分析：GPT-4o 视觉分析截图
通知：Telegram 推送
交互：TG Bot 命令 + MCP Server（供其他 AI Agent 调用）
"""

import asyncio
import os
import threading


def run_mcp_server():
    """在独立线程中运行 MCP SSE Server"""
    from app.mcp_server import mcp
    port = int(os.getenv("MCP_PORT") or "8080")
    mcp.run(transport="sse", host="0.0.0.0", port=port)


async def run_main():
    """运行主监控循环"""
    from app.scheduler import main
    await main()


if __name__ == "__main__":
    # MCP Server 在独立线程（非阻塞）
    mcp_enabled = os.getenv("MCP_ENABLED", "true").lower() == "true"
    if mcp_enabled:
        mcp_thread = threading.Thread(target=run_mcp_server, daemon=True)
        mcp_thread.start()
        print(f"🔌 MCP Server started on port {os.getenv('MCP_PORT', '8080')}")

    # 主循环（TG Bot + 价格监控）
    asyncio.run(run_main())
