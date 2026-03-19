FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 如需启用截图兜底，取消注释以下两行（镜像会增加约 1.3GB）：
# RUN python -m playwright install --with-deps chromium \
#     && apt-get install -y --no-install-recommends fonts-noto-cjk \
#     && rm -rf /var/lib/apt/lists/*

COPY main.py .
COPY app/ app/

# 持久化目录
VOLUME ["/app/data"]

ENV PYTHONUNBUFFERED=1

EXPOSE 8081

CMD ["python", "main.py"]
