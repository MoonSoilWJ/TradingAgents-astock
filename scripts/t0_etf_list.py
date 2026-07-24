"""A 股 T+0 ETF 池 — 跨境、黄金、商品 ETF（可日内买卖，无 T+1 限制）。

所有跨境 ETF、黄金 ETF、商品 ETF、可转债 ETF 均支持 T+0。
本列表尽量覆盖全部有流动性的 T+0 品种。
"""

from __future__ import annotations

from pathlib import Path

# (code, name, sina_symbol)
CROSS_BORDER_ETFS: list[tuple[str, str, str]] = [
    # === 港股系列 ===
    ("159920", "恒生ETF", "sz159920"),
    ("510900", "H股ETF", "sh510900"),
    ("513180", "恒生科技ETF", "sh513180"),
    ("513130", "恒生科技指数ETF", "sh513130"),
    ("513010", "恒生科技30ETF", "sh513010"),
    ("513050", "中概互联ETF", "sh513050"),
    ("513330", "恒生互联网ETF", "sh513330"),
    ("513060", "恒生医疗ETF", "sh513060"),
    ("513120", "港股创新药ETF", "sh513120"),
    ("513190", "港股通互联网ETF", "sh513190"),
    ("513200", "港股通科技ETF", "sh513200"),
    ("513260", "恒生科技ETF", "sh513260"),
    ("513280", "港股科技50ETF", "sh513280"),
    ("513600", "恒生国企ETF", "sh513600"),
    ("513630", "港股通50ETF", "sh513630"),
    ("513660", "恒生医疗ETF", "sh513660"),
    ("513680", "港股通消费ETF", "sh513680"),
    ("513700", "港股通互联网ETF", "sh513700"),
    ("513730", "港股通50ETF", "sh513730"),
    ("513750", "港股通科技ETF", "sh513750"),
    ("513770", "港股互联网ETF华宝", "sh513770"),
    ("513800", "日本东证ETF", "sh513800"),
    ("513880", "日经225ETF", "sh513880"),
    ("513900", "港股通50ETF", "sh513900"),
    ("513950", "中东ETF", "sh513950"),
    ("513960", "港股通科技ETF", "sh513960"),
    ("513970", "港股通医药ETF", "sh513970"),
    ("513980", "港股通消费ETF", "sh513980"),
    ("513990", "恒生通科技ETF", "sh513990"),
    ("159691", "港股通科技ETF", "sz159691"),
    ("159740", "恒生科技ETF", "sz159740"),
    ("159745", "恒生红利ETF", "sz159745"),
    ("159792", "港股通科技ETF", "sz159792"),
    ("159808", "恒生科技ETF", "sz159808"),
    ("159824", "港股通医药ETF", "sz159824"),
    ("159887", "越南ETF", "sz159887"),
    ("159687", "亚太精选ETF", "sz159687"),
    ("159632", "恒生科技ETF", "sz159632"),
    ("159991", "港股创新药ETF", "sz159991"),
    ("513590", "恒生医疗ETF", "sh513590"),
    ("513110", "恒生科技ETF", "sh513110"),
    ("513580", "日经225ETF", "sh513580"),
    ("513620", "港股通科技ETF", "sh513620"),
    ("513690", "港股通消费ETF", "sh513690"),
    ("513720", "港股通医药ETF", "sh513720"),

    # === 美股系列 ===
    ("513100", "纳斯达克ETF", "sh513100"),
    ("513500", "标普500ETF", "sh513500"),
    ("159941", "纳指ETF", "sz159941"),
    ("513400", "纳斯达克指数ETF", "sh513400"),
    ("513300", "纳斯达克指数ETF", "sh513300"),
    ("513550", "标普500ETF", "sh513550"),
    ("513650", "标普500ETF", "sh513650"),
    ("159509", "纳指科技ETF", "sz159509"),
    ("159712", "标普ETF", "sz159712"),
    ("513850", "纳指100ETF", "sh513850"),
    ("513860", "标普医药ETF", "sh513860"),
    ("159518", "标普油气ETF", "sz159518"),
    ("159696", "标普消费ETF", "sz159696"),
    ("159697", "标普500ETF", "sz159697"),
    ("513530", "标普信息科技ETF", "sh513530"),
    ("161125", "标普中国新机会", "sz161125"),
    ("159685", "标普500ETF", "sz159685"),

    # === 日本/亚洲系列 ===
    ("513520", "日经ETF", "sh513520"),
    ("513880", "日经225ETF", "sh513880"),
    ("513580", "日经225ETF", "sh513580"),
    ("513800", "日本东证ETF", "sh513800"),

    # === 欧洲系列 ===
    ("513030", "德国ETF", "sh513030"),
    ("513080", "法国ETF", "sh513080"),
    ("513400", "德国DAXETF", "sh513400"),

    # === 其他跨境 ===
    ("159687", "亚太精选ETF", "sz159687"),
    ("159887", "越南ETF", "sz159887"),
    ("513950", "中东ETF", "sh513950"),
    ("159745", "恒生红利ETF", "sz159745"),
    ("159792", "港股通科技ETF", "sz159792"),
    ("159510", "中韩半导体ETF", "sz159510"),
    ("159541", "中韩半导体ETF", "sz159541"),
    ("159658", "中阿ETF", "sz159658"),
    ("513520", "日经ETF", "sh513520"),
    ("513850", "纳指100ETF", "sh513850"),
    ("513860", "标普医药ETF", "sh513860"),
    ("513530", "标普信息科技ETF", "sh513530"),
    ("159612", "恒生科技ETF", "sz159612"),
    ("159615", "恒生科技ETF", "sz159615"),
    ("159620", "恒生科技ETF", "sz159620"),
    ("159625", "恒生科技ETF", "sz159625"),
    ("159628", "恒生科技ETF", "sz159628"),
    ("159636", "恒生科技ETF", "sz159636"),
    ("159643", "恒生科技ETF", "sz159643"),
    ("159655", "恒生科技ETF", "sz159655"),
    ("159723", "恒生科技ETF", "sz159723"),
    ("159740", "恒生科技ETF", "sz159740"),
    ("159824", "港股通医药ETF", "sz159824"),
    ("159833", "恒生科技ETF", "sz159833"),
    ("159840", "恒生科技ETF", "sz159840"),
    ("159856", "恒生科技ETF", "sz159856"),
    ("159863", "恒生科技ETF", "sz159863"),
    ("159876", "恒生科技ETF", "sz159876"),
    ("159887", "越南ETF", "sz159887"),
    ("159888", "恒生科技ETF", "sz159888"),
    ("159892", "恒生科技ETF", "sz159892"),
    ("159895", "恒生科技ETF", "sz159895"),
    ("159899", "恒生科技ETF", "sz159899"),
    ("159901", "恒生科技ETF", "sz159901"),
]

