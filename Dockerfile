FROM python:3.11-slim

# 系统依赖（Playwright Chromium 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libxshmfence1 \
    fonts-noto-cjk fonts-noto-cjk-extra \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium

COPY flight_monitor.py .

# 持久化目录
VOLUME ["/app/data"]

ENV PYTHONUNBUFFERED=1

CMD ["python", "flight_monitor.py"]
