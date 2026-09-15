#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""封顶增益 + 推送起始时点复盘 (2026-09-15, 基于实盘判定记录)。

━━━ 回答两个问题 ━━━
1. P1 封顶(题材≤4→prob≤72 / 盘口≤4→prob≤70)的增益:
   1a. 全部 BUY 信号(不限 prob)按 题材≤4 vs ≥5 分组 — 子分本身有无判别力
   1b. prob≥75(推送域)内 封顶拦截组 vs 放行组 + 每日前3配额模拟
2. 推送若从 09:40 才开始(实际 09:33 就推了), 收益是增是减。
   → 样本为【每天所有 BUY 信号】(2026-09-15 用户要求, 不再限 prob≥75)。

━━━ 口径 ━━━
- 样本 = ai_judgements.jsonl 中全部 action=BUY 判定(不限 prob);
  主口径: 同日同码取首条(每只票每天只算一次, 防止冷却期内重复判定
  把同一结果计多次); 附: 全部原始判定记录口径(含同票重复)对照。
- 封顶当时未在进程内生效 → 记录 prob 即 AI 原始分, 可直接模拟封顶。
- 入场 = 判定日昨收 × (1+pct/100); 卖出 = 次日开盘价(东财日K, 前复权);
  seal = youzi_calibrate 回填的当日封板布尔。
