"""回归：Arkham 必须按金额抓取，再筛币安现货与合约币种。"""

from __future__ import annotations

from app.arkham_api_polling import ArkhamApiPoller
from app.arkham_home_polling import response_matches_transfer_query
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
