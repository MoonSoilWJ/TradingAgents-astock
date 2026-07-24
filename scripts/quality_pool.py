#!/usr/bin/env python3
"""优质 ETF 池 — 由 100 日交易复盘推导筛选规则 + walk-forward 滚动定池。

100 日复盘结论（全市场+原池 135 笔，2026-02~07）:
  - 商品能源×中性: 均 +3.47%, 胜 75% → 保留
  - 港股×中性: 100日复盘均 -0.67%; 不再永久剔除，交给训练窗滚动 + regime 白名单
  - T1 主题 LOF×中性: 均 +0.85% → 中性期可入池
  - 成交额: 极低(<500万)多笔大亏(501097/161626); 501046 仅 2162 万仍盈利 → 下限 500 万非 3000 万
  - 训练窗入池: n>=2 且复利>0, 或单笔>=5%; 训练窗 n>=2 复利<-3% 剔除

全市场 → 品类/黑名单/流动性 → 训练窗模拟 → quality_pool.json
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from backtest_t0_hybrid_sell import SIGNAL_TIME, run_strategy
from backtest_t0_today1 import (
    FEE_PCT,
    MIN_GAIN,
    passes_gain_filter,
    rank_by_today_gain,
    regime_on_date,
)
from t0_etf_list import get_all_market_etf_lof, sina_symbol_for

# 100 日复盘：商品/原油在趋势+中性均稳定盈利
CORE_CODES: tuple[str, ...] = (
    "501018", "161129", "159981", "162411", "162719", "159509",
    "518880", "159518",
)

# 极低流动性结构性黑名单（其余差票交给训练窗 compound<-3% 滚动剔除）
BLACKLIST_CODES: frozenset[str] = frozenset({
    "161626",  # 融通通福 ~2万/日，滑点/买不到
})

# 复盘：Q25≈6691万仍盈利; 剔除<500万极端冷门
MIN_AMOUNT_CNY = 5_000_000
LIQUIDITY_LOOKBACK = 20
MIN_TRAIN_PICKS = 2
MIN_TRAIN_COMPOUND_PCT = 0.0
MIN_SINGLE_PICK_RETURN_PCT = 5.0
MAX_TRAIN_COMPOUND_EXCLUDE = -3.0  # 训练窗 n>=2 复利低于此 → 不入池
MAX_POOL_SIZE = 25
DEFAULT_LOOKBACK = 30

# 选股时按 regime 限制品类；港股不再永久剔除，训练窗表现差则自然出池
ALLOWED_CATEGORIES: dict[str, frozenset[str]] = {
    "趋势": frozenset({"商品能源", "美股", "T1主题LOF", "港股", "其他", "海外其他"}),
    "中性": frozenset({"商品能源", "T1主题LOF", "港股", "其他"}),
}
EXCLUDE_CATEGORIES: frozenset[str] = frozenset()

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POOL_PATH = REPO_ROOT / "strategies" / "data" / "quality_pool.json"

SELECTION_RULES_DOC = {
    "source": "100d trade post-mortem 2026-02~07",
    "exclude_categories": sorted(EXCLUDE_CATEGORIES),
    "blacklist_codes": sorted(BLACKLIST_CODES),
    "min_amount_cny": MIN_AMOUNT_CNY,
    "train_include": "n>=2 compound>0 OR single trade>=5%",
    "train_exclude": f"n>=2 compound<{MAX_TRAIN_COMPOUND_EXCLUDE}%",
    "pick_by_regime": {k: sorted(v) for k, v in ALLOWED_CATEGORIES.items()},
}


def trade_category(name: str | None, code: str = "") -> str:
    """与 100 日复盘脚本一致的品类标签。"""
    n = name or ""
    if any(x in n for x in ("原油", "石油", "油气", "能源", "化工", "黄金", "白银", "商品", "豆粕", "有色", "铜")):
        if any(x in n for x in ("科创", "芯片", "半导体", "LOF")) and "油" not in n and "金" not in n:
            return "T1主题LOF"
        return "商品能源"
    if any(x in n for x in ("恒生", "港股", "H股", "中概", "香港", "创新药")):
        return "港股"
    if any(x in n for x in ("纳指", "标普", "美股", "纳斯达克", "美国")):
        return "美股"
    if any(x in n for x in (
        "科创", "芯片", "半导体", "通信", "卫星", "军工", "医药", "消费", "证券",
        "新能源", "LOF", "加银", "国寿", "福鑫", "芯易",
    )):
        return "T1主题LOF"
    if any(x in n for x in ("日经", "日本", "东证", "亚太", "越南")):
        return "海外其他"
    return "其他"


def _avg_daily_amount(etf_daily: dict, code: str, dates: list[str]) -> float:
    info = etf_daily.get(code)
    if not info:
        return 0.0
    idx = {r["date"]: r for r in info["returns"]}
    amounts: list[float] = []
    for d in dates[-LIQUIDITY_LOOKBACK:]:
        r = idx.get(d)
        if not r:
            continue
        close = float(r.get("close") or 0)
        vol = float(r.get("volume") or 0)
        if close > 0 and vol > 0:
            amounts.append(close * vol * 100)
    return sum(amounts) / len(amounts) if amounts else 0.0


def universe_from_rules(
    etf_daily: dict,
    etf_5min: dict,
    as_of_dates: list[str],
    *,
    min_amount: float = MIN_AMOUNT_CNY,
) -> list[dict]:
    """全市场 ∩ 5分K ∩ 流动性 ∩ 非结构性黑名单。"""
    codes_5m = set(etf_5min.keys())
    out: list[dict] = []
    for e in get_all_market_etf_lof():
        code = e["code"]
        if code not in codes_5m or code in BLACKLIST_CODES:
            continue
        if _avg_daily_amount(etf_daily, code, as_of_dates) < min_amount:
            continue
        out.append(e)
    return out


def _trades_in_window(
    pool: list[dict],
    train_dates: list[str],
    etf_daily: dict,
    etf_5min: dict,
    all_dates: list[str],
    proxy_klines: list[dict],
) -> list[dict]:
    picks: dict = {}
    for day in train_dates:
        picks[(SIGNAL_TIME, day)] = pick_top1_from_pool(
            pool, day, etf_daily, etf_5min, proxy_klines,
            use_regime_filter=False,
        )
    r = run_strategy("trix", train_dates, all_dates, picks, etf_5min, FEE_PCT)
    return (r or {}).get("trades") or []


def build_pool_from_train(
    train_dates: list[str],
    etf_daily: dict,
    etf_5min: dict,
    all_dates: list[str],
    proxy_klines: list[dict],
    *,
    min_amount: float = MIN_AMOUNT_CNY,
    max_size: int = MAX_POOL_SIZE,
) -> list[dict]:
    """训练窗：复盘规则宇宙 → 模拟成交 → 表现入池 + 核心底仓。"""
    if len(train_dates) < 5:
        return []

    universe = universe_from_rules(etf_daily, etf_5min, train_dates, min_amount=min_amount)
    if len(universe) < 5:
        return universe

    trades = _trades_in_window(
        universe, train_dates, etf_daily, etf_5min, all_dates, proxy_klines,
    )
    by_code: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_code[t["etf"]].append(t["return_pct"])

    scored: list[tuple[float, str]] = []
    for code, rets in by_code.items():
        if code in CORE_CODES or code in BLACKLIST_CODES:
            continue
        eq = 1.0
        for r in rets:
            eq *= 1 + r / 100
        compound = (eq - 1) * 100
        if len(rets) >= MIN_TRAIN_PICKS:
            if compound <= MAX_TRAIN_COMPOUND_EXCLUDE:
                continue
            if compound > MIN_TRAIN_COMPOUND_PCT:
                scored.append((compound, code))
        elif len(rets) == 1 and rets[0] >= MIN_SINGLE_PICK_RETURN_PCT:
            scored.append((rets[0], code))
    scored.sort(reverse=True)

    am_map = {e["code"]: e for e in get_all_market_etf_lof()}
    chosen: list[str] = []
    for code in CORE_CODES:
        if code in am_map and code in etf_5min and code not in chosen:
            chosen.append(code)
    for _, code in scored:
        if code not in chosen:
            chosen.append(code)
        if len(chosen) >= max_size:
            break

    return [am_map[c] for c in chosen[:max_size] if c in am_map]


def _pick_first_allowed(
    rows: list[tuple[float, str, str]],
    regime: dict | None,
    *,
    use_regime_filter: bool,
    t0_only: bool,
) -> tuple[str, float, str] | None:
    from tradingagents.dataflows.instrument import settlement_rule

    mode = (regime or {}).get("mode", "中性")
    allowed = ALLOWED_CATEGORIES.get(mode, ALLOWED_CATEGORIES["中性"]) if use_regime_filter else None
    for g, code, name in rows:
        if not passes_gain_filter(g):
            continue
        if code in BLACKLIST_CODES:
            continue
        if t0_only and settlement_rule(code, name) != "T0":
            continue
        cat = trade_category(name, code)
        if EXCLUDE_CATEGORIES and cat in EXCLUDE_CATEGORIES:
            continue
        if allowed is not None and cat not in allowed:
            continue
        return code, g, name
    return None


def pick_top1_from_pool(
    pool: list[dict],
    day: str,
    etf_daily: dict,
    etf_5min: dict,
    proxy_klines: list[dict],
    *,
    skip_choppy: bool = True,
    use_regime_filter: bool = True,
    t0_only: bool = False,
    signal_time: str = SIGNAL_TIME,
) -> tuple[str, float, str] | None:
    reg = regime_on_date(proxy_klines, day)
    if skip_choppy and reg and reg.get("skip_choppy"):
        return None
    scores = rank_by_today_gain(pool, etf_daily, etf_5min, day, signal_time)
    rows = [
        (g, e["code"], e.get("name") or e.get("etf_name") or e["code"])
        for g, e in scores
    ]
    return _pick_first_allowed(rows, reg, use_regime_filter=use_regime_filter, t0_only=t0_only)


def pick_from_ranked_live(
    ranked: list[dict],
    regime: dict | None,
    *,
    use_regime_filter: bool = True,
    t0_only: bool = False,
) -> dict | None:
    """实盘：已按涨幅排序的行情列表 → 与回测一致的品类/regime 过滤。"""
    rows = [
        (float(r["today_gain"]), r["code"], r.get("name") or r.get("etf_name") or r["code"])
        for r in ranked
    ]
    picked = _pick_first_allowed(
        rows, regime, use_regime_filter=use_regime_filter, t0_only=t0_only,
    )
    if not picked:
        return None
    code = picked[0]
    return next(r for r in ranked if r["code"] == code)


def build_picks_fixed(
    eval_dates: list[str],
    lookback: int,
    etf_daily: dict,
    etf_5min: dict,
    all_dates: list[str],
    proxy_klines: list[dict],
) -> tuple[dict, list[dict]]:
    if len(eval_dates) <= lookback:
        return {}, []
    train = eval_dates[:lookback]
    pool = build_pool_from_train(
        train, etf_daily, etf_5min, all_dates, proxy_klines,
    )
    picks: dict = {}
    for day in eval_dates:
        if day in train:
            picks[(SIGNAL_TIME, day)] = None
            continue
        picks[(SIGNAL_TIME, day)] = pick_top1_from_pool(
            pool, day, etf_daily, etf_5min, proxy_klines,
        )
    return picks, pool


def build_picks_rolling(
    eval_dates: list[str],
    lookback: int,
    etf_daily: dict,
    etf_5min: dict,
    all_dates: list[str],
    proxy_klines: list[dict],
) -> tuple[dict, list[list[dict]]]:
    if len(eval_dates) <= lookback:
        return {}, []
    picks: dict = {}
    pool_history: list[list[dict]] = []
    for i in range(lookback, len(eval_dates)):
        day = eval_dates[i]
        train = eval_dates[i - lookback:i]
        pool = build_pool_from_train(
            train, etf_daily, etf_5min, all_dates, proxy_klines,
        )
        pool_history.append(pool)
        picks[(SIGNAL_TIME, day)] = pick_top1_from_pool(
            pool, day, etf_daily, etf_5min, proxy_klines,
        )
    return picks, pool_history


def build_picks_static_pool(
    pool: list[dict],
    eval_dates: list[str],
    etf_daily: dict,
    etf_5min: dict,
    proxy_klines: list[dict],
) -> dict:
    picks: dict = {}
    for day in eval_dates:
        picks[(SIGNAL_TIME, day)] = pick_top1_from_pool(
            pool, day, etf_daily, etf_5min, proxy_klines,
        )
    return picks


def compound_returns(rets: list[float]) -> float:
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return (eq - 1) * 100


def save_quality_pool(
    pool: list[dict],
    path: Path,
    *,
    lookback: int,
    train_end: str,
    meta: dict | None = None,
) -> Path:
    from tradingagents.dataflows.instrument import settlement_rule

    path.parent.mkdir(parents=True, exist_ok=True)
    from t0_etf_list import get_all_t0_etfs  # noqa: PLC0415

    orig_codes = {e["code"] for e in get_all_t0_etfs()}
    pool_codes = {e["code"] for e in pool}
    extra = [c for c in pool_codes if c not in orig_codes]
    am_map = {e["code"]: e for e in get_all_market_etf_lof()}
    extra_rows = [
        {
            "code": c,
            "name": (am_map[c].get("name") if c in am_map else c),
            "category": trade_category(am_map[c].get("name") if c in am_map else "", c),
        }
        for c in extra
    ]
    payload = {
        "version": 2,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "lookback_days": lookback,
        "train_end": train_end,
        "scan_mode": "original_t0_plus_extra",
        "selection_rules": SELECTION_RULES_DOC,
        "core_codes": list(CORE_CODES),
        "extra_codes": extra_rows,
        "meta": meta or {},
        "hybrid_mode": {
            "enabled": True,
            "rule": "趋势/震荡→滚动优质池(震荡仍交易)；中性→原T0池",
            "lookback_days": lookback,
        },
        "etfs": [
            {
                "code": e["code"],
                "name": e.get("name") or e.get("etf_name", ""),
                "sina_symbol": e.get("sina_symbol") or sina_symbol_for(e["code"]),
                "settlement": settlement_rule(e["code"], e.get("name")),
                "category": trade_category(e.get("name"), e["code"]),
            }
            for e in pool
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def refresh_and_save(
    etf_daily: dict,
    etf_5min: dict,
    all_dates: list[str],
    proxy_klines: list[dict],
    eval_dates: list[str],
    *,
    lookback: int = DEFAULT_LOOKBACK,
    path: Path | None = None,
) -> Path:
    train = eval_dates[-lookback:] if len(eval_dates) >= lookback else eval_dates
    pool = build_pool_from_train(
        train, etf_daily, etf_5min, all_dates, proxy_klines,
    )
    return save_quality_pool(
        pool,
        path or DEFAULT_POOL_PATH,
        lookback=lookback,
        train_end=train[-1],
        meta={"signal_days": len(eval_dates), "mode": "rolling_latest"},
    )


def load_quality_pool_meta(path: Path | None = None) -> dict:
    p = path or DEFAULT_POOL_PATH
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def load_quality_pool(path: Path | None = None) -> list[dict]:
    """JSON 中 etfs 字段：训练窗额外入选（非扫描全集）。"""
    data = load_quality_pool_meta(path)
    out: list[dict] = []
    for row in data.get("etfs") or []:
        code = row["code"]
        out.append({
            "code": code,
            "name": row.get("name", code),
            "etf_code": code,
            "etf_name": row.get("name", code),
            "etf_raw": code,
            "sina_symbol": row.get("sina_symbol") or sina_symbol_for(code),
            "type_name": "quality",
        })
    return out


def get_scan_universe(path: Path | None = None) -> list[dict]:
    """扫描全集 = 原 T+0 池 + 训练窗额外标的（T+0/T+1）；选股仍走 v2 规则过滤。"""
    from t0_etf_list import get_all_t0_etfs  # noqa: PLC0415

    base = get_all_t0_etfs()
    base_codes = {e["code"] for e in base}
    am_map = {e["code"]: e for e in get_all_market_etf_lof()}
    extras: list[dict] = []
    for row in load_quality_pool_meta(path).get("extra_codes") or []:
        code = row if isinstance(row, str) else row.get("code")
        if not code or code in base_codes:
            continue
        if code in am_map:
            extras.append(am_map[code])
        else:
            extras.append({
                "code": code,
                "name": (row.get("name") if isinstance(row, dict) else code),
                "etf_code": code,
                "etf_name": (row.get("name") if isinstance(row, dict) else code),
                "sina_symbol": sina_symbol_for(code),
                "type_name": "quality_extra",
            })
    # 兼容旧 JSON：etfs 里不在原池的也并入扫描
    for e in load_quality_pool(path):
        if e["code"] not in base_codes and e["code"] not in {x["code"] for x in extras}:
            extras.append(e)
    return base + extras


def has_quality_rules(path: Path | None = None) -> bool:
    return (path or DEFAULT_POOL_PATH).exists()


# 混合选池方案（walk-forward OOS 对比后 A 略优，见 backtest_parallel_pool.py）
HYBRID_SCHEME_A = "A"  # 趋势+震荡→优质池；中性→原T0池（震荡仍交易）
HYBRID_SCHEME_B = "B"  # 趋势+中性→原T0池；震荡→优质池（震荡仍交易）
DEFAULT_HYBRID_SCHEME = HYBRID_SCHEME_A


def pick_orig_top1(
    pool: list[dict],
    day: str,
    etf_daily: dict,
    etf_5min: dict,
    proxy_klines: list[dict],
    *,
    signal_time: str = SIGNAL_TIME,
) -> tuple[str, float, str] | None:
    """原 T+0 池 Top1（震荡跳过；不过滤 T+1，与混合回测一致）。"""
    reg = regime_on_date(proxy_klines, day)
    if reg and reg.get("skip_choppy"):
        return None
    scores = rank_by_today_gain(pool, etf_daily, etf_5min, day, signal_time)
    cands = [(g, e) for g, e in scores if passes_gain_filter(g)]
    if not cands:
        return None
    g, e = cands[0]
    return e["code"], g, e.get("name") or e.get("etf_name") or e["code"]


def _quality_pool_for_day(
    day: str,
    eval_dates: list[str],
    etf_daily: dict,
    etf_5min: dict,
    all_dates: list[str],
    proxy_klines: list[dict],
    *,
    lookback: int,
    static_quality: list[dict],
    orig_pool: list[dict],
) -> list[dict]:
    idx_map = {d: i for i, d in enumerate(eval_dates)}
    j = idx_map.get(day, -1)
    if j >= lookback:
        train = eval_dates[j - lookback:j]
        return build_pool_from_train(
            train, etf_daily, etf_5min, all_dates, proxy_klines,
        )
    if static_quality:
        return static_quality
    return orig_pool


def regime_uses_quality_pool(regime: dict | None, *, scheme: str = DEFAULT_HYBRID_SCHEME) -> bool:
    """混合策略下当前环境是否用优质池。"""
    if not regime:
        return scheme == HYBRID_SCHEME_A
    mode = regime.get("mode")
    if scheme == HYBRID_SCHEME_B:
        return mode == "震荡"
    return mode in ("趋势", "震荡")


def hybrid_should_skip_choppy(regime: dict | None, *, hybrid: bool = True) -> bool:
    """混合策略下震荡期用优质池继续交易，不跳过。"""
    if not regime or not regime.get("skip_choppy"):
        return False
    if hybrid and regime.get("mode") == "震荡":
        return False
    return True


def build_picks_hybrid(
    eval_dates: list[str],
    orig_pool: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    all_dates: list[str],
    proxy_klines: list[dict],
    *,
    lookback: int = DEFAULT_LOOKBACK,
    warmup: int = 0,
    static_quality: list[dict] | None = None,
    scheme: str = DEFAULT_HYBRID_SCHEME,
    signal_times: list[str] | None = None,
) -> dict:
    """混合选池回测。

    A（默认）: 趋势/震荡→滚动优质池；中性→原 T+0 池
    B: 趋势/中性→原 T+0 池；震荡→滚动优质池（震荡仍交易）
    """
    static_quality = static_quality if static_quality is not None else load_quality_pool()
    sigs = signal_times or [SIGNAL_TIME]
    picks: dict = {}
    for sig in sigs:
        for i, day in enumerate(eval_dates):
            if i < warmup:
                picks[(sig, day)] = None
                continue
            reg = regime_on_date(proxy_klines, day)
            if regime_uses_quality_pool(reg, scheme=scheme):
                pool = _quality_pool_for_day(
                    day, eval_dates, etf_daily, etf_5min, all_dates, proxy_klines,
                    lookback=lookback, static_quality=static_quality, orig_pool=orig_pool,
                )
                picks[(sig, day)] = pick_top1_from_pool(
                    pool, day, etf_daily, etf_5min, proxy_klines,
                    skip_choppy=(reg or {}).get("mode") != "震荡",
                    use_regime_filter=True,
                    signal_time=sig,
                )
            else:
                picks[(sig, day)] = pick_orig_top1(
                    orig_pool, day, etf_daily, etf_5min, proxy_klines,
                    signal_time=sig,
                )
    return picks


def pick_hybrid_from_ranked(
    ranked: list[dict],
    regime: dict | None,
    *,
    orig_pool: list[dict],
    quality_pool: list[dict],
    scheme: str = DEFAULT_HYBRID_SCHEME,
) -> tuple[dict | None, list[dict], str]:
    """实盘/监控混合选池（默认方案 A）。

    A: 趋势/震荡→优质池；中性→原 T+0 池
    B: 趋势/中性→原 T+0 池；震荡→优质池
    """
    mode = (regime or {}).get("mode", "中性")
    orig_codes = {e["code"] for e in orig_pool}
    qual_codes = {e["code"] for e in quality_pool}

    if regime_uses_quality_pool(regime, scheme=scheme) and quality_pool:
        sub = [r for r in ranked if r["code"] in qual_codes]
        top = pick_from_ranked_live(sub, regime, use_regime_filter=True, t0_only=False)
        tag = "优质池·震荡" if scheme == HYBRID_SCHEME_B else f"优质池·{mode}"
        return top, sub or ranked, tag

    sub = [r for r in ranked if r["code"] in orig_codes]
    for row in sub:
        if row["today_gain"] >= MIN_GAIN:
            tag = "原T0池·中性" if mode == "中性" else f"原T0池·{mode}"
            return row, sub or ranked, tag
    return None, sub or ranked, f"原T0池·{mode}"


def build_picks_rules_on_universe(
    universe: list[dict],
    eval_dates: list[str],
    etf_daily: dict,
    etf_5min: dict,
    proxy_klines: list[dict],
    *,
    oos_from: int = 0,
) -> dict:
    """原池（或指定 universe）+ v2 品类/regime 规则，不做缩池。"""
    picks: dict = {}
    for i, day in enumerate(eval_dates):
        if i < oos_from:
            picks[(SIGNAL_TIME, day)] = None
            continue
        picks[(SIGNAL_TIME, day)] = pick_top1_from_pool(
            universe, day, etf_daily, etf_5min, proxy_klines,
        )
    return picks


if __name__ == "__main__":
    import argparse
    from backtest_t0_today1 import resolve_eval_dates

    parser = argparse.ArgumentParser(description="刷新 strategies/data/quality_pool.json")
    parser.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK)
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--cache", type=str, default=str(
        Path.home() / ".tradingagents/cache/t0_5min/pool_20260721_days100_allmarket.json"
    ))
    args = parser.parse_args()
    cache_path = Path(args.cache)
    if not cache_path.exists():
        print(f"ERROR: 缓存不存在 {cache_path}")
        raise SystemExit(1)
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    eval_dates = resolve_eval_dates(cache["all_dates"], args.days, "", "")
    out = refresh_and_save(
        cache["etf_daily"], cache["etf_5min"], cache["all_dates"], cache["proxy_klines"],
        eval_dates, lookback=args.lookback,
    )
    pool = load_quality_pool(out)
    print(f"已刷新: {out} ({len(pool)} 只)")
    for e in pool:
        print(f"  {e['code']} {e['name']} [{trade_category(e['name'], e['code'])}]")
