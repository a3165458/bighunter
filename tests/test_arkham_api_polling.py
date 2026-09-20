"""Arkham REST API poller: parse / params / rate-safe fetch / prime+dedup."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx
import pytest

from app.alerts import should_alert
from app.arkham_api_polling import (
    ArkhamApiPoller,
    _dedup_key,
    _entity_label,
    _extract_list,
    _parse_transfer,
    reset_dedup_state,
)
from app.arkham_polling import _parse_transfer_response
from app.config import settings


# ---------------------------------------------------------------------------
# Official-shaped EnrichedTransfers fixture (from arkm.com/llms/get-transfers.md)
# ---------------------------------------------------------------------------

OFFICIAL_TRANSFER_BINANCE_HOT = {
    "blockHash": "0xf65f6387d2b83c76197d5358b0bfbf7de6d082dc6861a7772b494729bd0af455",
    "blockNumber": 25495862,
    "blockTimestamp": "2026-07-09T15:16:47Z",
    "chain": "ethereum",
    "fromAddress": {
        "address": "0xEe7aE85f2Fe2239E27D9c1E23fFFe168D63b4055",
        "arkhamEntity": {
            "id": "binance",
            "name": "Binance",
            "type": "cex",
            "website": "https://binance.com",
        },
        "arkhamLabel": {
            "address": "0xEe7aE85f2Fe2239E27D9c1E23fFFe168D63b4055",
            "chainType": "evm",
            "name": "Hot Wallet",
        },
        "chain": "ethereum",
        "contract": True,
        "isUserAddress": False,
    },
    "fromIsContract": True,
    "historicalUSD": 2995710.06,
    "id": "0xaf33dc1e353cc2ba85f67cd25889e79889a7ce41bf28773f904ac27ce8fc2ff3_68",
    "toAddress": {
        "address": "0xB5f80b0d276Bc4eCC2E95F9Bd36BC368361f87A6",
        "chain": "ethereum",
        "contract": False,
        "isUserAddress": False,
    },
    "toIsContract": False,
    "tokenAddress": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "tokenDecimals": 6,
    "tokenId": "usd-coin",
    "tokenName": "USD Coin",
    "tokenSymbol": "USDC",
    "transactionHash": "0xaf33dc1e353cc2ba85f67cd25889e79889a7ce41bf28773f904ac27ce8fc2ff3",
    "type": "",
    "unitValue": 2995710.06,
}

OFFICIAL_TRANSFER_DEPOSIT_SERVICE = {
    "blockHash": "0xfa69e420c70184c582d8cd5c6a84992372bf447b3b5656404be8c1f16c0b9ed7",
    "blockNumber": 109001701,
    "blockTimestamp": "2026-07-09T15:13:10Z",
    "chain": "bsc",
    "fromAddress": {
        "address": "0xdFD961Dc7B7A18f45F041507115a2Fe922250AaC",
        "arkhamLabel": {
            "address": "0xdFD961Dc7B7A18f45F041507115a2Fe922250AaC",
            "chainType": "evm",
            "name": "Some User Wallet",
        },
        "chain": "bsc",
        "contract": False,
        "depositServiceID": "binance",
        "isUserAddress": False,
    },
    "fromIsContract": False,
    "historicalUSD": 1637110.638546851,
    "id": "0x0349d63753ac6feb7de638455074354c672fa9bd7ba9b7d0b1da61ae3cddd029_258",
    "toAddress": {
        "address": "0x8894E0a0c962CB723c1976a4421c95949bE2D4E3",
        "arkhamEntity": {
            "id": "binance",
            "name": "Binance",
            "type": "cex",
        },
        "arkhamLabel": {
            "address": "0x8894E0a0c962CB723c1976a4421c95949bE2D4E3",
            "chainType": "evm",
            "name": "Hot Wallet",
        },
        "chain": "bsc",
        "contract": False,
        "isUserAddress": False,
    },
    "toIsContract": False,
    "tokenAddress": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
    "tokenDecimals": 18,
    "tokenId": "binance-bridged-usdc-bnb-smart-chain",
    "tokenName": "USD Coin",
    "tokenSymbol": "USDC",
    "transactionHash": "0x0349d63753ac6feb7de638455074354c672fa9bd7ba9b7d0b1da61ae3cddd029",
    "type": "",
    "unitValue": 1637110.638546851,
}


# ---------------------------------------------------------------------------
# Parse / label
# ---------------------------------------------------------------------------


def test_parse_enriched_transfer_shape() -> None:
    transfer = _parse_transfer(
        {
            "historicalUSD": 125000,
            "unitValue": 5000000,
            "tokenSymbol": "pepe",
            "chain": "ethereum",
            "transactionHash": "0xabc",
            "fromAddress": {
                "address": "0x1111111111111111111111111111111111111111",
                "arkhamEntity": {"name": "Binance"},
            },
            "toAddress": {
                "address": "0x2222222222222222222222222222222222222222",
                "arkhamEntity": {"name": "Unknown Whale"},
            },
        }
    )

    assert transfer is not None
    assert transfer["tokenSymbol"] == "PEPE"
    assert transfer["usdValue"] == 125000
    assert transfer["unitAmount"] == 5000000
    assert transfer["fromAddressLabel"] == "Binance"
    assert transfer["toAddressLabel"] == "Unknown Whale"
    assert transfer["arkhamUrl"] == "https://arkm.com/tx/0xabc"


def test_entity_preferred_over_hot_wallet_label() -> None:
    """官方示例：arkhamEntity=Binance + arkhamLabel=Hot Wallet → 用 Binance。"""
    transfer = _parse_transfer(OFFICIAL_TRANSFER_BINANCE_HOT)
    assert transfer is not None
    assert transfer["tokenSymbol"] == "USDC"
    assert transfer["usdValue"] == pytest.approx(2995710.06)
    assert transfer["unitAmount"] == pytest.approx(2995710.06)
    assert transfer["fromAddressLabel"] == "Binance"
    assert transfer["fromAddressLabel"] != "Hot Wallet"
    assert transfer["toAddress"] == "0xB5f80b0d276Bc4eCC2E95F9Bd36BC368361f87A6"
    assert transfer["blockchain"] == "ethereum"
    assert (
        transfer["arkhamUrl"]
        == "https://arkm.com/tx/0xaf33dc1e353cc2ba85f67cd25889e79889a7ce41bf28773f904ac27ce8fc2ff3"
    )
    assert transfer["_id"] == OFFICIAL_TRANSFER_BINANCE_HOT["id"]


def test_deposit_service_id_preferred_over_generic_label() -> None:
    """depositServiceID=binance 应映射为 Binance，而非模糊钱包标签。"""
    label = _entity_label(OFFICIAL_TRANSFER_DEPOSIT_SERVICE["fromAddress"])
    assert label == "Binance"

    transfer = _parse_transfer(OFFICIAL_TRANSFER_DEPOSIT_SERVICE)
    assert transfer is not None
    assert transfer["fromAddressLabel"] == "Binance"
    assert transfer["toAddressLabel"] == "Binance"  # entity over Hot Wallet


def test_extract_enriched_transfer_envelope() -> None:
    payload = {"count": 1, "transfers": [{"tokenSymbol": "PEPE"}]}
    assert _extract_list(payload) == payload["transfers"]


def test_extract_list_bare_array() -> None:
    items = [{"a": 1}, "skip", {"b": 2}]
    assert _extract_list(items) == [{"a": 1}, {"b": 2}]


def test_parse_public_page_transfer_response() -> None:
    payload = {
        "transfers": [
            {
                "historicalUSD": 90000,
                "unitValue": 123,
                "tokenSymbol": "MOCK",
                "chain": "base",
                "transactionHash": "0xdef",
                "fromAddress": "0x1111111111111111111111111111111111111111",
                "toAddress": "0x2222222222222222222222222222222222222222",
            }
        ]
    }

    transfers = _parse_transfer_response(payload)

    assert len(transfers) == 1
    assert transfers[0]["tokenSymbol"] == "MOCK"
    assert transfers[0]["usdValue"] == 90000


# ---------------------------------------------------------------------------
# Request params (usdGte from min threshold, combined base)
# ---------------------------------------------------------------------------


def test_transfer_request_parameters_usd_gte_from_min(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "arkham_api_key", "test-key")
    monkeypatch.setattr(settings, "min_usd_value", 50000.0)
    monkeypatch.setattr(settings, "arkham_api_usd_gte", 0.0)
    monkeypatch.setattr(settings, "arkham_api_limit", 10)

    poller = ArkhamApiPoller(cast(httpx.AsyncClient, None))

    assert poller._request_params("binance") == {
        "base": "binance",
        "flow": "all",
        "usdGte": 50000.0,
        "sortKey": "time",
        "sortDir": "desc",
        "limit": 10,
    }


def test_request_params_combined_bases_single_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认多实体合并为一条 base=a,b,c 请求，而非 N 次并行。"""
    monkeypatch.setattr(settings, "arkham_api_bases", "")
    monkeypatch.setattr(settings, "require_binance_listing", False)
    monkeypatch.setattr(settings, "arkham_api_usd_gte", 0.0)
    monkeypatch.setattr(settings, "min_usd_value", 1_000_000.0)
    monkeypatch.setattr(settings, "arkham_api_limit", 50)

    poller = ArkhamApiPoller(cast(httpx.AsyncClient, None))
    params = poller._request_params()  # no base → combined

    base = str(params["base"])
    assert "," in base
    parts = base.split(",")
    assert "binance" in parts
    assert "coinbase" in parts
    assert len(parts) >= 4
    assert params["usdGte"] == 1_000_000.0
    assert params["sortKey"] == "time"
    assert params["sortDir"] == "desc"


