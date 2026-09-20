#!/usr/bin/env python3
"""无 API Key / 无 Arkham 账户登录：轮询公开 transfers 页 → Telegram。

推荐在「你自己的电脑」上运行（浏览器能直接打开 https://arkm.com/transfers 的网络环境）。
云服务器 IP 常被 Cloudflare 拦截，这时请用 CDP 挂接本机 Chrome（见 README）。

用法:
  cd /path/to/bighunter
  cp .env.example .env   # 填好 TELEGRAM_* 与 MIN_USD_VALUE
  python scripts/run_public_arkm.py

可选环境变量:
  ARKHAM_TRANSFERS_URL=https://arkm.com/transfers
  ARKHAM_POLL_INTERVAL_SECONDS=30
  ARKHAM_HEADLESS=true
  ARKHAM_CDP_URL=http://127.0.0.1:9222   # 挂接本机 Chrome 时
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# 允许从仓库根目录直接 python scripts/run_public_arkm.py
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from app.config import settings
from app.arkham_polling import ArkhamPoller


def _setup_logging() -> None:
    logging.basicConfig(
        level=(settings.log_level or "INFO").upper(),
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )


async def main() -> None:
    _setup_logging()
    log = logging.getLogger("whalescope.public_runner")

    # 强制走公开页路径（本脚本用途就是无 Key）
    settings.arkham_polling_enabled = True
    # 本机跑时：若 Docker 路径 /data 不存在，改用仓库 ./data
    storage = settings.arkham_storage_state_path or ""
    if storage.startswith("/data") and not Path(storage).parent.exists():
        local = ROOT / "data" / "arkham-storage-state.json"
        local.parent.mkdir(parents=True, exist_ok=True)
        settings.arkham_storage_state_path = str(local)
        log.info("使用本机 cookie 路径: %s", settings.arkham_storage_state_path)

    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.error("请先在 .env 配置 TELEGRAM_BOT_TOKEN 与 TELEGRAM_CHAT_ID")
        sys.exit(1)

    log.info(
        "Public ARKM runner | min_usd=%s | url=%s | headless=%s | cdp=%s",
        f"{settings.min_usd_value:,.0f}",
        settings.arkham_transfers_url,
        settings.arkham_headless,
        settings.arkham_cdp_url or "(none)",
    )
    log.info(
        "说明: 无需 API Key、无需 Arkham 登录；"
        "若卡在 Cloudflare，请在本机运行或设置 ARKHAM_CDP_URL"
    )

    async with httpx.AsyncClient(timeout=30.0) as http:
        poller = ArkhamPoller(http)
        await poller.start()
        try:
            # 一直跑到 Ctrl+C
            while True:
                await asyncio.sleep(3600)
        except (asyncio.CancelledError, KeyboardInterrupt):
            log.info("Stopping…")
        finally:
            await poller.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
