#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 全权游资决断实验 (30个交易日) — 最后一个可测组合。

设定: AI 扮演游资大佬, 收盘后看当日人气涨停股(题材/连板/情绪),
      自主决定次日接力哪些(可以全放弃)。
执行: AI 点名的票 T+1 竞价买(排一字) → T+1 收盘卖。A股T+1约束。
判定: AI 点名组 vs 无差别接力全体的次日收益。
"""
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

MB = Path.home() / ".tradingagents" / "youzi" / "mb_daily.pkl"
TCACHE = Path.home() / ".tradingagents" / "youzi" / "topics_cache.json"
JCACHE = Path("/tmp/ai_full_judge.json")
COST = 0.002
TOP_N = 30

SYSTEM = """你是一位资金体量数亿的 A 股游资大佬, 深耕打板接力十几年。

每天收盘后, 你看到当日人气涨停股列表(题材/连板/市场情绪)。
任务: 决定明天竞价接力哪些票。可以一只都不接 — 空仓也是仓位。

【赚的钱】接力溢价: 强势股次日惯性高开或继续涨停。
【怕的坑】一字板买不到(已剔除); 高位断板; 冰点接飞刀; 孤板杂毛。

【基准】无差别接力所有涨停股: 笔均 -0.05%(扣成本后微亏)。
你的价值 = 只挑明显强于平均的, 或识别"今天不值得做"。

【框架】题材主线(多股涨停=有合力, 孤板=危险) / 高度结构(空间板在哪,
梯队是否完整) / 情绪(涨停家数) / 量能(放量首板>缩量板, 天量滞涨警惕)。
每只票给接力意愿评分(0-100)。

