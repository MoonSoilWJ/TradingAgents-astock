#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T0 跨境ETF轮动: AI 增益回测(选股 / regime 参数 两个维度)

四组对照(同一份历史数据, 同一批调仓日):
  A 基线        动量 Top1(现役规则, 固定 LOOKBACK=30)
  B AI选股      动量 Top5 → AI 选 1
  C AI regime   AI 判环境 → 动态参数(窗口/是否持仓/集中度) + 规则执行
  D 叠加        C 的环境判断 + B 的选股

⚠ 无前视: 每次决策只喂 t 日(含)之前的数据; AI 输出按 date 缓存, 重跑不再计费。
用法: python3 backtest_t0_ai.py [--months 0] [--start 2023-03-01]
"""
import argparse
import json
import os
import sys
from pathlib import Path

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
try:                                  # youzi_live 才会加载, 回测脚本需自己来
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

POOL_F = Path.home() / ".tradingagents" / "cache" / "t0_daily" / "pool_3y.json"
NAME_F = HERE / "auto_t0_etfs.json"
CACHE = Path.home() / ".tradingagents" / "youzi" / "t0_ai_backtest_cache.jsonl"
OUT = Path.home() / ".tradingagents" / "youzi" / "t0_ai_backtest.json"

COST = 0.001          # 每次调仓双边成本(ETF 佣金+冲击)


# ────────────────────────────── 数据 ──────────────────────────────
def load_data():
    raw = json.loads(POOL_F.read_text())["etf_daily"]
    names = {}
    if NAME_F.exists():
        names = {x["code"]: x["name"] for x in json.loads(NAME_F.read_text())}
    px = {}
    for code, v in raw.items():
        rows = v.get("returns") or []
        if len(rows) < 80:
            continue
        d = pd.DataFrame(rows)
        d = d[d["date"] >= "2023-01-01"]
        if len(d) < 80:
            continue
        s = pd.Series(d["close"].astype(float).values,
                      index=pd.Index(d["date"].values, name="date"))
        px[code] = s[~s.index.duplicated()].sort_index()
    df = pd.DataFrame(px).sort_index()
    df = df.dropna(axis=1, thresh=int(len(df) * 0.9))     # 剔除上市太晚的
    return df, names


def mom(df: pd.DataFrame, date: str, win: int) -> pd.Series:
    """截至 date(含) 的 win 日动量(%); 逐只用自身有效数据算(早期 NaN 不传染)。"""
    sub = df.loc[:date]
    if len(sub) < 5:
        return pd.Series(dtype=float)
    out = {}
    for c in df.columns:
        s = sub[c].dropna()
        if len(s) >= win + 1 and s.iloc[-(win + 1)] > 0:
            out[c] = float(s.iloc[-1] / s.iloc[-(win + 1)] - 1) * 100
    return pd.Series(out, dtype=float)


def feats(df: pd.DataFrame, code: str, date: str) -> dict | None:
    sub = df.loc[:date, code].dropna()
    if len(sub) < 31:          # 至少够算 m30; m60 不足时记 0 而非丢弃样本
        return None
    r = sub.pct_change().dropna()
    return {
        "m10": float((sub.iloc[-1] / sub.iloc[-11] - 1) * 100) if len(sub) > 11 else 0,
        "m30": float((sub.iloc[-1] / sub.iloc[-31] - 1) * 100) if len(sub) > 31 else 0,
        "m60": float((sub.iloc[-1] / sub.iloc[-61] - 1) * 100) if len(sub) > 61 else 0,
        "vol": float(r.tail(60).std() * np.sqrt(244) * 100) if len(r) > 20 else 0,
        "dd": float((sub.tail(30) / sub.tail(30).cummax() - 1).min() * 100),
    }


def rebal_dates(df: pd.DataFrame, start: str) -> list[str]:
    """每月第一个交易日。"""
    ds = [d for d in df.index if d >= start]
    out, cur = [], None
    for d in ds:
        if d[:7] != cur:
            out.append(d)
            cur = d[:7]
    return out


# ────────────────────────────── LLM ──────────────────────────────
def llm_client():
    from tradingagents.llm_clients.factory import create_llm_client
    provider = (os.getenv("YOUZI_LLM_PROVIDER") or "qwen").strip()
    model = (os.getenv("YOUZI_LLM_MODEL") or "qwen3-max").strip()
    # 注意: create_llm_client 返回包装器, 真正能 invoke 的是 .get_llm()
    return create_llm_client(provider=provider, model=model,
                             temperature=0).get_llm()


def ask(llm, sys_txt: str, prompt: str) -> str:
    from langchain_core.messages import SystemMessage, HumanMessage
    try:
        r = llm.invoke([SystemMessage(content=sys_txt),
                        HumanMessage(content=prompt)])
        return getattr(r, "content", None) or str(r)
    except Exception as e:
        print(f"    [LLM失败] {type(e).__name__}", flush=True)
        return ""


def jget(txt: str) -> dict:
    txt = (txt or "").strip()
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        return json.loads(txt[i:j + 1])
    except Exception:
        return {}


def cache_load() -> dict:
    c = {}
    if CACHE.exists():
        for line in CACHE.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
                c[(o["date"], o["kind"])] = o
            except Exception:
                pass
    return c


def cache_save(o: dict):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE, "a", encoding="utf-8") as f:
        f.write(json.dumps(o, ensure_ascii=False) + "\n")


# ──────────────────────────── AI 决策 ────────────────────────────
SYS_PICK = """你是跨境ETF轮动策略的选股决策者。每月从候选中选1只, 持有到下次调仓。
只能依据给出的历史量价特征与你对当时宏观环境的理解判断, 不得假设你知道未来。
输出严格JSON: {"pick":"6位代码","confidence":1-10,"reason":"40字内"}"""

SYS_REG = """你是跨境ETF轮动策略的仓位与参数决策者。
根据候选池整体状态判断市场环境, 并给出本月该用的参数。
输出严格JSON: {"regime":"TREND|CHOP|RISK_OFF","lookback":10|30|60,
"hold":true|false,"topn":1|3,"reason":"30字内"}"""


def ai_pick(llm, cache, date, cands: list[dict]) -> dict:
    k = (date, "pick")
    if k in cache:
        return cache[k]
    lines = []
    for i, c in enumerate(cands, 1):
        lines.append(
            f"{i}. {c['name']}({c['code']}): 近30日 {c['m30']:+.1f}% | "
            f"近10日 {c['m10']:+.1f}% | 近60日 {c['m60']:+.1f}% | "
            f"年化波动 {c['vol']:.0f}% | 近30日最大回撤 {c['dd']:.1f}%")
    prompt = (f"调仓日 {date}\n候选(按近30日动量排序Top5):\n"
              + "\n".join(lines)
              + "\n\n判断: 动量质量(平滑趋势/加速 vs 单日脉冲)、波动与回撤、"
                "拥挤反转风险、标的资产的宏观环境。\n选1只并给出理由。")
    o = jget(ask(llm, SYS_PICK, prompt))
    rec = {"date": date, "kind": "pick", "pick": str(o.get("pick", "")),
           "confidence": o.get("confidence"), "reason": o.get("reason", "")}
    cache_save(rec)
    cache[k] = rec
    return rec


def ai_regime(llm, cache, date, mkt: dict) -> dict:
    k = (date, "reg")
    if k in cache:
        return cache[k]
    prompt = (f"调仓日 {date}\n候选池({mkt['n']}只跨境ETF)整体状态:\n"
              f"近30日平均 {mkt['avg30']:+.1f}% | 上涨占比 {mkt['up']:.0f}% | "
              f"平均年化波动 {mkt['vol']:.0f}% | 最强动量 {mkt['top']:+.1f}% | "
              f"近60日平均 {mkt['avg60']:+.1f}%\n\n"
              "判断环境并给参数: TREND=趋势明确动量易延续; "
              "CHOP=震荡动量易反转; RISK_OFF=避险应空仓。")
    o = jget(ask(llm, SYS_REG, prompt))
    rec = {"date": date, "kind": "reg",
           "regime": str(o.get("regime", "TREND")).upper(),
           "lookback": int(o.get("lookback", 30) or 30),
           "hold": bool(o.get("hold", True)),
           "topn": int(o.get("topn", 1) or 1),
           "reason": o.get("reason", "")}
    cache_save(rec)
    cache[k] = rec
    return rec


# ────────────────────────────── 回测 ──────────────────────────────
def run(months: int = 0, start: str = "2023-03-01"):
    df, names = load_data()
    dates = rebal_dates(df, start)
    if months:
        dates = dates[:months]
    print(f"数据: {len(df.columns)} 只 ETF | {df.index[0]} ~ {df.index[-1]}")
    print(f"调仓日: {len(dates)} 个 ({dates[0]} ~ {dates[-1]})\n")

    llm = llm_client()
    cache = cache_load()
    rows = []

    for i, d in enumerate(dates[:-1]):
        nxt = dates[i + 1]
        m30 = mom(df, d, 30)
        if m30.empty:
            continue
        top5 = m30.dropna().sort_values(ascending=False).head(5)
        if len(top5) == 0:
            continue
        a = [top5.index[0]]

        # ── 候选特征(B 用)
        cands = []
        for c in top5.index:
            f = feats(df, c, d)
            if f:
                f.update({"code": c, "name": names.get(c, c)})
                cands.append(f)
        if not cands:
            continue

        # ── B: AI 选股
        pb = ai_pick(llm, cache, d, cands)
        b = [pb["pick"]] if pb.get("pick") in df.columns else a

        # ── C: AI regime → 参数
        sub30 = m30.dropna()
        avg60 = mom(df, d, 60).dropna().mean() if len(df.loc[:d]) > 61 else 0
        mkt = {"n": len(sub30), "avg30": float(sub30.mean()),
               "up": float((sub30 > 0).mean() * 100),
               "vol": float(np.mean([c["vol"] for c in cands])),
               "top": float(sub30.max()), "avg60": float(avg60)}
        rc = ai_regime(llm, cache, d, mkt)
        if not rc.get("hold"):
            c_pick = []
        else:
            lb = rc.get("lookback", 30)
            m = mom(df, d, lb).dropna().sort_values(ascending=False)
            c_pick = list(m.head(max(rc.get("topn", 1), 1)).index)

        # ── D: C 的仓位/集中度 + B 的选股
        if not rc.get("hold"):
            d_pick = []
        elif rc.get("topn", 1) >= 3:
            d_pick = c_pick
        else:
            d_pick = b

        def ret_of(codes: list[str]) -> float:
            if not codes:
                return 0.0            # 空仓
            rs = []
            for c in codes:
                try:
                    p0 = df.loc[:d, c].dropna().iloc[-1]
                    p1 = df.loc[:nxt, c].dropna().iloc[-1]
                    rs.append(p1 / p0 - 1)
                except Exception:
                    pass
            if not rs:
                return 0.0
            return float(np.mean(rs)) - COST

        rows.append({
            "date": d, "nxt": nxt,
            "A": ret_of(a), "B": ret_of(b), "C": ret_of(c_pick),
            "D": ret_of(d_pick),
            "a_pick": a[0], "b_pick": b[0],
            "c_pick": ",".join(c_pick) or "空仓",
            "regime": rc.get("regime", ""), "lb": rc.get("lookback", 30),
            "topn": rc.get("topn", 1), "hold": rc.get("hold", True),
        })
        print(f"  [{i+1}/{len(dates)-1}] {d} A={rows[-1]['A']*100:+.1f}% "
              f"B={rows[-1]['B']*100:+.1f}% C={rows[-1]['C']*100:+.1f}%"
              f"({rc.get('regime','')}) D={rows[-1]['D']*100:+.1f}%",
              flush=True)

    return pd.DataFrame(rows), df


def stats(r: pd.DataFrame) -> str:
    out = []
    out.append(f"{'组':<22}{'年化':>8}{'累计':>9}{'夏普':>7}{'最大回撤':>9}"
               f"{'胜率':>7}{'笔数':>6}")
    out.append("-" * 68)
    n_y = len(r) / 12
    for key, name in (("A", "A 基线 动量Top1"), ("B", "B AI选股"),
                      ("C", "C AI regime参数"), ("D", "D 叠加")):
        s = r[key].values
        eq = np.cumprod(1 + s)
        tot = eq[-1] - 1
        cagr = (eq[-1] ** (1 / n_y) - 1) if n_y > 0 else 0
        sharpe = (s.mean() / s.std() * np.sqrt(12)) if s.std() > 0 else 0
        dd = ((eq / np.maximum.accumulate(eq)) - 1).min()
        out.append(f"{name:<22}{cagr*100:>+7.1f}%{tot*100:>+8.1f}%"
                   f"{sharpe:>7.2f}{dd*100:>8.1f}%"
                   f"{(s>0).mean()*100:>6.0f}%{len(s):>6}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=0, help="只跑前N个月(0=全部)")
    ap.add_argument("--start", default="2023-03-01")
    a = ap.parse_args()

    r, _ = run(a.months, a.start)
    if r.empty:
        print("无结果")
        return
    print("\n" + "=" * 68)
    print(stats(r))
    print("=" * 68)
    # regime 分布
    if "regime" in r:
        print("\nAI 环境判断分布:")
        for k, v in r["regime"].value_counts().items():
            sub = r[r["regime"] == k]
            print(f"  {k:<10} {v:>3} 次 | 该环境下 C 组平均 "
                  f"{sub['C'].mean()*100:+.2f}%/月 | "
                  f"同期 A 组 {sub['A'].mean()*100:+.2f}%")
    r.to_json(OUT, orient="records", force_ascii=False)
    print(f"\n明细: {OUT}")


if __name__ == "__main__":
    main()
