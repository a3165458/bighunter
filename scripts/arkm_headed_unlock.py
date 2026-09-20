#!/usr/bin/env python3
"""有头浏览器 +（可选）VNC 人工过 Cloudflare，保存 cookie 后供轮询复用。

为什么你本机能过、服务器无头过不了？
  Cloudflare 主要看「出口 IP 信誉 + 浏览器是否像真人」。
  家宽 + 真人手点 → 很容易过。
  机房 IP + 自动脚本 → 经常永远停在 “Performing security verification”。

本脚本做什么？
  1. 在 DISPLAY（默认 :99，本机已有 Xvfb + x11vnc:5901）上打开 **有头** Chrome
  2. 打开 arkm.com/transfers
  3. 若出现验证：请你用 VNC 连上服务器桌面，像本机一样点一次验证
  4. 通过后把 cookie 写入 data/arkham-storage-state.json
  5. 之后 docker / 轮询可复用（同一服务器 IP 下有效一段时间）

用法（在服务器上）:
  # 确认 VNC（示例：本机已有 x11vnc 监听 127.0.0.1:5901）
  # 用 SSH 隧道把 5901 转到你电脑，再用 VNC 客户端连
  #   ssh -L 5901:127.0.0.1:5901 user@这台服务器
  #   客户端连接 localhost:5901

  export DISPLAY=:99
  python scripts/arkm_headed_unlock.py

  # 若仍过不去：加住宅代理
  ARKHAM_PROXY_SERVER='http://user:pass@host:port' python scripts/arkm_headed_unlock.py

通过后 .env:
  ARKHAM_POLLING_ENABLED=true
  ARKHAM_HEADLESS=true          # 解锁后可无头复用 cookie
  ARKHAM_ANONYMOUS_MODE=true
  ARKHAM_STORAGE_STATE_PATH=data/arkham-storage-state.json
  # Docker 里路径多为 /data/arkham-storage-state.json（已挂载 ./data）
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings
from app.arkham_polling import _parse_proxy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
log = logging.getLogger("arkm_unlock")


def _storage_path() -> Path:
    raw = (settings.arkham_storage_state_path or "data/arkham-storage-state.json").strip()
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / p
    # Docker 映射：若配置的是 /data/... 且本机跑脚本，回退到仓库 data/
    if str(p).startswith("/data") and not p.parent.exists():
        p = ROOT / "data" / "arkham-storage-state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


async def main() -> None:
    display = os.environ.get("DISPLAY", "").strip()
    if not display:
        os.environ["DISPLAY"] = ":99"
        display = ":99"
        log.info("DISPLAY 未设置，使用 %s（Xvfb/VNC）", display)
    else:
        log.info("DISPLAY=%s", display)

    wait_s = int(os.environ.get("ARKHAM_UNLOCK_WAIT_SECONDS", "300"))
    url = settings.arkham_transfers_url or "https://arkm.com/transfers"
    storage = _storage_path()
    proxy = _parse_proxy(settings.arkham_proxy_server)

    log.info("目标 URL: %s", url)
    log.info("cookie 保存到: %s", storage)
    log.info("代理: %s", proxy.get("server") if proxy else "(无 — 机房 IP 可能永远过不了 CF)")
    log.info(
        "请用 VNC 连接本机显示（常见 127.0.0.1:5901，经 SSH 隧道），"
        "在弹出的浏览器里像本机一样完成验证（最多等 %ds）",
        wait_s,
    )

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": False,  # 有头：你才能在 VNC 里看到并点击
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1400,900",
            ],
        }
        try:
            browser = await p.chromium.launch(channel="chrome", **launch_kwargs)
            log.info("已启动 Google Chrome（有头）")
        except Exception as exc:
            log.warning("Chrome channel 失败 (%s)，改用 Chromium", exc)
            browser = await p.chromium.launch(**launch_kwargs)

        ctx_kw: dict = {
            "viewport": {"width": 1400, "height": 900},
            "locale": "en-US",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
        }
        if proxy:
            ctx_kw["proxy"] = proxy
        if storage.exists():
            ctx_kw["storage_state"] = str(storage)
            log.info("已加载已有 cookie 罐")

        context = await browser.new_context(**ctx_kw)
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()

        log.info("打开页面…")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as exc:
            log.warning("goto 异常（仍继续等待你手动处理）: %s", exc)

        deadline = asyncio.get_event_loop().time() + wait_s
        passed = False
        while asyncio.get_event_loop().time() < deadline:
            try:
                title = (await page.title()).lower()
            except Exception:
                title = ""
            challenged = any(
                x in title for x in ("moment", "attention", "verification", "just a")
            )
            # 业务页特征：有表格行 或 标题不再是挑战
            rows = 0
            try:
                rows = len(
                    await page.query_selector_all(
                        "table tbody tr, [role='row']"
                    )
                )
            except Exception:
                pass

            if not challenged and (rows >= 2 or "arkham" in title or "transfer" in title):
                passed = True
                log.info("检测到已通过验证 title=%r rows=%d", await page.title(), rows)
                break

            left = int(deadline - asyncio.get_event_loop().time())
            log.info(
                "等待人工验证… remaining=%ds title=%r challenged=%s rows=%d",
                left,
                title[:60],
                challenged,
                rows,
            )
            await asyncio.sleep(5)

        if not passed:
            # 再给一次宽松判断：只要 title 不像挑战且 body 较长
            try:
                title = await page.title()
                body = await page.inner_text("body")
                if "moment" not in title.lower() and len(body) > 500:
                    passed = True
                    log.info("宽松判定：可能已通过 title=%r body_len=%d", title, len(body))
            except Exception:
                pass

        if passed:
            await context.storage_state(path=str(storage))
            log.info("✅ 已保存 cookie → %s", storage)
            log.info(
                "下一步: 设置 ARKHAM_POLLING_ENABLED=true 后 "
                "docker compose up -d 或 python scripts/run_public_arkm.py"
            )
        else:
            log.error(
                "❌ %ds 内仍未通过 Cloudflare。\n"
                "  1) 确认 VNC 能看到浏览器窗口并手动点验证\n"
                "  2) 若页面一直转圈无按钮：机房 IP 被硬拦，必须加住宅代理 "
                "ARKHAM_PROXY_SERVER 或换家宽机器跑\n"
                "  3) 或申请 ARKHAM_API_KEY",
                wait_s,
            )
            # 仍保存现场 cookie，便于排查
            try:
                await context.storage_state(path=str(storage))
            except Exception:
                pass
            await browser.close()
            sys.exit(2)

        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("interrupted")
