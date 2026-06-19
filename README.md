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
Starting Arkham polling | url=... | interval=30s | headless=True
```

### 3. 本地开发

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## 配置 Arkham Webhook

1. 登录 [Arkham Intelligence](https://platform.arkhamintelligence.com/)
2. 创建一个 Alert，设置监控条件
3. 在 Alert 的 Webhook 选项中填入你的公网地址：

```
https://你的域名或IP:8000/webhook
```

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

## 页面轮询排障

如果 webhook 正常、但页面轮询没抓到数据，优先检查：

1. `ARKHAM_TRANSFERS_URL` 是否真的是你筛选后的最终页面 URL
2. `./data/arkham-storage-state.json` 是否存在
3. 日志里是否出现 `All extraction strategies failed`
4. 你的筛选结果是否本身就是主流币，或低于 `MIN_USD_VALUE`
5. Arkham 页面是否改版导致 DOM 结构变化

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
