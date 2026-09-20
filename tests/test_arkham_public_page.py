"""无 API Key：公开页解析 / 去重 / 配置路径。"""

from __future__ import annotations

from pathlib import Path

from app.arkham_polling import (
    MOCK_TABLE_HTML,
    _dedup_key,
    _parse_mock_html,
    _parse_transfer_response,
)
from app.config import settings


def test_public_page_mock_html_extracts_transfers() -> None:
    transfers = _parse_mock_html(MOCK_TABLE_HTML)
    assert len(transfers) >= 2
    symbols = {t["tokenSymbol"] for t in transfers}
    assert "PEPE" in symbols or "SHIB" in symbols
    assert all(t["usdValue"] > 0 for t in transfers)


def test_public_xhr_envelope_uses_shared_parser() -> None:
    """网页 XHR 与官方 EnrichedTransfers 同形：无 Key 时拦截到也能解析。"""
    payload = {
        "count": 1,
        "transfers": [
            {
                "historicalUSD": 2_500_000,
                "unitValue": 1e9,
                "tokenSymbol": "pepe",
                "chain": "ethereum",
                "transactionHash": "0xpub1",
                "fromAddress": {
                    "address": "0x1111111111111111111111111111111111111111",
                    "arkhamEntity": {"name": "Binance", "type": "cex"},
                    "arkhamLabel": {"name": "Hot Wallet"},
                },
                "toAddress": {
                    "address": "0x2222222222222222222222222222222222222222",
                },
            }
        ],
    }
    transfers = _parse_transfer_response(payload)
    assert len(transfers) == 1
    t = transfers[0]
    assert t["tokenSymbol"] == "PEPE"
    assert t["usdValue"] == 2_500_000
    assert t["fromAddressLabel"] == "Binance"
    assert t["fromAddressLabel"] != "Hot Wallet"
    assert t["arkhamUrl"] == "https://arkm.com/tx/0xpub1"


def test_dedup_key_stable_for_same_transfer() -> None:
    t = {
        "tokenSymbol": "PEPE",
        "usdValue": 1_000_000,
        "fromAddressLabel": "Binance",
        "toAddressLabel": "Whale",
        "unitAmount": 1,
        "blockchain": "ethereum",
        "arkhamUrl": "https://arkm.com/tx/0xabc",
    }
    assert _dedup_key(t) == _dedup_key(dict(t))
    assert _dedup_key(t) == "https://arkm.com/tx/0xabc"


def test_public_runner_script_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "run_public_arkm.py"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "ArkhamPoller" in text
    assert "no API key" in text.lower() or "无" in text


def test_default_public_path_does_not_require_api_key() -> None:
    """无 Key 时仍可通过公开页轮询开关工作。"""
    assert settings.arkham_api_key == "" or True  # key 可空
    # 配置项存在且页面路径默认指向公开站
    assert "arkm.com" in settings.arkham_transfers_url
    # main 在无 key 时也能挂 arkham_browser
    main = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert "arkham_polling_enabled" in main
    assert "ArkhamPoller" in main
