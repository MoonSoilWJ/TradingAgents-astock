#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 层分钟级回测 — 当前最新提示词(自洽+子分), 60 天, 买卖点精确到分钟。

数据链:
  · 阶段1: min30.pkl 快筛候选(30分钟级首次触及 8% 未封板, 沪深主板)
  · 阶段2: 东财历史1分钟K(http直连, 按票拉, 仅候选票+次日) → 定位首触的精确分钟
  · 阶段3: 每日抽样 10 只 → 当前提示词判定(自洽+子分) → prob≥70 放行
  · 阶段4: 次日 09:31 分钟开盘价卖出 → 交易明细(分钟级)

口径:
  · 触发: 分钟收盘涨幅 ≥8% 且该分钟未封板 → 首根为信号分钟(买入价=该分钟收盘)
    (30分钟K触发的候选若分钟级无 close≥8% 的根, 回退用 30 分钟口径)
  · 封板: 当日最后分钟收盘 = 涨停价
  · 成本: 0.2%
输出: ~/.tradingagents/youzi/ai_backtest_min.jsonl + 控制台统计

局限: 内外盘/委托量/资金流/财务无历史数据 → 相关维度缺失(会低估 AI)。
用法: python3 scripts/backtest_ai_min.py [days=60] [sample=10]
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"

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
OUT = Path.home() / ".tradingagents" / "youzi" / "ai_backtest_min.jsonl"
CACHE = Path.home() / ".tradingagents" / "youzi" / "min1_cache.pkl"
COST = 0.002
MINP = 70.0
HEAD = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


HEAD_SINA = {"User-Agent": "Mozilla/5.0",
             "Referer": "https://finance.sina.com.cn"}


def em_min_kline(code: str, beg: str, end: str,
                 timeout: float = 15.0) -> pd.DataFrame | None:
    """新浪 5 分钟K(最近 ~21 个交易日, datalen 上限 1023 根)。

    B 方案数据源: 东财被限流封禁时的新浪替代(接口稳定, 但粒度 5 分钟、
    窗口缩至 ~21 天)。amt 置 0 → 证据中 vwap 分支自动跳过。
    """
    sym = ("sh" if code[0] in "569" else "sz") + code
    for _ in range(3):
        try:
            u = (f"https://quotes.sina.cn/cn/api/json_v2.php/"
                 f"CN_MarketDataService.getKLineData?symbol={sym}"
                 f"&scale=5&ma=no&datalen=1023")
            r = requests.get(u, headers=HEAD_SINA, timeout=timeout,
                             proxies={"http": None, "https": None})
            d = r.json()
            if not isinstance(d, list) or not d:
                return None
            df = pd.DataFrame(d).rename(columns={"day": "datetime",
                                                 "volume": "vol"})
            for c in ("open", "close", "high", "low", "vol"):
                df[c] = df[c].astype(float)
            df["amt"] = 0.0
            df["date"] = df["datetime"].str[:10]
            return df[["datetime", "date", "open", "close", "high",
                       "low", "vol", "amt"]]
        except Exception:
            time.sleep(2)
    return None


