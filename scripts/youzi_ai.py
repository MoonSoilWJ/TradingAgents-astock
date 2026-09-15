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
# 同 youzi_live: 绕开 macOS 系统代理, 否则 LLM/东财等 HTTPS 调用 ProxyError
import os as _os
_os.environ["no_proxy"] = _os.environ["NO_PROXY"] = "*"
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, time as dtime, timedelta
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
2. 资金面: 主力/超大单净流入方向与持续性; 超大单主导买入 vs 中小单为主;
   龙虎榜: 近期上榜净买/净卖方向与机构席位动向(机构净卖出+游资接力
   = 聪明钱出逃, 重大负证据)
3. 题材联动: 主线还是杂毛? 同题材当日几只涨停(有合力 vs 孤板)? 龙头还是末位跟风?
4. 基本面: 涨停原因(业绩/政策/概念/无)、盈亏状况、负债水平;
   公司公告: 异常波动/风险提示/澄清公告 — 公司亲自否认核心炒作逻辑
   (如"相关业务未产生营收") = 炒作根基被自己打脸, 重大负证据
5. 首触形态与时段: 【当前时间】附有各时段历史统计均值 — 它描述平均规律,
   不代表当下这只票, 结合个股证据自行权衡其权重
6. 位置与情绪: 连板高度与梯队、流通市值、均线形态、距60日高、大盘涨停家数;
   高位减分项(RSI>80 / 5日涨幅>15% / 10日涨幅>25%) = 超买+乖离红灯,
   触发越多, 炸板与次日兑现压力越大, prob 应显著下调

【数据缺失】某维度缺失时降低整体置信度, 聚焦可得的维度判断, 不因此单独否决。

【输出】严格 JSON, 对【每只候选】都给出判定(BUY 或 SKIP):
[{"id":"A","action":"BUY","prob":80,
  "scores":{"盘口":7,"资金":8,"题材":3,"基本面":5,"首触":8,"位置":6},
  "judgement":{"盘口":"...","资金":"...","题材":"...","基本面":"...","首触":"...","风险":"..."},
  "reason":"40字内结论"}]
scores = 六个维度的子分(各 1-10 分, 按你对该维度证据的评估打分)。
prob = 你对"今天封住板且明日有溢价"的真实概率估计(0-100), 是六维子分的综合。
【prob 与子分必须自洽】维度间可以有主次与交互 — 某维度极强/极差可以主导判断,
这是操盘直觉, 优于机械加权平均; 但组合必须能自圆其说:
全部子分≥7 时 prob 不应低于 65; 多数子分≤4 时 prob 不应高于 55;
若你认定某维度 decisive(如盘口承接极差), 允许它压低整体, 但要在 judgement 里写明。
【prob≥75 的门槛条件 — 实盘校准 2026-09-14】
prob≥75 是系统放行线, 必须由【多维度共振】支撑, 不可被单一维度抬高:
- 若"题材"子分≤4(孤板/无联动): prob 封顶 72, 无论资金多强;
- 若"盘口"子分≤4(反复炸板/委量比<1): prob 封顶 70;
- 资金单项强(≥8)只是必要条件之一, 不是充分条件 — 过热资金(换手异常高)
  反而是抛压前兆;
- 只有"盘口≥6 且 资金≥6 且 题材≥5"三者同时成立, prob 才允许超过 75。
此规则来自实盘复盘: 三天 19 笔推送中, 单靠资金高分(82分)的票全部大亏
(北自科技 −11.6%/浙江新能未封), 而多维度均衡的 76 分票反而封板。
每次判断都会被记录, 收盘后与实际结果(封板/次日溢价)比对, 且子分会被单独校准 —
哪个维度打分与结果相关性高, 哪个维度就会被采信; 给分必须经得起复盘。
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


# ── 公告/龙虎榜风险证据(2026-09-15 超声电子教训: 公司两次澄清否认核心炒作
#    逻辑 + 机构龙虎榜出货 1.68亿, 系统当时完全失明 → 接入三类负证据输入) ──
RISK_KW = ("异常波动", "风险提示", "澄清", "否认", "不实", "减持", "立案",
           "警示", "问询", "处罚", "诉讼", "仲裁", "质押")
