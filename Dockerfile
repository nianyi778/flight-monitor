FROM python:3.11-slim

WORKDIR /app

# Node.js + Playwright 运行依赖（letsfg 本地 connectors 依赖 Playwright Chromium）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    wget \
    gnupg \
    libnss3 \
    libnspr4 \
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
    libxshmfence1 \
    libx11-xcb1 \
    libgtk-3-0 \
    libxext6 \
    libxrender1 \
    libxi6 \
    libxtst6 \
    libglib2.0-0 \
    libpango-1.0-0 \
    libcairo2 \
    fonts-liberation \
    fonts-noto-color-emoji \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g agent-browser \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "letsfg[cli]" playwright \
    && python -m playwright install chromium

COPY main.py .
COPY app/ app/

ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright
# CHROME_PATH 在运行时由 letsfg 自动探测，无需硬编码版本号

EXPOSE 8081

CMD ["python", "main.py"]
