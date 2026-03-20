#!/bin/zsh
# 从真实 Chrome (port 9222) 捕获携程 Session Profile
#
# 用法: ./capture_real_chrome.sh [出发] [到达] [日期]
# 示例: ./capture_real_chrome.sh NRT PVG 2026-03-27
#
# 前提: 存在可访问的 CDP 端点。
# 默认优先读取 CTRIP_CDP_PORT，其次使用 9222。

ORIGIN=${1:-NRT}
DEST=${2:-PVG}
DATE=${3:-$(date -v+7d +%Y-%m-%d 2>/dev/null || date -d "+7 days" +%Y-%m-%d)}

city_code() {
  case $1 in
    NRT|HND) echo "tyo" ;;
    PVG|SHA) echo "sha" ;;
    *) echo "${(L)1}" ;;
  esac
}

OC=$(city_code $ORIGIN)
DC=$(city_code $DEST)
SEARCH_URL="https://flights.ctrip.com/online/list/oneway-${OC}-${DC}?depdate=${DATE}&cabin=y_s&adult=1&child=0&infant=0&containstax=1"
CDP_TARGET=${CTRIP_CDP_PORT:-9222}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
mkdir -p "$DATA_DIR"
PROFILE_PATH="$DATA_DIR/ctrip_batch_profile.json"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  携程 Profile 捕获（CDP）"
echo "  航线: $ORIGIN → $DEST   日期: $DATE"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ─── Step 0: 检查 CDP 可达性 ───────────────────────────────────────────────
echo "\n[0] 检查 Chrome CDP (${CDP_TARGET})..."

if ! python3 - <<PYEOF 2>/dev/null
import sys, urllib.request
endpoint = "${CDP_TARGET}"
if endpoint.isdigit():
    url = f"http://127.0.0.1:{endpoint}/json/version"
elif endpoint.startswith("http://") or endpoint.startswith("https://"):
    url = endpoint.rstrip("/") + "/json/version"
else:
    url = f"http://{endpoint}/json/version"
try:
    urllib.request.urlopen(url, timeout=3)
    sys.exit(0)
except Exception:
    sys.exit(1)
PYEOF
then
  echo "  ✗ 无法连接 CDP: ${CDP_TARGET}"
  echo ""
  echo "  Docker/VPS 推荐："
  echo "  1. 先启动 compose 内的 chromium 容器"
  echo "  2. 通过 https://<服务器>:3001 打开浏览器界面"
  echo "  3. 人工完成一次携程验证码后再重跑本脚本"
  exit 1
fi
echo "  ✓ Chrome CDP 已就绪"

# ─── Step 1: 连接并打开携程搜索页 ───────────────────────────────────────────
echo "\n[1] 打开携程搜索页..."
agent-browser --cdp "$CDP_TARGET" open "$SEARCH_URL"
echo "  等待 10 秒让航班数据加载..."
agent-browser --cdp "$CDP_TARGET" wait 10000

# ─── Step 2: 清空网络记录，重新加载，再次等待 ────────────────────────────────
echo "\n[2] 清空网络记录，重新加载..."
agent-browser --cdp "$CDP_TARGET" network requests --clear
agent-browser --cdp "$CDP_TARGET" reload
echo "  等待 10 秒..."
agent-browser --cdp "$CDP_TARGET" wait 10000

# ─── Step 3: 抓取所有数据 ────────────────────────────────────────────────────
echo "\n[3] 抓取 headers + cookies + GlobalSearchCriteria..."

