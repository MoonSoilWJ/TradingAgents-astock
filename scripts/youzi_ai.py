#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""youzi_live 的 AI 评分层 — 游资大佬提示词 + 六维证据(含财务/盘中站稳度)。

升级依据:
  · 提示词: 30天决断实验(人气涨停股接力)验证的游资大佬框架, 适配半路板语境
  · 新维度: 财务(akshare 绕代理: 净利增速/毛利率/负债率) + 盘中站稳度(30分钟K)
  · 输出: 强制 judgement 六项证据, 只输出 BUY(0~3只), 全放弃输出 []
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, time as dtime
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

SYSTEM = """你是一位资金体量数亿的 A 股游资大佬, 深耕打板接力十几年。

【你在做什么】盘中半路板: 候选是"涨幅达到系统筛选阈值、但尚未封板"的票,
现价买入, 赌它今天封住涨停, 明天吃溢价。好处: 还没封板, 盘口有卖单, 买得到。
风险: 封不住就回落, 当天即亏。

【筛选口径】涨幅 ≥ {THR}% — 系统已按此筛好, 候选必然满足;
**不得再以"未达 9%/更高阈值"为由扣分**, 那是重复筛选。

【统计基准 — 仅供校准 prob 标尺, 不构成出手限制】
无差别买入该口径的全部候选: 封板率 {SEAL}%, 次日均 {AVG}%(净)。

【弹药约束 — 你的决策现实, 不是判断规则】
系统每日最多执行 3 笔, 单轮最多 2 笔; prob 达标({MINP}+)的候选往往多于名额。
你的每个高分都在与其他候选竞争稀缺名额 — 把 prob 留给真正有把握的票,
大多数候选应落在 40-60 区间; 大面积 65+ 说明你的标尺过松。

【判断证据 — 逐项看, 权衡由你, 无任何单条否决规则】
封板本质是盘口资金博弈 — 亏损妖股照样封死, 业绩暴增股也可能反复炸板。
所有维度都只是证据, 出不出手是你作为操盘手的综合判断;
历史平均值描述的是"平均规律", 个体永远可能偏离平均。
1. 盘口承接: 距涨停距离(越近越主动)、今日触板被砸次数
   (0-1次=抛压轻, ≥3次=反复炸板抛压重)、买一/卖一委量比(买方主导易封)、
   现价相对分时均价(上方=承接强, 下方=回落中)
2. 资金面: 主力/超大单净流入方向与持续性; 超大单主导买入 vs 中小单为主
3. 题材联动: 主线还是杂毛? 同题材当日几只涨停(有合力 vs 孤板)? 龙头还是末位跟风?
4. 基本面: 涨停原因(业绩/政策/概念/无)、盈亏状况、负债水平
5. 首触形态与时段: 【当前时间】附有各时段历史统计均值 — 它描述平均规律,
   不代表当下这只票, 结合个股证据自行权衡其权重
6. 位置与情绪: 连板高度与梯队、流通市值、均线形态、距60日高、大盘涨停家数

【数据缺失】某维度缺失时降低整体置信度, 聚焦可得的维度判断, 不因此单独否决。

【输出】严格 JSON, 对【每只候选】都给出判定(BUY 或 SKIP):
[{"id":"A","action":"BUY","prob":80,
  "judgement":{"盘口":"...","资金":"...","题材":"...","基本面":"...","首触":"...","风险":"..."},
  "reason":"40字内结论"}]
prob = 你对"今天封住板且明日有溢价"的真实概率估计(0-100)。
你的每次判断都会被记录, 并在收盘后与实际结果(是否封板/次日溢价)比对 —
给分必须经得起复盘, 不要为了措辞松紧而漂移。
"""


def _tool(name: str):
    try:
        import tradingagents.agents.utils.agent_utils as U
        return getattr(U, name)
    except Exception:
        return None


def _client():
    from tradingagents.llm_clients.factory import create_llm_client
    provider = (os.getenv("YOUZI_LLM_PROVIDER") or "qwen").strip()
    model = (os.getenv("YOUZI_LLM_MODEL") or "qwen-plus").strip()
    # temperature=0: 盘中决策需要可复现 — 同样输入必须得到同样判定
    return create_llm_client(provider=provider, model=model, temperature=0)