# 黄金 ETF
GOLD_ETFS: list[tuple[str, str, str]] = [
    ("518880", "黄金ETF", "sh518880"),
    ("159812", "黄金基金", "sz159812"),
    ("159934", "黄金ETF", "sz159934"),
    ("518600", "上海金ETF", "sh518600"),
    ("518850", "黄金ETF", "sh518850"),
    ("518660", "黄金ETF", "sh518660"),
    ("518800", "黄金ETF", "sh518800"),
    ("159562", "黄金股票ETF", "sz159562"),
    ("517520", "黄金ETF", "sh517520"),
    ("159934", "黄金ETF", "sz159934"),
    ("518880", "黄金ETF", "sh518880"),
    ("159812", "黄金基金", "sz159812"),
]

# 商品 ETF
COMMODITY_ETFS: list[tuple[str, str, str]] = [
    ("159985", "豆粕ETF", "sz159985"),
    ("162411", "原油基金", "sz162411"),
    ("159981", "能源化工ETF", "sz159981"),
    ("562990", "稀土ETF", "sh562990"),
    ("159518", "标普油气ETF", "sz159518"),
    ("159696", "标普消费ETF", "sz159696"),
    ("159697", "标普500ETF", "sz159697"),
    ("159612", "恒生科技ETF", "sz159612"),
    ("162719", "原油ETF", "sz162719"),
    ("501018", "南方原油", "sh501018"),
    ("161129", "原油基金", "sz161129"),
]

