"""
航班组合匹配模块 - 弹性日期搜索 + 最优组合 + 预算比较
v6: 动态路线生成，4个独立时间窗（去程出发/落地，回程出发/落地），max_stops
"""

from datetime import datetime, timedelta

from app.airports import get_route_pairs, get_throwaway_searches, expand_airport


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
    ob_flex = trip.get("outbound_flex", 0) or 0
    rt_flex = trip.get("return_flex", 1) or 1
    if days_to_depart > 90:
        return 0, 0
    elif days_to_depart <= 30:
        return ob_flex, min(rt_flex + 1, 3)
    else:
        return ob_flex, rt_flex


def _flight_passes_filters(f, depart_start, depart_end, arrive_start, arrive_end, max_stops):
    """
    检查单个航班是否通过时间窗和经停过滤。
    时间窗 None = 不过滤该维度。无时间数据的航班通过过滤（不丢弃）。
    """
    dep_hour = _parse_hour(f.get("departure_time"))
    arr_hour = _parse_hour(f.get("arrival_time"))

    if dep_hour is not None and depart_start is not None and depart_end is not None:
        if not (depart_start <= dep_hour <= depart_end):
            return False

    if arr_hour is not None and arrive_start is not None and arrive_end is not None:
        if not (arrive_start <= arr_hour <= arrive_end):
            return False

    if max_stops is not None and f.get("stops") is not None:
        if f["stops"] > max_stops:
            return False

    return True


def get_search_urls(trip):
    """
    根据行程动态生成搜索 URL。
    - 从 trip.origin / trip.destination 展开路线对
    - 支持弹性日期
    - 单程只生成去程 URL
    - throwaway=True 时额外生成甩尾搜索 URL（携程专用）
    """
    is_one_way = trip.get("trip_type") == "one_way"
    days_to_depart = (
        datetime.strptime(trip["outbound_date"], "%Y-%m-%d").date() - datetime.now().date()
    ).days
    ob_flex, rt_flex = _effective_flex(trip, days_to_depart)

    ob_dates = _date_range(trip["outbound_date"], ob_flex, "before")
    rt_dates = [] if is_one_way else _date_range(trip.get("return_date", ""), rt_flex, "before")

    origin = trip.get("origin", "TYO")
    destination = trip.get("destination", "PVG")

    # 所有 (IATA, IATA) 路由对
    ob_pairs = get_route_pairs(origin, destination)
    rt_pairs = [(d, o) for o, d in ob_pairs]

    urls = []

    def _add_urls(pairs, direction, dates):
        for orig, dest in pairs:
            pair_str = f"{orig}-{dest}"
            pair_label = f"{orig}→{dest}"
            base_date = trip["outbound_date"] if direction == "outbound" else trip.get("return_date", "")
            dir_label = "去程" if direction == "outbound" else "回程"

            for date in dates:
                suffix = f"({date})" if date != base_date else ""

                # 携程
                urls.append({
                    "name": f"携程_{pair_str}{suffix}",
                    "direction": direction,
                    "label": f"{dir_label} {pair_label} {date}",
                    "url": (
                        f"https://flights.ctrip.com/online/list/oneway-{orig.lower()}-{dest.lower()}"
                        f"?depdate={date}&cabin=y&adult=1&child=0&infant=0"
                    ),
                    "wait": 8,
                    "flight_date": date,
                    "origin": orig,
                    "destination": dest,
                    "source_type": "ctrip",
                    "throwaway_for": None,
                })

                # LetsFG
                urls.append({
                    "name": f"LetsFG_{pair_str}{suffix}",
                    "direction": direction,
                    "label": f"{dir_label} {pair_label} {date} [LetsFG]",
                    "url": f"letsfg://search/{orig}-{dest}/{date}",
                    "wait": 8,
                    "flight_date": date,
                    "origin": orig,
                    "destination": dest,
                    "source_type": "letsfg",
                    "throwaway_for": None,
                })

            # Google（每个路由对只搜主日期）
            urls.append({
                "name": f"Google_{pair_str}",
                "direction": direction,
                "label": f"{dir_label} {pair_label} {base_date} [Google]",
                "url": (
                    f"https://www.google.co.jp/travel/flights"
                    f"#flt={orig}.{dest}.{base_date};c:JPY;e:1;sd:1;t:f"
                ),
                "wait": 10,
                "flight_date": base_date,
                "origin": orig,
                "destination": dest,
                "source_type": "google",
                "throwaway_for": None,
            })

    _add_urls(ob_pairs, "outbound", ob_dates)
    if not is_one_way:
        _add_urls(rt_pairs, "return", rt_dates)

    # 甩尾搜索（仅携程，去程日期，搜 origin→beyond，中转点=destination）
    if trip.get("throwaway"):
        throwaway_pairs = get_throwaway_searches(origin, destination)
        base_date = trip["outbound_date"]
        for orig, beyond in throwaway_pairs[:6]:
            urls.append({
                "name": f"携程甩尾_{orig}_{beyond}",
                "direction": "outbound",
                "label": f"去程甩尾 {orig}→{beyond} via {destination} {base_date}",
                "url": (
                    f"https://flights.ctrip.com/online/list/oneway-{orig.lower()}-{beyond.lower()}"
                    f"?depdate={base_date}&cabin=y&adult=1&child=0&infant=0"
                ),
                "wait": 8,
                "flight_date": base_date,
                "origin": orig,
                "destination": beyond,
                "source_type": "ctrip",
                "throwaway_for": destination,  # 真实目的地
            })

    return urls


