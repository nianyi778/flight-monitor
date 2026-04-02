# ✈️ Flight Monitor

东京⇄上海机票价格自动监控系统。

## 架构

```
app/
├── config.py             # 配置 & 常量 & 时区(JST)
├── db.py                 # TiDB 数据层（连接池 + SSL）
├── kiwi_api.py           # Kiwi GraphQL 聚合搜索
├── google_flights_api.py # Google Flights（fast-flights protobuf）
├── spring_api.py         # 春秋航空官网 API 直连（9C + IJ）
├── matcher.py            # 弹性日期搜索 + 航班组合匹配
├── search_engine.py      # 搜索引擎（去重、缓存、API 调用编排）
├── alerter.py            # 告警推送 & 确认等待
├── flight_source.py      # API 数据源 Protocol 接口定义
├── notifier.py           # Telegram 通知 & 消息格式化
├── bot.py                # TG Bot 交互（Inline Keyboard + 频率限制）
├── mcp_server.py         # MCP Server（Bearer Token 认证）
├── anti_bot.py           # 反作弊识别 & HTTP 状态分类
├── airports.py           # 机场数据 & 路由对生成
├── source_runtime.py     # 数据源健康管理 & 代理评分
└── scheduler.py          # 智能调度（编排核心）
```

## 功能

### 数据源

| 数据源 | 方式 | 成本 | 覆盖 |
|--------|------|------|------|
| 春秋航空官网 | REST API 直连 | 零 | 春秋中国(9C) + 春秋日本(IJ) 直销价 |
| Kiwi.com | GraphQL API，零认证 | 零 | MU/CA/NH/CZ/HO/FM/9C/IJ 等多航司 |
| Google Flights | CDP 驱动 Chrome sidecar | 零 | 全航司（含 JAL/Peach/GK） |

> **Google 覆盖说明**：JAL(JL)、乐桃(MM)、捷星日本(GK) 仅通过 Google Flights 覆盖。Chrome sidecar 不可用时系统会自动推送 Telegram 降级告警，Spring + Kiwi 渠道不受影响。

### 机场覆盖

| 出发 \ 到达 | PVG(浦东) | SHA(虹桥) | PEK(首都) |
|------------|-----------|-----------|-----------|
| NRT(成田) | Spring + Kiwi + Google | Spring + Kiwi + Google | Kiwi + Google |
| HND(羽田) | Spring + Kiwi + Google | Spring + Kiwi + Google | Kiwi + Google |
| NGO(名古屋) | Spring(IJ) + Google | Google | Google |
| KIX(关西) | Kiwi + Google | Kiwi + Google | Kiwi + Google |

### 航司覆盖

| 航司 | Spring | Kiwi | Google |
|------|:------:|:----:|:------:|
| 春秋中国 9C | ✅ | ✅ | ✅ |
| 春秋日本 IJ | ✅(NGO) | ✅ | ✅ |
| 东航 MU | — | ✅ | ✅ |
| 国航 CA | — | ✅ | ✅ |
| 南航 CZ | — | ✅ | ✅ |
| 吉祥 HO | — | ✅ | ✅ |
| 上航 FM | — | ✅ | ✅ |
| ANA NH | — | ✅ | ✅ |
| JAL JL | — | — | ✅ |
| 乐桃 MM | — | — | ✅ |
| 捷星日本 GK | — | — | ✅ |

### 智能调度

- **去重**：多行程共享相同日期的抓取结果
- **分频检查**：<30天每小时 / 30-90天每3小时 / >90天每6小时
- **弹性日期**：自动搜索目标日期 ±N 天（双向），找最便宜的组合
- **源健康管理**：连续失败自动冷却，恢复后自动重启
- **时间窗过滤**：去程/回程各设时间窗，无时间信息的航班直接收录不丢弃
- **覆盖降级告警**：Google/Chrome 不可用时推送 Telegram 告警（每小时最多一次）

### TG Bot 交互

菜单命令：
- `/check` — 立即查价
- `/trips` — 行程管理（Inline Keyboard 按钮操作）
- `/status` — 系统状态（数据源、代理、行程摘要）
- `/health` — 健康检查（DB / 代理 / TG）
- `/history` — 价格趋势
- `/help` — 使用帮助

行程管理：
- `/trip add 2026-09-18 2026-09-28 1500 去19-23 回0-6` — 添加（校验 → 预览 → 确认）
- 编辑日期 / 预算 / 时间窗口 / 弹性天数 — 按钮引导
- 暂停 / 恢复 / 删除 — 按钮操作，删除需二次确认

### MCP Server

暴露 Tools 和 Resources，供其他 AI Agent 发现和操作本系统：

**Tools**: `list_trips`, `add_trip`, `edit_trip`, `delete_trip`, `pause_trip`, `resume_trip`, `get_price_history`, `get_cheapest_flights`, `health_check`, `get_system_info`, `get_runtime_metrics_snapshot`, `get_metrics_history`

**Resources**: `trips://active`, `system://status`

**HTTP**: `GET http://<host>:8081/health` — 轻量健康检查（适合外部监控轮询）

**认证**: 设置 `MCP_AUTH_TOKEN` 环境变量启用 Bearer Token 认证（未设置则无认证，向后兼容）

SSE 连接地址：`http://<host>:8081/sse`

## 部署

详见 [DEPLOYMENT.md](./DEPLOYMENT.md)，包含从零开始的操作指南、环境变量说明、建表 SQL、Docker Compose 完整示例及常用 SQL 查询。

```bash
# VPS 拉取新镜像并重启
docker compose pull && docker compose up -d --force-recreate flight-monitor

# 本地构建（多架构推送）
docker buildx build --platform linux/amd64,linux/arm64 \
  --output type=image,name=ghcr.io/nianyi778/flight-monitor:v7.0,push=true \
  --network host .
```

## 版本历史

| 版本 | 主要更新 |
|------|---------|
| v7.0 | 全面审计重构：修复中转航班丢失(Kiwi/Google)、弹性日期双向搜索、去重key含路由、连接池(PooledDB)+SSL、非root容器、MCP认证、Bot频率限制、scheduler拆分(search_engine+alerter)、CI加pytest+ruff+多架构、45个单元测试 |
| v6.22 | 修复 get_cheapest_flights 返回历史无关日期数据的 bug（JOIN trips 表按 flight_date ± flex 过滤） |
| v6.21 | Chrome healthcheck（CDP 端口 9222 探活）；Google 覆盖降级 Telegram 告警 |
| v6.20 | spring_api 自动识别 NGO 出发切换 IsIJFlight=true，接入春秋日本(IJ)；扩展 _AIRPORT_NAMES |
| v6.19 | 修复 Google Flights flex 日期 bug（Google 搜索 URL 未随弹性日期循环生成，导致非基准日价格盲区） |
| v6.18 | bot.py 文本命令重构：内联 200 行替换为 asyncio.to_thread(_dispatch_text_command) |
| v6.9 | 引入 Kiwi.com GraphQL 渠道替换 ctrip/letsfg |
| v6.0 | 多机场/城市路由；ob_flex/rt_flex 弹性天数；max_stops 经停过滤；throwaway 甩尾检测 |
| v5.0 | 移除携程数据源；修复 LetsFG 无时间信息航班被过滤；Google Flights import 错误不再缓存 |
| v3.5 | 春秋航空官网 API 直连 |
| v3.2 | 智能调度：日期去重 + 分频检查 |
| v3.1 | MCP Server 集成 |
| v2.8 | 弹性日期搜索 |
| v1.0 | 初始版本 |