def finance_block(code: str) -> str:
    """akshare 财务摘要: 净利增速/毛利率/负债率(临时绕代理, 直连更快)。"""
    saved = {k: os.environ.pop(k) for k in list(os.environ)
             if "proxy" in k.lower()}
    try:
        import akshare as ak
        df = ak.stock_financial_analysis_indicator(
            symbol=code, start_year=str(datetime.now().year - 1))
        if df is None or len(df) == 0:
            return ""
        row = df.iloc[-1]
        parts = []
        for col, key in (("净利润增长率(%)", "净利增"), ("销售毛利率(%)", "毛利率"),
                         ("负债与所有者权益比率(%)", "负债/权益")):
            v = row.get(col)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                parts.append(f"{key} 无")
            else:
                parts.append(f"{key} {float(v):.1f}%")
        return "，".join(parts)
    except Exception:
        return ""
    finally:
        os.environ.update(saved)


def first_touch_block(api, code: str) -> str:
    """首触形态: 10:00 时是否已在 9% 上方。

    回测(609笔,【A】表): 开盘半小时内已冲到 9% 的票次日 +0.17% (差),
    10:00-10:30 间走强到 9% 的 +1.66% (好) — "早站稳"反而弱。
    """
    try:
        # pytdx: MARKET_SH=1, MARKET_SZ=0 (此前写反导致沪市票拉不到数据)
        from pytdx.params import TDXParams
        m = TDXParams.MARKET_SH if code[0] in "569" else TDXParams.MARKET_SZ
        bars = None
        try:
            bars = api.get_security_bars(2, m, code.encode(), 0, 16)
        except Exception:
            bars = None
        if not bars and api is not None:
            try:
                api.disconnect()
            except Exception:
                pass
            for _ in range(3):
                try:
                    if api.connect("180.153.18.170", 7709, time_out=5):
                        bars = api.get_security_bars(2, m, code.encode(),
                                                     0, 16)
                        break
                except Exception:
                    time.sleep(1)
        if not bars:
            return ""
        d = api.to_df(bars)
        d["date"] = d["datetime"].str[:10]
        today = d["date"].iloc[-1]
        y = d["date"] != today
        if not y.any():
            return ""
        prev_c = float(d[y]["close"].iloc[-1])
        t = d[d["date"] == today]
        if len(t) == 0:
            return ""
        p10 = float(t["close"].iloc[0]) / prev_c - 1    # 10:00 时点涨幅
        if p10 >= 0.09:
            return "开盘半小时内已冲至9% (历史表现差组)"
        return "10:00后走强至9% (历史表现优组)"
    except Exception:
        return ""


def money_proxy_block(api, code: str) -> str:
    """东财资金流不可达时的本地代理指标(30分钟K算, 与主力动向高度相关):
       ① 收盘位置 = (现价-当日低)/(当日高-当日低)  → 收在高位=买盘主动
       ② 近30分钟量能 = 最后一根成交额 / 前5根均值 → 放量方向
       ③ 相对均价 = 现价 / 当日成交额均价的偏离   → 均价上方=资金承接强
    """
    try:
        # pytdx: MARKET_SH=1, MARKET_SZ=0 (此前写反导致沪市票拉不到数据)
        from pytdx.params import TDXParams
        m = TDXParams.MARKET_SH if code[0] in "569" else TDXParams.MARKET_SZ
        bars = api.get_security_bars(2, m, code.encode(), 0, 16)
        if not bars:
            return ""
        d = api.to_df(bars)
        d["date"] = d["datetime"].str[:10]
        today = d["date"].iloc[-1]
        t = d[d["date"] == today]
        if len(t) < 3:
            return ""
        hi, lo = float(t["high"].max()), float(t["low"].min())
        close = float(t["close"].iloc[-1])
        pos = (close - lo) / (hi - lo) if hi > lo else 0.5
        amt = t["amount"].astype(float)
        vr30 = float(amt.iloc[-1]) / max(float(amt.iloc[-6:-1].mean()), 1e-9)
        vol = t["vol"].astype(float).sum()
        vwap = float(amt.sum()) / max(vol, 1e-9) / 100.0
        vs = close / vwap - 1 if vwap > 0 else 0
        return (f"收盘位置 {pos*100:.0f}% | 近30分量能 {vr30:.1f}x | 相对均价 {vs*100:+.1f}%")
    except Exception:
        return ""


