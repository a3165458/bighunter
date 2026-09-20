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
from app.arkham_api_polling import _extract_list as _extract_api_list
from app.arkham_api_polling import _parse_transfer as _parse_api_transfer

logger = logging.getLogger("whalescope.polling")


def _parse_proxy(raw: str) -> dict:
    """把 ARKHAM_PROXY_SERVER 转成 Playwright proxy 字典。

    支持:
      http://host:port
      http://user:pass@host:port
      socks5://user:pass@host:port
    """
    from urllib.parse import urlparse

    raw = (raw or "").strip()
    if not raw:
        return {}
    if "://" not in raw:
        raw = "http://" + raw
    u = urlparse(raw)
    if not u.hostname:
        return {"server": raw}
    scheme = u.scheme or "http"
    port = f":{u.port}" if u.port else ""
    server = f"{scheme}://{u.hostname}{port}"
    conf: dict = {"server": server}
    if u.username:
        conf["username"] = u.username
    if u.password:
        conf["password"] = u.password
    return conf


# ---------------------------------------------------------------------------
# Cloudflare 人机验证自动通关（DOM 层，无需 X 显示器）
# ---------------------------------------------------------------------------

# Turnstile 复选框的可能选择器（challenges.cloudflare.com 框架内）
_CF_CHECKBOX_SELECTORS = (
    "input[type=checkbox]",
    ".ctp-checkbox-label",
    "label[for=challenge-check]",
    "input#challenge-check",
)


async def _cf_frames(page) -> list:
    """返回挑战相关 frame（含嵌套 Turnstile 子框架）。"""
    return [
        f
        for f in page.frames
        if "challenges.cloudflare.com" in (f.url or "").lower()
    ]


