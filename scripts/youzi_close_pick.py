#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游资 AI 收盘点名 v2 — 增强基本面/封板质量/资金面维度, 收盘后模拟点名。

新增维度(vs v1): ①业绩快报(净利同比, 区分业绩兑现型/概念炒作型)
②封板质量(30分钟K: 尾盘封死/开板次数) ③资金流持续性。
输出: picks.json 研究日志(次日自动验证) + 钉钉推送。
crontab: 5 15 * * 1-5
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
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

from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams
from tradingagents.notify.dingtalk import send_markdown

MB = Path.home() / ".tradingagents" / "youzi" / "mb_daily.pkl"
TCACHE = Path.home() / ".tradingagents" / "youzi" / "topics_cache.json"
YJBB = Path.home() / ".tradingagents" / "youzi" / "yjbb.json"
PICKS = Path.home() / ".tradingagents" / "youzi" / "picks.json"
TDX = ("180.153.18.170", 7709)
COST = 0.002
TOP_N = 30

SYSTEM = """你是一位资金体量数亿的 A 股游资大佬, 深耕打板接力十几年。
每天收盘后看到当日人气涨停股的多维数据, 决定明天竞价接力哪些(可全放弃)。

【决策维度 — 证据先于结论】
1. 涨停性质: 业绩兑现型(预增实锤+行业景气) > 政策事件驱动 > 纯概念炒作。
   历史规律: 业绩兑现型涨停的次日接力溢价显著高于概念炒作型。
2. 业绩: 净利润同比增速为实数据。亏损或大幅下滑 → 接力谨慎。
3. 资金面: 主力当日流入且持续 → 强; 仅当日爆量(一日游) → 弱。
4. 封板质量: 全天封死 → 强; 反复开板/尾盘炸 → 烂板次日弱。
5. 题材主线: 同题材多股涨停(合力) > 孤板。
6. 高度结构: 空间板、梯队完整性。7. 量能: 放量健康 > 天量滞涨。

【BUY 纪律】
- 无差别接力所有涨停股笔均 -0.05%。你的价值是只挑强的或识别"今天不值得做"。
- 最多 BUY 2 只; 都不够格输出 [] 是正确答案。
- 概念炒作型(无业绩纯题材)只在题材为市场绝对主线时才可 BUY。

【输出】严格 JSON 数组(0~2只), 先证据后结论:
[{"id":"A","evidence":{"涨停性质":"..","业绩":"..","资金面":"..","封板质量":"..","题材":".."},
  "prob":78,"action":"BUY","reason":"40字内"}]
"""


def streak_matrix(lim):
    out = pd.DataFrame(0.0, index=lim.index, columns=lim.columns)
    for c in lim.columns:
        s = lim[c].fillna(False).astype(bool)
        out[c] = s.groupby((~s).cumsum()).cumsum()
    return out


def yjbb_map() -> dict:
    if YJBB.exists():
        try:
            return json.loads(YJBB.read_text())
        except Exception:
            pass
    out = {}
    try:
        import akshare as ak
        today = date.today()
        for mmdd in ("0630", "0331", "0930", "1231"):
            y = today.year if int(mmdd[:2]) < today.month else today.year - 1
            try:
                d = ak.stock_yjbb_em(date=f"{y}{mmdd}")
            except Exception:
                continue
            if d is None or len(d) == 0:
                continue
            c_code = next((c for c in d.columns if "股票代码" in c), None)
            c_gr = next((c for c in d.columns if "净利润" in c and "同比" in c), None)
            c_np = next((c for c in d.columns if c == "净利润-净利润"), None)
            if not c_code or not c_gr:
                continue
            for _, r in d.iterrows():
                code = str(r[c_code]).zfill(6)
                try:
                    g = float(r[c_gr])
                except Exception:
                    continue
                tag = f"净利同比{g:+.0f}%"
                if c_np:
                    try:
                        if float(r[c_np]) < 0:
                            tag += "(亏损)"
                    except Exception:
                        pass
                out[code] = tag
            out["__period__"] = f"{y}{mmdd}"
            break
    except Exception:
        pass
    YJBB.parent.mkdir(parents=True, exist_ok=True)
    YJBB.write_text(json.dumps(out, ensure_ascii=False))
    return out


