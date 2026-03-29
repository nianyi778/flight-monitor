"""
航班组合匹配模块 - 弹性日期搜索 + 最优组合 + 预算比较
"""

from datetime import datetime, timedelta


def _parse_hour(value):
    """把 HH:MM 格式解析为小时，失败返回 None。"""
    try:
        return int(str(value).split(":")[0])
    except Exception:
        return None


def _date_range(base_date_str, flex_days, direction="before"):
    """生成日期列表：base_date 向前(before)或向后(after) flex_days 天"""
    base = datetime.strptime(base_date_str, "%Y-%m-%d").date()
    dates = [base_date_str]
    for i in range(1, flex_days + 1):
        if direction == "before":
            d = base - timedelta(days=i)
        else:
            d = base + timedelta(days=i)
        dates.append(str(d))
    return dates


def _effective_flex(trip, days_to_depart):
    """根据倒计时动态决定 flex 天数"""
    if days_to_depart > 90:
        return 0, 0          # 远期：不搜弹性日期
    elif days_to_depart <= 30:
        return trip.get("outbound_flex", 0), min(trip.get("return_flex", 1) + 1, 3)  # 临近：回程多搜1天
    else:
        return trip.get("outbound_flex", 0), trip.get("return_flex", 1)


def get_search_urls(trip):
    """根据行程生成搜索 URL（支持弹性日期，按倒计时自动收缩；单程只生成去程URL）"""
    is_one_way = trip.get("trip_type") == "one_way"
    days_to_depart = (datetime.strptime(trip["outbound_date"], "%Y-%m-%d").date() - datetime.now().date()).days
    ob_flex, rt_flex = _effective_flex(trip, days_to_depart)

    ob_dates = _date_range(trip["outbound_date"], ob_flex, "before")
    rt_dates = [] if is_one_way else _date_range(trip["return_date"], rt_flex, "before")

    urls = []

    # 搜索模板 - 东京(NRT/HND) × 上海浦东(PVG)
    # SHA(虹桥)廉航覆盖率低，不纳入搜索
    templates = [
        # ━━━ 携程: NRT/HND → PVG ━━━
        ("携程_NRT_PVG", "outbound", "NRT-PVG",
         "https://flights.ctrip.com/online/list/oneway-NRT-PVG?depdate={date}&cabin=y&adult=1&child=0&infant=0"),
        ("携程_NRT_PVG", "return", "PVG-NRT",
         "https://flights.ctrip.com/online/list/oneway-PVG-NRT?depdate={date}&cabin=y&adult=1&child=0&infant=0"),
        ("携程_HND_PVG", "outbound", "HND-PVG",
         "https://flights.ctrip.com/online/list/oneway-HND-PVG?depdate={date}&cabin=y&adult=1&child=0&infant=0"),
        ("携程_HND_PVG", "return", "PVG-HND",
         "https://flights.ctrip.com/online/list/oneway-PVG-HND?depdate={date}&cabin=y&adult=1&child=0&infant=0"),
        # ━━━ LetsFG: NRT/HND ⇄ PVG (多连接器聚合) ━━━
        ("LetsFG_NRT_PVG", "outbound", "NRT-PVG", "letsfg://search/NRT-PVG/{date}"),
        ("LetsFG_NRT_PVG", "return", "PVG-NRT", "letsfg://search/PVG-NRT/{date}"),
        ("LetsFG_HND_PVG", "outbound", "HND-PVG", "letsfg://search/HND-PVG/{date}"),
        ("LetsFG_HND_PVG", "return", "PVG-HND", "letsfg://search/PVG-HND/{date}"),
        # ━━━ Google JP: NRT⇄PVG (聚合比价) ━━━
        ("Google_JP", "outbound", "NRT-PVG",
         "https://www.google.co.jp/travel/flights?q=Flights+from+NRT+to+PVG+on+{date}+one+way&curr=JPY&hl=ja"),
        ("Google_JP", "return", "PVG-NRT",
         "https://www.google.co.jp/travel/flights?q=Flights+from+PVG+to+NRT+on+{date}+one+way&curr=JPY&hl=ja"),
    ]

    for name, direction, pair, url_tpl in templates:
        if is_one_way and direction == "return":
            continue  # 单程：跳过所有回程搜索
        dates = ob_dates if direction == "outbound" else rt_dates
        origin, destination = pair.split("-")
        source_type = "ctrip" if "携程" in name else ("letsfg" if "LetsFG" in name else "google")
        for date in dates:
            # 弹性日期加日期后缀区分
            base_date = trip["outbound_date"] if direction == "outbound" else trip.get("return_date", "")
            suffix = f"({date})" if date != base_date else ""
            from_to = pair.replace("-", "→")
            urls.append({
                "name": f"{name}{suffix}",
                "direction": direction,
                "label": f"{'去程' if direction == 'outbound' else '回程'} {from_to} {date}",
                "url": url_tpl.format(date=date),
                "wait": 8 if ("携程" in name or "LetsFG" in name) else 10,
                "flight_date": date,
                "origin": origin,
                "destination": destination,
                "source_type": source_type,
            })

    return urls


def find_best_combinations(results, trip):
    """找出符合条件的最优组合（往返或单程）"""
    depart_after = trip.get("depart_after", 19)
    depart_before = trip.get("depart_before", 23)
    arrive_after = trip.get("arrive_after", 0)
    arrive_before = trip.get("arrive_before", 6)
    budget = trip.get("budget", 1500)
    is_one_way = trip.get("trip_type") == "one_way"

    outbound_flights = []
    for src in results["outbound"]:
        for f in src.get("flights", []):
            dep_hour = _parse_hour(f.get("departure_time"))
            # 有时间信息时才过滤时间窗；无时间信息则直接收录
            if dep_hour is not None and not (depart_after <= dep_hour <= depart_before):
                continue
            f["_source"] = src["source"]
            f["_url"] = src.get("url", "")
            f["_flight_date"] = src.get("flight_date", trip["outbound_date"])
            outbound_flights.append(f)

    outbound_flights.sort(key=lambda x: x.get("price_cny", 99999))

    if is_one_way:
        # 单程：每个去程航班单独成为一个 combo，total = 去程价格
        combos = []
        for ob in outbound_flights[:10]:
            if not ob.get("price_cny"):
                continue
            total = ob["price_cny"]
            combos.append({
                "outbound": ob,
                "return": None,
                "total": total,
                "within_budget": total <= budget,
            })
        return combos[:10]

    return_flights = []
    for src in results["return"]:
        for f in src.get("flights", []):
            arr_hour = _parse_hour(f.get("arrival_time"))
            # 有时间信息时才过滤时间窗；无时间信息则直接收录
            if arr_hour is not None and not (arrive_after <= arr_hour <= arrive_before):
                continue
            f["_source"] = src["source"]
            f["_url"] = src.get("url", "")
            f["_flight_date"] = src.get("flight_date", trip["return_date"])
            return_flights.append(f)

    return_flights.sort(key=lambda x: x.get("price_cny", 99999))

    combos = []
    for ob in outbound_flights[:8]:
        for rt in return_flights[:8]:
            total = (ob.get("price_cny") or 99999) + (rt.get("price_cny") or 99999)
            combos.append({
                "outbound": ob,
                "return": rt,
                "total": total,
                "within_budget": total <= budget,
            })

    combos.sort(key=lambda x: x["total"])
    return combos[:10]