def test_request_params_custom_bases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "arkham_api_bases", "binance, okx")
    monkeypatch.setattr(settings, "arkham_api_usd_gte", 250000.0)
    monkeypatch.setattr(settings, "min_usd_value", 1.0)
    monkeypatch.setattr(settings, "arkham_api_limit", 20)

    poller = ArkhamApiPoller(cast(httpx.AsyncClient, None))
    params = poller._request_params()
    assert params["base"] == "binance,okx"
    assert params["usdGte"] == 250000.0


def test_large_mainstream_transfer_is_allowed_when_full_mode_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "filter_mainstream_tokens", False)
    monkeypatch.setattr(settings, "require_binance_listing", False)
    monkeypatch.setattr(settings, "min_usd_value", 1_000_000.0)

    assert should_alert("ETH", 1_000_001) == (True, "")
    assert should_alert("USDT", 999_999) == (False, "below_threshold")


# ---------------------------------------------------------------------------
# Mocked HTTP: real ArkhamApiPoller methods + prime/dedup
# ---------------------------------------------------------------------------


def _mock_transport(body: dict[str, Any], status: int = 200) -> httpx.MockTransport:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        # attach count for assertions via response header
        return httpx.Response(
            status,
            json=body,
            request=request,
            headers={"X-Call-Count": str(call_count["n"])},
        )

    transport = httpx.MockTransport(handler)
    # stash counter on transport for tests
    transport.call_count = call_count  # type: ignore[attr-defined]
    return transport


