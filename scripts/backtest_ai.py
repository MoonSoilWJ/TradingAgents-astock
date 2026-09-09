#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 决策层【回测】— 检验 AI 精排是否真的优于"无差别买入"。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
对照三线
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  A 规则基线: 9% 未封板全买 (历史回测年化 +225%, 封板率 63.4%)
  B AI 放行:  只买模型判定 BUY 的票
  C AI 否决:  被判 SKIP 的票(若它们反而更好 → AI 是负价值)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ AI 回测特有的坑: 模型训练数据里可能"记得"这些票后来的走势
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  缓解: **匿名化** —— 隐藏真实日期(用"交易日T")与股票代码(用 A/B/C 代号),
  只喂"当时可知"的结构化信息(题材/资金流/量价/位置/市场情绪)。
  残留风险: 题材组合仍可能让模型模糊联想到某段行情, 因此结果视为**参考上界**。

流程: 生成历史候选 → 拉历史题材+资金流(外网) → LLM 批量判断 → 回测对比
用法:
  python3 scripts/backtest_ai.py --days 60        # 快速验证(约15分钟)
  python3 scripts/backtest_ai.py                  # 全量 2024-07~2026-09
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

from youzi_ai import SYSTEM, _client, _tool  # noqa: E402

MIN30 = Path.home() / ".tradingagents" / "youzi" / "min30.pkl"
CACHE = Path.home() / ".tradingagents" / "youzi" / "ai_ctx_cache.json"
OUT = Path("/tmp/ai_backtest_result.json")
COST = 0.002


def load_candidates(thr=0.09, max_days=None):
    """从 30 分钟K 生成历史候选: 10:30(idx=1) 涨幅∈[thr,9.8%) 且未涨停。"""
    df = pd.read_pickle(MIN30)
    df["date"] = df["datetime"].dt.date
    df = df.sort_values(["code", "datetime"])
    df["idx"] = df.groupby(["code", "date"]).cumcount()
    df["cum_amt"] = df.groupby(["code", "date"])["amount"].cumsum()
    daily = (df.groupby(["code", "date"])
             .agg(close=("close", "last"), amt=("amount", "sum")).reset_index())
    daily["prev_close"] = daily.groupby("code")["close"].shift(1)
    daily["prev_amt"] = daily.groupby("code")["amt"].shift(1)
    daily["next_close"] = daily.groupby("code")["close"].shift(-1)
    d = df.merge(daily[["code", "date", "prev_close", "prev_amt", "next_close", "amt"]],
                 on=["code", "date"], how="left").dropna(
        subset=["prev_close", "prev_amt", "next_close"])
    s = d[d["idx"] == 1].copy()
    s["pct"] = s["close"] / s["prev_close"] - 1
    s["vr"] = (s["cum_amt"] / 60 * 240) / s["prev_amt"]
    s["ret"] = s["next_close"] / s["close"] - 1      # 次日收盘卖
    s["sealed"] = (daily.set_index(["code", "date"])["close"] /
                   daily.set_index(["code", "date"])["prev_close"] - 1
                   ).reindex(pd.MultiIndex.from_arrays(
                       [s["code"], s["date"]])).values >= 0.098
    c = s[(s["pct"] >= thr) & (s["pct"] < 0.098)].copy()
    days = sorted(c["date"].unique())
    if max_days:
        days = days[-max_days:]
        c = c[c["date"].isin(days)]
    return c, s, days


def fetch_ctx(date, codes, cache):
    """历史题材(每日一次) + 资金流(每只一次)。带缓存。"""
    ds = str(date)
    if ds not in cache:
        topics = {}
        t = _tool("get_hot_stocks")
        if t is not None:
            try:
                for line in str(t.invoke({"curr_date": ds})).splitlines():
                    if ":" in line and "|" in line:
                        w = line.split()
                        if w and w[0].isdigit() and len(w[0]) == 6:
                            topics[w[0]] = line.split("|")[-1].strip()
            except Exception:
                pass
        cache[ds] = {"topics": topics, "fund": {}}
    ff = _tool("get_fund_flow")
    for code in codes:
        if code in cache[ds]["fund"]:
            continue
        txt = ""
        if ff is not None:
            try:
                txt = str(ff.invoke({"ticker": code, "curr_date": ds}))[:300]
            except Exception:
                txt = ""
        cache[ds]["fund"][code] = txt
    return cache[ds]


def build_anon_prompt(cands: pd.DataFrame, ctx: dict, n_limit: int) -> tuple[str, list]:
    """匿名化: 不出现真实日期与股票代码, 只给 A/B/C 代号。"""
    from collections import Counter
    cnt: Counter = Counter()
    for _c, tp in (ctx.get("topics") or {}).items():
        for tag in str(tp).replace("、", "+").split("+"):
            if tag.strip():
                cnt[tag.strip()] += 1
    hot = " | ".join(f"{t}×{n}" for t, n in cnt.most_common(16)) or "数据缺失"
    ids, lines = [], [
        "【交易日 T】日期与股票代码均已隐去(防止依赖记忆), 请仅依据下列当时可知的信息判断。",
        f"【市场情绪】当日涨停约 {n_limit} 只",
        f"【今日热门题材】(当日强势股题材标注统计) {hot}",
        "", "【候选标的】(均为未封板, 盘口有卖单可成交)", ""]
    for i, (_, r) in enumerate(cands.iterrows()):
        tag = chr(ord("A") + i)
        ids.append((tag, r["code"]))
        fund = (ctx.get("fund", {}).get(r["code"], "") or "").replace("\n", " ")[:220]
        lines.append(
            f"### 标的 {tag}\n"
            f"- 涨幅 {r['pct']*100:.2f}% (距涨停 {9.8 - r['pct']*100:.1f}pct) | "
            f"量比 {r['vr']:.1f} | 成交额 {r['amount']/1e8:.2f}亿\n"
            f"- 资金流: {fund or '缺失'}")
        lines.append("")
    lines.append("对每只给出判断, 严格输出 JSON 数组, id 用 A/B/C:")
    lines.append('[{"id":"A","action":"BUY","prob":70,"reason":"..."}]')
    return "\n".join(lines), ids


