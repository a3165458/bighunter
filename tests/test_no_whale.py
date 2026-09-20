"""回归：Whale Alert 数据源已彻底移除，源码中不得残留引用。"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_config_has_no_whale_alert_settings() -> None:
    from app.config import settings

    for attr in (
        "whale_alert_polling_enabled",
        "whale_alert_url",
        "whale_alert_poll_interval_seconds",
    ):
        assert not hasattr(settings, attr), f"settings 仍残留 {attr}"


def test_whale_alert_module_deleted() -> None:
    assert not (ROOT / "app" / "whale_alert_polling.py").exists()


def test_main_no_longer_starts_whale_alert() -> None:
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    for token in ("whale_alert", "whale-alert", "whalealert"):
        assert token not in main.lower(), f"main 仍引用 {token}"


def test_no_whale_alert_references_in_managed_files() -> None:
    """源码与配置中不允许再出现 whale-alert 域名 / WhaleAlert / whale_alert。"""
    targets = [
        "app/config.py",
        "app/main.py",
        "app/arkham_polling.py",
        "app/arkham_home_polling.py",
        "app/arkham_api_polling.py",
        "app/alerts.py",
        "docker-compose.yml",
        ".env.example",
    ]
    forbidden = ("whale.alerts", "whale_alert", "whale_alert.io")
    for rel in targets:
        p = ROOT / rel
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"{rel} 仍引用 {token}"


def test_arkham_single_source_precedence() -> None:
    """main 中 API Key 与公开页二选一，防双源重复推送。"""
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "arkham_api_key" in main
    assert "arkham_polling_enabled" in main
    # Key 分支与无 Key 分支互斥（elif）
    assert "elif settings.arkham_polling_enabled" in main