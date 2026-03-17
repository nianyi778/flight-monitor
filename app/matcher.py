"""
航班组合匹配模块 - 弹性日期搜索 + 最优组合 + 预算比较
"""

from datetime import datetime, timedelta


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


def get_search_urls(trip):
    """根据行程生成搜索 URL（支持弹性日期）"""
    ob_dates = _date_range(trip["outbound_date"], trip.get("outbound_flex", 0), "before")
    rt_dates = _date_range(trip["return_date"], trip.get("return_flex", 1), "before")

    urls = []

    # 搜索模板
    templates = [
        # (name, direction, airport_pair, base_url_template)
        ("携程", "outbound", "NRT-PVG",
         "https://flights.ctrip.com/online/list/oneway-NRT-PVG?depdate={date}&cabin=y&adult=1&child=0&infant=0"),
        ("携程", "return", "PVG-NRT",
         "https://flights.ctrip.com/online/list/oneway-PVG-NRT?depdate={date}&cabin=y&adult=1&child=0&infant=0"),
        ("携程_HND", "outbound", "HND-PVG",
         "https://flights.ctrip.com/online/list/oneway-HND-PVG?depdate={date}&cabin=y&adult=1&child=0&infant=0"),
        ("携程_HND", "return", "PVG-HND",
         "https://flights.ctrip.com/online/list/oneway-PVG-HND?depdate={date}&cabin=y&adult=1&child=0&infant=0"),
        ("Google_JP", "outbound", "NRT-PVG",
         "https://www.google.co.jp/travel/flights?q=Flights+from+NRT+to+PVG+on+{date}+one+way&curr=JPY&hl=ja"),
        ("Google_JP", "return", "PVG-NRT",
         "https://www.google.co.jp/travel/flights?q=Flights+from+PVG+to+NRT+on+{date}+one+way&curr=JPY&hl=ja"),
    ]

    for name, direction, pair, url_tpl in templates:
        dates = ob_dates if direction == "outbound" else rt_dates
        for date in dates:
            # 弹性日期加日期后缀区分
            suffix = f"({date})" if date != (trip["outbound_date"] if direction == "outbound" else trip["return_date"]) else ""
            from_to = pair.replace("-", "→")
            urls.append({
                "name": f"{name}{suffix}",
                "direction": direction,
                "label": f"{'去程' if direction == 'outbound' else '回程'} {from_to} {date}",
                "url": url_tpl.format(date=date),
                "wait": 8 if "携程" in name else 10,
                "flight_date": date,
            })

    return urls


def find_best_combinations(results, trip):
    """找出符合条件的最优往返组合"""
    depart_after = trip.get("depart_after", 19)
    budget = trip.get("budget", 1500)

    outbound_flights = []
    for src in results["outbound"]:
        for f in src.get("flights", []):
            try:
                dep_hour = int(f["departure_time"].split(":")[0])
                if dep_hour >= depart_after:
                    f["_source"] = src["source"]
                    f["_url"] = src["url"]
                    f["_flight_date"] = src.get("flight_date", trip["outbound_date"])
                    outbound_flights.append(f)
            except:
                continue

    return_flights = []
    for src in results["return"]:
        for f in src.get("flights", []):
            f["_source"] = src["source"]
            f["_url"] = src["url"]
            f["_flight_date"] = src.get("flight_date", trip["return_date"])
            return_flights.append(f)

    outbound_flights.sort(key=lambda x: x.get("price_cny", 99999))
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
