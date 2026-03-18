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


async def _wait_for_prices(page, site_name, fallback_s):
    """等待价格元素出现，比固定 sleep 更快；超时则 fallback 固定等待"""
    if "Google" in site_name:
        selector = '[aria-label*="円"]'
        timeout_ms = 20000
    else:
        selector = '[class*="price"]'
        timeout_ms = 15000

    try:
        await page.wait_for_selector(selector, timeout=timeout_ms)
        await asyncio.sleep(random.uniform(1.5, 3))  # 少量抖动确保渲染完成
        log.info(f"  ⚡ 价格元素已就绪")
    except Exception:
        log.info(f"  ⏳ 价格元素超时，fallback 等待 {fallback_s}s")
        await asyncio.sleep(fallback_s + random.uniform(1, 3))


async def _run_context(p, profile_subdir, searches, timestamp, label):
    """在独立浏览器 context 中顺序抓取一组 URL"""
    from app.scheduler import shutdown_event

    profile_dir = BROWSER_PROFILE / profile_subdir
    profile_dir.mkdir(parents=True, exist_ok=True)

    screenshots = []
    ua = random.choice(_UA_POOL)
    viewport = random.choice(_VIEWPORT_POOL)

    launch_opts = dict(
        user_data_dir=str(profile_dir),
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

    try:
        context = await p.chromium.launch_persistent_context(**launch_opts)
        stealth = Stealth(
            navigator_languages_override=("ja", "ja-JP"),
            navigator_platform_override="Win32",
        )
        await stealth.apply_stealth_async(context)

        for idx, search in enumerate(searches):
            if shutdown_event.is_set():
                break

            if idx > 0:
                delay = random.uniform(5, 10)
                log.info(f"  [{label}] ⏳ 等待 {delay:.0f}s...")
                await asyncio.sleep(delay)

            page = await context.new_page()
            date_tag = f"_{search['flight_date']}" if search.get("flight_date") else ""
            ss_name = f"{timestamp}_{search['name']}{date_tag}_{search['direction']}.png"
            ss_path = SCREENSHOT_DIR / ss_name

            try:
                log.info(f"[{label}] 抓取: {search['label']} ({search['name']})")
                await page.goto(search["url"], wait_until="domcontentloaded", timeout=30000)

                await _wait_for_prices(page, search["name"], search["wait"])
                await _simulate_human(page)

                for sel in ["button:has-text('Reject')", "button:has-text('Accept')",
                            "button:has-text('同意')", "button:has-text('知道了')"]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=500):
                            await btn.click()
                            await asyncio.sleep(0.5)
                    except:
                        pass

                await asyncio.sleep(random.uniform(0.5, 1.5))
                await page.screenshot(path=str(ss_path), full_page=False)
                screenshots.append({
                    "path": str(ss_path),
                    "name": search["name"],
                    "direction": search["direction"],
                    "label": search["label"],
                    "url": search["url"],
                    "flight_date": search.get("flight_date", ""),
                })
                log.info(f"  [{label}] ✓ {ss_path.name}")
            except Exception as e:
                log.error(f"  [{label}] ✗ {search['name']} {search['direction']}: {e}")
            finally:
                await page.close()

        await context.close()
    except Exception as e:
        log.error(f"[{label}] 浏览器启动失败: {e}")

    return screenshots


async def capture_screenshots(trip):
    """抓取单个行程的截图（兼容旧接口）"""
    search_urls = get_search_urls(trip)
    return await capture_screenshots_batch(search_urls)


async def capture_screenshots_batch(search_urls):
    """批量抓取：携程 / Google 并行双 context，内部各自串行"""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    timestamp = now_jst().strftime("%Y%m%d_%H%M")

    # 随机跳过部分 URL（降低指纹）
    if len(search_urls) > 4 and random.random() > 0.3:
        skip_count = random.randint(1, min(2, len(search_urls) // 5))
        skip_indices = set(random.sample(range(len(search_urls)), skip_count))
        log.info(f"🎲 本轮随机跳过 {skip_count}/{len(search_urls)} 个搜索")
        search_urls = [s for i, s in enumerate(search_urls) if i not in skip_indices]

    # 按数据源分组
    ctrip = [s for s in search_urls if "Google" not in s["name"]]
    google = [s for s in search_urls if "Google" in s["name"]]

    log.info(f"📡 携程 {len(ctrip)} 个 / Google {len(google)} 个，并行抓取")
    if PROXY_URL:
        log.info(f"🏠 代理: {PROXY_URL.split('@')[-1] if '@' in PROXY_URL else PROXY_URL}")

    async with async_playwright() as p:
        tasks = []
        if ctrip:
            tasks.append(_run_context(p, "ctrip", ctrip, timestamp, "携程"))
        if google:
            tasks.append(_run_context(p, "google", google, timestamp, "Google"))

        results = await asyncio.gather(*tasks)

    screenshots = [ss for batch in results for ss in batch]
    return screenshots
