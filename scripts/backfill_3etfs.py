#!/usr/bin/env python3
"""增量补 3 只【已退市/缺5min】的 ETF 到无偏5min池, 使回测候选池 = 106只实盘全集。

背景:
  backfill_5min_pre2024 只抓了「当前在市」的 103 只, 漏掉了清单里但已退市的
  159833/513680/513960。这 3 只在 full_daily 里有完整日K(上市~退市区间),
  故历史上属于可交易标的, 回测应能在它们未退市的日子选中。补其5min后
  codes5 由 103->106, 回测候选池与实盘/shadow 的 get_all_t0_etfs() 全集对齐。

落盘规则(与回测 codes5 = pre∪post 一致):
  - date < 2022-06-15  -> tdx_5min_pre2024.json
  - date >= 2024-07-03 -> tdx_5min_2y.json
  - 中间 2022-06-15~2024-07-02 (回测无数据段) 跳过

对齐日K用 full_daily_2015_2026.json; 量纲 scale = dclose / 末根1min价。
支持断点续跑(已抓过的 code/date 自动跳过)。
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from backtest_planC_1min_4y import (  # noqa: E402
    connect, daily_close_of, recon_5min, safe_get_minute,
)
from backfill_5min_pre2024 import normalize  # noqa: E402

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"
FULL = CACHE / "full_daily_2015_2026.json"
PRE = CACHE / "tdx_5min_pre2024.json"
POST = CACHE / "tdx_5min_2y.json"
TARGETS = ["159833", "513680", "513960"]
PRE_CUT = "2022-06-15"
POST_START = "2024-07-03"


def main() -> None:
    fd = json.loads(FULL.read_text(encoding="utf-8"))
    pre_d = json.loads(PRE.read_text(encoding="utf-8"))["etf_5min"]
    post_d = json.loads(POST.read_text(encoding="utf-8"))["etf_5min"]

    api = connect()
    t0 = time.time()
    total_ok = total_fail = 0
    try:
        for ci, code in enumerate(TARGETS, 1):
            dates = [r["date"] for r in fd.get(code, {}).get("returns", [])]
            # 只补回测用得到的区间; 断点续跑跳过已抓的
            todo = []
            for d in dates:
                if d < PRE_CUT:
                    if d not in pre_d.get(code, {}):
                        todo.append(d)
                elif d >= POST_START:
                    if d not in post_d.get(code, {}):
                        todo.append(d)
            ok = fail = 0
            for d in todo:
                raw = safe_get_minute(api, code, d)
                if not raw:
                    fail += 1
                    continue
                dc = daily_close_of(fd, code, d)
                raw_last = float(raw[-1]["price"]) if raw else None
                scale = (dc / raw_last) if (dc and dc > 0 and raw_last and raw_last > 0) else 1.0
                bars = recon_5min(raw, d, scale=scale)
                if not bars:
                    fail += 1
                    continue
                nb = normalize(bars)
                if d < PRE_CUT:
                    pre_d.setdefault(code, {})[d] = nb
                else:
                    post_d.setdefault(code, {})[d] = nb
                ok += 1
            total_ok += ok
            total_fail += fail
            el = (time.time() - t0) / 60
            print(f"[{ci}/{len(TARGETS)}] {code}: +{ok}天(失败{fail}) | "
                  f"累计{total_ok} 用时{el:.1f}min", flush=True)
            PRE.write_text(json.dumps({"etf_5min": pre_d}, ensure_ascii=False), encoding="utf-8")
            POST.write_text(json.dumps({"etf_5min": post_d}, ensure_ascii=False), encoding="utf-8")
    finally:
        try:
            api.disconnect()
        except Exception:  # noqa: BLE001
            pass
        PRE.write_text(json.dumps({"etf_5min": pre_d}, ensure_ascii=False), encoding="utf-8")
        POST.write_text(json.dumps({"etf_5min": post_d}, ensure_ascii=False), encoding="utf-8")

    print(f">>> 完成: ok={total_ok} fail={total_fail}")
    # 对齐校验: 末根 close / 日K close
    import statistics
    ratios, bad = [], 0
    for code in TARGETS:
        tail = list(pre_d.get(code, {}))[-5:] + list(post_d.get(code, {}))[-5:]
        for d in tail:
            src = pre_d.get(code, {}).get(d) or post_d.get(code, {}).get(d)
            dc = daily_close_of(fd, code, d)
            if not src or not dc or not src[-1].get("close"):
                continue
            r = src[-1]["close"] / dc
            ratios.append(r)
            if abs(r - 1) > 0.005:
                bad += 1
    if ratios:
        print(f">>> 对齐校验 n={len(ratios)} bad={bad} 中位比值={statistics.median(ratios):.5f}")


if __name__ == "__main__":
    main()
