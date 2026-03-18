"""
截图抓取模块 - Playwright + playwright-stealth + 随机行为模拟
"""

import asyncio
import random

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from app.config import SCREENSHOT_DIR, BROWSER_PROFILE, PROXY_URL, now_jst, log
from app.matcher import get_search_urls


# 真实浏览器 UA 池（随机选一个）
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
]

# 屏幕分辨率池
_VIEWPORT_POOL = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 800},
]


async def _simulate_human(page):
    """模拟真人行为：随机滚动 + 鼠标移动"""
    # 随机滚动
    scroll_y = random.randint(100, 400)
    await page.evaluate(f"window.scrollBy(0, {scroll_y})")
    await asyncio.sleep(random.uniform(0.3, 0.8))

    # 随机鼠标移动
    x = random.randint(200, 800)
    y = random.randint(150, 500)
    await page.mouse.move(x, y)
    await asyncio.sleep(random.uniform(0.2, 0.5))

    # 偶尔再滚一下
    if random.random() > 0.5:
        await page.evaluate(f"window.scrollBy(0, {random.randint(50, 200)})")
        await asyncio.sleep(random.uniform(0.3, 0.6))


async def capture_screenshots(trip):
    """抓取单个行程的截图（兼容旧接口）"""
    search_urls = get_search_urls(trip)
    return await capture_screenshots_batch(search_urls)


async def capture_screenshots_batch(search_urls):
    """批量抓取一组搜索URL的截图（去重后的统一入口）"""
    from app.scheduler import shutdown_event

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    timestamp = now_jst().strftime("%Y%m%d_%H%M")

    screenshots = []

    # 随机跳过部分数据源（降低指纹）
    if len(search_urls) > 6 and random.random() > 0.3:
        skip_count = random.randint(1, min(3, len(search_urls) // 4))
        skip_indices = set(random.sample(range(len(search_urls)), skip_count))
        log.info(f"🎲 本轮随机跳过 {skip_count}/{len(search_urls)} 个搜索")
    else:
        skip_indices = set()

    async with async_playwright() as p:
        try:
            ua = random.choice(_UA_POOL)
            viewport = random.choice(_VIEWPORT_POOL)

            launch_opts = dict(
                user_data_dir=str(BROWSER_PROFILE),
                headless=True,
                viewport=viewport,
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                user_agent=ua,
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

            if PROXY_URL:
                launch_opts["proxy"] = {"server": PROXY_URL}
                log.info(f"🏠 代理: {PROXY_URL.split('@')[-1] if '@' in PROXY_URL else PROXY_URL}")

            context = await p.chromium.launch_persistent_context(**launch_opts)

            stealth = Stealth(
                navigator_languages_override=("ja", "ja-JP"),
                navigator_platform_override="Win32",
            )
            await stealth.apply_stealth_async(context)

            for idx, search in enumerate(search_urls):
                if shutdown_event.is_set():
                    break

                if idx in skip_indices:
                    log.info(f"  ⏭ 跳过: {search['name']} {search['direction']}")
                    continue

                # 页面间随机延迟 8-15 秒（模拟真人浏览节奏）
                if idx > 0:
                    delay = random.uniform(8, 15)
                    log.info(f"  ⏳ 等待 {delay:.0f}s...")
                    await asyncio.sleep(delay)

                page = await context.new_page()

                ss_name = f"{timestamp}_{search['name']}_{search['direction']}.png"
                ss_path = SCREENSHOT_DIR / ss_name

                try:
                    log.info(f"抓取: {search['label']} ({search['name']})")
                    await page.goto(search["url"], wait_until="domcontentloaded", timeout=30000)

                    # 等待页面加载 + 随机抖动
                    await asyncio.sleep(search["wait"] + random.uniform(2, 5))

                    # 模拟真人行为
                    await _simulate_human(page)

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

                    await asyncio.sleep(random.uniform(1, 2))
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
