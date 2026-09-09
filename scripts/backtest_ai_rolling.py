#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 层滚动回测 — 验证 AI 能否超越规则层基线。

【为什么需要它】规则层基线(8%未封板无差别买入, 次日开盘卖)笔均仅 +0.28%,
AI 层是唯一的价值来源; 本脚本检验它到底有没有选股能力。

【设计】
  · 数据: min30.pkl(30分钟K) 重建历史候选 — 8% 未封板首次触发, 沪深主板
  · 证据: 全部来自历史数据(首触/盘口/均线/位置), 不调实时接口(否则前视)
  · 滚动: 每日注入"截至前一日"的成绩单 — 与实盘同源, 非前视
  · 卖出: 次日开盘(A 方案结论: 开盘卖 +0.28% > 收盘卖 +0.04%)
  · 对照: AI 的 BUY vs 当日全部候选(无差别)的次日开盘收益

【已知局限 — 会低估 AI】
  · 盘口委量比/资金流/财务无历史数据 → 三维度缺失(实盘有, 回测没有)
  · 每日只送 8 只抽样候选(实盘最多 40 只)

用法: python3 scripts/backtest_ai_rolling.py [days=60] [sample=8]
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:
    pass

MIN30 = Path.home() / ".tradingagents" / "youzi" / "min30.pkl"
OUT = Path.home() / ".tradingagents" / "youzi" / "ai_backtest.jsonl"
COST = 0.002
MINP = 65.0
LABEL = {1: "10:00", 2: "10:30", 3: "11:00", 4: "11:30",
         5: "13:30", 6: "14:00", 7: "14:30", 8: "15:00"}