_RISK_CACHE: dict = {"day": None, "notice": {}, "lhb": None, "inst": None}


def _risk_tables() -> dict:
    """全市场公告(近8自然日) + 龙虎榜(近14日) + 机构席位(近30日), 每日拉一次。

    盘中逐候选拉外部接口会拖垮节奏且限频 → 每日首轮一次性建内存索引,
    之后同日全部查缓存。任一表失败置 None/空不阻断(enrich 兜底)。
    首轮一次约 10 个请求(~15s), 仅发生在当日第一次有候选进 AI 层时。
    """
    day = datetime.now().strftime("%Y-%m-%d")
    if _RISK_CACHE["day"] == day:
        return _RISK_CACHE
    saved = {k: os.environ.pop(k) for k in list(os.environ)
             if "proxy" in k.lower()}
    notice: dict[str, list] = {}
    lhb = inst = None
    try:
        import akshare as ak
        # 公告: 东财按日全市场 → 按代码索引(周末/节假日返回空, 跳过)
        for off in range(8):
            d = (datetime.now() - timedelta(days=off)).strftime("%Y%m%d")
            try:
                df = ak.stock_notice_report(symbol="全部", date=d)
            except Exception:
                continue
            if df is None or not len(df):
                continue
            for _, r in df.iterrows():
                c = str(r.get("代码") or "").zfill(6)
                if c:
                    notice.setdefault(c, []).append((
                        str(r.get("公告日期") or "")[:10],
                        str(r.get("公告标题") or ""),
                        str(r.get("公告类型") or "")))
        end = datetime.now().strftime("%Y%m%d")
        start14 = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
        start30 = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        try:
            lhb = ak.stock_lhb_detail_em(start_date=start14, end_date=end)
        except Exception:
            lhb = None
        try:
            inst = ak.stock_lhb_jgmmtj_em(start_date=start30, end_date=end)
        except Exception:
            inst = None
    except Exception:
        pass
    finally:
        os.environ.update(saved)
    _RISK_CACHE.update({"day": day, "notice": notice, "lhb": lhb,
                        "inst": inst})
    return _RISK_CACHE


def notice_block(code: str) -> str:
    """公告/异动风险: 近7日公告命中风险关键词 → 列出。
    公司否认核心炒作逻辑(如"未产生相关营收")是复盘中最致命的负证据。"""
    try:
        rows = _risk_tables()["notice"].get(str(code).zfill(6)) or []
        if not rows:
            return ""
        hits = [f"{d[5:]} {t[:36]}" for d, t, _ty in rows
                if any(k in t for k in RISK_KW)]
        if hits:
            return f"⚠ 风险公告{len(hits)}条: " + " | ".join(hits[:3])
        return f"近7日公告{len(rows)}条, 无风险关键词"
    except Exception:
        return ""


def lhb_block(code: str) -> str:
    """龙虎榜方向: 近14日上榜+净买额, 近30日机构席位净买卖。
    机构净卖出+游资接力 = 聪明钱出逃的结构性负证据。"""
    try:
        t = _risk_tables()
        code6 = str(code).zfill(6)
        parts = []
        d = t.get("lhb")
        if d is not None and len(d):
            sub = d[d["代码"].astype(str).str.zfill(6) == code6]
            if len(sub):
                sub = sub.sort_values("上榜日")
                last = sub.iloc[-1]
                seg = f"近14日上榜{len(sub)}次(最近{last['上榜日']})"
                try:
                    net = float(last["龙虎榜净买额"]) / 1e8
                    seg += f" 净买{net:+.2f}亿"
                except Exception:
                    pass
                parts.append(seg)
        i = t.get("inst")
        if i is not None and len(i):
            sub = i[i["代码"].astype(str).str.zfill(6) == code6]
            if len(sub):
                g = sub.groupby("上榜日期", as_index=False).last()
                net = float(pd.to_numeric(g["机构买入净额"],
                                          errors="coerce").sum()) / 1e8
                nsell = int(g["卖方机构数"].max())
                parts.append(f"机构近30日净{'买' if net >= 0 else '卖'}"
                             f"{abs(net):.2f}亿(单日卖方机构最多{nsell}家)")
        return " | ".join(parts) if parts else ""
    except Exception:
        return ""


