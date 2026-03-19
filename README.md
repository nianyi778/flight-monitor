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

|  | PVG(浦东) |
|--|-----------|
| NRT(成田) | 携程 + 春秋API + Google |
| HND(羽田) | 携程 + 春秋API |

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
- 页面间 5-10 秒随机延迟 + 鼠标移动模拟
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

**HTTP**: `GET http://<host>:8081/health` — 轻量健康检查（不调用 LLM，适合外部监控轮询）

SSE 连接地址：`http://<host>:8081/sse`

## 部署

详见 [DEPLOYMENT.md](./DEPLOYMENT.md)，包含从零开始的 7 步操作指南、环境变量说明、建表 SQL、Docker Compose 完整示例及常用 SQL 查询。

## 版本历史

| 版本 | 主要更新 |
|------|---------|
| v3.15 | 修复春秋API 405：阿里云WAF需要 acw_tc cookie，改用 Session+warm-up GET 共享 cookie |
| v3.14 | 修复截图文件名冲突：多行程同数据源同direction导致后者覆盖前者价格数据，文件名加入 flight_date |
| v3.13 | 抓取提速：砍SHA路线/携程+Google并行双context/等待价格元素替代固定sleep/截图预校验/弹性日期按倒计时收缩 |
| v3.12 | 修复 TG 推送 400：Markdown 失败自动降级纯文本，去掉提醒消息多余 Markdown |
| v3.11 | 修复架构：改为 linux/arm64（服务器为 aarch64） |
| v3.10 | 修复 amd64 构建（改用 docker buildx，避免 OrbStack 混入 arm64 缓存） |
| v3.9 | 新增 GET /health HTTP 端点（DB 状态 + 系统信息，供外部监控轮询，降级时返回 503） |
| v3.8 | 修复日志时区（%(asctime)s 统一显示 JST） |
| v3.7 | LLM 并行分析（4线程），删除死代码 tg_check_ack |
| v3.6 | 全机场覆盖 NRT/HND⇄PVG/SHA |
| v3.5 | 春秋航空官网 API 直连 |
| v3.4 | 全部用 gpt-4o-mini（成本降95%） |
| v3.2 | 智能调度：日期去重 + 分频检查 |
| v3.1 | MCP Server 集成 |
| v2.8 | 弹性日期搜索 |
| v2.0 | playwright-stealth + 住宅代理 |
| v1.6 | TG Inline Keyboard 交互 |
| v1.4 | 模块化架构重构 |
| v1.0 | 初始版本 |
