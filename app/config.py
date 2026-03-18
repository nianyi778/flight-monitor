"""
配置模块 - 环境变量 & 常量
"""

import logging
import os
from datetime import timezone, timedelta, datetime
from pathlib import Path

# 东京时间 UTC+9
JST = timezone(timedelta(hours=9))


def now_jst():
    return datetime.now(JST)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 配置（全部从环境变量读取）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
# 允许响应的 chat ID 列表（私聊 + 群聊）
TG_ALLOWED_CHATS = set(filter(None, [
    TG_CHAT_ID,
    *os.getenv("TG_GROUP_IDS", "").split(","),
]))

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL") or "3600")
PUSH_INTERVAL = int(os.getenv("PUSH_INTERVAL") or "60")
ACK_KEYWORD = "确认收到"

# 住宅代理（可选，格式: socks5://user:pass@host:port 或 http://host:port）
PROXY_URL = os.getenv("PROXY_URL", "")

# TiDB 数据库
DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = int(os.getenv("DB_PORT") or "4000")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME") or "test"

# 数据持久化目录（Docker volume 挂载点）
DATA_DIR = Path("/app/data")
SCREENSHOT_DIR = DATA_DIR / "screenshots"
PRICE_LOG = DATA_DIR / "price_log.jsonl"
STATE_FILE = DATA_DIR / "state.json"
BROWSER_PROFILE = DATA_DIR / "browser_profile"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _JSTFormatter(logging.Formatter):
    """让 %(asctime)s 显示 JST 而非容器本地时间（通常是 UTC）"""
    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, JST)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


_fmt = _JSTFormatter("%(asctime)s [%(levelname)s] %(message)s")
_handlers = [logging.StreamHandler()]
if DATA_DIR.exists():
    _fh = logging.FileHandler(DATA_DIR / "monitor.log", encoding="utf-8")
    _fh.setFormatter(_fmt)
    _handlers.append(_fh)
_handlers[0].setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=_handlers)
log = logging.getLogger("flight_monitor")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 状态持久化
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_state():
    if STATE_FILE.exists():
        import json
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state):
    import json
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
