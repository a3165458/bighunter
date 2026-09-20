"""首页 RECENT TRANSFERS 轮询（有头 Chrome + 自动点 Cloudflare 复选框）。

数据来源与官网首页一致：
  GET https://api.arkm.com/transfers?base=all&flow=all&usdGte=1&sortKey=time&sortDir=desc&limit=16&offset=0

流程：
  1. 启动系统 Google Chrome（有头，DISPLAY，持久 profile）
  2. 打开 https://arkm.com/ ，自动点击 “Verify you are human”
  3. 监听 / 主动拉取 transfers 接口（带页面同款 x-timestamp / x-payload）
  4. 解析后走 app.alerts.process_payload → Telegram

注意：若接口被 Cloudflare 单独拦截，首页区块会显示「出了点问题」——
这与手动打开看到错误一致；一旦接口放行即可自动播报。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

from app.alerts import process_payload
from app.arkham_api_polling import _extract_list, _parse_transfer
from app.arkham_polling import click_cf_checkbox
from app.config import settings
from app import poller_health

logger = logging.getLogger("whalescope.arkham_home")

_sent: set[str] = set()
_MAX = 10_000

HOME_URL = "https://arkm.com/"
# 与首页 RECENT TRANSFERS 组件相同的查询
TRANSFERS_URL_TMPL = (
    "https://api.arkm.com/transfers"
    "?base=all&flow=all&usdGte={usd}&sortKey=time&sortDir=desc&limit={limit}&offset=0"
)


def response_matches_transfer_query(url: str, min_usd: float) -> bool:
    """仅接收满足业务金额阈值的全量转账响应。"""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        usd_gte = float(params.get("usdGte", ["0"])[0])
    except (TypeError, ValueError):
        return False
    return (
        parsed.netloc == "api.arkm.com"
        and parsed.path == "/transfers"
        and params.get("base") == ["all"]
        and params.get("flow") == ["all"]
        and usd_gte >= min_usd
    )


def rewrite_transfer_query_url(url: str, min_usd: float) -> str:
    """保留网页签名请求结构，只把 transfers 查询改为业务金额阈值。"""
    parsed = urlparse(url)
    if parsed.netloc != "api.arkm.com" or parsed.path != "/transfers":
        return url
    params = parse_qs(parsed.query, keep_blank_values=True)
    params["base"] = ["all"]
    params["flow"] = ["all"]
    params["usdGte"] = [str(int(min_usd) if min_usd >= 1 else 1)]
    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))


def _dedup_key(t: dict) -> str:
    tid = (t.get("_id") or "").strip()
    if tid:
        return f"home:{tid}"
    url = (t.get("arkhamUrl") or "").strip()
    if url:
        return f"home:{url}"
    raw = "|".join(
        str(t.get(k, ""))
        for k in ("tokenSymbol", "usdValue", "fromAddressLabel", "toAddressLabel", "unitAmount")
    )
    return "home:" + hashlib.md5(raw.encode()).hexdigest()


def _remember(key: str) -> bool:
    if key in _sent:
        return False
    if len(_sent) >= _MAX:
        _sent.clear()
    _sent.add(key)
    return True


def reset_home_dedup() -> None:
    _sent.clear()


def cdp_session_is_dead(exc: BaseException) -> bool:
    """Playwright/CDP 会话已死，应立刻重连，而不是再空转几轮。"""
    text = f"{type(exc).__name__} {exc}".lower()
    needles = (
        "targetclosed",
        "target closed",
        "browser has been closed",
        "connection closed",
        "websocket",
        "disconnected",
        "connect_over_cdp",
    )
    return any(n in text for n in needles)


def network_error_needs_browser_restart(exc: BaseException) -> bool:
    """页面级网络错误（常见为代理/CDP 挂接的 Chrome 出口不可达）。"""
    text = f"{type(exc).__name__} {exc}".lower()
    needles = (
        "err_timed_out",
        "err_proxy",
        "err_connection",
        "err_name_not_resolved",
        "err_internet_disconnected",
        "net::err_",
    )
    return any(n in text for n in needles)


def build_chrome_command(profile: str, proxy_server: str = "") -> list[str]:
    """构建 Arkham Chrome 命令；VLESS 由本地 SOCKS/HTTP 桥接后传入。"""
    command = [
        "google-chrome",
        "--no-sandbox",
        "--remote-debugging-port=9222",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1440,900",
    ]
    if proxy_server.strip():
        command.append(f"--proxy-server={proxy_server.strip()}")
    command.append(HOME_URL)
    return command


class ArkhamHomePoller:
    """有头 Chrome 轮询首页转账。"""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._primed = False
        self._chrome_proc: Optional[subprocess.Popen] = None
        # CDP 目标：优先使用配置的 ARKHAM_CDP_URL（如挂接宿主已过 CF 的 Chrome）
        self._cdp = (settings.arkham_cdp_url or "").strip() or "http://127.0.0.1:9222"
        # 自建 Chrome 的 profile 目录：跟随 storage state 所在目录，持久于 Volume
        storage_parent = Path(
            settings.arkham_storage_state_path or "data/arkham-storage-state.json"
        ).parent
        self._profile = str((storage_parent / "chrome-home-profile").resolve())

    async def start(self) -> None:
        if not settings.arkham_polling_enabled:
            logger.info("Arkham home polling disabled (ARKHAM_POLLING_ENABLED=false)")
            return
        # 仅在未配置 API Key 时作为无 Key 主路径；有 Key 时 REST 更稳
        logger.info(
            "Starting Arkham HOME RECENT TRANSFERS poller | display=%s | cdp=%s | profile=%s",
            os.environ.get("DISPLAY", "(unset)"),
            self._cdp,
            self._profile,
        )
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="arkham-home-poller")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._stop_chrome()
        logger.info("Arkham home polling stopped")

    def _stop_chrome(self) -> None:
        if self._chrome_proc and self._chrome_proc.poll() is None:
            try:
                self._chrome_proc.send_signal(signal.SIGTERM)
                self._chrome_proc.wait(timeout=5)
            except Exception:
                try:
                    self._chrome_proc.kill()
                except Exception:
                    pass
        self._chrome_proc = None

    def _ensure_display(self) -> None:
        if not os.environ.get("DISPLAY"):
            os.environ["DISPLAY"] = ":99"
            logger.info("DISPLAY unset → using :99 (Xvfb/VNC)")

    def _start_chrome(self) -> None:
        self._ensure_display()
        Path(self._profile).mkdir(parents=True, exist_ok=True)
        # 若已有 CDP 可连则复用
        try:
            import urllib.request

            urllib.request.urlopen(self._cdp + "/json/version", timeout=2)
            logger.info("Reusing existing Chrome CDP at %s", self._cdp)
            return
        except Exception:
            pass

        cmd = build_chrome_command(
            profile=self._profile,
            proxy_server=settings.arkham_proxy_server,
        )
        logger.info("Launching Chrome: %s", " ".join(cmd[:4]) + " ...")
        self._chrome_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
        )
        time.sleep(4)

    async def _click_cf_checkbox(self) -> bool:
        """截图找 Cloudflare 橙色 logo，点击左侧 Verify checkbox。"""
        try:
            from PIL import Image
        except ImportError:
            logger.warning("PIL not available for CF click")
            return False

        shot = "/tmp/arkm_cf_shot.png"
        try:
            subprocess.check_call(
                ["import", "-window", "root", shot],
                env=os.environ.copy(),
                timeout=10,
            )
        except Exception as exc:
            logger.debug("screenshot failed: %s", exc)
            # fallback center clicks
            for _ in range(5):
                subprocess.call(
                    ["xdotool", "mousemove", "525", "473", "click", "1"],
                    env=os.environ.copy(),
                )
                await asyncio.sleep(1)
            return False

        img = Image.open(shot)
        px = img.load()
        orange: list[tuple[int, int]] = []
        w, h = img.size
        for y in range(int(h * 0.3), int(h * 0.7), 2):
            for x in range(int(w * 0.25), int(w * 0.75), 2):
                r, g, b = px[x, y][:3]
                if r > 200 and 80 < g < 160 and b < 80:
                    orange.append((x, y))
        if not orange:
            # no orange = maybe already passed or no CF widget
            return False
        xs = [c[0] for c in orange]
        ys = [c[1] for c in orange]
        # checkbox is left of CF orange logo inside the widget
        cx = min(xs) - 35
        cy = sum(ys) // len(ys)
        logger.info("CF checkbox click at (%d,%d)", cx, cy)
        subprocess.call(
            ["xdotool", "mousemove", "--sync", str(cx), str(cy), "click", "1"],
            env=os.environ.copy(),
        )
        return True

    async def _wait_cf_clear(self, page, max_s: int = 90) -> bool:
        end = time.time() + max_s
        tried_dom = False
        while time.time() < end:
            try:
                title = (await page.title()).lower()
            except Exception:
                title = ""
            if not any(x in title for x in ("moment", "attention", "verification", "just a")):
                return True
            # 1) 纯 DOM 勾选 Turnstile（CDP/有头均可用，无需显示器）
            if not tried_dom:
                if await click_cf_checkbox(page):
                    tried_dom = True
                    logger.info("CF checkbox auto-clicked via DOM (home)")
            # 2) 有头环境兜底：按坐标点击 + iframe checkbox 点击
            try:
                await page.mouse.click(525, 473)
                for fr in page.frames:
                    el = await fr.query_selector("input[type=checkbox]")
                    if el:
                        await el.click(timeout=1000)
            except Exception:
                pass
            # 3) 无头无 X 时，依赖 CF 自动校验或人工（VNC）
            await asyncio.sleep(2)
        return False


    def _min_usd_threshold(self) -> float:
        if settings.arkham_api_usd_gte > 0:
            return settings.arkham_api_usd_gte
        return max(1.0, settings.min_usd_value)

    def _transfers_page_url(self) -> str:
        return settings.arkham_transfers_url or "https://arkm.com/transfers"

    async def _navigate_for_transfers(self, page) -> None:
        """触发 Arkham 前端重新签名 transfers 请求（reload 无效时回退 goto）。"""
        target = self._transfers_page_url()
        try:
            await page.reload(wait_until="domcontentloaded", timeout=90_000)
            return
        except Exception as reload_exc:
            logger.warning("page.reload failed (%s)", reload_exc)
            if network_error_needs_browser_restart(reload_exc):
                raise
        logger.info("Falling back to goto %s", target)
        await page.goto(target, wait_until="domcontentloaded", timeout=90_000)

    async def _fetch_transfers_via_page(self, page) -> list[dict]:
        """监听浏览器 signed transfers XHR，客户端按金额阈值过滤。

        不能改写请求 URL（会破坏 x-payload/x-timestamp 签名）；usdGte=1 由前端发起，
        大额过滤在 Python 侧完成。
        """
        min_usd = self._min_usd_threshold()
        captured: list[dict] = []
        api_failed = False

        async def on_response(response) -> None:
            if not response.url.startswith("https://api.arkm.com/transfers?"):
                return
            if response.status != 200:
                logger.warning("Transfers API HTTP %s", response.status)
                return
            try:
                payload = await response.json()
                captured.extend(self._normalize_payloads([payload]))
            except Exception as exc:
                logger.warning("Transfers API parse failed: %s", exc)

        def on_request_failed(request) -> None:
            nonlocal api_failed
            if "api.arkm.com/transfers" not in request.url or captured:
                return
            api_failed = True
            logger.warning(
                "Transfers API request failed (%s) — 机房 IP 常被 Arkham 单独拦截，"
                "需可用住宅代理（Chrome --proxy-server）或 ARKHAM_API_KEY",
                request.failure,
            )

        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)
        try:
            url_l = (page.url or "").lower()
            if "arkm.com/transfers" not in url_l:
                await page.goto(
                    self._transfers_page_url(),
                    wait_until="domcontentloaded",
                    timeout=90_000,
                )
            else:
                await self._navigate_for_transfers(page)
            # 等待首页/转账页 XHR 完成
            for _ in range(12):
                if captured or api_failed:
                    break
                await asyncio.sleep(1)
            if not captured and not api_failed:
                logger.info("No transfers XHR yet, retry goto %s", self._transfers_page_url())
                await page.goto(
                    self._transfers_page_url(),
                    wait_until="networkidle",
                    timeout=90_000,
                )
                await asyncio.sleep(3)
        finally:
            page.remove_listener("response", on_response)
            page.remove_listener("requestfailed", on_request_failed)

        filtered = [
            t
            for t in captured
            if float(t.get("usdValue") or 0) >= min_usd
        ]
        logger.info(
            "Arkham browser transfers: raw=%d filtered>=%s=%d",
            len(captured),
            f"{min_usd:,.0f}",
            len(filtered),
        )
        if not captured and api_failed:
            raise RuntimeError(
                "api.arkm.com/transfers blocked (net::ERR_FAILED) — "
                "fix outbound proxy or set ARKHAM_API_KEY"
            )
        return filtered

    def _normalize_payloads(self, payloads: list[Any]) -> list[dict]:
        out: list[dict] = []
        for payload in payloads:
            for item in _extract_list(payload):
                t = _parse_transfer(item)
                if t:
                    out.append(t)
        return out

    async def _dispatch(self, transfers: list[dict]) -> None:
        if not self._primed:
            if not transfers:
                return
            for t in transfers:
                _remember(_dedup_key(t))
            self._primed = True
            logger.info(
                "Home first cycle: primed %d transfer(s), no push", len(transfers)
            )
            return
        sent = 0
        for t in transfers:
            if not _remember(_dedup_key(t)):
                continue
            result = await process_payload(t, self._http, source="arkham_home")
            if result.get("status") == "sent":
                sent += 1
        if sent:
            logger.info("Home cycle: sent %d alert(s)", sent)

    async def _restore_storage_cookies(self, ctx) -> None:
        """把落盘的 CF / 会话 cookie 注入 CDP 浏览器，加速过验证。"""
        path = (settings.arkham_storage_state_path or "").strip()
        if not path or not Path(path).is_file():
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            cookies = [
                c
                for c in (data.get("cookies") or [])
                if "arkm" in (c.get("domain") or "").lower()
            ]
            if cookies:
                await ctx.add_cookies(cookies)
                logger.info("Restored %d Arkham cookie(s) from storage state", len(cookies))
        except Exception as exc:
            logger.warning("Failed to restore storage cookies: %s", exc)

    async def _connect_cdp(self, playwright) -> Any:
        """挂接已有 Chrome。timeout 必须短，避免卡死的 DevTools 会话拖 3 分钟。"""
        self._start_chrome()
        browser = await playwright.chromium.connect_over_cdp(self._cdp, timeout=30_000)
        if not browser.contexts:
            try:
                await browser.close()
            except Exception:
                pass
            raise RuntimeError("CDP browser has no contexts")
        await self._restore_storage_cookies(browser.contexts[0])
        return browser

    async def _cycle(self, browser) -> None:
        ctx = browser.contexts[0] if browser.contexts else None
        if not ctx:
            raise RuntimeError("No browser context")
        # 优先复用已在 arkm.com 的标签页（登录会话/转账列表所在），避免选到其他标签
        page = next(
            (p for p in ctx.pages if "arkm.com" in (p.url or "").lower()),
            None,
        )
        if page is None:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        # 首页本身包含 RECENT TRANSFERS。复用现有 Arkham 标签页，避免每轮
        # 导航重置已应用的高金额筛选；仅首次进入或显式要求 reload 时导航。
        url_l = (page.url or "").lower()
        already_on_arkham = "arkm.com" in url_l
        if settings.arkham_cdp_reload or not already_on_arkham:
            try:
                await page.goto(
                    self._transfers_page_url(),
                    wait_until="domcontentloaded",
                    timeout=90_000,
                )
            except Exception as exc:
                logger.warning("goto transfers: %s", exc)
                if network_error_needs_browser_restart(exc):
                    raise
        ok = await self._wait_cf_clear(page, max_s=max(30, settings.arkham_cf_wait_seconds))
        if not ok:
            logger.warning("Cloudflare not cleared this cycle")
            return
        logger.info("CF cleared, title=%s", await page.title())
        await self._save_storage_state(page)
        transfers = await self._fetch_transfers_via_page(page)
        logger.info("Home extracted %d transfer(s)", len(transfers))
        poller_health.mark_success("arkham_home", len(transfers))
        await self._dispatch(transfers)

    async def _save_storage_state(self, page) -> None:
        """把当前会话的 CF clearance cookie / localStorage 落盘，供重启复用。"""
        path = (settings.arkham_storage_state_path or "").strip()
        if not path:
            return
        try:
            ctx = page.context
            parent = Path(path).parent
            parent.mkdir(parents=True, exist_ok=True)
            await ctx.storage_state(path=path)
            logger.debug("Saved Arkham storage state to %s", path)
        except Exception as exc:
            logger.warning("Failed to save storage state: %s", exc)

    async def _loop(self) -> None:
        from playwright.async_api import async_playwright

        while self._running:
            browser = None
            reconnect_soon = False
            try:
                async with async_playwright() as p:
                    browser = await self._connect_cdp(p)
                    logger.info("CDP session established, reusing until error")
                    consecutive_errors = 0
                    while self._running:
                        try:
                            await self._cycle(browser)
                            consecutive_errors = 0
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            consecutive_errors += 1
                            poller_health.mark_error("arkham_home", str(exc))
                            logger.error("Home poll error: %s", exc, exc_info=True)
                            if network_error_needs_browser_restart(exc):
                                logger.warning(
                                    "Network error (check Chrome proxy/outbound), "
                                    "will reconnect CDP"
                                )
                                reconnect_soon = True
                                break
                            if cdp_session_is_dead(exc) or consecutive_errors >= 3:
                                logger.warning("CDP session looks stale, reconnecting")
                                reconnect_soon = True
                                break
                        await asyncio.sleep(settings.arkham_poll_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                reconnect_soon = True
                poller_health.mark_error("arkham_home", str(exc))
                logger.error("Home poll connect error: %s", exc, exc_info=True)
            finally:
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        pass
            delay = 5 if reconnect_soon else settings.arkham_poll_interval_seconds
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