def _live_pct(code: str) -> float | None:
    """实时快照涨幅(小数, 如 0.0929)。10:00 前的首触判定只能用它。"""
    try:
        from tx_quote import snapshot as tx_snap
        v = (tx_snap([code]).get(code) or {})
        prev = float(v.get("prev_close") or 0)
        px = float(v.get("price") or 0)
        if prev > 0 and px > 0:
            return px / prev - 1
    except Exception:
        pass
    return None


def first_touch_block(api, code: str, now: datetime | None = None,
                      cur_pct: float | None = None) -> str:
    """首触形态: 10:00 时是否已在 9% 上方。

    回测(609笔,【A】表): 开盘半小时内已冲到 9% 的票次日 +0.17% (差),
    10:00-10:30 间走强到 9% 的 +1.66% (好) — "早站稳"反而弱。

    ⚠ 时点保护(2026-09-11 605188 事故): 10:00 之前, "10:00 时点涨幅"尚未
    产生。原实现拿最新一根分时顶替, 而分时比实时快照慢一拍 → 正在急速冲板
    的票被判成"10:00 后走强(优组)"并拿到高首触分, 实际它 1 分钟后就触板,
    属历史最差的"开盘急拉"组。现在 10:00 前改为:
      · 当前已 ≥9%  → 差组(已发生, 确凿)
      · 当前 <9%    → 未定, 明确标注不可按优组计
    """
    now = now or datetime.now()
    if now.time() < dtime(10, 0):
        pct = cur_pct if cur_pct is not None else _live_pct(code)
        if pct is None:
            return ""
        if pct >= 0.09:
            return "开盘半小时内已冲至9% (历史表现差组)"
        return (f"未到10:00, 当前仅{pct * 100:.1f}% — 首触形态未定"
                f"(尚未冲上9%, 不可按'10:00后走强'优组计)")
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
        if not bars:                       # TDX 失效 → 腾讯分时兜底
            try:
                from tx_quote import minute_bars as tx_min
                from tx_quote import snapshot as tx_snap
                dd = tx_min(code)
                prev_c = float((tx_snap([code]).get(code) or {})
                               .get("prev_close") or 0)
                if dd is not None and len(dd) and prev_c > 0:
                    row = dd[dd["datetime"] <= "1000"]
                    if len(row) == 0:
                        return ""      # 已过10:00却无10:00前分时 → 缺失, 不猜
                    p10 = float(row["price"].iloc[-1]) / prev_c - 1
                    return ("开盘半小时内已冲至9% (历史表现差组)"
                            if p10 >= 0.09 else "10:00后走强至9% (历史表现优组)")
            except Exception:
                pass
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
        if not bars:                       # TDX 失效 → 腾讯分时兜底
            try:
                from tx_quote import minute_bars as tx_min
                from tx_quote import snapshot as tx_snap
                dd = tx_min(code)
                v = tx_snap([code]).get(code) or {}
                if dd is None or len(dd) < 3:
                    return ""
                hi, lo = float(dd["price"].max()), float(dd["price"].min())
                close = float(dd["price"].iloc[-1])
                pos = (close - lo) / (hi - lo) if hi > lo else 0.5
                vol = dd["vol"].astype(float)
                vr30 = float(vol.tail(30).sum()) / max(
                    float(vol.tail(60).head(30).sum()), 1e-9)
                vwap = float(v.get("vwap") or 0)
                vs = close / vwap - 1 if vwap > 0 else 0
                return (f"收盘位置 {pos*100:.0f}% | 近30分量能 {vr30:.1f}x"
                        f" | 相对均价 {vs*100:+.1f}%")
            except Exception:
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
        q = None
        try:
            if api is not None:
                qs = api.get_security_quotes([(m, code)])
                q = qs[0] if qs else None
        except Exception:
            q = None
        if q is None:                      # TDX 失效 → 腾讯快照
            try:
                from tx_quote import snapshot as tx_snap
                v = tx_snap([code]).get(code)
            except Exception:
                v = None
            if not v:
                return ""
            q = {"price": v["price"], "last_close": v["prev_close"],
                 "ask_vol": v["ask1_vol"], "bid_vol": v["bid1_vol"],
                 "outer": v.get("outer"), "inner": v.get("inner")}
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
        highs = None
        if bars:
            d = api.to_df(bars)
            d["date"] = d["datetime"].str[:10]
            t = d[d["date"] == d["date"].iloc[-1]]
            highs = t["high"].astype(float).tolist()
        else:                              # 腾讯分时兜底(1分钟采样)
            try:
                from tx_quote import minute_bars as tx_min
                dd = tx_min(code)
                if dd is not None and len(dd):
                    highs = dd["price"].astype(float).tolist()
            except Exception:
                highs = None
        if highs:
            touches, in_touch = 0, False
            for h in highs:
                hit = h >= limit_up - 0.001
                if hit and not in_touch:
                    touches += 1
                in_touch = hit
            if touches:
                parts.append(f"今日触板被砸 {touches} 次"
                             + ("(反复炸板,抛压重)" if touches >= 3
                                else "(首次冲板)" if touches == 1 else ""))
        bv, av = int(q.get("bid_vol") or 0), int(q.get("ask_vol") or 0)
        if av > 0:
            parts.append(f"买一/卖一委量 {bv / av:.1f}x")
        elif bv > 0:
            parts.append("卖一无委托(临近封板)")
        # 内外盘比: 外盘=主动买成交, 内盘=主动卖成交(外盘>内盘=买方主动)
        ob = q.get("active_buy") or q.get("outer")
        ib = q.get("active_sell") or q.get("inner")
        if ob and ib and float(ob) > 0 and float(ib) > 0:
            ratio = float(ob) / float(ib)
            parts.append(f"外盘/内盘 {ratio:.2f}"
                         + ("(买方主动)" if ratio > 1.2
                            else "(卖方主动)" if ratio < 0.8 else ""))
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