- 今日判定无次日K、无 seal → 只计数, 不进收益/封板率。
- 2026-09-10 批次 pct 被双倍缩放(896.23 实为 8.96%), 载入时归一化。
"""
from __future__ import annotations

import json
import os as _os
_os.environ["no_proxy"] = _os.environ["NO_PROXY"] = "*"
import json as _json
import ssl
import time
import urllib.request
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
STATE = Path.home() / ".tradingagents" / "youzi"
DAILY_MAX = 3


def _em_get(url: str) -> dict:
    """东财 GET: 浏览器 UA(裸 urllib UA 会被批量拒绝) + 失败重试。"""
    hdr = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36",
           "Referer": "https://quote.eastmoney.com/"}
    last = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers=hdr)
            return _json.loads(urllib.request.urlopen(req, timeout=10).read())
        except Exception as exc:
            last = exc
            time.sleep(1)
    raise last if last else RuntimeError("em get failed")


def fetch_daily(codes: list[str]) -> dict[str, pd.DataFrame]:
    """腾讯日K(不复权, 近80根) → {code: df(date, open, close)}。

    pytdx get_security_bars 已失效; 东财对批量请求限频(2026-09-15 实测
    连发即 RemoteDisconnected) → 复盘只需昨收/次日开盘(不需要成交额),
    用腾讯 fqkline 最稳。本机 Clash 注入自签证书 → 跳过 SSL 校验。
    """
    ssl._create_default_https_context = ssl._create_unverified_context
    out: dict[str, pd.DataFrame] = {}
    for i, code in enumerate(codes):
        mkt = "sh" if code[0] in "569" else "sz"
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={mkt}{code},day,,,80,")
        try:
            d = _json.loads(urllib.request.urlopen(url, timeout=8).read())
            node = d["data"][f"{mkt}{code}"]
            key = "day" if "day" in node else next(
                k for k in node if k.endswith("day"))
            df = pd.DataFrame(
                [{"date": r[0], "open": float(r[1]), "close": float(r[2])}
                 for r in node[key]])
            df = df[(df["close"] > 0) & (df["open"] > 0)].sort_values("date")
            if len(df):
                out[code] = df
        except Exception:
            continue
        if (i + 1) % 25 == 0:
            print(f"    ...日K {i + 1}/{len(codes)}", flush=True)
        time.sleep(0.15)
    return out


def _stat(rows: list[dict]) -> dict:
    rets = [r["ret"] for r in rows if r.get("ret") is not None]
    seals = [r["seal"] for r in rows if isinstance(r.get("seal"), bool)]
    return {
        "n": len(rows),
        "n_ret": len(rets),
        "avg_ret": (sum(rets) / len(rets) * 100) if rets else None,
        "win": (sum(1 for x in rets if x > 0) / len(rets) * 100) if rets else None,
        "seal_rate": (sum(1 for x in seals if x) / len(seals) * 100)
                     if seals else None,
        "n_seal_known": len(seals),
    }


def _fmt(tag: str, s: dict) -> str:
    ar = f"{s['avg_ret']:+.2f}%" if s["avg_ret"] is not None else "  —  "
    wn = f"{s['win']:.0f}%" if s["win"] is not None else " —"
    sr = f"{s['seal_rate']:.0f}%" if s["seal_rate"] is not None else " —"
    return (f"{tag:<26} n={s['n']:<4} 有次日收益 {s['n_ret']:<3} "
            f"次日开盘收益 {ar:>8}  胜率 {wn:>4}  封板率 {sr:>4}"
            f"(已知{s['n_seal_known']})")


def main() -> int:
    recs = [json.loads(l) for l in
            (STATE / "ai_judgements.jsonl").open(encoding="utf-8")]
    for r in recs:      # 2026-09-10 批次 pct 被双倍缩放(896.23 实为 8.96%)
        p = float(r.get("pct") or 0)
        if p > 25:
            r["pct"] = p / 100
    cand = [r for r in recs if r.get("action") == "BUY"]   # 全部BUY, 不限prob
    seen: set = set()
    first: list[dict] = []
    for r in sorted(cand, key=lambda r: r["ts"]):   # 同日同码取首条
        k = (r["ts"][:10], r["code"])
        if k not in seen:
            seen.add(k)
            first.append(r)
    print(f"全部 BUY 判定 {len(cand)} 条 → 去重(同日同码) {len(first)} 条, "
          f"覆盖 {sorted({r['ts'][:10] for r in first})}\n")

    codes = sorted({r["code"] for r in first})
    print(f"按需拉取 {len(codes)} 只日K(东财) ...")
    daily = fetch_daily(codes)
    print(f"日K到手 {len(daily)} 只\n")

    def attach(r: dict) -> dict:
        r = dict(r)
        day = r["ts"][:10]
        d = daily.get(r["code"])
        if d is None:
            return r
        hist = d[d["date"] < day]
        nxt = d[d["date"] > day]
        if not len(hist):
            return r
        prev_close = float(hist["close"].iloc[-1])
        r["entry"] = prev_close * (1 + float(r["pct"]) / 100)
        if len(nxt):
            r["ret"] = float(nxt["open"].iloc[0]) / r["entry"] - 1
        return r

    rows = [attach(r) for r in first]
    rows_all = [attach(r) for r in cand]   # 含同票重复判定口径

    def sc(r: dict, k: str) -> float | None:
        v = (r.get("scores") or {}).get(k)
        return float(v) if v is not None else None

    # ── 问题1a: 题材子分判别力(全部 BUY 信号) ────────────────────────
    print("━━━ 问题1a: 题材子分 vs 结果(全部BUY信号, 不限prob, 去重口径) ━━━")
    g_low = [r for r in rows if sc(r, "题材") is not None and sc(r, "题材") <= 4]
    g_high = [r for r in rows if sc(r, "题材") is not None and sc(r, "题材") >= 5]
    print(_fmt("题材≤4(孤板/弱联动)", _stat(g_low)))
    print(_fmt("题材≥5(有联动)", _stat(g_high)))
    print("(全部BUY原始记录口径, 含同票重复判定)")
    print(_fmt("  题材≤4", _stat([r for r in rows_all
                                if sc(r, "题材") is not None
                                and sc(r, "题材") <= 4])))
    print(_fmt("  题材≥5", _stat([r for r in rows_all
                                if sc(r, "题材") is not None
                                and sc(r, "题材") >= 5])))
    print()

    # ── 问题1b: P1封顶(prob≥75 推送域) ──────────────────────────────
    dom = [r for r in rows if float(r.get("prob") or 0) >= 75]
    blocked = [r for r in dom
               if (sc(r, "题材") is not None and sc(r, "题材") <= 4)
               or (sc(r, "盘口") is not None and sc(r, "盘口") <= 4)]
    passed = [r for r in dom if r not in blocked]
    print("━━━ 问题1b: 封顶拦截组 vs 放行组(仅prob≥75推送域, 去重口径) ━━━")
    print(_fmt("封顶会拦掉的", _stat(blocked)))
    print(_fmt("封顶放行的", _stat(passed)))
    print()

    print("━━━ 配额模拟(每日 prob 降序取前 3, 有封顶 vs 无封顶) ━━━")
    days = sorted({r["ts"][:10] for r in dom})
    port_with, port_wo, notes = [], [], []
    for day in days:
        for cap in (True, False):
            pool = [r for r in dom if r["ts"][:10] == day
                    and r.get("ret") is not None]
            if cap:
                pool = [r for r in pool
                        if not ((sc(r, "题材") is not None
                                 and sc(r, "题材") <= 4)
                                or (sc(r, "盘口") is not None
                                    and sc(r, "盘口") <= 4))]
            top = sorted(pool, key=lambda r: -float(r["prob"]))[:DAILY_MAX]
            ret = (sum(r["ret"] for r in top) / len(top)) if top else 0.0
            (port_with if cap else port_wo).append((day, ret, len(top)))
    for (d1, r1, n1), (d2, r2, n2) in zip(port_with, port_wo):
        notes.append(f"{d1}  无封顶: {r2 * 100:+6.2f}%/{n2}笔   "
                     f"有封顶: {r1 * 100:+6.2f}%/{n1}笔   "
                     f"差 {((r1 - r2) * 100):+6.2f}pp")
    print("\n".join(notes))
    wo = [r for _, r, _ in port_wo]
    wi = [r for _, r, _ in port_with]
    print(f"\n日均(等权): 无封顶 {sum(wo) / len(wo) * 100:+.2f}% → "
          f"有封顶 {sum(wi) / len(wi) * 100:+.2f}%  "
          f"(差 {(sum(wi) - sum(wo)) / len(wo) * 100:+.2f}pp/日)")
    print()

    # ── 问题2: 推送起始时点(全部 BUY 信号) ──────────────────────────
    print("━━━ 问题2: 按判定时点分桶(全部BUY信号, 去重口径) ━━━")

    def bucket(ts: str) -> str:
        hm = ts[11:16]
        if hm < "09:40":
            return "09:30-09:40"
        if hm < "10:00":
            return "09:40-10:00"
        if hm < "11:30":
            return "10:00-11:30"
        return "午后"

    buckets: dict[str, list] = {}
    for r in rows:
        buckets.setdefault(bucket(r["ts"]), []).append(r)
    for b in ["09:30-09:40", "09:40-10:00", "10:00-11:30", "午后"]:
        print(_fmt(b, _stat(buckets.get(b, []))))
    early = _stat(buckets.get("09:30-09:40", []))
    late = _stat(sum((buckets.get(b, []) for b in
                      ["09:40-10:00", "10:00-11:30", "午后"]), []))
    print("(全部BUY原始记录口径, 含同票重复判定)")
    raw: dict[str, list] = {}
    for r in rows_all:
        raw.setdefault(bucket(r["ts"]), []).append(r)
    for b in ["09:30-09:40", "09:40-10:00", "10:00-11:30", "午后"]:
        print(_fmt("  " + b, _stat(raw.get(b, []))))
    print()
    if early["avg_ret"] is not None and late["avg_ret"] is not None:
        diff = late["avg_ret"] - early["avg_ret"]
        print(f"结论: 砍掉 09:40 前的推送, 剩余信号均笔 "
              f"{late['avg_ret']:+.2f}% vs 09:40前的 {early['avg_ret']:+.2f}% "
              f"→ {'增加' if diff > 0 else '减少'} {abs(diff):.2f}pp/笔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
