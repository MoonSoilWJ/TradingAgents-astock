#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""30 分钟级情绪闸门回测 —— 验证日内闸门对最近真实交易的影响。

方法:
  1. 交易 = ai_pushed.jsonl 的 BUY 推送(有精确推送时刻 ts), 收益来自 youzi_sold.jsonl
  2. 每个交易日的 cohort = 昨日涨停股(limits_{D-1}.json)
  3. 对 cohort 拉 1 分 K(akshare, 按 code 缓存一次), 在 30 分钟检查点
     (09:35/10:05/.../14:35) 用截至 t 的价格重算 实时晋级率/均涨
  4. 序列 → 30 分钟斜率 → 与实盘同款闸门规则(_gate_blocked) → 轨迹
  5. 每笔交易按其推送时刻的闸门状态分成 放行/被拦, 对比笔均收益

用法: python3 scripts/backtest_regime_gate.py [--days 12]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"
os.environ.setdefault("TQDM_DISABLE", "1")

import pandas as pd                                       # noqa: E402
STATE_DIR = Path.home() / ".tradingagents" / "youzi"


def gate_state(series, t_ts, prev_close_map, cohort, df_map, limit_pct):
    """在时刻 t 用 cohort 的 1 分 K(≤t) 算 晋级率/均涨 + 斜率 → 闸门。"""
    sealed = 0
    pcts = []
    for c in cohort:
        df = df_map.get(c)
        if df is None:
            continue
        sub = df[df["datetime"] <= t_ts]
        if len(sub) == 0:
            continue
        price = float(sub["close"].iloc[-1])
        pc = prev_close_map.get(c)
        if not pc or price <= 0:
            continue
        pcts.append(price / pc - 1)
        if price >= round(pc * (1 + limit_pct(c)), 2) - 0.01:
            sealed += 1
    n = len(pcts)
    if n < 5:
        return None
    rate = sealed / n * 100
    avg = sum(pcts) / n * 100
    # 斜率基准: ≥25 分钟前最近检查点
    ref = None
    for pt, pr, pa in reversed(series):
        if (t_ts - pt).total_seconds() >= 25 * 60:
            ref = (pr, pa)
            break
    if ref is None and series:
        ref = (series[0][1], series[0][2])
    rs = rate - ref[0] if ref else 0.0
    as_ = avg - ref[1] if ref else 0.0
    series.append((t_ts, rate, avg))
    # ── 与实盘 youzi_live._gate_blocked 同款规则 ──
    if avg < -1.5:
        return dict(rate=rate, avg=avg, rs=rs, as_=as_, blocked=True, why="大面日")
    base = (rate < 30 and avg < 1.0) or (rate < 40 and avg < 0)
    if not base:
        return dict(rate=rate, avg=avg, rs=rs, as_=as_, blocked=False, why="放行")
    if rs >= 5 and as_ >= 0.3:
        return dict(rate=rate, avg=avg, rs=rs, as_=as_, blocked=False, why="修复中")
    return dict(rate=rate, avg=avg, rs=rs, as_=as_, blocked=True, why="退潮")


