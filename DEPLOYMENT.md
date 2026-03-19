# 部署手册

## 第一步：创建 Telegram Bot

1. 在 Telegram 搜索 `@BotFather`，发送 `/newbot`，按提示填写名称
2. 保存返回的 **Bot Token**（格式 `123456789:AABBcc...`）
3. 给 Bot 发任意消息，然后访问：
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   在返回 JSON 里找 `"chat":{"id":...}`，即为你的 **Chat ID**（私聊为正数，群聊为负数）

## 第二步：创建数据库

推荐 [TiDB Cloud Serverless](https://tidbcloud.com)（有免费额度）。

1. 注册并创建 Serverless Cluster，记录连接信息（host / port / user / password）
2. 在 SQL Editor 执行下方"建表 SQL"章节的三条语句

也可使用任意 MySQL 5.7+ 实例，端口默认 `3306`（TiDB Cloud 默认 `4000`）。

## 第三步：准备 LLM API Key

系统使用 `gpt-4o-mini` 对截图做视觉分析，需要支持 vision 的 OpenAI 兼容 API。

- OpenAI 官方：`https://api.openai.com/v1`，在 [platform.openai.com](https://platform.openai.com) 创建 API Key
- 其他兼容服务（Azure OpenAI、第三方代理等）：填对应的 `LLM_BASE_URL` 和 `LLM_API_KEY` 即可

## 第四步：部署容器

```bash
# 1. 克隆仓库
git clone https://github.com/nianyi778/flight-monitor-docker.git
cd flight-monitor-docker

# 2. 复制配置文件
cp .env.example .env

# 3. 编辑 .env，填入真实值
#    必填：LLM_BASE_URL / LLM_API_KEY / TG_BOT_TOKEN / TG_CHAT_ID / DB_HOST / DB_USER / DB_PASSWORD
vim .env

# 4. 启动
docker compose up -d

# 5. 查看日志（确认正常启动）
docker compose logs -f
```

正常启动日志示例：
```
🔌 MCP Server started on port 8081
✈️  Flight Monitor 启动 (#1)
📋 监控行程: 0 个
```

## 第五步：建表 SQL

在数据库执行以下三条语句（首次部署执行一次即可）：

```sql
CREATE TABLE trips (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    outbound_date DATE NOT NULL,
    return_date DATE NOT NULL,
    budget INT NOT NULL DEFAULT 1500,
    status VARCHAR(10) DEFAULT 'active',
    best_price INT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    outbound_depart_start INT DEFAULT 19,
    outbound_depart_end INT DEFAULT 23,
    return_arrive_start INT DEFAULT 0,
    return_arrive_end INT DEFAULT 6,
    outbound_flex INT DEFAULT 0,
    return_flex INT DEFAULT 1,
    INDEX idx_status (status)
);

CREATE TABLE flight_prices (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    trip_id BIGINT,
    check_time DATETIME NOT NULL,
    direction VARCHAR(10) NOT NULL,
    source VARCHAR(30) NOT NULL,
    airline VARCHAR(50),
    flight_no VARCHAR(20),
    departure_time VARCHAR(10),
    arrival_time VARCHAR(10),
    origin VARCHAR(5),
    destination VARCHAR(5),
    price_cny INT NOT NULL,
    original_price INT,
    original_currency VARCHAR(5) DEFAULT 'CNY',
    stops INT DEFAULT 0,
    flight_date DATE NOT NULL,
    INDEX idx_check_time (check_time),
    INDEX idx_trip (trip_id),
    INDEX idx_price (price_cny),
    INDEX idx_trip_dir_price (trip_id, direction, price_cny)
);

CREATE TABLE check_summary (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    trip_id BIGINT,
    check_time DATETIME NOT NULL,
    best_total INT,
    outbound_lowest INT,
    return_lowest INT,
    best_outbound_airline VARCHAR(50),
    best_return_airline VARCHAR(50),
    flights_found INT DEFAULT 0,
    INDEX idx_check_time (check_time),
    INDEX idx_trip_time (trip_id, check_time)
);
```

## 第六步：添加监控行程

容器启动后，在 Telegram 与 Bot 对话：

```
# 查看帮助
/help

# 添加行程：去程日期 回程日期 预算 去程时间窗口 回程时间窗口
/trip add 2026-09-18 2026-09-28 1500 去19-23 回0-6

# 立即触发一次查价（验证配置是否正确）
/check

# 查看系统状态
/status

# 查看健康状态（LLM / DB / 代理 / TG 连通性）
/health
```

## 第七步：验证部署

```bash
# HTTP 健康检查（返回 200 表示正常，503 表示 DB 异常）
curl http://localhost:8081/health
```

返回示例：
```json
{
  "status": "ok",
  "database": "ok",
  "active_trips": 1,
  "check_count": 3,
  "server_time_jst": "2026-03-19 10:00:00"
}
```

---

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `LLM_BASE_URL` | ✅ | — | LLM API 地址（OpenAI 兼容） |
| `LLM_API_KEY` | ✅ | — | LLM API Key |
| `LLM_MODEL` | | `gpt-4o-mini` | 仅供 health check 连通性测试；实际截图分析固定使用 `gpt-4o-mini` |
| `TG_BOT_TOKEN` | ✅ | — | Telegram Bot Token |
| `TG_CHAT_ID` | ✅ | — | Telegram 私聊 Chat ID |
| `TG_GROUP_IDS` | | — | 群聊 Chat ID（逗号分隔，如 `-100123,-100456`） |
| `DB_HOST` | ✅ | — | TiDB/MySQL 主机 |
| `DB_PORT` | | `4000` | 端口 |
| `DB_USER` | ✅ | — | 数据库用户 |
| `DB_PASSWORD` | ✅ | — | 数据库密码 |
| `DB_NAME` | | `flight_monitor` | 数据库名 |
| `CHECK_INTERVAL` | | `3600` | 基础检查间隔(秒)；实际按倒计时分频 |
| `PUSH_INTERVAL` | | `3600` | 价格提醒推送间隔(秒) |
| `PROXY_URL` | | — | 住宅代理，如 `http://user:pass@host:port` |
| `MCP_ENABLED` | | `true` | MCP Server 开关 |
| `MCP_PORT` | | `8081` | MCP Server 端口 |

## Docker Compose 完整示例

```yaml
services:
  flight-monitor:
    image: ghcr.io/nianyi778/flight-monitor:v3.15
    restart: unless-stopped
    ports:
      - "8081:8081"
    environment:
      - LLM_BASE_URL=https://api.openai.com/v1
      - LLM_API_KEY=sk-xxx
      - LLM_MODEL=gpt-4o-mini
      - TG_BOT_TOKEN=xxx
      - TG_CHAT_ID=xxx
      - TG_GROUP_IDS=-100xxx
      - DB_HOST=xxx.tidbcloud.com
      - DB_PORT=4000
      - DB_USER=xxx
      - DB_PASSWORD=xxx
      - DB_NAME=flight_monitor
      - CHECK_INTERVAL=3600
      - PUSH_INTERVAL=3600
      - PROXY_URL=http://user:pass@host:port
      - MCP_ENABLED=true
      - MCP_PORT=8081
    volumes:
      - flight-data:/app/data
    deploy:
      resources:
        limits:
          memory: 2G
    shm_size: "1gb"

volumes:
  flight-data:
```

## 可选：住宅代理

携程和 Google Flights 有反爬检测，长期使用建议配置住宅代理：

```env
PROXY_URL=http://user:pass@host:port
# 或 socks5://user:pass@host:port
```

代理 IP 验证：TG 发送 `/health`，返回结果中 `proxy.exit_ip` 应为代理出口 IP。

## 可选：MCP Server 接入

将 AI Agent（如 Claude、OpenClaw）指向 SSE 地址即可：

```
http://<your-host>:8081/sse
```

Agent 可调用：查行程、加行程、改行程、删行程、查价格历史、健康检查等。

## 常用 SQL 查询

```sql
-- 某航司价格趋势
SELECT check_time, price_cny, origin, destination
FROM flight_prices
WHERE airline LIKE '%春秋%' AND direction='outbound'
ORDER BY check_time;

-- 每日最低往返价
SELECT DATE(check_time) AS day, MIN(best_total) AS min_total
FROM check_summary GROUP BY day ORDER BY day;

-- OTA vs 官网价格对比
SELECT DATE(check_time) AS day,
    MIN(CASE WHEN source LIKE '%官网%' THEN price_cny END) AS spring_direct,
    MIN(CASE WHEN source LIKE '%携程%' THEN price_cny END) AS ctrip_ota
FROM flight_prices
WHERE airline LIKE '%春秋%' AND direction='outbound'
GROUP BY day ORDER BY day;

-- 各机场组合最低价
SELECT origin, destination, MIN(price_cny) AS min_price, COUNT(*) AS samples
FROM flight_prices
WHERE direction='outbound'
GROUP BY origin, destination
ORDER BY min_price;
```
