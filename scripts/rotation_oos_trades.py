"""样本外(OOS)逐笔交易明细 — 最终优化策略。

参数(与 --final 一致): ma20 / mom20 / ma_dev / hold_rank=3 / buffer=1% / rebal=5
OOS 区间: 2023-01-01 起 (走查训练段 2017-2022 之外的部分)
输出: 最近 N 笔交易的 入场/出场/动量/净收益/出场原因。

用法:
  python3 scripts/rotation_oos_trades.py [--oos-start 2023-01-01] [--last 10]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from backtest_8wide_ma20_rotation import fetch_universe  # noqa: E402
from optimize_8wide_ma20_rotation import build_series     # noqa: E402


def oos_trades(series: dict, dates: list, code_of: dict, *, ma_win: int, mom_win: int,
               mom_def: str, hold_rank: int, buffer: float, rebal: int,
               fee_pct: float = 0.05) -> list:
    for s in series.values():
        s.build_ma(ma_win)
        s.build_mom(mom_win, mom_def)

    fee = fee_pct / 100.0
    mkey = (mom_win, mom_def)
    names = list(series.keys())

    held = None
    seg = None  # 当前持仓段: dict(name, e_i, e_d, e_p, e_mom)
    trades: list[dict] = []

    def close_of(nm, d):
        s = series[nm]
        i = s.pos.get(d)
        return s.close[i] if i is not None else None

    for di, d in enumerate(dates):
        # 1) 盯市(仅记账, 不改变决策)
        if held is not None:
            s = series[held]
            i = s.pos.get(d)
            if i is not None:
                pass  # 收益已在 equity 中体现, 此处只为交易记录

        # 2) 仅决策日处理
        if di % rebal != 0:
            continue

        # 3) 候选: 站上MA + 有动量
        cand = []
        held_above = False
        held_mom = None
        for nm in names:
            s = series[nm]
            i = s.pos.get(d)
            if i is None:
                continue
            m = s.ma[ma_win][i]
            mo = s.mom[mkey][i]
            if m is None or mo is None or m <= 0:
                continue
            if s.close[i] > m:
                cand.append((mo, nm))
                if nm == held:
                    held_above = True
                    held_mom = mo

        if cand:
            cand.sort(reverse=True)
            best_mom, best = cand[0]
        else:
            best_mom, best = None, None

        # 4) 决策
        if held is None:
            if best is not None:
                held = best
                seg = {"name": best, "e_d": d, "e_p": close_of(best, d),
                       "e_mom": best_mom, "e_idx": series[best].pos[d]}
        else:
            keep = False
            reason_parts: list[str] = []
            rank = None
            if held_above and held_mom is not None:
                rank = 1 + sum(1 for mo, _ in cand if mo > held_mom)
                if rank <= hold_rank:
                    keep = True
                elif best is not None and best_mom is not None \
                        and (best_mom - held_mom) <= buffer:
                    keep = True
                else:
                    if rank > hold_rank:
                        reason_parts.append(f"排名#{rank}(>容忍{hold_rank})")
                    if best is not None and best_mom is not None \
                            and (best_mom - held_mom) > buffer:
                        reason_parts.append(
                            f"最强超出{(best_mom - held_mom) * 100:.2f}pp"
                            f"(>缓冲{buffer * 100:.0f}%)")
            else:
                # 持仓已跌破 MA20 → 强制出场
                reason_parts.append("持仓跌破MA20")

            if not keep:
                # 出场当前段
                x_p = close_of(held, d)
                x_idx = series[held].pos[d]
                gross = (x_p / seg["e_p"] - 1) if seg["e_p"] else 0.0
                if best is None:
                    reason = "无达标候选(全部跌破MA20) → 清仓观望"
                    net = gross - fee
                else:
                    reason = (" ".join(reason_parts) + " → 换仓") \
                        if reason_parts else "→ 换仓"
                    net = gross - 2 * fee
                trades.append({
                    "name": seg["name"], "code": code_of.get(seg["name"], ""),
                    "e_d": seg["e_d"], "e_p": round(seg["e_p"], 4),
                    "e_mom": round(seg["e_mom"] * 100, 2),
                    "x_d": d, "x_p": round(x_p, 4),
                    "x_mom": round(held_mom * 100, 2) if held_mom is not None else None,
                    "gross_pct": round(gross * 100, 2),
                    "net_pct": round(net * 100, 2),
                    "hold_days": x_idx - seg["e_idx"],
                    "reason": reason,
                })
                if best is None:
                    held = None
                    seg = None
                else:
                    # 换仓到 best
                    held = best
                    seg = {"name": best, "e_d": d, "e_p": close_of(best, d),
                           "e_mom": best_mom, "e_idx": series[best].pos[d]}

    # 收尾: 若仍持仓, 记为未平仓(最新价)
    if held is not None and seg is not None:
        last_d = dates[-1]
        x_p = close_of(held, last_d)
        gross = (x_p / seg["e_p"] - 1) if seg["e_p"] else 0.0
        trades.append({
            "name": seg["name"], "code": code_of.get(seg["name"], ""),
            "e_d": seg["e_d"], "e_p": round(seg["e_p"], 4),
            "e_mom": round(seg["e_mom"] * 100, 2),
            "x_d": last_d, "x_p": round(x_p, 4),
            "x_mom": None,
            "gross_pct": round(gross * 100, 2),
            "net_pct": round((gross - fee) * 100, 2),
            "hold_days": series[held].pos[last_d] - seg["e_idx"],
            "reason": "● 当前仍持仓(未平仓, 按最新价估算)",
        })
    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oos-start", default="2023-01-01")
    ap.add_argument("--last", type=int, default=10)
    ap.add_argument("--fee", type=float, default=0.05)
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    data = fetch_universe(fresh=args.fresh)
    series = build_series(data)
    # 代码映射(name -> code)供显示
    code_of = {nm: info["code"] for nm, info in data.items() if nm != "__benchmark"}

    # 全周期 dates (用于 di%rebal 对齐, 必须从早起点开始累计, 但只记录 >= oos-start)
    all_dates = sorted({d for s in series.values() for d in s.dates if d >= "2017-01-01"})
    oos_dates = [d for d in all_dates if d >= args.oos_start]
    # 关键: rebal 决策日的对齐必须基于全周期 di%rebal, 而非仅 OOS 段
    full_map = {d: i for i, d in enumerate(all_dates)}

    P = dict(ma_win=20, mom_win=20, mom_def="ma_dev",
             hold_rank=3, buffer=0.01, rebal=5)
    tr = oos_trades(series, all_dates, code_of, fee_pct=args.fee, **P)

    # 仅保留 OOS 段内的交易(入场日在 OOS 之后)
    tr_oos = [t for t in tr if t["e_d"] >= args.oos_start]
    last = tr_oos[-args.last:]

    print("=" * 110)
    print(f"[样本外逐笔交易] 起点 {args.oos_start}  终点 {all_dates[-1]}  "
          f"参数 ma20/mom20/ma_dev/rank3/buf1%/reb5  费率 {args.fee}%")
    print(f"OOS 总交易段: {len(tr_oos)} 笔  展示最近 {len(last)} 笔")
    print("=" * 110)
    hdr = (f"{'#':>2}  {'入场日':<11}{'标的':<9}{'代码':<8}{'入场价':>9}"
           f"{'入场动量%':>9}  {'出场日':<11}{'出场价':>9}{'出场动量%':>9}"
           f"{'持有日':>6}{'毛收益%':>9}{'净收益%':>9}  出场原因")
    print(hdr)
    print("-" * 110)
    for k, t in enumerate(last, 1):
        xmom = f"{t['x_mom']:.2f}" if t["x_mom"] is not None else "  -  "
        print(f"{k:>2}  {t['e_d']:<11}{t['name']:<9}{t['code']:<8}{t['e_p']:>9.4f}"
              f"{t['e_mom']:>8.2f}%  {t['x_d']:<11}{t['x_p']:>9.4f}{xmom:>8}%"
              f"{t['hold_days']:>6}{t['gross_pct']:>8.2f}%{t['net_pct']:>8.2f}%  {t['reason']}")

    print("-" * 110)
    wins = [t for t in last if t["net_pct"] > 0]
    print(f"最近 {len(last)} 笔: 盈利 {len(wins)} / 亏损 {len(last)-len(wins)}  "
          f"净收益合计 {sum(t['net_pct'] for t in last):+.2f}%  "
          f"平均 {sum(t['net_pct'] for t in last)/len(last):+.2f}%")


if __name__ == "__main__":
    main()
