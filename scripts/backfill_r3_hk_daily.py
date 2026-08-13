#!/usr/bin/env python3
"""Backfill 14 只宽基港股 ETF 日K 到 full_daily_2015_2026.json。

背景(循环依赖修复):
  refresh_t0_pool 的 _passes_quality 从该文件读 _daily_metrics(上市天数/流动性);
  若 full_daily 没有某 code → listing=None → 判"无行情/幽灵"拒掉 → 不写进 auto_t0_etfs.json
  → 不进 universe(= full∪aligned) → 不进 R3 月度池。
  这 14 只 = 白名单修复(genuine=True) 且 不被 drop_sector 拦的宽基港股, 但因日K缓存缺它们
  一直双重缺席。本脚本先把日K补进 full_daily, 后续 refresh + export 才能把它们真正纳入。

数据源: akshare fund_etf_hist_sina(与 update_live_cache.py 完全一致, 保证 volume 单位对齐)。
格式: {code: {"returns": [{date,open,high,low,close,volume,return_pct}]}}。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

CACHE = Path.home() / ".tradingagents/cache/t0_5min"
FULL = CACHE / "full_daily_2015_2026.json"

# 宽基港股(修白名单后 genuine=True 且 不被 drop_sector 拦):
#   19只港股ETF中 7只主题(新药/汽车/医疗/消费)被 drop_sector 拦 → 排除;
#   余 12只 + 501023港中小企(501前缀, 宽基) = 14只。
TARGETS = [
    "159126", "159131", "159143", "159179", "159277", "159331", "159519",
    "159545", "159568", "159711", "159741", "159788", "159198", "501023",
]


def main() -> None:
    import akshare as ak

    fd = json.loads(FULL.read_text(encoding="utf-8"))
    t0 = time.time()
    ok = fail = 0
    for code in TARGETS:
        sym = ("sh" if code[0] in "56" else "sz") + code
        try:
            h = ak.fund_etf_hist_sina(symbol=sym)
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] {code} 拉取失败: {e}", flush=True)
            fail += 1
            continue
        rows = []
        for _, r in h.iterrows():
            d = str(r["date"])[:10]
            try:
                rows.append({
                    "date": d, "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "volume": float(r.get("volume") or 0),
                })
            except Exception:  # noqa: BLE001
                continue
        rows.sort(key=lambda x: x["date"])
        prev = None
        for rec in rows:
            rec["return_pct"] = ((rec["close"] - prev) / prev * 100) if prev else 0.0
            prev = rec["close"]
        if rows:
            fd[code] = {"returns": rows}
            print(f"  {code}: {len(rows)} 根, {rows[0]['date']}~{rows[-1]['date']}", flush=True)
            ok += 1
        else:
            print(f"  [warn] {code} 空数据", flush=True)
            fail += 1
    FULL.write_text(json.dumps(fd, ensure_ascii=False), encoding="utf-8")
    print(f">>> 落盘 {FULL.name} ({FULL.stat().st_size/1e6:.1f}MB) | ok={ok} fail={fail} "
          f"用时{(time.time()-t0)/60:.1f}min")


if __name__ == "__main__":
    main()
