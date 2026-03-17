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

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3600"))
PUSH_INTERVAL = int(os.getenv("PUSH_INTERVAL", "60"))
ACK_KEYWORD = "确认收到"

# 航班参数（旧版单行程配置，用于日志/状态显示）
OUTBOUND_DATE = os.getenv("OUTBOUND_DATE", "")
RETURN_DATE = os.getenv("RETURN_DATE", "")
BUDGET_TOTAL = int(os.getenv("BUDGET_TOTAL", "1500"))
OUTBOUND_DEPART_AFTER = int(os.getenv("OUTBOUND_DEPART_AFTER", "19"))

# TiDB 数据库
DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = int(os.getenv("DB_PORT", "4000"))
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "test")

# 数据持久化目录（Docker volume 挂载点）
DATA_DIR = Path("/app/data")
SCREENSHOT_DIR = DATA_DIR / "screenshots"
PRICE_LOG = DATA_DIR / "price_log.jsonl"
STATE_FILE = DATA_DIR / "state.json"
BROWSER_PROFILE = DATA_DIR / "browser_profile"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(DATA_DIR / "monitor.log", encoding="utf-8"),
    ] if DATA_DIR.exists() else [logging.StreamHandler()]
)
log = logging.getLogger("flight_monitor")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 反检测 JS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEALTH_JS = """
// 隐藏 webdriver
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
delete navigator.__proto__.webdriver;

// 伪装 chrome 对象
window.chrome = {
    runtime: { onConnect: {}, onMessage: {}, sendMessage: function(){} },
    loadTimes: function(){}, csi: function(){},
    app: { isInstalled: false, InstallState: { DISABLED: 'disabled' } },
};

// 伪装 plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const p = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
            { name: 'Native Client', filename: 'internal-nacl-plugin' },
        ];
        p.length = 3;
        return p;
    }
});

// 伪装 languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['ja', 'zh-CN', 'zh', 'en-US', 'en']
});

// 伪装 platform & vendor
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

// 伪装 permissions API
const origQuery = window.navigator.permissions?.query;
if (origQuery) {
    window.navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(params);
}

// 防止 headless 检测
Object.defineProperty(document, 'hidden', { get: () => false });
Object.defineProperty(document, 'visibilityState', { get: () => 'visible' });

// 伪装 WebGL renderer
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Intel Inc.';
    if (param === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, param);
};

// 伪装 canvas fingerprint（加微小噪声）
const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type) {
    if (type === 'image/png' && this.width > 16 && this.height > 16) {
        const ctx = this.getContext('2d');
        if (ctx) {
            const style = ctx.fillStyle;
            ctx.fillStyle = 'rgba(0,0,1,0.01)';
            ctx.fillRect(0, 0, 1, 1);
            ctx.fillStyle = style;
        }
    }
    return origToDataURL.apply(this, arguments);
};
"""


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