def llm_judge(prompt: str) -> list[dict]:
    from langchain_core.messages import HumanMessage, SystemMessage
    llm = _client().get_llm()
    try:
        r = llm.invoke([SystemMessage(content=SYSTEM), HumanMessage(content=prompt)])
        text = getattr(r, "content", None) or str(r)
    except Exception as exc:
        print(f"    [AI] 失败 {type(exc).__name__} {str(exc)[:80]}")
        return []
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except Exception:
        return []


def perf(series: pd.Series) -> tuple[float, float, float]:
    if len(series) < 5:
        return (np.nan,) * 3
    eq = (1 + series).cumprod()
    tot = (eq.iloc[-1] - 1) * 100
    yrs = max((series.index[-1] - series.index[0]).days / 365.25, 0.1)
    ann = ((1 + tot / 100) ** (1 / yrs) - 1) * 100
    return tot, ann, series.mean() * 100


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只跑最近 N 个交易日")
    ap.add_argument("--thr", type=float, default=0.09)
    args = ap.parse_args()

    c, allrows, days = load_candidates(args.thr, args.days or None)
    print("=" * 86)
    print("  AI 决策层回测 (匿名化防模型记忆泄漏)")
    print(f"  {days[0]} ~ {days[-1]} | {len(days)} 天 | 候选 {len(c)} 笔")
    print("=" * 86)

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    print("\n[1/3] 拉取历史题材 + 资金流 (外网, 较慢) ...", flush=True)
    for i, d in enumerate(days):
        sub = c[c["date"] == d]
        fetch_ctx(d, list(sub["code"]), cache)
        if (i + 1) % 10 == 0:
            print(f"  ...{i+1}/{len(days)}", flush=True)
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(cache, ensure_ascii=False))

    print("\n[2/3] LLM 批量判断 ...", flush=True)
    JR = Path("/tmp/ai_judge_cache.json")
    jcache = json.loads(JR.read_text()) if JR.exists() else {}
    rows = []
    for i, d in enumerate(days):
        sub = c[c["date"] == d]
        if sub.empty:
            continue
        nlim = int((allrows[allrows["date"] == d]["pct"] >= 0.098).sum())
        prompt, ids = build_anon_prompt(sub, cache.get(str(d), {}), nlim)
        if str(d) in jcache:                    # 中断可续
            dec = jcache[str(d)]
        else:
            dec = llm_judge(prompt)
            jcache[str(d)] = dec
            JR.write_text(json.dumps(jcache, ensure_ascii=False))
        by = {str(x.get("id", "")).upper(): x for x in dec}
        for tag, code in ids:
            r = sub[sub["code"] == code].iloc[0]
            dd = by.get(tag, {})
            rows.append({
                "date": str(d), "code": code, "pct": r["pct"], "vr": r["vr"],
                "ret": r["ret"], "sealed": bool(r["sealed"]),
                "action": str(dd.get("action", "NA")).upper(),
                "prob": float(dd.get("prob", 0) or 0),
                "reason": str(dd.get("reason", ""))[:100],
            })
        if (i + 1) % 10 == 0:
            print(f"  ...{i+1}/{len(days)}", flush=True)
    R = pd.DataFrame(rows)
    R.to_csv("/tmp/ai_bt.csv", index=False)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1))

    print("\n[3/3] 回测对比 (成本 0.2%/天, 次日收盘卖)")
    def bt(sub):
        if sub.empty:
            return (np.nan,) * 5
        g = sub.groupby("date")["ret"].mean() - COST
        g.index = pd.to_datetime(g.index)         # date 是字符串, 需转换
        t, a, dr = perf(g)
        return t, a, dr, (sub["ret"] > 0).mean() * 100, sub["sealed"].mean() * 100

    print(f"  {'口径':<24}{'笔数':>7}{'日均':>9}{'累计':>12}{'年化':>10}"
          f"{'胜率':>8}{'封板率':>9}")
    for label, sub in (("A 规则基线(全买)", R),
                       ("B AI放行(BUY)", R[R["action"] == "BUY"]),
                       ("C AI否决(SKIP)", R[R["action"] == "SKIP"]),
                       ("D AI观望(WAIT)", R[R["action"] == "WAIT"])):
        t, a, dr, wr, sr = bt(sub)
        print(f"  {label:<24}{len(sub):>7}{dr:>+8.3f}%{t:>+11.1f}%{a:>+9.1f}%"
              f"{wr:>7.1f}%{sr:>8.1f}%")

    # 概率校准: AI 说的封板概率 vs 实际
    print(f"\n  概率校准 (AI 估计的封板概率 vs 实际封板率)")
    print(f"  {'AI概率区间':<16}{'笔数':>7}{'实际封板率':>12}")
    R["pb"] = pd.cut(R["prob"], [-1, 40, 55, 70, 101],
                     labels=["<40", "40-55", "55-70", ">70"])
    for b in ["<40", "40-55", "55-70", ">70"]:
        s = R[R["pb"] == b]
        if len(s) == 0:
            continue
        print(f"  {b:<16}{len(s):>7}{s['sealed'].mean()*100:>11.1f}%")
    print(f"\n  (若实际封板率不随 AI 概率单调上升 → 模型只是编理由, 该层应删除)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
