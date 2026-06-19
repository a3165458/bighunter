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

    # 过滤阈值
    min_usd_value: float = 50_000.0

    # Webhook 鉴权（可选）
    webhook_secret: str = ""

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

    # ---- 主流代币黑名单（硬编码，大写匹配） ----
    exclude_tokens: set[str] = {
        "BTC", "ETH", "USDT", "USDC", "DAI", "FDUSD",
        "WBTC", "WETH", "SOL", "BNB", "STETH",
        "BUSD", "TUSD", "USDD", "PYUSD", "GUSD",
    }


settings = Settings()
