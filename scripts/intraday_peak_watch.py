#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 【盘中动态盯高点 → 钉钉提醒卖】
#
# 逻辑(不预测顶, 只跟回落):
#   09:30 起监控持仓股, 记录日内最高价(用交易所快照 high);
#   实时价从日内最高回落 >= TRAIL(默认0.8%) → 钉钉推送"建议卖出";
#   每只每日只推一次; 同日状态存 intraday_peak_state.json, 隔日自动重置.
#
# 为什么是"回落"而不是"预测最高":
#   实证(1600股票日): 41% 的日内最高点出现在 09:35, 前30分钟占55%;
#   开盘冲高后回落0.8%即提醒, 大概率卖在开盘附近的高点。
#
# 用法:
#   python3 scripts/intraday_peak_watch.py --dry-run          # 测试不推送
#   python3 scripts/intraday_peak_watch.py                    # 正式(读持仓)
#   python3 scripts/intraday_peak_watch.py --codes 600539,603823
#   python3 scripts/intraday_peak_watch.py --trail 0.005      # 更敏感(0.5%)
#
# cron: */2 9-11,13-14 * * 1-5  cd <repo> && python3 scripts/trading_day_gate.py && python3 scripts/intraday_peak_watch.py
import argparse
import json
import sys
from datetime import datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from tx_quote import snapshot  # noqa: E402

HOLDINGS = Path("/Users/licheng/文档/TradingAgents-astock/results/holdings_cluster.csv")
STATE = Path("/Users/licheng/.tradingagents/rotation/intraday_peak_state.json")


def in_session(now):
    t = now.time()
    return (time(9, 30) <= t <= time(11, 30)) or (time(13, 0) <= t <= time(15, 0))


def load_holdings():
    out = []
    if not HOLDINGS.exists():
        return out
    for line in HOLDINGS.read_text().splitlines()[1:]:
        p = line.split(",")
        if len(p) >= 9 and p[8].strip() == "1":
            out.append({"code": p[1], "name": p[2],
                        "buy_price": float(p[7]) if p[7] else 0.0})
    # 去重(同一股票多行)
    seen, uniq = set(), []
    for h in out:
        if h["code"] not in seen:
            seen.add(h["code"])
            uniq.append(h)
    return uniq


def load_state(today):
    if STATE.exists():
        try:
            s = json.loads(STATE.read_text())
            if s.get("date") == today:
                return s
        except Exception:
            pass
    return {"date": today, "stocks": {}}


def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trail", type=float, default=0.008, help="从日内最高回落比例触发(默认0.8%)")
    ap.add_argument("--min-gain", type=float, default=-1.0, help="日内涨幅下限(默认-1=不限制, 设1.0则只监控涨1%以上的)")
    ap.add_argument("--codes", default="", help="手动指定代码, 逗号分隔; 默认读持仓")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = datetime.now()
    if not args.dry_run:
        if now.weekday() >= 5 or not in_session(now):
            return 0

    holds = load_holdings()
    if args.codes:
        manual = [c.strip() for c in args.codes.split(",") if c.strip()]
        by_code = {h["code"]: h for h in holds}
        holds = [by_code.get(c) or {"code": c, "name": "", "buy_price": 0.0} for c in manual]
    if not holds:
        print("无持仓(position=1), 退出")
        return 0

    print("监控 %d 只: %s" % (len(holds), ", ".join(h["code"] for h in holds)))
    snap = snapshot([h["code"] for h in holds])
    today = now.strftime("%Y-%m-%d")
    state = load_state(today)
    alerts = []

    for h in holds:
        code = h["code"]
        q = snap.get(code)
        if not q or q["price"] <= 0:
            print("! %s 无行情, 跳过" % code)
            continue
        price, high = q["price"], q["high"]
        name = q["name"] or h["name"] or code
        st = state["stocks"].setdefault(code, {"peak": 0.0, "alerted": False, "name": name})
        st["peak"] = max(st["peak"], high, price)
        peak = st["peak"]
        dd = 1 - price / peak
        gain = q["pct"]
        print("%s(%s) 现价 %.3f 日内高 %.3f 回落 %.2f%% 涨幅 %+.2f%% peak=%.3f%s"
              % (name, code, price, high, dd * 100, gain * 100, peak,
                 "  [已提醒]" if st["alerted"] else ""))

        if st["alerted"]:
            continue
        if dd >= args.trail and (gain >= args.min_gain):
            bp = h["buy_price"]
            pnl = (price / bp - 1) * 100 if bp else 0.0
            alerts.append(
                "- **%s(%s)** 从日内高 %.3f 回落 **%.2f%%**\n"
                "  现价 %.3f | 日内涨幅 %+.2f%% | 成本 %.3f | 浮动 %+.2f%%"
                % (name, code, peak, dd * 100, price, gain * 100, bp, pnl))
            st["alerted"] = True

    if not args.dry_run:
        save_state(state)

    if not alerts:
        print("\n无触发(回落未达 %.1f%%)" % (args.trail * 100))
        return 0

    text = ("### 盘中高点回落提醒（建议卖出）\n\n" + "\n".join(alerts)
            + "\n\n时间: %s\n规则: 从日内最高回落 >= %.1f%% 即提醒, 每只每日一次"
            % (now.strftime("%Y-%m-%d %H:%M"), args.trail * 100))
    print("\n" + text)
    if args.dry_run:
        print("\n[dry-run] 跳过钉钉推送")
        return 0
    try:
        from tradingagents.notify.dingtalk import send_markdown
        ok = send_markdown("盘中高点回落提醒", text)
        print("钉钉推送:", "成功" if ok else "失败")
    except Exception as e:  # noqa: BLE001
        print("钉钉推送异常:", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
