#!/usr/bin/env python3
"""B(全市场Top1选股) + idle(闲置资金隔夜动量腿) SHADOW 监控 —— 不替代实盘, 只记录。

与 t0_monitor.py 平行运行, 但:
  1. 核心腿选股用 B 方案: 扫全市场 T0 ETF (含跨境/商品, 不限 T+0 交割, 不限品类),
     取当日涨幅 ≥ MIN_GAIN(3%) 的 Top1, 不 regime 过滤, 不 skip_choppy。
  2. 核心腿卖出用 TRIX(5,3)死叉; 09:40~11:05 内未触发则 11:05 收盘 fallback 平仓
     (对齐回测 simulate_exit("trix0940_cut") 窗口 09:40~11:05)。
     【2026-07-31 升级】由 hybrid(TRIX+追踪回落止盈) 改为纯 TRIX: 去偏差+保守成交价重验后,
     hybrid 的全部超额(+250~320pp)来自不可兑现成交价(穿价按精确止损价成交, 实盘只能在
     发现之后成交), 保守口径下 hybrid 全面劣于 TRIX; 而 TRIX 成交价稳健(保守≈乐观),
     且 B+确认+TRIX 在全4年/各子段均稳定优于 A+确认+TRIX(实盘), 故 SHADOW 切到 TRIX。
  3. idle 腿【2026-07-31 起已停用, IDLE_ENABLED=False】: 原为核心 14:45 未触发时 14:50 买
     当日最强 ≥IDLE_THR(1.0%) 隔夜持有、次日 14:50 平仓。改用无偏 5min 数据(tdx_5min_2y)
     重算后为负期望(100天 -16.85% / 390天 -4.67%, 12 组参数全负), 故关闭, 只保留核心 B 腿。
  4. 绝不真下单, 不读写实盘状态/流水。独立写入:
       STATE_FILE = ~/.tradingagents/rotation/b_idle_shadow_state.json
       JOURNAL   = ~/.tradingagents/rotation/b_idle_journal.jsonl

实盘对照 (t0_monitor.py, hybrid-A 选股 + TRIX 卖) 状态在 t0_monitor_state.json / t0_trade_journal.jsonl。

crontab (与实盘同窗口并行):
  45 14 * * 1-5  cd /path && python3 scripts/t0_b_idle_shadow.py --signal
  40 9  * * 1-5  cd /path && python3 scripts/t0_b_idle_shadow.py --sell-loop      # 09:40~11:05 每50秒 TRIX 卖出监控(对齐回测)
  49 14 * * 1-5  cd /path && python3 scripts/t0_b_idle_shadow.py --sell-check --idle-sell  # idle 次日14:50固定卖
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import t0_monitor as TM  # noqa: E402 复用行情/推送/TRIX 函数 (仅函数, 不读写其实盘状态)
from t0_monitor import (  # noqa: E402
    MIN_GAIN, SELL_RULE, SELL_BAR_LABEL, TRIX_PERIOD, TRIX_SIGNAL_PERIOD,
    TRIX_MIN_SELL, SELL_CUTOFF, SELL_CHECK_START, SELL_CHECK_END,
    REGIME_PROXY, SINA_INTERVAL,
    fetch_tencent_quotes, rank_t0_by_today_gain, fetch_regime,
    fetch_sell_kline, is_trading_day, is_sell_check_window,
    time_to_min, trix_death_cross_hit, send_dingtalk,
    resolve_exec_prices, strategy_header_lines, format_regime_block,
    get_all_t0_etfs, settlement_rule, scan_etf_universe,
)
from t0_monitor import (  # noqa: E402
    confirm_signal_gain,
)

STATE_DIR = Path.home() / ".tradingagents/rotation"
STATE_FILE = STATE_DIR / "b_idle_shadow_state.json"
TRADE_JOURNAL = STATE_DIR / "b_idle_journal.jsonl"

SIGNAL_TIME = "14:45"
IDLE_BUY = "14:50"
IDLE_SELL_HM = "14:50"
IDLE_THR = 1.0          # idle 动量选股阈值
# 双时点确认沿用 t0_monitor.CONFIRM_TIME(14:40) —— confirm_signal_gain 内部读该常量
CONFIRM_TIME = TM.CONFIRM_TIME

# ── idle 隔夜动量腿: 已停用 (2026-07-31) ──
# 原 +98.92% 结论建立在 aligned_live_4y 稀疏5min 上, 该缓存"先按当日收盘涨幅排序再抓TopK",
# 存在数据可得性前视偏差。改用无偏 tdx_5min_2y 重算后:
#   最近100天 -16.85%(21笔,胜29%) / 390天OOS -4.67%(135笔) ;
#   敏感性 4阈值×3卖点 共12组在100天窗口全负 → 无正期望, 不再记录该腿。
IDLE_ENABLED = False

# ── hybrid 卖点 (核心腿, 与回测 simulate_hybrid_v2 对齐) ──
HYBRID_SELL_END = SELL_CUTOFF  # "11:05", 核心腿 hybrid 窗口 09:40~11:05 (对齐回测 simulate_hybrid_v2 / +550.39%)
TRAIL_DROP_PCT = 0.5           # 追踪回落止盈阈值 (peak 回落 0.5%, 与 t0_monitor 一致)
SELL_LOOP_INTERVAL = 50        # --sell-loop 循环间隔秒

# ── Shadow 独立状态 (不碰实盘 state) ──
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "strategy": {"name": "B+idle SHADOW", "mode": "shadow", "core": "B全市场Top1", "idle": "隔夜动量"},
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


# ── B 选股: 全市场 Top1, 不 regime 过滤, 不 skip ──
def pick_b_candidate() -> dict | None:
    """扫全部 T0 ETF, 按腾讯实时涨幅排名, 取第一个 ≥ MIN_GAIN 的 (不限 T+0/品类)。"""
    quotes = fetch_tencent_quotes([e["code"] for e in get_all_t0_etfs()])
    ranked = rank_t0_by_today_gain(quotes, get_all_t0_etfs())
    for row in ranked:
        if row["today_gain"] < MIN_GAIN:
            continue
        # B 方案: 不限制 settlement_rule / 品类, 全市场择优
        return row
    return None


def pick_idle_candidate() -> dict | None:
    """idle 日 14:50 取全市场 T0 ETF 当日涨幅最强且 ≥ IDLE_THR 的。"""
    quotes = fetch_tencent_quotes([e["code"] for e in get_all_t0_etfs()])
    ranked = rank_t0_by_today_gain(quotes, get_all_t0_etfs())
    for row in ranked:
        if row["today_gain"] >= IDLE_THR:
            return row
    return None


def is_hybrid_sell_window(now: datetime | None = None) -> bool:
    """核心腿 hybrid 卖出监控窗口 09:40~11:05 (对齐回测 simulate_hybrid_v2 / +550.39%)。"""
    now = now or datetime.now()
    if not is_trading_day(now.date()):
        return False
    hm = now.hour * 60 + now.minute
    return time_to_min(SELL_CHECK_START) <= hm <= time_to_min(HYBRID_SELL_END)


def hybrid_trail_hit(
    buy_price: float,
    bars_today: list[dict],
    *,
    trail_drop_pct: float = TRAIL_DROP_PCT,
) -> tuple[bool, float, str]:
    """5分K 追踪回落止盈: 累计 peak, 若某根 K 的 low <= peak*(1-drop%) 则触发。
    返回 (是否触发, 触发价, 触发时间HH:MM)。与回测 simulate_hybrid_v2 对齐。"""
    peak = buy_price
    for b in bars_today:
        t = b.get("time", "")[:5]
        if not t or time_to_min(t) < time_to_min(SELL_CHECK_START):
            continue
        high = float(b.get("high") or 0)
        low = float(b.get("low") or 0)
        if high <= 0:
            continue
        peak = max(peak, high)
        if peak > buy_price and low <= peak * (1 - trail_drop_pct / 100):
            return True, peak * (1 - trail_drop_pct / 100), t
    return False, 0.0, ""


def run_signal(dry_run: bool = False) -> int:
    print("=== [SHADOW B+idle] 买入信号 ===")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    if not is_trading_day():
        print("非交易日，跳过")
        return 0

    state = load_state()
    today = date.today().isoformat()
    if state.get("last_signal_date") == today:
        print("今日已记录信号，跳过")
        return 0

    # ① 核心腿: B 选股 (双时点确认 14:45 + 14:50)
    top = pick_b_candidate()
    chosen = None
    if top:
        ok, _ = confirm_signal_gain(top)
        if ok:
            chosen = top
    if chosen:
        price = float(chosen["price"])
        state["position"] = {
            "etf": chosen["code"], "name": chosen["name"], "type": "core_B",
            "buy_price": price, "buy_date": today, "today_gain": chosen["today_gain"],
            "sold": False,
        }
        state["last_signal_date"] = today
        print(f">>> 核心 B 命中: {chosen['name']}({chosen['code']}) 涨幅{chosen['today_gain']:+.2f}% @ {price:.4f} [SHADOW]")
        lines = [
            "### [SHADOW B+idle] 核心腿买入信号触发",
            f"- 策略: B 全市场Top1 (≥{MIN_GAIN}%)",
            f"- 标的: **{chosen['name']}** ({chosen['code']}) 涨幅 {chosen['today_gain']:+.2f}%",
            f"- 计划买价: {price:.4f} | 卖点: TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD})死叉"
            f", {SELL_CHECK_START}~{HYBRID_SELL_END} 内未触发则 {HYBRID_SELL_END} 收盘平",
            "> ⚠️ 此为 SHADOW 影子策略，仅记录，不下单。",
        ]
        if not dry_run:
            save_state(state)
            append_journal({
                "signal_date": today, "signal_time": SIGNAL_TIME, "leg": "core_B",
                "etf": chosen["code"], "name": chosen["name"], "buy_price": price,
                "today_gain": chosen["today_gain"], "note": "SHADOW-未实盘下单",
            })
            send_dingtalk(f"[SHADOW] B核心信号{chosen['name']}", "\n".join(lines))
        else:
            print("\n".join(lines))
        return 0

    # ② idle 腿: 核心未命中 → 14:50 动量隔夜 (IDLE_ENABLED=False 时空仓)
    if IDLE_ENABLED:
        print(">>> 核心 B 未命中 (idle 日), 等待 14:50 动量腿")
    else:
        print(">>> 核心 B 未命中 (idle 日), idle 腿已停用(无偏数据下无正期望), 今日空仓")
    state["last_signal_date"] = today
    state["idle_pending"] = bool(IDLE_ENABLED)
    if not dry_run:
        save_state(state)
    return 0


def run_idle_buy(dry_run: bool = False) -> int:
    """14:50 执行 idle 动量腿 (仅当今日核心未命中)。"""
    print("=== [SHADOW B+idle] idle 动量腿 14:50 买入 ===")
    if not IDLE_ENABLED:
        print("idle 腿已停用(无偏数据回测无正期望: 100天 -16.85% / 390天 -4.67%)，跳过")
        return 0
    if not is_trading_day():
        print("非交易日，跳过")
        return 0
    state = load_state()
    today = date.today().isoformat()
    if not state.get("idle_pending") or state.get("last_signal_date") != today:
        print("今日无 idle 待办，跳过")
        return 0
    if state.get("position"):
        print("已有持仓，跳过 idle")
        return 0
    top = pick_idle_candidate()
    if not top:
        print("idle 无 ≥%.1f%% 标的，跳过" % IDLE_THR)
        state["idle_pending"] = False
        if not dry_run:
            save_state(state)
        return 0
    price = float(top["price"])
    state["position"] = {
        "etf": top["code"], "name": top["name"], "type": "idle_momentum",
        "buy_price": price, "buy_date": today, "today_gain": top["today_gain"],
        "sold": False,
    }
    state["idle_pending"] = False
    print(f">>> idle 命中: {top['name']}({top['code']}) 涨幅{top['today_gain']:+.2f}% @ {price:.4f} [SHADOW]")
    lines = [
        "### [SHADOW B+idle] idle 动量腿买入信号",
        f"- 标的: **{top['name']}** ({top['code']}) 当日涨幅 {top['today_gain']:+.2f}%",
        f"- 计划买价: {price:.4f} | 卖点: 次日 {IDLE_SELL_HM} 固定平仓",
        "> ⚠️ SHADOW 影子策略，仅记录，不下单。",
    ]
    if not dry_run:
        save_state(state)
        append_journal({
            "signal_date": today, "signal_time": IDLE_BUY, "leg": "idle_momentum",
            "etf": top["code"], "name": top["name"], "buy_price": price,
            "today_gain": top["today_gain"], "note": "SHADOW-未实盘下单",
        })
        send_dingtalk(f"[SHADOW] idle信号{top['name']}", "\n".join(lines))
    else:
        print("\n".join(lines))
    return 0


def run_sell_check(dry_run: bool = False, idle_sell: bool = False) -> int:
    print("=== [SHADOW B+idle] 卖出检查 ===")
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

    is_idle = pos.get("type") == "idle_momentum"
    now_hm = datetime.now().strftime("%H:%M")
    # 窗口: 核心腿 hybrid 09:40~11:05; idle 腿 14:49~14:50 (--idle-sell)
    if is_idle:
        if not idle_sell:
            print("idle 持仓, 需 --idle-sell 在 14:50 触发, 跳过本次")
            return 0
        if now_hm < IDLE_SELL_HM:
            print(f"idle 固定卖时点 {IDLE_SELL_HM}, 当前 {now_hm} 未到, 跳过")
            return 0
    elif not is_hybrid_sell_window():
        print(f"非核心卖出监控时段（{SELL_CHECK_START}~{HYBRID_SELL_END}），跳过")
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

    if is_idle:
        # idle 固定 14:50 平仓
        sell_hm = IDLE_SELL_HM
        prices = resolve_exec_prices(sym, bars_today, sell_hm, cur)
        sell_price = prices["primary"]
        ret_num = (sell_price - buy_price) / buy_price * 100 - 0.02  # 含费近似
        reason = "idle_fixed_1450"
        theory_price = sell_price  # idle 腿无回测对照口径, 理论=实际
        theory_src = "idle_1450_noref"
    else:
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
        elif now_hm >= HYBRID_SELL_END:
            # 11:05 收盘 fallback (time_sell): 窗口内无死叉则收盘平
            sell_hm = HYBRID_SELL_END
            prices = resolve_exec_prices(sym, bars_today, sell_hm, cur)
            sell_price = prices["primary"]
            ret_num = (sell_price - buy_price) / buy_price * 100 - 0.02
            reason = f"trix_time_sell_{HYBRID_SELL_END.replace(':', '')}"
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
        f"### [SHADOW B+idle] 卖出信号 | {pos['name']} ({etf})",
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
            "etf": etf, "name": pos["name"], "buy_price": buy_price,
            "sell_price": sell_price, "return_pct": ret_num, "sell_reason": reason,
            "theory_price": round(theory_price, 4), "theory_return_pct": theory_ret,
            "actual_price_src": actual_src, "slippage_pp": slippage_pp,
            "note": "SHADOW-未实盘平仓",
        })
        send_dingtalk(f"[SHADOW] 卖出{pos['name']} {ret_num:+.2f}%", "\n".join(lines))
    else:
        print("\n".join(lines))
    return 0


def run_sell_loop(dry_run: bool = False) -> int:
    """09:40~11:05 每50秒循环跑 run_sell_check (hybrid 卖点监控, 对齐回测)。
    持仓平仓后提前退出; 窗口结束自然退出。"""
    print(f"=== [SHADOW B+idle] 卖出监控循环 | 每 {SELL_LOOP_INTERVAL}s | "
          f"窗口 {SELL_CHECK_START}~{HYBRID_SELL_END} ===")
    while is_hybrid_sell_window():
        run_sell_check(dry_run=dry_run)
        state = load_state()
        if not state.get("position"):
            print("持仓已平, 退出循环")
            return 0
        if not is_hybrid_sell_window():
            break
        time.sleep(SELL_LOOP_INTERVAL)
    print("=== 卖出监控循环结束 ===")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="B+idle SHADOW 监控 (不替代实盘)")
    ap.add_argument("--signal", action="store_true", help="14:45 核心 B 信号")
    ap.add_argument("--idle-buy", action="store_true", help="14:50 idle 动量腿买入")
    ap.add_argument("--sell-check", action="store_true", help="卖出检查(单次)")
    ap.add_argument("--sell-loop", action="store_true",
                    help="09:40~11:05 每50秒循环检查(hybrid 卖点监控)")
    ap.add_argument("--idle-sell", action="store_true", help="idle 次日14:50固定卖")
    ap.add_argument("--dry-run", action="store_true", help="仅打印不推送")
    args = ap.parse_args()

    if args.signal:
        raise SystemExit(run_signal(dry_run=args.dry_run))
    if args.idle_buy:
        raise SystemExit(run_idle_buy(dry_run=args.dry_run))
    if args.sell_loop:
        raise SystemExit(run_sell_loop(dry_run=args.dry_run))
    if args.sell_check:
        raise SystemExit(run_sell_check(dry_run=args.dry_run, idle_sell=args.idle_sell))
    ap.print_help()


if __name__ == "__main__":
    main()
