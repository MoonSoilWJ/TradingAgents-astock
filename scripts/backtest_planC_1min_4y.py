#!/usr/bin/env python3
"""Plan C：用 pytdx 按日期分钟接口回填 2022-2023 精确 5min，做全 4 年精确回测。

背景（已探明的数据边界）：
  - pytdx get_security_bars 连续 5min 有 ~23850 根上限 → 只能到 2024-07（2 年墙）。
  - 但 get_history_minute_time_data(mkt, code, date) 按「指定交易日」取当天 1 分钟
    数据（240 根/日），实测可回溯到 2015 年，不受连续根数限制。
  - 本脚本只为「信号日 + 次日」的少量标的拉分钟数据，而非全池全历史，故可行。

做法：
  1. 用已有的 4 年日K(backfill_daily_1000.json) 构建交易日历与选股池。
  2. 每日先用日K收盘涨幅取 TOP-K(K=20) 候选（日K收盘≈14:51，proxy 极稳）；
     再为这些候选拉当日 1 分钟→聚合成5分钟→算真实 14:51 涨幅重排，取 TOP1。
  3. 为选中标的拉 信号日(全段) + 次日(全段) 1 分钟→5分钟，
     用 backtest_t0_today1 的 simulate_trix_cross_after 算精确出场
     （实盘对齐口径：signal 14:51 / buy 14:56 / confirm 14:41 / 次日09:40后TRIX死叉）。
  4. 所有 1 分钟按 (code,date) 落盘缓存，可断点续跑；重跑免费。

这样得到的是「真实分钟执行、无日K隔夜近似、无 ×2.97 外推」的 4 年曲线，
直接可比现有 2 年 5min 精确回测。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from pytdx.hq import TdxHq_API  # noqa: E402
from backtest_t0_idle_dual import compound  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    MIN_GAIN,
    MAX_GAIN,
    TRIX_PERIOD,
    apply_net_return,
    bars_for_trix,
    price_at_time,
    select_etf,
    simulate_trix_cross_after,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"
BACKFILL = CACHE / "backfill_daily_1000.json"
OUT = CACHE / "planC_1min_4y.json"          # 1分钟原始缓存
RESULT = CACHE / "planC_result.json"        # 回测结果

SERVERS = [
    ("115.238.56.198", 7709), ("115.238.90.165", 7709),
    ("180.153.18.170", 7709), ("218.108.98.244", 7709),
    ("123.125.108.14", 7709), ("60.28.23.80", 7709),
]
K_CAND = 20          # 日K收盘 TOP-K 候选数（重排用真实14:51涨幅）
SIGNAL_TIME = "14:51"
BUY_TIME = "14:56"
CONFIRM_TIME = "14:41"
MIN_SELL = "09:40"
COVER_THR = 0.5      # 交易日历覆盖阈值（与 backtest_daily_4y 一致）


def market_of(code: str) -> int:
    return 1 if code[0] in ("5", "6") else 0


def date_int(s: str) -> int:
    return int(s.replace("-", ""))


def iso_week(d: str) -> str:
    y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
    return f"{y}/W{w:02d}"


def mdd(trades: list[dict]) -> float:
    eq, peak, m = 1.0, 1.0, 0.0
    for t in sorted(trades, key=lambda x: x["signal_date"]):
        eq *= 1 + t["return_pct"] / 100
        peak = max(peak, eq)
        m = min(m, (eq - peak) / peak * 100)
    return m


def worst_week(trades: list[dict]) -> tuple[str, float]:
    wr: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        wr[iso_week(t["signal_date"])].append(t["return_pct"])
    if not wr:
        return "-", 0.0
    wk = min(wr, key=lambda k: compound(wr[k]))
    return wk, compound(wr[wk])


# ---------- 1分钟拉取 + 聚合为5分钟 ----------
def connect() -> TdxHq_API:
    api = TdxHq_API()
    for h, p in SERVERS:
        try:
            if api.connect(h, p, time_out=5):
                return api
        except Exception:
            continue
    raise RuntimeError("无可用通达信服务器")


def daily_close_of(etf_daily: dict, code: str, day: str):
    info = etf_daily.get(code)
    if not info:
        return None
    returns = info["returns"]
    idx_map = {r["date"]: i for i, r in enumerate(returns)}
    if day not in idx_map:
        return None
    return returns[idx_map[day]]["close"]


def recon_5min(raw, day_str: str, scale: float = 1.0) -> list[dict] | None:
    """将当日 1 分钟原始数据(raw) → 聚合为 48 根 5 分钟，带 time/day/datetime 标签。

    scale: 量纲修正系数（get_history_minute_time_data 价格缩放因标的而异，
    如 ETF 常 10x、茅台 1x；用 日K收盘/末根1分钟价 对齐整日）。
    """
    if not raw:
        return None
    # 1分钟 → 5分钟（每5根一组，OHLC）
    five: list[dict] = []
    for g in range(0, len(raw), 5):
        chunk = raw[g:g + 5]
        if not chunk:
            continue
        op = float(chunk[0]["price"]) * scale
        cl = float(chunk[-1]["price"]) * scale
        hi = max(float(b["price"]) for b in chunk) * scale
        lo = min(float(b["price"]) for b in chunk) * scale
        vol = sum(float(b.get("vol", 0)) for b in chunk)
        # 5分钟bar收盘时刻：第 g//5 段
        seg = g // 5
        # 09:30起每5分钟一段：0->09:35,...,22->11:30,23->13:05,...,47->15:00
        if seg < 23:
            total = 9 * 60 + 30 + (seg + 1) * 5
        else:
            total = 13 * 60 + (seg - 23 + 1) * 5
        hh, mm = divmod(total, 60)
        t = f"{hh:02d}:{mm:02d}"
        five.append({
            "open": op, "high": hi, "low": lo, "close": cl, "volume": vol,
            "time": t, "day": day_str,
            "datetime": f"{day_str} {t}:00",
        })
    return five


def load_cache() -> dict:
    if OUT.exists():
        return json.loads(OUT.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    OUT.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def reconnect(api: TdxHq_API) -> bool:
    try:
        api.disconnect()
    except Exception:
        pass
    return bool(api.connect(*SERVERS[0], time_out=5))


def reconnect_any() -> TdxHq_API:
    """轮换尝试所有服务器，返回已连接的新 API（无可用服务器则抛错）。"""
    api = TdxHq_API()
    for h, p in SERVERS:
        try:
            if api.connect(h, p, time_out=5):
                return api
        except Exception:
            continue
    raise RuntimeError("无可用通达信服务器")


def safe_get_minute(api: TdxHq_API, code: str, day_str: str,
                    timeout: float = 10.0) -> list | None:
    """线程包裹的 1 分钟拉取：超时即强制断连并轮换服务器重连，绝不阻塞主线程。

    修复：get_history_minute_time_data 在 socket 半开时会无限阻塞（connect 的
    time_out 仅作用于建连），故用线程 + join(timeout) 兜底；超时/异常均轮换重连。
    返回原始 1 分钟列表或 None。
    """
    mkt = market_of(code)
    di = date_int(day_str)
    for _ in range(3):
        box: dict = {}
        def _call() -> None:
            try:
                box["v"] = api.get_history_minute_time_data(mkt, code, di)
            except Exception as e:  # noqa: BLE001
                box["e"] = e
        th = threading.Thread(target=_call, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            try:
                api.disconnect()
            except Exception:
                pass
            try:
                api = reconnect_any()
            except Exception:
                pass
            continue
        if box.get("v"):
            return box["v"]
        if "e" in box:
            try:
                api.disconnect()
            except Exception:
                pass
            try:
                api = reconnect_any()
            except Exception:
                pass
    return None


def get_5min(api: TdxHq_API, cache: dict, code: str, day_str: str,
             etf_daily: dict, missing: list, code_scale: dict):
    key = f"{code}_{date_int(day_str)}"
    if key in cache:
        return cache[key]
    # 量纲修正：get_history_minute_time_data 的价格缩放因标的而异（ETF 常 10x、
    # 茅台 1x），且按标的恒定。优先用当日日K收盘对齐；若当日缺日K，则回退到
    # 该标的已算出的 code 级系数（避免次日/缺数据日卖出价未被修正成 10x）。
    raw_last = None
    r0 = safe_get_minute(api, code, day_str)
    if r0:
        raw_last = float(r0[-1]["price"])
    dclose = daily_close_of(etf_daily, code, day_str)
    if dclose and dclose > 0 and raw_last and raw_last > 0:
        scale = dclose / raw_last
        code_scale[code] = scale          # 当日有日K：用当日对齐（最准），并更新 code 系数
    else:
        scale = code_scale.get(code, 1.0)  # 缺日K：回退到该标的已算系数
    bars = recon_5min(r0, day_str, scale=scale) if r0 else None
    cache[key] = bars
    missing.append(key)
    if len(missing) >= 50:
        save_cache(cache)
        missing.clear()
    return bars


# ---------- 选股/执行 ----------
def daily_gain(etf_daily: dict, code: str, day: str):
    info = etf_daily.get(code)
    if not info:
        return None
    returns = info["returns"]
    idx_map = {r["date"]: i for i, r in enumerate(returns)}
    if day not in idx_map or idx_map[day] == 0:
        return None
    prev = returns[idx_map[day] - 1]["close"]
    close = returns[idx_map[day]]["close"]
    if not prev or prev <= 0 or not close or close <= 0:
        return None
    return (close - prev) / prev * 100


def prev_close_of(etf_daily: dict, code: str, day: str):
    info = etf_daily.get(code)
    if not info:
        return None
    returns = info["returns"]
    idx_map = {r["date"]: i for i, r in enumerate(returns)}
    if day not in idx_map or idx_map[day] == 0:
        return None
    return returns[idx_map[day] - 1]["close"]


def run(api: TdxHq_API, cache: dict, etf_list: list[dict],
        etf_daily: dict, all_dates: list[str], eval_dates: list[str]) -> dict:
    trades: list[dict] = []
    skipped: list[dict] = []
    missing: list = []
    code_scale: dict = {}
    for di, day in enumerate(eval_dates, 1):
        # 1) 日K收盘涨幅 TOP-K 候选
        cands = []
        for etf in etf_list:
            g = daily_gain(etf_daily, etf["code"], day)
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

        # 安全网：单笔 |收益|>40% 几乎必为量纲异常，跳过以免污染复利
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


def report(tag: str, res: dict) -> None:
    trades = res["trades"]
    wk, wv = worst_week(trades)
    print(f"\n=== {tag} ===")
    print(f"总计: {res['trade_count']} 笔, 累计 {res['final_equity_pct']:+.2f}%, "
          f"最大回撤 {mdd(trades):.2f}%, 最差周 {wk} {wv:+.2f}%")
    by_year: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_year[t["signal_date"][:4]].append(t)
    print(f"{'年':<6}{'笔':>4}{'收益':>10}{'回撤':>9}{'最差周':>18}")
    for y in sorted(by_year):
        ts = by_year[y]
        wk2, wv2 = worst_week(ts)
        print(f"{y:<6}{len(ts):>4}{compound([t['return_pct'] for t in ts]):+10.2f}%"
              f"{mdd(ts):>9.2f}%{wk2:>13}{wv2:>+8.2f}%")


def main() -> None:
    bf = json.loads(BACKFILL.read_text(encoding="utf-8"))
    etf_daily = bf["etf_daily"]
    codes = list(etf_daily.keys())
    all_dates = sorted({r["date"] for info in etf_daily.values() for r in info["returns"]})
    cover = {d: sum(1 for c in codes if any(r["date"] == d for r in etf_daily[c]["returns"]))
             for d in all_dates}
    thr = COVER_THR * len(codes)
    eval_dates = [d for d in all_dates if cover[d] >= thr]
    print(f">>> 日K池 {len(codes)} 只, 窗口 {eval_dates[0]} ~ {eval_dates[-1]} "
          f"({len(eval_dates)} 交易日)")

    etf_list = get_all_t0_etfs()
    # 只保留日K池内有的
    etf_list = [e for e in etf_list if e["code"] in etf_daily]
    print(f">>> 参与回测标的 {len(etf_list)} 只")

    cache = load_cache()
    print(f">>> 1分钟缓存已加载 {len(cache)} 条")

    api = connect()
    try:
        res = run(api, cache, etf_list, etf_daily, all_dates, eval_dates)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass

    report("Plan C 精确5min(实盘对齐口径) — 全4年", res)
    RESULT.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    print(f">>> 结果已落盘 {RESULT}")


if __name__ == "__main__":
    main()