def build_candidates(days_n: int):
    df = pd.read_pickle(MIN30)
    df = df[df["code"].astype(str).str[0].isin(["6", "0"])].copy()
    df = df[~df["code"].astype(str).str.startswith("688")]
    df["date"] = df["datetime"].dt.date
    df = df.sort_values(["code", "datetime"])
    df["idx"] = df.groupby(["code", "date"]).cumcount() + 1
    daily = df.groupby(["code", "date"]).agg(
        d_close=("close", "last"), d_high=("high", "max"),
        d_amt=("amount", "sum")).reset_index()
    daily = daily.sort_values(["code", "date"])
    daily["prev"] = daily.groupby("code")["d_close"].shift(1)
    daily["is_limit"] = daily["d_close"] / daily["prev"] - 1 >= 0.098
    daily["nxt_close"] = daily.groupby("code")["d_close"].shift(-1)
    daily["hi60"] = daily.groupby("code")["d_high"].transform(
        lambda s: s.shift(1).rolling(60, min_periods=20).max())
    daily["ma5"] = daily.groupby("code")["d_close"].transform(
        lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    daily["ma20"] = daily.groupby("code")["d_close"].transform(
        lambda s: s.shift(1).rolling(20, min_periods=20).mean())
    df = df.merge(daily[["code", "date", "prev", "is_limit", "nxt_close",
                         "hi60", "ma5", "ma20", "d_amt"]],
                  on=["code", "date"], how="left")
    df["pct"] = df["close"] / df["prev"] - 1
    df["limit_px"] = (df["prev"] * 1.10).round(2)
    df["sealed_now"] = df["close"] >= df["limit_px"] - 0.001
    sig = df[(df["pct"] >= 0.08) & (~df["sealed_now"])].copy()
    sig = sig.sort_values(["code", "date", "idx"]).groupby(
        ["code", "date"]).head(1)
    return sig, daily, df


def evidence_min(k: pd.DataFrame, prev: float, limit_px: float,
                 sig_dt: str, ma5: float, ma20: float) -> tuple[str, str]:
    """分钟级证据 → (obook文本, stance文本)。k=当日分钟(截至信号时刻)。"""
    price = float(k["close"].iloc[-1])
    parts = [f"距涨停 {(limit_px/price-1)*100:.1f}%"]
    hits = (k["high"] >= limit_px - 0.001).tolist()
    touches, in_t = 0, False
    for h in hits:
        if h and not in_t:
            touches += 1
        in_t = h
    if touches:
        parts.append(f"今日触板被砸 {touches} 次"
                     + ("(反复炸板,抛压重)" if touches >= 3
                        else "(首次冲板)" if touches == 1 else ""))
    hi, lo = float(k["high"].max()), float(k["low"].min())
    pos = (price - lo) / (hi - lo) if hi > lo else 0.5
    vol = k["vol"].astype(float)
    v30 = float(vol.tail(30).sum()) / max(float(vol.tail(60).head(30).sum()), 1e-9)
    parts.append(f"收盘位置 {pos*100:.0f}% | 近30分钟量能 {v30:.1f}x")
    if "amt" in k and k["amt"].sum() > 0 and vol.sum() > 0:
        vwap = float(k["amt"].sum()) / (float(vol.sum()) * 100)
        if vwap > 0:
            parts.append(f"相对均价 {(price/vwap-1)*100:+.1f}%")
    p10 = k[k["datetime"].str[11:16] <= "10:00"]
    p10r = (float(p10["close"].iloc[-1]) / prev - 1
            if len(p10) else float(k["close"].iloc[0]) / prev - 1)
    stance = ("开盘半小时内已冲至9% (历史表现差组)" if p10r >= 0.09
              else "10:00后走强至9% (历史表现优组)")
    ma = ""
    if ma5 == ma5 and ma20 == ma20 and ma5 > 0:
        ma = ("均线多头(5>20) — 承接强" if ma5 > ma20
              else "均线空头(5<20) — 反弹抛压重")
    return " | ".join(parts), stance, ma


def report_txt(hist: list[dict]) -> str:
    if len(hist) < 8:
        return ""
    rows = [f"AI 判断校准报告 (已回填 {len(hist)} 条判定)", ""]
    rows.append("【近期 BUY 案例回顾 · 你的判断理由与实际结果】")
    for r in [h for h in hist if h["buy"]][-5:]:
        rows.append(f"  {r['date']} prob={r['prob']} — {r.get('reason','')}"
                    f" → {'封板' if r['seal'] else '未封板'}, 次日开盘 {r['ret']:+.2f}%")
    rows += ["", f"{'prob档':<8}{'笔数':>5}{'封板率':>9}{'次日开盘':>9}"]
    for name, lo, hi in (("70+", 70, 101), ("65-70", 65, 70), ("<65", 0, 65)):
        sub = [h for h in hist if lo <= h["prob"] < hi]
        if not sub:
            continue
        sr = sum(1 for h in sub if h["seal"]) / len(sub) * 100
        o = sum(h["ret"] for h in sub) / len(sub)
        rows.append(f"{name:<8}{len(sub):>5}{sr:>8.1f}%{o:>+8.2f}%")
    return "\n".join(rows)


def main() -> int:
    days_n = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    sample_n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    print("阶段1: 构建 30 分钟级候选 ...", flush=True)
    sig, daily, df = build_candidates(days_n)
    days = sorted(sig["date"].unique())[-days_n:]
    sig = sig[sig["date"].isin(days)]
    codes_all = sorted(sig["code"].unique())
    print(f"候选 {len(sig):,} 笔次 / {len(codes_all)} 只票 | "
          f"{days[0]} ~ {days[-1]}", flush=True)

    # ── 阶段2: 东财分钟K(按票, 带磁盘缓存) ──
    cache = {}
    if CACHE.exists():
        try:
            cache = pd.read_pickle(CACHE)
            for c in list(cache):       # 旧缓存缺 date 列 → 从 datetime 重建
                if "date" not in cache[c].columns:
                    cache[c]["date"] = cache[c]["datetime"].str[:10]
        except Exception:
            cache = {}
    beg = (datetime.strptime(str(days[0]), "%Y-%m-%d")
           - timedelta(days=3)).strftime("%Y%m%d")
    end = (datetime.strptime(str(days[-1]), "%Y-%m-%d")
           + timedelta(days=3)).strftime("%Y%m%d")
    minute: dict[str, pd.DataFrame] = {}
    t0 = time.time()
    fail_streak = 0
    for i, c in enumerate(codes_all, 1):
        if c in cache:
            continue
        if time.time() - t0 > 7200:    # 总超时保护 2 小时
            print("  ! 拉取总超时 2h, 用已缓存部分继续", flush=True)
            break
        k = em_min_kline(c, beg, end)
        if k is not None and len(k):
            cache[c] = k[["datetime", "date", "open", "close", "high",
                          "low", "vol", "amt"]]
            fail_streak = 0
            time.sleep(1.2)            # 正常限速
        else:
            fail_streak += 1
            print(f"  ! {c} 拉取失败 (连续{fail_streak})", flush=True)
            if fail_streak >= 3:       # 疑似被封 → 退避 5 分钟等解封
                print("  ! 疑似限流封禁, 退避 300s ...", flush=True)
                time.sleep(300)
        if i % 50 == 0:
            pd.to_pickle(cache, CACHE)
            print(f"  进度 {i}/{len(codes_all)} | 已缓存 {len(cache)} 票 "
                  f"| {time.time()-t0:.0f}s", flush=True)
    pd.to_pickle(cache, CACHE)
    print(f"分钟K缓存 {len(cache)}/{len(codes_all)} 票 "
          f"({time.time()-t0:.0f}s)", flush=True)

    # ── 阶段2.5: 分钟级精确定位信号 ──
    min_rows = []
    for _, r in sig.iterrows():
        k = cache.get(r["code"])
        if k is None:
            continue
        kd = k[k["date"] == str(r["date"])]
        if len(kd) < 5:
            continue
        kd = kd.reset_index(drop=True)
        ok = kd[kd["close"] / r["prev"] - 1 >= 0.08]
        ok = ok[ok["close"] < r["limit_px"] - 0.001]
        if len(ok):
            row = kd.loc[ok.index[0]]
        else:                      # 回退: 用 30 分钟K的结束时刻定位
            end_hm = pd.Timestamp(r["datetime"]).strftime("%H:%M")
            row = kd[kd["datetime"].str[11:16] >= end_hm]
            if not len(row):
                continue
            row = row.iloc[0]
        # 当日截至信号分钟的分钟 + 次日首分钟(卖出)
        upto = kd[kd["datetime"] <= row["datetime"]]
        nxt = k[(k["date"] > str(r["date"]))].head(1)
        sell_px = float(nxt["open"].iloc[0]) if len(nxt) else np.nan
        seal = bool(kd["close"].iloc[-1] >= r["limit_px"] - 0.001)
        min_rows.append({**r.to_dict(), "sig_dt": row["datetime"],
                         "sig_hm": row["datetime"][11:16],
                         "buy_px": float(row["close"]),
                         "sell_px": sell_px, "seal": seal,
                         "upto": upto})
    print(f"分钟级定位成功 {len(min_rows):,} 笔", flush=True)

    # ── 阶段3: 每日抽样 → LLM 判定 ──
    from youzi_ai import decide
    random.seed(42)
    by_day: dict = {}
    for m in min_rows:
        by_day.setdefault(m["date"], []).append(m)
    hist: list[dict] = []
    out_rows: list[dict] = []
    for i, d in enumerate(days, 1):
        pool_m = by_day.get(d) or []
        if not pool_m:
            continue
        pick = random.sample(pool_m, min(sample_n, len(pool_m)))
        sigs, ev = [], {}
        for m in pick:
            dd = float(m["hi60"]) if m["hi60"] == m["hi60"] else None
            ddv = ((dd - m["buy_px"]) / dd * 100
                   if dd and dd > 0 else None)
            sigs.append({"code": m["code"], "name": m.get("name", ""),
                         "pct": float(m["pct"]) * 100, "thr": 9.8,
                         "mv": None, "turn": None, "dd": ddv, "vr": None,
                         "amt_yi": float(m["d_amt"]) / 1e8, "streak": None,
                         "price": m["buy_px"]})
            ob, st, ma = evidence_min(m["upto"], float(m["prev"]),
                                      float(m["limit_px"]), m["sig_dt"],
                                      m.get("ma5"), m.get("ma20"))
            ev[m["code"]] = {"obook": ob, "stance": st, "ma": ma,
                             "fin": "缺失", "fund": "缺失"}
        n_limit = sum(1 for x in min_rows if x["date"] == d) * 4 + 20
        rpt = report_txt(hist)
        try:
            decs, stt = decide(
                sigs, {"n_limit": n_limit, "pool_n": 3000, "max_st": 3},
                datetime.combine(d, datetime.min.time()), thr=8.0,
                min_prob=MINP, report=rpt, evidence=ev, verbose=False)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"  [{i}] {d} 调用失败: {type(exc).__name__}", flush=True)
            continue
        by = {x["code"]: x for x in decs}
        for m in pick:
            a = by.get(m["code"]) or {}
            ret = (float(m["sell_px"]) / m["buy_px"] - 1 - COST
                   if m["sell_px"] == m["sell_px"] else None)
            row = {"date": str(d), "code": m["code"],
                   "name": m.get("name", ""), "act": a.get("action", "?"),
                   "prob": a.get("prob", 0),
                   "scores": a.get("scores", {}),
                   "sig_hm": m["sig_hm"], "buy_px": m["buy_px"],
                   "sell_px": m["sell_px"] if m["sell_px"] == m["sell_px"] else None,
                   "ret": None if ret is None else round(ret * 100, 2),
                   "seal": m["seal"]}
            out_rows.append(row)
            if ret is not None:
                hist.append({"prob": float(a.get("prob", 0)), "seal": m["seal"],
                             "ret": row["ret"],
                             "buy": a.get("action") == "BUY",
                             "reason": a.get("reason", ""), "date": str(d)})
        bs = [r for r in out_rows
              if r["date"] == str(d) and r["act"] == "BUY"]
        print(f"  [{i}/{len(days)}] {d} 判{len(pick)} → BUY {len(bs)}",
              flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w") as f:
        for r in out_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    buys = [r for r in out_rows if r["act"] == "BUY" and r["ret"] is not None]
    allr = [r for r in out_rows if r["ret"] is not None]
    print("\n" + "=" * 62)
    print(f"分钟级回测 {len(days)} 天 | 判定 {len(allr)} | BUY {len(buys)}")
    if allr:
        print(f"无差别基线: 笔均 {np.mean([r['ret'] for r in allr]):+.2f}% "
              f"| 封板率 {np.mean([r['seal'] for r in allr])*100:.1f}%")
    if buys:
        print(f"AI BUY(prob≥70): 笔均 {np.mean([r['ret'] for r in buys]):+.2f}% "
              f"| 封板率 {np.mean([r['seal'] for r in buys])*100:.1f}%")
        dr = pd.DataFrame(buys).groupby("date")["ret"].mean()
        print(f"按日复利净值: {(1 + dr / 100).prod():.3f}x ({len(dr)} 天)")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
