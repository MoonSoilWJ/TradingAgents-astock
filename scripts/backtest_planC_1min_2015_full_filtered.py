#!/usr/bin/env python3
"""Plan C 从 2015 起全历史 + ETF 级质量过滤（事前、无未来函数）正式回测。

动机（来自归因/敏感性分析）：
  - 回放显示剔除黄金类累计仅 -1.1%（可安全缩池）；
  - 剔除所有负收益 ETF 的事后上界累计 +7848%（翻倍），说明亏损 ETF 是真实复利拖累。
  本脚本用「事前可复制」的指标把上述事后结论转成正式策略：
    1) 类型过滤：剔除黄金类（回放已证影响仅 1.1%）；
    2) 质量门限：要求候选 ETF 截至信号日「过去 120 交易日累计涨幅 >= 0」
       （中期动量质量确认，纯用历史数据，无未来函数），过滤掉长期颓势标的。
  其余执行口径（日K Top-K → 14:51 真实涨幅重排 → 双时点确认 → TRIX 死叉出场）
  与原 Plan C / 方案B 完全一致，确保可比。复用已落盘的 5min 缓存（planC_1min_4y.json）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(k, None)

import akshare as ak  # noqa: E402
from pytdx.hq import TdxHq_API  # noqa: E402
from backtest_planC_1min_4y import (  # noqa: E402
    K_CAND, SIGNAL_TIME, BUY_TIME, CONFIRM_TIME, MIN_SELL, SERVERS,
    market_of, date_int, load_cache, save_cache, report, recon_5min,
    daily_close_of, daily_gain, prev_close_of, get_5min,
)
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT, MIN_GAIN, MAX_GAIN, TRIX_PERIOD,
    apply_net_return, bars_for_trix, price_at_time, select_etf,
    simulate_trix_cross_after,
)
from run_2015_full import fresh_connect, safe_fetch  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"
DAILY1000 = CACHE / "backfill_daily_1000.json"
DAILY2015 = CACHE / "backfill_daily_2015.json"
OUT = CACHE / "planC_1min_4y.json"            # 共用 5min 缓存
RESULT = CACHE / "planC_result_2015_filtered.json"

CUT = "2022-06-15"
OLD10 = {'159920', '510900', '513100', '513500', '513030',
         '159901', '518880', '159934', '518800', '162411'}

# ---- 过滤超参（通用风控，非针对本次数据过拟合）----
WIN_MOM = 120      # 中期动量窗口（交易日）
MIN_MOM = 0.0      # 过去 WIN_MOM 日累计涨幅门限（>=0 才入选）
MIN_DATA = 40      # 日K 不足此天数则不阻挡（避免新上市标的被误杀）
DROP_GOLD = True   # 剔除黄金类


def sina_daily(code: str) -> dict:
    try:
        h = ak.fund_etf_hist_sina(symbol=("sh" if code[0] in "56" else "sz") + code)
    except Exception as e:
        print(f"    [warn] sina {code} 拉取失败: {e}", flush=True)
        return {}
    out: dict = {}
    for _, row in h.iterrows():
        d = str(row["date"])[:10]
        if d >= CUT:
            continue
        try:
            out[d] = {
                "date": d, "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row.get("volume") or 0),
            }
        except Exception:
            continue
    return out


def build_long_mom(etf_daily: dict, win: int = WIN_MOM, min_data: int = MIN_DATA) -> dict:
    """为每只 ETF 预计算「截至每个交易日、过去 win 日的累计收盘价涨幅」。
    返回 {code: {date: cum_or_None}}；cum=None 表示数据不足，不阻挡。"""
    out: dict = {}
    for code, info in etf_daily.items():
        rs = info["returns"]
        m: dict = {}
        n = len(rs)
        for i in range(n):
            if i + 1 < min_data:
                m[rs[i]["date"]] = None
                continue
            lo = max(0, i - win + 1)
            c0 = rs[lo].get("close")
            c1 = rs[i].get("close")
            if not c0 or not c1 or c0 <= 0:
                m[rs[i]["date"]] = None
            else:
                m[rs[i]["date"]] = (c1 / c0 - 1) * 100
        out[code] = m
    return out


def filtered_run(api, cache, etf_list, etf_daily, all_dates, eval_dates, lmm) -> dict:
    trades: list[dict] = []
    skipped: list[dict] = []
    missing: list = []
    code_scale: dict = {}
    for di, day in enumerate(eval_dates, 1):
        # 1) 日K收盘涨幅 TOP-K 候选（叠加两层事前过滤）
        cands = []
        for etf in etf_list:
            if DROP_GOLD and etf.get("type_name") == "黄金":
                continue
            code = etf["code"]
            lm = lmm.get(code, {}).get(day)
            if lm is not None and lm < MIN_MOM:   # None=数据不足，不阻挡
                continue
            g = daily_gain(etf_daily, code, day)
            if g is not None:
                cands.append((g, etf))
        cands.sort(key=lambda x: x[0], reverse=True)
        if len(cands) < 2:
            continue
        topk = cands[:K_CAND]

        # 2) 拉候选当日5分钟 → 真实 14:51 涨幅重排
        ranked = []
        for g, etf in topk:
            bars = get_5min(api, cache, etf["code"], day, etf_daily, missing, code_scale)
            if not bars:
                continue
            p = price_at_time(bars, SIGNAL_TIME)
            prev = prev_close_of(etf_daily, etf["code"], day)
            if p is None or p <= 0 or not prev or prev <= 0:
                continue
            ranked.append(((p - prev) / prev * 100, etf))
        if len(ranked) < 2:
            continue
        ranked.sort(key=lambda x: x[0], reverse=True)

        picked = select_etf(ranked, True, anti_pulse=False)
        if picked is None:
            topg = ranked[0][0]
            reason = ("无满足条件ETF" if not (MIN_GAIN <= ranked[0][0] <= MAX_GAIN)
                      else f"防脉冲({ranked[0][0]:.1f}%)")
            skipped.append({"date": day, "reason": reason, "top_gain": topg})
            continue

        gain, top1 = picked
        code = top1["code"]

        # 3) 双时点确认：14:41 涨幅也须≥MIN_GAIN
        day_bars = get_5min(api, cache, code, day, etf_daily, missing, code_scale)
        if not day_bars:
            skipped.append({"date": day, "reason": "无信号日分钟", "top_gain": gain, "etf": code})
            continue
        g_confirm = None
        pc = price_at_time(day_bars, CONFIRM_TIME)
        prev = prev_close_of(etf_daily, code, day)
        if pc and prev:
            g_confirm = (pc - prev) / prev * 100
        if g_confirm is not None and g_confirm < MIN_GAIN:
            skipped.append({"date": day, "reason": f"双时点确认失败({CONFIRM_TIME} {g_confirm:.2f}%<{MIN_GAIN:.0f}%)",
                            "top_gain": gain, "etf": code})
            continue

        sell_day = all_dates[all_dates.index(day) + 1] if all_dates.index(day) + 1 < len(all_dates) else None
        if not sell_day:
            continue
        sell_bars = get_5min(api, cache, code, sell_day, etf_daily, missing, code_scale)
        if not sell_bars:
            skipped.append({"date": day, "reason": "无次日分钟", "top_gain": gain, "etf": code})
            continue

        buy_price = price_at_time(day_bars, BUY_TIME)
        if buy_price is None or buy_price <= 0:
            buy_price = price_at_time(day_bars, SIGNAL_TIME)
        if buy_price is None or buy_price <= 0:
            continue

        _, sell_reason, detail = simulate_trix_cross_after(
            buy_price, bars_for_trix(day_bars), bars_for_trix(sell_bars),
            trix_period=TRIX_PERIOD, min_sell_time=MIN_SELL,
        )
        sell_price = detail.get("sell_price")
        if sell_price is None:
            sell_price = float(sell_bars[-1]["close"])
        ret = apply_net_return(buy_price, sell_price, FEE_PCT)

        # 安全网：单笔 |收益|>40% 几乎必为量纲异常，跳过
        if abs(ret) > 40:
            skipped.append({"date": day, "reason": f"收益异常({ret:.1f}%)疑似量纲",
                            "top_gain": gain, "etf": code})
            continue

        rank = next((i + 1 for i, (_, e) in enumerate(ranked) if e["code"] == code), 1)
        trades.append({
            "signal_date": day, "sell_date": sell_day, "etf": code,
            "type": top1.get("type_name", ""), "rank": rank,
            "today_gain": round(gain, 2), "buy_price": round(buy_price, 4),
            "buy_time": BUY_TIME, "sell_price": round(sell_price, 4),
            "sell_time": detail.get("bar", sell_day), "sell_reason": sell_reason,
            "return_pct": ret,
        })

        if di % 50 == 0:
            save_cache(cache)
            print(f"    [{di}/{len(eval_dates)}] {day} 累计 {len(trades)} 笔", flush=True)

    save_cache(cache)
    rets = [t["return_pct"] for t in trades]
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return {"trades": trades, "trade_count": len(trades),
            "skipped_count": len(skipped), "final_equity_pct": (eq - 1) * 100}


def main() -> None:
    api = fresh_connect()
    etfs = get_all_t0_etfs()
    bf = json.loads(DAILY1000.read_text(encoding="utf-8"))
    bd = bf["etf_daily"]
    d2015 = json.loads(DAILY2015.read_text(encoding="utf-8")) if DAILY2015.exists() else {}

    # ---- 1) 构建 106 只全历史 etf_daily ----
    full: dict = {}
    for e in etfs:
        code = e["code"]
        post = {r["date"]: r for r in bd.get(code, {}).get("returns", [])}
        if code in OLD10 and code in d2015:
            pre = {r["date"]: r for r in d2015[code]["returns"]}
        else:
            pre = sina_daily(code)
        merged = {**pre, **post}
        if merged:
            full[code] = {"returns": sorted(merged.values(), key=lambda x: x["date"])}
    print(f">>> full 池 {len(full)} 只", flush=True)

    # ---- 2) eval_dates（覆盖 >= 2）----
    codes = list(full.keys())
    all_dates = sorted({r["date"] for info in full.values() for r in info["returns"]})
    cover = defaultdict(int)
    for c in codes:
        for r in full[c]["returns"]:
            cover[r["date"]] += 1
    eval_dates = [d for d in all_dates if cover[d] >= 2]
    print(f">>> eval {eval_dates[0]}~{eval_dates[-1]} ({len(eval_dates)} 交易日)", flush=True)

    # ---- 3) 预填 5min（缓存命中则秒过）----
    cache = load_cache()
    etf_list = [e for e in etfs if e["code"] in full]
    need = [e["code"] for e in etfs if e["code"] in full and e["code"] not in OLD10]
    print(f">>> 需预填 pre 段 5min 的 ETF: {len(need)} 只 (缓存命中则秒过)", flush=True)
    for code in need:
        pre_dates = [r["date"] for r in full[code]["returns"] if r["date"] < CUT]
        if not pre_dates:
            continue
        miss = [d for d in pre_dates if f"{code}_{date_int(d)}" not in cache]
        if not miss:
            continue
        t0 = time.time()
        print(f"    预填 {code}: {len(miss)} 天 pre 段 5min", flush=True)
        done = 0
        for i, d in enumerate(miss, 1):
            raw = safe_fetch(api, code, d)
            if not raw:
                continue
            dc = daily_close_of(full, code, d)
            raw_last = float(raw[-1]["price"])
            scale = dc / raw_last if (dc and raw_last > 0) else 1.0
            five = recon_5min(raw, d, scale=scale)
            if five:
                cache[f"{code}_{date_int(d)}"] = five
                done += 1
            if i % 200 == 0:
                save_cache(cache)
        save_cache(cache)
        print(f"    {code} 完成预填 {done} 天", flush=True)
    print(">>> pre 段 5min 预填完成", flush=True)

    # ---- 4) 构建中期动量质量表 + 跑 filtered_run ----
    print(">>> 构建中期动量质量表 (WIN=%d, MIN_MOM=%.1f, DROP_GOLD=%s) ..."
          % (WIN_MOM, MIN_MOM, DROP_GOLD), flush=True)
    lmm = build_long_mom(full)
    print(">>> 跑 filtered_run ...", flush=True)
    res = filtered_run(api, cache, etf_list, full, all_dates, eval_dates, lmm)
    report("PlanC 2015起全历史 + ETF质量过滤", res)
    RESULT.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    print(f">>> 结果已落盘 {RESULT}  (对照: 全量 +3449.55%)", flush=True)
    try:
        api.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    main()