【输出】严格 JSON, 只输出值得接力的票(0~3只, 宁缺毋滥), 全放弃输出 []:
[{"id":"A","action":"BUY","score":80,"reason":"30字内"}]
"""


def streak_matrix(lim):
    out = pd.DataFrame(0.0, index=lim.index, columns=lim.columns)
    for c in lim.columns:
        s = lim[c].fillna(False).astype(bool)
        out[c] = s.groupby((~s).cumsum()).cumsum()
    return out


def llm_judge(prompt: str) -> list[dict]:
    from langchain_core.messages import HumanMessage, SystemMessage
    from tradingagents.llm_clients.factory import create_llm_client
    llm = create_llm_client(provider="qwen", model="qwen-plus").get_llm()
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
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    df = pd.read_pickle(MB)
    piv = lambda v: df.pivot(index="date", columns="code", values=v).sort_index()
    close, openp, amount = piv("close"), piv("open"), piv("amount")
    close = close[close.index >= pd.Timestamp("2022-04-01")]
    for m in (close, openp, amount):
        m.columns = [str(c) for c in m.columns]
    openp = openp.reindex(close.index)
    amount = amount.reindex(close.index)
    prev = close.shift(1)
    dret = close / prev - 1
    lim = dret >= 0.098
    st = streak_matrix(lim)
    nlimit = lim.sum(axis=1)
    max_st = st.where(lim).max(axis=1)
    amt_rank = amount.rank(axis=1, ascending=False)

    t_yizi = openp / prev - 1 >= 0.098
    cand_mask = lim & (amt_rank <= TOP_N) & ~t_yizi
    nx_open, nx_close = openp.shift(-1), close.shift(-1)
    exec_ok = (nx_open / close - 1) < 0.098
    ret = nx_close / nx_open - 1

    days = [str(pd.Timestamp(d).date()) for d in close.index
            if cand_mask.loc[d].any()]
    days = days[-args.days:]
    print("=" * 88)
    print("  AI 全权游资决断实验 — 人气涨停股, AI 自选接力对象")
    print(f"  {days[0]} ~ {days[-1]} | {len(days)} 天")
    print("=" * 88)

    cache = json.loads(TCACHE.read_text()) if TCACHE.exists() else {}
    jcache = json.loads(JCACHE.read_text()) if JCACHE.exists() else {}
    rows = []
    for i, ds in enumerate(days):
        dt = pd.Timestamp(ds)
        s = cand_mask.loc[dt]
        codes = list(s.index[s])
        if not codes:
            continue
        if ds not in cache:
            topics = {}
            try:
                from tradingagents.agents.utils.agent_utils import get_hot_stocks
                for line in str(get_hot_stocks.invoke(
                        {"curr_date": ds})).splitlines():
                    if ":" in line and "|" in line:
                        w = line.split()
                        if w and w[0].isdigit() and len(w[0]) == 6:
                            topics[w[0]] = line.split("|")[-1].strip()
            except Exception:
                topics = {}
            cache[ds] = topics
            TCACHE.parent.mkdir(parents=True, exist_ok=True)
            TCACHE.write_text(json.dumps(cache, ensure_ascii=False))
        topics = cache.get(ds) or {}
        freq = Counter()
        for tg in topics.values():
            for tag in str(tg).replace("、", "+").split("+"):
                if tag.strip():
                    freq[tag.strip()] += 1
        hot = " | ".join(f"{t}×{n}" for t, n in freq.most_common(12)) or "缺失"

        cands = sorted(codes, key=lambda c: -float(amount.loc[dt, c]))
        ids, lines = [], [
            f"【交易日T】日期与代码已隐去。涨停家数 {int(nlimit.loc[dt])}, "
            f"市场最高连板 {int(max_st.loc[dt])} 板",
            f"【当日热门题材】{hot}",
            "【候选人气涨停股】(按成交额降序, 次日非一字可竞价买入)", ""]
        for j, c in enumerate(cands):
            tag = chr(ord("A") + j)
            ids.append((tag, c))
            lines.append(f"- {tag}: 涨停 | {int(st.loc[dt, c])}板 | "
                         f"题材: {str(topics.get(c, '无标注'))[:40]}")
        lines += ["", "决定明天接力哪些? 严格输出 JSON(0~3只):",
                  '[{"id":"A","action":"BUY","score":80,"reason":"..."}]']

        if ds in jcache:
            dec = jcache[ds]
        else:
            dec = llm_judge("\n".join(lines))
            jcache[ds] = dec
            JCACHE.write_text(json.dumps(jcache, ensure_ascii=False))
        by = {str(x.get("id", "")).upper(): x for x in dec}
        for tag, c in ids:
            d = by.get(tag, {})
            r_exec = ret.loc[dt, c] if c in ret.columns else np.nan
            rows.append({
                "date": ds, "code": c, "ret": r_exec,
                "exec_ok": bool(exec_ok.loc[dt, c]) if c in exec_ok.columns else False,
                "picked": d.get("action") == "BUY",
                "score": float(d.get("score", 0) or 0),
                "reason": str(d.get("reason", ""))[:60],
            })
        if (i + 1) % 10 == 0:
            print(f"  ...{i+1}/{len(days)}", flush=True)

    R = pd.DataFrame(rows)
    R.to_csv("/tmp/ai_full_bt.csv", index=False)
    E = R[R["exec_ok"]]
    print("\n" + "=" * 84)
    print("  结果 (T+1竞价买→收盘卖, 扣成本0.2%)")
    print("=" * 84)
    for lab, sub in (("AI 点名接力", E[E["picked"]]),
                     ("对照:无差别接力全体", E),
                     ("AI 放弃的", E[~E["picked"]])):
        v = sub["ret"].dropna()
        if len(v) == 0:
            print(f"  {lab:<22} 0 笔")
            continue
        eq = (1 + v.sort_index()).cumprod()
        print(f"  {lab:<22}{len(v):>5}笔  笔均 {v.mean()*100:>+6.2f}%  "
              f"胜率 {(v>0).mean()*100:>5.1f}%  简单累计 {(eq.iloc[-1]-1)*100:>+7.0f}%")
    pk = E[E["picked"]]
    if len(pk):
        print(f"\n  次日继续涨停率: AI点名 {(pk['ret']>=0.098).mean()*100:.1f}%"
              f" vs 全体 {(E['ret']>=0.098).mean()*100:.1f}%")
    print("\n  [AI 点名理由]")
    for _, r in pk.iterrows():
        print(f"  {r['date']} {r['code']}: {r['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
