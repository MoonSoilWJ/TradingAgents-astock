#!/usr/bin/env python3
"""ETF 前复权日K收盘 共享数据模块(东财 akshare + 本地缓存 + 增量刷新)。

为什么必须用它而不是 pytdx 原始价:
  ETF 会做【份额折算/拆分】(如 1:2 拆分 → 价格腰斩、份额翻倍、资产不变),
  pytdx 原始价在折算日出现假跳空 → 回测/信号里变成假 -50% + 假 MA20 死叉。
  实测踩坑: 芯片ETF @3.0→@1.501(-50%)、银行 @1.607→@0.894 均为折算假象。

用法:
    from etf_qfq_data import fetch_qfq_close
    s = fetch_qfq_close("512480")   # pd.Series(index=date, values=前复权close)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

CACHE_DIR = Path.home() / ".tradingagents" / "cache" / "etf_full"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
STALE_DAYS = 3   # 缓存最后日期距今超过 N 天则重拉(容忍周末/节假日)


def fetch_qfq_close(code: str, start: str = "2019-01-01") -> pd.Series | None:
    """前复权收盘序列。缓存优先, 过期则全量重拉(东财, 每只约1秒)。"""
    import akshare as ak

    f = CACHE_DIR / f"qfq_{code}.json"
    rows: list[list] = []
    if f.exists():
        try:
            rows = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            rows = []

    fresh = rows and rows[-1][0] >= (datetime.now() - timedelta(days=STALE_DAYS)
                                     ).strftime("%Y-%m-%d")
    if not fresh:
        for k in range(3):
            try:
                df = ak.fund_etf_hist_em(symbol=code, period="daily", adjust="qfq")
                if df is not None and len(df) > 0:
                    rows = [[str(r["日期"]), float(r["收盘"])]
                            for _, r in df.iterrows()
                            if r["收盘"] and float(r["收盘"]) > 0]
                    rows.sort(key=lambda x: x[0])
                    f.write_text(json.dumps(rows, ensure_ascii=False),
                                 encoding="utf-8")
                    break
            except Exception:
                import time
                time.sleep(1.0 * (k + 1))
        else:
            rows = rows or []

    if not rows:
        return None
    s = pd.Series({pd.Timestamp(d): float(v) for d, v in rows}).sort_index()
    return s[s.index >= pd.Timestamp(start)] if start else s


if __name__ == "__main__":
    import sys
    for code in (sys.argv[1:] or ["512480", "159995", "512800"]):
        s = fetch_qfq_close(code)
        if s is None:
            print(f"{code}: 拉取失败")
            continue
        # 折算断点检测: 单日 |涨跌| > 25% 必是数据问题
        r = s.pct_change().dropna()
        jumps = r[abs(r) > 0.25]
        print(f"{code}: {len(s)}根 {s.index[0].date()}~{s.index[-1].date()} | "
              f"末值 {s.iloc[-1]:.3f} | >25%跳空日 {len(jumps)}")
        for d, v in jumps.items():
            print(f"    ⚠ {d.date()} {v:+.1%}")
