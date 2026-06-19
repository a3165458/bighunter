"""WhaleScope — 冷门代币链上异动监控 Bot (FastAPI)。"""

import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from app.config import settings
from app.alerts import process_payload, parse_usd_value
from app import binance_listings

# ---- 日志 ----
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("whalescope")

# ---- 全局资源（由 lifespan 管理） ----
_http_client: httpx.AsyncClient | None = None
_poller = None  # ArkhamPoller instance，仅当 polling 启用时创建


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _http_client, _poller

    _http_client = httpx.AsyncClient(timeout=15.0)
    logger.info(
        "WhaleScope Bot started | min_usd_value=%s | exclude_tokens=%s | "
        "webhook_secret=%s | polling=%s | binance_filter=%s",
        f"{settings.min_usd_value:,.0f}",
        len(settings.exclude_tokens),
        "enabled" if settings.webhook_secret else "disabled",
        "enabled" if settings.arkham_polling_enabled else "disabled",
        "enabled" if settings.require_binance_listing else "disabled",
    )

    # 启动币安上币列表缓存刷新
    if settings.require_binance_listing:
        await binance_listings.start_refresh_task(
            _http_client,
            interval=settings.binance_refresh_interval,
        )

    # 启动 Arkham 轮询（若已配置）
    if settings.arkham_polling_enabled:
        from app.arkham_polling import ArkhamPoller
        _poller = ArkhamPoller(_http_client)
        await _poller.start()

    yield

    # 关闭轮询
    if _poller is not None:
        await _poller.stop()

    # 关闭币安缓存刷新
    await binance_listings.stop_refresh_task()

    await _http_client.aclose()
    logger.info("WhaleScope Bot stopped")


app = FastAPI(
    title="WhaleScope",
    description="冷门代币链上异动监控 Bot",
    version="2.0.0",
    lifespan=lifespan,
)


# ---- 健康检查 ----
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "whalescope",
        "polling": "enabled" if settings.arkham_polling_enabled else "disabled",
        "binance_filter": "enabled" if settings.require_binance_listing else "disabled",
    }


# ---- 币安上币缓存状态 ----
@app.get("/binance-status")
async def binance_status():
    info = binance_listings.get_cache_info()
    return {
        "status": "ok",
        "filter_enabled": settings.require_binance_listing,
        **info,
    }


# ---- Webhook 入口 ----
@app.post("/webhook")
async def webhook(
    request: Request,
    arkham_webhook_token: str | None = Header(None, alias="Arkham-Webhook-Token"),
):
    # 可选鉴权
    if settings.webhook_secret and arkham_webhook_token != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")

    logger.debug("Received webhook payload: %s", payload)

    # 提前校验 usdValue 格式，webhook 路径对格式错误返回 400
    if parse_usd_value(payload.get("usdValue", 0)) is None:
        raise HTTPException(status_code=400, detail="Invalid usdValue")

    if _http_client is None:
        raise HTTPException(status_code=500, detail="HTTP client not initialized")

    # 调用共享处理管线
    result = await process_payload(payload, _http_client, source="webhook")

    if result["status"] == "error":
        # 区分客户端错误（invalid_usd_value 已在上方处理）与服务端错误
        raise HTTPException(status_code=502, detail=result["reason"])

    return result

