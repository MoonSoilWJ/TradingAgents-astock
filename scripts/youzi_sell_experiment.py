#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卖出AI 对照实验: 新提示词(SENS 灵敏度) + 压缩历史  vs  基线(BASE journal).

目的(用户 2026-09-18):
  ① 验证"调卖点灵敏度"(高位回落/破位也能触发)是否有效;
  ② 验证"压缩此前扫描记录"(省token)的效果;
  ③ 在刷新 pool_daily.pkl 后, 看均线/市场情绪数据缺口是否补齐、是否改变决策。

做法:
  - 用独立 cache(不命中旧基线缓存) + 不污染真实 journal, 重跑 9/18 的 000700/002774;
  - 读取新分数, 与 BASE journal(旧提示词)逐点对照;
  - 用 BASE journal 的真实历史, 量化"压缩历史"相对"全量历史"的 token 降幅。
"""
import json
import sys
from pathlib import Path

import youzi_sell_ai as YS

EXP_CACHE = Path("/tmp/youzi_sell_exp_cache.json")
CODES = "000700,002774"
DATE = "2026-09-18"

# 1) 隔离缓存 + 不污染真实 journal
def _exp_cache():
    return EXP_CACHE, (json.loads(EXP_CACHE.read_text()) if EXP_CACHE.exists() else {})
YS._bt_cache = _exp_cache
YS.log_judgement = lambda *a, **k: None
# 只评 positions.json 里的 9/17 买盘(避免 youzi_sold.jsonl 重复计入同一票)
YS.SOLD = Path("/tmp/__nonexistent_sold__.jsonl")


def run_llm() -> None:
    print("[实验] 重跑 %s %s (新提示词+压缩历史, step=5) ..." % (DATE, CODES))
    YS.backtest(limit=0, step=5, max_hold=2, dry_data=False, thr=70,
                days=0, date=DATE, show_io=False, code=CODES)


def load_new_scores() -> dict:
    cache = json.loads(EXP_CACHE.read_text()) if EXP_CACHE.exists() else {}
    out = {}
    for k, v in cache.items():
        code = k.split("|")[0]
        t = k.split("|")[2]
        dec = v.get("dec", {})
        out.setdefault(code, []).append(
            (t, dec.get("sell_score"), dec.get("verdict"), dec.get("action")))
    for c in out:
        out[c].sort(key=lambda x: x[0])
    return out


def load_base_scores() -> dict:
    jpath = YS.LOG_DIR / "sell_judgements.jsonl"
    out = {}
    if not jpath.exists():
        return out
    for ln in open(jpath, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        r = json.loads(ln)
        if (r.get("mode") == "backtest" and r.get("date") == DATE
                and r.get("code") in CODES.split(",")):
            t = r["time"].replace(":", "")
            out.setdefault(r["code"], []).append(
                (t, r.get("sell_score"), r.get("verdict"), r.get("action")))
    for c in out:
        out[c].sort(key=lambda x: x[0])
    return out


def token_saving() -> None:
    """用 BASE journal 的真实历史, 量化压缩历史的 token 降幅。"""
    jpath = YS.LOG_DIR / "sell_judgements.jsonl"
    recs = []
    for ln in open(jpath, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        r = json.loads(ln)
        if (r.get("mode") == "backtest" and r.get("date") == DATE
                and r.get("code") == "002774"):
            recs.append(r)
    recs.sort(key=lambda r: r["time"])
    if len(recs) < 10:
        print("[token] 样本不足, 跳过")
        return
    # 取最后一个扫描点(历史最长)构建 snaps
    history = []
    for r in recs:
        history.append(YS._snap_dict(
            __import__("datetime").datetime.strptime(r["time"], "%H:%M"),
            r["cur_pct"], r["from_high"], r["shape"],
            {"sell_score": r.get("sell_score"), "verdict": r.get("verdict"),
             "reason": r.get("reason", "")}))
    cur_now, fh_now = recs[-1]["cur_pct"], recs[-1]["from_high"]
    verbose = "\n".join(YS._snap_to_line(h) for h in history)
    compact = "\n".join(YS._compact_history_lines(history))
    trend = YS._trend_summary(history, cur_now, fh_now)
    v_total = len(("【此前扫描记录】\n" + verbose + "\n【趋势统计】" + trend).encode("utf-8"))
    c_total = len(("【此前扫描记录】\n" + compact + "\n【趋势统计】" + trend).encode("utf-8"))
    print("\n[token] 历史块长度(字节, 含趋势统计):")
    print("  全量(原): %d 字节 / %d 行" % (v_total, len(history)))
    print("  压缩(新): %d 字节 / %d 行" % (c_total, len(YS._compact_history_lines(history))))
    print("  降幅: %.1f%%" % (100 * (1 - c_total / v_total)))


def main() -> None:
    run_llm()
    new = load_new_scores()
    base = load_base_scores()
    print("\n" + "=" * 78)
    print("  新提示词(SENS) vs 基线(BASE) 逐点 sell_score 对照  (%s)" % DATE)
    print("=" * 78)
    for code in CODES.split(","):
        nb = {t: (s, v) for t, s, v, _ in base.get(code, [])}
        nn = {t: (s, v) for t, s, v, _ in new.get(code, [])}
        times = sorted(set(nb) | set(nn))
        trig_base = sum(1 for _, (s, _) in nb.items() if s is not None and s >= 70)
        trig_new = sum(1 for _, (s, _) in nn.items() if s is not None and s >= 70)
        print("\n  %s  基线触发(≥70):%d  新触发(≥70):%d" % (code, trig_base, trig_new))
        for t in times:
            bs, bv = nb.get(t, (None, ""))
            ns, nv = nn.get(t, (None, ""))
            mark = ""
            if ns is not None and bs is not None and ns - bs >= 20:
                mark = "  <<< 升"
            elif ns is not None and bs is not None and bs - ns >= 20:
                mark = "  >>> 降"
            print("    %s  基线 %s/%-4s  新 %s/%-4s%s"
                  % (t, _f(bs), (bv or ""), _f(ns), (nv or ""), mark))
    token_saving()
    print("\n[完成] 新分数见 %s" % EXP_CACHE)


def _f(v):
    return "-" if v is None else ("%.0f" % v)


if __name__ == "__main__":
    main()
