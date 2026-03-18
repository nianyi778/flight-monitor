# ✈️ Flight Monitor

东京⇄上海机票价格自动监控系统。

## 架构

```
app/
├── config.py       # 配置 & 常量 & 时区(JST)
├── db.py           # TiDB 数据层
├── scraper.py      # Playwright 截图抓取 + 反检测
├── analyzer.py     # LLM 视觉分析（gpt-4o-mini）
├── spring_api.py   # 春秋航空官网 API 直连
├── matcher.py      # 弹性日期搜索 + 航班组合匹配
├── notifier.py     # Telegram 通知 & 消息格式化
├── bot.py          # TG Bot 交互（Inline Keyboard）
├── mcp_server.py   # MCP Server（供其他 AI Agent 调用）
└── scheduler.py    # 智能调度（去重 + 分频）
```

## 功能

### 数据源

| 数据源 | 方式 | 成本 | 准确率 | 覆盖 |
|--------|------|------|--------|------|
| 春秋航空官网 | API 直连 JSON | 零 | 100% | 春秋(9C/IJ) 直销价 |
| 携程 | 截图 + gpt-4o-mini | ~$0.002/张 | ~98% | 全航司 OTA 价 |
| Google Flights JP | 截图 + gpt-4o-mini | ~$0.002/张 | ~98% | 全航司日本站价 |

### 机场覆盖

|  | PVG(浦东) | SHA(虹桥) |
|--|-----------|-----------|
| NRT(成田) | 携程 + 春秋API + Google | 携程 |
| HND(羽田) | 携程 + 春秋API | 携程 |

### 航司覆盖

春秋(9C/IJ)、捷星(GK)、乐桃(MM)、东航(MU)、上航(FM)、国航(CA)、南航(CZ)、吉祥(HO)、ANA(NH)、JAL(JL)

### 智能调度

- **日期去重**：多行程共享相同日期的截图和分析结果
- **分频检查**：<30天每小时 / 30-90天每3小时 / >90天每6小时
- **弹性日期**：自动搜索目标日期 ±N 天，找最便宜的组合
- **随机跳过**：每轮随机跳过部分数据源，降低指纹特征

### 反检测

- `playwright-stealth` 插件（社区维护）
- UA 池 + 分辨率池随机
- 页面间 8-15 秒随机延迟 + 鼠标移动模拟
- 住宅代理（PROXY_URL）支持
- 持久化浏览器 profile（复用指纹）

### TG Bot 交互

菜单命令：
- `/check` — 立即查价
- `/trips` — 行程管理（Inline Keyboard 按钮操作）
- `/status` — 系统状态（模型、代理、行程摘要）
- `/health` — 健康检查（LLM / DB / 代理 / TG）
- `/history` — 价格趋势
- `/help` — 使用帮助

行程管理：
- `/trip add 2026-09-18 2026-09-28 1500 去19-23 回0-6` — 添加（校验 → 预览 → 确认）
- 编辑日期 / 预算 / 时间窗口 / 弹性天数 — 按钮引导
- 暂停 / 恢复 / 删除 — 按钮操作，删除需二次确认

### MCP Server

暴露 Tools 和 Resources，供其他 AI Agent 发现和操作本系统：

**Tools**: `list_trips`, `add_trip`, `edit_trip`, `delete_trip`, `pause_trip`, `resume_trip`, `get_price_history`, `get_cheapest_flights`, `health_check`, `get_system_info`

**Resources**: `trips://active`, `system://status`

连接地址：`http://<host>:8081/sse`

## 部署

### 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `LLM_BASE_URL` | ✅ | LLM API 地址（OpenAI 兼容） |
| `LLM_API_KEY` | ✅ | LLM API Key |
| `LLM_MODEL` | | 模型名，默认 `gpt-4o`（实际分析全部用 `gpt-4o-mini`，此变量仅供 health check 展示） |
| `TG_BOT_TOKEN` | ✅ | Telegram Bot Token |
| `TG_CHAT_ID` | ✅ | Telegram 私聊 Chat ID |
| `TG_GROUP_IDS` | | 群聊 Chat ID（逗号分隔） |
| `CHECK_INTERVAL` | | 基础检查间隔(秒)，默认 `3600` |
| `DB_HOST` | ✅ | TiDB/MySQL 主机 |
| `DB_PORT` | | 端口，默认 `4000` |
| `DB_USER` | ✅ | 数据库用户 |
| `DB_PASSWORD` | ✅ | 数据库密码 |
| `DB_NAME` | | 数据库名，默认 `test` |
| `PROXY_URL` | | 住宅代理，如 `http://ip:port` |
| `MCP_ENABLED` | | MCP Server 开关，默认 `true` |
| `MCP_PORT` | | MCP 端口，默认 `8080` |

### Docker Compose (Dokploy)

```yaml
services:
  flight-monitor:
    image: ghcr.io/nianyi778/flight-monitor:v3.6
    restart: unless-stopped
    ports:
      - "8081:8081"
    environment:
      - LLM_BASE_URL=https://your-api/v1
      - LLM_API_KEY=sk-xxx
      - LLM_MODEL=gpt-4o
      - TG_BOT_TOKEN=xxx
      - TG_CHAT_ID=xxx
      - TG_GROUP_IDS=-100xxx
      - CHECK_INTERVAL=3600
      - DB_HOST=xxx
      - DB_PORT=4000
      - DB_USER=xxx
      - DB_PASSWORD=xxx
      - DB_NAME=test
      - PROXY_URL=http://ip:port
      - MCP_ENABLED=true
      - MCP_PORT=8081
```

### 数据库

需要三张表：`flight_prices`（航班明细）、`check_summary`（巡查汇总）、`trips`（行程配置）。

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
    INDEX idx_price (price_cny)
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
    INDEX idx_check_time (check_time)
);
```

## 常用查询

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

## 版本历史

| 版本 | 主要更新 |
|------|---------|
| v3.8 | 修复日志时区（%(asctime)s 统一显示 JST） |
| v3.7 | LLM 并行分析（4线程），删除死代码 tg_check_ack |
| v3.6 | 全机场覆盖 NRT/HND⇄PVG/SHA |
| v3.5 | 春秋航空官网 API 直连 |
| v3.4 | 全部用 gpt-4o-mini（成本降95%） |
| v3.2 | 智能调度：日期去重 + 分频检查 |
| v3.1 | MCP Server 集成 |
| v2.8 | 弹性日期搜索 |
| v2.7 | 确认收到修复 |
| v2.3 | /health 健康检查 + /status 增强 |
| v2.0 | playwright-stealth + 住宅代理 |
| v1.8 | trip add 严格校验 + 预览确认 |
| v1.6 | TG Inline Keyboard 交互 |
| v1.4 | 模块化架构重构 |
| v1.0 | 初始版本 |
