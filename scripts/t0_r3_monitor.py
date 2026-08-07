#!/usr/bin/env python3
"""R3(月度轮动质量池) SHADOW 监控 —— 不替代实盘, 只记录。

与 t0_monitor.py / t0_b_idle_shadow.py 平行运行, 选股用 R3 月度轮动攻击池:
  - 候选 = jq_attack_pools.JQ_ATTACK_POOLS["R3"][使用月]; 键=使用月(当月交易),
    值=上月末 pool_as_of (严格无未来函数, 无前视), 取 <= 当前月的最后一个非空月份。
    code 由聚宽后缀(513600.XSHG / 159329.XSHE)转本地6位+新浪symbol(sh513600/sz159329)。
  - 取当日涨幅 ≥ MIN_GAIN(3%) 的 Top1 (drop_sector 已在池生成时应用)。
  - 卖出用 TRIX(5,3)死叉; 09:40~11:05 内未触发则 11:05 收盘 fallback (对齐回测
    simulate_exit('trix0940_cut'), 全4年 R3 等价 +613% 量级卖点)。
  - 绝不真下单, 不读写实盘 / SHADOW B 状态。独立写入:
      STATE_FILE = ~/.tradingagents/rotation/r3_shadow_state.json
      JOURNAL   = ~/.tradingagents/rotation/r3_journal.jsonl

crontab (与实盘同窗口并行):
  45 14 * * 1-5  cd /path && python3 scripts/t0_r3_monitor.py --signal
  40 9  * * 1-5  cd /path && python3 scripts/t0_r3_monitor.py --sell-loop
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import jq_attack_pools  # noqa: E402 月度轮动攻击池 (R1~R6 内联字面量)
import t0_monitor as TM  # noqa: E402 复用行情/推送/TRIX 函数
from sync_web import sync_to_web  # noqa: E402 买卖信号触发后同步到 Web
from t0_monitor import (  # noqa: E402
    MIN_GAIN, SELL_BAR_LABEL, TRIX_PERIOD, TRIX_SIGNAL_PERIOD,
    TRIX_MIN_SELL, SELL_CUTOFF, SELL_CHECK_START, SELL_CHECK_END,
    SINA_INTERVAL,
    fetch_tencent_quotes, rank_t0_by_today_gain, fetch_sell_kline,
    is_trading_day, time_to_min, trix_death_cross_hit, send_dingtalk,
    resolve_exec_prices, get_all_t0_etfs, scan_etf_universe,
    confirm_signal_gain,
)

STATE_DIR = Path.home() / ".tradingagents/rotation"
STATE_FILE = STATE_DIR / "r3_shadow_state.json"
TRADE_JOURNAL = STATE_DIR / "r3_journal.jsonl"

SIGNAL_TIME = "14:45"
BUY_TIME = "14:50"
SELL_LOOP_INTERVAL = 50
CONFIRM_TIME = TM.CONFIRM_TIME  # 14:40 双时点确认


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "strategy": {"name": "R3 月度轮动 SHADOW", "mode": "shadow", "pick": "月度轮动池Top1"},
        "position": None,
        "last_signal_date": None,
        "updated_at": None,
    }


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_journal(rec: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with TRADE_JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def r3_universe_etfs() -> list[dict]:
    """取 R3 月度轮动池 (<= 当前月最近非空月), 转本地 etf dict 列表。"""
    pool = jq_attack_pools.JQ_ATTACK_POOLS.get("R3", {})
    ym_now = datetime.now().strftime("%Y-%m")
    valid = [k for k in pool if k <= ym_now and pool[k]]
    if not valid:
        return []
    ym = max(valid)
    out = []
    for c in pool[ym]:
        code, mkt = c.split(".")
        prefix = "sh" if mkt == "XSHG" else "sz"
        out.append({"code": code, "sina_symbol": prefix + code, "name": code, "pool_month": ym})
    return out


def pick_r3_candidate() -> dict | None:
    """扫 R3 月度轮动池, 按腾讯实时涨幅排名, 取第一个 ≥ MIN_GAIN(3%) 的 Top1。"""
    etfs = r3_universe_etfs()
    if not etfs:
        return None
    quotes = fetch_tencent_quotes([e["code"] for e in etfs])
    ranked = rank_t0_by_today_gain(quotes, etfs)
    for row in ranked:
        if row["today_gain"] < MIN_GAIN:
            continue
        return row
    return None


def is_r3_sell_window(now: datetime | None = None) -> bool:
    """R3 卖出监控窗口 09:40~11:05 (对齐回测 simulate_exit('trix0940_cut'))。"""
    now = now or datetime.now()
    if not is_trading_day(now.date()):
        return False
    hm = now.hour * 60 + now.minute
    return time_to_min(SELL_CHECK_START) <= hm <= time_to_min(SELL_CUTOFF)


def run_signal(dry_run: bool = False) -> int:
    print("=== [SHADOW R3] 买入信号 ===")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    if not is_trading_day():
        print("非交易日，跳过")
        return 0

    state = load_state()
    today = date.today().isoformat()
    if state.get("last_signal_date") == today:
        print("今日已记录信号，跳过")
        return 0

    etfs = r3_universe_etfs()
    print(f">>> R3 月度轮动池: {etfs[0]['pool_month'] if etfs else 'N/A'} 月, 候选 {len(etfs)} 只")

    top = pick_r3_candidate()
    chosen = None
    if top:
        ok, _ = confirm_signal_gain(top)
        if ok:
            chosen = top
    if chosen:
        price = float(chosen["price"])
        state["position"] = {
            "etf": chosen["code"], "name": chosen["name"], "type": "R3_月度轮动",
            "buy_price": price, "buy_date": today, "today_gain": chosen["today_gain"],
            "signal_time": SIGNAL_TIME, "buy_time": BUY_TIME,
            "sold": False,
        }
        state["last_signal_date"] = today
        print(f">>> R3 命中: {chosen['name']}({chosen['code']}) 涨幅{chosen['today_gain']:+.2f}% @ {price:.4f} [SHADOW]")
        lines = [
            "### [SHADOW R3] 核心腿买入信号触发",
            f"- 策略: R3 月度轮动池 Top1 (≥{MIN_GAIN}%, drop_sector 已应用)",
            f"- 标的: **{chosen['name']}** ({chosen['code']}) 涨幅 {chosen['today_gain']:+.2f}%",
            f"- 计划买价: {price:.4f} | 卖点: TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD})死叉"
            f", {SELL_CHECK_START}~{SELL_CUTOFF} 内未触发则 {SELL_CUTOFF} 收盘平",
            "> ⚠️ 此为 SHADOW 影子策略，仅记录，不下单。",
        ]
        if not dry_run:
            save_state(state)
            append_journal({
                "signal_date": today, "signal_time": SIGNAL_TIME, "buy_time": BUY_TIME,
                "leg": "R3_月度轮动",
                "etf": chosen["code"], "name": chosen["name"], "buy_price": price,
                "today_gain": chosen["today_gain"], "note": "SHADOW-未实盘下单",
            })
            send_dingtalk(f"[SHADOW R3] 信号{chosen['name']}", "\n".join(lines))
            sync_to_web()
        else:
            print("\n".join(lines))
        return 0

    print(">>> R3 未命中 (当月轮动池无 ≥%.1f%% 标的), 今日空仓" % MIN_GAIN)
    state["last_signal_date"] = today
    if not dry_run:
        save_state(state)
    return 0


def run_sell_check(dry_run: bool = False) -> int:
    print("=== [SHADOW R3] 卖出检查 ===")
    if not is_trading_day():
        print("非交易日，跳过")
        return 0
    state = load_state()
    pos = state.get("position")
    if not pos or pos.get("sold"):
        print("无持仓，跳过")
        return 0
    if pos.get("buy_date") == date.today().isoformat():
        print("买入当日不可按隔夜规则卖出，跳过")
        return 0

    now_hm = datetime.now().strftime("%H:%M")
    if not is_r3_sell_window():
        print(f"非卖出监控时段（{SELL_CHECK_START}~{SELL_CUTOFF}），跳过")
        return 0

    etf = pos["etf"]
    buy_price = float(pos["buy_price"])
    etf_list, _ = scan_etf_universe()
    etf_info = next((e for e in etf_list if e["code"] == etf), None)
    if not etf_info:
        etf_info = next((e for e in get_all_t0_etfs() if e["code"] == etf), None)
    if not etf_info:
        print(f"ERROR: 未知 ETF {etf}")
        return 1
    sym = etf_info["sina_symbol"]
    buy_date = pos["buy_date"]
    today = date.today().isoformat()

    print(f">>> 监控 {pos['name']} ({etf}) 买入@{buy_price:.4f} ({buy_date}) [SHADOW]")
    by_day = fetch_sell_kline(sym)
    time.sleep(SINA_INTERVAL)
    if not by_day:
        print(f"ERROR: 无法获取 {SELL_BAR_LABEL}")
        return 1
    bars_today = by_day.get(today, [])
    if not bars_today:
        if time_to_min(now_hm) < time_to_min(SELL_CUTOFF):
            print(f"WARN: 当日 {SELL_BAR_LABEL} 尚未就绪, 下轮重试")
            return 0
        print(f"ERROR: 当日 {SELL_BAR_LABEL} 为空且已过卖出截止")
        return 1

    q = fetch_tencent_quotes([etf]).get(etf, {})
    cur = q.get("price", 0)
    float_ret = (cur - buy_price) / buy_price * 100 if cur and buy_price else 0

    # TRIX 卖点: TRIX(5,3)死叉; 09:40~11:05 内未触发则 11:05 收盘 fallback 平仓
    # (对齐回测 simulate_exit("trix0940_cut"))
    # 第2返回值 = 死叉当根5min收盘价 = 回测 simulate_trix_cross_after 成交口径(理论价)
    hit_trix, theory_dc_price, trix_time_str, _ = trix_death_cross_hit(
        buy_price, by_day.get(buy_date, []), bars_today, now_hm,
    )
    trix_hm = ""
    if hit_trix:
        trix_hm = trix_time_str.split(" ")[-1][:5] if " " in trix_time_str else trix_time_str[:5]

    if hit_trix:
        sell_hm = trix_hm
        prices = resolve_exec_prices(sym, bars_today, sell_hm, cur)
        sell_price = prices["primary"]
        ret_num = (sell_price - buy_price) / buy_price * 100 - 0.02
        reason = "trix_death_cross"
        theory_price = theory_dc_price
        theory_src = "trix_dc_5m_close"
    elif now_hm >= SELL_CUTOFF:
        # 11:05 收盘 fallback: 窗口内无死叉则收盘平
        sell_hm = SELL_CUTOFF
        prices = resolve_exec_prices(sym, bars_today, sell_hm, cur)
        sell_price = prices["primary"]
        ret_num = (sell_price - buy_price) / buy_price * 100 - 0.02
        reason = f"trix_time_sell_{SELL_CUTOFF.replace(':', '')}"
        # 理论价 = 截止时刻最近已完成5minK收盘(回测 window[-1].close 的实时近似)
        theory_price = prices.get("px_5m") or sell_price
        theory_src = "time_sell_5m_close"
    else:
        print(f"TRIX 未触发, 浮盈 {float_ret:+.2f}% [SHADOW 继续持有]")
        return 0

    ret_num = round(ret_num, 4)
    # 理论价(回测口径) vs 实际价(1min/实时), 量化滑点
    theory_ret = round((theory_price - buy_price) / buy_price * 100 - 0.02, 4)
    slippage_pp = round(ret_num - theory_ret, 4)
    actual_src = prices.get("source", "")
    print(f">>> 卖出触发 @{sell_hm} 实际价 {sell_price:.4f}({actual_src}) 收益 {ret_num:+.2f}% | "
          f"理论价 {theory_price:.4f}({theory_src}) 收益 {theory_ret:+.2f}% | 滑点 {slippage_pp:+.2f}pp [SHADOW]")
    pos["sold"] = True
    pos["sell_date"] = today
    pos["sell_price"] = sell_price
    pos["return_pct"] = ret_num
    pos["sell_reason"] = reason
    pos["theory_price"] = round(theory_price, 4)
    pos["theory_return_pct"] = theory_ret
    pos["actual_price_src"] = actual_src
    pos["slippage_pp"] = slippage_pp
    lines = [
        f"### [SHADOW R3] 卖出信号 | {pos['name']} ({etf})",
        f"- 类型: {pos.get('type')} | 卖点: {reason}",
        f"- 买入: {buy_date} @ {buy_price:.4f} | 卖出: {today} @ {sell_price:.4f}({actual_src})",
        f"- **影子收益: {ret_num:+.2f}%** (未实盘下单)",
        f"- 理论价(回测口径): {theory_price:.4f}({theory_src}) 收益 {theory_ret:+.2f}% | 滑点 {slippage_pp:+.2f}pp",
    ]
    if not dry_run:
        state["position"] = None
        save_state(state)
        append_journal({
            "sell_date": today, "sell_time": sell_hm, "leg": pos.get("type"),
            "signal_time": pos.get("signal_time", ""), "buy_time": pos.get("buy_time", ""),
            "etf": etf, "name": pos["name"], "buy_price": buy_price,
            "sell_price": sell_price, "return_pct": ret_num, "sell_reason": reason,
            "theory_price": round(theory_price, 4), "theory_return_pct": theory_ret,
            "actual_price_src": actual_src, "slippage_pp": slippage_pp,
            "note": "SHADOW-未实盘平仓",
        })
        send_dingtalk(f"[SHADOW R3] 卖出{pos['name']} {ret_num:+.2f}%", "\n".join(lines))
        sync_to_web()
    else:
        print("\n".join(lines))
    return 0


def run_sell_loop(dry_run: bool = False) -> int:
    """09:40~11:05 每50秒循环跑 run_sell_check (TRIX 卖点监控, 对齐回测)。
    持仓平仓后提前退出; 窗口结束自然退出。"""
    print(f"=== [SHADOW R3] 卖出监控循环 | 每 {SELL_LOOP_INTERVAL}s | "
          f"窗口 {SELL_CHECK_START}~{SELL_CUTOFF} ===")
    while is_r3_sell_window():
        run_sell_check(dry_run=dry_run)
        state = load_state()
        if not state.get("position"):
            print("持仓已平, 退出循环")
            return 0
        if not is_r3_sell_window():
            break
        time.sleep(SELL_LOOP_INTERVAL)
    print("=== 卖出监控循环结束 ===")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="R3 月度轮动 SHADOW 监控 (不替代实盘)")
    ap.add_argument("--signal", action="store_true", help="14:45 R3 月度轮动信号")
    ap.add_argument("--sell-check", action="store_true", help="卖出检查(单次)")
    ap.add_argument("--sell-loop", action="store_true",
                    help="09:40~11:05 每50秒循环检查(TRIX 卖点监控)")
    ap.add_argument("--dry-run", action="store_true", help="仅打印不推送")
    args = ap.parse_args()

    if args.signal:
        raise SystemExit(run_signal(dry_run=args.dry_run))
    if args.sell_loop:
        raise SystemExit(run_sell_loop(dry_run=args.dry_run))
    if args.sell_check:
        raise SystemExit(run_sell_check(dry_run=args.dry_run))
    ap.print_help()


if __name__ == "__main__":
    main()
