#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游资战法 · 封板跟踪卖出提醒 (v2)

规则实证 (backtest, 2.2年):
  A 固定次日收盘卖:  笔均 +1.49%  累计 +18,304%  回撤 -36.9%
  B 封板持有·断板卖:  笔均 +1.90%  累计 +58,912%  回撤 -33.4%   ← 本脚本采用
  持有分布: 81% 次日走 / 13% 拿2天 / 少数连板 3~5天, 平均 1.24 天

状态机(每笔持仓, 买入日T收盘起逐日检查 dret ≥ 9.8%):
  · 封板链未断            → HOLD      持有中
  · 断板日 = 昨天          → SELL_TODAY 今日必须走
  · 断板日更早(逾期)       → OVERDUE   立即处理
  · 持有 ≥5 交易日         → FORCE     强制清仓
  · 盘中: 昨封板+今炸板    → BREAK_NOW 纪律: 走

用法:
  python3 scripts/youzi_sell.py             # 推持仓状态
  python3 scripts/youzi_sell.py --dry-run   # 只打印
  crontab: 31 9 * * 1-5 与 50 14 * * 1-5 各一次
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:
    pass

import pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

from tradingagents.notify.dingtalk import send_markdown

TDX_HOST, TDX_PORT = "180.153.18.170", 7709
POSITIONS = Path.home() / ".tradingagents" / "youzi" / "positions.json"
MAX_HOLD = 5
SEALED = 0.098
STATUS_ORDER = {"OVERDUE": 0, "SELL_TODAY": 1, "BREAK_NOW": 2,
                "FORCE": 3, "HOLD": 4, "DATA_SHORT": 5}
STATUS_CN = {"OVERDUE": "🔴逾期未卖", "SELL_TODAY": "🔴今日必走",
             "BREAK_NOW": "🟠盘中炸板", "FORCE": "🟠到期清仓",
             "HOLD": "🟢封板持有", "DATA_SHORT": "⚪数据不足"}


def load_positions() -> list[dict]:
    if not POSITIONS.exists():
        return []
    try:
        raw = json.loads(POSITIONS.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for day, holds in raw.items():
        for code, meta in holds.items():
            out.append({"buy_date": day, "code": code, **meta})
    return out


def fetch_daily(api, code: str, n: int = 15):
    m = TDXParams.MARKET_SH if code[0] in "56" else TDXParams.MARKET_SZ
    try:
        bars = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, m,
                                     code.encode(), 0, n)
    except Exception:
        return None
    if not bars:
        return None
    d = api.to_df(bars)
    d["date"] = pd.to_datetime(d["datetime"]).dt.date
    return d.sort_values("date").reset_index(drop=True)


def analyze(pos: dict, daily: pd.DataFrame, price: float = 0,
            high: float = 0) -> dict:
    """单笔持仓状态。daily 需含 date/dclose/prev 列。"""
    bd = pd.Timestamp(pos["buy_date"]).date()
    s = daily[daily["date"] >= bd].reset_index(drop=True)
    if len(s) < 2:
        return {**pos, "status": "DATA_SHORT", "msg": "数据不足"}
    chain = [r["dclose"] / r["prev"] - 1 >= SEALED for _, r in s.iterrows()]
    days_held = len(s) - 1
    broken = next((i for i, ok in enumerate(chain) if not ok), None)
    yesterday_sealed = chain[-2] if len(chain) >= 2 else chain[-1]
    # 盘中炸板: 今日曾触涨停, 现价明显回落
    broke_intraday = False
    if price > 0 and high > 0:
        prev_c = s["prev"].iloc[-1]
        if prev_c and high / prev_c - 1 >= SEALED and price / prev_c - 1 < 0.095:
            broke_intraday = True

    if broken is not None and broken < len(chain) - 1:
        status, msg = "OVERDUE", f"断板已 {len(chain) - 1 - broken} 天, 立即卖"
    elif not yesterday_sealed:
        status, msg = "SELL_TODAY", "昨日断板 → 今日必须走"
    elif days_held >= MAX_HOLD:
        status, msg = "FORCE", f"持有 {days_held} 天 → 强制清仓"
    elif broke_intraday:
        status, msg = "BREAK_NOW", "今日炸板(封后回落) → 纪律: 走"
    else:
        status, msg = "HOLD", f"封板链 {sum(chain)}/{len(chain)} 天 → 持有中"
    return {**pos, "status": status, "msg": msg, "days_held": days_held}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    holds = load_positions()
    if not holds:
        print("[跳过] 无持仓记录")
        return 0

    api = TdxHq_API()
    if not api.connect(TDX_HOST, TDX_PORT, time_out=5):
        print("! pytdx 连接失败")
        return 1
    rows = []
    try:
        for pos in holds:
            d = fetch_daily(api, pos["code"])
            if d is None:
                rows.append({**pos, "status": "DATA_SHORT", "msg": "无行情"})
                continue
            d["prev"] = d["close"].shift(1)
            d = d.rename(columns={"close": "dclose"}).dropna(subset=["prev"])
            # 盘中炸板检测用实时价
            price = high = 0.0
            try:
                m = (TDXParams.MARKET_SH if pos["code"][0] in "56"
                     else TDXParams.MARKET_SZ)
                q = api.get_security_quotes([(m, pos["code"])])
                if q:
                    price = float(q[0].get("price") or 0)
                    high = float(q[0].get("high") or 0)
            except Exception:
                pass
            rows.append(analyze(pos, d, price, high))
    finally:
        try:
            api.disconnect()
        except Exception:
            pass

    rows.sort(key=lambda r: STATUS_ORDER.get(r["status"], 9))
    now = datetime.now()
    title = f"游资持仓跟踪 {len(rows)} 笔 {now.strftime('%m-%d %H:%M')}"
    lines = [f"### 游资持仓跟踪 · {now.strftime('%Y-%m-%d %H:%M')}", ""]
    for r in rows:
        pnl = ""
        try:
            if r.get("entry"):
                px = [x for x in rows if x["code"] == r["code"]]
                pnl = f"　浮盈参考见行情"
        except Exception:
            pass
        lines.append(
            f"- {STATUS_CN.get(r['status'], r['status'])} "
            f"**{r.get('name', r['code'])}({r['code']})**\n"
            f"　买入 {r.get('buy_date', '?')} @ {r.get('entry', '?')}"
            f"　持有 {r.get('days_held', '?')} 天\n"
            f"　**{r['msg']}**")
    lines += ["", "> 封板链=买入日起每日收盘涨幅≥9.8%。断板日收盘卖, 最多持 5 天。",
              "> 盘中炸板(封后回落)按纪律即时走, 不等收盘。"]
    text = "\n".join(lines)
    print("=" * 70)
    print(title)
    print(text)
    print("=" * 70)
    if args.dry_run:
        print("\n[dry-run] 不推送")
        return 0
    webhook = (os.getenv("DINGTALK_YOUZI_WEBHOOK")
               or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    keyword = (os.getenv("DINGTALK_YOUZI_KEYWORD")
               or os.getenv("DINGTALK_KEYWORD") or "游资").strip()
    if not webhook:
        print("! 钉钉未配置")
        return 0
    ok = send_markdown(title, text, webhook=webhook, keyword=keyword)
    print(f"推送: {'成功' if ok else '失败'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
