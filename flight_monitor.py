"""
✈️ 机票价格自动监控系统 - Docker 版
东京⇄上海 往返机票监控

数据源：携程(NRT/HND) + Google Flights(CN/JP)
分析：GPT-4o 视觉分析截图
通知：Telegram 推送（低于预算持续推送直到确认）
反检测：Playwright stealth + 持久化指纹 + 随机行为模拟
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import signal
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.async_api import async_playwright

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

# 优雅关闭
shutdown_event = asyncio.Event()

def handle_signal(sig, frame):
    log.info(f"收到信号 {sig}，准备优雅关闭...")
    shutdown_event.set()


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
# 搜索 URL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_active_trips():
    """从数据库读取所有 active 行程"""
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "SELECT id, outbound_date, return_date, budget, best_price, "
            "outbound_depart_start, outbound_depart_end, return_arrive_start, return_arrive_end "
            "FROM trips WHERE status='active'"
        )
        rows = cur.fetchall()
        db.close()
        return [
            {"id": r[0], "outbound_date": str(r[1]), "return_date": str(r[2]),
             "budget": r[3], "best_price": r[4],
             "depart_after": r[5] or 19, "depart_before": r[6] or 23,
             "arrive_after": r[7] or 0, "arrive_before": r[8] or 6}
            for r in rows
        ]
    except Exception as e:
        log.error(f"读取行程失败: {e}")
        return []


def update_trip_best_price(trip_id, best_price):
    """更新行程历史最低价"""
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("UPDATE trips SET best_price = LEAST(COALESCE(best_price, 99999), %s) WHERE id = %s",
                    (best_price, trip_id))
        db.commit()
        db.close()
    except Exception as e:
        log.error(f"更新行程最低价失败: {e}")


def get_search_urls(trip):
    """根据行程生成搜索 URL"""
    OB = trip["outbound_date"]
    RT = trip["return_date"]

    return [
        # ━━━ 携程（最稳定，CNY）━━━
        {"name": "携程", "direction": "outbound", "label": "去程 NRT→PVG",
         "url": f"https://flights.ctrip.com/online/list/oneway-NRT-PVG?depdate={OB}&cabin=y&adult=1&child=0&infant=0",
         "wait": 8},
        {"name": "携程", "direction": "return", "label": "回程 PVG→NRT",
         "url": f"https://flights.ctrip.com/online/list/oneway-PVG-NRT?depdate={RT}&cabin=y&adult=1&child=0&infant=0",
         "wait": 8},
        # 携程 HND（乐桃、ANA红眼）
        {"name": "携程_HND", "direction": "outbound", "label": "去程 HND→PVG",
         "url": f"https://flights.ctrip.com/online/list/oneway-HND-PVG?depdate={OB}&cabin=y&adult=1&child=0&infant=0",
         "wait": 8},
        {"name": "携程_HND", "direction": "return", "label": "回程 PVG→HND",
         "url": f"https://flights.ctrip.com/online/list/oneway-PVG-HND?depdate={RT}&cabin=y&adult=1&child=0&infant=0",
         "wait": 8},
        # ━━━ Google Flights 中国站（CNY）━━━
        {"name": "Google_CN", "direction": "outbound", "label": "去程 NRT→PVG",
         "url": f"https://www.google.com/travel/flights?q=Flights+from+NRT+to+PVG+on+{OB}+one+way&curr=CNY&hl=zh-CN",
         "wait": 10},
        {"name": "Google_CN", "direction": "return", "label": "回程 PVG→NRT",
         "url": f"https://www.google.com/travel/flights?q=Flights+from+PVG+to+NRT+on+{RT}+one+way&curr=CNY&hl=zh-CN",
         "wait": 10},
        # ━━━ Google Flights 日本站（JPY，反杀熟）━━━
        {"name": "Google_JP", "direction": "outbound", "label": "去程 NRT→PVG",
         "url": f"https://www.google.co.jp/travel/flights?q=Flights+from+NRT+to+PVG+on+{OB}+one+way&curr=JPY&hl=ja",
         "wait": 10},
        {"name": "Google_JP", "direction": "return", "label": "回程 PVG→NRT",
         "url": f"https://www.google.co.jp/travel/flights?q=Flights+from+PVG+to+NRT+on+{RT}+one+way&curr=JPY&hl=ja",
         "wait": 10},
    ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 截图抓取
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def capture_screenshots(trip):
    """用 Playwright + 持久化指纹抓取所有平台截图"""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")

    screenshots = []

    async with async_playwright() as p:
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE),
                headless=True,
                viewport={"width": 1366, "height": 900},
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-site-isolation-trials",
                    "--lang=ja,zh-CN,zh,en",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
                ignore_default_args=["--enable-automation"],
            )

            await context.add_init_script(STEALTH_JS)

            for idx, search in enumerate(get_search_urls(trip)):
                if shutdown_event.is_set():
                    break

                # 页面间随机延迟
                if idx > 0:
                    await asyncio.sleep(random.uniform(2, 5))

                page = await context.new_page()
                ss_name = f"{timestamp}_{search['name']}_{search['direction']}.png"
                ss_path = SCREENSHOT_DIR / ss_name

                try:
                    log.info(f"抓取: {search['label']} ({search['name']})")
                    await page.goto(search["url"], wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(search["wait"] + random.uniform(1, 3))

                    # 模拟真人滚动
                    await page.evaluate("window.scrollBy(0, Math.floor(Math.random() * 300 + 100))")
                    await asyncio.sleep(random.uniform(0.5, 1.5))

                    # 关闭弹窗
                    for sel in ["button:has-text('Reject')", "button:has-text('Accept')",
                                "button:has-text('同意')", "button:has-text('知道了')"]:
                        try:
                            btn = page.locator(sel).first
                            if await btn.is_visible(timeout=500):
                                await btn.click()
                                await asyncio.sleep(0.5)
                        except:
                            pass

                    await asyncio.sleep(1)
                    await page.screenshot(path=str(ss_path), full_page=False)
                    screenshots.append({
                        "path": str(ss_path),
                        "name": search["name"],
                        "direction": search["direction"],
                        "label": search["label"],
                        "url": search["url"],
                    })
                    log.info(f"  ✓ {ss_path.name}")
                except Exception as e:
                    log.error(f"  ✗ {search['name']} {search['direction']}: {e}")
                finally:
                    await page.close()

            await context.close()
        except Exception as e:
            log.error(f"浏览器启动失败: {e}")

    return screenshots


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLM 视觉分析
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def image_to_base64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def analyze_screenshot(screenshot_info):
    """用 GPT-4o 分析截图，提取结构化航班数据"""
    img_b64 = image_to_base64(screenshot_info["path"])
    direction = "去程（东京→上海）" if screenshot_info["direction"] == "outbound" else "回程（上海→东京）"

    prompt = f"""分析这张机票搜索截图，提取所有航班信息。

