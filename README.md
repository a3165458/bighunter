# 🐋 WhaleScope — 服务器上的大额转账 → Telegram 自动播报（数据源：Arkham）

**唯一数据源为 Arkham（arkm.com）**：
- ⚠️ **无 API Key**可用公开页 + Playwright/CDP 尝试监听，但受 Arkham 前端签名和页面变更影响
- ✅ 借助 **CDP 挂接已过 Cloudflare 的 Chrome** 或 **自动勾选 Turnstile 验证框**通关
- ✅ 有 API Key 时自动切到官方 REST；生产大额监控推荐此模式
- ❌ 已移除 Whale Alert 数据源（本仓库不再包含/不会启用）

## 两种运行模式（二选一）

| 模式 | 前提 | 数据通道 | 说明 |
|------|------|----------|------|
| **模式 1：无 Key（尽力而为）** | 不填 `ARKHAM_API_KEY` | 浏览器公开页 | 免登录；受 Cloudflare、前端签名和页面结构变化影响 |
| **模式 2：有 Key（生产推荐）** | 填 `ARKHAM_API_KEY` | 官方 REST | 走 `/transfers` 接口，机房 IP 也能稳定用；自动跳过网页轮询 |

> 两模式只会启用其一：检测到 `ARKHAM_API_KEY` 就只走 REST，否则只走公开页——避免同一笔转账双源重复推送。

## 模式 1 详解：无 API / 无登录公开页监听

Bot 通过 Playwright 驱动真实 Chrome 打开 `https://arkm.com/`，
拦截首页 RECENT TRANSFERS 请求，解析最新转账 → 过滤 → Telegram。Arkham 可能要求 API Key
或调整前端签名/页面结构；需要稳定覆盖金额阈值以上的全部转账时，应使用模式 2。

### Cloudflare 如何通关（三种手段，按优先级）

1. **CDP 复用已通过验证的浏览器（推荐，零验证摩擦）**
   在本机/服务器上先用 Chrome 打开一次 `arkm.com`，手滑过 Cloudflare 验证后保持浏览器运行，
   然后让 Bot 通过 CDP 挂上去——直接复用对方的 `cf_clearance` cookie 与会话，不再弹验证：

   ```env
   ARKHAM_POLLING_ENABLED=true
   ARKHAM_CDP_URL=http://127.0.0.1:9222   # 本服务器 Chrome CDP 端口
   ARKHAM_ANONYMOUS_MODE=true               # 不要求账户登录
   ```

   浏览器以远程调试端口启动示例（有头窗口，退出前保持运行）：

   ```bash
   google-chrome --remote-debugging-port=9222 --user-data-dir=$HOME/.arkm-cdp https://arkm.com/
   # 手动过 1 次验证；之后 Bot 就能复用该会话
   ```

   Docker（`network_mode: host`）下填 `http://127.0.0.1:9222` 即可直接连宿主上的 Chrome。

2. **自动勾选 Turnstile 验证框（DOM 级，无需显示器）**
   未挂 CDP 时，Bot 自建浏览器并自动在 `challenges.cloudflare.com` 框架里点击 "Verify you are human" 复选框
   （`click_cf_checkbox`，纯 DOM 实现，CDP/有头/无头均可）。托管挑战（managed challenge）勾选后即自动放行。

3. **人工兜底（VNC）**
   仍有拦截时 Bot 会等待 `ARKHAM_CF_WAIT_SECONDS`（默认 60~120s），
   期间可用 VNC 连上服务器桌面手工点一下验证（见 `scripts/arkm_headed_unlock.py`）。

### 最省心的启动姿势（暗含 CDP + cookie 罐）

```bash
# 1) 让 Chrome 保持一个已通过验证的 arkm.com 标签页（一次性的，登录与否都行）
google-chrome --remote-debugging-port=9222 --user-data-dir=$HOME/.chrome-cdp https://arkm.com/

# 2) .env（模式 1）
ARKHAM_POLLING_ENABLED=true
ARKHAM_CDP_URL=http://127.0.0.1:9222
ARKHAM_ANONYMOUS_MODE=true
ARKHAM_HEADLESS=true
ARKHAM_STORAGE_STATE_PATH=data/arkham-storage-state.json   # 或 Docker 内 /data/…

# 3) 起服务，之后每次轮询都复用该会话；CF clearance 也会落盘到 storage state
docker compose up -d --build
```

## 快速部署

```bash
cp .env.example .env
# 编辑 .env：TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / MIN_USD_VALUE
# 无 Key：保持 ARKHAM_POLLING_ENABLED=true；有 Key：填 ARKHAM_API_KEY

docker compose up -d --build
docker compose logs -f whalescope-bot
```

期望日志：

```text
sources=['arkham_browser']          # (无 Key) 或 sources=['arkham_api'] (有 Key)
Starting Arkham HOME RECENT TRANSFERS poller | display=:99 | cdp=http://127.0.0.1:9222
CF cleared, title=Arkham | The Source of Truth On-Chain
Home extracted 5 transfer(s)
... Alert sent: PEPE $1,250,000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health        # 查看 polling 状态
curl -X POST http://127.0.0.1:8000/test-telegram
curl http://127.0.0.1:8000/blocklist     # 当前屏蔽列表
```

