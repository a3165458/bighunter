"""WhaleScope — 共享告警处理管线（过滤 → 格式化 → 发送）。

Webhook 和 Polling 两条路径均调用此模块，避免逻辑重复。
"""

import logging
from typing import Optional

import httpx

from app.config import settings
from app.formatter import format_alert
from app import binance_listings
from app import token_guard

logger = logging.getLogger("whalescope.alerts")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def parse_usd_value(raw_value: object) -> Optional[float]:
    """尝试将任意值转为 float，失败返回 None。"""
    try:
        return float(raw_value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def should_alert(token: str, usd_value: float) -> tuple[bool, str]:
    """
    判断一笔转账是否需要发送告警。

    Returns:
        (True, "")           — 应发送
        (False, skip_reason) — 应跳过，附带跳过原因
    """
    if settings.filter_mainstream_tokens and token in settings.exclude_tokens:
        return False, "mainstream_token"
    if usd_value < settings.min_usd_value:
        return False, "below_threshold"
    if settings.require_binance_listing and not binance_listings.is_listed(token):
        return False, "not_on_binance"
    ok, skip_reason = token_guard.evaluate(token, usd_value)
    if not ok:
        return False, skip_reason
    return True, ""


# ---------------------------------------------------------------------------
# Telegram 推送
# ---------------------------------------------------------------------------

async def send_telegram_message(
    text: str,
    http_client: httpx.AsyncClient,
    reply_markup: Optional[dict] = None,
    parse_mode: str = "MarkdownV2",
) -> dict:
    """
    发送 Telegram 消息，返回结果 dict，不抛出异常。

    Returns:
        {"ok": True,  "message_id": int}
        {"ok": False, "error": str}
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return {"ok": False, "error": "Telegram not configured (missing token or chat_id)"}

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if settings.telegram_topic_id:
        payload["message_thread_id"] = settings.telegram_topic_id

    try:
        resp = await http_client.post(
            url,
            json=payload,
        )
    except httpx.RequestError as exc:
        logger.error("Telegram request error: %s", exc)
        return {"ok": False, "error": f"Request error: {exc}"}

    if resp.status_code != 200:
        logger.error("Telegram API HTTP %s: %s", resp.status_code, resp.text)
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    data = resp.json()
    if not data.get("ok"):
        logger.error("Telegram API returned ok=false: %s", data)
        return {"ok": False, "error": f"Telegram rejected: {data}"}

    msg_id = data["result"]["message_id"]
    logger.debug("Telegram message sent, message_id=%s", msg_id)
    return {"ok": True, "message_id": msg_id}


# ---------------------------------------------------------------------------
# 共享处理管线
# ---------------------------------------------------------------------------

async def process_payload(
    payload: dict,
    http_client: httpx.AsyncClient,
    source: str = "unknown",
) -> dict:
    """
    统一处理管线：解析 → 过滤 → 格式化 → 发送。

    **不抛异常**，所有错误均通过返回值的 status 字段反映，
    适合 polling 路径直接调用。Webhook 路径可在调用后根据
    status 决定是否升级为 HTTPException。

    Returns:
        {"status": "sent",    "token": ..., "usd_value": ..., "telegram_message_id": ...}
        {"status": "skipped", "reason": skip_reason}
        {"status": "error",   "reason": error_description}
    """
    token = (payload.get("tokenSymbol") or "").upper()
    raw_usd = payload.get("usdValue", 0)
    usd_value = parse_usd_value(raw_usd)

    if usd_value is None:
        logger.warning("[%s] Invalid usdValue: %r", source, raw_usd)
        return {"status": "error", "reason": "invalid_usd_value"}

    ok, skip_reason = should_alert(token, usd_value)
    if not ok:
        logger.info("[%s] Skipped %s ($%.0f): %s", source, token, usd_value, skip_reason)
        return {"status": "skipped", "reason": skip_reason}

    message = format_alert(payload)
    markup = token_guard.mute_keyboard(token) if settings.telegram_mute_button else None
    tg_result = await send_telegram_message(message, http_client, reply_markup=markup)

    if not tg_result["ok"]:
        logger.error(
            "[%s] Telegram send failed for %s ($%.0f): %s",
            source, token, usd_value, tg_result["error"],
        )
        return {"status": "error", "reason": tg_result["error"]}

    token_guard.record_alert(token)
    logger.info("[%s] Alert sent: %s $%.0f", source, token, usd_value)
    return {
        "status": "sent",
        "token": token,
        "usd_value": usd_value,
        "telegram_message_id": tg_result["message_id"],
    }
