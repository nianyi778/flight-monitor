"""
截图抓取模块 - Playwright + playwright-stealth + 随机行为模拟
"""

import asyncio

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from app.anti_bot import inspect_browser_page, make_result
from app.config import SCREENSHOT_DIR, BROWSER_PROFILE, now_jst, log
from app.matcher import get_search_urls
from app.source_runtime import choose_profile_id, choose_proxy


_PROFILE_POOL = {
    "ctrip_win": {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "viewport": {"width": 1366, "height": 768},
        "locale": "ja-JP",
        "platform": "Win32",
    },
    "ctrip_mac": {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "viewport": {"width": 1440, "height": 900},
        "locale": "ja-JP",
        "platform": "MacIntel",
    },
    "google_win": {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "viewport": {"width": 1536, "height": 864},
        "locale": "ja-JP",
        "platform": "Win32",
    },
    "google_safari": {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
        "viewport": {"width": 1280, "height": 800},
        "locale": "ja-JP",
        "platform": "MacIntel",
    },
}

_PROFILE_CHOICES = {
    "ctrip": ["ctrip_win", "ctrip_mac"],
    "google": ["google_win", "google_safari"],
}


async def _simulate_human(page):
    """模拟真人行为：随机滚动 + 鼠标移动"""
    await page.evaluate("window.scrollBy(0, 220)")
    await asyncio.sleep(0.5)
    await page.mouse.move(420, 260)
    await asyncio.sleep(0.3)
    await page.evaluate("window.scrollBy(0, 120)")
    await asyncio.sleep(0.4)


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
        await asyncio.sleep(2)
        log.info(f"  ⚡ 价格元素已就绪")
    except Exception:
        log.info(f"  ⏳ 价格元素超时，fallback 等待 {fallback_s}s")
        await asyncio.sleep(fallback_s + 1)


async def _run_context(p, profile_id, searches, timestamp, label, proxy):
    """在独立浏览器 context 中顺序抓取一组 URL"""
    from app.scheduler import shutdown_event

    profile = _PROFILE_POOL[profile_id]
    profile_dir = BROWSER_PROFILE / f"{label.lower()}_{profile_id}"
    profile_dir.mkdir(parents=True, exist_ok=True)

    screenshots = []

    launch_opts = dict(
        user_data_dir=str(profile_dir),
        headless=True,
        viewport=profile["viewport"],
        locale=profile["locale"],
        timezone_id="Asia/Tokyo",
        user_agent=profile["user_agent"],
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
    if proxy.get("url"):
        launch_opts["proxy"] = {"server": proxy["url"]}

    try:
        context = await p.chromium.launch_persistent_context(**launch_opts)
        stealth = Stealth(
            navigator_languages_override=("ja", "ja-JP"),
            navigator_platform_override=profile["platform"],
        )
        await stealth.apply_stealth_async(context)

        for idx, search in enumerate(searches):
            if shutdown_event.is_set():
                break

            if idx > 0:
                delay = 6
                log.info(f"  [{label}] ⏳ 等待 {delay:.0f}s...")
                await asyncio.sleep(delay)

            page = await context.new_page()
            date_tag = f"_{search['flight_date']}" if search.get("flight_date") else ""
            ss_name = f"{timestamp}_{search['name']}{date_tag}_{search['direction']}.png"
            ss_path = SCREENSHOT_DIR / ss_name

            try:
                log.info(f"[{label}] 抓取: {search['label']} ({search['name']})")
                site_home = "https://www.google.co.jp/travel/flights" if "Google" in search["name"] else "https://flights.ctrip.com/"
                await page.goto(site_home, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(1)
                await _simulate_human(page)
                await page.goto(search["url"], wait_until="domcontentloaded", timeout=30000)

                title = await page.title()
                body_text = ""
                try:
                    body_text = await page.locator("body").inner_text(timeout=2000)
                except Exception:
                    pass
                page_status, block_reason = inspect_browser_page(body_text[:2000], title=title, url=page.url)
                if page_status == "blocked":
                    screenshots.append({
                        "name": search["name"],
                        "direction": search["direction"],
                        "label": search["label"],
                        "url": search["url"],
                        "flight_date": search.get("flight_date", ""),
                        "analysis": make_result(
                            source=search["name"],
                            url=search["url"],
                            flight_date=search.get("flight_date", ""),
                            error=f"浏览器页面被识别为风控页: {block_reason}",
                            status="blocked",
                            block_reason=block_reason,
                            retryable=False,
                            request_mode="browser",
                            proxy_id=proxy.get("id"),
                            profile_id=profile_id,
                        ),
                    })
                    continue

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

                await asyncio.sleep(1)
                await page.screenshot(path=str(ss_path), full_page=False)
                screenshots.append({
                    "path": str(ss_path),
                    "name": search["name"],
                    "direction": search["direction"],
                    "label": search["label"],
                    "url": search["url"],
                    "flight_date": search.get("flight_date", ""),
                    "proxy_id": proxy.get("id"),
                    "profile_id": profile_id,
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


async def capture_screenshots_batch(search_urls, runtime_state=None):
    """批量抓取：携程 / Google 并行双 context，内部各自串行"""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    timestamp = now_jst().strftime("%Y%m%d_%H%M")

    # 按数据源分组
    ctrip = [s for s in search_urls if "Google" not in s["name"]]
    google = [s for s in search_urls if "Google" in s["name"]]

    log.info(f"📡 携程 {len(ctrip)} 个 / Google {len(google)} 个，并行抓取")

    async with async_playwright() as p:
        tasks = []
        if ctrip:
            proxy = choose_proxy(runtime_state or {}, "browser_fallback", now_jst())
            profile_id = choose_profile_id(runtime_state or {}, "ctrip_browser", _PROFILE_CHOICES["ctrip"], now_jst())
            tasks.append(_run_context(p, profile_id, ctrip, timestamp, "携程", proxy))
        if google:
            proxy = choose_proxy(runtime_state or {}, "browser_fallback", now_jst())
            profile_id = choose_profile_id(runtime_state or {}, "google_browser", _PROFILE_CHOICES["google"], now_jst())
            tasks.append(_run_context(p, profile_id, google, timestamp, "Google", proxy))

        results = await asyncio.gather(*tasks)

    screenshots = [ss for batch in results for ss in batch]
    return screenshots
