"""代币屏蔽、同币冷却、妖币热度与 Telegram 按钮。"""

from __future__ import annotations

import asyncio
import time

import httpx

from app import token_guard
from app.alerts import process_payload, should_alert
from app.config import settings
from app.formatter import format_alert
from app.telegram_commands import TelegramCommandPoller


def test_env_blocklist_skips_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "blocked_tokens", "pepe, doge")
    monkeypatch.setattr(settings, "filter_mainstream_tokens", False)
    monkeypatch.setattr(settings, "require_binance_listing", False)
    monkeypatch.setattr(settings, "min_usd_value", 1.0)

    assert should_alert("PEPE", 2_000_000) == (False, "blocked_token")
    assert should_alert("WIF", 2_000_000) == (True, "")


def test_runtime_mute_and_unmute(monkeypatch) -> None:
    monkeypatch.setattr(settings, "filter_mainstream_tokens", False)
    monkeypatch.setattr(settings, "require_binance_listing", False)
    monkeypatch.setattr(settings, "min_usd_value", 1.0)

    token_guard.block_token("BONK", reason="manual", source="test")
    assert should_alert("BONK", 2_000_000) == (False, "blocked_token")
    assert token_guard.unblock_token("bonk") is True
    assert should_alert("BONK", 2_000_000) == (True, "")


def test_snooze_expires(monkeypatch) -> None:
    monkeypatch.setattr(settings, "filter_mainstream_tokens", False)
    monkeypatch.setattr(settings, "require_binance_listing", False)
    monkeypatch.setattr(settings, "min_usd_value", 1.0)

    now = time.time()
    token_guard.block_token("MEW", reason="snooze", source="test", until=now + 60)
    assert token_guard.evaluate("MEW", 2_000_000, now=now) == (False, "token_snoozed")
    assert token_guard.evaluate("MEW", 2_000_000, now=now + 120) == (True, "")


def test_cooldown_and_heat_cap(monkeypatch) -> None:
    monkeypatch.setattr(settings, "filter_mainstream_tokens", False)
    monkeypatch.setattr(settings, "require_binance_listing", False)
    monkeypatch.setattr(settings, "min_usd_value", 1.0)
    monkeypatch.setattr(settings, "yaobi_mode", True)
    monkeypatch.setattr(settings, "token_cooldown_seconds", 3600)
    monkeypatch.setattr(settings, "token_max_alerts_per_window", 2)
    monkeypatch.setattr(settings, "token_alert_window_seconds", 21600)
    monkeypatch.setattr(settings, "token_cooldown_break_usd", 0.0)

    now = 1_700_000_000.0
    assert token_guard.evaluate("WIF", 1_000_000, now=now) == (True, "")
    token_guard.record_alert("WIF", now=now)
    assert token_guard.evaluate("WIF", 1_000_000, now=now + 60) == (False, "token_cooldown")
    assert token_guard.evaluate("WIF", 1_000_000, now=now + 3700) == (True, "")
    token_guard.record_alert("WIF", now=now + 3700)
    assert token_guard.evaluate("WIF", 1_000_000, now=now + 8000) == (False, "token_heat")


def test_large_transfer_can_break_cooldown(monkeypatch) -> None:
    monkeypatch.setattr(settings, "token_cooldown_seconds", 3600)
    monkeypatch.setattr(settings, "yaobi_mode", True)
    monkeypatch.setattr(settings, "token_max_alerts_per_window", 1)
    monkeypatch.setattr(settings, "token_cooldown_break_usd", 5_000_000)

    now = 1_700_000_000.0
    token_guard.record_alert("POPCAT", now=now)
    assert token_guard.evaluate("POPCAT", 1_000_000, now=now + 10) == (False, "token_cooldown")
    assert token_guard.evaluate("POPCAT", 5_000_000, now=now + 10) == (True, "")


def test_format_alert_includes_heat_and_keyboard(monkeypatch) -> None:
    monkeypatch.setattr(settings, "yaobi_mode", True)
    monkeypatch.setattr(settings, "token_alert_window_seconds", 21600)
    monkeypatch.setattr(settings, "telegram_mute_button", True)

    text = format_alert({
        "tokenSymbol": "WIF",
        "usdValue": 1_200_000,
        "fromAddressLabel": "Unknown",
        "toAddressLabel": "Binance",
        "fromAddress": "0x1111111111111111111111111111111111111111",
        "toAddress": "0x2222222222222222222222222222222222222222",
        "blockchain": "solana",
        "unitAmount": 1000,
    })
    assert "窗口内首次出现" in text
    assert "妖币异动" in text
    keyboard = token_guard.mute_keyboard("WIF")
    assert keyboard["inline_keyboard"][0][0]["callback_data"] == "mute|WIF"
    assert keyboard["inline_keyboard"][0][1]["callback_data"] == "snooze|WIF|21600"


def test_telegram_button_mutes_token() -> None:
    poller = TelegramCommandPoller(httpx.AsyncClient())
    text = poller._apply_callback("mute|PEPE")
    assert "已永久屏蔽 PEPE" in text
    assert token_guard.evaluate("PEPE", 2_000_000) == (False, "blocked_token")


def test_telegram_commands_mute_and_list() -> None:
    poller = TelegramCommandPoller(httpx.AsyncClient())
    assert "已永久屏蔽 FLOKI" in poller._handle_command("/mute", ["floki"])
    listing = poller._handle_command("/blocklist", [])
    assert "FLOKI" in listing
    assert "已取消屏蔽 FLOKI" in poller._handle_command("/unmute", ["FLOKI"])


def test_process_payload_attaches_mute_button_and_records_heat(monkeypatch) -> None:
    monkeypatch.setattr(settings, "filter_mainstream_tokens", False)
    monkeypatch.setattr(settings, "require_binance_listing", False)
    monkeypatch.setattr(settings, "min_usd_value", 1.0)
    monkeypatch.setattr(settings, "telegram_bot_token", "tok")
    monkeypatch.setattr(settings, "telegram_chat_id", "123")
    monkeypatch.setattr(settings, "telegram_topic_id", 0)
    monkeypatch.setattr(settings, "telegram_mute_button", True)
    monkeypatch.setattr(settings, "yaobi_mode", True)
    monkeypatch.setattr(settings, "token_cooldown_seconds", 3600)

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.content
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    async def run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await process_payload(
                {"tokenSymbol": "MEW", "usdValue": 1_500_000, "unitAmount": 10},
                client,
                source="test",
            )

    result = asyncio.run(run())
    assert result["status"] == "sent"
    body = captured["json"].decode()
    assert "mute|MEW" in body
    assert token_guard.evaluate("MEW", 1_500_000) == (False, "token_cooldown")
