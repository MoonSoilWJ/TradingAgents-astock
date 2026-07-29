#!/usr/bin/env python3
"""全场 ETF「20日涨幅最高」买入持有型动量轮动回测（最近4年）。

策略（用户原话）：
  从全场 ETF 中选择 20 日涨幅最高的买入；
  一旦 top1 换标的了就卖出旧的、买入新的；否则就一直持有。

实现口径（贴近实盘、可复现）：
  - 信号判定日 D：用截至 D 收盘的 20 日涨幅（= close_D / close_{D-20} - 1）对全池排名。
  - 若 D 的 top1 与当前持仓不同 → 在 D 收盘卖出旧持仓、买入新 top1（换仓）。
    若相同 → 持有不动（不产生交易）。
  - 收益按持仓区间复利累乘（每日用持仓 ETF 的日收益率推进净值）。
  - 初始有 20 日预热期（前 20 根不排名）。
  - 手续费按 ETF 免五 ~0.05%（双边各一次）计入；可 --fee 调整。

数据源：复用本地缓存 backfill_daily_1000.json（106 只 T+0 ETF 真实日K，
2019-01-21~2026-07-28，约 4 年），无需联网，秒级完成。
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"
DAILY = CACHE / "backfill_daily_1000.json"

PERIOD = 20  # 20 日涨幅
WARMUP = 20  # 预热


def load_pool() -> tuple[dict, list[str]]:
    bf = json.loads(DAILY.read_text(encoding="utf-8"))
    etf_daily = bf["etf_daily"]
    # 仅保留有 >= PERIOD+1 根日K的标的
    clean = {}
    for code, info in etf_daily.items():
        rets = info.get("returns") or []
        if len(rets) >= PERIOD + 1:
            clean[code] = rets
    # 统一交易日历（取所有标的交集，避免某标的缺 day 导致 None）
    from collections import Counter
    cnt = Counter()
    for rets in clean.values():
        for r in rets:
            cnt[r["date"]] += 1
    all_dates = sorted(d for d, c in cnt.items() if c >= max(2, len(clean) // 2))
    return clean, all_dates


def momentum_series(returns: list[dict], period: int) -> dict[str, float | None]:
    """返回 {date: 20日涨幅% 或 None}。"""
    out: dict[str, float | None] = {}
    for i, r in enumerate(returns):
        if i < period:
            out[r["date"]] = None
            continue
        prev = returns[i - period]["close"]
        cur = r["close"]
        if prev and prev > 0 and cur and cur > 0:
            out[r["date"]] = (cur - prev) / prev * 100
        else:
            out[r["date"]] = None
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fee", type=float, default=0.05, help="单边手续费% (默认0.05)")
    ap.add_argument("--period", type=int, default=PERIOD, help="动量窗口(日), 默认20")
    ap.add_argument("--start", type=str, default="", help="起始日 YYYY-MM-DD（默认用全样本起点）")
    ap.add_argument("--end", type=str, default="", help="结束日 YYYY-MM-DD")
    ap.add_argument("--pool", type=str, default="t0",
                    choices=["t0", "all"], help="标的范围: t0(默认106只T0) / all(全市场)")
    args = ap.parse_args()

    period = args.period
    clean, all_dates = load_pool()

    if args.pool == "all":
        # 全市场：用 get_all_market_etf_lof + 各自日K（此处仅演示 t0 池已足够；
        # all 市场需额外拉全市场日K，暂退回 t0 池并提示）
        print("⚠️ --pool all 需要全市场日K，当前缓存仅 106 只 T+0 ETF；"
              "自动回退到 t0 池。")
    # 各标的动量序列
    mom = {code: momentum_series(rets, period) for code, rets in clean.items()}

    # 过滤日期区间
    dates = all_dates
    if args.start:
        dates = [d for d in dates if d >= args.start]
    if args.end:
        dates = [d for d in dates if d <= args.end]
    # 预热：前 period 天无法排名
    dates = dates[period:]

    fee = args.fee
    equity = 1.0
    held = None          # 当前持仓 code
    held_buy_equity = equity  # 持仓起始净值（用于算该段收益）
    trades: list[dict] = []
    switches = 0
    # 逐年净值（用于逐年收益）
    year_eq: dict[str, float] = defaultdict(lambda: 1.0)
    cur_year = None

    for d in dates:
        # 当日可排名的标的
        cands = []
        for code, ms in mom.items():
            m = ms.get(d)
            if m is not None:
                cands.append((m, code))
        if not cands:
            # 无法排名：若仍持仓，按旧仓当日收益推进净值
            if held:
                ret = _daily_ret(clean[held], d)
                if ret is not None:
                    equity *= (1 + ret / 100)
            continue
        cands.sort(key=lambda x: x[0], reverse=True)
        top_m, top_code = cands[0]

        if held is None:
            # 首仓：收盘买入，仅扣买入费，当日不计入收益（收盘后才持有）
            equity *= (1 - fee / 100)
            held = top_code
            trades.append({"date": d, "action": "BUY", "code": top_code,
                           "mom": round(top_m, 2), "equity": round(equity, 6)})
            switches += 1
        elif top_code != held:
            # 换仓：旧仓赚到当日收益后于收盘卖出，再于收盘买入新仓（新仓当日不计入）
            sell_ret = _daily_ret(clean[held], d)
            if sell_ret is not None:
                equity *= (1 + sell_ret / 100)
            equity *= (1 - fee / 100)  # 卖出费
            trades.append({"date": d, "action": "SELL", "code": held,
                           "mom": None, "equity": round(equity, 6)})
            equity *= (1 - fee / 100)  # 买入费
            held = top_code
            trades.append({"date": d, "action": "BUY", "code": top_code,
                           "mom": round(top_m, 2), "equity": round(equity, 6)})
            switches += 1
        else:
            # 持有：旧仓赚当日收益
            ret = _daily_ret(clean[held], d)
            if ret is not None:
                equity *= (1 + ret / 100)

        y = d[:4]
        if y != cur_year:
            if cur_year is not None:
                pass
            cur_year = y
        year_eq[y] = equity  # 记录每年末净值（近似）

    final_pct = (equity - 1) * 100
    # 逐年收益
    years = sorted(year_eq.keys())
    print("=" * 64)
    print("  全场 ETF 20日涨幅动量轮动（买入持有·top1换仓才换）")
    print("=" * 64)
    print(f"  标的池: {len(clean)} 只 T+0 ETF | 窗口: {period}日 | 单边费: {fee}%")
    print(f"  区间: {dates[0]} ~ {dates[-1]} ({len(dates)} 交易日)")
    print(f"  换仓次数: {switches} | 末持仓: {held}")
    print(f"  累计收益: {final_pct:+.2f}%  (净值 {equity:.4f}x)")
    # 逐年（用年末净值相对上年末）
    prev_eq = 1.0
    print("  --- 逐年 ---")
    for y in years:
        yret = (year_eq[y] / prev_eq - 1) * 100
        print(f"    {y}: {yret:+.2f}%  (净值 {year_eq[y]:.4f}x)")
        prev_eq = year_eq[y]
    # 与买入持有沪深300风格对照略
    print("=" * 64)
    # 保存
    out = Path.home() / ".tradingagents" / "rotation" / "rotation_20d_result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "config": {"period": period, "fee": fee, "pool": "t0_106",
                   "start": dates[0], "end": dates[-1]},
        "final_pct": round(final_pct, 2), "final_equity": round(equity, 6),
        "switches": switches, "last_held": held,
        "yearly": {y: round((year_eq[y] / (year_eq.get(str(int(y)-1), 1.0) if str(int(y)-1) in year_eq else 1.0) - 1) * 100, 2) for y in years},
        "trades": trades[-30:],  # 仅存末30笔
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"  结果已存: {out}")


def _daily_ret(returns: list[dict], date: str) -> float | None:
    idx = {r["date"]: i for i, r in enumerate(returns)}
    if date not in idx or idx[date] == 0:
        return None
    prev = returns[idx[date] - 1]["close"]
    cur = returns[idx[date]]["close"]
    if not prev or prev <= 0 or not cur or cur <= 0:
        return None
    return (cur - prev) / prev * 100


if __name__ == "__main__":
    main()
