"""回归：Arkham 必须按金额抓取，再筛币安现货与合约币种。"""

from __future__ import annotations
import logging
import asyncio

import httpx

from app import binance_listings
from app.alerts import should_alert

from app.arkham_api_polling import ArkhamApiPoller
from app.arkham_home_polling import (
    build_chrome_command,
    response_matches_transfer_query,
    rewrite_transfer_query_url,
)
from app.config import settings


def test_public_page_ignores_recent_transfer_response_below_threshold() -> None:
    low_value_feed = (
        "https://api.arkm.com/transfers?base=all&flow=all&usdGte=1"
        "&sortKey=time&sortDir=desc&limit=16&offset=0"
    )
    target_feed = low_value_feed.replace("usdGte=1", "usdGte=500000")

    assert not response_matches_transfer_query(low_value_feed, 500_000)
    assert response_matches_transfer_query(target_feed, 500_000)


def test_api_queries_all_transfers_when_filtering_binance_assets(monkeypatch) -> None:
    monkeypatch.setattr(settings, "require_binance_listing", True)
    monkeypatch.setattr(settings, "arkham_api_bases", "")

    poller = ArkhamApiPoller(http_client=None)  # type: ignore[arg-type]

    assert poller._request_params()["base"] == "all"


def test_binance_spot_and_futures_assets_are_all_eligible(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.binance.com":
            symbols = [{"baseAsset": "SPOTONLY", "status": "TRADING"}]
        elif request.url.host == "fapi.binance.com":
            symbols = [{"baseAsset": "USDTMFUT", "status": "TRADING"}]
        else:
            symbols = [{"baseAsset": "COINMFUT", "contractStatus": "TRADING"}]
        return httpx.Response(200, json={"symbols": symbols})

    async def refresh() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await binance_listings.refresh_listings(client)

    asyncio.run(refresh())
    monkeypatch.setattr(settings, "require_binance_listing", True)
    monkeypatch.setattr(settings, "filter_mainstream_tokens", False)
    monkeypatch.setattr(settings, "min_usd_value", 500_000.0)

    assert should_alert("SPOTONLY", 500_000.0) == (True, "")
    assert should_alert("USDTMFUT", 500_000.0) == (True, "")
    assert should_alert("COINMFUT", 500_000.0) == (True, "")
    assert should_alert("NOTLISTED", 500_000.0) == (False, "not_on_binance")


def test_home_chrome_uses_configured_vless_socks_bridge() -> None:
    command = build_chrome_command(
        profile="/tmp/arkham-profile",
        proxy_server="socks5://127.0.0.1:10808",
    )

    assert "--proxy-server=socks5://127.0.0.1:10808" in command


def test_signed_browser_transfer_url_is_rewritten_to_business_threshold() -> None:
    original = (
        "https://api.arkm.com/transfers?base=all&flow=all&usdGte=1"
        "&sortKey=time&sortDir=desc&limit=16&offset=0"
    )

    rewritten = rewrite_transfer_query_url(original, 500_000)

    assert "usdGte=500000" in rewritten
    assert "base=all" in rewritten
    assert "flow=all" in rewritten


def test_http_client_logs_cannot_expose_telegram_bot_token() -> None:
    import app.main  # noqa: F401

    assert logging.getLogger("httpx").level == logging.WARNING
