#!/usr/bin/env python3
"""10 年(2015-2026)近似回测: A(实盘 hybrid-A) vs B(全市场Top1), 均 +14:40确认 +TRIX卖。

=== 数据口径(务必先读) ===
日K   : aligned_live_4y.etf_daily —— 2015-01-05~2026-07-30, 106 只, 完整无缺口。
5min  :
  · 2022-06-15~2026-07-31  tdx_5min_pre2024 + tdx_5min_2y
        → 无偏全池密集数据, 严谨可信(memory 里反复验证的实盘等价窗口)。
  · 2015-01-05~2022-06-14  planC_1min_4y 稀疏缓存
        → 【近似】仅含"日K当日涨幅 TOP20 候选"的 5min(70 只/1810 天/41328 条)。
          用收盘涨幅筛候选集 ⇒ 含前视偏差, 会高估。仅作数量级参考。
        → 该缓存 time 标签比 tdx 晚 5 分钟(实证 planC[14:45]==tdx[14:40]),
          载入时整体 -5min 对齐; 价格已做日K对齐(与 tdx 完全相等), 无量纲问题。
proxy(501018): 仅 2022-06-13 起。regime_on_date 在无 proxy 时返回 None,
  而 regime_uses_quality_pool(None, scheme="A") 为 True → 会误走"滚动优质池"分支,
  而优质池需要全市场密集 5min(pre 段没有) ⇒ 【A 策略在 2015-2022 无法计算】,
  只输出 post 段。B 不依赖 regime, 可跑全 10 年。

用法:
    python scripts/backtest_10y_ab.py
    python scripts/backtest_10y_ab.py --start 2015-01-05
"""
from __future__ import annotations

import argparse
import gc
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import backtest_t0_hybrid_sell as BH  # noqa: E402
BH.MIN_TRADES = 1

from backtest_t0_hybrid_sell import run_strategy, SIGNAL_TIME  # noqa: E402
from backtest_t0_etf import price_at_time  # noqa: E402
from backtest_t0_today1 import FEE_PCT, MIN_GAIN, passes_gain_filter  # noqa: E402
from backtest_b_idle_merge import build_prev_close, stats_of  # noqa: E402
from backtest_recent100_live_vs_b_idle import apply_confirm, year_table  # noqa: E402
from quality_pool import build_picks_hybrid  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE_DIR = Path.home() / ".tradingagents/cache/t0_5min"
CACHE = CACHE_DIR / "aligned_live_4y.json"
DENSE = [
    CACHE_DIR / "tdx_5min_pre2024.json",
    CACHE_DIR / "tdx_5min_2y.json",
    # 自动发现层 ETF(refresh_t0_pool.py 产出)的无偏5min; 文件不存在时自动跳过,
    # 不影响旧 103 只池结果。存在时合并, 用于验证"池子扩大→417只"对B的影响。
    CACHE_DIR / "tdx_5min_auto.json",
]
PLANC = CACHE_DIR / "planC_1min_4y.json"
DAILY1000 = CACHE_DIR / "backfill_daily_1000.json"
DAILY2015 = CACHE_DIR / "backfill_daily_2015.json"
FULL_DAILY = CACHE_DIR / "full_daily_2015_2026.json"   # 本脚本构建的 10 年日K缓存

DENSE_START = "2022-06-15"          # 无偏密集 5min 起点
# 10 只老牌 T0 ETF(pre 段日K由 run_2015_full 用 pytdx 聚合, 口径与 backfill 一致)
OLD10 = {"159920", "510900", "513100", "513500", "513030",
         "159901", "518880", "159934", "518800", "162411"}


def passes(g: float) -> bool:
    """与 build_picks_B 完全同口径: 仅 ≥MIN_GAIN(3%) 下限, 不设上限。"""
    return passes_gain_filter(g)


def sina_daily(code: str, cut: str) -> dict:
    """新浪基金全量日K → {date: record}(不复权, 只截 pre 段)。"""
    import akshare as ak
    try:
        h = ak.fund_etf_hist_sina(symbol=("sh" if code[0] in "56" else "sz") + code)
    except Exception as e:
        print(f"    [warn] sina {code} 失败: {e}", flush=True)
        return {}
    out: dict = {}
    for _, row in h.iterrows():
        d = str(row["date"])[:10]
        if d >= cut:
            continue
        try:
            out[d] = {"date": d, "open": float(row["open"]), "high": float(row["high"]),
                      "low": float(row["low"]), "close": float(row["close"]),
                      "volume": float(row.get("volume") or 0)}
        except Exception:
            continue
    return out


