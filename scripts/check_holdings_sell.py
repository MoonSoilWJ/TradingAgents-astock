#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""14:55 持仓簇值卖点检查 — 触发 frac<=0.5 时钉钉通知。

数据源:
  - 持仓清单: results/holdings_cluster.csv (position=1 的最新一行/标的)
  - 日K: akshare 前复权; 14:55 盘中若缺当日K, 用腾讯快照补一根
  - 簇值: 6 组 TRIX 投票 (与科创 N12 策略一致)

用法:
  python3 scripts/check_holdings_sell.py            # 检查 + 推送
  python3 scripts/check_holdings_sell.py --dry-run  # 只打印, 不推送

crontab:
  55 14 * * 1-5 cd /Users/licheng/文档/TradingAgents-astock && \
    /usr/local/bin/python3 scripts/check_holdings_sell.py >> \
    ~/.tradingagents/rotation/holdings_sell_check.log 2>&1
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import akshare as ak  # noqa: E402

import backtest_588000_n12 as B  # noqa: E402

CSV_PATH = ROOT / "results" / "holdings_cluster.csv"
FIELDS = ["date", "code", "name", "close", "frac", "long_count",
          "buy_date", "buy_price", "position", "note"]
EPS = 1e-9


def load_holdings() -> list[dict]:
    """读 CSV, 每个 code 取最新一行, 只保留 position=1."""
    if not CSV_PATH.exists():
        return []
    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8-sig")))
    latest: dict[str, dict] = {}
    for r in rows:
        if r.get("code"):
            latest[r["code"]] = r
    return [r for r in latest.values() if str(r.get("position", "1")) == "1"]


def fetch_close(code: str) -> tuple[np.ndarray, np.ndarray]:
    """前复权日K; 若最后一天不是今天, 用腾讯快照补今日盘中价."""
    today = date.today().isoformat()
    df = ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date="20250101", end_date=today.replace("-", ""), adjust="qfq",
    )
    df = df.rename(columns={"日期": "date", "收盘": "close"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    close = df["close"].astype(float).values
    dates = df["date"].values
    if len(close) and str(pd.Timestamp(dates[-1]).date()) < today:
        try:
            from tx_quote import snapshot
            px = float(snapshot([code]).get(code, {}).get("price") or 0)
            if px > 0:
                close = np.append(close, px)
                dates = np.append(dates, np.datetime64(today))
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] 今日快照补价失败: {exc}")
    return dates, close


def upsert_csv(code: str, name: str, d: str, close: float, frac: float,
               buy_date: str, buy_price: str, note: str) -> None:
    """当天同标的已有记录则更新, 否则追加."""
    rows = []
    if CSV_PATH.exists():
        rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8-sig")))
    found = False
    for r in rows:
        if r.get("code") == code and r.get("date") == d:
            r.update({"close": f"{close:.3f}", "frac": f"{frac:.2f}",
                      "long_count": str(int(round(frac * 6))), "note": note})
            found = True
    if not found:
        rows.append({"date": d, "code": code, "name": name,
                     "close": f"{close:.3f}", "frac": f"{frac:.2f}",
                     "long_count": str(int(round(frac * 6))),
                     "buy_date": buy_date, "buy_price": buy_price,
                     "position": "1", "note": note})
    rows.sort(key=lambda r: (r.get("code", ""), r.get("date", "")))
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="持仓簇值卖点检查")
    ap.add_argument("--dry-run", action="store_true", help="只打印, 不推送钉钉")
    args = ap.parse_args()

    holds = load_holdings()
    if not holds:
        print("无持仓记录（results/holdings_cluster.csv 无 position=1）")
        return 0

    alerts: list[str] = []
    for h in holds:
        code = h["code"]
        name = h.get("name", "")
        try:
            dates, close = fetch_close(code)
            if len(close) < 50:
                print(f"! {name}({code}) 数据不足")
                continue
            _pos, frac = B.vote_from(close, B.COMB_N12, 0.5)
            f = float(frac[-1])
            px = float(close[-1])
            d = str(pd.Timestamp(dates[-1]).date())
            n = int(round(f * 6))
            print(f"[{datetime.now():%H:%M:%S}] {name}({code}) "
                  f"现价 {px:.3f} 簇值 {f:.2f} ({n}/6)")
            upsert_csv(code, name, d, px, f,
                       h.get("buy_date", ""), h.get("buy_price", ""), "14:55检查")

            if f <= 0.5 + EPS:
                bp = float(h.get("buy_price") or 0)
                pnl = (px / bp - 1) * 100 if bp else 0.0
                alerts.append(
                    f"- **{name}({code})** 簇值 **{f:.2f} ({n}/6)**\n"
                    f"  现价 {px:.3f} | 成本 {bp:.3f} | 浮动 {pnl:+.2f}%"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"! {name}({code}) 检查失败: {exc}")

    if not alerts:
        print("无卖出信号（全部 frac > 0.5）")
        return 0

    text = ("### 持仓卖出信号（簇值 <= 0.5）\n\n"
            + "\n".join(alerts)
            + f"\n\n时间: {datetime.now():%Y-%m-%d %H:%M}")
    print("\n" + text)
    if args.dry_run:
        print("\n[dry-run] 跳过钉钉推送")
        return 0

    try:
        from tradingagents.notify.dingtalk import send_markdown
        ok = send_markdown("持仓卖点提醒 test", text)
        print("钉钉推送:", "成功" if ok else "失败")
    except Exception as exc:  # noqa: BLE001
        print("钉钉推送异常:", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
