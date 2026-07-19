"""WhaleScope — Arkham transfers/filter 页面轮询模块（Playwright）。

通过真实浏览器定期抓取 Arkham Intelligence 的 transfers 过滤页面，
解析最新转账记录，复用 app.alerts 的共享过滤/发送逻辑，
并做内存级去重，避免重复推送。

设计原则
---------
* 多策略 DOM 解析 + 正则文本兜底，尽量健壮地提取字段。
* 抓取失败时明确记录日志并在下一轮重试，不崩溃整个进程。
* 不重复任何过滤/格式化逻辑，均调用 app.alerts.process_payload。
* storage_state 支持保留 Arkham 登录态（见 README）。
"""

import asyncio
import hashlib
import logging
import os
import re
from typing import Optional

import httpx

from app.config import settings
from app.alerts import process_payload

logger = logging.getLogger("whalescope.polling")

# ---------------------------------------------------------------------------
# 内存去重
# ---------------------------------------------------------------------------

_sent_keys: set[str] = set()
_MAX_DEDUP_SIZE = 10_000  # 防止无限增长；满后整体清空（简单策略）


def _dedup_key(transfer: dict) -> str:
    """生成转账记录的去重 key。优先使用 arkhamUrl，否则字段哈希。"""
    url = (transfer.get("arkhamUrl") or "").strip()
    if url:
        return url
    raw = "|".join([
        str(transfer.get("tokenSymbol", "")),
        str(transfer.get("usdValue", "")),
        str(transfer.get("fromAddressLabel", "")),
        str(transfer.get("toAddressLabel", "")),
        str(transfer.get("unitAmount", "")),
        str(transfer.get("blockchain", "")),
    ])
    return hashlib.md5(raw.encode()).hexdigest()


def _is_new_transfer(transfer: dict) -> bool:
    """返回 True 表示该记录是新的（首次出现）并将其加入已见集合。"""
    key = _dedup_key(transfer)
    if key in _sent_keys:
        return False
    if len(_sent_keys) >= _MAX_DEDUP_SIZE:
        logger.info("Dedup set full (%d), clearing", _MAX_DEDUP_SIZE)
        _sent_keys.clear()
    _sent_keys.add(key)
    return True


# ---------------------------------------------------------------------------
# 正则解析工具
# ---------------------------------------------------------------------------

# $1,234,567 / $1.23M / $500K
_USD_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*([KMBkmb]?)", re.IGNORECASE)
# 1,234 TOKEN / 1.23M TOKEN
_AMOUNT_RE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*([KMBkmb])?\s+([A-Z]{2,10})\b",
    re.IGNORECASE,
)
# Ethereum address
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")
# Chain names
_CHAIN_RE = re.compile(
    r"\b(ethereum|bitcoin|bsc|binance|arbitrum|polygon|base|optimism|avalanche|solana|tron)\b",
    re.IGNORECASE,
)
_MULTIPLIERS = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def _parse_usd_text(text: str) -> float:
    """从文本中提取 USD 数值，支持 K/M/B 后缀。"""
    m = _USD_RE.search(text)
    if not m:
        return 0.0
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0.0
    return num * _MULTIPLIERS.get((m.group(2) or "").upper(), 1)


