# ✈️ Flight Monitor

东京⇄上海机票价格自动监控系统。

## 功能

- **多平台抓取**：携程(NRT/HND) + Google Flights(CN/JP)，覆盖春秋、捷星、乐桃、东航、国航、吉祥、ANA、JAL 等航司
- **AI 视觉分析**：GPT-4o 分析截图，提取结构化航班数据
- **Telegram 通知**：低于预算持续推送，常规巡查发送简报
- **TG Bot 交互**：`/check` 立即查价、`/status` 系统状态、`/history` 价格趋势、`/budget` 预算信息
- **数据入库**：航班明细 + 巡查汇总写入 TiDB，支持 SQL 分析
- **反检测**：Playwright stealth + 持久化指纹 + 随机行为模拟
- **反杀熟**：中日双站交叉比价（CNY/JPY）

## 部署

### 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `LLM_BASE_URL` | ✅ | LLM API 地址 |
| `LLM_API_KEY` | ✅ | LLM API Key |
| `LLM_MODEL` | | 模型名，默认 `gpt-4o` |
| `TG_BOT_TOKEN` | ✅ | Telegram Bot Token |
| `TG_CHAT_ID` | ✅ | Telegram Chat ID |
| `OUTBOUND_DATE` | ✅ | 去程日期 `YYYY-MM-DD` |
| `RETURN_DATE` | ✅ | 回程日期 `YYYY-MM-DD` |
| `BUDGET_TOTAL` | | 往返预算(CNY)，默认 `1500` |
| `CHECK_INTERVAL` | | 检查间隔(秒)，默认 `3600` |
| `DB_HOST` | ✅ | TiDB/MySQL 主机 |
| `DB_PORT` | | 端口，默认 `4000` |
| `DB_USER` | ✅ | 数据库用户 |
| `DB_PASSWORD` | ✅ | 数据库密码 |
| `DB_NAME` | | 数据库名，默认 `test` |

### Docker Compose

```yaml
services:
  flight-monitor:
    image: ghcr.io/nianyi778/flight-monitor:v1.1
    restart: unless-stopped
    environment:
      - LLM_BASE_URL=xxx
      - LLM_API_KEY=xxx
      - TG_BOT_TOKEN=xxx
      - TG_CHAT_ID=xxx
      - OUTBOUND_DATE=2026-09-18
      - RETURN_DATE=2026-09-27
      - BUDGET_TOTAL=1500
      - CHECK_INTERVAL=3600
      - DB_HOST=xxx
      - DB_USER=xxx
      - DB_PASSWORD=xxx
    volumes:
      - flight-data:/app/data

volumes:
  flight-data:
```

### 数据库表

建表 SQL 见首次启动日志，或手动执行：

```sql
-- 航班明细
CREATE TABLE flight_prices (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
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
    INDEX idx_direction_date (direction, flight_date),
    INDEX idx_airline (airline),
    INDEX idx_price (price_cny)
);

-- 巡查汇总
CREATE TABLE check_summary (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    check_time DATETIME NOT NULL,
    best_total INT,
    outbound_lowest INT,
    return_lowest INT,
    best_outbound_airline VARCHAR(50),
    best_return_airline VARCHAR(50),
    flights_found INT DEFAULT 0,
    INDEX idx_check_time (check_time)
);
```

## 常用查询

```sql
-- 某航司价格趋势
SELECT check_time, price_cny FROM flight_prices
WHERE airline LIKE '%春秋%' AND direction='outbound' ORDER BY check_time;

-- 每日最低往返价
SELECT DATE(check_time) AS day, MIN(best_total) AS min_total
FROM check_summary GROUP BY day ORDER BY day;

-- 最便宜的10个往返组合
SELECT a.airline AS ob_airline, a.price_cny AS ob_price,
       b.airline AS rt_airline, b.price_cny AS rt_price,
       a.price_cny + b.price_cny AS total
FROM flight_prices a, flight_prices b
WHERE a.direction='outbound' AND b.direction='return'
  AND a.check_time = b.check_time
ORDER BY total LIMIT 10;
```
