#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""科创50 N12 + 防御组轮动: AI 介入买卖判断的增益回测

组:
  A0 原版          N12 投票决定 588000 买/空, 空仓期防御组轮动(基线)
  A2 AI否决制      N12 喊买时, AI 复核; 否决则改持国债
  A3 AI全权(切换点) 每次 N12 信号切换时由 AI 决定"是否持 588000"

AI 只在信号切换点调用(一年约 32 次) → 成本极低; 结果按 date 缓存。
⚠ 无前视: 只喂当日(含)之前的数据。
用法: python3 backtest_kc50_ai.py [--start 2024-01-02] [--ai/--no-ai]
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
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from backtest_588000_n12 import (COMB_N12, SLIP, BOND, DEF, NAMES,
                                 trix_series, vote_from, trix_cross,
                                 _momentum)

CACHE = Path.home() / ".tradingagents" / "youzi" / "kc50_ai_cache.jsonl"
OUT = Path.home() / ".tradingagents" / "youzi" / "kc50_ai_backtest.json"
EXPAND = (Path.home() / ".tradingagents" / "cache" / "etf_full"
          / "etf_daily_qfq_expand.json")
NEED = ["588000"] + DEF          # 588000 + 511260 518880 510880 515080 512890


# ────────────────────────────── 数据 ──────────────────────────────
def _tx_daily(code: str) -> pd.Series | None:
    """腾讯日线(补全本地缓存缺失的标的, 如 511260 国债ETF)。"""
    import requests
    pre = "sh" if code[0] in "569" else "sz"
    try:
        r = requests.get(
            f"https://web.ifzq.gtimg.cn/appstock/app/day/query?code={pre}{code}",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
            proxies={"http": None, "https": None}).json()
        # 结构: data[code]['data'] = [{'date':'20260911','data':['日期 开 收 高 低 量..']}]
        rows = r["data"][f"{pre}{code}"].get("data") or []
        out = {}
        for x in rows:
            if not isinstance(x, dict):
                continue
            dt = str(x.get("date", ""))
            parts = x.get("data") or []
            if len(dt) != 8 or not parts:
                continue
            f = str(parts[0]).split()
            if len(f) < 3:
                continue
            out[f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"] = float(f[2])   # 收盘
        if len(out) < 200:
            return None
        return pd.Series(out).sort_index()
    except Exception:
        return None


def load(start: str) -> pd.DataFrame:
    cols = {}
    # 1) etf_daily.json(按代码, 覆盖防御组多数标的)
    p2 = EXPAND.parent / "etf_daily.json"
    if p2.exists():
        for c, rows in json.loads(p2.read_text()).items():
            if c in NEED and len(rows) > 200:
                cols[c] = pd.Series([float(x[1]) for x in rows],
                                    index=pd.Index([x[0] for x in rows]))
    # 2) qfq_expand(按名称, 含 588000 更长历史)
    if EXPAND.exists():
        for k, v in json.loads(EXPAND.read_text()).items():
            c = str(v.get("code", ""))
            if c in NEED and (c not in cols or len(cols[c]) < len(v.get("rows") or [])):
                rows = v.get("rows") or []
                if rows:
                    cols[c] = pd.Series([float(x[1]) for x in rows],
                                        index=pd.Index([x[0] for x in rows]))
    for c in NEED:                       # 本地缺的用腾讯补
        if c not in cols or len(cols[c]) < 200:
            s = _tx_daily(c)
            if s is not None and len(s) > 200:
                cols[c] = s
                print(f"  [数据] {c} {NAMES.get(c,c)} 腾讯补全 {len(s)} 行")
    df = pd.DataFrame(cols).sort_index()
    df = df[~df.index.duplicated()]
    for c in df:                          # 补缺(停牌/缺失用前值)
        df[c] = df[c].ffill()
    df = df.dropna()
    df = df[df.index >= start]
    if "511260" not in df.columns:      # 国债ETF 历史数据不可得 → 用现金(收益0)替代
        print("  [数据] 511260 国债ETF 不可得, 空仓期按现金(0收益)计, "
              "绝对收益会略低于官方回测")
        df["511260"] = 1.0
    return df[NEED]


# ────────────────────────────── 基线 ──────────────────────────────
def base_core(kc: np.ndarray) -> np.ndarray:
    core, _ = vote_from(kc, COMB_N12, thr=0.5)
    return core


def hold_series(core: np.ndarray, df: pd.DataFrame) -> list[str]:
    """按原 V3 规则生成每日持仓标的序列。"""
    kc = df["588000"].values.astype(float)
    kc_cross = trix_cross(kc)
    def_cross = {c: trix_cross(df[c].values.astype(float)) for c in DEF}
    mom = {c: _momentum(df[c].values.astype(float), 20) for c in DEF}
    out = []
    for i in range(len(kc)):
        if core[i] == 1:
            out.append("588000")
        elif kc_cross[i] == 0:
            g = [c for c in DEF if def_cross[c][i] == 1]
            if g:
                out.append(max(g, key=lambda c: mom[c][i]
                               if not np.isnan(mom[c][i]) else -1e9))
            else:
                out.append(BOND)
        else:
            out.append(BOND)
    return out


def sim(hold: list[str], df: pd.DataFrame) -> dict:
    """按每日持仓标的计算净值(切换日双边滑点)。"""
    px = {c: df[c].values.astype(float) for c in df.columns}
    n = len(df)
    eq = np.ones(n)
    for i in range(1, n):
        c0, c1 = hold[i - 1], hold[i]
        if c0 == c1:
            r = px[c1][i] / px[c1][i - 1] - 1
        elif "511260" in (c0, c1):         # 现金腿无滑点, 只扣单边
            real = c1 if c0 == "511260" else c0
            r = px[real][i] / px[real][i - 1] - 1 - SLIP
        else:                              # 切换: 卖出滑点 + 买入滑点
            r = (px[c0][i] / px[c0][i - 1] - 1) - SLIP * 2
        eq[i] = eq[i - 1] * (1 + r)
    yrs = max(n / 244, 1e-9)
    ret = np.diff(eq) / eq[:-1]
    return {
        "total": float(eq[-1] - 1),
        "cagr": float(eq[-1] ** (1 / yrs) - 1),
        "sharpe": float(ret.mean() / ret.std() * np.sqrt(244)) if ret.std() > 0 else 0,
        "mdd": float(((eq / np.maximum.accumulate(eq)) - 1).min()),
        "sw": int(sum(1 for i in range(1, n) if hold[i] != hold[i - 1])),
        "win": float((ret > 0).mean()),
    }


# ────────────────────────────── AI ──────────────────────────────
SYS = """你是A股科创板择时操盘手。根据截至今日的客观数据, 判断【明天起是否应持有
科创50ETF(588000)】。它代表高波动成长风格; 不持有则资金进低风险防御资产(国债/黄金/红利)。
只依据给出的数据与你对该类资产历史规律的理解, 不得假设你知道未来。
输出严格JSON: {"hold": true|false, "confidence":1-10, "reason":"30字内"}"""


def llm_client():
    from tradingagents.llm_clients.factory import create_llm_client
    return create_llm_client(
        provider=(os.getenv("YOUZI_LLM_PROVIDER") or "qwen").strip(),
        model=(os.getenv("YOUZI_LLM_MODEL") or "qwen3-max").strip(),
        temperature=0).get_llm()


def ask(llm, sys_txt, prompt):
    from langchain_core.messages import SystemMessage, HumanMessage
    try:
        r = llm.invoke([SystemMessage(content=sys_txt),
                        HumanMessage(content=prompt)])
        return getattr(r, "content", None) or str(r)
    except Exception as e:
        print(f"    [LLM失败] {type(e).__name__}", flush=True)
        return ""


def jget(t):
    t = (t or "").strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return {}


def cache_load():
    c = {}
    if CACHE.exists():
        for line in CACHE.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
                c[o["date"]] = o
            except Exception:
                pass
    return c


def cache_save(o):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE, "a", encoding="utf-8") as f:
        f.write(json.dumps(o, ensure_ascii=False) + "\n")


