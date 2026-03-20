# ✈️ Flight Monitor

东京⇄上海机票价格自动监控系统。

## 架构

```
app/
├── config.py             # 配置 & 常量 & 时区(JST)
├── db.py                 # TiDB 数据层
├── letsfg_api.py         # LetsFG CLI/SDK 聚合搜索
├── google_flights_api.py # Google Flights fast-flights protobuf
├── spring_api.py         # 春秋航空官网 API 直连
├── matcher.py            # 弹性日期搜索 + 航班组合匹配
├── notifier.py           # Telegram 通知 & 消息格式化
├── bot.py                # TG Bot 交互（Inline Keyboard）
├── mcp_server.py         # MCP Server（供其他 AI Agent 调用）
└── scheduler.py          # 智能调度（去重 + 分频）
```

## 功能

### 数据源

| 数据源 | 方式 | 成本 | 覆盖 |
|--------|------|------|------|
| 春秋航空官网 | REST API 直连 | 零 | 春秋(9C/IJ) 直销价 |
| LetsFG | CLI/SDK（100+ connectors + 可选 GDS/NDC） | 免费 | 多航司聚合价 |
| 携程 | browser DOM（CDP 连接 Chrome sidecar） | 零 | 全航司 OTA 价 |
| Google Flights | fast-flights protobuf 逆向，纯 HTTP | 零 | 全航司日本站价 |

> **携程依赖**：需要 Chrome sidecar 容器（compose 内 `flight-chrome`）+ 有效的 `ctrip_batch_profile.json`。profile 失效时运行 `capture_real_chrome.sh` 重新捕获。

### 机场覆盖

|  | PVG(浦东) |
|--|-----------|
| NRT(成田) | LetsFG + Google + 春秋 |
| HND(羽田) | LetsFG + Google + 春秋 |

### 航司覆盖

春秋(9C/IJ)、捷星(GK)、乐桃(MM)、东航(MU)、上航(FM)、国航(CA)、南航(CZ)、吉祥(HO)、ANA(NH)、JAL(JL)

### 智能调度

- **去重**：多行程共享相同日期的抓取结果
- **分频检查**：<30天每小时 / 30-90天每3小时 / >90天每6小时
- **弹性日期**：自动搜索目标日期 ±N 天，找最便宜的组合
- **源健康管理**：连续失败自动冷却，恢复后自动重启
- **时间窗过滤**：去程/回程各设时间窗，无时间信息的航班直接收录不丢弃

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

SSE 连接地址：`http://<host>:8081/sse`

## 部署

详见 [DEPLOYMENT.md](./DEPLOYMENT.md)，包含从零开始的操作指南、环境变量说明、建表 SQL、Docker Compose 完整示例及常用 SQL 查询。

```bash
# VPS 拉取新镜像并重启
docker compose pull && docker compose up -d --force-recreate flight-monitor

# 本地构建（多架构推送）
docker buildx build --platform linux/amd64,linux/arm64 \
  --output type=image,name=ghcr.io/nianyi778/flight-monitor:v5.0,push=true \
  --network host .
```

## LetsFG

可选增强源。在 Docker 镜像内已内置 `letsfg`、`playwright` 和 Chromium，本地 connectors 可直接运行。

环境变量：
```bash
LETSFG_API_KEY=trav_xxx   # 可选；有值则优先走 letsfg search（云端）
LETSFG_MODE=auto          # auto/local/cloud
LETSFG_TIMEOUT=90
```

如配置 `LETSFG_API_KEY`，优先走 `letsfg search`；否则默认走 `letsfg search-local`。

## 版本历史

| 版本 | 主要更新 |
|------|---------|
| v5.0 | 移除携程数据源（接口下线）；修复 LetsFG 无时间信息航班被过滤导致"无数据"；Google Flights import 错误不再缓存；移除镜像中 agent-browser 和 COPY data/；fast-flights 固定 <3.0 |
| v4.9 | LetsFG 多 connector 聚合；源健康管理优化 |
| v4.3 | 移除截图+LLM兜底；Google 适配 fast-flights v2.2 新接口 |
| v4.2 | 携程降级链重构 |
| v3.9 | 新增 GET /health HTTP 端点 |
| v3.5 | 春秋航空官网 API 直连 |
| v3.2 | 智能调度：日期去重 + 分频检查 |
| v3.1 | MCP Server 集成 |
| v2.8 | 弹性日期搜索 |
| v1.0 | 初始版本 |
