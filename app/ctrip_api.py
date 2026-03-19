"""
携程航班抓取 — DOM 模式
- 通过 agent-browser 打开搜索页，从页面 SSR 注入的 window state / DOM 提取全量航班数据
- 无 API token 依赖，稳定可靠
- DOM 抓取失败时直接标记 blocked，由调度器负责告警
"""

import json
import os
import random
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from app.anti_bot import finalize_result_status, make_result
from app.config import log

_BASE_URL = "https://flights.ctrip.com"
_BROWSER_SESSION = "ctrip-api-live"
# 设置此变量可让 agent-browser 通过 CDP 连接真实 Chrome（本机或 Docker host）
# Docker 内使用: CTRIP_CDP_PORT=host.docker.internal:9222
_CDP_PORT = os.getenv("CTRIP_CDP_PORT", "")  # e.g. "9222" or "host.docker.internal:9222"

_CITY_META = {
    "NRT": {
        "city_code": "TYO",
        "city_name": "东京",
        "country_id": 78,
        "country_code": "JP",
        "country_name": "日本",
        "province_id": 0,
        "city_id": 228,
        "timezone": 540,
        "airport_name": "成田国际机场",
    },
    "HND": {
        "city_code": "TYO",
        "city_name": "东京",
        "country_id": 78,
        "country_code": "JP",
        "country_name": "日本",
        "province_id": 0,
        "city_id": 228,
        "timezone": 540,
        "airport_name": "羽田机场",
    },
    "PVG": {
        "city_code": "SHA",
        "city_name": "上海",
        "country_id": 1,
        "country_code": "CN",
        "country_name": "中国",
        "province_id": 2,
        "city_id": 2,
        "timezone": 480,
        "airport_name": "浦东国际机场",
    },
    "SHA": {
        "city_code": "SHA",
        "city_name": "上海",
        "country_id": 1,
        "country_code": "CN",
        "country_name": "中国",
        "province_id": 2,
        "city_id": 2,
        "timezone": 480,
        "airport_name": "虹桥国际机场",
    },
}


