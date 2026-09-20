"""Telegram 屏蔽按钮与 /mute 命令轮询。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

import httpx

from app.alerts import send_telegram_message
from app.config import settings
from app import token_guard

logger = logging.getLogger("whalescope.telegram")

_HELP = (
    "代币屏蔽命令：\n"
    "/mute PEPE — 永久屏蔽\n"
    "/unmute PEPE — 取消屏蔽\n"
    "/snooze PEPE 6h — 临时静音\n"
    "/blocklist — 查看当前屏蔽列表\n"
    "也可以直接点告警消息下的按钮。"
)


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


def _same_chat(chat_id: object) -> bool:
    return str(chat_id or "") == str(settings.telegram_chat_id or "")


def _parse_duration(raw: str) -> int:
    text = (raw or "").strip().lower()
    if not text:
        return max(int(settings.token_alert_window_seconds), 3600)
    unit = text[-1]
    if unit in {"s", "m", "h", "d"}:
        try:
            value = float(text[:-1] or "0")
        except ValueError:
            return 0
        multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return int(value * multiplier)
    try:
        return int(float(text))
    except ValueError:
        return 0


def _format_blocklist() -> str:
    items = token_guard.list_blocked()
    if not items:
        return "当前没有屏蔽中的代币。"
    lines = ["当前屏蔽列表："]
    for item in items:
        if item["permanent"]:
            extra = "永久"
        else:
            extra = "临时"
        lines.append(f"- {item['token']}（{extra}，{item['reason']}）")
    return "\n".join(lines)


class TelegramCommandPoller:
    """长轮询 getUpdates，处理屏蔽按钮和命令。"""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._offset = 0

    async def start(self) -> None:
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            logger.info("Telegram command poller disabled (missing token or chat_id)")
            return
        try:
            await self._http.post(_api_url("deleteWebhook"), json={"drop_pending_updates": False})
        except httpx.HTTPError as exc:
            logger.warning("deleteWebhook failed: %s", exc)
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="telegram-command-poller")
        logger.info("Telegram command poller started (mute buttons + /mute)")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Telegram command poller stopped")

    async def _loop(self) -> None:
        await self._skip_backlog()
        while self._running:
            try:
                updates = await self._get_updates()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(5)
                continue
            for update in updates:
                update_id = int(update.get("update_id") or 0)
                if update_id:
                    self._offset = update_id + 1
                try:
                    await self.handle_update(update)
                except Exception as exc:
                    logger.warning("Telegram update handler failed: %s", exc)

    async def _skip_backlog(self) -> None:
        try:
            resp = await self._http.get(
                _api_url("getUpdates"),
                params={"timeout": 0, "offset": -1, "limit": 1},
                timeout=15.0,
            )
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to skip Telegram backlog: %s", exc)
            return
        results = data.get("result") if isinstance(data, dict) else None
        if isinstance(results, list) and results:
            update_id = int(results[-1].get("update_id") or 0)
            if update_id:
                self._offset = update_id + 1

    async def _get_updates(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": 25,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if self._offset:
            params["offset"] = self._offset
        resp = await self._http.get(_api_url("getUpdates"), params=params, timeout=35.0)
        data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError(f"getUpdates rejected: {data}")
        results = data.get("result") or []
        return [item for item in results if isinstance(item, dict)]

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return
        message = update.get("message") or update.get("edited_message")
        if isinstance(message, dict):
            await self._handle_message(message)

    async def _handle_callback(self, query: dict[str, Any]) -> None:
        data = str(query.get("data") or "")
        from_user = query.get("from") if isinstance(query.get("from"), dict) else {}
        message = query.get("message") if isinstance(query.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        query_id = str(query.get("id") or "")
        if not _same_chat(chat.get("id")):
            await self._answer_callback(query_id, "只能在配置的播报群里操作")
            return
        if not token_guard.is_user_allowed(from_user.get("id")):
            await self._answer_callback(query_id, "你没有屏蔽权限")
            return
        text = self._apply_callback(data)
        await self._answer_callback(query_id, text[:180])
        await send_telegram_message(text, self._http, parse_mode="")

    def _apply_callback(self, data: str) -> str:
        parts = [p for p in data.split("|") if p]
        if len(parts) < 2:
            return "无法识别的按钮"
        action, token = parts[0], token_guard.normalize_token(parts[1])
        if not token:
            return "代币为空"
        if action == "mute":
            token_guard.block_token(token, reason="telegram_button", source="telegram")
            return f"已永久屏蔽 {token}，之后不再播报。"
        if action == "unmute":
            token_guard.unblock_token(token)
            return f"已取消屏蔽 {token}。"
        if action == "snooze":
            seconds = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 21600
            until = time.time() + max(seconds, 60)
            token_guard.block_token(
                token, reason="snooze", source="telegram", until=until,
            )
            hours = max(seconds // 3600, 1)
            return f"已将 {token} 静音 {hours} 小时。"
        return "无法识别的按钮"

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        from_user = message.get("from") if isinstance(message.get("from"), dict) else {}
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        if not _same_chat(chat.get("id")):
            return
        command = text.split()[0].split("@", 1)[0].lower()
        args = text.split()[1:]
        if command not in {"/mute", "/block", "/unmute", "/unblock", "/snooze", "/blocklist", "/help"}:
            return
        if not token_guard.is_user_allowed(from_user.get("id")):
            await send_telegram_message("你没有屏蔽权限。", self._http, parse_mode="")
            return
        reply = self._handle_command(command, args)
        await send_telegram_message(reply, self._http, parse_mode="")

    def _handle_command(self, command: str, args: list[str]) -> str:
        if command == "/help":
            return _HELP
        if command == "/blocklist":
            return _format_blocklist()
        if command in {"/mute", "/block"}:
            token = token_guard.normalize_token(args[0] if args else "")
            if not token:
                return "用法：/mute PEPE"
            token_guard.block_token(token, reason="command", source="telegram")
            return f"已永久屏蔽 {token}。"
        if command in {"/unmute", "/unblock"}:
            token = token_guard.normalize_token(args[0] if args else "")
            if not token:
                return "用法：/unmute PEPE"
            if token_guard.unblock_token(token):
                return f"已取消屏蔽 {token}。"
            return f"{token} 不在运行时屏蔽列表里（环境变量黑名单需改 BLOCKED_TOKENS）。"
        if command == "/snooze":
            token = token_guard.normalize_token(args[0] if args else "")
            if not token:
                return "用法：/snooze PEPE 6h"
            seconds = _parse_duration(args[1] if len(args) > 1 else "")
            if seconds <= 0:
                return "时间格式不对，例如 30m / 6h / 1d"
            token_guard.block_token(
                token, reason="snooze", source="telegram", until=time.time() + seconds,
            )
            return f"已将 {token} 静音 {args[1] if len(args) > 1 else '6h'}。"
        return _HELP

    async def _answer_callback(self, query_id: str, text: str) -> None:
        if not query_id:
            return
        try:
            await self._http.post(
                _api_url("answerCallbackQuery"),
                json={"callback_query_id": query_id, "text": text[:180]},
            )
        except httpx.HTTPError as exc:
            logger.warning("answerCallbackQuery failed: %s", exc)