搜索条件：{direction}
平台：{screenshot_info['name']}

请严格按以下 JSON 格式返回（不要返回其他内容）：
{{
  "flights": [
    {{
      "airline": "航空公司名",
      "flight_no": "航班号（如有）",
      "departure_time": "HH:MM",
      "arrival_time": "HH:MM",
      "duration": "Xh Xm",
      "stops": 0,
      "price_cny": 1234,
      "original_price": 20000,
      "original_currency": "JPY",
      "origin": "NRT/HND/PVG/SHA",
      "destination": "PVG/SHA/NRT/HND",
      "booking_note": "平台备注"
    }}
  ],
  "lowest_price": 1234,
  "currency": "CNY",
  "error": null
}}

如果截图中没有航班结果，返回：
{{"flights": [], "lowest_price": null, "currency": "CNY", "error": "描述问题"}}

重要注意事项：
- 仔细读取截图上完整的价格数字，不要遗漏任何数位（如 ¥1,064 → 1064）
- 币种判断规则：
  - 携程/去哪儿/同程 → 人民币(CNY)
  - Google Flights URL含 curr=CNY → 人民币(CNY)
  - Google Flights URL含 curr=JPY 或日本站 → 日元(JPY)
  - 日元价格在数万级别（如 ¥15,000~¥50,000）