def test_fetch_all_single_request_not_n_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多实体配置时 _fetch_all 只发 1 次 /transfers，不并行 N 次。"""
    monkeypatch.setattr(settings, "arkham_api_key", "test-key")
    monkeypatch.setattr(settings, "arkham_api_base_url", "https://api.arkm.com")
    monkeypatch.setattr(
        settings, "arkham_api_bases", "binance,coinbase,okx,bybit,kraken,bitfinex"
    )
    monkeypatch.setattr(settings, "arkham_api_usd_gte", 0.0)
    monkeypatch.setattr(settings, "min_usd_value", 1_000_000.0)
    monkeypatch.setattr(settings, "arkham_api_limit", 50)

    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={"count": 1, "transfers": [OFFICIAL_TRANSFER_BINANCE_HOT]},
        )

    async def _run() -> list[dict]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            poller = ArkhamApiPoller(client)
            return await poller._fetch_all()

    transfers = asyncio.run(_run())

    assert len(requests_seen) == 1, f"expected 1 request, got {len(requests_seen)}"
    req = requests_seen[0]
    assert req.method == "GET"
    assert "/transfers" in str(req.url)
    assert req.headers.get("API-Key") == "test-key"
    # combined base in query
    assert "binance" in str(req.url)
    assert "coinbase" in str(req.url) or "base=binance" in str(req.url)
    assert len(transfers) == 1
    assert transfers[0]["fromAddressLabel"] == "Binance"
    assert transfers[0]["tokenSymbol"] == "USDC"


def test_prime_then_dedup_second_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首轮 prime 不推送；相同 transfer 第二轮不重新 dispatch。"""
    reset_dedup_state()
    monkeypatch.setattr(settings, "arkham_api_key", "test-key")
    monkeypatch.setattr(settings, "arkham_api_base_url", "https://api.arkm.com")
    monkeypatch.setattr(settings, "arkham_api_bases", "binance")
    monkeypatch.setattr(settings, "filter_mainstream_tokens", False)
    monkeypatch.setattr(settings, "require_binance_listing", False)
    monkeypatch.setattr(settings, "min_usd_value", 1_000_000.0)
    monkeypatch.setattr(settings, "telegram_bot_token", "tok")
    monkeypatch.setattr(settings, "telegram_chat_id", "1")

    body = {"count": 1, "transfers": [OFFICIAL_TRANSFER_BINANCE_HOT]}
    process_calls: list[dict] = []

    async def fake_process(payload: dict, http_client: Any, source: str = "unknown") -> dict:
        process_calls.append(payload)
        return {"status": "sent", "token": payload.get("tokenSymbol"), "usd_value": 1}

    monkeypatch.setattr("app.arkham_api_polling.process_payload", fake_process)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async def _run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            poller = ArkhamApiPoller(client)

            # cycle 1: prime
            t1 = await poller._fetch_all()
            await poller._dispatch(t1)
            assert poller._primed is True
            assert process_calls == []

            # cycle 2: same id → no re-dispatch
            t2 = await poller._fetch_all()
            await poller._dispatch(t2)
            assert process_calls == []

            # cycle 3: new transfer id → dispatch once
            new_item = dict(OFFICIAL_TRANSFER_BINANCE_HOT)
            new_item["id"] = "brand-new-transfer-id-999"
            new_item["transactionHash"] = "0xnewhash"
            new_item["tokenSymbol"] = "PEPE"
            new_item["historicalUSD"] = 2_000_000

            def handler2(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={"count": 1, "transfers": [new_item]})

            client._transport = httpx.MockTransport(handler2)
            t3 = await poller._fetch_all()
            await poller._dispatch(t3)
            assert len(process_calls) == 1
            assert process_calls[0]["tokenSymbol"] == "PEPE"
            assert process_calls[0]["fromAddressLabel"] == "Binance"

    asyncio.run(_run())


