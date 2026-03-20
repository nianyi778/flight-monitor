#!/usr/bin/env python3
"""
本地携程页面 API 探针。

用途：
1. 打开真实携程搜索页
2. 抓取页面发出的 network requests
3. 提取候选航班接口
4. 读取页面内的 GlobalSearchCriteria
5. 在页面上下文里直接 replay 新接口，验证响应

依赖：
- npx -y agent-browser
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSION = "ctrip-probe"
AGENT_BROWSER_HOME = os.getenv("AGENT_BROWSER_HOME")
PLAYWRIGHT_BROWSERS_PATH = os.getenv("PLAYWRIGHT_BROWSERS_PATH")


def _run(args: list[str]) -> str:
    parts = ["npx", "-y", "agent-browser", "--session", SESSION, *args]
    cmd = " ".join(shlex.quote(part) for part in parts)
    env = dict(os.environ)
    if AGENT_BROWSER_HOME:
        Path(AGENT_BROWSER_HOME).mkdir(parents=True, exist_ok=True)
        env["HOME"] = AGENT_BROWSER_HOME
    if PLAYWRIGHT_BROWSERS_PATH:
        env["PLAYWRIGHT_BROWSERS_PATH"] = PLAYWRIGHT_BROWSERS_PATH
    proc = subprocess.run(
        ["/bin/zsh", "-lc", cmd],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"命令失败: {cmd}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
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


def _route_meta(origin: str, destination: str):
    city_map = {
        "NRT": {
            "city_code": "TYO",
            "city_name": "东京",
            "airport_name": "成田国际机场",
            "country_id": 78,
            "country_name": "日本",
            "province_id": 0,
            "city_id": 228,
            "timezone": 540,
        },
        "HND": {
            "city_code": "TYO",
            "city_name": "东京",
            "airport_name": "羽田机场",
            "country_id": 78,
            "country_name": "日本",
            "province_id": 0,
            "city_id": 228,
            "timezone": 540,
        },
        "PVG": {
            "city_code": "SHA",
            "city_name": "上海",
            "airport_name": "浦东国际机场",
            "country_id": 1,
            "country_name": "中国",
            "province_id": 2,
            "city_id": 2,
            "timezone": 480,
        },
        "SHA": {
            "city_code": "SHA",
            "city_name": "上海",
            "airport_name": "虹桥国际机场",
            "country_id": 1,
            "country_name": "中国",
            "province_id": 2,
            "city_id": 2,
            "timezone": 480,
        },
    }
    if origin not in city_map or destination not in city_map:
        raise SystemExit(f"暂不支持路线 {origin}-{destination}")
    return city_map[origin], city_map[destination]


def _build_url(origin: str, destination: str, depdate: str) -> str:
    return (
        f"https://flights.ctrip.com/online/list/oneway-{origin}-{destination}"
        f"?depdate={depdate}&cabin=y&adult=1&child=0&infant=0"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="NRT")
    parser.add_argument("--destination", default="PVG")
    parser.add_argument("--date", default="2026-09-18")
    parser.add_argument("--probe-date", default=None, help="replay 接口时使用的日期，默认与 --date 相同")
    args = parser.parse_args()

    origin_meta, destination_meta = _route_meta(args.origin, args.destination)
    target_url = _build_url(args.origin, args.destination, args.date)
    probe_date = args.probe_date or args.date

    _run(["open", target_url])
    _run(["wait", "4000"])
    _run(["network", "requests", "--clear"])
    _run(["reload"])
    _run(["wait", "5000"])

    requests_blob = _extract_last_json_blob(_run(["network", "requests", "--json"]))
    requests = requests_blob.get("data", {}).get("requests", [])
    candidates = [
        item for item in requests
        if "FlightIntlAndInlandLowestPriceSearch" in item.get("url", "")
        or "/international/search/api/" in item.get("url", "")
    ]

    criteria = _extract_last_json_blob(
        _run([
            "eval",
            "JSON.stringify(window.GlobalSearchCriteria || null)",
        ])
    )

    if not criteria:
        criteria = {
            "adultCount": 1,
            "childCount": 0,
            "infantCount": 0,
            "flightWay": "S",
            "cabin": "Y",
            "scope": "i",
            "extensionAttributes": {"LoggingSampling": False, "isFlightIntlNewUser": False},
            "segmentNo": 1,
            "directFlight": False,
            "extGlobalSwitches": {"useAllRecommendSwitch": True, "unfoldPriceListSwitch": True},
            "noRecommend": False,
            "flightWayEnum": "OW",
            "cabinEnum": "Y",
            "isMultiplePassengerType": 0,
            "departCountryName": origin_meta["country_name"],
            "departProvinceId": origin_meta["province_id"],
            "departureCityId": origin_meta["city_id"],
            "arrivalCountryName": destination_meta["country_name"],
            "arrivalProvinceId": destination_meta["province_id"],
            "arrivalCityId": destination_meta["city_id"],
            "flightSegments": [{
                "departureCityCode": origin_meta["city_code"],
                "arrivalCityCode": destination_meta["city_code"],
                "departureAirportCode": args.origin,
                "arrivalAirportCode": args.destination,
                "departureCityName": origin_meta["city_name"],
                "arrivalCityName": destination_meta["city_name"],
                "departureDate": probe_date,
                "departureCountryId": origin_meta["country_id"],
                "departureCountryName": origin_meta["country_name"],
                "departureProvinceId": origin_meta["province_id"],
                "departureCityId": origin_meta["city_id"],
                "arrivalCountryId": destination_meta["country_id"],
                "arrivalCountryName": destination_meta["country_name"],
                "arrivalProvinceId": destination_meta["province_id"],
                "arrivalCityId": destination_meta["city_id"],
                "departureAirportName": origin_meta["airport_name"],
                "arrivalAirportName": destination_meta["airport_name"],
                "departureCityTimeZone": origin_meta["timezone"],
                "arrivalCityTimeZone": destination_meta["timezone"],
                "timeZone": origin_meta["timezone"],
            }],
        }

    replay_js = (
        "(async () => {"
        f"const fallback = {json.dumps(criteria, ensure_ascii=False)};"
        "const payload = JSON.parse(JSON.stringify(window.GlobalSearchCriteria || fallback));"
        f"payload.flightSegments[0].departureDate = {json.dumps(probe_date)};"
        "const res = await fetch("
        "'https://m.ctrip.com/restapi/soa2/15380/bjjson/FlightIntlAndInlandLowestPriceSearch?v=' + Math.random(),"
        "{method:'POST',credentials:'include',headers:{'content-type':'application/json;charset=UTF-8','accept':'application/json'},body:JSON.stringify(payload)}"
        ");"
        "const text = await res.text();"
        "return JSON.stringify({status: res.status, ok: res.ok, body: text});"
        "})()"
    )
    replay = _extract_last_json_blob(_run(["eval", replay_js]))

    report = {
        "target_url": target_url,
        "probe_date": probe_date,
        "candidate_request_count": len(candidates),
        "candidate_requests": candidates,
        "global_search_criteria": criteria,
        "replay_result": replay,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