def _run_agent_browser(args: list[str]) -> str:
    if _CDP_PORT:
        ab = shutil.which("agent-browser")
        if ab:
            parts = [ab, "--cdp", _CDP_PORT, *args]
        else:
            parts = ["npx", "-y", "agent-browser", "--cdp", _CDP_PORT, *args]
    else:
        parts = ["npx", "-y", "agent-browser", "--session", _BROWSER_SESSION, *args]
    cmd = " ".join(shlex.quote(part) for part in parts)
    env = dict(os.environ)
    agent_browser_home = Path(env.get("AGENT_BROWSER_HOME") or "/tmp/agent-browser-home")
    agent_browser_home.mkdir(parents=True, exist_ok=True)
    env["AGENT_BROWSER_HOME"] = str(agent_browser_home)
    env["HOME"] = str(agent_browser_home)
    # Docker 内通常只有 /bin/sh，macOS 上有 /bin/zsh
    shell = "/bin/zsh" if Path("/bin/zsh").exists() else "/bin/sh"
    proc = subprocess.run(
        [shell, "-lc", cmd],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"agent-browser 失败: {cmd}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return (proc.stdout or "").strip()


def _extract_last_json_blob(text: str):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("{") or line.startswith("[") or line.startswith('"'):
            try:
                return json.loads(line)
            except Exception:
                continue
    raise ValueError(f"未找到可解析 JSON 输出: {text[:500]}")


def _normalize_json_object(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _canonical_search_url(origin: str, destination: str, date_str: str) -> str:
    ob = _CITY_META.get(origin, {})
    rt = _CITY_META.get(destination, {})
    pair = f"{ob.get('city_code', origin).lower()}-{rt.get('city_code', destination).lower()}"
    return (
        f"{_BASE_URL}/online/list/oneway-{pair}"
        f"?depdate={date_str}&cabin=y_s&adult=1&child=0&infant=0&containstax=1"
    )


def _browser_dom_scrape_flights(search_url: str, origin: str, destination: str) -> list[dict]:
    """在浏览器中打开搜索页，从 DOM / window state 提取已渲染的全量航班数据。

    策略（按可靠性排序）:
    1. window.__NEXT_DATA__ / window.__INITIAL_STATE__ 等服务端注入的 JSON
    2. <script type="application/json"> 内嵌数据块
    3. DOM 结构化选择器（多套备选，覆盖携程各 class 命名风格）
    4. 全文 innerText 正则兜底（价格 + 时间对）
    """
    if not (shutil.which("agent-browser") or shutil.which("npx")):
        raise RuntimeError("agent-browser / npx 未安装，无法执行 DOM 抓取")
    _run_agent_browser(["open", search_url])
    _run_agent_browser(["wait", "10000"])
    # 滚动触发懒加载，携程列表有虚拟滚动
    _run_agent_browser(["scroll", "down", "3000"])
    _run_agent_browser(["wait", "3000"])
    _run_agent_browser(["scroll", "down", "3000"])
    _run_agent_browser(["wait", "2000"])

    # ── 策略 1: 提取页面内嵌 window state JSON（最全，携程 SSR 注入）──
    js_state = r"""(() => {
      // 尝试多个携程常见的全局 state key
      const candidates = [
        window.__NEXT_DATA__,
        window.__INITIAL_STATE__,
        window.__serverData,
        window.FlightSearchData,
        window.GlobalFlightList,
      ];
      for (const c of candidates) {
        if (c && typeof c === 'object') {
          const s = JSON.stringify(c);
          // 含 flightNo / price 的才是真正航班数据
          if (s.includes('flightNo') || s.includes('price')) return s;
        }
      }
      // 尝试 <script id="__NEXT_DATA__"> 或类似标签
      for (const sel of ['#__NEXT_DATA__', 'script[type="application/json"]']) {
        const el = document.querySelector(sel);
        if (el && el.textContent.includes('flightNo')) return el.textContent;
      }
      return null;
    })()"""
    try:
        raw_state = _extract_last_json_blob(_run_agent_browser(["eval", js_state]))
        state_obj = _normalize_json_object(raw_state)
        flights_from_state = _extract_flights_from_state(state_obj, origin, destination)
        if flights_from_state:
            log.info(f"  DOM 策略1(window state): {len(flights_from_state)} 条")
            return sorted(flights_from_state, key=lambda x: x.get("price_cny") or 99999)
    except Exception as e:
        log.debug(f"  DOM 策略1 失败: {e}")

    # ── 策略 2: DOM 多套选择器（携程用了多套 class 风格）──
    js_dom = r"""(() => {
      // 携程实际使用的 class 片段（通过 DevTools 验证）:
      // .flight-list-item, .flt_list_C, [class*="FlightItem"], [class*="list-item"]
      // 也有可能是 li[data-id] 等数据属性
      const SELECTORS = [
        '[class*="FlightItem"]',
        '[class*="flight-list"] > li',
        '[class*="flt_list"] li',
        '[class*="list-item--"]',
        'li[data-flightno]',
        'li[data-flight-no]',
        '[data-testid*="flight"]',
      ];
      let rows = [];
      for (const sel of SELECTORS) {
        const found = [...document.querySelectorAll(sel)];
        if (found.length > rows.length) rows = found;
      }
      return JSON.stringify(rows.map(row => {
        const t = row.innerText || '';
        const times = t.match(/\d{2}:\d{2}/g) || [];
        const priceMatch = t.match(/[\xA5\uFFE5](\d{3,5})/) || t.match(/(\d{3,5})\s*起/);
        const price = priceMatch ? +priceMatch[1] : null;
        const fnos = t.match(/[A-Z]{2}\d{3,4}/g) || [];
        const lines = t.split('\n').map(l=>l.trim()).filter(Boolean);
        const airline = lines[0] ? lines[0].slice(0, 20) : '';
        return {airline, flightNo: fnos[0]||'', dep: times[0]||'', arr: times[1]||'', price};
      }).filter(f => f.dep && f.arr && f.price));
    })()"""
    try:
        raw_dom = _extract_last_json_blob(_run_agent_browser(["eval", js_dom]))
        items = _normalize_json_object(raw_dom)
        if isinstance(items, list) and items:
            log.info(f"  DOM 策略2(选择器): {len(items)} 条")
            return _dom_items_to_flights(items, origin, destination)
    except Exception as e:
        log.debug(f"  DOM 策略2 失败: {e}")

    # ── 策略 3: 全文 innerText 兜底，靠价格+时间对匹配 ──
    js_fulltext = r"""(() => {
      const body = document.body.innerText || '';
      const lines = body.split('\n').map(l=>l.trim()).filter(Boolean);
      const results = [];
      for (let i = 0; i < lines.length; i++) {
        const times = lines[i].match(/(\d{2}:\d{2}).*?(\d{2}:\d{2})/);
        if (!times) continue;
        // 在附近 5 行找价格
        const ctx = lines.slice(Math.max(0,i-2), i+5).join(' ');
        const pm = ctx.match(/[\xA5\uFFE5](\d{3,5})/) || ctx.match(/(\d{3,5})\s*起/);
        if (!pm) continue;
        const fnos = ctx.match(/[A-Z]{2}\d{3,4}/g) || [];
        results.push({airline:'', flightNo:fnos[0]||'', dep:times[1], arr:times[2], price:+pm[1]});
      }
      return JSON.stringify(results);
    })()"""
    try:
        raw_ft = _extract_last_json_blob(_run_agent_browser(["eval", js_fulltext]))
        items = _normalize_json_object(raw_ft)
        if isinstance(items, list) and items:
            log.info(f"  DOM 策略3(全文): {len(items)} 条")
            return _dom_items_to_flights(items, origin, destination)
    except Exception as e:
        log.debug(f"  DOM 策略3 失败: {e}")

    return []


def _extract_flights_from_state(state_obj, origin: str, destination: str) -> list[dict]:
    """递归从页面 state JSON 中找到航班列表并提取。"""
    if not isinstance(state_obj, (dict, list)):
        return []

    def _walk(obj, depth=0):
        if depth > 8:
            return []
        if isinstance(obj, list):
            if obj and isinstance(obj[0], dict) and (
                "flightNo" in obj[0] or "departureTime" in obj[0] or "price" in obj[0]
            ):
                return obj
            results = []
            for item in obj:
                r = _walk(item, depth + 1)
                if len(r) > len(results):
                    results = r
            return results
        if isinstance(obj, dict):
            for key in ("flightList", "flights", "flightItems", "data", "list", "result"):
                if key in obj:
                    r = _walk(obj[key], depth + 1)
                    if r:
                        return r
            best = []
            for v in obj.values():
                r = _walk(v, depth + 1)
                if len(r) > len(best):
                    best = r
            return best
        return []

    raw_flights = _walk(state_obj)
    result = []
    for f in raw_flights:
        if not isinstance(f, dict):
            continue
        dep = f.get("departureTime") or f.get("dep") or f.get("depTime") or ""
        arr = f.get("arrivalTime") or f.get("arr") or f.get("arrTime") or ""
        price = None
        for pk in ("price", "lowestPrice", "minPrice", "salePrice"):
            if f.get(pk):
                try:
                    price = int(float(f[pk]))
                except Exception:
                    pass
                break
        if not (dep and arr and price):
            continue
        result.append({
            "airline": f.get("airlineName") or f.get("airline") or "",
            "flight_no": f.get("flightNo") or f.get("flightNumber") or "",
            "departure_time": str(dep)[:5],
            "arrival_time": str(arr)[:5],
            "price_cny": price,
            "origin": origin,
            "destination": destination,
        })
    return result


def _dom_items_to_flights(items: list, origin: str, destination: str) -> list[dict]:
    seen = set()
    flights = []
    for item in items:
        key = (item.get("dep"), item.get("arr"), item.get("price"))
        if key in seen or not all(key):
            continue
        seen.add(key)
        flights.append({
            "airline": item.get("airline", ""),
            "flight_no": item.get("flightNo", ""),
            "departure_time": item.get("dep", ""),
            "arrival_time": item.get("arr", ""),
            "price_cny": item.get("price"),
            "origin": origin,
            "destination": destination,
        })
    return sorted(flights, key=lambda x: x.get("price_cny") or 99999)


def get_ctrip_flights_for_searches(searches, proxy_url=None, proxy_id=None):
    """
    批量查询携程航班价格（DOM 模式）

    Args:
        searches: list of search dicts (each has url, origin, destination, flight_date, name)

    Returns:
        {url: analysis_result}  — 与 analyzer.py 格式兼容
    """
    if not searches:
        return {}

    results = {}

    for s in searches:
        url = s["url"]
        origin = s.get("origin", "")
        destination = s.get("destination", "")
        date_str = s.get("flight_date", "")

        result = make_result(
            source=f"携程DOM_{origin}_{destination}",
            url=url,
            flight_date=date_str,
            proxy_id=proxy_id,
            request_mode="browser_dom",
        )

        if not (origin and destination and date_str):
            result["error"] = "缺少 origin/destination/date"
            result["status"] = "degraded"
            results[url] = result
            continue

        canonical_url = _canonical_search_url(origin, destination, date_str)
        log.info(f"  🏷️ 携程DOM: {origin}→{destination} {date_str}")

        try:
            dom_flights = _browser_dom_scrape_flights(canonical_url, origin, destination)
        except Exception as e:
            dom_flights = []
            log.warning(f"  ⚠️ 携程 DOM 抓取异常: {e}")

        if dom_flights:
            result["flights"] = dom_flights
            result["lowest_price"] = dom_flights[0]["price_cny"]
            log.info(f"  ✓ 携程 DOM: {len(dom_flights)} 个航班, 最低 ¥{result['lowest_price']}")
        else:
            result["error"] = "携程 DOM 抓取无结果，浏览器不可用或页面结构变化"
            result["status"] = "blocked"
            result["block_reason"] = "dom_scrape_failed"
            result["retryable"] = False
            log.warning(f"  ⚠️ 携程 DOM 抓取失败 {origin}→{destination} {date_str}")

        results[url] = finalize_result_status(result)
        time.sleep(random.uniform(0.3, 0.8))

    return results
