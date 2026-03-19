FROM python:3.11-slim

WORKDIR /app

# 安装 Node.js 20 LTS（携程 DOM 抓取需要 agent-browser / npx）
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g agent-browser \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY app/ app/

# 持久化目录
VOLUME ["/app/data"]

ENV PYTHONUNBUFFERED=1

EXPOSE 8081

CMD ["python", "main.py"]
