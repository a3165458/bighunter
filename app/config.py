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
    min_usd_value: float = 1_000_000.0

    # Webhook 鉴权（可选）
    webhook_secret: str = ""

    # ---- 币安上币过滤 ----
    # 仅播报已在币安（现货或合约）上架的代币
    require_binance_listing: bool = False
    # 币安上币列表缓存刷新间隔（秒），默认 1 小时
    binance_refresh_interval: int = 3600
    # false 时不排除 BTC/ETH/稳定币等主流代币，只按金额阈值处理
    filter_mainstream_tokens: bool = False

    # ---- 代币屏蔽 / 同币冷却 / 妖币热度 ----
    # 环境变量静态黑名单（逗号分隔，如 PEPE,DOGE），与 Telegram 运行时屏蔽叠加
    blocked_tokens: str = ""
    # 运行时屏蔽/冷却状态落盘路径（Docker 建议 /data/token_guard.json）
    token_guard_path: str = "data/token_guard.json"
    # 同一代币两次播报的最短间隔（秒）；0=关闭
    token_cooldown_seconds: int = 3600
    # 金额 ≥ 该值时可突破冷却与热度上限；0=不允许突破
    token_cooldown_break_usd: float = 0.0
    # 妖币模式：窗口内同一代币超过次数则视为噪音，自动跳过
    yaobi_mode: bool = True
    token_max_alerts_per_window: int = 2
    token_alert_window_seconds: int = 21600
    # 告警消息附带「屏蔽此币 / 静音 6h」按钮
    telegram_mute_button: bool = True
    # 允许操作屏蔽的 Telegram user id（逗号分隔）；留空=配置 chat 内任何人可点
    telegram_admin_user_ids: str = ""

    # 日志
    log_level: str = "INFO"

    # ---- Arkham 官方 REST API（可选；有 Key 时用，无 Key 可完全忽略） ----
    arkham_api_key: str = ""
    arkham_api_base_url: str = "https://api.arkm.com"
    arkham_api_bases: str = ""
    arkham_api_usd_gte: float = 0.0  # 0 = 使用 min_usd_value
    arkham_api_limit: int = 50
    arkham_api_poll_interval_seconds: int = 60

    # ---- Arkham 公开网页轮询（无 API Key / 无账户登录的主路径） ----
    # 数据来源为 arkm.com 首页 RECENT TRANSFERS 与其公开 transfers 接口：
    # 通过 Playwright/CDP 驱动真实浏览器（复用已过 Cloudflare 的 Chrome 会话），
    # 匿名模式不要求 Arkham 账户登录。有 ARKHAM_API_KEY 时优先走 REST 更稳。
    arkham_polling_enabled: bool = True
    # 要轮询的公开 transfers 页面 URL（你在网页里筛好条件后可粘贴完整 URL）
    arkham_transfers_url: str = "https://arkm.com/transfers"
    # 轮询间隔（秒），默认 30s
    arkham_poll_interval_seconds: int = 30
    # cookie 罐：保存 Cloudflare 等反爬 cookie（不是账户登录）
    # Docker 映射到 /data；本机脚本 scripts/run_public_arkm.py 会自动回退到 ./data/
    arkham_storage_state_path: str = "data/arkham-storage-state.json"
    # true=不要求 Arkham 账户登录（仍会读写 cookie 罐以应对 CF）
    arkham_anonymous_mode: bool = True
    # Playwright 是否无头。服务器上若要「像本机一样点验证」：设 false + 接 VNC 看 DISPLAY
    # 例: DISPLAY=:99（本机常见 Xvfb）+ x11vnc，运行 scripts/arkm_headed_unlock.py
    arkham_headless: bool = True
    # CDP 模式：挂接一个已在运行的 Chrome（如 http://172.17.0.1:9223）。
    # 设置后不再自建浏览器，直接操作已打开的 transfers 页——复用其中的
    # Cloudflare clearance cookie / 登录态，每轮轮询时刷新该页拿最新数据。
    arkham_cdp_url: str = ""
    # 是否允许"全页文本正则"兜底提取（Strategy 4）。页面半渲染时该策略
    # 会把价格/市值等无关文本误判成转账，生产环境默认关闭。
    arkham_text_fallback: bool = False
    # CDP 模式下：遇到 Cloudflare 时最长等待秒数（不刷新、不打断人工验证）
    arkham_cf_wait_seconds: int = 120
    # CDP：是否每轮 reload。false 时只解析当前 DOM（更不易触发 CF，数据可能略旧）
    arkham_cdp_reload: bool = False
    # 住宅/移动代理（强烈建议云服务器抓 ARKM 时配置）
    # 例: http://user:pass@host:port  或 socks5://user:pass@host:port
    # Cloudflare 对机房 IP 几乎不放行；代理出口需是家宽/住宅 IP
    arkham_proxy_server: str = ""

    # ---- 主流代币黑名单（硬编码，大写匹配） ----
    exclude_tokens: set[str] = {
        "BTC", "ETH", "USDT", "USDC", "DAI", "FDUSD",
        "WBTC", "WETH", "SOL", "BNB", "STETH",
        "BUSD", "TUSD", "USDD", "PYUSD", "GUSD",
    }


settings = Settings()
