#!/usr/bin/env python3
"""探测本机/代理能否通过 Cloudflare 打开 arkm.com/transfers。"""
from __future__ import annotations
import asyncio, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import settings

async def main():
    from playwright.async_api import async_playwright
    from app.arkham_polling import _parse_proxy
    proxy = _parse_proxy(settings.arkham_proxy_server)
    print("proxy", proxy.get("server") if proxy else "(none)")
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=True,
                args=["--no-sandbox","--disable-dev-shm-usage","--disable-blink-features=AutomationControlled"])
        except Exception:
            browser = await p.chromium.launch(headless=True,
                args=["--no-sandbox","--disable-dev-shm-usage","--disable-blink-features=AutomationControlled"])
        ctx_kw = {"viewport":{"width":1400,"height":900}}
        if proxy:
            ctx_kw["proxy"] = proxy
        ctx = await browser.new_context(**ctx_kw)
        page = await ctx.new_page()
        r = await page.goto(settings.arkham_transfers_url, wait_until="domcontentloaded", timeout=60000)
        print("http", r.status if r else None, "title", await page.title())
        for i in range(12):
            await page.wait_for_timeout(5000)
            t = await page.title()
            ok = not any(x in t.lower() for x in ("moment","attention","verification"))
            print(f"t+{(i+1)*5}s title={t!r} ok={ok}")
            if ok:
                rows = await page.query_selector_all("table tbody tr, [role='row']")
                print("PASS rows=", len(rows))
                await ctx.storage_state(path=settings.arkham_storage_state_path or "data/arkham-storage-state.json")
                break
        else:
            print("FAIL: still on Cloudflare challenge")
            print("→ 需要住宅代理 ARKHAM_PROXY_SERVER，或 ARKHAM_API_KEY")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
