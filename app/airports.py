"""
机场数据中心模块
- 机场组展开（TYO = NRT + HND）
- 甩尾延伸目的地
- 路由对生成
"""

# 机场组：metro code → 组内所有 IATA 机场
# 仅 TYO / OSA 等不与单个机场 IATA 冲突的 metro code 用作组
AIRPORT_GROUPS: dict[str, list[str]] = {
    "TYO": ["NRT", "HND"],       # 东京大都市圈
    "OSA": ["KIX", "ITM"],       # 大阪/关西
    "NGO": ["NGO"],              # 名古屋（单机场）
    "CTS": ["CTS"],              # 札幌（单机场）
    "FUK": ["FUK"],              # 福冈（单机场）
    "OKA": ["OKA"],              # 冲绳（单机场）
    "SYD": ["SYD"],              # 悉尼
    "MEL": ["MEL"],              # 墨尔本
    "LAX": ["LAX"],              # 洛杉矶
    "YVR": ["YVR"],              # 温哥华
    "SIN": ["SIN"],              # 新加坡
    "BKK": ["BKK", "DMK"],      # 曼谷
    "SEL": ["ICN", "GMP"],      # 首尔
    "CAN": ["CAN"],              # 广州
    "CTU": ["CTU"],              # 成都
    "HKG": ["HKG"],              # 香港
    "PEK": ["PEK", "PKX"],      # 北京
    "SZX": ["SZX"],              # 深圳
}

# 上海机场作为单机场使用（PVG=浦东, SHA=虹桥），不建组避免与 IATA 冲突
# 用户输入 PVG 或 SHA 各自独立，如需搜索两个机场分别填两条行程

# 已知单机场 IATA（验证用）
KNOWN_AIRPORTS: set[str] = {
    "NRT", "HND",               # 东京
    "PVG", "SHA",               # 上海
    "KIX", "ITM",               # 大阪
    "NGO",                      # 名古屋
    "CTS",                      # 札幌
    "FUK",                      # 福冈
    "OKA",                      # 冲绳
    "SYD", "MEL",               # 澳大利亚
    "LAX", "SFO", "JFK",        # 美国
    "YVR", "YYZ",               # 加拿大
    "SIN",                      # 新加坡
    "BKK", "DMK",               # 泰国
    "ICN", "GMP",               # 韩国
    "CAN", "CTU", "HKG",        # 中国大陆/港
    "PEK", "PKX", "SZX",
}

# 甩尾延伸目的地：真实目的地 → 常用 beyond 机场列表
# 搜索 origin→beyond，中转点即为真实目的地
THROWAWAY_BEYOND: dict[str, list[str]] = {
    # 想到东京，搜东京→以下目的地，中转东京的联程票
    "NRT": ["KIX", "CTS", "FUK", "OKA", "SYD", "LAX", "YVR", "SIN", "ICN"],
    "HND": ["KIX", "CTS", "FUK", "OKA", "SYD", "LAX", "YVR", "SIN", "ICN"],
    "TYO": ["KIX", "CTS", "FUK", "OKA", "SYD", "LAX", "YVR", "SIN", "ICN"],
    # 想到上海，搜上海→以下目的地，中转上海的联程票
    "PVG": ["CAN", "CTU", "SZX", "HKG", "SIN", "BKK", "ICN", "SYD"],
    "SHA": ["CAN", "CTU", "SZX", "HKG", "SIN", "BKK", "ICN", "SYD"],
    # 想到大阪
    "KIX": ["NGO", "CTS", "FUK", "SYD", "LAX"],
}


def expand_airport(code: str) -> list[str]:
    """把机场组展开为 IATA 列表；单机场直接返回 [code]。"""
    code = code.upper().strip()
    return AIRPORT_GROUPS.get(code, [code])


def normalize_airport(code: str) -> str:
    """统一大写；验证是否为已知机场或组代码。返回原值（不抛异常）。"""
    return code.upper().strip()


def get_route_pairs(origin: str, destination: str) -> list[tuple[str, str]]:
    """
    把 origin × destination 展开为所有 (IATA, IATA) 组合。

    例如:
        get_route_pairs("TYO", "PVG") → [("NRT","PVG"), ("HND","PVG")]
        get_route_pairs("NRT", "PVG") → [("NRT","PVG")]
    """
    origins = expand_airport(origin)
    destinations = expand_airport(destination)
    return [(o, d) for o in origins for d in destinations]


def get_throwaway_searches(origin: str, true_destination: str) -> list[tuple[str, str]]:
    """
    返回甩尾搜索的 (origin, beyond_destination) 对列表。
    搜到后检查中转点是否 == true_destination。

    例如 origin=PVG, true_destination=NRT:
    → [(PVG, KIX), (PVG, CTS), ...]  搜这些票，找中转在 NRT 的
    """
    origins = expand_airport(origin)
    dest_keys = expand_airport(true_destination)  # [NRT, HND] for TYO

    beyonds: list[tuple[str, str]] = []
    for dest_key in dest_keys:
        for beyond in THROWAWAY_BEYOND.get(dest_key, []):
            for orig in origins:
                if beyond != orig and beyond != dest_key:
                    beyonds.append((orig, beyond))

    # 去重，保持顺序
    seen: set[tuple[str, str]] = set()
    result = []
    for pair in beyonds:
        if pair not in seen:
            seen.add(pair)
            result.append(pair)
    return result[:8]  # 最多8个，控制请求量


def display_route(origin: str, destination: str) -> str:
    """返回人类可读的路线标签，如 TYO→PVG 或 NRT→PVG。"""
    return f"{origin}→{destination}"