def orderbook_block(api, code: str, thr_pct: float) -> str:
    """盘口承接(半路板最直接的封板证据, pytdx 快照+1分钟K):
       ① 距涨停 = 现价离涨停价还差几个点(越近越主动)
       ② 触板次数 = 当日冲到涨停价又被砸回的次数(≥3次=反复炸板抛压重)
       ③ 委量比 = 买一委量 / 卖一委量(买方排队意愿)
    """
    try:
        from pytdx.params import TDXParams
        m = TDXParams.MARKET_SH if code[0] in "569" else TDXParams.MARKET_SZ
        qs = api.get_security_quotes([(m, code)])
        if not qs:
            return ""
        q = qs[0]
        prev = float(q.get("last_close") or 0)
        price = float(q.get("price") or 0)
        if prev <= 0 or price <= 0:
            return ""
        limit_up = round(prev * (1 + thr_pct / 100), 2)
        parts = [f"距涨停 {(limit_up / price - 1) * 100:.1f}%"]
        bars = None
        try:
            bars = api.get_security_bars(8, m, code.encode(), 0, 240)
        except Exception:
            bars = None
        if bars:
            d = api.to_df(bars)
            d["date"] = d["datetime"].str[:10]
            t = d[d["date"] == d["date"].iloc[-1]]
            touches, in_touch = 0, False
            for h in (t["high"].astype(float) >= limit_up - 0.001).tolist():
                if h and not in_touch:
                    touches += 1
                in_touch = h
            if touches:
                parts.append(f"今日触板被砸 {touches} 次"
                             + ("(反复炸板,抛压重)" if touches >= 3
                                else "(首次冲板)" if touches == 1 else ""))
        bv, av = int(q.get("bid_vol") or 0), int(q.get("ask_vol") or 0)
        if av > 0:
            parts.append(f"买一/卖一委量 {bv / av:.1f}x")
        elif bv > 0:
            parts.append("卖一无委托(临近封板)")
        return " | ".join(parts)
    except Exception:
        return ""


def ma_block(code: str) -> str:
    """均线形态(池日K缓存, 无外网开销): 多头排列承接强, 空头排列反弹抛压重。"""
    try:
        p = Path.home() / ".tradingagents" / "youzi" / "pool_daily.pkl"
        if not p.exists():
            return ""
        d = pd.read_pickle(p)
        g = d[d["code"] == code].sort_values("date")["close"].astype(float)
        if len(g) < 20:
            return ""
        ma5, ma10, ma20 = g.tail(5).mean(), g.tail(10).mean(), g.tail(20).mean()
        last = float(g.iloc[-1])
        if ma5 > ma10 > ma20 and last > ma5:
            return "均线多头(5>10>20, 价上5日线) — 承接强"
        if ma5 < ma10 < ma20:
            return "均线空头(5<10<20) — 反弹抛压重"
        return "均线纠缠 — 方向未明"
    except Exception:
        return ""


def market_snapshot(date: str) -> dict:
    topics = {}
    t = _tool("get_hot_stocks")
    if t is not None:
        try:
            for line in str(t.invoke({"curr_date": date})).splitlines():
                if ":" in line and "|" in line:
                    w = line.split()
                    if w and w[0].isdigit() and len(w[0]) == 6:
                        topics[w[0]] = line.split("|")[-1].strip()
        except Exception:
            pass
    return {"topics": topics}