def enrich(sig: dict, date: str, api=None, thr_pct: float = 10.0,
           now: datetime | None = None) -> dict:
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
            # cur_pct: 扫描时的实时涨幅(百分数→小数)。10:00 前首触判定必须
            # 用它, 不能用慢一拍的分时(见 first_touch_block 时点保护说明)
            info["stance"] = first_touch_block(
                api2, code, now=now,
                cur_pct=(float(sig.get("pct") or 0) / 100.0
                         if sig.get("pct") is not None else None))
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
    try:                            # 公告/异动 + 龙虎榜方向(每日一次索引)
        info["notice"] = notice_block(code)
        info["lhb"] = lhb_block(code)
    except Exception:
        pass
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
             f"【今日热门题材】{hot}"]
    emo = str(market.get("emotion") or "")
    if emo:
        lines.append(f"【接力赚钱效应】{emo} — 这是封板次日溢价最直接的温度计: "
                     "冰点时封板次日普遍低开, 修复时高开; 权衡权重由你定")
    lines += ["", "【候选标的】(均为涨幅≥{t}% 未封板, 盘口有卖单可成交)".format(t=f"{thr:g}"), ""]
    for s in sigs:
        stk = s.get("streak")
        board = "首板候选" if stk == 0 else (f"{stk}板后" if stk else "?")
        mv = f"{s['mv']:.0f}亿" if s.get("mv") else "?"
        tn = f"{s['turn']:.1f}%" if s.get("turn") else "?"
        dd = f"{s['dd']:.0f}%" if s.get("dd") is not None else "?"
        vr_txt = (f"{(s.get('vr') or 0):.1f}(早盘折算失真, 不采信)"
                  if s.get("vr_na") else f"{(s.get('vr') or 0):.1f}")
        lines.append(
            f"- {s['id']}: +{s['pct']:.1f}% 未封板 | {board} | 题材: "
            f"{str(snap.get('topics', {}).get(s['code'], '无标注'))[:36]}\n"
            f"  市值 {mv} 换手 {tn} 距60日高 {dd} 量比 {vr_txt} "
            f"额 {s['amt_yi']:.1f}亿\n"
            f"  盘口承接: {s.get('obook') or '缺失'}\n"
            f"  均线: {s.get('ma') or '缺失'}\n"
            f"  基本面: {s.get('fin') or '缺失'}\n"
            f"  资金面: {str(s.get('fund'))[:200] or '缺失'}\n"
            f"  首触形态: {s.get('stance') or '缺失'}\n"
            f"  公告异动: {s.get('notice') or '缺失'}\n"
            f"  龙虎榜: {s.get('lhb') or '近两周未上榜(或数据缺失)'}")
        rsi, p5, p10 = s.get("rsi"), s.get("pct5"), s.get("pct10")
        ded = []
        if rsi is not None and rsi > 80:
            ded.append(f"RSI14={rsi:.0f}>80")
        if p5 is not None and p5 > 15:
            ded.append(f"5日涨{p5:+.0f}%>15%")
        if p10 is not None and p10 > 25:
            ded.append(f"10日涨{p10:+.0f}%>25%")
        lines.append("  高位减分: " + ("⚠ " + "、".join(ded)
                     + " — 超买+乖离极端, 炸板/兑现压力大"
                       if ded else "无"))
        lines.append("")
    lines.append("任务: 对每只候选都输出判定(BUY 或 SKIP), 每项都附 judgement 与 reason。")
    return "\n".join(lines)


