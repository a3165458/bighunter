"""WhaleScope — Arkham 官方 REST API 轮询（无需浏览器 / 页面登录）。

使用 ARKHAM_API_KEY（Header: API-Key）调用:
  GET {base}/transfers?base=...&usdGte=...&sortKey=time&sortDir=desc&limit=...

官方 /transfers 限速 1 req/s；本模块用单次请求 + 逗号分隔 base 实体，
避免并行多 base 打爆限速。

API Key 需在 https://arkm.com/api 申请（与平台账号登录态无关，一次配置长期可用）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Optional

import httpx

from app.alerts import process_payload
from app.config import settings
from app import poller_health

logger = logging.getLogger("whalescope.arkham_api")

_sent_keys: set[str] = set()
_MAX_DEDUP = 10_000

# 已知 CEX depositServiceID → 展示名（direction 逻辑依赖这些名字）
_DEPOSIT_SERVICE_NAMES: dict[str, str] = {
    "binance": "Binance",
    "coinbase": "Coinbase",
    "okx": "OKX",
    "bybit": "Bybit",
    "kraken": "Kraken",
    "bitfinex": "Bitfinex",
    "kucoin": "KuCoin",
    "huobi": "Huobi",
    "htx": "HTX",
    "gate": "Gate",
    "mexc": "MEXC",
    "bitget": "Bitget",
    "upbit": "Upbit",
}


def _title_entity_id(entity_id: str) -> str:
    """将 depositServiceID / entity id 转成可读名。"""
    key = (entity_id or "").strip().lower()
    if not key:
        return ""
    if key in _DEPOSIT_SERVICE_NAMES:
        return _DEPOSIT_SERVICE_NAMES[key]
    return key.replace("_", " ").replace("-", " ").title()


def _nested_name(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    for k in ("name", "label", "id"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:100]
    return ""


def _entity_label(obj: Any) -> str:
    """从 EnrichedAddress 取展示标签。

    优先顺序（官方示例：Hot Wallet 标签 + Binance 实体并存）：
      1. arkhamEntity.name（CEX / 机构名，方向判断需要）
      2. depositServiceID（充值服务，如 binance → Binance）
      3. arkhamLabel.name（Hot Wallet / Deposit 等钱包角色）
      4. 其它 name / label / address 兜底
    """
    if obj is None:
        return "Unknown"
    if isinstance(obj, str):
        return obj[:100] or "Unknown"
    if not isinstance(obj, dict):
        return "Unknown"

    # 1) 实体名（Binance 优先于 Hot Wallet）
    ent = obj.get("arkhamEntity") or obj.get("entity")
    name = _nested_name(ent)
    if name:
        return name

    # 2) 充值服务 ID（官方示例：仅有 depositServiceID + arkhamLabel "Binance Deposit"）
    dep = obj.get("depositServiceID") or obj.get("depositServiceId")
    if isinstance(dep, str) and dep.strip():
        return _title_entity_id(dep) or dep.strip()[:100]

    # 3) 钱包标签
    lab = obj.get("arkhamLabel") or obj.get("label")
    name = _nested_name(lab)
    if name:
        return name

    # 4) 顶层 name / label / address
    for k in ("name", "label", "address", "addr"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:100]
        if isinstance(v, dict):
            n = _nested_name(v)
            if n:
                return n

    return "Unknown"


def _address_of(obj: Any) -> str:
    if isinstance(obj, str) and obj:
        return obj
    if isinstance(obj, dict):
        for k in ("address", "addr"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
    return ""


def _parse_transfer(item: dict) -> Optional[dict]:
    """将官方 EnrichedTransfers 条目规范为共享告警管线字段。"""
    # token
    token: Any = item.get("tokenSymbol") or item.get("symbol")
    if token is None:
        raw_token = item.get("token")
        if isinstance(raw_token, dict):
            token = raw_token.get("symbol") or raw_token.get("name")
        else:
            token = raw_token
    if isinstance(token, dict):
        token = token.get("symbol") or token.get("name")
    if isinstance(token, str) and token.strip():
        token = token.strip().upper()
    else:
        token = "UNKNOWN"

    # usd（官方字段 historicalUSD）
    usd = item.get("usdValue") or item.get("historicalUSD") or item.get("usd") or 0
    try:
        if isinstance(usd, dict):
            usd = usd.get("value") or usd.get("usd") or 0
        usd_value = float(usd or 0)
    except (TypeError, ValueError):
        return None
    if usd_value <= 0:
        return None

    # unit（官方字段 unitValue）
    unit = item.get("unitAmount") or item.get("unitValue") or item.get("value") or 0
    try:
        if isinstance(unit, dict):
            unit = unit.get("value") or 0
        unit_amount = float(unit or 0)
    except (TypeError, ValueError):
        unit_amount = 0.0

    from_obj = item.get("fromAddress") or item.get("from") or item.get("sender")
    to_obj = item.get("toAddress") or item.get("to") or item.get("receiver")

    # 显式 label 字段优先（webhook 形态），否则从嵌套地址对象解析
    from_label = item.get("fromAddressLabel")
    if not from_label:
        from_label = _entity_label(from_obj)
    to_label = item.get("toAddressLabel")
    if not to_label:
        to_label = _entity_label(to_obj)

    from_addr = item.get("fromAddress") if isinstance(item.get("fromAddress"), str) else _address_of(from_obj)
    to_addr = item.get("toAddress") if isinstance(item.get("toAddress"), str) else _address_of(to_obj)

    chain = (
        item.get("blockchain")
        or item.get("chain")
        or item.get("network")
        or "ethereum"
    )
    if isinstance(chain, dict):
        chain = chain.get("name") or chain.get("id") or "ethereum"
    chain = str(chain).lower()

    txid = item.get("txid") or item.get("transactionHash") or item.get("hash") or ""
    transfer_id = item.get("id") or ""
    arkham_url = item.get("arkhamUrl") or ""
    if not arkham_url and txid:
        arkham_url = f"https://arkm.com/tx/{txid}"

    return {
        "tokenSymbol": token,
        "usdValue": usd_value,
        "fromAddressLabel": str(from_label)[:100] if from_label else "Unknown",
        "toAddressLabel": str(to_label)[:100] if to_label else "Unknown",
        "fromAddress": from_addr if isinstance(from_addr, str) else "",
        "toAddress": to_addr if isinstance(to_addr, str) else "",
        "blockchain": chain,
        "arkhamUrl": arkham_url,
        "unitAmount": unit_amount,
        "_txid": txid,
        "_id": str(transfer_id) if transfer_id else "",
    }


def _dedup_key(t: dict) -> str:
    # 官方 transfer id 最稳（同一 tx 可能有多笔 transfer）
    tid = (t.get("_id") or "").strip()
    if tid:
        return f"id:{tid}"
    url = (t.get("arkhamUrl") or "").strip()
    if url:
        return url
    txid = (t.get("_txid") or "").strip()
    if txid:
        # 同一 hash 可能多笔；拼上金额/地址降低碰撞
        return f"tx:{txid}|{t.get('usdValue','')}|{t.get('fromAddress','')}|{t.get('toAddress','')}"
    raw = "|".join(
        [
            str(t.get("tokenSymbol", "")),
            str(t.get("usdValue", "")),
            str(t.get("fromAddressLabel", "")),
            str(t.get("toAddressLabel", "")),
            str(t.get("unitAmount", "")),
        ]
    )
    return hashlib.md5(raw.encode()).hexdigest()


def _remember(key: str) -> bool:
    """记录 key；若已存在返回 False（重复），否则 True（新）。"""
    if key in _sent_keys:
        return False
    if len(_sent_keys) >= _MAX_DEDUP:
        _sent_keys.clear()
    _sent_keys.add(key)
    return True


def reset_dedup_state() -> None:
    """测试用：清空去重集合。"""
    _sent_keys.clear()


def _extract_list(payload: Any) -> list[dict]:
    """从 EnrichedTransfers 信封或裸 list 中取出 transfer dict 列表。"""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in ("transfers", "data", "results", "items"):
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                inner = v.get("transfers") or v.get("items")
                if isinstance(inner, list):
                    return [x for x in inner if isinstance(x, dict)]
    return []


class ArkhamApiPoller:
    """使用官方 API Key 轮询 transfers（单请求 / 周期，尊重 1 rps）。"""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._primed = False

    async def start(self) -> None:
        if not settings.arkham_api_key:
            logger.info("Arkham API polling disabled (no ARKHAM_API_KEY)")
            return
        logger.info(
            "Starting Arkham API polling | base=%s | interval=%ds | entities=%s",
            settings.arkham_api_base_url,
            settings.arkham_api_poll_interval_seconds,
            settings.arkham_api_bases or "(default exchanges)",
        )
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="arkham-api-poller")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Arkham API polling stopped")

    def _bases(self) -> list[str]:
        raw = (settings.arkham_api_bases or "").strip()
        if raw:
            return [b.strip() for b in raw.split(",") if b.strip()]
        if settings.require_binance_listing:
            # 先取全局大额转账，再由共享管线筛选币安现货/合约币种。
            # 按交易所实体查询会漏掉这些币种在任意钱包间的转账。
            return ["all"]
        # 默认盯几家主流 CEX 的大额进出，覆盖冷门币充提场景
        return [
            "binance",
            "coinbase",
            "okx",
            "bybit",
            "kraken",
            "bitfinex",
        ]

    def _usd_gte(self) -> float:
        if settings.arkham_api_usd_gte > 0:
            return settings.arkham_api_usd_gte
        return settings.min_usd_value

    def _request_params(self, base: str | None = None) -> dict[str, object]:
        """构建 /transfers 查询参数。

        base 支持逗号分隔多实体（官方 array → 单 query 串），
        默认合并 _bases() 为一次请求，避免 N 次并行打爆 1 rps。
        """
        if base is None:
            bases = self._bases()
            base = ",".join(bases)
        return {
            "base": base,
            "flow": "all",
            "usdGte": self._usd_gte(),
            "sortKey": "time",
            "sortDir": "desc",
            "limit": settings.arkham_api_limit,
        }

    async def _fetch_transfers(self) -> list[dict]:
        """单次 GET /transfers（合并 base 列表），解析为归一化 transfer dict。"""
        params = self._request_params()
        url = f"{settings.arkham_api_base_url.rstrip('/')}/transfers"
        try:
            resp = await self._http.get(
                url,
                params=params,
                headers={
                    "API-Key": settings.arkham_api_key,
                    "Accept": "application/json",
                    "User-Agent": "WhaleScope/2.0",
                },
                timeout=30.0,
            )
        except httpx.RequestError as exc:
            logger.warning("Arkham transfer request error: %s", exc)
            return []
        except Exception as exc:
            logger.warning("Arkham transfer request failed: %s", exc)
            return []

        if resp.status_code != 200:
            logger.warning(
                "Arkham transfer HTTP %s: %s",
                resp.status_code,
                resp.text[:200],
            )
            return []

        try:
            payload = resp.json()
        except ValueError:
            logger.warning("Arkham API non-JSON response")
            return []

        items = _extract_list(payload)
        transfers: list[dict] = []
        for item in items:
            t = _parse_transfer(item)
            if t:
                transfers.append(t)
        return transfers

    # 兼容旧名：单 base 拉取（测试 / 调试）；生产路径走 _fetch_transfers
    async def _fetch_base(self, base: str) -> list[dict]:
        params = self._request_params(base=base)
        url = f"{settings.arkham_api_base_url.rstrip('/')}/transfers"
        try:
            resp = await self._http.get(
                url,
                params=params,
                headers={
                    "API-Key": settings.arkham_api_key,
                    "Accept": "application/json",
                    "User-Agent": "WhaleScope/2.0",
                },
                timeout=30.0,
            )
        except httpx.RequestError as exc:
            logger.warning("Arkham transfer request error base=%s: %s", base, exc)
            return []
        except Exception as exc:
            logger.warning("Arkham transfer request failed base=%s: %s", base, exc)
            return []

        if resp.status_code != 200:
            logger.warning(
                "Arkham transfer HTTP %s base=%s: %s",
                resp.status_code,
                base,
                resp.text[:200],
            )
            return []

        try:
            payload = resp.json()
        except ValueError:
            logger.warning("Arkham API non-JSON base=%s", base)
            return []

        items = _extract_list(payload)
        transfers: list[dict] = []
        for item in items:
            t = _parse_transfer(item)
            if t:
                transfers.append(t)
        return transfers

    async def _fetch_all(self) -> list[dict]:
        """每周期最多 1 次 /transfers 请求（合并 base），符合 1 rps。"""
        return await self._fetch_transfers()

    async def _dispatch(self, transfers: list[dict]) -> None:
        if not self._primed:
            if not transfers:
                return
            for t in transfers:
                _remember(_dedup_key(t))
            self._primed = True
            logger.info(
                "Arkham API first cycle: primed %d transfer(s), no push",
                len(transfers),
            )
            return

        sent = 0
        for t in transfers:
            if not _remember(_dedup_key(t)):
                continue
            result = await process_payload(t, self._http, source="arkham_api")
            if result.get("status") == "sent":
                sent += 1
        if sent:
            logger.info("Arkham API cycle: sent %d new alert(s)", sent)

    async def _loop(self) -> None:
        while self._running:
            try:
                transfers = await self._fetch_all()
                logger.info("Arkham API extracted %d transfer(s)", len(transfers))
                poller_health.mark_success("arkham_api", len(transfers))
                await self._dispatch(transfers)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                poller_health.mark_error("arkham_api", str(exc))
                logger.error("Arkham API poll error: %s", exc, exc_info=True)
            try:
                await asyncio.sleep(settings.arkham_api_poll_interval_seconds)
            except asyncio.CancelledError:
                break