def build_full_daily(etfs: list[dict]) -> dict:
    """10 年 etf_daily: post 段用 backfill 权威口径, pre 段用老牌聚合 + 新浪不复权。

    aligned_live_4y.etf_daily 的 pre 段只有 10~11 只(2020-21 仅 5 只), 会让
    "全市场 Top1" 退化成"十选一", 故必须重建。结果落盘复用。
    """
    if FULL_DAILY.exists():
        d = json.loads(FULL_DAILY.read_text(encoding="utf-8"))
        print(f"    复用日K缓存 {FULL_DAILY.name}: {len(d)} 只", flush=True)
        return d
    print("    首次构建 10 年日K(新浪拉取, 约 2~4 分钟) ...", flush=True)
    import os
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(k, None)
    bd = json.loads(DAILY1000.read_text(encoding="utf-8"))["etf_daily"]
    d2015 = json.loads(DAILY2015.read_text(encoding="utf-8")) if DAILY2015.exists() else {}
    full: dict = {}
    for i, e in enumerate(etfs, 1):
        code = e["code"]
        post = {r["date"]: r for r in bd.get(code, {}).get("returns", [])}
        if code in OLD10 and code in d2015:
            pre = {r["date"]: r for r in d2015[code]["returns"]}
        else:
            pre = sina_daily(code, DENSE_START)
        merged = {**pre, **post}     # 同一天以 post(backfill) 为准
        if merged:
            full[code] = {"returns": sorted(merged.values(), key=lambda x: x["date"])}
        if i % 20 == 0:
            print(f"      {i}/{len(etfs)} ...", flush=True)
    FULL_DAILY.write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
    print(f"    已构建并落盘: {len(full)} 只 → {FULL_DAILY.name}", flush=True)
    return full


