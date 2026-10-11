"""Decode the existing camera viewer on demand; never capture placeholder artwork."""

import asyncio
import base64
from contextlib import suppress


async def capture_frame(base_url="http://127.0.0.1:5000"):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required"],
        )
        try:
            async with asyncio.timeout(50):
                page = await browser.new_page(viewport={"width": 960, "height": 720})
                # The capture browser has no reason to send mower/job/config commands.
                for path in ("mower", "jobs", "movement", "connection", "settings"):

                    async def read_only(route):
                        if route.request.method in {"GET", "HEAD"}:
                            await route.continue_()
                        else:
                            await route.abort()

                    await page.route(f"**/api/{path}**", read_only)
                await page.goto(base_url, wait_until="domcontentloaded")
                await page.get_by_role("button", name="Drive", exact=True).click()
                await page.get_by_role("button", name="Connect camera", exact=True).click(
                    timeout=10_000
                )
                await page.wait_for_function(
                    """() => {
                    const v = document.querySelector('.camera-panel video');
                    const c = document.querySelector('.camera-panel canvas[data-native-frame][data-ready=true]');
                    return Boolean(c && c.width > 0) || (v && v.readyState >= 2 && v.videoWidth > 0 && v.currentTime > 0);
                }""",
                    timeout=40_000,
                )
                image = await page.evaluate("""() => {
                    const v = document.querySelector('.camera-panel canvas[data-native-frame][data-ready=true]') || document.querySelector('.camera-panel video');
                    const c = document.createElement('canvas');
                    const w = v.videoWidth || v.width, h = v.videoHeight || v.height;
                    c.width = Math.min(960, w);
                    c.height = Math.round(c.width * h / w);
                    c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
                    return c.toDataURL('image/jpeg', 0.8).split(',')[1];
                }""")
                return base64.b64decode(image, validate=True)
        finally:
            with suppress(Exception):
                await browser.close()