def seal_quality(api, code: str, prev_close: float):
    m = TDXParams.MARKET_SH if code[0] in "56" else TDXParams.MARKET_SZ
    try:
        bars = api.get_security_bars(TDXParams.KLINE_TYPE_30MIN, m,
                                     code.encode(), 0, 10)
    except Exception:
        return "未知"
    if not bars:
        return "未知"
    d = api.to_df(bars)
    d["date"] = pd.to_datetime(d["datetime"]).dt.date
    today = d[d["date"] == d["date"].iloc[-1]]
    if len(today) == 0:
        return "未知"
    lim_px = prev_close * 1.098
    sealed = today["close"] >= lim_px - 1e-6
    opens = int((~sealed).sum())
    tail = bool(sealed.iloc[-1])
    if opens == 0 and tail:
        return "全天封死"
    if tail:
        return f"开板{opens}次后尾盘回封"
    if opens == 0:
        return "盘中未封死"
    return f"开板{opens}次尾盘未回封"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    now = datetime.now()
    ds = now.strftime("%Y-%m-%d")
    api = TdxHq_API()
    if not api.connect(*TDX, time_out=5):
        print("! pytdx 连接失败")
        return 1

    df = pd.read_pickle(MB)
    piv = lambda v: df.pivot(index="date", columns="code", values=v).sort_index()
    close, openp, amount = piv("close"), piv("open"), piv("amount")
    for m_ in (close, openp, amount):
        m_.columns = [str(c) for c in m_.columns]
    openp = openp.reindex(close.index)
    amount = amount.reindex(close.index)
    # mb_daily 更新于每日 8:30(缺当日行) → 收盘后用 pytdx 实时快照合成今日行
    all_codes = []
    for market in (TDXParams.MARKET_SH, TDXParams.MARKET_SZ):
        cnt = api.get_security_count(market)
        for s0 in range(0, cnt, 1000):
            for it in (api.get_security_list(market, s0) or []):
                code = str(it.get("code", ""))
                name = str(it.get("name", "")).strip()
                if code.startswith(("60", "00")) and "ST" not in name \
                        and "退" not in name \
                        and not name.startswith(("N", "XD", "XR", "DR")):
                    all_codes.append((market, code))
    quotes = {}
    for i in range(0, len(all_codes), 80):
        try:
            q = api.get_security_quotes(all_codes[i:i + 80]) or []
        except Exception:
            q = []
        for x in q:
            pc = float(x.get("last_close") or 0)
            px = float(x.get("price") or 0)
            if pc > 0 and px > 0:
                quotes[str(x.get("code", ""))] = (px, pc, float(x.get("amount") or 0))
    tdate = pd.Timestamp(ds)
    close.loc[tdate] = pd.Series({c: px for c, (px, pc, am) in quotes.items()})
    amount.loc[tdate] = pd.Series({c: am for c, (px, pc, am) in quotes.items()})
    prev = close.shift(1)
    lim = close / prev - 1 >= 0.098
    st = streak_matrix(lim)
    amt_rank = amount.rank(axis=1, ascending=False)
    last = close.index[-1]
    print(f"[诊断] 面板末日 {last.date()} | 当日涨停 {int(lim.loc[last].sum())} | "
          f"快照数 {len(quotes)} | rank≤30 {int((amt_rank.loc[last] <= TOP_N).sum())} | "
          f"交集 {int((lim.loc[last] & (amt_rank.loc[last] <= TOP_N)).sum())} | "
          f"amount末日均值 {float(amount.loc[last].mean()):.0f}")
    s = lim.loc[last] & (amt_rank.loc[last] <= TOP_N)
    codes = sorted(s.index[s], key=lambda c: -float(amount.loc[last, c]))
    if not codes:
        print("[跳过] 今日无候选")
        return 0

    yj = yjbb_map()
    topics = json.loads(TCACHE.read_text()) if TCACHE.exists() else {}
    topics = topics.get(ds) or {}
    freq = Counter()
    for tg in topics.values():
        for tag in str(tg).replace("、", "+").split("+"):
            if tag.strip():
                freq[tag.strip()] += 1
    hot = " | ".join(f"{t}×{n}" for t, n in freq.most_common(12)) or "缺失"
    max_st = int(st.loc[last].where(lim.loc[last]).max())

    ids, lines = [], [
        f"【交易日T】日期与代码已隐去。涨停家数 {int(lim.loc[last].sum())}, "
        f"市场最高连板 {max_st} 板, 当日热门题材: {hot}",
        "【候选人气涨停股】(多维数据)", ""]
    try:
        from tradingagents.agents.utils.agent_utils import get_fund_flow
        ff_tool = get_fund_flow
    except Exception:
        ff_tool = None
    for j, c in enumerate(codes):
        tag = chr(ord("A") + j)
        ids.append((tag, c))
        sq = seal_quality(api, c, float(prev.loc[last, c]))
        ff = "缺失"
        if ff_tool:
            try:
                ff = str(ff_tool.invoke({"ticker": c, "curr_date": ds}))[:170].replace("\n", " ")
            except Exception:
                pass
        lines.append(f"- {tag}: {int(st.loc[last, c])}板 | 业绩: "
                     f"{yj.get(c, '未披露/无快报')} | 题材: "
                     f"{str(topics.get(c, '无标注'))[:36]} | 封板: {sq} | 资金: {ff}")
    lines += ["", "决定明天竞价接力哪些? 严格输出 JSON(0~2只):",
              '[{"id":"A","evidence":{"涨停性质":"..","业绩":"..","资金面":"..",'
              '"封板质量":"..","题材":".."},"prob":78,"action":"BUY","reason":".."}]']
    prompt = "\n".join(lines)
    print(prompt[:2600])

    if args.dry_run:
        try:
            api.disconnect()
        except Exception:
            pass
        return 0

    from langchain_core.messages import HumanMessage, SystemMessage
    from tradingagents.llm_clients.factory import create_llm_client
    llm = create_llm_client(provider="qwen", model="qwen-plus").get_llm()
    try:
        r = llm.invoke([SystemMessage(content=SYSTEM), HumanMessage(content=prompt)])
        text = getattr(r, "content", None) or str(r)
    except Exception as exc:
        print(f"[AI] 失败 {exc}")
        return 1
    m = re.search(r"\[.*\]", text, re.S)
    dec = json.loads(m.group(0)) if m else []
    by = {str(x.get("id", "")).upper(): x for x in dec}
    try:
        api.disconnect()
    except Exception:
        pass

    # 存研究日志(次日语境验证: 记录候选全量与点名)
    picks = json.loads(PICKS.read_text()) if PICKS.exists() else {}
    picks[ds] = {
        "candidates": [{"id": t, "code": c, "entry_next_open": None} for t, c in ids],
        "picks": {c: {"tag": t, **by.get(t, {})} for t, c in ids
                  if by.get(t, {}).get("action") == "BUY"},
        "ai_raw": dec,
    }
    PICKS.parent.mkdir(parents=True, exist_ok=True)
    PICKS.write_text(json.dumps(picks, ensure_ascii=False, indent=1))

    pk = [x for x in dec if str(x.get("action", "")).upper() == "BUY"]
    title = f"游资AI收盘点名 {len(pk)}只 {ds}"
    text = f"### 游资AI收盘点名 · {ds}\n**候选**: {len(codes)} 只人气涨停 → 点名 {len(pk)}\n"
    for t, c in ids:
        d = by.get(t, {})
        if d.get("action") == "BUY":
            ev = d.get("evidence", {})
            text += (f"\n- **[{d.get('prob',0)}%] {t}** {d.get('reason','')}\n"
                     f"  业绩:{ev.get('业绩','-')} | 封板:{ev.get('封板质量','-')} | "
                     f"题材:{ev.get('题材','-')}")
    if not pk:
        text += "\n- 今日全部放弃(空仓也是仓位)"
    text += "\n\n> 研究日志模式: 点名次日竞价买入(模拟), 次日自动验证收益。不下单。"
    print(text)
    import os
    webhook = (os.getenv("DINGTALK_YOUZI_WEBHOOK")
               or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    if webhook:
        send_markdown(title, text, webhook=webhook,
                      keyword=(os.getenv("DINGTALK_YOUZI_KEYWORD")
                               or os.getenv("DINGTALK_KEYWORD") or "游资").strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
