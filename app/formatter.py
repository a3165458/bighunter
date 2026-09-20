"""Telegram MarkdownV2 消息格式化器。"""

import re

from app import binance_listings
from app import token_guard
from app.config import settings

# ---- MarkdownV2 特殊字符转义 ----
_MD2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _esc(text: str) -> str:
    """转义 MarkdownV2 特殊字符。"""
    return _MD2_ESCAPE_RE.sub(r"\\\1", str(text))


def _short_addr(addr: str) -> str:
    """地址脱敏：0x1234...abcd"""
    if addr and len(addr) > 12:
        return f"{addr[:6]}\\.\\.\\.{addr[-4:]}"
    return _esc(addr or "Unknown")


def _to_float(value: object) -> float:
    """宽容地转 float：webhook/页面解析出的数值可能是字符串。"""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _fmt_usd(value: float) -> str:
    """金额千分位格式化：$1,234,567"""
    return _esc(f"${value:,.0f}")


def _fmt_amount(amount: float, symbol: str) -> str:
    """代币数量格式化"""
    if amount >= 1_000_000:
        return _esc(f"{amount:,.0f} {symbol}")
    return _esc(f"{amount:,.2f} {symbol}")


def _direction_emoji(from_label: str, to_label: str) -> tuple[str, str]:
    """根据转账方向推断类型和 Emoji。"""
    fl = (from_label or "").lower()
    tl = (to_label or "").lower()

    cex_keywords = [
        "binance", "coinbase", "okx", "bybit", "kraken",
        "huobi", "htx", "kucoin", "gate", "bitfinex", "upbit",
        "mexc", "bitget",
    ]
    from_is_cex = any(k in fl for k in cex_keywords)
    to_is_cex = any(k in tl for k in cex_keywords)

    if from_is_cex and not to_is_cex:
        return "🏦 CEX 提现", "从交易所提出，可能进入 DeFi 或冷钱包"
    if not from_is_cex and to_is_cex:
        return "📥 CEX 充值", "充入交易所，可能准备抛售"
    if from_is_cex and to_is_cex:
        return "🔄 CEX 互转", "交易所间调仓"
    return "🐋 巨鲸转账", "链上大额移动，关注后续动作"


def _binance_badge(token: str) -> str:
    """生成币安上架状态标签。"""
    detail = binance_listings.listing_detail(token)
    badges = []
    if detail["spot"]:
        badges.append("现货")
    if detail["usdt_m"]:
        badges.append("U本位")
    if detail["coin_m"]:
        badges.append("币本位")
    if not badges:
        return ""
    return _esc(" | ".join(badges))


def _build_links(token: str, blockchain: str, arkham_url: str) -> str:
    """拼装 DexScreener / Etherscan / Arkham 快捷链接。"""
    chain_map = {
        "ethereum": "ethereum",
        "bsc": "bsc",
        "arbitrum": "arbitrum",
        "polygon": "polygon",
        "base": "base",
        "optimism": "optimism",
        "avalanche": "avalanche",
    }
    dex_chain = chain_map.get((blockchain or "").lower(), "ethereum")

    links: list[str] = []
    links.append(
        f"[DexScreener](https://dexscreener.com/{_esc(dex_chain)}/{_esc(token)})"
    )

    explorer_map = {
        "ethereum": "https://etherscan.io",
        "bsc": "https://bscscan.com",
        "arbitrum": "https://arbiscan.io",
        "polygon": "https://polygonscan.com",
        "base": "https://basescan.org",
        "optimism": "https://optimistic.etherscan.io",
        "avalanche": "https://snowtrace.io",
    }
    explorer = explorer_map.get((blockchain or "").lower())
    if explorer:
        links.append(f"[区块浏览器]({_esc(explorer)})")

    if arkham_url:
        links.append(f"[Arkham]({_esc(arkham_url)})")

    return " \\| ".join(links)


def format_alert(payload: dict) -> str:
    """将 Arkham Webhook payload 格式化为 MarkdownV2 消息。"""
    token = payload.get("tokenSymbol") or "UNKNOWN"
    usd_value = _to_float(payload.get("usdValue", 0))
    from_label = payload.get("fromAddressLabel") or "Unknown"
    to_label = payload.get("toAddressLabel") or "Unknown"
    from_addr = payload.get("fromAddress") or ""
    to_addr = payload.get("toAddress") or ""
    blockchain = payload.get("blockchain") or "ethereum"
    arkham_url = payload.get("arkhamUrl") or ""
    unit_amount = _to_float(payload.get("unitAmount", 0))

    direction, hint = _direction_emoji(from_label, to_label)
    badge = _binance_badge(token)

    title = "🔥 *WhaleScope 妖币异动*" if settings.yaobi_mode else "🔔 *WhaleScope 冷门币异动*"
    lines = [
        title,
        "",
        f"*{direction}*",
        "",
        f"💰 代币: *{_esc(token)}*",
        f"💵 价值: *{_fmt_usd(usd_value)}*",
        f"📦 数量: {_fmt_amount(unit_amount, token)}",
        f"⛓ 链: {_esc(blockchain.capitalize())}",
    ]

    if badge:
        lines.append(f"🏦 币安: {badge}")

    heat = token_guard.describe_heat(token)
    if heat:
        lines.append(f"📊 {_esc(heat)}")

    lines.extend([
        "",
        f"📤 From: `{_esc(from_label)}` \\({_short_addr(from_addr)}\\)",
        f"📥 To:   `{_esc(to_label)}` \\({_short_addr(to_addr)}\\)",
        "",
        f"💡 _{_esc(hint)}_",
        "",
        f"🔗 {_build_links(token, blockchain, arkham_url)}",
    ])
    return "\n".join(lines)