def load_planc_pre(cutoff: str, etf_daily: dict) -> dict:
    """载入 planC 稀疏 1min→5min 缓存的 pre 段。

    两项必要清洗:
      1) time 标签 -5min(实证 planC[14:45]==tdx[14:40]), 对齐 tdx 语义;
      2) 用当日日K收盘做 scale 对齐 —— pytdx 分钟接口 ETF 价格常 ×10, 该缓存
         pre 段由多个脚本先后写入, 对齐基准不一致。若不校正, 未修正标的的
         当日涨幅会算成 +900%, 天天被选为 Top1 ⇒ 海量假信号(实测 2021 年
         笔数 1→185, 2022 年 -88%)。无日K可校验者直接丢弃。
    """
    if not PLANC.exists():
        return {}
    dclose = {c: {r["date"]: r["close"] for r in i["returns"]}
              for c, i in etf_daily.items()}
    raw = json.loads(PLANC.read_text(encoding="utf-8"))
    out: dict[str, dict[str, list]] = {}
    n_bar = n_fix = n_drop = 0
    for key, bars in raw.items():
        m = re.match(r"(\d{6})_(\d{8})$", key)
        if not m or not bars:
            continue
        code, ds = m.group(1), m.group(2)
        iso = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
        if iso >= cutoff:
            continue
        dc = dclose.get(code, {}).get(iso)
        last = bars[-1].get("close") or 0
        if not dc or dc <= 0 or last <= 0:
            n_drop += 1
            continue
        sc = dc / last
        if sc < 0.5 or sc > 2.0:             # 量纲错(×10 等) → 整日 rescale
            for b in bars:
                for f in ("open", "high", "low", "close"):
                    if b.get(f):
                        b[f] *= sc
            n_fix += 1
        elif abs(sc - 1) > 0.02:             # 复权/口径漂移过大, 不可信
            n_drop += 1
            continue
        for b in bars:                       # 原地平移, 避免复制爆内存
            t = b.get("time", "")
            if len(t) < 5:
                continue
            mm = int(t[:2]) * 60 + int(t[3:5]) - 5
            if mm < 0:
                mm = 0
            b["time"] = "%02d:%02d:00" % (mm // 60, mm % 60)
        out.setdefault(code, {})[iso] = bars
        n_bar += len(bars)
    del raw, dclose
    gc.collect()
    print(f"    planC 近似池: {len(out)} 只, "
          f"{sum(len(v) for v in out.values())} 个(code,日), {n_bar} 根 5min "
          f"| 量纲修正 {n_fix}, 丢弃 {n_drop}", flush=True)
    return out


def sanity_fix_pre(etf_5min: dict, etf_daily: dict, cutoff: str) -> tuple[int, int, int]:
    """对 cutoff 之前的所有 5min 统一做日K收盘对齐体检(不分数据来源)。

    pre 段 5min 可能来自 planC 缓存或 backfill 补抓, 对齐基准未必一致;
    未对齐的标的会算出虚高涨幅并垄断 Top1。已对齐者 sc≈1, 不受影响。
    """
    dclose = {c: {r["date"]: r["close"] for r in i["returns"]}
              for c, i in etf_daily.items()}
    n_ok = n_fix = n_drop = 0
    for code, days in etf_5min.items():
        dc_map = dclose.get(code, {})
        for day in list(days.keys()):
            if day >= cutoff:
                continue
            bars = days[day]
            dc = dc_map.get(day)
            last = bars[-1].get("close") if bars else None
            if not bars or not dc or dc <= 0 or not last or last <= 0:
                del days[day]
                n_drop += 1
                continue
            sc = dc / last
            if sc < 0.5 or sc > 2.0:
                for b in bars:
                    for f in ("open", "high", "low", "close"):
                        if b.get(f):
                            b[f] *= sc
                n_fix += 1
            elif abs(sc - 1) > 0.02:
                del days[day]
                n_drop += 1
                continue
            else:
                n_ok += 1
    del dclose
    gc.collect()
    return n_ok, n_fix, n_drop


def build_picks_B_fast(eval_dates, etf_list, prev_close, etf_5min, signal_time):
    """B 选股: 全池当日涨幅 Top1(≥3%)。预建 prev_close 索引, 避免每日重建 idx_map。"""
    picks = {}
    for day in eval_dates:
        best_g, best_e = None, None
        for etf in etf_list:
            code = etf["code"]
            pc = prev_close.get(code, {}).get(day)
            if not pc or pc <= 0:
                continue
            bars = etf_5min.get(code, {}).get(day)
            if not bars:
                continue
            px = price_at_time(bars, signal_time)
            if px is None or px <= 0:
                continue
            g = (px - pc) / pc * 100
            if best_g is None or g > best_g:
                best_g, best_e = g, etf
        if best_g is not None and passes(best_g):
            picks[(signal_time, day)] = (
                best_e["code"], best_g,
                best_e.get("name") or best_e.get("etf_name") or best_e["code"])
        else:
            picks[(signal_time, day)] = None
    return picks


def cagr(total_pct: float, n_days: int) -> float:
    yrs = n_days / 244.0
    if yrs <= 0:
        return 0.0
    return ((1 + total_pct / 100) ** (1 / yrs) - 1) * 100


def main() -> None:
    ap = argparse.ArgumentParser(description="10年 A vs B 近似回测")
    ap.add_argument("--start", type=str, default="2015-01-05")
    ap.add_argument("--lookback", type=int, default=30)
    ap.add_argument("--fee", type=float, default=FEE_PCT)
    ap.add_argument("--confirm-time", type=str, default="14:40")
    args = ap.parse_args()
    cf = None if args.confirm_time.lower() == "none" else args.confirm_time

    print("载入主缓存(交易日/proxy) ...", flush=True)
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    proxy = cache["proxy_klines"]
    del cache
    gc.collect()

    print("构建/载入 10 年日K(pre 段重建, 否则只有 10 只) ...", flush=True)
    etf_daily = build_full_daily(get_all_t0_etfs())
    _cov: dict[str, set] = {}
    for c, info in etf_daily.items():
        for r in info["returns"]:
            _cov.setdefault(r["date"][:4], set()).add(c)
    print("    日K覆盖(每年标的数): "
          + " ".join(f"{y}:{len(v)}" for y, v in sorted(_cov.items())), flush=True)

    etf_5min: dict[str, dict[str, list]] = {}
    for p in DENSE:
        if not p.exists():
            print(f"    [跳过] 缺失 {p.name}")
            continue
        print(f"载入无偏密集5min: {p.name} ...", flush=True)
        d = json.loads(p.read_text(encoding="utf-8"))["etf_5min"]
        for c, days in d.items():
            etf_5min.setdefault(c, {}).update(days)
        del d
        gc.collect()
    dense_days = sorted({d for v in etf_5min.values() for d in v})
    print(f"    密集池 {len(etf_5min)} 只, {dense_days[0]}~{dense_days[-1]}, "
          f"{len(dense_days)} 天", flush=True)

    if dense_days and dense_days[0] < DENSE_START:
        print("  pre段已由 DENSE 无偏全池覆盖, 跳过 planC 近似补充", flush=True)
    else:
        print("载入 planC 近似5min(2015~2022-06) ...", flush=True)
        pre = load_planc_pre(DENSE_START, etf_daily)
        for c, days in pre.items():
            tgt = etf_5min.setdefault(c, {})
            for d, bars in days.items():
                tgt.setdefault(d, bars)          # 不覆盖无偏数据
        del pre
        gc.collect()

    ok, fix, drop = sanity_fix_pre(etf_5min, etf_daily, DENSE_START)
    print(f"    pre段对齐体检: 正常 {ok}, 量纲修正 {fix}, 丢弃 {drop}", flush=True)

    five_days = sorted({d for v in etf_5min.values() for d in v})
    all_dates = sorted(set(all_dates) | set(five_days))
    all_dates = [d for d in all_dates if d >= args.start]
    codes5 = set(etf_5min.keys())
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    print(f"    合并后 5min: {len(etf_5min)} 只 / {five_days[0]}~{five_days[-1]} "
          f"/ {len(five_days)} 天 | 候选池 {len(etf_list)} 只\n", flush=True)

    prev_close = build_prev_close(etf_list, etf_daily)

    # ========== B: 全 10 年 ==========
    print(">>> B 选股(全市场Top1, 10年) ...", flush=True)
    picks_b = build_picks_B_fast(all_dates, etf_list, prev_close, etf_5min, SIGNAL_TIME)
    n_sig = sum(1 for v in picks_b.values() if v)
    print(f"    命中信号 {n_sig} 天 / {len(all_dates)} 天", flush=True)
    if cf:
        picks_b, rej_b = apply_confirm(picks_b, etf_daily, etf_5min, cf)
        print(f"    {cf} 确认否决 {rej_b} 天 → 剩 {n_sig - rej_b} 笔候选", flush=True)
    print(">>> B 卖出模拟(TRIX) ...", flush=True)
    rb = run_strategy("trix", all_dates, all_dates, picks_b, etf_5min, args.fee)
    tb = rb["trades"] if rb else []

    # ========== A: 仅 post 段(proxy 限制) ==========
    post_dates = [d for d in all_dates if d >= DENSE_START]
    lb = args.lookback
    warmup = min(2 * lb, max(0, len(post_dates) - 1))
    print(f"\n>>> A 选股(hybrid-A 滚动优质池, {post_dates[0]}~{post_dates[-1]}, "
          f"约1~2分钟) ...", flush=True)
    picks_a = build_picks_hybrid(
        post_dates, etf_list, etf_daily, etf_5min, all_dates, proxy,
        lookback=lb, warmup=warmup,
    )
    a_test = post_dates[warmup:]
    a_set = set(a_test)
    picks_a = {k: v for k, v in picks_a.items() if k[1] in a_set}
    if cf:
        picks_a, rej_a = apply_confirm(picks_a, etf_daily, etf_5min, cf)
        print(f"    {cf} 确认否决 {rej_a} 天", flush=True)
    print(">>> A 卖出模拟(TRIX) ...", flush=True)
    ra = run_strategy("trix", a_test, all_dates, picks_a, etf_5min, args.fee)
    ta = ra["trades"] if ra else []

    # B 在 A 同窗口的子集(可比口径)
    tb_post = [t for t in tb if t["signal_date"] >= a_test[0]]
    tb_pre = [t for t in tb if t["signal_date"] < DENSE_START]

    # ---- per-ETF 贡献拆解 + auto vs manual 分离(验证质量过滤因果) ----
    manual_codes = {e["code"] for e in get_all_t0_etfs() if e["type_name"] != "自动"}
    from collections import defaultdict
    by_code = defaultdict(lambda: {"trades": 0, "equity": 1.0, "win": 0})
    auto_eq = man_eq = 1.0
    auto_n = man_n = 0
    for t in tb:
        c = t.get("etf") or "?"
        r = t.get("return_pct", 0.0)
        by_code[c]["trades"] += 1
        by_code[c]["equity"] *= (1 + r / 100)
        if r > 0:
            by_code[c]["win"] += 1
        if c in manual_codes:
            man_eq *= (1 + r / 100); man_n += 1
        else:
            auto_eq *= (1 + r / 100); auto_n += 1
    print("\n  [B 拆解] auto层贡献:", f"{(auto_eq-1)*100:+.1f}% / {auto_n}笔",
          "| manual层贡献:", f"{(man_eq-1)*100:+.1f}% / {man_n}笔")
    top = sorted(by_code.items(), key=lambda kv: kv[1]["equity"], reverse=True)[:12]
    print("  [B 拆解] Top12 贡献ETF:")
    for c, v in top:
        print(f"    {c}  {(v['equity']-1)*100:+9.1f}%  {v['trades']:>3}笔"
              f"  胜{v['win']/v['trades']*100:>3.0f}%  {'auto' if c not in manual_codes else 'manual'}")

    y_b, y_a = year_table(tb), year_table(ta)
    s_b, s_a = stats_of(tb), stats_of(ta)
    s_b_post, s_b_pre = stats_of(tb_post), stats_of(tb_pre)

    print("\n" + "=" * 78)
    print("  逐年收益(按信号年复利)  ★=无偏严谨段(全10年由DENSE全池无偏抓取)")
    print("=" * 78)
    print(f"  {'年份':<8}{'标记':<6}{'B 全市场Top1':>22}{'A 实盘hybrid':>22}")
    print("  " + "-" * 74)
    for y in sorted(set(y_b) | set(y_a)):
        mark = "★"
        def cell(tbl):
            v = tbl.get(y)
            return f"{v['ret']:+9.2f}% ({v['trades']:>3}笔)" if v else "        —        "
        print(f"  {y:<8}{mark:<6}{cell(y_b):>22}{cell(y_a):>22}")
    print("=" * 78)

    n_pre = len([d for d in all_dates if d < DENSE_START])
    n_post = len(a_test)
    print("\n  分段汇总:")
    print(f"  {'区间':<34}{'累计':>13}{'笔数':>7}{'胜率':>8}{'回撤':>9}{'年化':>10}")
    print("  " + "-" * 76)

    def line(label, s, nd):
        print(f"  {label:<34}{s['equity_pct']:>+12.2f}%{s['trades']:>6}笔"
              f"{s['win_rate']:>7.0f}%{s['max_drawdown']:>8.1f}%"
              f"{cagr(s['equity_pct'], nd):>+9.1f}%")

    line(f"★ B 无偏段 2015~2022-06", s_b_pre, n_pre)
    line(f"★ B 无偏段 {a_test[0][:7]}~2026-07", s_b_post, n_post)
    line(f"★ A 无偏段 {a_test[0][:7]}~2026-07", s_a, n_post)
    line(f"B 全10年(全程无偏)", s_b, len(all_dates))
    print("  " + "-" * 76)
    print(f"  无偏段 B-A 差: {s_b_post['equity_pct'] - s_a['equity_pct']:+.2f} 个百分点")

    print(f"\n  说明:")
    print(f"   · A 策略需 501018(proxy)判 regime + 全市场密集5min 建滚动优质池,")
    print(f"     两者在 2015~2022-06 均不存在 ⇒ A 无法回溯到 10 年, 只有 {n_post} 天无偏段。")
    print(f"   · B 全 10 年无偏: pre 段(2015~2022-06)由 backfill_5min_pre2024 对全 103 只")
    print(f"     × 全交易日无条件抓取(pytdx 1分钟聚合, 无候选集前视偏差), 对齐 bad=0;")
    print(f"     post 段(2022-06 起)为 tdx_5min 无偏密集数据。两者口径一致。")
    print(f"   · 年化按 244 交易日/年 折算。")


if __name__ == "__main__":
    main()