## 同一代币反复刷屏：屏蔽 + 妖币热度

Arkham 大额流里，PEPE / WIF 这类币会连续出现。开源「妖币雷达」项目
（[crypto-radar](https://github.com/brandyoy97-maker/crypto-radar)、
[yaobi-radar](https://github.com/2458283786-blip/yaobi-radar)）用**黑名单 + 频次抑制**
把刷屏币拿掉；它们本身是合约量价扫描器，数据源对不上，所以把这套思路接到本仓库的
Arkham 转账管线，而不是整仓 fork。

默认开启（可在 `.env` 改）：

| 能力 | 默认 | 作用 |
|------|------|------|
| 同币冷却 | 1 小时 | 同一代币 1 小时内只播 1 次 |
| 妖币热度上限 | 6 小时内 2 次 | 再出现视为噪音，自动跳过 |
| Telegram 按钮 | 开 | 每条告警底部可「屏蔽此币」或「静音 6h」 |
| 静态黑名单 | 空 | `BLOCKED_TOKENS=PEPE,DOGE` |

Telegram 里也可以发命令（需在配置的播报群）：

```text
/mute PEPE
/unmute PEPE
/snooze PEPE 6h
/blocklist
```

超大额想突破冷却时设 `TOKEN_COOLDOWN_BREAK_USD=5000000`。完全关闭热度抑制：

```env
YAOBI_MODE=false
TOKEN_COOLDOWN_SECONDS=0
```

## 可选：官方 REST（有 Key 时）

```env
ARKHAM_API_KEY=你的密钥
ARKHAM_API_BASES=all        # 全量大额；币安上架过滤开启且留空时也自动使用 all
```

填写后公开页轮询自动关闭（单源防重复）。生产环境的全量大额监听应配置 API Key。

## 避免 Chrome CDP 假活停摆

`/json/version` 通了不等于 Playwright 还能驱动页面。Chrome 挂接 CDP 过久会
WebSocket 假活：HTTP 200，协议不再应答，播报静默停掉。防护分三层：

1. Bot 复用同一条 CDP 长连接，断线立刻重连（超时 30s，不再每轮重挂）
2. `arkham-chrome.service` 每 6 小时 `RuntimeMaxSec` 强制换一轮 Chrome
3. 宿主 timer 每 2 分钟跑 `scripts/arkham_cdp_watchdog.sh`：心跳超过 10 分钟
   或 CDP HTTP 挂掉就 `systemctl restart arkham-chrome`

心跳文件：`/data/arkham-heartbeat.json`（宿主机 `./data/arkham-heartbeat.json`）。
`GET /health` 的 `poller` 字段可看上次成功抓数时间。

```bash
systemctl enable --now arkham-cdp-watchdog.timer
curl -s http://127.0.0.1:8000/health | python3 -m json.tool
```

## 常见问题

- **怎么判断 CDP 是否可用？**
  `curl http://127.0.0.1:9222/json/version` 返回 JSON 只说明 Chrome 还在听端口。
  还要看 `/health` 里 `poller.ok` 和最近是否 `Home extracted`。
- **VLESS 怎么接入？**
  先用 xray/sing-box 将 VLESS 暴露为本地 SOCKS/HTTP 端口，再把桥接地址写入 `ARKHAM_PROXY_SERVER`。Bot 自建 Chrome 时会传入 `--proxy-server`；CDP 模式则要求被挂接的 Chrome 本身已使用该代理。
- **明明放行了，还是不断被 CF 拦？**
  代理只解决出口和 Cloudflare。首次匿名访问仍需保留已通过验证的 CF cookie；`ARKHAM_CDP_RELOAD=false` 可避免反复触发挑战。
- **为什么日志出现 `Arkham public page in CF backoff`？**
  短时间拦截次数过多自动降频（最长退避 2h），不影响 Telegram 播报链路。

## 目录

| 路径 | 作用 |
|------|------|
| `app/arkham_home_polling.py` | **无 Key 主路径**：首页 RECENT TRANSFERS（CDP 挂接 + 自动点验证框） |
| `app/arkham_polling.py` | Arkham 公开页通用轮询（多策略 DOM/拦截 XHR + CF 通关） |
| `app/arkham_api_polling.py` | 有 Key 时官方 REST 轮询 |
| `app/alerts.py` / `app/formatter.py` | 过滤 → 格式化 → Telegram |
| `app/token_guard.py` | 屏蔽列表、同币冷却、妖币热度 |
| `app/telegram_commands.py` | 告警按钮与 `/mute` 命令 |
| `scripts/arkham_cdp_watchdog.sh` | 宿主看门狗：心跳过期则重启 Chrome |
| `scripts/systemd/` | `arkham-chrome` / watchdog 的 unit |
| `scripts/arkm_headed_unlock.py` | 有头浏览器手动过 CF，保存 cookie 供复用 |
| `scripts/probe_arkm_cf.py` | 探测当前 IP/代理能否过 CF |
| `scripts/run_public_arkm.py` | 不加 Docker、直接 python 跑公开页轮询 |