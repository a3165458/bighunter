"""WhaleScope — 冷门代币链上异动监控 Bot (FastAPI)。"""

import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from app.config import settings
from app.alerts import process_payload, parse_usd_value
from app import binance_listings
from app import token_guard
from app import poller_health

# ---- 日志 ----
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("whalescope")
# httpx 的 INFO 请求日志包含完整 URL；Telegram Bot token 位于 URL 路径中。
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---- 全局资源（由 lifespan 管理） ----
_http_client: httpx.AsyncClient | None = None
_pollers: list = []  # 各数据源 poller 实例


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _http_client, _pollers

    _http_client = httpx.AsyncClient(timeout=15.0)
    # 唯一数据源：Arkham。有 API Key → REST；无 Key → 公开页浏览器/CDP（匿名）。
    # 两种模式取其一，避免同一笔转账被双源重复推送。
    sources = []
    if settings.arkham_api_key:
        sources.append("arkham_api")
    elif settings.arkham_polling_enabled:
        sources.append("arkham_browser")
    logger.info(
        "WhaleScope Bot started | min_usd_value=%s | exclude_tokens=%s | "
        "webhook_secret=%s | sources=%s | binance_filter=%s | yaobi=%s | "
        "cooldown=%ss | blocked=%s",
        f"{settings.min_usd_value:,.0f}",
        len(settings.exclude_tokens),
        "enabled" if settings.webhook_secret else "disabled",
        sources or ["webhook-only"],
        "enabled" if settings.require_binance_listing else "disabled",
        "on" if settings.yaobi_mode else "off",
        settings.token_cooldown_seconds,
        token_guard.snapshot()["blocked_count"],
    )

    # 启动币安上币列表缓存刷新
    if settings.require_binance_listing:
        await binance_listings.start_refresh_task(
            _http_client,
            interval=settings.binance_refresh_interval,
        )

    # 1) Arkham 官方 REST（有 Key）：无需浏览器，最稳
    if settings.arkham_api_key:
        from app.arkham_api_polling import ArkhamApiPoller
        p = ArkhamApiPoller(_http_client)
        await p.start()
        _pollers.append(p)
        if settings.arkham_polling_enabled:
            logger.info("ARKHAM_API_KEY 已配置：优先 REST，跳过网页轮询（避免双源重复推送）")

    # 2) Arkham 公开页（无 Key / 无账户登录）：首页 RECENT TRANSFERS + CDP 过 CF
    elif settings.arkham_polling_enabled:
        try:
            from app.arkham_home_polling import ArkhamHomePoller
            p = ArkhamHomePoller(_http_client)
            await p.start()
            _pollers.append(p)
        except Exception as exc:
            logger.warning("Arkham home poller failed to start (%s), fallback page poller", exc)
            from app.arkham_polling import ArkhamPoller
            p = ArkhamPoller(_http_client)
            await p.start()
            _pollers.append(p)

    if settings.telegram_bot_token and settings.telegram_chat_id:
        from app.telegram_commands import TelegramCommandPoller
        commands = TelegramCommandPoller(_http_client)
        await commands.start()
        _pollers.append(commands)

    yield

    # 关闭轮询
    for p in _pollers:
        try:
            await p.stop()
        except Exception as exc:
            logger.warning("Error stopping poller: %s", exc)
    _pollers = []

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
    sources = []
    if settings.arkham_api_key:
        sources.append("arkham_api")
    elif settings.arkham_polling_enabled:
        sources.append("arkham_browser")
    poller = poller_health.snapshot()
    return {
        "status": "ok" if (not sources or poller["ok"] or poller["age_seconds"] is None) else "degraded",
        "service": "whalescope",
        "polling_sources": sources,
        "polling": "enabled" if sources else "disabled",
        "binance_filter": "enabled" if settings.require_binance_listing else "disabled",
        "token_guard": token_guard.snapshot(),
        "poller": poller,
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


# ---- 代币屏蔽 ----
@app.get("/blocklist")
async def get_blocklist():
    return {"status": "ok", **token_guard.snapshot(), "items": token_guard.list_blocked()}


@app.post("/mute")
async def mute_token(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    token = token_guard.normalize_token((body or {}).get("token") if isinstance(body, dict) else "")
    if not token:
        raise HTTPException(status_code=400, detail="token is required")
    until = 0.0
    seconds = 0
    if isinstance(body, dict) and body.get("seconds"):
        try:
            seconds = int(body["seconds"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="seconds must be an integer") from exc
        if seconds > 0:
            until = time.time() + seconds
    token_guard.block_token(
        token,
        reason="api",
        source="http",
        until=until,
    )
    return {"status": "blocked", "token": token, "until": until, "seconds": seconds}


@app.post("/unmute")
async def unmute_token(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    token = token_guard.normalize_token((body or {}).get("token") if isinstance(body, dict) else "")
    if not token:
        raise HTTPException(status_code=400, detail="token is required")
    existed = token_guard.unblock_token(token)
    return {"status": "unblocked" if existed else "not_found", "token": token}


# ---- Telegram 连通性自测 ----
@app.post("/test-telegram")
async def test_telegram():
    """发送一条测试消息到已配置的 Telegram 群组，验证 token/chat_id/topic 配置。"""
    from app.alerts import send_telegram_message

    if _http_client is None:
        raise HTTPException(status_code=500, detail="HTTP client not initialized")

    result = await send_telegram_message(
        "✅ *WhaleScope 测试消息*\n\nBot 推送链路正常，等待转账告警中。", _http_client
    )
    if not result["ok"]:
        raise HTTPException(status_code=502, detail=result["error"])
    return {"status": "sent", "telegram_message_id": result["message_id"]}


# ---- Webhook 入口 ----
@app.post("/webhook")
async def webhook(
    request: Request,
    arkham_webhook_token: str | None = Header(None, alias="Arkham-Webhook-Token"),
    token: str | None = None,
):
    # 可选鉴权：支持 Header（Arkham-Webhook-Token）或 URL 查询参数 ?token=
    # （部分平台配置 webhook 时无法自定义 Header，可将密钥拼在 URL 里）
    if settings.webhook_secret and settings.webhook_secret not in (
        arkham_webhook_token, token
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")

    # INFO 级记录原始 payload：Arkham 实际字段格式若与预期不符，
    # 处理管线会静默跳过，必须能从日志还原现场
    logger.info("Received webhook payload: %s", payload)

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