# 各阈值档的无差别基准(回测, 动态池无前视): thr -> (封板率%, 次日均%)
_BENCH = {"9": (61.7, 1.18), "8": (52.6, 0.79)}


def decide(sigs: list[dict], market: dict, now: datetime | None = None,
           api=None, verbose: bool = True, thr: float = 8.0,
           min_prob: float = 75.0,
           report: str | None = None,
           evidence: dict | None = None) -> tuple[list[dict], str]:
    """返回 (BUY列表, status)。status: ok=模型正常判定(可能全放弃) / failed=调用失败。"""
    if not sigs:
        return [], "ok"
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    snap = market_snapshot(date)
    own = None
    for i, s in enumerate(sigs):
        s["id"] = chr(ord("A") + i)
        # evidence: 回测用 — 由历史数据预置证据(不调实时接口, 否则前视)
        if evidence and s["code"] in evidence:
            s.update(evidence[s["code"]])
        else:
            try:
                s.update(enrich(s, date, api, thr_pct=s.get("thr", 10.0),
                                now=now))
            except Exception:
                pass
    if own is not None:
        try:
            own.disconnect()
        except Exception:
            pass
    prompt = build_prompt(sigs, snap, market, now, thr=thr)
    # 校准闭环: 成绩单注入(只含 T 日之前的判定 — 实盘与回测同源, 非前视)
    #   report=None → 读实盘成绩单文件
    #   report=文本 → 回测用: 传"截至该回测日"滚动生成的成绩单
    #   report=""   → 不注入(对照实验)
    if report is None and not os.getenv("YOUZI_AI_NO_REPORT"):
        try:
            rpt = (Path.home() / ".tradingagents" / "youzi"
                   / "ai_calibration_report.txt")
            report = (rpt.read_text().strip()
                      if rpt.exists() and rpt.stat().st_size > 50 else "")
        except Exception:
            report = ""
    if report:
        prompt += ("\n\n【你截至当前的历史判断成绩单 — 据此校准你的 prob 标尺】\n"
                   + report[:800])
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
            prob = float(d.get("prob", d.get("score", 0)) or 0)
            sc = d.get("scores", {}) or {}
            # ── P1 封顶的代码强制(2026-09-15): 提示词规则 LLM 会概率性违反
            #    (骏亚科技单日 4 次 76 分违反题材≤4→封顶72), 放行层机械执行。
            #    子分仍是 AI 打的(判断权在 AI), 此处只执行已校准的封顶规则,
            #    性质与 min_prob 门槛相同 ──
            cap_note = []
            try:
                if float(sc.get("题材", 10)) <= 4:
                    prob = min(prob, 72.0)
                    cap_note.append("题材≤4封顶72")
                if float(sc.get("盘口", 10)) <= 4:
                    prob = min(prob, 70.0)
                    cap_note.append("盘口≤4封顶70")
            except Exception:
                pass
            out.append({
                "code": cid,
                "action": str(d.get("action", "SKIP")).upper(),
                "prob": prob,
                "capped": cap_note,                  # 被封顶记录(校准用)
                "reason": str(d.get("reason", ""))[:120],
                "judgement": d.get("judgement", {}),
                "scores": sc,                        # 维度子分(校准用)
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