async def click_cf_checkbox(page) -> bool:
    """DOM 层自动勾选 Cloudflare Turnstile 复选框（不依赖 X 显示器）。

    适用于 CDP 挂接外部 Chrome / 有头浏览器；对「勾选后自动放行」的
    托管挑战（managed challenge）有效，无需人工。返回是否点击成功。
    """
    clicked = False
    for frame in _cf_frames(page) or [page.main_frame]:
        for sel in _CF_CHECKBOX_SELECTORS:
            try:
                el = await frame.query_selector(sel)
            except Exception:
                continue
            if el is None:
                continue
            try:
                box = await el.bounding_box()
            except Exception:
                box = None
            if box and box["width"] > 0 and box["height"] > 0:
                # 用全局鼠标事件命中 iframe 内元素（跨域也生效）
                await page.mouse.click(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
                clicked = True
                logger.info("Auto-clicked CF checkbox in %s", frame.url[:90])
            else:
                try:
                    await el.click(timeout=1000)
                    clicked = True
                except Exception:
                    pass
    return clicked


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


def _parse_transfer_response(payload: object) -> list[dict]:
    transfers: list[dict] = []
    for item in _extract_api_list(payload):
        transfer = _parse_api_transfer(item)
        if transfer:
            transfers.append(transfer)
    return transfers


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
        self._page = None     # 持久页面：避免每轮新开标签触发 Cloudflare
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # 首轮只登记页面上已有的记录、不推送，避免每次重启把旧转账重播一遍
        self._primed = False
        # CDP 模式：缓存上次成功提取数据的页面。arkm.com 加载 /transfers 后
        # URL 会归一到 /（首页即实时转账流），按 URL 匹配每轮都会失配并
        # 触发整页导航，徒增 Cloudflare 挑战风险
        self._cdp_page = None
        self._response_tasks: set[asyncio.Task] = set()
        self._network_transfers: list[dict] = []
        # 连续 Cloudflare 失败计数：机房 IP 上公开页通常过不了，自动降频避免空转
        self._cf_fail_streak = 0
        self._cf_backoff_until = 0.0

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not settings.arkham_polling_enabled:
            logger.info("Arkham polling is disabled (ARKHAM_POLLING_ENABLED=false)")
            return
        logger.info(
            "Starting Arkham public-page polling (no API key / no account login) | "
            "url=%s | interval=%ds | mode=%s | anonymous=%s",
            settings.arkham_transfers_url,
            settings.arkham_poll_interval_seconds,
            f"cdp({settings.arkham_cdp_url})" if settings.arkham_cdp_url
            else f"launch(headless={settings.arkham_headless})",
            settings.arkham_anonymous_mode,
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
        await self._save_storage_state()
        await self._cleanup_browser()
        logger.info("Arkham polling stopped")

    # ------------------------------------------------------------------
    # 浏览器管理
    # ------------------------------------------------------------------

    async def _init_browser(self) -> None:
        """初始化 Playwright。无需 Arkham 账号登录。

        storage_state 仅用于保存/复用 Cloudflare 等反爬 cookie（不是账户登录态）。
        在能打开 arkm.com 的网络环境（家宽/本机）下，首轮通过后会自动落盘，
        后续轮询更稳。
        """
        from playwright.async_api import async_playwright  # 延迟导入，避免启动时报错

        if settings.arkham_cdp_url:
            # CDP：挂接本机/VNC 里已能打开 arkm 的真实 Chrome（推荐无 Key 方案）
            logger.info("Connecting to existing Chrome via CDP: %s", settings.arkham_cdp_url)
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.connect_over_cdp(
                settings.arkham_cdp_url
            )
            if not self._browser.contexts:
                raise RuntimeError("CDP browser has no contexts")
            self._context = self._browser.contexts[0]
            return

        headless = bool(settings.arkham_headless)
        # 有头模式需要 DISPLAY（服务器用 Xvfb :99 + VNC 人工过 CF）
        if not headless and not os.environ.get("DISPLAY"):
            os.environ["DISPLAY"] = ":99"
            logger.info(
                "ARKHAM_HEADLESS=false 且无 DISPLAY，已设 DISPLAY=:99 "
                "(请用 VNC 连接以便人工完成 Cloudflare)"
            )
        logger.info(
            "Initializing Playwright for public transfers page "
            "(no API key, headless=%s, display=%s)",
            headless,
            os.environ.get("DISPLAY", "(none)"),
        )
        self._pw = await async_playwright().start()

        launch_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ]
        if headless:
            launch_args.append("--disable-gpu")
        # 代理：云服务器过 Cloudflare 的关键路径（住宅/移动出口）
        proxy_cfg = None
        raw_proxy = (settings.arkham_proxy_server or "").strip()
        if raw_proxy:
            proxy_cfg = _parse_proxy(raw_proxy)
            logger.info(
                "Using ARKHAM_PROXY_SERVER for browser (host=%s)",
                proxy_cfg.get("server", raw_proxy)[:80],
            )

        launch_kwargs: dict = {
            "headless": headless,
            "args": launch_args,
        }
        # channel=chrome 若系统有 Google Chrome，指纹更接近真人
        try:
            self._browser = await self._pw.chromium.launch(
                channel="chrome", **launch_kwargs
            )
            logger.info("Launched Google Chrome channel")
        except Exception:
            self._browser = await self._pw.chromium.launch(**launch_kwargs)
            logger.info("Launched bundled Chromium")

        ctx_kwargs: dict = {
            "viewport": {"width": 1400, "height": 900},
            "locale": "en-US",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
        }
        if proxy_cfg:
            ctx_kwargs["proxy"] = proxy_cfg

        # 始终尝试加载 cookie 罐：只是反爬凭证，不是「登录 Arkham 账户」
        storage_path = settings.arkham_storage_state_path
        if storage_path and os.path.exists(storage_path):
            logger.info(
                "Loading browser cookie jar from %s (CF cookies, not account login)",
                storage_path,
            )
            ctx_kwargs["storage_state"] = storage_path
        elif settings.arkham_anonymous_mode:
            logger.info(
                "Anonymous mode: fresh browser context (no cookie jar yet); "
                "will save CF cookies after first successful load if possible"
            )

        self._context = await self._browser.new_context(**ctx_kwargs)
        try:
            await self._context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
        except Exception:
            pass

    async def _save_storage_state(self) -> None:
        """把当前 cookie 罐写回磁盘，供下次启动复用（含 CF clearance）。"""
        if settings.arkham_cdp_url:
            return
        path = (settings.arkham_storage_state_path or "").strip()
        if not path or self._context is None:
            return
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            await self._context.storage_state(path=path)
            logger.debug("Saved browser cookie jar to %s", path)
        except Exception as exc:
            logger.warning("Failed to save storage state: %s", exc)

    async def _cleanup_browser(self) -> None:
        """关闭浏览器资源，容忍错误。CDP 模式下只断开连接，不动外部 Chrome 的页面。"""
        self._page = None
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

        if self._context is not None:
            try:
                await self._context.close()
            except Exception as exc:
                logger.warning("Error closing context: %s", exc)
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:
                logger.warning("Error closing browser: %s", exc)
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception as exc:
                logger.warning("Error stopping playwright: %s", exc)
        self._context = None
        self._browser = None
        self._pw = None

    # ------------------------------------------------------------------
    # 轮询逻辑
    # ------------------------------------------------------------------

    async def _parse_network_response(self, response) -> None:
        """拦截网页公开 JSON（页面本身无登录也能加载的 transfers 接口）。"""
        try:
            if response.status != 200:
                return
            url = (response.url or "").lower()
            content_type = response.headers.get("content-type", "").lower()
            # 优先 transfers 相关；其它 JSON 也尝试解析（字段对不上会得到空列表）
            interesting = (
                "json" in content_type
                or "transfer" in url
                or "/api/" in url
                or "api.arkm" in url
            )
            if not interesting:
                return
            if "json" not in content_type and "transfer" not in url:
                return
            payload = await response.json()
            transfers = _parse_transfer_response(payload)
            if transfers:
                self._network_transfers.extend(transfers)
                logger.info(
                    "Captured %d transfer(s) from public page XHR %s",
                    len(transfers),
                    response.url[:160],
                )
        except Exception as exc:
            logger.debug("Public response parse skipped: %s", exc)

    def _watch_page_responses(self, page) -> None:
        def on_response(response) -> None:
            task = asyncio.create_task(self._parse_network_response(response))
            self._response_tasks.add(task)
            task.add_done_callback(self._response_tasks.discard)

        page.on("response", on_response)

    async def _drain_network_transfers(self) -> list[dict]:
        if self._response_tasks:
            await asyncio.gather(*tuple(self._response_tasks), return_exceptions=True)
        transfers = self._network_transfers
        self._network_transfers = []
        return transfers

    def _find_cdp_page(self):
        """在外部 Chrome 已打开的标签页中定位可用的 Arkham 页面。

        匹配优先级：
        1. URL 精确包含配置的 transfers 路径
        2. 任意 arkm.com / arkhamintelligence.com 页面（非 login / 非 CF 挑战）
        3. 已缓存的 self._cdp_page（由调用方处理 closed 状态）
        """
        target = settings.arkham_transfers_url.split("://", 1)[-1].rstrip("/")
        candidates = list(self._context.pages) if self._context else []

        # 1) 精确路径
        if target:
            for page in candidates:
                if target in (page.url or ""):
                    return page

        # 2) 任意 Arkham 业务页（首页加载后 URL 常归一为 /）
        arkham_hosts = ("arkm.com", "arkhamintelligence.com")
        for page in candidates:
            url = (page.url or "").lower()
            if not any(h in url for h in arkham_hosts):
                continue
            if "/login" in url or "/signup" in url:
                continue
            return page

        return None

    async def _wait_cf_clear(self, page, max_wait: int | None = None) -> bool:
        """若页面处于 Cloudflare 挑战，先尝试 DOM 自动勾选 Turnstile，
        仍被拦截则等待其自动通过或人工完成（VNC）。返回是否已清除。"""
        wait = max_wait if max_wait is not None else settings.arkham_cf_wait_seconds
        if wait <= 0:
            await click_cf_checkbox(page)
            return not await self._is_challenged(page)
        elapsed = 0
        interval = 5
        while elapsed < wait:
            if not await self._is_challenged(page):
                if elapsed:
                    logger.info("Cloudflare challenge cleared after %ds", elapsed)
                return True
            if await click_cf_checkbox(page):
                logger.info("Cloudflare checkbox auto-clicked, waiting for clearance")
                await asyncio.sleep(min(interval, 4))
            if elapsed == 0:
                logger.warning(
                    "Cloudflare challenge detected — waiting up to %ds "
                    "(auto-click attempted; complete via VNC if needed); "
                    "not reloading so clearance cookie stays valid",
                    wait,
                )
            await asyncio.sleep(interval)
            elapsed += interval
        await click_cf_checkbox(page)
        return not await self._is_challenged(page)

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
        """CDP 模式单次轮询：驱动外部 Chrome 中已打开的页面并提取。

        设计要点（避免反复触发 Cloudflare）：
        * 优先复用已缓存页面，用宽松 URL 匹配（首页 / 也算）
        * 挑战页上绝不 reload / goto
        * 默认不 reload（ARKHAM_CDP_RELOAD=false），只读当前 DOM
        * 仅在完全找不到 Arkham 标签时才导航一次
        """
        if self._context is None:
            await self._init_browser()

        try:
            page = None
            if self._cdp_page is not None and not self._cdp_page.is_closed():
                page = self._cdp_page
            else:
                self._cdp_page = None
                page = self._find_cdp_page()

            if page is None:
                if not self._context.pages:
                    logger.warning("CDP mode: browser has no open tabs")
                    return
                page = self._context.pages[0]
                if await self._is_challenged(page):
                    cleared = await self._wait_cf_clear(page)
                    if not cleared:
                        return
                # 仅当当前标签根本不是 Arkham 时才导航
                url_l = (page.url or "").lower()
                if "arkm.com" not in url_l and "arkhamintelligence.com" not in url_l:
                    if not settings.arkham_transfers_url:
                        logger.warning("CDP mode: no Arkham tab and no ARKHAM_TRANSFERS_URL")
                        return
                    logger.info(
                        "CDP mode: no Arkham tab found, navigating once to %s",
                        settings.arkham_transfers_url,
                    )
                    try:
                        await page.goto(
                            settings.arkham_transfers_url,
                            wait_until="domcontentloaded",
                            timeout=45_000,
                        )
                    except Exception as exc:
                        logger.warning("CDP mode: navigation failed: %s", exc)
                        return
                    if await self._is_challenged(page):
                        await self._wait_cf_clear(page)
                        if await self._is_challenged(page):
                            return
            else:
                if await self._is_challenged(page):
                    cleared = await self._wait_cf_clear(page)
                    if not cleared:
                        return
                # 可选 reload；默认关闭，避免数据中心 IP 反复触发 CF
                if settings.arkham_cdp_reload:
                    try:
                        await page.reload(
                            wait_until="domcontentloaded", timeout=45_000
                        )
                    except Exception as exc:
                        logger.warning(
                            "CDP mode: page reload failed (%s), parsing current DOM",
                            exc,
                        )
                    if await self._is_challenged(page):
                        await self._wait_cf_clear(page)
                        if await self._is_challenged(page):
                            return

            # 等数据表格真正渲染出多行，避免解析半渲染页面
            try:
                await page.wait_for_function(
                    "document.querySelectorAll("
                    "\"table tbody tr, [role='row']\").length >= 3",
                    timeout=15_000,
                )
            except Exception:
                pass

            if await self._is_challenged(page):
                logger.warning(
                    "CDP mode: still on Cloudflare challenge after wait — skip cycle"
                )
                return

            transfers = await _extract_transfers_from_page(
                page, allow_text_fallback=settings.arkham_text_fallback
            )
            logger.info("Extracted %d transfer(s) from CDP page", len(transfers))
            # 即使本轮无数据也缓存页面，避免下轮误判 tab lost 并重新导航
            self._cdp_page = page
            await self._dispatch(transfers)
        except Exception as exc:
            logger.error("CDP polling error: %s", exc, exc_info=True)
            # 连接可能断开，下次重连
            self._cdp_page = None
            await self._cleanup_browser()

    async def _ensure_page(self):
        """获取或创建持久标签页，并挂上网络监听。"""
        if self._context is None:
            await self._init_browser()
        if self._page is not None and not self._page.is_closed():
            return self._page
        page = await self._context.new_page()
        self._watch_page_responses(page)
        self._page = page
        return page

    async def _poll_once(self) -> None:
        """单次轮询：加载/刷新公开 transfers 页 → 优先 XHR JSON → DOM 兜底 → 推送。"""
        if settings.arkham_cdp_url:
            await self._poll_once_cdp()
            return

        # 机房 IP 连续被 CF 拦时自动退避，避免空转拖垮进程
        import time as _time
        now = _time.time()
        if self._cf_backoff_until > now:
            left = int(self._cf_backoff_until - now)
            if left % 300 < settings.arkham_poll_interval_seconds:
                logger.info(
                    "Arkham public page in CF backoff (%ds left); "
                    "server IP likely blocked — 请配置 ARKHAM_CDP_URL 挂接已过验证的浏览器",
                    left,
                )
            return

        try:
            page = await self._ensure_page()
            self._network_transfers = []

            need_nav = True
            url_l = (page.url or "").lower()
            if "arkm.com" in url_l or "arkhamintelligence.com" in url_l:
                if not await self._is_challenged(page):
                    # 已在业务页：轻量 reload 拿新数据（比每轮 new page 更不易触发 CF）
                    need_nav = False
                    try:
                        await page.reload(
                            wait_until="domcontentloaded", timeout=60_000
                        )
                    except Exception as exc:
                        logger.warning("Page reload failed (%s), will re-navigate", exc)
                        need_nav = True

            if need_nav:
                logger.info("Navigating to public page %s", settings.arkham_transfers_url)
                await page.goto(
                    settings.arkham_transfers_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )

            if await self._is_challenged(page):
                # 服务器上几乎过不了 CF：短等一次即可，不要每次卡 120s
                wait_s = min(int(settings.arkham_cf_wait_seconds or 0), 20)
                logger.warning(
                    "Cloudflare challenge on public page (server/datacenter IP 常见) — "
                    "wait up to %ds",
                    wait_s,
                )
                if not await self._wait_cf_clear(page, max_wait=wait_s):
                    self._cf_fail_streak += 1
                    # 1 次失败 → 10min；之后最长 2h
                    backoff = min(7200, 600 * self._cf_fail_streak)
                    self._cf_backoff_until = _time.time() + backoff
                    logger.warning(
                        "CF still blocking after %ds (streak=%d). "
                        "Backoff %ds. 请配置 ARKHAM_CDP_URL 挂接已过验证的浏览器，"
                        "或申请 ARKHAM_API_KEY",
                        wait_s,
                        self._cf_fail_streak,
                        backoff,
                    )
                    return
                self._cf_fail_streak = 0

            # 等列表或 XHR 数据
            try:
                await page.wait_for_function(
                    "document.querySelectorAll("
                    "\"table tbody tr, [role='row']\").length >= 2",
                    timeout=20_000,
                )
            except Exception:
                # 有些布局完全靠 XHR + 虚拟列表，DOM 可能很少
                await asyncio.sleep(3)

            # 给 XHR 一点时间落地
            await asyncio.sleep(2)
            net = await self._drain_network_transfers()
            dom = await _extract_transfers_from_page(
                page, allow_text_fallback=settings.arkham_text_fallback
            )

            # 合并去重
            merged: list[dict] = []
            seen: set[str] = set()
            for t in net + dom:
                k = _dedup_key(t)
                if k in seen:
                    continue
                seen.add(k)
                merged.append(t)

            logger.info(
                "Extracted %d transfer(s) from public page (xhr=%d dom=%d)",
                len(merged),
                len(net),
                len(dom),
            )
            if merged:
                self._cf_fail_streak = 0
                self._cf_backoff_until = 0.0
                await self._save_storage_state()
            await self._dispatch(merged)

        except Exception as exc:
            logger.error("Polling error during page fetch/parse: %s", exc, exc_info=True)
            # 浏览器状态可能损坏，下次重建
            await self._cleanup_browser()

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