def _parse_text_to_transfer(text: str) -> Optional[dict]:
    """
    从一段文本（单行或多行）用正则提取转账字段，返回 normalized dict。
    这是最后兜底策略，字段完整性不保证。
    """
    text = text.strip()
    if not text:
        return None

    usd_value = _parse_usd_text(text)
    if usd_value <= 0:
        return None  # 没有金额，无法构建有意义的告警

    # Token + 数量
    token_symbol = "UNKNOWN"
    unit_amount = 0.0
    for m in _AMOUNT_RE.finditer(text):
        sym = m.group(3).upper()
        if len(sym) >= 2:
            token_symbol = sym
            try:
                unit_amount = float(m.group(1).replace(",", "")) * \
                              _MULTIPLIERS.get((m.group(2) or "").upper(), 1)
            except ValueError:
                pass
            break

    # 地址
    addresses = _ADDR_RE.findall(text)
    from_address = addresses[0] if addresses else ""
    to_address = addresses[1] if len(addresses) > 1 else ""

    # 链
    blockchain = "ethereum"
    cm = _CHAIN_RE.search(text)
    if cm:
        blockchain = cm.group(1).lower()

    # 方向标签（启发式：找 → 或 from/to）
    from_label, to_label = "Unknown", "Unknown"
    for pat in [
        r"(.+?)\s*[→\->]+\s*(.+?)(?:\s+\$|\s*$)",
        r"from[:\s]+(.+?)\s+to[:\s]+(.+?)(?:\s+\$|\s*$)",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            from_label = m.group(1).strip()[:100]
            to_label = m.group(2).strip()[:100]
            break

    return {
        "tokenSymbol": token_symbol,
        "usdValue": usd_value,
        "fromAddressLabel": from_label,
        "toAddressLabel": to_label,
        "fromAddress": from_address,
        "toAddress": to_address,
        "blockchain": blockchain,
        "arkhamUrl": "",
        "unitAmount": unit_amount,
    }


# ---------------------------------------------------------------------------
# DOM 解析策略
# ---------------------------------------------------------------------------

async def _find_arkham_link(element) -> str:
    """在元素内查找 Arkham 交易链接。"""
    try:
        links = await element.query_selector_all(
            "a[href*='/tx/'], a[href*='/address/'], a[href*='arkham']"
        )
        if links:
            href = await links[0].get_attribute("href")
            if href:
                if href.startswith("/"):
                    return f"https://platform.arkhamintelligence.com{href}"
                return href
    except Exception:
        pass
    return ""


def _normalize_cells(texts: list[str], href: str = "") -> Optional[dict]:
    """将单元格文本列表合并后解析。"""
    full = " | ".join(t.strip() for t in texts if t.strip())
    result = _parse_text_to_transfer(full)
    if result and href:
        result["arkhamUrl"] = href
    return result


async def _parse_row_element(row, cell_selector: str) -> Optional[dict]:
    """通用行解析：取所有单元格文本 + 链接。"""
    try:
        cells = await row.query_selector_all(cell_selector)
        if len(cells) < 3:
            return None
        texts = [await c.inner_text() for c in cells]
        href = await _find_arkham_link(row)
        return _normalize_cells(texts, href)
    except Exception as exc:
        logger.debug("Row parse error: %s", exc)
        return None


async def _extract_transfers_from_page(
    page, allow_text_fallback: bool = False
) -> list[dict]:
    """
    多策略从 Arkham 页面提取转账记录。

    Strategy 1 — <table tbody tr> + <td>
    Strategy 2 — role="row" + role="cell"/"gridcell"
    Strategy 3 — data-testid 含 transfer/transaction/row
    Strategy 4 — 全页面文本正则兜底（仅 allow_text_fallback=True 时启用；
                 页面渲染未完成时会把价格/市值等无关文本切成假转账，
                 生产轮询默认关闭以防误报）
    """
    transfers: list[dict] = []

    # --- Strategy 1: 标准 HTML table ---
    try:
        rows = await page.query_selector_all("table tbody tr")
        if rows:
            logger.debug("S1: %d table rows", len(rows))
            for row in rows[:50]:
                t = await _parse_row_element(row, "td")
                if t:
                    transfers.append(t)
            if transfers:
                return transfers
    except Exception as exc:
        logger.debug("S1 failed: %s", exc)

    # --- Strategy 2: ARIA role="row" ---
    try:
        rows = await page.query_selector_all('[role="row"]')
        if len(rows) > 1:  # 第 0 行通常是表头
            logger.debug("S2: %d role=row elements", len(rows))
            for row in rows[1:51]:
                t = await _parse_row_element(
                    row, '[role="cell"], [role="gridcell"], td, div[class*="cell"]'
                )
                if t:
                    transfers.append(t)
            if transfers:
                return transfers
    except Exception as exc:
        logger.debug("S2 failed: %s", exc)

    # --- Strategy 3: data-testid 启发式 ---
    try:
        items = await page.query_selector_all(
            '[data-testid*="transfer"],[data-testid*="transaction"],[data-testid*="row"]'
        )
        if items:
            logger.debug("S3: %d data-testid items", len(items))
            for item in items[:50]:
                try:
                    text = await item.inner_text()
                    href = await _find_arkham_link(item)
                    t = _parse_text_to_transfer(text)
                    if t:
                        if href:
                            t["arkhamUrl"] = href
                        transfers.append(t)
                except Exception:
                    pass
            if transfers:
                return transfers
    except Exception as exc:
        logger.debug("S3 failed: %s", exc)

    # --- Strategy 4: 全页文本正则兜底 ---
    if not allow_text_fallback:
        logger.warning(
            "DOM extraction strategies found no transfers — skipping this cycle "
            "(text fallback disabled, set ARKHAM_TEXT_FALLBACK=true to enable)"
        )
        return []
    try:
        logger.debug("S4: fallback full-page text parsing")
        content = await page.content()
        # 去除 HTML 标签
        plain = re.sub(r"<[^>]+>", " ", content)
        plain = re.sub(r"\s+", " ", plain)
        # 以 $ 符号为分割点逐段解析
        for seg in re.split(r"(?=\$[\d,])", plain)[:200]:
            if len(seg) < 10:
                continue
            t = _parse_text_to_transfer(seg[:600])
            if t:
                transfers.append(t)
        if transfers:
            logger.info("S4: extracted %d transfers via text fallback", len(transfers))
            return transfers
    except Exception as exc:
        logger.debug("S4 failed: %s", exc)

    logger.warning(
        "All extraction strategies failed — page may not be loaded correctly "
        "or the DOM structure has changed"
    )
    return []


# ---------------------------------------------------------------------------
# 模拟 HTML 测试入口（无需真实浏览器）
# ---------------------------------------------------------------------------

MOCK_TABLE_HTML = """
<html><body>
<table>
  <thead><tr><th>Time</th><th>From</th><th>To</th><th>Token</th><th>Amount</th><th>Value</th></tr></thead>
  <tbody>
    <tr>
      <td>2024-01-15 10:23</td>
      <td>Binance Hot Wallet</td>
      <td>Unknown Whale</td>
      <td>PEPE</td>
      <td>5,000,000,000 PEPE</td>
      <td>$125,000</td>
      <td><a href="/tx/0xabc123def456">View</a></td>
    </tr>
    <tr>
      <td>2024-01-15 10:20</td>
      <td>0x1234567890abcdef1234567890abcdef12345678</td>
      <td>OKX Exchange</td>
      <td>SHIB</td>
      <td>10,000,000,000 SHIB</td>
      <td>$88,000</td>
      <td><a href="/tx/0xdef789">View</a></td>
    </tr>
    <tr>
      <td>2024-01-15 10:18</td>
      <td>Coinbase</td>
      <td>0xabcdef1234567890abcdef1234567890abcdef12</td>
      <td>ETH</td>
      <td>50.00 ETH</td>
      <td>$160,000</td>
      <td><a href="/tx/0xeth001">View</a></td>
    </tr>
  </tbody>
</table>
</body></html>
"""


def _parse_mock_html(html: str) -> list[dict]:
    """
    解析模拟 HTML（不依赖 Playwright），用于单元测试路径验证。
    使用正则替代 DOM 查询，验证文本解析逻辑。
    """
    # 提取 <tr> 内容
    row_re = re.compile(r"<tr>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    cell_re = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)
    link_re = re.compile(r'href="(/tx/[^"]+)"')
    tag_re = re.compile(r"<[^>]+>")

    results = []
    for row_m in row_re.finditer(html):
        row_html = row_m.group(1)
        cells = [tag_re.sub("", c.group(1)).strip() for c in cell_re.finditer(row_html)]
        if len(cells) < 3:
            continue
        href_m = link_re.search(row_html)
        href = (
            f"https://platform.arkhamintelligence.com{href_m.group(1)}"
            if href_m else ""
        )
        t = _normalize_cells(cells, href)
        if t:
            results.append(t)
    return results


# ---------------------------------------------------------------------------
# ArkhamPoller — 主轮询类
# ---------------------------------------------------------------------------

class ArkhamPoller:
    """
    封装 Playwright 浏览器生命周期与定时轮询逻辑。
    由 main.py lifespan 负责 start() / stop()。
    """

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http_client = http_client
        self._pw = None       # playwright instance
        self._browser = None
        self._context = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # 首轮只登记页面上已有的记录、不推送，避免每次重启把旧转账重播一遍
        self._primed = False

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not settings.arkham_polling_enabled:
            logger.info("Arkham polling is disabled (ARKHAM_POLLING_ENABLED=false)")
            return
        if not settings.arkham_transfers_url:
            logger.error(
                "ARKHAM_POLLING_ENABLED=true but ARKHAM_TRANSFERS_URL is empty — polling disabled"
            )
            return

        logger.info(
            "Starting Arkham polling | url=%s | interval=%ds | mode=%s",
            settings.arkham_transfers_url,
            settings.arkham_poll_interval_seconds,
            f"cdp({settings.arkham_cdp_url})" if settings.arkham_cdp_url
            else f"launch(headless={settings.arkham_headless})",
        )
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="arkham-poller")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._cleanup_browser()
        logger.info("Arkham polling stopped")

    # ------------------------------------------------------------------
    # 浏览器管理
    # ------------------------------------------------------------------

    async def _init_browser(self) -> None:
        """初始化 Playwright Chromium 实例，支持 storage_state 登录态。"""
        from playwright.async_api import async_playwright  # 延迟导入，避免启动时报错

        if settings.arkham_cdp_url:
            # CDP 模式：挂接外部已登录的 Chrome，不自建浏览器
            logger.info("Connecting to existing Chrome via CDP: %s", settings.arkham_cdp_url)
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.connect_over_cdp(
                settings.arkham_cdp_url
            )
            if not self._browser.contexts:
                raise RuntimeError("CDP browser has no contexts")
            self._context = self._browser.contexts[0]
            return

        logger.info("Initializing Playwright browser")
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=settings.arkham_headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )

        ctx_kwargs: dict = {
            "viewport": {"width": 1280, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        storage_path = settings.arkham_storage_state_path
        if storage_path and os.path.exists(storage_path):
            logger.info("Loading Arkham storage state from %s", storage_path)
            ctx_kwargs["storage_state"] = storage_path
        else:
            logger.debug(
                "No storage state found at %s — proceeding without login",
                storage_path,
            )

        self._context = await self._browser.new_context(**ctx_kwargs)

    async def _cleanup_browser(self) -> None:
        """关闭浏览器资源，容忍错误。CDP 模式下只断开连接，不动外部 Chrome 的页面。"""
        if settings.arkham_cdp_url:
            for obj, name in [(self._browser, "cdp connection")]:
                if obj is not None:
                    try:
                        await obj.close()
                    except Exception as exc:
                        logger.warning("Error closing %s: %s", name, exc)
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
            self._context = None
            self._browser = None
            self._pw = None
            return

        for obj, name in [
            (self._context, "context"),
            (self._browser, "browser"),
            (self._pw, "playwright"),
        ]:
            if obj is not None:
                try:
                    await obj.close()
                except Exception as exc:
                    logger.warning("Error closing %s: %s", name, exc)
        self._context = None
        self._browser = None
        self._pw = None

    # ------------------------------------------------------------------
    # 轮询逻辑
    # ------------------------------------------------------------------

    def _find_cdp_page(self):
        """在外部 Chrome 已打开的标签页中定位 transfers 页面。"""
        target = settings.arkham_transfers_url.split("://", 1)[-1].rstrip("/")
        for page in self._context.pages:
            if target and target in page.url:
                return page
        return None

    async def _dispatch(self, transfers: list[dict]) -> None:
        """去重后经共享管线过滤并发送。首轮只预热去重集，不推送。"""
        if not self._primed:
            if not transfers:
                # 空结果不算预热完成，否则下一轮会把存量记录全当新增推送
                return
            for transfer in transfers:
                _is_new_transfer(transfer)
            self._primed = True
            logger.info(
                "First polling cycle: primed dedup with %d existing transfer(s), "
                "no alerts sent", len(transfers),
            )
            return

        sent_count = 0
        for transfer in transfers:
            if not _is_new_transfer(transfer):
                continue
            result = await process_payload(
                transfer, self._http_client, source="polling"
            )
            if result.get("status") == "sent":
                sent_count += 1

        if sent_count:
            logger.info("Polling cycle: sent %d new alert(s)", sent_count)

    @staticmethod
    async def _is_challenged(page) -> bool:
        """判断页面当前是否停在 Cloudflare 人机验证页。"""
        try:
            title = (await page.title()).lower()
        except Exception:
            return False
        return "moment" in title or "attention" in title or "verification" in title

    async def _poll_once_cdp(self) -> None:
        """CDP 模式单次轮询：驱动外部 Chrome 中已打开的页面刷新并提取。"""
        if self._context is None:
            await self._init_browser()

        try:
            page = self._find_cdp_page()
            if page is None:
                # transfers 标签页丢了（被重定向/手动关闭）：
                # 复用现有标签页导航回去，而不是永久卡死等人工干预
                if not self._context.pages:
                    logger.warning("CDP mode: browser has no open tabs")
                    return
                page = self._context.pages[0]
                if await self._is_challenged(page):
                    logger.warning(
                        "CDP mode: tab is on a Cloudflare challenge — complete "
                        "the verification manually via VNC, polling is paused"
                    )
                    return
                logger.info(
                    "CDP mode: transfers tab lost, re-navigating %s",
                    settings.arkham_transfers_url,
                )
                try:
                    await page.goto(
                        settings.arkham_transfers_url,
                        wait_until="domcontentloaded",
                        timeout=45_000,
                    )
                except Exception as exc:
                    logger.warning("CDP mode: re-navigation failed: %s", exc)
                    return
            else:
                # 挑战页上绝不 reload——否则 VNC 里的人工验证会被反复打断。
                # 只有正常页面才刷新（后台标签页被节流，DOM 不会自己更新；
                # clearance cookie 有效期内刷新不会重新触发验证）
                if await self._is_challenged(page):
                    logger.warning(
                        "CDP mode: transfers tab is on a Cloudflare challenge — "
                        "complete the verification manually via VNC, polling is paused"
                    )
                    return
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=45_000)
                except Exception as exc:
                    logger.warning(
                        "CDP mode: page reload failed (%s), parsing current DOM", exc
                    )

            # 等数据表格真正渲染出多行，避免解析半渲染页面
            try:
                await page.wait_for_function(
                    "document.querySelectorAll("
                    "\"table tbody tr, [role='row']\").length >= 3",
                    timeout=25_000,
                )
            except Exception:
                pass

            if await self._is_challenged(page):
                logger.warning(
                    "CDP mode: Cloudflare challenge appeared after reload — "
                    "complete the verification manually via VNC, polling is paused"
                )
                return

            transfers = await _extract_transfers_from_page(
                page, allow_text_fallback=settings.arkham_text_fallback
            )
            logger.info("Extracted %d transfer(s) from CDP page", len(transfers))
            await self._dispatch(transfers)
        except Exception as exc:
            logger.error("CDP polling error: %s", exc, exc_info=True)
            # 连接可能断开，下次重连
            await self._cleanup_browser()

    async def _poll_once(self) -> None:
        """单次轮询：加载页面 → 提取 → 过滤发送。"""
        if settings.arkham_cdp_url:
            await self._poll_once_cdp()
            return

        if self._context is None:
            await self._init_browser()

        page = await self._context.new_page()
        try:
            logger.debug("Navigating to %s", settings.arkham_transfers_url)
            await page.goto(
                settings.arkham_transfers_url,
                wait_until="networkidle",
                timeout=60_000,
            )

            # 等待列表内容出现（容忍超时）
            try:
                await page.wait_for_selector(
                    "table, [role='row'], [data-testid*='transfer'], [data-testid*='row']",
                    timeout=20_000,
                )
            except Exception:
                logger.warning("Expected selectors not found within timeout, proceeding anyway")

            transfers = await _extract_transfers_from_page(
                page, allow_text_fallback=settings.arkham_text_fallback
            )
            logger.info("Extracted %d transfer(s) from page", len(transfers))
            await self._dispatch(transfers)

        except Exception as exc:
            logger.error("Polling error during page fetch/parse: %s", exc, exc_info=True)
            # 浏览器状态可能损坏，下次重建
            await self._cleanup_browser()
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _poll_loop(self) -> None:
        """主轮询循环，每隔 ARKHAM_POLL_INTERVAL_SECONDS 执行一次。"""
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Unexpected error in poll loop: %s", exc, exc_info=True)

            # 等待下一轮（可被 cancel 打断）
            try:
                await asyncio.sleep(settings.arkham_poll_interval_seconds)
            except asyncio.CancelledError:
                break
