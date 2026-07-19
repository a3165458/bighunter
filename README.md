# 🐋 WhaleScope — 冷门代币链上异动监控 Bot

通过接收 [Arkham](https://www.arkhamintelligence.com/) Webhook 告警，或主动轮询 **Arkham 筛选转账页**，过滤主流代币噪音，仅针对 **冷门代币** 大额异动发送 Telegram 中文告警。

## 功能特性

| 功能 | 说明 |
|------|------|
| 🔍 主流币过滤 | 自动拦截 BTC/ETH/USDT/USDC 等 16 种主流代币 |
| 💰 动态阈值 | 仅处理 ≥ MIN_USD_VALUE（默认 $50,000）的交易 |
| 🏦 流向识别 | 自动判断 CEX 充值/提现/巨鲸对转 |
| 🔗 快捷链接 | 自动拼装 DexScreener / Etherscan / Arkham 链接 |
| 📱 精美推送 | MarkdownV2 格式 + Emoji 中文告警 |
| 👀 页面轮询 | 主动盯 Arkham transfers/filter 页面 |
| 🐳 Docker 部署 | 一键 `docker-compose up -d` |

## 快速开始

### 1. 准备环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入你的 Telegram Bot Token 和 Chat ID：

```env
TELEGRAM_BOT_TOKEN=你的Bot Token
TELEGRAM_CHAT_ID=你的Chat ID
MIN_USD_VALUE=50000
```

如果你想启用“盯 Arkham 页面”模式，再额外配置：

```env
ARKHAM_POLLING_ENABLED=true
ARKHAM_TRANSFERS_URL=https://platform.arkhamintelligence.com/...
ARKHAM_POLL_INTERVAL_SECONDS=30
ARKHAM_STORAGE_STATE_PATH=/data/arkham-storage-state.json
ARKHAM_HEADLESS=true
```

> **如何获取 Bot Token？** 在 Telegram 中找 [@BotFather](https://t.me/BotFather)，发送 `/newbot` 创建。
>
> **如何获取 Chat ID？** 将 Bot 加入目标群组，访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` 查看。

### 2. Docker 部署（推荐）

```bash
docker-compose up -d --build
```

查看日志：

```bash
docker-compose logs -f whalescope-bot
```

停止服务：

```bash
docker-compose down
```

## 两种模式怎么选

| 模式 | 优点 | 缺点 | 适用场景 |
|------|------|------|----------|
| Webhook | 最稳、最轻、实时性好 | 依赖 Arkham 原生告警能力 | 已能在 Arkham 配出目标提醒 |
| 页面轮询 | 能直接复用你在 Arkham 页面手工筛好的结果 | 依赖浏览器和登录态，页面改版后可能失效 | 页面能筛到，但 webhook 配不出来 |

## 配置 Arkham 页面轮询

### 1. 先在 Arkham 页面手工筛好条件

在 Arkham 首页或 Transfers 页面，把你想看的条件都筛好，然后复制最终页面 URL，填到：

```env
ARKHAM_TRANSFERS_URL=...
```

### 2. 准备 Arkham 登录态（推荐）

Arkham 对直接 HTTP 抓取通常返回 403，所以轮询模式走的是 **Playwright 真浏览器**。如果页面需要登录，推荐你把登录态文件放到：

```bash
./data/arkham-storage-state.json
```

容器启动时会自动挂载到：

```bash
/data/arkham-storage-state.json
```

> 没有 storage state 也能尝试打开页面，但遇到登录墙、验证码或 Cloudflare 时成功率会下降。

### 3. 启动页面轮询

```bash
mkdir -p data
docker-compose up -d --build
docker-compose logs -f whalescope-bot
```

日志里会看到类似：

```text
WhaleScope Bot started | ... | polling=enabled
Starting Arkham polling | url=... | interval=30s | mode=launch(headless=True)
```

### 4. CDP 模式（推荐：挂接已通过验证的 Chrome）

arkm.com 由 Cloudflare 保护，数据中心 IP 上自建的无头浏览器通常会被交互式人机验证拦住。
CDP 模式改为**挂接一个已在运行的真实 Chrome**：人先在浏览器里手动过一次验证，
Bot 之后只驱动这个浏览器定时刷新、提取数据。

服务器上的参考搭建（Xvfb 虚拟屏 + 真 Chrome + noVNC 远程操作）：

```bash
# 1. 虚拟显示器 + Chrome（调试端口 9222，profile 持久化到 ./data）
Xvfb :99 -screen 0 1440x900x24 &
DISPLAY=:99 google-chrome --no-sandbox --user-data-dir=./data/chrome-profile \
  --remote-debugging-port=9222 --window-size=1440,860 https://arkm.com/transfers &

# 2. noVNC 远程桌面（浏览器访问 http://服务器IP:6080/vnc.html 手动过验证）
x11vnc -display :99 -rfbport 5901 -listen 127.0.0.1 -forever -shared -quiet &
websockify --web /usr/share/novnc 0.0.0.0:6080 127.0.0.1:5901 &

# 3. 把 9222 转发给容器网段（Chrome 调试端口只监听 127.0.0.1）
socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222 &
```

然后在 `.env` 配置：

```env
ARKHAM_POLLING_ENABLED=true
ARKHAM_TRANSFERS_URL=https://arkm.com/transfers
ARKHAM_CDP_URL=http://172.17.0.1:9223   # docker 网桥网关地址，按实际网段调整
```

CDP 模式的行为：

- 每轮轮询先检查页面是否停在 Cloudflare 验证页——**是则暂停轮询并打日志提醒**，
  等你在 noVNC 里手动点完验证后自动恢复（暂停期间不会刷新页面打断你的操作）
- 正常页面每轮 `reload` 拿最新数据（clearance cookie 有效期内刷新不会重新触发验证）
- transfers 标签页被重定向或误关时，会自动导航回去

## 本地开发

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## 配置 Arkham Webhook

1. 登录 [Arkham Intelligence](https://platform.arkhamintelligence.com/)
2. 创建一个 Alert，设置监控条件
3. 在 Alert 的 Webhook 选项中填入你的公网地址：

```
http://你的域名或IP:8000/webhook
```

如果设置了 `WEBHOOK_SECRET`，鉴权支持两种方式（任选其一）：

- 请求 Header：`Arkham-Webhook-Token: <你的密钥>`
- URL 查询参数（适合无法自定义 Header 的平台）：

```
http://你的域名或IP:8000/webhook?token=<你的密钥>
```

> **注意防火墙**：确认服务器防火墙（ufw / 云安全组）放行了 8000 端口，否则 Arkham 的请求根本到不了服务器。

## 验证 Telegram 推送链路

部署完成后可先发一条测试消息，确认 Bot Token / Chat ID / Topic 配置无误：

```bash
curl -X POST http://localhost:8000/test-telegram
```

预期返回 `{"status": "sent", "telegram_message_id": ...}`，同时群里会收到一条测试消息。

## 本地测试（ngrok / cpolar）

开发环境通常没有公网 IP，可以用内网穿透工具暴露本地端口。

### 方案 A：ngrok

```bash
# 安装 ngrok（https://ngrok.com/download）
ngrok http 8000

# 输出类似：
# Forwarding  https://xxxx.ngrok-free.app -> http://localhost:8000
# 将 https://xxxx.ngrok-free.app/webhook 填入 Arkham Webhook URL
```

### 方案 B：cpolar

```bash
# 安装 cpolar（https://www.cpolar.com/）
cpolar http 8000

# 输出类似：
# https://xxxx.cpolar.cn -> http://localhost:8000
# 将 https://xxxx.cpolar.cn/webhook 填入 Arkham Webhook URL
```

### 手动测试 Webhook

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "tokenSymbol": "PEPE",
    "usdValue": 125000,
    "fromAddressLabel": "Binance",
    "toAddressLabel": "Unknown Whale",
    "fromAddress": "0x28C6c06298d514Db089934071355E5743bf21d60",
    "toAddress": "0x1234567890abcdef1234567890abcdef12345678",
    "blockchain": "ethereum",
    "arkhamUrl": "https://platform.arkhamintelligence.com/tx/0xabc123",
    "unitAmount": 1000000000
  }'
```

预期返回：

```json
{"status": "sent", "token": "PEPE", "usd_value": 125000, "telegram_message_id": 123}
```

## 排障清单

完全收不到告警时，按顺序检查：

1. **容器是否在运行**：`docker ps` 里 `whalescope-bot` 是否为 `Up` 状态（`docker-compose up -d` 启动）
2. **Telegram 链路**：`curl -X POST http://localhost:8000/test-telegram` 群里是否收到测试消息
3. **防火墙**：`ufw status` 是否放行 8000 端口（webhook 模式必须）
4. **Arkham 侧**：Alert 是否创建且启用、Webhook URL 是否填对（含 `?token=` 密钥）
5. **过滤条件**：目标代币是否被 `MIN_USD_VALUE` / 主流币黑名单 / 币安上币过滤（`REQUIRE_BINANCE_LISTING`）拦下——日志里会打印 `Skipped ... : <原因>`

### 页面轮询专项

如果 webhook 正常、但页面轮询没抓到数据，优先检查：

1. `ARKHAM_TRANSFERS_URL` 是否真的是你筛选后的最终页面 URL
2. 日志里是否出现 `Cloudflare challenge ... polling is paused`——需要打开
   `http://服务器IP:6080/vnc.html` 手动完成人机验证，之后轮询自动恢复
3. 日志里是否出现 `DOM extraction strategies found no transfers`（页面结构变化
   或未渲染完成；`ARKHAM_TEXT_FALLBACK=true` 可开启全页文本兜底，但可能误报）
4. 你的筛选结果是否本身就是主流币，或低于 `MIN_USD_VALUE`
5. 非 CDP 模式下：`./data/arkham-storage-state.json` 登录态文件是否存在

> 注：轮询模式重启后的**第一轮**只登记页面上已有的记录、不推送（防止旧转账重播），从第二轮开始播报新增记录。

## 项目结构

```
bighunter/
├── app/
│   ├── __init__.py
│   ├── alerts.py       # Webhook / Polling 共用处理管线
│   ├── arkham_polling.py # Arkham 页面轮询服务（Playwright）
│   ├── config.py       # 配置加载（pydantic-settings）
│   ├── formatter.py    # Telegram 消息格式化
│   └── main.py         # FastAPI 路由 & 推送逻辑
├── .env.example        # 环境变量模板
├── requirements.txt    # Python 依赖
├── Dockerfile          # 容器构建
├── docker-compose.yml  # 编排部署
└── README.md           # 本文件
```

## Arkham Webhook Payload 参考

```json
{
  "tokenSymbol": "PEPE",
  "usdValue": 125000,
  "fromAddressLabel": "Binance",
  "toAddressLabel": "Unknown Whale",
  "fromAddress": "0x28C6c06298d514Db089934071355E5743bf21d60",
  "toAddress": "0x1234567890abcdef1234567890abcdef12345678",
  "blockchain": "ethereum",
  "arkhamUrl": "https://platform.arkhamintelligence.com/tx/0xabc123",
  "unitAmount": 1000000000
}
```

## License

MIT
