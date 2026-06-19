"""WhaleScope — 币安上币状态缓存模块。

定期从币安公开 API 拉取现货 & 合约（U本位/币本位）交易对列表，
缓存 base asset 集合，供 alerts 过滤使用。

数据源（均为公开，无需 API Key）：
  现货:    https://api.binance.com/api/v3/exchangeInfo
  U本位:   https://fapi.binance.com/fapi/v1/exchangeInfo
  币本位:  https://dapi.binance.com/dapi/v1/exchangeInfo

缓存策略：
  - 启动时立即拉取一次
  - 之后每隔 refresh_interval 秒自动刷新
  - 任何子源失败不影响其他子源，保留上次成功的数据
  - 缓存为空时 is_listed() 返回 True（fail-open，避免漏报）
"""

import asyncio
import logging
import time

import httpx

logger = logging.getLogger("whalescope.binance")

# ---- 币安公开 API ----
_SPOT_URL = "https://api.binance.com/api/v3/exchangeInfo"
_USDT_M_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
_COIN_M_URL = "https://dapi.binance.com/dapi/v1/exchangeInfo"

# ---- 缓存（模块级单例） ----
_spot_assets: set[str] = set()
_usdtm_assets: set[str] = set()
_coinm_assets: set[str] = set()
_all_assets: set[str] = set()
_last_refresh: float = 0.0
_refresh_task: asyncio.Task | None = None


# --------------------------------------------------------------------------- #
# 解析函数
# --------------------------------------------------------------------------- #

def _extract_spot(symbols: list[dict]) -> set[str]:
    """现货：status == TRADING"""
    return {
        s["baseAsset"].upper()
        for s in symbols
        if s.get("status") == "TRADING" and s.get("baseAsset")
    }


def _extract_usdtm(symbols: list[dict]) -> set[str]:
    """U本位合约：status == TRADING"""
    return {
        s["baseAsset"].upper()
        for s in symbols
        if s.get("status") == "TRADING" and s.get("baseAsset")
    }


def _extract_coinm(symbols: list[dict]) -> set[str]:
    """币本位合约：contractStatus == TRADING"""
    return {
        s["baseAsset"].upper()
        for s in symbols
        if s.get("contractStatus") == "TRADING" and s.get("baseAsset")
    }


# --------------------------------------------------------------------------- #
# 刷新逻辑
# --------------------------------------------------------------------------- #

async def refresh_listings(http_client: httpx.AsyncClient) -> None:
    """拉取三个端点并更新缓存。任一失败保留旧数据。"""
    global _spot_assets, _usdtm_assets, _coinm_assets, _all_assets, _last_refresh

    results = await asyncio.gather(
        http_client.get(_SPOT_URL, timeout=20.0),
        http_client.get(_USDT_M_URL, timeout=20.0),
        http_client.get(_COIN_M_URL, timeout=20.0),
        return_exceptions=True,
    )

    new_spot, new_usdtm, new_coinm = set(), set(), set()

    # 现货
    r = results[0]
    if isinstance(r, httpx.Response) and r.status_code == 200:
        new_spot = _extract_spot(r.json().get("symbols", []))
        logger.info("Binance spot: %d assets", len(new_spot))
    else:
        logger.warning("Spot fetch failed: %r, keeping %d cached", r, len(_spot_assets))
        new_spot = _spot_assets  # 保留旧数据

    # U本位
    r = results[1]
    if isinstance(r, httpx.Response) and r.status_code == 200:
        new_usdtm = _extract_usdtm(r.json().get("symbols", []))
        logger.info("Binance USDT-M: %d assets", len(new_usdtm))
    else:
        logger.warning("USDT-M fetch failed: %r, keeping %d cached", r, len(_usdtm_assets))
        new_usdtm = _usdtm_assets

    # 币本位
    r = results[2]
    if isinstance(r, httpx.Response) and r.status_code == 200:
        new_coinm = _extract_coinm(r.json().get("symbols", []))
        logger.info("Binance COIN-M: %d assets", len(new_coinm))
    else:
        logger.warning("COIN-M fetch failed: %r, keeping %d cached", r, len(_coinm_assets))
        new_coinm = _coinm_assets

    _spot_assets = new_spot
    _usdtm_assets = new_usdtm
    _coinm_assets = new_coinm
    _all_assets = new_spot | new_usdtm | new_coinm
    _last_refresh = time.time()

    logger.info("Binance listings refreshed: %d total unique assets", len(_all_assets))


async def start_refresh_task(
    http_client: httpx.AsyncClient,
    interval: int = 3600,
) -> asyncio.Task:
    """启动后台定时刷新任务，返回 Task 句柄。"""
    global _refresh_task

    async def _loop():
        # 首次立即拉取
        await refresh_listings(http_client)
        while True:
            await asyncio.sleep(interval)
            await refresh_listings(http_client)

    _refresh_task = asyncio.create_task(_loop())
    logger.info("Binance listings refresh task started (interval=%ds)", interval)
    return _refresh_task


async def stop_refresh_task() -> None:
    """停止后台刷新任务。"""
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        _refresh_task.cancel()
        try:
            await _refresh_task
        except asyncio.CancelledError:
            pass
    _refresh_task = None


# --------------------------------------------------------------------------- #
# 查询接口
# --------------------------------------------------------------------------- #

def is_listed(symbol: str) -> bool:
    """
    判断代币是否在币安上架（现货或合约任一）。

    缓存为空时 fail-open 返回 True，避免因 API 故障漏报。
    """
    if not _all_assets:
        return True  # fail-open
    return (symbol or "").upper() in _all_assets


def listing_detail(symbol: str) -> dict:
    """返回代币在币安的详细上架信息。"""
    s = (symbol or "").upper()
    return {
        "listed": s in _all_assets if _all_assets else True,
        "spot": s in _spot_assets,
        "usdt_m": s in _usdtm_assets,
        "coin_m": s in _coinm_assets,
    }


def get_cache_info() -> dict:
    """返回缓存状态摘要。"""
    return {
        "total": len(_all_assets),
        "spot": len(_spot_assets),
        "usdt_m": len(_usdtm_assets),
        "coin_m": len(_coinm_assets),
        "last_refresh": _last_refresh,
    }