def ai_hold(llm, cache, date, feat: dict, show_n12: bool = True) -> dict:
    """show_n12=False → 不透露 N12 信号, 测 AI 独立判断力(否则 AI 会锚定跟随)。"""
    if date in cache:
        return cache[date]
    p = (f"日期 {date}\n"
         f"科创50(588000) 现价 {feat['px']:.3f}\n"
         f"近5日 {feat['r5']:+.2f}% | 近10日 {feat['r10']:+.2f}% | "
         f"近20日 {feat['r20']:+.2f}% | 近60日 {feat['r60']:+.2f}%\n"
         f"TRIX簇看多占比 {feat['frac']*100:.0f}% | 年化波动 {feat['vol']:.0f}% | "
         f"距60日高 {feat['dh']:+.1f}% | 距60日低 {feat['dl']:+.1f}%\n"
         f"近5日均量/近20日均量 {feat['vr']:.2f}x\n"
         + (f"当前N12信号: {'持有' if feat['core'] else '空仓(转防御)'}\n\n"
            if show_n12 else "\n")
         + "请判断是否应持有科创50(高波动成长), 还是转入防御。")
    o = jget(ask(llm, SYS, p))
    rec = {"date": date, "hold": bool(o.get("hold", feat["core"] == 1)),
           "confidence": o.get("confidence"), "reason": o.get("reason", "")}
    cache_save(rec)
    cache[date] = rec
    return rec


