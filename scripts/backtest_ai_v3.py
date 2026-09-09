#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI v3(游资大佬提示词+六维证据) 回测 — 半路板候选, 无前视动态池。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
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

from youzi_ai import SYSTEM, finance_block, _tool  # noqa: E402

MIN30 = Path.home() / ".tradingagents" / "youzi" / "min30.pkl"
TCACHE = Path.home() / ".tradingagents" / "youzi" / "topics_cache.json"
JCACHE = Path("/tmp/ai_v3_judge.json")
FJ = Path.home() / ".tradingagents" / "youzi" / "fin_cache.json"
COST = 0.002
POOL_N = 600


def streak_matrix(lim):
    out = pd.DataFrame(0.0, index=lim.index, columns=lim.columns)
    for c in lim.columns:
        s = lim[c].fillna(False).astype(bool)
        out[c] = s.groupby((~s).cumsum()).cumsum()
    return out


def llm_judge(prompt):
    from langchain_core.messages import HumanMessage, SystemMessage
    from youzi_ai import _client
    llm = _client().get_llm()
    try:
        r = llm.invoke([SystemMessage(content=SYSTEM),
                        HumanMessage(content=prompt)])
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = pd.read_pickle(MIN30)
    df["date"] = df["datetime"].dt.date
    df = df.sort_values(["code", "datetime"])
    df["idx"] = df.groupby(["code", "date"]).cumcount()
    df["cum_amt"] = df.groupby(["code", "date"])["amount"].cumsum()
    daily = (df.groupby(["code", "date"])
             .agg(dclose=("close", "last"), amt=("amount", "sum"))
             .reset_index())
    daily["prev"] = daily.groupby("code")["dclose"].shift(1)
    daily["next"] = daily.groupby("code")["dclose"].shift(-1)
    dpiv = daily.pivot(index="date", columns="code", values="amt").sort_index()
    in_pool = (dpiv.rolling(20).mean().shift(1)
               .rank(axis=1, ascending=False) <= POOL_N)
    in_pool_s = in_pool.stack().rename("ok")

    d = df.merge(daily[["code", "date", "prev", "next"]], on=["code", "date"])
    d = d.dropna(subset=["prev", "next"])
    d["pct"] = d["close"] / d["prev"] - 1
    hit = d[(d["pct"] >= 0.09) & (d["pct"] < 0.098)]
    first = hit.loc[hit.groupby(["code", "date"])["idx"].idxmin()].copy()
    cand = first[first["idx"].isin([1, 2])].copy()
    cand["ret"] = cand["next"] / cand["close"] - 1
    keys = pd.MultiIndex.from_arrays([pd.to_datetime(cand["date"]), cand["code"]])
    cand = cand[in_pool_s.reindex(keys).fillna(False).values]
    days = sorted(cand["date"].unique())[-args.days:]
    cand = cand[cand["date"].isin(days)]
    print(f"候选 {len(cand)} 笔 / {len(days)} 天 ({days[0]}~{days[-1]})", flush=True)

    cache = json.loads(TCACHE.read_text()) if TCACHE.exists() else {}
    miss = [str(d) for d in days if str(d) not in cache]
    if miss:
        from tradingagents.agents.utils.agent_utils import get_hot_stocks
        print(f"[题材] 补拉 {len(miss)} 天 ...", flush=True)
        for i, ds in enumerate(miss):
            topics = {}
            try:
                for line in str(get_hot_stocks.invoke(
                        {"curr_date": ds})).splitlines():
                    if ":" in line and "|" in line:
                        w = line.split()
                        if w and w[0].isdigit() and len(w[0]) == 6:
                            topics[w[0]] = line.split("|")[-1].strip()
            except Exception:
                topics = {}
            cache[ds] = topics
            if (i + 1) % 20 == 0:
                TCACHE.write_text(json.dumps(cache, ensure_ascii=False))
                print(f"  ...{i+1}/{len(miss)}", flush=True)
        TCACHE.write_text(json.dumps(cache, ensure_ascii=False))

    print("[证据] 财务(akshare) ...", flush=True)
    fcache = json.loads(FJ.read_text()) if FJ.exists() else {}
    for i, code in enumerate(cand["code"].unique()):
        if code not in fcache:
            fcache[code] = finance_block(code)
        if (i + 1) % 30 == 0:
            FJ.write_text(json.dumps(fcache, ensure_ascii=False))
            print(f"  ...{i+1}", flush=True)
    FJ.write_text(json.dumps(fcache, ensure_ascii=False))

    jcache = json.loads(JCACHE.read_text()) if JCACHE.exists() else {}
    rows = []
    for i, ds in enumerate(days):
        sub = cand[cand["date"] == ds]
        if sub.empty:
            continue
        ds_s = str(ds)
        if ds_s in jcache:
            dec = jcache[ds_s]
        else:
            topics = cache.get(ds_s, {})
            freq = Counter()
            for tg in topics.values():
                for tag in str(tg).replace("、", "+").split("+"):
                    if tag.strip():
                        freq[tag.strip()] += 1
            hot = " | ".join(f"{t}×{n}" for t, n in freq.most_common(12))
            limcnt = int((sub["pct"] >= 0.098).sum())  # 候选内涨停数(近似)
            ids = []
            lines = [
                "【交易日T】日期与代码已隐去, 仅依据当时可知信息判断。",
                f"【市场情绪】当日涨停家数约 {limcnt}",
                f"【今日热门题材】{hot}",
                "【候选】均为: 盘中涨至9%以上、当前未封板(可现价买入), "
                "买入赌当日封板吃次日溢价。", ""]
            for j, (_, r) in enumerate(sub.iterrows()):
                tag = chr(ord("A") + j)
                ids.append((tag, r["code"]))
                lines.append(
                    f"- {tag}: +{r['pct']*100:.1f}% 未封板 | "
                    f"题材: {str(topics.get(r['code'], '无标注'))[:36]}\n"
                    f"  财务: {fcache.get(r['code']) or '缺失'}")
            lines += ["", "对每只候选都输出判定(BUY 或 SKIP, 附 judgement 与 "
                          "reason, prob≥75 才 BUY)。严格 JSON。"]
            dec = llm_judge("\n".join(lines))
            jcache[ds_s] = dec
            JCACHE.write_text(json.dumps(jcache, ensure_ascii=False))
        by = {str(x.get("id", "")).upper(): x for x in dec}
        for j, (_, r) in enumerate(sub.iterrows()):
            tag = chr(ord("A") + j)
            d0 = by.get(tag, {})
            rows.append({
                "date": ds_s, "code": r["code"], "ret": r["ret"],
                "action": str(d0.get("action", "NA")).upper(),
                "prob": float(d0.get("prob", 0) or 0),
            })
        if (i + 1) % 10 == 0:
            print(f"  ...{i+1}/{len(days)}", flush=True)

    R = pd.DataFrame(rows)
    R.to_csv("/tmp/ai_v3_bt.csv", index=False)
    print("\n" + "=" * 84)
    print("  AI v3(六维证据) 回测 — 半路板候选, 次日收盘卖, 扣成本0.2%")
    print("=" * 84)
    rng = np.random.default_rng(0)
    res = {}
    for lab, sub in (("AI BUY", R[R["action"] == "BUY"]),
                     ("全体候选", R)):
        v = sub["ret"].dropna().values
        g = sub.groupby("date")["ret"].mean() - COST
        eq = (1 + g).cumprod()
        tot = (eq.iloc[-1] - 1) * 100
        yrs = max((pd.Timestamp(g.index[-1]) - pd.Timestamp(g.index[0])).days
                  / 365.25, 0.1)
        ann = ((1 + tot / 100) ** (1 / yrs) - 1) * 100
        print(f"  {lab:<18}{len(v):>5}笔  笔均 {v.mean()*100:>+6.2f}%  "
              f"组合累计 {tot:>+8.0f}%  年化 {ann:>+7.1f}%  胜率 "
              f"{(v>0).mean()*100:.1f}%")
        res[lab] = v
    if "AI BUY" in res and len(res["AI BUY"]) > 5:
        pk, al = res["AI BUY"], res["全体候选"]
        dd = np.array([rng.choice(pk, len(pk), True).mean()
                       - rng.choice(al, len(al), True).mean()
                       for _ in range(5000)]) * 100
        print(f"\n  AI超额: {(pk.mean()-al.mean())*100:+.2f}pp | "
              f"95%CI [{np.percentile(dd,2.5):+.2f},{np.percentile(dd,97.5):+.2f}]"
              f" | P(>0)={(dd>0).mean()*100:.1f}%")
    print("\n  [prob 校准]")
    R["pb"] = pd.cut(R["prob"], [-1, 40, 55, 75, 101],
                     labels=["<40", "40-55", "55-75", "≥75"])
    for b in ["<40", "40-55", "55-75", "≥75"]:
        s = R[R["pb"] == b]
        if len(s):
            print(f"  {b:<8}{len(s):>5}笔  次日均值 {s['ret'].mean()*100:+.2f}%"
                  f"  胜率 {(s['ret']>0).mean()*100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
