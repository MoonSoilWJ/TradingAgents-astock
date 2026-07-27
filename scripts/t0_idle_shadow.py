#!/usr/bin/env python3
"""闲置窗口三段 Shadow — 仅日志，不改 t0_monitor 实盘基线。

回测定版（backtest_t0_idle_dual.py 100日 +275% 全链）:
  段1: 11:25 v6选 → 13:05 买 → 13:30 定时卖
  段2: 11:05 v6选 → 14:05 买 → 14:15 TRIX(5,3) 卖
  段3: 14:45/14:50 基线（仍由 t0_monitor.py 实盘）

Shadow 只模拟段1+段2，写入 JSONL，不推送买卖指令。

用法:
    python scripts/t0_idle_shadow.py --leg2-pick      # 11:05 段2选股
    python scripts/t0_idle_shadow.py --leg1-pick      # 11:25 段1选股
    python scripts/t0_idle_shadow.py --leg1-buy       # 13:05 段1模拟买
    python scripts/t0_idle_shadow.py --leg1-sell      # 13:30 段1模拟卖
    python scripts/t0_idle_shadow.py --leg2-buy       # 14:05 段2模拟买
    python scripts/t0_idle_shadow.py --leg2-sell      # 14:15 段2 TRIX卖
    python scripts/t0_idle_shadow.py --tick           # 按当前时刻自动执行一步
    python scripts/t0_idle_shadow.py --log --days 7

日志: ~/.tradingagents/rotation/t0_idle_shadow.jsonl
状态: ~/.tradingagents/rotation/t0_idle_shadow_state.json

定时: bash scripts/install_crontab.sh --install-idle-shadow
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from backtest_top1 import fetch_sina_kline  # noqa: E402
from backtest_t0_etf import (  # noqa: E402
    apply_net_return,
    compute_daily_data,
    fetch_5min_kline,
    normalize_5min_bars,
    price_at_time,
)
from backtest_t0_idle_window import sell_time_mode, sell_trix_mode  # noqa: E402
from backtest_t0_today1 import TRIX_PERIOD  # noqa: E402
from rotation_monitor import fetch_tencent_quotes  # noqa: E402
from rotation_v6 import partial_score_at  # noqa: E402
from t0_etf_list import (  # noqa: E402
    filter_t0_settlement,
    get_all_market_etf_lof,
    get_all_t0_etfs,
    sina_symbol_for,
)

try:
    from tradingagents.intraday.calendar import is_trading_day
except ImportError:
    def is_trading_day(day: date | None = None) -> bool:  # type: ignore[misc]
        day = day or date.today()
        return day.weekday() < 5

STATE_DIR = Path.home() / ".tradingagents" / "rotation"
STATE_FILE = STATE_DIR / "t0_idle_shadow_state.json"
SHADOW_LOG = STATE_DIR / "t0_idle_shadow.jsonl"

SHADOW_VERSION = "idle_triple_shadow_v6_allt0_20260727"
FEE_PCT = 0.03
MIN_GAIN_V6 = 2.0
SINA_INTERVAL = 0.25

LEG1 = {"id": "leg1", "signal": "11:25", "buy": "13:05", "sell": "13:30", "sell_mode": "time"}
LEG2 = {"id": "leg2", "signal": "11:05", "buy": "14:05", "sell": "14:15", "sell_mode": "trix"}

# --tick 时间窗口（±分钟）
TICK_WINDOWS = [
    ("leg2-pick", "11:03", "11:08"),
    ("leg1-pick", "11:23", "11:28"),
    ("leg1-buy", "13:03", "13:08"),
    ("leg1-sell", "13:28", "13:33"),
    ("leg2-buy", "14:03", "14:08"),
    ("leg2-sell", "14:13", "14:18"),
]


def load_pool() -> list[dict]:
    """全市场 T+0 池；失败则回退原 T+0 列表。"""
    try:
        raw = get_all_market_etf_lof()
        pool = filter_t0_settlement(raw)
        if len(pool) >= 50:
            return pool
    except Exception as e:
        print(f"WARN: 全市场池加载失败 {e}")
    return get_all_t0_etfs()


def load_state() -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return {"trade_date": "", "leg1": None, "leg2": None}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"trade_date": "", "leg1": None, "leg2": None}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_today(state: dict) -> dict:
    today = date.today().isoformat()
    if state.get("trade_date") != today:
        return {"trade_date": today, "leg1": None, "leg2": None}
    return state


def append_log(entry: dict) -> None:
    entry.setdefault("shadow_version", SHADOW_VERSION)
    entry.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
    with SHADOW_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _today_gain_from_quote(q: dict) -> float | None:
    price = float(q.get("price") or 0)
    last_close = float(q.get("last_close") or 0)
    if price <= 0 or last_close <= 0:
        return None
    gain = q.get("change_pct")
    if gain is None:
        return (price - last_close) / last_close * 100
    return float(gain)


def pick_v6_top1(pool: list[dict], signal_label: str) -> dict | None:
    """v6 partial 得分 TOP1，且当日涨幅 ≥ MIN_GAIN_V6。"""
    codes = [e["code"] for e in pool]
    quotes = fetch_tencent_quotes(codes)
    if not quotes:
        print("ERROR: 无法获取行情")
        return None

    pre: list[tuple[float, dict]] = []
    for etf in pool:
        code = etf["code"]
        q = quotes.get(code)
        if not q:
            continue
        gain = _today_gain_from_quote(q)
        if gain is None or gain < MIN_GAIN_V6:
            continue
        pre.append((gain, {**etf, "today_gain": gain, "quote": q}))
    pre.sort(key=lambda x: x[0], reverse=True)
    pre = pre[:25]

    cands: list[tuple[float, float, dict]] = []
    for gain, etf in pre:
        q = etf["quote"]
        sym = etf.get("sina_symbol") or sina_symbol_for(etf["code"])
        daily = fetch_sina_kline(sym, datalen=45)
        time.sleep(SINA_INTERVAL)
        if not daily or len(daily) < 5:
            continue
        returns = compute_daily_data(daily)
        if not returns:
            continue
        idx = len(returns) - 1
        partial_close = float(q.get("price") or returns[idx]["close"])
        partial_vol = float(q.get("volume") or returns[idx].get("volume") or 0)
        score = partial_score_at(returns, idx, partial_close, partial_vol)
        if score <= 0:
            continue
        cands.append((score, gain, etf))

    if not cands:
        return None
    cands.sort(key=lambda x: x[0], reverse=True)
    score, gain, etf = cands[0]
    live_name = (etf["quote"].get("name") or "").strip()
    return {
        "code": etf["code"],
        "name": live_name or etf.get("name") or etf.get("etf_name") or etf["code"],
        "sina_symbol": etf.get("sina_symbol") or sina_symbol_for(etf["code"]),
        "v6_score": round(score, 4),
        "today_gain": round(gain, 2),
        "signal_time": signal_label,
        "pick_ts": datetime.now().isoformat(timespec="seconds"),
    }


def quote_price(code: str, pool: list[dict]) -> float | None:
    q = fetch_tencent_quotes([code]).get(code)
    if not q:
        return None
    px = float(q.get("price") or q.get("now") or 0)
    return px if px > 0 else None


def fetch_today_5min(sina_symbol: str) -> list[dict]:
    klines = fetch_5min_kline(sina_symbol, datalen=500)
    if not klines:
        return []
    grouped = normalize_5min_bars(klines)
    today = date.today().isoformat()
    return grouped.get(today, [])


def run_pick(state: dict, leg: dict, dry_run: bool) -> int:
    leg_id = leg["id"]
    slot = state.get(leg_id)
    if slot and slot.get("code") and not slot.get("closed"):
        print(f"  [{leg_id}] 已有选股 {slot['code']} {slot.get('name')}")
        return 0

    print(f">>> [{leg_id}] v6 选股 @ {leg['signal']} (涨幅≥{MIN_GAIN_V6}%)...")
    pool = load_pool()
    print(f"    扫描池 {len(pool)} 只 T+0")
    picked = pick_v6_top1(pool, leg["signal"])
    if not picked:
        print(f"    无满足条件标的")
        append_log({"event": "pick_skip", "leg": leg_id, "signal": leg["signal"]})
        return 0

    state[leg_id] = {**picked, "leg": leg_id, "buy_price": None, "sell_price": None, "closed": False}
    save_state(state)
    append_log({"event": "pick", "leg": leg_id, **picked})
    print(
        f"    TOP1: {picked['code']} {picked['name']} "
        f"v6={picked['v6_score']:.2f} 涨{picked['today_gain']:+.2f}%"
    )
    if dry_run:
        print("    [--dry-run]")
    return 0


def run_buy(state: dict, leg: dict, dry_run: bool) -> int:
    leg_id = leg["id"]
    slot = state.get(leg_id)
    if not slot or not slot.get("code"):
        print(f"  [{leg_id}] 无待买标的，跳过")
        return 0
    if slot.get("buy_price"):
        print(f"  [{leg_id}] 已记录买入 @ {slot['buy_price']}")
        return 0

    px = quote_price(slot["code"], load_pool())
    if not px:
        bars = fetch_today_5min(slot["sina_symbol"])
        px = price_at_time(bars, leg["buy"])
    if not px or px <= 0:
        print(f"  [{leg_id}] 无法获取 {leg['buy']} 买价")
        return 1

    slot["buy_price"] = round(px, 4)
    slot["buy_time"] = leg["buy"]
    slot["buy_ts"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    append_log({"event": "buy", "leg": leg_id, "code": slot["code"], "name": slot["name"],
                "buy_time": leg["buy"], "buy_price": slot["buy_price"]})
    print(f"    [{leg_id}] 模拟买 {slot['code']} @ {slot['buy_price']:.4f} ({leg['buy']})")
    return 0


def run_sell(state: dict, leg: dict, dry_run: bool) -> int:
    leg_id = leg["id"]
    slot = state.get(leg_id)
    if not slot or not slot.get("code"):
        print(f"  [{leg_id}] 无持仓，跳过")
        return 0
    if slot.get("closed"):
        print(f"  [{leg_id}] 已平仓 ret={slot.get('return_pct')}%")
        return 0
    if not slot.get("buy_price"):
        print(f"  [{leg_id}] 未记录买入价，跳过")
        return 0

    buy_price = float(slot["buy_price"])
    bars = fetch_today_5min(slot["sina_symbol"])
    sell_reason = "time_sell"
    ret: float
    sell_price: float

    if leg["sell_mode"] == "trix":
        out = sell_trix_mode(bars, slot.get("buy_time") or leg["buy"], leg["sell"], buy_price, FEE_PCT)
    else:
        out = sell_time_mode(bars, slot.get("buy_time") or leg["buy"], leg["sell"], buy_price, FEE_PCT)

    if out:
        ret, sell_reason = out
        sell_price = price_at_time(bars, leg["sell"]) if bars else None
        if not sell_price or sell_price <= 0:
            sell_price = float(bars[-1]["close"]) if bars else buy_price * (1 + ret / 100)
    else:
        sell_price = quote_price(slot["code"], load_pool())
        if not sell_price:
            print(f"  [{leg_id}] 无法定价卖出")
            return 1
        ret = apply_net_return(buy_price, sell_price, FEE_PCT)
        sell_reason = "quote_fallback"

    slot["sell_price"] = round(float(sell_price), 4)
    slot["sell_time"] = leg["sell"]
    slot["sell_reason"] = sell_reason
    slot["return_pct"] = round(ret, 4)
    slot["closed"] = True
    slot["sell_ts"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    append_log({
        "event": "sell", "leg": leg_id, "code": slot["code"], "name": slot["name"],
        "buy_price": buy_price, "sell_price": sell_price, "sell_time": leg["sell"],
        "sell_reason": sell_reason, "return_pct": slot["return_pct"],
    })
    print(
        f"    [{leg_id}] 模拟卖 {slot['code']} @ ~{sell_price:.4f} "
        f"→ {ret:+.2f}% ({sell_reason})"
    )
    return 0


def print_log(days: int) -> int:
    if not SHADOW_LOG.exists():
        print(f"暂无日志: {SHADOW_LOG}")
        return 0
    cutoff = date.today().toordinal() - days
    rows = []
    for line in SHADOW_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    recent = [r for r in rows if r.get("ts", "")[:10] >= date.fromordinal(cutoff).isoformat()]
    sells = [r for r in recent if r.get("event") == "sell"]
    print(f"=== 闲置 Shadow 日志 最近 {days} 日 ===")
    print(f"文件: {SHADOW_LOG}")
    print(f"记录 {len(recent)} 条 | 平仓 {len(sells)} 笔\n")
    if sells:
        total = 1.0
        for s in sells:
            total *= 1 + s.get("return_pct", 0) / 100
        print(f"Shadow 平仓复利: {(total - 1) * 100:+.2f}% ({len(sells)} 笔)\n")
    for r in recent[-30:]:
        ev = r.get("event", "?")
        leg = r.get("leg", "")
        if ev == "sell":
            print(
                f"  {r.get('ts','')[:16]} [{leg}] {r.get('code')} {r.get('return_pct'):+.2f}% "
                f"{r.get('sell_reason')} | 买{r.get('buy_price')}→卖{r.get('sell_price')}"
            )
        elif ev == "pick":
            print(
                f"  {r.get('ts','')[:16]} [{leg}] 选 {r.get('code')} {r.get('name')} "
                f"v6={r.get('v6_score')} 涨{r.get('today_gain'):+.1f}%"
            )
        else:
            print(f"  {r.get('ts','')[:16]} [{leg}] {ev} {r.get('code', '')}")
    return 0


def run_tick(dry_run: bool) -> int:
    now = datetime.now().strftime("%H:%M")
    nm = int(now[:2]) * 60 + int(now[3:5])

    def in_win(start: str, end: str) -> bool:
        sm = int(start[:2]) * 60 + int(start[3:5])
        em = int(end[:2]) * 60 + int(end[3:5])
        return sm <= nm <= em

    dispatch = {
        "leg2-pick": (LEG2, run_pick),
        "leg1-pick": (LEG1, run_pick),
        "leg1-buy": (LEG1, run_buy),
        "leg1-sell": (LEG1, run_sell),
        "leg2-buy": (LEG2, run_buy),
        "leg2-sell": (LEG2, run_sell),
    }
    for action, start, end in TICK_WINDOWS:
        if in_win(start, end):
            leg, fn = dispatch[action]
            state = ensure_today(load_state())
            print(f"=== tick → {action} ({start}~{end}) ===")
            return fn(state, leg, dry_run)
    print(f"当前 {now} 不在 shadow 窗口内")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="闲置窗口段1+段2 Shadow（不改实盘）")
    parser.add_argument("--leg1-pick", action="store_true")
    parser.add_argument("--leg1-buy", action="store_true")
    parser.add_argument("--leg1-sell", action="store_true")
    parser.add_argument("--leg2-pick", action="store_true")
    parser.add_argument("--leg2-buy", action="store_true")
    parser.add_argument("--leg2-sell", action="store_true")
    parser.add_argument("--tick", action="store_true", help="按当前时间自动执行")
    parser.add_argument("--log", action="store_true", help="查看 shadow 日志")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.log:
        return print_log(args.days)

    if not is_trading_day():
        print("非交易日，跳过")
        return 0

    actions = [
        (args.leg2_pick, LEG2, run_pick),
        (args.leg1_pick, LEG1, run_pick),
        (args.leg1_buy, LEG1, run_buy),
        (args.leg1_sell, LEG1, run_sell),
        (args.leg2_buy, LEG2, run_buy),
        (args.leg2_sell, LEG2, run_sell),
    ]
    if args.tick:
        return run_tick(args.dry_run)

    picked = [(leg, fn) for flag, leg, fn in actions if flag]
    if len(picked) != 1:
        print("请指定一个动作: --leg1-pick|--leg1-buy|--leg1-sell|--leg2-pick|--leg2-buy|--leg2-sell|--tick|--log")
        return 1

    leg, fn = picked[0]
    state = ensure_today(load_state())
    print(f"=== T+0 闲置 Shadow | {leg['id']} | {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    print(f"版本: {SHADOW_VERSION} | 仅日志，实盘基线不受影响\n")
    return fn(state, leg, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