# ────────────────────────────── 主流程 ──────────────────────────────
def feats_at(df: pd.DataFrame, i: int, core: np.ndarray, frac) -> dict:
    kc = df["588000"].values.astype(float)
    r = pd.Series(kc).pct_change()
    return {
        "px": float(kc[i]),
        "r5": float((kc[i] / kc[i - 5] - 1) * 100) if i >= 5 else 0,
        "r10": float((kc[i] / kc[i - 10] - 1) * 100) if i >= 10 else 0,
        "r20": float((kc[i] / kc[i - 20] - 1) * 100) if i >= 20 else 0,
        "r60": float((kc[i] / kc[i - 60] - 1) * 100) if i >= 60 else 0,
        "frac": float(frac[i]) if i < len(frac) else 0.5,
        "vol": float(r.iloc[max(i - 60, 1):i + 1].std() * np.sqrt(244) * 100),
        "dh": float((kc[i] / kc[max(i - 60, 0):i + 1].max() - 1) * 100),
        "dl": float((kc[i] / kc[max(i - 60, 0):i + 1].min() - 1) * 100),
        "vr": 1.0,
        "core": int(core[i]),
    }


def run(start: str, use_ai: bool = True):
    print("加载数据 ...", flush=True)
    df = load(start)
    print(f"  {len(df.columns)} 只 | {df.index[0]} ~ {df.index[-1]} | "
          f"{len(df)} 个交易日", flush=True)
    kc = df["588000"].values.astype(float)
    core, frac = vote_from(kc, COMB_N12, thr=0.5)

    # A0 基线
    h0 = hold_series(core, df)
    r0 = sim(h0, df)
    print(f"\nA0 原版: 累计 {r0['total']*100:.1f}% | 年化 {r0['cagr']*100:.1f}% "
          f"| Sharpe {r0['sharpe']:.2f} | 回撤 {r0['mdd']*100:.1f}% "
          f"| 切换 {r0['sw']}", flush=True)
    res = {"A0": r0}
    if not use_ai:
        return res, df

    # 切换点
    sw_idx = [i for i in range(1, len(core)) if core[i] != core[i - 1]]
    print(f"信号切换点 {len(sw_idx)} 个 → AI 逐点复核", flush=True)
    llm = llm_client()
    cache = cache_load()

    # A2 否决制: 仅在 N12 由空转多(想买)时询问, 否决则保持空仓
    core2 = core.copy()
    for i in sw_idx:
        if core[i] == 1:                      # 想买 → AI 复核
            f = feats_at(df, i, core, frac)
            rec = ai_hold(llm, cache, df.index[i], f)
            if not rec.get("hold"):
                j = i
                while j < len(core2) and core2[j] == 1:
                    core2[j] = 0
                    j += 1
    h2 = hold_series(core2, df)
    r2 = sim(h2, df)
    res["A2"] = r2
    print(f"A2 AI否决制: 累计 {r2['total']*100:.1f}% | Sharpe {r2['sharpe']:.2f} "
          f"| 回撤 {r2['mdd']*100:.1f}%", flush=True)

    # A3 AI 独立判断(切换点, 不透露 N12 信号): 检验"AI 能否替代 N12"
    core3 = core.copy()
    for k, i in enumerate(sw_idx):
        end = sw_idx[k + 1] if k + 1 < len(sw_idx) else len(core)
        f = feats_at(df, i, core, frac)
        rec = ai_hold(llm, cache, f"{df.index[i]}#indep", f, show_n12=False)
        core3[i:end] = 1 if rec.get("hold") else 0
    h3 = hold_series(core3, df)
    r3 = sim(h3, df)
    res["A3"] = r3
    agree = sum(1 for k, i in enumerate(sw_idx)
                if (1 if cache.get(f"{df.index[i]}#indep", {}).get("hold")
                    else 0) == core[i])
    print(f"A3 AI独立: 累计 {r3['total']*100:.1f}% | Sharpe {r3['sharpe']:.2f} "
          f"| 回撤 {r3['mdd']*100:.1f}% | 与N12一致 {agree}/{len(sw_idx)}",
          flush=True)
    return res, df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--no-ai", action="store_true")
    a = ap.parse_args()
    res, _ = run(a.start, not a.no_ai)
    print("\n" + "=" * 66)
    print(f"{'组':<16}{'累计':>9}{'年化':>8}{'Sharpe':>8}{'回撤':>8}{'切换':>6}")
    print("-" * 66)
    for k, n in (("A0", "A0 原版N12"), ("A2", "A2 AI否决制"),
                 ("A3", "A3 AI全权")):
        if k not in res:
            continue
        r = res[k]
        print(f"{n:<16}{r['total']*100:>+8.1f}%{r['cagr']*100:>+7.1f}%"
              f"{r['sharpe']:>8.2f}{r['mdd']*100:>+7.1f}%{r['sw']:>6}")
    print("=" * 66)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