# 可转债 ETF
BOND_ETFS: list[tuple[str, str, str]] = [
    ("511380", "可转债ETF", "sh511380"),
    ("511180", "上证可转债ETF", "sh511180"),
    ("159649", "可转债ETF", "sz159649"),
]


def get_all_t0_etfs() -> list[dict]:
    """返回所有 T+0 ETF 列表（去重）。"""
    seen: set[str] = set()
    result: list[dict] = []
    for pool, type_name in [
        (CROSS_BORDER_ETFS, "跨境"),
        (GOLD_ETFS, "黄金"),
        (COMMODITY_ETFS, "商品"),
        (BOND_ETFS, "债券"),
    ]:
        for code, name, sina in pool:
            if code in seen:
                continue
            seen.add(code)
            result.append({
                "code": code,
                "name": name,
                "etf_code": code,
                "etf_name": name,
                "etf_raw": code,
                "sina_symbol": sina,
                "type_name": type_name,
            })
    return result


def sina_symbol_for(code: str) -> str:
    if code.startswith(("5", "6")):
        return f"sh{code}"
    return f"sz{code}"


def filter_t0_settlement(etf_list: list[dict], *, live_names: dict[str, str] | None = None) -> list[dict]:
    """仅保留 settlement_rule 判定为 T+0 的标的（可用实时名称覆盖）。"""
    from tradingagents.dataflows.instrument import settlement_rule  # noqa: PLC0415

    out: list[dict] = []
    for info in etf_list:
        code = info["code"]
        name = (live_names or {}).get(code) or info.get("name") or info.get("etf_name")
        if settlement_rule(code, name) == "T0":
            row = dict(info)
            if live_names and code in live_names:
                row["name"] = live_names[code]
                row["etf_name"] = live_names[code]
            out.append(row)
    return out


def get_t0_only_etfs() -> list[dict]:
    """原 T+0 池经交割规则过滤（剔除误收录的 T+1）。"""
    return filter_t0_settlement(get_all_t0_etfs())


def get_quality_etfs(path: Path | None = None) -> list[dict]:
    """扫描全集（原 T+0 + 额外标的）；缺失时回退原 T+0 池。"""
    from quality_pool import DEFAULT_POOL_PATH, get_scan_universe, has_quality_rules  # noqa: PLC0415

    if has_quality_rules(path or DEFAULT_POOL_PATH):
        uni = get_scan_universe(path)
        if uni:
            return uni
    return get_all_t0_etfs()


def get_all_market_etf_lof() -> list[dict]:
    """全市场场内 ETF/LOF（mootdx 名单，含 T+0 与 T+1）。"""
    from tradingagents.dataflows.instrument import is_on_exchange_etf_code  # noqa: PLC0415
    from tradingagents.dataflows.a_stock import lookup_astock_name  # noqa: PLC0415

    try:
        from tradingagents.dataflows.a_stock import _build_name_code_map  # noqa: PLC0415

        _, code_to_name = _build_name_code_map()
    except Exception:
        code_to_name = {}

    result: list[dict] = []
    for code, name in sorted(code_to_name.items()):
        if not is_on_exchange_etf_code(code):
            continue
        clean = name.strip()
        result.append({
            "code": code,
            "name": clean,
            "etf_code": code,
            "etf_name": clean,
            "etf_raw": code,
            "sina_symbol": sina_symbol_for(code),
            "type_name": "全市场",
        })
    return result


def pool_stats(etf_list: list[dict], *, live_names: dict[str, str] | None = None) -> dict:
    from tradingagents.dataflows.instrument import settlement_rule  # noqa: PLC0415

    t0 = t1 = 0
    for info in etf_list:
        code = info["code"]
        name = (live_names or {}).get(code) or info.get("name")
        if settlement_rule(code, name) == "T0":
            t0 += 1
        else:
            t1 += 1
    return {"total": len(etf_list), "t0": t0, "t1": t1}
