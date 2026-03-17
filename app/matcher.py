"""
航班组合匹配模块 - 最优组合 + 预算比较
"""


def get_search_urls(trip):
    """根据行程生成搜索 URL"""
    OB = trip["outbound_date"]
    RT = trip["return_date"]

    return [
        # ━━━ 携程（最稳定，CNY）━━━
        {"name": "携程", "direction": "outbound", "label": "去程 NRT→PVG",
         "url": f"https://flights.ctrip.com/online/list/oneway-NRT-PVG?depdate={OB}&cabin=y&adult=1&child=0&infant=0",
         "wait": 8},
        {"name": "携程", "direction": "return", "label": "回程 PVG→NRT",
         "url": f"https://flights.ctrip.com/online/list/oneway-PVG-NRT?depdate={RT}&cabin=y&adult=1&child=0&infant=0",
         "wait": 8},
        # 携程 HND（乐桃、ANA红眼）
        {"name": "携程_HND", "direction": "outbound", "label": "去程 HND→PVG",
         "url": f"https://flights.ctrip.com/online/list/oneway-HND-PVG?depdate={OB}&cabin=y&adult=1&child=0&infant=0",
         "wait": 8},
        {"name": "携程_HND", "direction": "return", "label": "回程 PVG→HND",
         "url": f"https://flights.ctrip.com/online/list/oneway-PVG-HND?depdate={RT}&cabin=y&adult=1&child=0&infant=0",
         "wait": 8},
        # ━━━ Google Flights 日本站（JPY，反杀熟）━━━
        # 注：Google CN 已移除，LLM 持续误读其 CNY 四位数价格
        #     日本站用 JPY 五位数价格，LLM 识别更稳定
        {"name": "Google_JP", "direction": "outbound", "label": "去程 NRT→PVG",
         "url": f"https://www.google.co.jp/travel/flights?q=Flights+from+NRT+to+PVG+on+{OB}+one+way&curr=JPY&hl=ja",
         "wait": 10},
        {"name": "Google_JP", "direction": "return", "label": "回程 PVG→NRT",
         "url": f"https://www.google.co.jp/travel/flights?q=Flights+from+PVG+to+NRT+on+{RT}+one+way&curr=JPY&hl=ja",
         "wait": 10},
    ]


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
                    outbound_flights.append(f)
            except:
                continue

    return_flights = []
    for src in results["return"]:
        for f in src.get("flights", []):
            f["_source"] = src["source"]
            f["_url"] = src["url"]
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
