"""
截图抓取模块 - Playwright screenshot capture + anti-detection
"""

import asyncio
import random

from playwright.async_api import async_playwright

from app.config import (
    SCREENSHOT_DIR, BROWSER_PROFILE, STEALTH_JS,
    now_jst, log,
)
from app.matcher import get_search_urls


async def capture_screenshots(trip):
    """用 Playwright + 持久化指纹抓取所有平台截图"""
    from app.scheduler import shutdown_event

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    timestamp = now_jst().strftime("%Y%m%d_%H%M")

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