def build_candidates() -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_pickle(MIN30)
    df = df[df["code"].astype(str).str[0].isin(["6", "0"])].copy()
    df = df[~df["code"].astype(str).str.startswith("688")]
    df["date"] = df["datetime"].dt.date
    df = df.sort_values(["code", "datetime"])
    df["idx"] = df.groupby(["code", "date"]).cumcount() + 1
    df["cum_amt"] = df.groupby(["code", "date"])["amount"].cumsum()

    daily = df.groupby(["code", "date"]).agg(
        d_open=("open", "first"), d_close=("close", "last"),
        d_high=("high", "max"), d_amt=("amount", "sum")).reset_index()
    daily = daily.sort_values(["code", "date"])
    daily["prev"] = daily.groupby("code")["d_close"].shift(1)
    daily["pct_day"] = daily["d_close"] / daily["prev"] - 1
    daily["is_limit"] = daily["pct_day"] >= 0.098
    daily["nxt_open"] = daily.groupby("code")["d_open"].shift(-1)
    daily["nxt_close"] = daily.groupby("code")["d_close"].shift(-1)
    daily["hi60"] = daily.groupby("code")["d_high"].transform(
        lambda s: s.shift(1).rolling(60, min_periods=20).max())
    daily["n5"] = daily.groupby("code")["is_limit"].transform(
        lambda s: s.shift(1).rolling(5, min_periods=1).sum())
    daily["ma5"] = daily.groupby("code")["d_close"].transform(
        lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    daily["ma20"] = daily.groupby("code")["d_close"].transform(
        lambda s: s.shift(1).rolling(20, min_periods=20).mean())

    df = df.merge(daily[["code", "date", "prev", "d_amt", "is_limit",
                         "nxt_open", "nxt_close", "hi60", "n5", "ma5",
                         "ma20"]], on=["code", "date"], how="left")
    df["pct"] = df["close"] / df["prev"] - 1
    df["limit_px"] = (df["prev"] * 1.10).round(2)
    df["sealed_now"] = df["close"] >= df["limit_px"] - 0.001

    sig = df[(df["pct"] >= 0.08) & (~df["sealed_now"])].copy()
    sig = sig.sort_values(["code", "date", "idx"]).groupby(
        ["code", "date"]).head(1)                    # 每日每票首次触发
    return sig, df


def evidence_of(r, all_k: pd.DataFrame) -> dict:
    """用历史 30 分钟 K 构建证据(不调任何实时接口)。"""
    code, d = r["code"], r["date"]
    lpx = r["limit_px"]
    k = all_k[(all_k["code"] == code) & (all_k["date"] == d)
              & (all_k["idx"] <= r["idx"])]
    gap = (lpx / r["close"] - 1) * 100
    parts = [f"距涨停 {gap:.1f}%"]
    touch = int((k["high"] >= lpx - 0.001).sum())
    if touch:
        parts.append(f"今日触板被砸 {touch} 次"
                     + ("(反复炸板,抛压重)" if touch >= 3
                        else "(首次冲板)" if touch == 1 else ""))
    if len(k) >= 2:
        hi, lo = float(k["high"].max()), float(k["low"].min())
        pos = (r["close"] - lo) / (hi - lo) if hi > lo else 0.5
        parts.append(f"收盘位置 {pos*100:.0f}%")
    # 首触: 10:00 那根是否已到 9%
    k10 = all_k[(all_k["code"] == code) & (all_k["date"] == d)
                & (all_k["idx"] == 1)]
    stance = ""
    if len(k10):
        p10 = float(k10["pct"].iloc[0])
        stance = ("开盘半小时内已冲至9% (历史表现差组)" if p10 >= 0.09
                  else "10:00后走强至9% (历史表现优组)")
    ma = ""
    m5, m20 = r.get("ma5"), r.get("ma20")
    if m5 == m5 and m20 == m20 and m5 > 0:
        ma = ("均线多头(5>20) — 承接强" if m5 > m20
              else "均线空头(5<20) — 反弹抛压重")
    return {"obook": " | ".join(parts), "stance": stance, "ma": ma,
            "fin": "缺失", "fund": "缺失"}


def report_txt(hist: list[dict]) -> str:
    """滚动成绩单 — 只含"今天之前"的判定结果(与实盘同源)。"""
    if len(hist) < 8:
        return ""
    rows = [f"AI 判断校准报告 (已回填 {len(hist)} 条判定)", "",
            f"{'prob档':<8}{'笔数':>5}{'封板率':>8}{'次日开盘':>9}"]
    for name, lo, hi in (("70+", 70, 101), ("65-70", 65, 70),
                         ("60-65", 60, 65), ("<60", 0, 60)):
        sub = [h for h in hist if lo <= h["prob"] < hi]
        if not sub:
            rows.append(f"{name:<8}{0:>5}{'—':>8}{'—':>9}")
            continue
        seal = sum(1 for h in sub if h["seal"]) / len(sub) * 100
        o = sum(h["ret"] for h in sub) / len(sub)
        rows.append(f"{name:<8}{len(sub):>5}{seal:>7.1f}%{o:>+8.2f}%")
    buys = [h for h in hist if h["buy"]]
    if buys:
        seal = sum(1 for h in buys if h["seal"]) / len(buys) * 100
        o = sum(h["ret"] for h in buys) / len(buys)
        rows += ["", f"BUY 判定 {len(buys)} 笔: 封板率 {seal:.1f}% | "
                     f"次日开盘 {o:+.2f}%"]
    return "\n".join(rows)


def main() -> int:
    days_n = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    sample_n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    print("构建历史候选 ...", flush=True)
    sig, all_k = build_candidates()
    days = sorted(sig["date"].unique())[-days_n:]
    print(f"候选 {len(sig):,} 笔 | 回测 {len(days)} 个交易日 "
          f"({days[0]} ~ {days[-1]}) | 每日送 AI {sample_n} 只", flush=True)

    from youzi_ai import decide
    hist: list[dict] = []
    rows: list[dict] = []
    for i, d in enumerate(days, 1):
        day = sig[sig["date"] == d]
        if len(day) == 0:
            continue
        pick = day.sample(min(sample_n, len(day)), random_state=i)
        sigs = []
        for _, r in pick.iterrows():
            prog = {1: 30, 2: 60, 3: 90, 4: 120, 5: 150, 6: 180,
                    7: 210, 8: 240}.get(int(r["idx"]), 120) / 240
            vr = (float(r["cum_amt"]) / max(prog, 0.04)) / max(
                float(r["d_amt"]), 1) if r["d_amt"] == r["d_amt"] else 0
            sigs.append({"code": r["code"], "name": "", "pct": float(r["pct"]),
                         "thr": 10.0, "mv": None, "turn": None,
                         "dd": (float(r["hi60"] - r["close"]) / r["hi60"] * 100
                                if r["hi60"] == r["hi60"] and r["hi60"] > 0
                                else None),
                         "vr": float(vr),
                         "amt_yi": float(r["cum_amt"]) / 1e8,
                         "streak": None, "price": float(r["close"])})
        ev = {r["code"]: evidence_of(r, all_k) for _, r in pick.iterrows()}
        n_limit = int(day["is_limit"].sum()) if "is_limit" in day else 0
        market = {"n_limit": n_limit + 20, "pool_n": 3000, "max_st": 3}
        rpt = report_txt(hist)
        try:
            decs, stt = decide(sigs, market,
                               now=datetime.combine(d, datetime.min.time()),
                               thr=8.0, min_prob=MINP, report=rpt, evidence=ev,
                               verbose=False)
        except Exception as exc:
            print(f"  {d} 调用失败: {type(exc).__name__} {str(exc)[:60]}")
            continue
        by = {x["code"]: x for x in decs}
        for s in sigs:
            a = by.get(s["code"]) or {}
            src = pick[pick["code"] == s["code"]].iloc[0]
            ret = (float(src["nxt_open"]) / float(src["close"]) - 1 - COST
                   if src["nxt_open"] == src["nxt_open"] else np.nan)
            seal = bool(src["is_limit"])
            rec = {"date": str(d), "code": s["code"],
                   "act": a.get("action", "?"), "prob": a.get("prob", 0),
                   "ret": None if ret != ret else round(ret * 100, 2),
                   "seal": seal, "slot": LABEL.get(int(src["idx"]), "?")}
            rows.append(rec)
            if ret == ret:
                hist.append({"prob": float(a.get("prob", 0)),
                             "seal": seal, "ret": rec["ret"],
                             "buy": a.get("action") == "BUY"})
        buys = [r for r in rows if r["date"] == str(d) and r["act"] == "BUY"]
        base = [r["ret"] for r in rows
                if r["date"] == str(d) and r["ret"] is not None]
        print(f"  [{i}/{len(days)}] {d} 候选{len(sigs)} → BUY {len(buys)} "
              f"| 当日基线 {np.mean(base):+.2f}%" if base else
              f"  [{i}/{len(days)}] {d} 候选{len(sigs)} → BUY {len(buys)}",
              flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w") as f:
        for r in rows:
            f.write(__import__("json").dumps(r, ensure_ascii=False) + "\n")

    buys = [r for r in rows if r["act"] == "BUY" and r["ret"] is not None]
    allr = [r for r in rows if r["ret"] is not None]
    print("\n" + "=" * 58)
    print(f"回测 {len(days)} 天 | 判定 {len(allr)} 笔 | BUY {len(buys)} 笔")
    if allr:
        print(f"无差别基线(全部候选): 笔均 {np.mean([r['ret'] for r in allr]):+.2f}% "
              f"| 封板率 {np.mean([r['seal'] for r in allr])*100:.1f}%")
    if buys:
        print(f"AI 选出的 BUY:       笔均 {np.mean([r['ret'] for r in buys]):+.2f}% "
              f"| 封板率 {np.mean([r['seal'] for r in buys])*100:.1f}%")
        eq = np.prod([1 + r["ret"] / 100 for r in buys])
        print(f"BUY 复利净值: {eq:.3f}x")
    else:
        print("AI 一笔未买(全程 SKIP)")
    print("=" * 58)
    return 0


if __name__ == "__main__":
    sys.exit(main())
