"""WhaleScope 配置模块 — 基于 pydantic-settings，从 .env 加载。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_topic_id: int = 0

    # 过滤阈值
    min_usd_value: float = 50_000.0

    # Webhook 鉴权（可选）
    webhook_secret: str = ""

    # ---- 币安上币过滤 ----
    # 仅播报已在币安（现货或合约）上架的代币
    require_binance_listing: bool = True
    # 币安上币列表缓存刷新间隔（秒），默认 1 小时
    binance_refresh_interval: int = 3600

    # 日志
    log_level: str = "INFO"

    # ---- Arkham Polling（可选模式） ----
    arkham_polling_enabled: bool = False
    # 要轮询的 Arkham transfers/filter 页面完整 URL
    arkham_transfers_url: str = ""
    # 轮询间隔（秒），默认 30s
    arkham_poll_interval_seconds: int = 30
    # Playwright storage state 路径，用于保留登录态（见 README）
    arkham_storage_state_path: str = "/data/arkham-storage-state.json"
    # Playwright 是否无头模式
    arkham_headless: bool = True
    # CDP 模式：挂接一个已在运行的 Chrome（如 http://172.17.0.1:9223）。
    # 设置后不再自建浏览器，直接操作已打开的 transfers 页——复用其中的
    # Cloudflare clearance cookie / 登录态，每轮轮询时刷新该页拿最新数据。
    arkham_cdp_url: str = ""
    # 是否允许"全页文本正则"兜底提取（Strategy 4）。页面半渲染时该策略
    # 会把价格/市值等无关文本误判成转账，生产环境默认关闭。
    arkham_text_fallback: bool = False

    # ---- 主流代币黑名单（硬编码，大写匹配） ----
    exclude_tokens: set[str] = {
        "BTC", "ETH", "USDT", "USDC", "DAI", "FDUSD",
        "WBTC", "WETH", "SOL", "BNB", "STETH",
        "BUSD", "TUSD", "USDD", "PYUSD", "GUSD",
    }


settings = Settings()
