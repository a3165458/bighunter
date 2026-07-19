FROM python:3.10-slim

# 不生成 .pyc，日志即时输出
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ---- 系统依赖 ----
# Playwright Chromium 所需的系统库（在 python:3.10-slim 上手动补齐）
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium 核心依赖
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libpango-1.0-0 \
    libcairo2 \
    # 字体
    fonts-liberation \
    # 其他工具
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---- Python 依赖 ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- 安装 Playwright Chromium 浏览器 ----
# playwright install 会下载浏览器二进制到 ~/.local/share/ms-playwright
RUN playwright install chromium \
    && playwright install-deps chromium

# ---- 应用代码 ----
COPY . .

# /data 目录用于存放 Arkham storage state（登录态持久化）
RUN mkdir -p /data

EXPOSE 8000

# gunicorn + uvicorn workers，生产就绪
# 单 worker：币安列表刷新 / Arkham 轮询 / 内存去重都是进程内状态，
# 多 worker 会导致后台任务重复执行、告警重复推送
CMD ["gunicorn", "app.main:app", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-b", "0.0.0.0:8000", \
     "-w", "1", \
     "--access-logfile", "-"]
