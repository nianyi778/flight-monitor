#!/bin/zsh
# 携程 Session 捕获脚本
# 用法: ./capture_ctrip_profile.sh [出发机场] [到达机场] [日期 YYYY-MM-DD]
# 示例: ./capture_ctrip_profile.sh NRT PVG 2026-03-27
#
# 流程:
#   1. 用 agent-browser 打开携程搜索页（真实 Chrome session）
#   2. 等待页面加载 / 人工过验证码
#   3. 抓取 batchSearch 请求 headers + cookie + GlobalSearchCriteria
#   4. 写入 data/ctrip_batch_profile.json

set -e

ORIGIN=${1:-NRT}
DEST=${2:-PVG}
DATE=${3:-$(date -v+7d +%Y-%m-%d 2>/dev/null || date -d "+7 days" +%Y-%m-%d)}
SESSION="ctrip-api-live"

# 城市代码映射
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

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
mkdir -p "$DATA_DIR"
PROFILE_PATH="$DATA_DIR/ctrip_batch_profile.json"

run_ab() {
  npx -y agent-browser --session "$SESSION" "$@"
}

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  携程 Session 捕获"
echo "  航线: $ORIGIN → $DEST   日期: $DATE"
echo "  URL : $SEARCH_URL"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 1. 打开搜索页
echo "\n[1/5] 打开携程搜索页..."
run_ab open "$SEARCH_URL"

# 2. 初次等待（页面渲染 + 可能的验证码）
echo "[2/5] 等待 8 秒让页面加载..."
run_ab wait 8000

# 3. 清空已记录的网络请求，重新加载一次（确保抓到 batchSearch）
echo "[3/5] 清空网络记录，重新加载页面..."
run_ab network requests --clear
run_ab reload
echo "      等待 8 秒..."
run_ab wait 8000

# 4. 抓取 batchSearch 请求 headers
echo "[4/5] 抓取 batchSearch headers + GlobalSearchCriteria + Cookie..."

# 获取网络请求列表（JSON）
RAW_REQUESTS=$(run_ab network requests --json 2>/dev/null | tail -1)

# 获取 GlobalSearchCriteria
CRITERIA_RAW=$(run_ab eval 'JSON.stringify(window.GlobalSearchCriteria || null)' 2>/dev/null | tail -1)

# 获取所有 cookie
COOKIES_RAW=$(run_ab eval 'document.cookie' 2>/dev/null | tail -1)

# 5. 解析 & 保存 profile
echo "[5/5] 生成 profile 文件..."

python3 - "$PROFILE_PATH" "$RAW_REQUESTS" "$CRITERIA_RAW" "$COOKIES_RAW" <<'PYEOF'
import sys, json, re

profile_path = sys.argv[1]
raw_requests = sys.argv[2].strip()
criteria_raw = sys.argv[3].strip()
cookies_raw  = sys.argv[4].strip()

# ── 解析 batchSearch 请求 headers ───────────────────────────────────────────
headers = {}
try:
    blob = json.loads(raw_requests)
    if isinstance(blob, str):
        blob = json.loads(blob)
    requests = blob.get("data", {}).get("requests", [])
    if isinstance(requests, str):
        requests = json.loads(requests)
    WANT_HEADERS = {
        "rms-token", "sessionid", "sign", "token", "transactionid",
        "w-payload-source", "x-ctx-fvpc", "x-ctx-ubt-pageid",
        "x-ctx-ubt-pvid", "x-ctx-ubt-sid", "x-ctx-ubt-vid",
    }
    for item in (requests or []):
        if isinstance(item, str):
            item = json.loads(item)
        if not isinstance(item, dict):
            continue
        url = item.get("url", "")
        if "/international/search/api/search/batchSearch" not in url:
            continue
        req_headers = item.get("headers", {}) or {}
        if isinstance(req_headers, str):
            req_headers = json.loads(req_headers)
        if req_headers.get("token") or req_headers.get("rms-token"):
            headers = {k: v for k, v in req_headers.items() if k in WANT_HEADERS and v}
            print(f"  ✓ 找到 batchSearch 请求，提取 {len(headers)} 个 header")
            break
    else:
        print("  ⚠ 未找到 batchSearch 请求（可能页面未触发查询）")
except Exception as e:
    print(f"  ⚠ 解析 requests 失败: {e}")

# ── 解析 GlobalSearchCriteria ────────────────────────────────────────────────
criteria = None
try:
    c = json.loads(criteria_raw)
    if isinstance(c, str):
        c = json.loads(c)
    if isinstance(c, dict):
        criteria = c
        print(f"  ✓ GlobalSearchCriteria 提取成功 (transactionID={c.get('transactionID','?')})")
    else:
        print("  ⚠ GlobalSearchCriteria 为空")
except Exception as e:
    print(f"  ⚠ 解析 criteria 失败: {e}")

# ── 解析 cookie ──────────────────────────────────────────────────────────────
cookie_str = ""
try:
    cookie_val = json.loads(cookies_raw)
    if isinstance(cookie_val, str):
        cookie_str = cookie_val
    print(f"  ✓ Cookie 长度: {len(cookie_str)} 字符")
except Exception as e:
    # 可能直接就是字符串
    cookie_str = cookies_raw.strip('"').strip("'")
    print(f"  ✓ Cookie (raw) 长度: {len(cookie_str)} 字符")

if cookie_str:
    headers["cookie"] = cookie_str

# ── 写入 profile ─────────────────────────────────────────────────────────────
profile = {
    "headers": headers,
    "criteria": criteria,
}
with open(profile_path, "w", encoding="utf-8") as f:
    json.dump(profile, f, ensure_ascii=False, indent=2)

print(f"\n  ✅ Profile 已保存: {profile_path}")
print(f"     headers: {list(headers.keys())}")
print(f"     criteria keys: {list(criteria.keys()) if criteria else '(空)'}")
PYEOF

echo "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  完成！可以运行测试："
echo "  python3 test_ctrip.py $DATE"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