RAW_REQUESTS=$(agent-browser --cdp "$CDP_TARGET" network requests --json 2>/dev/null | tail -1)
CRITERIA_RAW=$(agent-browser --cdp "$CDP_TARGET" eval 'JSON.stringify(window.GlobalSearchCriteria || null)' 2>/dev/null | tail -1)
COOKIES_RAW=$(agent-browser --cdp "$CDP_TARGET" cookies get 2>/dev/null | tail -n +2 | python3 -c "
import sys, json
lines = sys.stdin.read().strip()
try:
    data = json.loads(lines)
    # filter for ctrip domains
    ctrip = [c for c in (data if isinstance(data, list) else [])
             if 'ctrip' in c.get('domain','') or 'trip.com' in c.get('domain','')]
    cookie_str = '; '.join(f\"{c['name']}={c['value']}\" for c in ctrip)
    print(cookie_str)
except:
    print('')
" 2>/dev/null)

# ─── Step 4: 也用 browser fetch 直接测一次 ───────────────────────────────────
echo "\n[4] 在浏览器上下文里直接测 batchSearch..."

python3 - "$ORIGIN" "$DEST" "$DATE" "$PROFILE_PATH" "$RAW_REQUESTS" "$CRITERIA_RAW" "$COOKIES_RAW" <<'PYEOF'
import sys, json, os

origin, dest, date_str = sys.argv[1], sys.argv[2], sys.argv[3]
profile_path = sys.argv[4]
raw_requests = sys.argv[5].strip()
criteria_raw = sys.argv[6].strip()
cookies_raw  = sys.argv[7].strip()

# ── 解析 batchSearch 请求 headers ────────────────────────────────────────────
headers = {}
WANT = {"rms-token","sessionid","sign","token","transactionid","w-payload-source",
        "x-ctx-fvpc","x-ctx-ubt-pageid","x-ctx-ubt-pvid","x-ctx-ubt-sid","x-ctx-ubt-vid"}
try:
    blob = json.loads(raw_requests)
    if isinstance(blob, str): blob = json.loads(blob)
    reqs = blob.get("data", {}).get("requests", [])
    if isinstance(reqs, str): reqs = json.loads(reqs)
    for item in (reqs or []):
        if isinstance(item, str): item = json.loads(item)
        if not isinstance(item, dict): continue
        if "/international/search/api/search/batchSearch" not in item.get("url", ""):
            continue
        h = item.get("headers", {}) or {}
        if isinstance(h, str): h = json.loads(h)
        if h.get("token") or h.get("rms-token"):
            headers = {k: v for k, v in h.items() if k in WANT and v}
            print(f"  ✓ 捕获 batchSearch headers: {list(headers.keys())}")
            break
    else:
        print("  ⚠ 未捕获到 batchSearch 请求 headers（正常，cookie 模式不需要）")
except Exception as e:
    print(f"  ⚠ 解析 requests 失败: {e}")

# ── 解析 GlobalSearchCriteria ─────────────────────────────────────────────────
criteria = None
try:
    c = json.loads(criteria_raw)
    if isinstance(c, str): c = json.loads(c)
    if isinstance(c, dict):
        criteria = c
        print(f"  ✓ GlobalSearchCriteria: transactionID={c.get('transactionID','?')}")
    else:
        print("  ⚠ GlobalSearchCriteria 为空（页面可能未完成加载）")
except Exception as e:
    print(f"  ⚠ 解析 criteria 失败: {e}")

# ── Cookie ────────────────────────────────────────────────────────────────────
cookie_str = cookies_raw.strip().strip('"').strip("'")
if cookie_str:
    headers["cookie"] = cookie_str
    print(f"  ✓ Cookie: {len(cookie_str)} 字符")
else:
    print("  ⚠ Cookie 为空")

# ── 写入 profile ──────────────────────────────────────────────────────────────
profile = {"headers": headers, "criteria": criteria}
with open(profile_path, "w", encoding="utf-8") as f:
    json.dump(profile, f, ensure_ascii=False, indent=2)

print(f"\n  ✅ Profile 保存: {profile_path}")
print(f"     cookie 长度: {len(cookie_str)}")
print(f"     extra headers: {[k for k in headers if k != 'cookie']}")
print(f"     criteria keys: {list(criteria.keys()) if criteria else '(空)'}")
PYEOF

echo "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  现在运行测试："
echo "  cd $SCRIPT_DIR && python3 test_ctrip.py $DATE"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