def find_best_combinations(results, trip):
    """找出符合条件的最优组合（往返或单程）"""
    budget = trip.get("budget", 1500)
    is_one_way = trip.get("trip_type") == "one_way"
    max_stops = trip.get("max_stops")  # None=不限

    # 去程时间窗
    ob_depart_start = trip.get("ob_depart_start")
    ob_depart_end   = trip.get("ob_depart_end")
    ob_arrive_start = trip.get("ob_arrive_start")
    ob_arrive_end   = trip.get("ob_arrive_end")

    # 回程时间窗
    rt_depart_start = trip.get("rt_depart_start")
    rt_depart_end   = trip.get("rt_depart_end")
    rt_arrive_start = trip.get("rt_arrive_start")
    rt_arrive_end   = trip.get("rt_arrive_end")

    # 真实目的地（用于甩尾检测）
    true_dest_iatas = set(expand_airport(trip.get("destination", "")))

    outbound_flights = []
    throwaway_flights = []  # 甩尾候选

    for src in results["outbound"]:
        for f in src.get("flights", []):
            # 甩尾票检测：via 包含真实目的地 → 进甩尾候选池
            via = f.get("via", "") or ""
            via_airports = {a.strip() for a in via.split(",") if a.strip()}
            is_throwaway = bool(via_airports & true_dest_iatas)

            if not _flight_passes_filters(
                f, ob_depart_start, ob_depart_end,
                ob_arrive_start, ob_arrive_end, max_stops
            ):
                continue

            f["_source"] = src.get("source", "")
            f["_url"] = src.get("url", "")
            f["_flight_date"] = src.get("flight_date", trip["outbound_date"])
            f["_throwaway"] = is_throwaway

            if is_throwaway:
                throwaway_flights.append(f)
            else:
                outbound_flights.append(f)

    outbound_flights.sort(key=lambda x: x.get("price_cny", 99999))
    throwaway_flights.sort(key=lambda x: x.get("price_cny", 99999))

    if is_one_way:
        combos = []
        for ob in outbound_flights[:10]:
            if ob.get("price_cny") is None:
                continue
            total = ob["price_cny"]
            combos.append({
                "outbound": ob,
                "return": None,
                "total": total,
                "within_budget": total <= budget,
                "throwaway": False,
            })
        # 甩尾单程 combo
        for ob in throwaway_flights[:5]:
            if ob.get("price_cny") is None:
                continue
            total = ob["price_cny"]
            combos.append({
                "outbound": ob,
                "return": None,
                "total": total,
                "within_budget": total <= budget,
                "throwaway": True,
            })
        combos.sort(key=lambda x: x["total"])
        return combos[:10]

    return_flights = []
    for src in results["return"]:
        for f in src.get("flights", []):
            if not _flight_passes_filters(
                f, rt_depart_start, rt_depart_end,
                rt_arrive_start, rt_arrive_end, max_stops
            ):
                continue
            f["_source"] = src.get("source", "")
            f["_url"] = src.get("url", "")
            f["_flight_date"] = src.get("flight_date", trip.get("return_date"))
            f["_throwaway"] = False
            return_flights.append(f)

    return_flights.sort(key=lambda x: x.get("price_cny", 99999))

    combos = []
    for ob in outbound_flights[:8]:
        for rt in return_flights[:8]:
            if ob.get("price_cny") is None or rt.get("price_cny") is None:
                continue
            total = ob["price_cny"] + rt["price_cny"]
            combos.append({
                "outbound": ob,
                "return": rt,
                "total": total,
                "within_budget": total <= budget,
                "throwaway": False,
            })

    combos.sort(key=lambda x: x["total"])
    return combos[:10]