def test_http_error_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "arkham_api_key", "test-key")
    monkeypatch.setattr(settings, "arkham_api_base_url", "https://api.arkm.com")
    monkeypatch.setattr(settings, "arkham_api_bases", "binance")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    async def _run() -> list[dict]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            poller = ArkhamApiPoller(client)
            return await poller._fetch_all()

    transfers = asyncio.run(_run())
    assert transfers == []


def test_dedup_key_uses_official_id() -> None:
    t = _parse_transfer(OFFICIAL_TRANSFER_BINANCE_HOT)
    assert t is not None
    key = _dedup_key(t)
    assert key.startswith("id:")
    assert OFFICIAL_TRANSFER_BINANCE_HOT["id"] in key


def test_main_registers_api_poller_when_key_set() -> None:
    """静态：main lifespan 在 ARKHAM_API_KEY 时注册 arkham_api，不依赖浏览器。"""
    from pathlib import Path

    main_src = Path(__file__).resolve().parents[1] / "app" / "main.py"
    text = main_src.read_text(encoding="utf-8")
    assert "arkham_api_key" in text
    assert "ArkhamApiPoller" in text
    assert 'sources.append("arkham_api")' in text
    # browser path is optional, gated on arkham_polling_enabled
    assert "arkham_polling_enabled" in text