def enrich(sig: dict, date: str, api=None, thr_pct: float = 10.0) -> dict:
    """六维证据: 题材(已有) + 概念 + 资金流 + 财务 + 盘中站稳度。失败不阻断。"""
    code = sig["code"]
    info = {}
    # 首触形态最先做(外网调用耗时, pytdx 连接闲置会被服务器踢)
    own = None
    try:
        api2 = api
        if api2 is None:
            from pytdx.hq import TdxHq_API
            api2 = TdxHq_API()
            if not api2.connect("180.153.18.170", 7709, time_out=5):
                api2 = None
        if api2 is not None:
            info["stance"] = first_touch_block(api2, code)
            info["mproxy"] = money_proxy_block(api2, code)   # 同连接, 趁活着
            info["obook"] = orderbook_block(api2, code, thr_pct)
        if own is not None:
            try:
                own.disconnect()
            except Exception:
                pass
    except Exception:
        if own is not None:
            try:
                own.disconnect()
            except Exception:
                pass
    cb = _tool("get_concept_blocks")
    if cb is not None:
        try:
            info["concepts"] = str(cb.invoke({"ticker": code}))[:250]
        except Exception:
            pass
    fund_ok = False
    ff = _tool("get_fund_flow")
    if ff is not None:
        for _try in range(2):
            try:
                r = str(ff.invoke({"ticker": code, "curr_date": date}))[:280]
                if r and "Error" not in r and "失败" not in r:
                    info["fund"] = r
                    fund_ok = True
                    break
            except Exception:
                pass
            time.sleep(1)
    if not fund_ok and info.get("mproxy"):
        info["fund"] = f"[本地代理指标] {info['mproxy']}"
    info["fin"] = finance_block(code)
    info["ma"] = ma_block(code)
    return info


def build_prompt(sigs: list[dict], snap: dict, market: dict,
                 now: datetime, thr: float = 9.0) -> str:
    from collections import Counter
    freq = Counter()
    for _c, tg in (snap.get("topics") or {}).items():
        for tag in str(tg).replace("、", "+").split("+"):
            if tag.strip():
                freq[tag.strip()] += 1
    hot = " | ".join(f"{t}×{n}" for t, n in freq.most_common(12)) or "缺失"

    tm = now.time()
    if tm < dtime(10, 0):
        phase = ("开盘急拉时段(9:30-10:00) — 历史统计: 此间才冲到9%的票"
                 "次日平均 +0.17%/笔(样本中多为一日游)")
    elif tm < dtime(11, 30):
        phase = ("走强确认时段(10:00-11:30) — 历史统计: 此间走强到9%的票"
                 "次日 +1.7~1.9%/笔(样本最优)")
    elif tm < dtime(13, 30):
        phase = "午间/午后初段 — 历史统计: +1.3%/笔"
    elif tm < dtime(14, 0):
        phase = "午后(13:30-14:00) — 历史统计: 中性偏弱"
    else:
        phase = ("尾盘时段(14:00后) — 历史统计: 此间才走强的票次日平均"
                 "+0.02%/笔(样本中多为尾盘偷袭拉升)")
    lines = [f"【当前时间】{now.strftime('%Y-%m-%d %H:%M')} → **{phase}**",
             f"【市场情绪】涨停 {market.get('n_limit', '?')} 只 / "
             f"扫描 {market.get('pool_n', '?')} 只 | 最高连板 "
             f"{market.get('max_st', '?')}",
             f"【今日热门题材】{hot}",
             "", "【候选标的】(均为涨幅≥{t}% 未封板, 盘口有卖单可成交)".format(t=f"{thr:g}"), ""]
    for s in sigs:
        stk = s.get("streak")
        board = "首板候选" if stk == 0 else (f"{stk}板后" if stk else "?")
        mv = f"{s['mv']:.0f}亿" if s.get("mv") else "?"
        tn = f"{s['turn']:.1f}%" if s.get("turn") else "?"
        dd = f"{s['dd']:.0f}%" if s.get("dd") is not None else "?"
        lines.append(
            f"- {s['id']}: +{s['pct']:.1f}% 未封板 | {board} | 题材: "
            f"{str(snap.get('topics', {}).get(s['code'], '无标注'))[:36]}\n"
            f"  市值 {mv} 换手 {tn} 距60日高 {dd} 量比 {s['vr']:.1f} "
            f"额 {s['amt_yi']:.1f}亿\n"
            f"  盘口承接: {s.get('obook') or '缺失'}\n"
            f"  均线: {s.get('ma') or '缺失'}\n"
            f"  基本面: {s.get('fin') or '缺失'}\n"
            f"  资金面: {str(s.get('fund'))[:200] or '缺失'}\n"
            f"  首触形态: {s.get('stance') or '缺失'}")
        lines.append("")
    lines.append("任务: 对每只候选都输出判定(BUY 或 SKIP), 每项都附 judgement 与 reason。")
    return "\n".join(lines)


