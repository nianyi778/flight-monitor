FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY flight_monitor.py .

# 持久化目录
VOLUME ["/app/data"]

ENV PYTHONUNBUFFERED=1

CMD ["python", "flight_monitor.py"]