def limit_pct(code):
    return 0.20 if code[:3] in ("300", "688", "689") else 0.10


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=12, help="回测最近N天")
    args = ap.parse_args()

    # 1) 交易与推送时刻
    pushed = {}     # (date, code) → push ts
    for l in open(STATE_DIR / "ai_pushed.jsonl"):
        try:
            o = json.loads(l)
        except Exception:
            continue
        if o.get("action") == "BUY" and o.get("ts"):
            key = (o["ts"][:10], str(o["code"]))
            pushed.setdefault(key, o["ts"])
    trades = []
    for l in open(STATE_DIR / "youzi_sold.jsonl"):
        try:
            o = json.loads(l)
        except Exception:
            continue
        bp, sp = float(o.get("buy_price") or 0), float(o.get("sell_price") or 0)
        bd = str(o.get("buy_date"))
        if bp <= 0 or sp <= 0 or bd not in [k[0] for k in pushed]:
            continue
        ts = pushed.get((bd, str(o["code"])))
        if not ts:
            continue
        trades.append(dict(bd=bd, code=str(o["code"]), name=o.get("name", ""),
                           push=pd.Timestamp(ts), ret=(sp / bp - 1) * 100))
    if not trades:
        print("无交易样本"); return 0
    d0 = min(t["push"] for t in trades) - timedelta(days=1)
    d1 = max(t["push"] for t in trades) + timedelta(days=1)

    # 2) 每日 cohort(昨日涨停列表) + 1 分 K 缓存
    day_cohort = {}
    need_codes = set()
    for t in trades:
        pday = (pd.Timestamp(t["bd"]) - timedelta(days=1)).strftime("%Y-%m-%d")
        f = STATE_DIR / ("limits_%s.json" % pday)
        if not f.exists():
            f = STATE_DIR / ("limits_%s.json" % (pd.Timestamp(t["bd"]) -
                                                 timedelta(days=3)).strftime("%Y-%m-%d"))
        if f.exists():
            co = [str(c) for c in json.loads(f.read_text(encoding="utf-8"))]
            day_cohort[t["bd"]] = co
            need_codes.update(co)
    print("交易 %d 笔, cohort 总计 %d 只唯一代码, 拉取 1 分 K ..." %
          (len(trades), len(need_codes)))

    df_map = {}
    prev_close_map = {}
    CACHE = STATE_DIR / "gate_min1_cache"
    CACHE.mkdir(exist_ok=True)

    def fetch_full_min(code):
        """整段 1 分历史(不按日切片), 磁盘缓存 → 一次拉取覆盖所有回测日。"""
        cf = CACHE / ("%s.csv" % code)
        if cf.exists():
            try:
                d = pd.read_csv(cf)
                d["datetime"] = pd.to_datetime(d["datetime"])
                return d
            except Exception:
                pass
        import time as _t
        d = None
        try:
            import akshare as ak
            pre = "sh" if code[0] in "569" else "sz"
            for _ in range(3):
                try:
                    r = ak.stock_zh_a_minute(symbol=pre + code, period="1",
                                             adjust="")
                except Exception:
                    r = None
                if r is not None and len(r):
                    d = r
                    break
                _t.sleep(1.5)
        except Exception:
            d = None
        if d is None or len(d) == 0:
            try:
                from youzi_sell_ai import fetch_hist_min_1
                d = fetch_hist_min_1(code, d1.strftime("%Y-%m-%d"))
            except Exception:
                d = None
        if d is None or len(d) == 0:
            return None
        d = d.copy()
        d["datetime"] = pd.to_datetime(d["day"] if "day" in d.columns
                                       else d["datetime"])
        for col in ("close", "open", "high", "low"):
            if col in d.columns:
                d[col] = d[col].astype(float)
        d.to_csv(cf, index=False)
        return d

    for i, c in enumerate(sorted(need_codes)):
        try:
            from youzi_sell_ai import prev_close_of
            df = fetch_full_min(c)
            if df is not None and len(df):
                df_map[c] = df
            for day in sorted({t["bd"] for t in trades}):
                prev_close_map.setdefault((c, day), None)
        except Exception as exc:
            print("  [warn] %s: %s" % (c, str(exc)[:60]))
        if (i + 1) % 20 == 0:
            print("  ... %d/%d" % (i + 1, len(need_codes)), flush=True)

    # 昨收: 用每只票自身 1 分历史的"上一交易日最后收盘"(零网络, 避免 254×10 次 akshare)
    days = sorted({t["bd"] for t in trades})
    for c, df in df_map.items():
        for day in days:
            day_start = pd.Timestamp("%s 09:00" % day)
            prior = df[df["datetime"] < day_start]
            if len(prior) == 0:
                continue
            last_day = prior["datetime"].dt.date.max()
            prev_close_map[(c, day)] = float(
                prior[prior["datetime"].dt.date == last_day]["close"].iloc[-1])

    # 3) 逐日模拟闸门轨迹 + 按推送时刻判定每笔交易
    blocked = taken = 0
    br = tr = 0.0
    print("\n=== 逐日闸门轨迹(30分钟检查点) ===")
    for day in days:
        cohort = day_cohort.get(day) or []
        series = []
        ck = []
        t0 = pd.Timestamp("%s 09:35" % day)
        cur = t0
        while cur <= pd.Timestamp("%s 14:35" % day):
            if cur.hour == 11 and cur.minute > 35:
                pass
            ck.append(cur)
            cur += timedelta(minutes=30)
            if cur.time().hour == 11 and cur.time().minute == 35:
                cur = pd.Timestamp("%s 13:05" % day)
        traj = []
        pc_day = {c: prev_close_map.get((c, day)) for c in cohort}
        for t_ts in ck:
            g = gate_state(series, t_ts.to_pydatetime(), pc_day,
                           cohort, df_map, limit_pct)
            if g:
                traj.append((t_ts.strftime("%H:%M"), g))
        b_times = [t for t, g in traj if g["blocked"]]
        if traj:
            print("  %s: %s" % (day, " ".join(
                "%s[%s%.0f/%+.1f%%%s]" % (tm, "拦" if g["blocked"] else "放",
                                          g["rate"], g["avg"],
                                          "·" + g["why"] if g["blocked"] else "")
                for tm, g in traj)))
        # 每笔交易按推送时刻的闸门状态
        for t in [x for x in trades if x["bd"] == day]:
            last = None
            for tm, g in traj:
                if pd.Timestamp("%s %s" % (day, tm)) <= t["push"]:
                    last = g
            if last is None:
                continue
            t["blocked_at_push"] = last["blocked"]
            if last["blocked"]:
                blocked += 1
                br += t["ret"]
            else:
                taken += 1
                tr += t["ret"]
            print("    → %s %s %s 推送@%s: %s  收益 %+.2f%%"
                  % (t["bd"], t["code"], t["name"][:6],
                     t["push"].strftime("%H:%M"),
                     "被拦" if last["blocked"] else "放行", t["ret"]))

    print("\n=== 30分钟级闸门回测结论 ===")
    print("可判交易 %d 笔: 被拦 %d / 放行 %d" % (blocked + taken, blocked, taken))
    if blocked:
        print("  被拦的: 笔均 %+.2f%%  累计 %+.2f%%" % (br / blocked, br))
    if taken:
        print("  放行的: 笔均 %+.2f%%  累计 %+.2f%%" % (tr / taken, tr))
    return 0


if __name__ == "__main__":
    sys.exit(main())