# 各阈值档的无差别基准(回测, 动态池无前视): thr -> (封板率%, 次日均%)
_BENCH = {"9": (61.7, 1.18), "8": (52.6, 0.79)}


def decide(sigs: list[dict], market: dict, now: datetime | None = None,
           api=None, verbose: bool = True, thr: float = 8.0,
           min_prob: float = 75.0) -> tuple[list[dict], str]:
    """返回 (BUY列表, status)。status: ok=模型正常判定(可能全放弃) / failed=调用失败。"""
    if not sigs:
        return [], "ok"
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    snap = market_snapshot(date)
    own = None
    for i, s in enumerate(sigs):
        s["id"] = chr(ord("A") + i)
        try:
            s.update(enrich(s, date, api, thr_pct=s.get("thr", 10.0)))
        except Exception:
            pass
    if own is not None:
        try:
            own.disconnect()
        except Exception:
            pass
    prompt = build_prompt(sigs, snap, market, now, thr=thr)
    # 校准闭环: 把 AI 自己的实盘成绩单注入提示词(无前视, 每日更新)
    try:
        rpt = (Path.home() / ".tradingagents" / "youzi"
               / "ai_calibration_report.txt")
        if rpt.exists() and rpt.stat().st_size > 50:
            txt = rpt.read_text().strip()
            if txt:
                prompt += ("\n\n【你的历史判断成绩单(实盘校准, 每日更新) — "
                           "据此校准你的 prob 标尺】\n" + txt[:800])
    except Exception:
        pass
    _t0 = time.time()
    if verbose:
        n = len(sigs)
        print(f"[AI] 六维证据已构建({n}只候选/{len(prompt)}字符), 调用模型 ...",
              flush=True)
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        seal, avg = _BENCH.get("9" if thr >= 8.5 else "8")
        # 用 replace 而非 format: SYSTEM 内含 JSON 示例的 {} 会被 format 误当占位符
        sys_txt = (SYSTEM.replace("{THR}", f"{thr:g}")
                   .replace("{SEAL}", str(seal)).replace("{AVG}", str(avg))
                   .replace("{MINP}", f"{min_prob:g}"))
        llm = _client().get_llm()
        resp = llm.invoke([SystemMessage(content=sys_txt),
                           HumanMessage(content=prompt)])
        text = getattr(resp, "content", None) or str(resp)
        if verbose:
            print(f"[AI] 模型返回 {time.time()-_t0:.1f}s", flush=True)
    except Exception as exc:
        print(f"[AI] 调用失败({type(exc).__name__}: {str(exc)[:100]}) "
              f"({time.time()-_t0:.1f}s)")
        return [], "failed"
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        print(f"[AI] 原始输出: {text[:300]}")
        return [], "failed"
    id2code = {s.get("id", ""): s["code"] for s in sigs}
    try:
        data = json.loads(m.group(0))
        if os.getenv("YOUZI_AI_DEBUG"):
            print(f"[AI] 返回 {len(data)} 项 id={[str(d.get('id','?')) for d in data]} "
                  f"期望 {list(id2code)}")
        out = []
        for d in data:
            cid = id2code.get(str(d.get("id", "")).upper(), "")
            if not cid:
                continue
            out.append({
                "code": cid,
                "action": str(d.get("action", "SKIP")).upper(),
                "prob": float(d.get("prob", d.get("score", 0)) or 0),
                "reason": str(d.get("reason", ""))[:120],
                "judgement": d.get("judgement", {}),
            })
        return out, "ok"
    except Exception as exc:
        print(f"[AI] JSON 解析失败: {exc} | {text[:200]}")
        return [], "failed"


def main() -> int:
    demo = [{"code": "000001", "name": "平安银行", "price": 12.5, "pct": 9.1,
             "thr": 10.0, "ask1": 12.51, "vr": 2.3, "amt_yi": 25.0,
             "streak": 0, "mv": 2400, "turn": 3.2, "dd": 8.0, "n20": 1}]
    r, stt = decide(demo, {"n_limit": 42, "pool_n": 2969, "max_st": 3})
    print(f"\n[AI status={stt}]")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if stt == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