- price_cny 统一换算为人民币整数（日元按 1 JPY = 0.048 CNY 换算）
- original_price 和 original_currency 保留原始价格和币种
- price_cny 合理范围：单程 300-5000
- 如果价格标注为"往返价"，请在 booking_note 中注明，price_cny 填往返总价的一半
- 只提取直达航班"""

    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/png;base64,{img_b64}",
                            "detail": "high",
                        }},
                    ],
                }],
                "max_tokens": 2000,
                "temperature": 0,
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

        json_match = re.search(r"\{[\s\S]*\}", content)
        if json_match:
            data = json.loads(json_match.group())
            # 价格校验：换算后 CNY 单程 200-8000 合理
            valid_flights = []
            for f in data.get("flights", []):
                price = f.get("price_cny")
                if price and 200 <= price <= 8000:
                    valid_flights.append(f)
                elif price:
                    log.warning(f"  过滤异常价格: ¥{price} ({f.get('airline', '')})")
            data["flights"] = valid_flights
            data["lowest_price"] = min((f["price_cny"] for f in valid_flights), default=None)
            return data
        else:
            return {"flights": [], "lowest_price": None, "error": f"LLM返回非JSON: {content[:200]}"}

    except Exception as e:
        log.error(f"LLM 分析失败: {e}")
        return {"flights": [], "lowest_price": None, "error": str(e)}


def analyze_all_screenshots(screenshots):
    """分析所有截图"""
    results = {"outbound": [], "return": [], "timestamp": datetime.now().isoformat()}

    for ss in screenshots:
        log.info(f"🤖 分析: {ss['name']} {ss['label']}")
        analysis = analyze_screenshot(ss)
        analysis["source"] = ss["name"]
        analysis["url"] = ss["url"]

        if ss["direction"] == "outbound":
            results["outbound"].append(analysis)
        else:
            results["return"].append(analysis)

        if analysis.get("error"):
            log.warning(f"  ⚠️ {analysis['error']}")
        elif analysis.get("flights"):
            log.info(f"  ✓ {len(analysis['flights'])} 个航班, 最低 ¥{analysis.get('lowest_price', '?')}")

    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 价格分析
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def find_best_combinations(results, trip):
    """找出符合条件的最优往返组合"""
    depart_after = trip.get("depart_after", 19)
    budget = trip.get("budget", 1500)

    outbound_flights = []
    for src in results["outbound"]:
        for f in src.get("flights", []):
            try:
                dep_hour = int(f["departure_time"].split(":")[0])
                if dep_hour >= depart_after:
                    f["_source"] = src["source"]
                    f["_url"] = src["url"]
                    outbound_flights.append(f)
            except:
                continue

    return_flights = []
    for src in results["return"]:
        for f in src.get("flights", []):
            f["_source"] = src["source"]
            f["_url"] = src["url"]
            return_flights.append(f)

    outbound_flights.sort(key=lambda x: x.get("price_cny", 99999))
    return_flights.sort(key=lambda x: x.get("price_cny", 99999))

    combos = []
    for ob in outbound_flights[:8]:
        for rt in return_flights[:8]:
            total = (ob.get("price_cny") or 99999) + (rt.get("price_cny") or 99999)
            combos.append({
                "outbound": ob,
                "return": rt,
                "total": total,
                "within_budget": total <= budget,
            })

    combos.sort(key=lambda x: x["total"])
    return combos[:10]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Telegram 通知
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def tg_send(text, parse_mode="Markdown"):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram 未配置，跳过通知")
        log.info(f"[TG预览]\n{text}")
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info("TG 通知已发送")
        return True
    except Exception as e:
        log.error(f"TG 发送失败: {e}")
        return False


def tg_check_ack():
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False

    try:
        state = load_state()
        last_update_id = state.get("last_tg_update_id", 0)

        resp = requests.get(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 1},
            timeout=10,
        )
        resp.raise_for_status()
        updates = resp.json().get("result", [])

        for update in updates:
            msg = update.get("message", {})
            text = msg.get("text", "")
            chat_id = str(msg.get("chat", {}).get("id", ""))
            state["last_tg_update_id"] = update["update_id"]

            if chat_id == str(TG_CHAT_ID) and ACK_KEYWORD in text:
                save_state(state)
                return True

        save_state(state)
        return False

    except Exception as e:
        log.error(f"TG 检查确认失败: {e}")
        return False


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


# 用于 /check 命令触发立即检查
force_check_event = asyncio.Event()


async def tg_command_listener():
    """后台监听 TG 命令"""
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
                    tg_send(
                        f"📊 *系统状态*\n\n"
                        f"启动次数: {boot}\n"
                        f"已巡查: {uptime_checks} 次\n"
                        f"历史最低: ¥{best}\n"
                        f"预算: ¥{BUDGET_TOTAL}\n"
                        f"检查间隔: {CHECK_INTERVAL//60} 分钟"
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


def format_alert_message(combos, results, trip=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    ob_date = trip["outbound_date"] if trip else "?"
    rt_date = trip["return_date"] if trip else "?"
    budget = trip["budget"] if trip else 1500
    trip_id = trip["id"] if trip else "?"

    lines = [f"✈️ *机票价格更新* ({ts}) 行程#{trip_id}\n"]
    lines.append(f"📅 去程: {ob_date} 东京→上海")
    lines.append(f"📅 回程: {rt_date} 上海→东京")
    lines.append(f"💰 预算: ¥{budget}(CNY) 往返\n")

    if combos:
        best = combos[0]
        ob = best["outbound"]
        rt = best["return"]
        emoji = "🎉" if best["within_budget"] else "📊"

        def _price_str(f):
            op = f.get("original_price")
            oc = f.get("original_currency", "")
            cny = f.get("price_cny", "?")
            if oc == "JPY" and op:
                return f"¥{cny}(≈{op:,}円)"
            return f"¥{cny}"

        lines.append(f"{emoji} *最优组合: ¥{best['total']}*")
        lines.append(f"{'✅ 低于预算!' if best['within_budget'] else '⚠️ 超出预算'}\n")

        lines.append(f"*去程* {ob.get('airline', '')} {ob.get('flight_no', '')}")
        lines.append(f"  {ob.get('departure_time', '')}→{ob.get('arrival_time', '')} {_price_str(ob)} ({ob.get('_source', '')})")

        lines.append(f"*回程* {rt.get('airline', '')} {rt.get('flight_no', '')}")
        lines.append(f"  {rt.get('departure_time', '')}→{rt.get('arrival_time', '')} {_price_str(rt)} ({rt.get('_source', '')})")

        lines.append(f"\n🔗 *购买链接:*")
        lines.append(f"去程: {ob.get('_url', '')}")
        lines.append(f"回程: {rt.get('_url', '')}")

        if len(combos) > 1:
            lines.append(f"\n📋 *其他组合 (前5):*")
            for i, c in enumerate(combos[1:5], 2):
                o, r = c["outbound"], c["return"]
                lines.append(
                    f"{i}. ¥{c['total']} | "
                    f"{o.get('airline', '?')} {o.get('departure_time', '')} + "
                    f"{r.get('airline', '?')} {r.get('departure_time', '')}"
                )
    else:
        lines.append("⚠️ 未能组合出符合时间要求的航班\n")
        lines.append("*各平台最低价:*")
        for direction, label in [("outbound", "去程"), ("return", "回程")]:
            for src in results[direction]:
                lp = src.get("lowest_price")
                if lp:
                    lines.append(f"  {label} {src['source']}: ¥{lp}")

    lines.append(f"\n💬 回复「{ACK_KEYWORD}」停止推送")
    return "\n".join(lines)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 状态持久化
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

def get_db():
    """获取 TiDB 数据库连接"""
    import pymysql
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME,
        ssl={"ca": None}, ssl_verify_cert=False,
        ssl_verify_identity=False, charset="utf8mb4",
    )


def save_to_db(results, combos, trip):
    """将所有航班数据和巡查汇总写入 TiDB"""
    now = datetime.now()
    trip_id = trip["id"]

    try:
        conn = get_db()
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
        conn.close()
        log.info(f"💾 已入库: {flights_count} 条航班 + 1 条汇总")

    except Exception as e:
        log.error(f"数据库写入失败: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主流程
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def push_until_ack(msg):
    """每分钟推送直到确认"""
    push_count = 1
    state = load_state()

    while not shutdown_event.is_set():
        await asyncio.sleep(PUSH_INTERVAL)

        if tg_check_ack():
            log.info("✅ 收到确认回复，停止推送")
            state["pending_ack"] = False
            save_state(state)
            tg_send("✅ 已确认收到，停止推送。")
            break

        push_count += 1
        log.info(f"📢 第 {push_count} 次推送...")
        tg_send(f"📢 *第{push_count}次提醒*\n\n{msg}")

        if push_count >= 60:
            log.warning("达到推送上限，停止")
            state["pending_ack"] = False
            save_state(state)
            break


async def run_check():
    """执行一次完整的价格检查（遍历所有 active 行程）"""
    trips = get_active_trips()
    if not trips:
        log.warning("没有 active 行程，跳过检查")
        return

    state = load_state()
    check_count = state.get("check_count", 0) + 1
    state["check_count"] = check_count
    save_state(state)

    brief_lines = [f"🕐 *{datetime.now().strftime('%H:%M')} 巡查报告* (第{check_count}次)\n"]

    for trip in trips:
        log.info("=" * 50)
        log.info(f"✈️ 检查行程#{trip['id']}: {trip['outbound_date']} → {trip['return_date']} (预算¥{trip['budget']})")

        screenshots = await capture_screenshots(trip)
        if not screenshots:
            log.error(f"行程#{trip['id']} 未获取到截图")
            brief_lines.append(f"❌ 行程#{trip['id']} 抓取失败")
            continue

        results = analyze_all_screenshots(screenshots)
        combos = find_best_combinations(results, trip)
        save_to_db(results, combos, trip)

        best_total = combos[0]["total"] if combos else None
        prev_best = trip.get("best_price")
        budget = trip["budget"]

        # 更新历史最低
        if best_total:
            update_trip_best_price(trip["id"], best_total)

        hit_budget = best_total and best_total <= budget
        price_dropped = best_total and prev_best and best_total < prev_best * 0.95

        if hit_budget or price_dropped:
            msg = format_alert_message(combos, results, trip)
            log.info(f"\n{msg}")
            tg_send(msg)
            s = load_state()
            s["pending_ack"] = True
            s["last_alert_msg"] = msg
            save_state(s)
            await push_until_ack(msg)
        else:
            # 简报
            trend = ""
            if prev_best and best_total:
                if best_total < prev_best:
                    trend = f" 📉↓¥{prev_best - best_total}"
                elif best_total > prev_best:
                    trend = f" 📈↑¥{best_total - prev_best}"
                else:
                    trend = " ➡️持平"

            diff = f"¥{best_total - budget}" if best_total else "?"
            new_best = min(best_total or 99999, prev_best or 99999)
            brief_lines.append(
                f"✈️ *#{trip['id']}* {trip['outbound_date']}→{trip['return_date']}\n"
                f"  最低往返: ¥{best_total or '?'}{trend} | 差预算: {diff}\n"
                f"  历史最低: ¥{new_best if new_best < 99999 else '?'}"
            )

    # 发送汇总简报（没有触发好价推送时）
    state = load_state()
    if not state.get("pending_ack") and len(brief_lines) > 1:
        tg_send("\n\n".join(brief_lines))


async def main():
    """主循环：定时执行价格检查"""
    # 确保数据目录存在
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 注册信号处理
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log.info("=" * 55)
    log.info("✈️ 机票价格监控系统启动 (Docker)")
    log.info(f"   去程: {OUTBOUND_DATE} 东京→上海 ({OUTBOUND_DEPART_AFTER}:00后)")
    log.info(f"   回程: {RETURN_DATE} 上海→东京 (红眼)")
    log.info(f"   预算: ¥{BUDGET_TOTAL} 往返")
    log.info(f"   检查间隔: {CHECK_INTERVAL}s")
    log.info(f"   TG通知: {'已配置' if TG_BOT_TOKEN else '⚠️ 未配置'}")
    log.info("=" * 55)

    # 检查必要配置
    if not LLM_API_KEY:
        log.error("❌ LLM_API_KEY 未配置，退出")
        return

    # 设置 TG Bot 菜单命令
    setup_tg_commands()

    # 🟢 启动打招呼（健康检查）
    state = load_state()
    boot_count = state.get("boot_count", 0) + 1
    state["boot_count"] = boot_count
    save_state(state)
    tg_send(
        f"🟢 *机票监控系统已上线* (第{boot_count}次启动)\n\n"
        f"📊 监控行程: {len(get_active_trips())} 个\n"
        f"⏰ 约每 {CHECK_INTERVAL//60} 分钟巡查（随机抖动防检测）\n\n"
        f"💡 可用命令:\n"
        f"/check - 立即查价\n"
        f"/status - 系统状态\n"
        f"/history - 价格趋势\n"
        f"/trips - 查看所有行程\n"
        f"/trip add 去程 回程 预算 - 添加行程\n"
        f"/trip del 编号 - 删除行程"
    )

    # 首次检查是否有未确认的通知
    if state.get("pending_ack") and state.get("last_alert_msg"):
        log.info("发现未确认的通知，继续推送...")
        if tg_check_ack():
            state["pending_ack"] = False
            save_state(state)
        else:
            await push_until_ack(state["last_alert_msg"])

    # 启动 TG 命令监听（后台）
    tg_listener_task = asyncio.create_task(tg_command_listener())

    # 主循环
    while not shutdown_event.is_set():
        try:
            await run_check()
        except Exception as e:
            log.error(f"检查异常: {e}", exc_info=True)
            tg_send(f"⚠️ 机票监控异常: {e}")

        # 等待下一次检查（可被 shutdown 或 /check 命令中断）
        # 随机间隔：CHECK_INTERVAL ± 30%，防止被目标站点识别为定时爬虫
        jitter = random.uniform(0.7, 1.3)
        wait_time = int(CHECK_INTERVAL * jitter)
        log.info(f"⏰ 下次检查: {wait_time}s 后 (随机抖动)")
        force_check_event.clear()
        done, _ = await asyncio.wait(
            [asyncio.create_task(shutdown_event.wait()),
             asyncio.create_task(force_check_event.wait())],
            timeout=wait_time,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if force_check_event.is_set():
            log.info("📢 收到 /check 命令，立即执行检查")

    tg_listener_task.cancel()
    log.info("🛑 监控系统已停止")


if __name__ == "__main__":
    asyncio.run(main())
