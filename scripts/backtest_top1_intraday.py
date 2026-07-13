#!/usr/bin/env python3
"""板块轮动 TOP1 盘中策略回测

策略规则：
1. T 日 14:50 跑 v6 选 TOP1 ETF，记录基准价（14:50 close）
2. 买入条件（观察期 T 日 14:55~15:00 + T+1 全天）：
   - 追涨：价 ≥ 基准 × 1.003 → 按触发价买
   - 抄底：价 ≤ 基准 × 0.98 后反弹 ≥ 0.3% → 按触发价买
3. 卖出（买入后立即监控，先到先触发）：
   - 止损：价 ≤ 买入价 × 0.995 → 卖
   - 追踪止盈：涨幅 ≥ +3% 后从最高点回落 ≥ 0.5% → 卖
   - 时间止损：T+1 收盘卖（无论盈亏）
4. 未买入：T+1 收盘未触发则放弃本次信号

数据：
- 日 K（scale=240）算 v6 得分
- 5 分钟 K（scale=5）还原盘中买卖触发

用法:
    python scripts/backtest_top1_intraday.py
    python scripts/backtest_top1_intraday.py --lookback 30 --slippage 0.1 --fee 0.05
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROXY = os.environ.get("ROTATION_PROXY", "http://127.0.0.1:7890")
SINA_INTERVAL = 0.3
TIMEOUT = 15

# 复用 backtest_top1.py 的 ETF 映射和公式
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_top1 import (
    SCORE_WINDOW, VOL_THRESHOLD, VOL_AVG_PERIOD, VOL_BASE,
    fetch_sina_kline, compute_daily_data, compute_v6_score,
    _calc_stats, _print_stats,
)
from sector_etf_map import etf_to_sina_symbol, load_pingan_sectors  # noqa: E402

# ── 5 分钟 K 线 ──────────────────────────────────────

def fetch_sina_5min_kline(symbol: str, datalen: int = 800) -> list[dict]:
    """拉 5 分钟 K 线。datalen 最大约 800 根（约 4 个月）。"""
    url = (
        f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=5&ma=no&datalen={datalen}"
    )
    raw = _curl_get(url)
    time.sleep(SINA_INTERVAL)
    if not raw or raw.strip() in ("null", "", "[]"):
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def _curl_get(url: str) -> str:
    for use_proxy in [False, True]:
        cmd = ["curl", "-s", "--connect-timeout", str(TIMEOUT)]
        if use_proxy:
            cmd += ["-x", PROXY]
        cmd.append(url)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 10)
            for enc in ["gbk", "utf-8"]:
                try:
                    text = r.stdout.decode(enc)
                    if text and len(text) > 10:
                        return text
                except (UnicodeDecodeError, AttributeError):
                    continue
        except subprocess.TimeoutExpired:
            continue
    return ""


def etf_to_sina_symbol(etf_code: str) -> str:
    if etf_code.startswith("5"):
        return f"sh{etf_code}"
    return f"sz{etf_code}"


# ── 5 分钟 K 线分组 ─────────────────────────────────

def group_5min_by_day(klines_5min: list[dict]) -> dict[str, list[dict]]:
    """按交易日分组，返回 {date: [bars]}，每根 bar 含 datetime/day/time/open/high/low/close/volume。"""
    by_day: dict[str, list[dict]] = {}
    for k in klines_5min:
        dt = k.get("day", "")
        if not dt:
            continue
        # 格式 "2026-07-13 10:05:00"
        parts = dt.split(" ")
        day = parts[0]
        t = parts[1] if len(parts) > 1 else "00:00:00"
        bar = {
            "datetime": dt,
            "day": day,
            "time": t,
            "open": float(k.get("open", 0)),
            "high": float(k.get("high", 0)),
            "low": float(k.get("low", 0)),
            "close": float(k.get("close", 0)),
            "volume": float(k.get("volume", 0)),
        }
        by_day.setdefault(day, []).append(bar)
    # 每天按时间排序
    for day in by_day:
        by_day[day].sort(key=lambda b: b["time"])
    return by_day


def find_1450_bar(bars: list[dict]) -> dict | None:
    """找 14:50 那根 5 分钟 K（收盘 14:55）。"""
    for b in bars:
        # 14:50:00 ~ 14:55:00 这根 K
        if b["time"].startswith("14:50"):
            return b
    return None


def bars_after_1450(bars: list[dict]) -> list[dict]:
    """返回 14:50 之后的 bars（含 14:50 当根，用于后续判断）。"""
    result = []
    started = False
    for b in bars:
        if b["time"].startswith("14:50"):
            started = True
        if started:
            result.append(b)
    return result


# ── 盘中买卖触发 ─────────────────────────────────────

def check_buy_trigger(
    bars: list[dict],
    baseline_price: float,
    buy_up_pct: float = 0.3,
    buy_down_pct: float = 2.0,
    buy_rebound_pct: float = 0.3,
) -> tuple[float | None, str, str]:
    """检查买入触发。

    Args:
        bars: 5 分钟 K 线列表（按时间排序）
        baseline_price: T 日 14:50 close（基准价）
        buy_up_pct: 追涨触发涨幅 %（0.3）
        buy_down_pct: 抄底触发跌幅 %（2.0）
        buy_rebound_pct: 抄底回弹幅度 %（0.3）

    Returns:
        (buy_price, trigger_type, trigger_time)
        buy_price=None 表示未触发
    """
    up_threshold = baseline_price * (1 + buy_up_pct / 100)
    down_threshold = baseline_price * (1 - buy_down_pct / 100)

    in_dip = False  # 是否已触发过下跌2%
    dip_low = baseline_price  # 下跌过程中的最低价

    for b in bars:
        # 先检查追涨：high 触及 up_threshold
        if b["high"] >= up_threshold:
            # 触发追涨，按 up_threshold 成交（近似）
            return up_threshold, "追涨", b["datetime"]

        # 检查抄底：low 触及 down_threshold
        if b["low"] <= down_threshold:
            in_dip = True
            if b["low"] < dip_low:
                dip_low = b["low"]

        # 如果已在下跌状态，检查回弹
        if in_dip:
            # 回弹基准：从 dip_low 回弹 buy_rebound_pct%
            rebound_threshold = dip_low * (1 + buy_rebound_pct / 100)
            if b["high"] >= rebound_threshold:
                return rebound_threshold, "抄底", b["datetime"]

    return None, "未触发", ""


def _fill_on_touch(bar: dict, trigger_price: float) -> float:
    """触及触发价时的成交价：T+1 跳空穿越用开盘价，否则用触发价。

    例：止损 1.468，开盘 1.330 → 按 1.330 成交（不能假设仍在 1.468 止损）。
    """
    if bar["open"] <= trigger_price:
        return bar["open"]
    return trigger_price


def check_sell_trigger(
    bars: list[dict],
    buy_price: float,
    buy_bar_idx: int,
    stop_loss_pct: float = -0.5,
    trail_trigger_pct: float = 3.0,
    trail_drop_pct: float = 0.5,
) -> tuple[float, str, str]:
    """检查 T+1 卖出触发（调用方仅传入卖出日 5 分 K）。

    止损/追踪：low 触及触发价时，若开盘价已穿越触发价则按开盘价成交。
    """
    stop_loss_price = buy_price * (1 + stop_loss_pct / 100)
    trail_trigger_price = buy_price * (1 + trail_trigger_pct / 100)

    max_high_after_trigger = buy_price
    trailing_active = False

    for i in range(buy_bar_idx, len(bars)):
        b = bars[i]

        # 1. 止损（优先）
        if b["low"] <= stop_loss_price:
            return _fill_on_touch(b, stop_loss_price), "止损", b["datetime"]

        # 2. 追踪止盈：先触发 +3%，再检查回落（含同根 K 先高后低）
        if not trailing_active:
            if b["high"] >= trail_trigger_price:
                trailing_active = True
                max_high_after_trigger = b["high"]
        elif b["high"] > max_high_after_trigger:
            max_high_after_trigger = b["high"]

        if trailing_active:
            trail_sell_price = max_high_after_trigger * (1 - trail_drop_pct / 100)
            if b["low"] <= trail_sell_price:
                return _fill_on_touch(b, trail_sell_price), "追踪止盈", b["datetime"]

    # 3. 未触发 → 卖出日收盘
    last_bar = bars[-1]
    return last_bar["close"], "收盘", last_bar["datetime"]


# ── 回测引擎 ──────────────────────────────────────────

def run_intraday_backtest(
    etf_daily: dict,
    etf_5min: dict,
    buy_up_pct: float = 0.3,
    buy_down_pct: float = 2.0,
    buy_rebound_pct: float = 0.3,
    stop_loss_pct: float = -0.5,
    trail_trigger_pct: float = 3.0,
    trail_drop_pct: float = 0.5,
    slippage_pct: float = 0.0,
    fee_pct: float = 0.0,
) -> list[dict]:
    """盘中策略回测。

    Args:
        etf_daily: {code: {name, etf_code, returns: [{date, close, ...}]}}
        etf_5min: {code: {date: [bars]}}
        其余参数见 check_buy_trigger / check_sell_trigger

    Returns:
        trades: [{signal_date, etf_code, baseline_price, buy_price, buy_time,
                  buy_reason, sell_price, sell_time, sell_reason, return_pct, ...}]
    """
    # 所有交易日（从日 K 提取）
    all_dates = set()
    for info in etf_daily.values():
        for r in info["returns"]:
            all_dates.add(r["date"])
    all_dates = sorted(all_dates)

    # 信号日：从 SCORE_WINDOW 开始到倒数第 2 天（留 T+1 卖出）
    signal_dates = all_dates[SCORE_WINDOW:-1]

    trades = []
    for signal_date in signal_dates:
        # 1. T 日 14:50 选 TOP1
        scores = []
        for code, info in etf_daily.items():
            returns = info["returns"]
            idx_map = {r["date"]: i for i, r in enumerate(returns)}
            if signal_date not in idx_map:
                continue
            idx = idx_map[signal_date]
            if idx < SCORE_WINDOW:
                continue
            score = compute_v6_score(returns, idx)
            scores.append((code, info["name"], info["etf_code"], score))

        if len(scores) < 2:
            continue

        scores.sort(key=lambda x: x[3], reverse=True)
        top1_code, top1_name, top1_etf, top1_score = scores[0]

        # 2. 找 T 日 14:50 基准价
        t1_5min = etf_5min.get(top1_code, {}).get(signal_date, [])
        if not t1_5min:
            continue
        bar_1450 = find_1450_bar(t1_5min)
        if not bar_1450:
            continue
        baseline_price = bar_1450["close"]

        # 3. T 日 14:55~15:00 + T+1 全天买入观察
        signal_idx = all_dates.index(signal_date)
        if signal_idx + 1 >= len(all_dates):
            continue
        next_date = all_dates[signal_idx + 1]

        # T 日剩余 bars（14:55, 15:00）
        t1_observation = bars_after_1450(t1_5min)[1:]  # 去掉 14:50 当根
        # T+1 全天 bars
        t2_5min = etf_5min.get(top1_code, {}).get(next_date, [])
        observation_bars = t1_observation + t2_5min

        if not observation_bars:
            continue

        buy_price, buy_reason, buy_time = check_buy_trigger(
            observation_bars, baseline_price,
            buy_up_pct, buy_down_pct, buy_rebound_pct,
        )

        if buy_price is None:
            # 未触发，记录放弃
            trades.append({
                "signal_date": signal_date,
                "next_date": next_date,
                "sector": top1_name,
                "etf_code": top1_etf,
                "score": top1_score,
                "baseline_price": baseline_price,
                "buy_price": None,
                "buy_reason": "未触发",
                "sell_price": None,
                "sell_reason": "未买入",
                "return_pct": 0.0,
                "held_days": 0,
            })
            continue

        # 4. T+1 卖出（A 股当日买入不可卖，仅监控 next_date 全天）
        sell_bars = t2_5min
        if not sell_bars:
            continue
        sell_price, sell_reason, sell_time = check_sell_trigger(
            sell_bars, buy_price, 0,
            stop_loss_pct, trail_trigger_pct, trail_drop_pct,
        )

        # 计算净价（含滑点手续费）
        buy_cost = buy_price * (1 + slippage_pct / 100) * (1 + fee_pct / 100)
        sell_income = sell_price * (1 - slippage_pct / 100) * (1 - fee_pct / 100)
        return_pct = (sell_income - buy_cost) / buy_cost * 100

        # 持仓天数：买入日至 T+1 卖出日
        sell_date = sell_time.split(" ")[0]
        buy_date = buy_time.split(" ")[0]
        held_days = 1 if sell_date == buy_date else 2

        trades.append({
            "signal_date": signal_date,
            "next_date": next_date,
            "sector": top1_name,
            "etf_code": top1_etf,
            "score": top1_score,
            "baseline_price": baseline_price,
            "buy_price": buy_price,
            "buy_time": buy_time,
            "buy_reason": buy_reason,
            "sell_price": sell_price,
            "sell_time": sell_time,
            "sell_reason": sell_reason,
            "return_pct": return_pct,
            "held_days": held_days,
        })

    return trades


# ── 报告输出 ──────────────────────────────────────────

def print_intraday_report(trades: list[dict], params: dict):
    bought = [t for t in trades if t["buy_price"] is not None]
    skipped = [t for t in trades if t["buy_price"] is None]

    print("=" * 80)
    print("          板块轮动 TOP1 盘中策略回测报告")
    print("=" * 80)
    print(f"策略: T日14:50选TOP1 → 上涨{params['buy_up']}%或下跌{params['buy_down']}%回弹{params['buy_rebound']}%买 →")
    print(f"      止损{params['stop_loss']}% / 追踪+{params['trail_trigger']}%落{params['trail_drop']}% / T+1收盘卖")
    print(f"滑点: {params['slippage']}%, 手续费: {params['fee']}%")
    print(f"信号总数: {len(trades)}, 买入: {len(bought)}, 放弃: {len(skipped)}")
    print()

    if not bought:
        print("无买入交易")
        return

    # 收益统计
    rets = [t["return_pct"] for t in bought]
    stats = _calc_stats(rets)

    print("─" * 60)
    print("收益统计（仅买入交易）")
    print("─" * 60)
    print(f"  胜率: {stats['wins']}/{stats['total']} = {stats['win_rate']:.1f}%")
    print(f"  累计收益率: {stats['cum']:+.2f}%")
    print(f"  年化收益率: {stats['ann']:+.2f}%")
    print(f"  平均每笔: {stats['avg']:+.3f}%")
    print(f"  最大单笔亏损: {stats['max_loss']:+.3f}%")
    print(f"  最大单笔盈利: {stats['max_gain']:+.3f}%")
    print(f"  最大回撤: {stats['max_drawdown']:+.2f}%")
    print(f"  夏普比率: {stats['sharpe']:.2f}")
    print()

    # 卖出原因分布
    reason_counts = {}
    for t in bought:
        reason_counts[t["sell_reason"]] = reason_counts.get(t["sell_reason"], 0) + 1
    print("卖出原因分布:")
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count} 次 ({count / len(bought) * 100:.1f}%)")
    print()

    # 买入原因分布
    buy_reason_counts = {}
    for t in bought:
        buy_reason_counts[t["buy_reason"]] = buy_reason_counts.get(t["buy_reason"], 0) + 1
    print("买入原因分布:")
    for reason, count in sorted(buy_reason_counts.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count} 次 ({count / len(bought) * 100:.1f}%)")
    print()

    # 逐笔明细
    print("─" * 60)
    print("逐笔交易明细")
    print("─" * 60)
    print(f"  {'信号日':>12s} {'板块':10s} {'ETF':>8s} {'基准':>8s} {'买入':>8s} "
          f"{'买入原因':>6s} {'卖出':>8s} {'卖出原因':>8s} {'收益':>8s} {'持仓':>4s}")
    for t in trades:
        if t["buy_price"] is None:
            print(f"  {t['signal_date']:>12s} {t['sector']:10s} {t['etf_code']:>8s} "
                  f"{t['baseline_price']:8.3f}        —    未触发        —          —    {t['return_pct']:+7.2f}%    —")
        else:
            sell_reason_short = {"止损": "止损", "追踪止盈": "追踪", "收盘": "收盘"}.get(
                t["sell_reason"], t["sell_reason"][:4])
            print(f"  {t['signal_date']:>12s} {t['sector']:10s} {t['etf_code']:>8s} "
                  f"{t['baseline_price']:8.3f} {t['buy_price']:8.3f} {t['buy_reason']:>6s} "
                  f"{t['sell_price']:8.3f} {sell_reason_short:>8s} {t['return_pct']:+7.2f}% "
                  f"{t['held_days']:>3d}日")

    print()
    print("=" * 80)
    if stats:
        print(f"结论: 累计 {stats['cum']:+.2f}%, 年化 {stats['ann']:+.2f}%, "
              f"胜率 {stats['win_rate']:.1f}%, 最大回撤 {stats['max_drawdown']:+.2f}%, 夏普 {stats['sharpe']:.2f}")
        print(f"      买入率 {len(bought)}/{len(trades)} = {len(bought) / len(trades) * 100:.1f}%")
    print("=" * 80)


# ── 收益曲线图 ────────────────────────────────────────

def plot_intraday_curve(trades: list[dict], output_path: str):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime as dt
    except ImportError:
        print("matplotlib 未安装，跳过图表")
        return

    import matplotlib.font_manager as fm
    for font_name in ["PingFang SC", "Heiti SC", "STHeiti", "Arial Unicode MS", "SimHei"]:
        try:
            fm.findfont(font_name, fallback_to_default=False)
            plt.rcParams["font.sans-serif"] = [font_name]
            break
        except Exception:
            continue
    else:
        plt.rcParams["font.sans-serif"] = ["PingFang SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    bought = [t for t in trades if t["buy_price"] is not None]
    if not bought:
        return

    dates = [dt.strptime(t["signal_date"], "%Y-%m-%d") for t in bought]
    rets = [t["return_pct"] for t in bought]

    cum_pct = [0.0]
    base = 1.0
    for r in rets:
        base *= (1 + r / 100)
        cum_pct.append((base - 1) * 100)

    fig, ax = plt.subplots(figsize=(14, 7))
    ax.plot([dates[0]] + dates, cum_pct, color="#e74c3c", linewidth=2,
            label=f"累计收益 ({cum_pct[-1]:+.1f}%)")
    ax.fill_between([dates[0]] + dates, 0, cum_pct, alpha=0.15, color="#e74c3c")
    ax.set_xlabel("信号日", fontsize=12)
    ax.set_ylabel("累计收益率 (%)", fontsize=12)
    ax.set_title("板块轮动 TOP1 盘中策略回测收益曲线", fontsize=14, fontweight="bold")
    ax.legend(fontsize=11, loc="upper left")
    ax.grid(True, alpha=0.3)
    ax.axhline(y=0, color="black", linewidth=0.5)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=3))
    fig.autofmt_xdate(rotation=45)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"收益曲线图已保存: {output_path}")


# ── 主流程 ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="板块轮动 TOP1 盘中策略回测")
    parser.add_argument("--lookback", type=int, default=30, help="历史天数（默认 30）")
    parser.add_argument("--buy-up", type=float, default=0.3, help="追涨触发涨幅%（默认 0.3）")
    parser.add_argument("--buy-down", type=float, default=2.0, help="抄底触发跌幅%（默认 2.0）")
    parser.add_argument("--buy-rebound", type=float, default=0.3, help="抄底回弹幅度%（默认 0.3）")
    parser.add_argument("--stop-loss", type=float, default=-0.5, help="止损幅度%（默认 -0.5）")
    parser.add_argument("--trail-trigger", type=float, default=3.0, help="追踪止盈触发涨幅%（默认 3.0）")
    parser.add_argument("--trail-drop", type=float, default=0.5, help="追踪止盈回落幅度%（默认 0.5）")
    parser.add_argument("--slippage", type=float, default=0.0, help="滑点%（默认 0）")
    parser.add_argument("--fee", type=float, default=0.0, help="手续费%（默认 0）")
    parser.add_argument("--no-chart", action="store_true", help="不生成收益曲线图")
    args = parser.parse_args()

    print(f"=== 板块轮动 TOP1 盘中策略回测 ===")
    print(f"策略: 上涨{args.buy_up}%或下跌{args.buy_down}%回弹{args.buy_rebound}%买 / "
          f"止损{args.stop_loss}% / 追踪+{args.trail_trigger}%落{args.trail_drop}% / T+1收盘卖")
    print(f"历史: {args.lookback} 天, 滑点: {args.slippage}%, 手续费: {args.fee}%")
    print()

    # 1. 获取板块列表（平安证券）
    print(">>> 获取板块列表（平安证券）...")
    sectors = load_pingan_sectors()
    print(f"    {len(sectors)} 个板块（均有 ETF）")

    # 2. 拉日 K 线（算 v6 得分）
    etf_sectors = sectors
    print(f">>> 获取 {len(etf_sectors)} 个 ETF 的日 K 线 ({args.lookback + 10} 日)...")
    etf_daily = {}
    for i, sec in enumerate(etf_sectors):
        etf_code, etf_name = sec["etf_code"], sec["etf_name"]
        sina_sym = etf_to_sina_symbol(sec["etf_raw"])
        klines = fetch_sina_kline(sina_sym, datalen=args.lookback + 10)
        if klines and len(klines) > SCORE_WINDOW + 1:
            returns = compute_daily_data(klines)
            etf_daily[sec["code"]] = {
                "name": sec["name"],
                "etf_code": etf_code,
                "etf_name": etf_name,
                "returns": returns,
            }
        if (i + 1) % 10 == 0:
            print(f"    日K进度: {i+1}/{len(etf_sectors)} ({len(etf_daily)} 有数据)")
    print(f"    日K完成: {len(etf_daily)} 个 ETF")

    if len(etf_daily) < 2:
        print("ERROR: ETF 日 K 数据不足")
        sys.exit(1)

    # 3. 拉 5 分钟 K 线（盘中触发判断）
    # 30 天约 1200 根/ETF，datalen 设 1500 留余量
    datalen_5min = min(args.lookback * 48 + 200, 2000)
    print(f">>> 获取 {len(etf_daily)} 个 ETF 的 5 分钟 K 线 (datalen={datalen_5min})...")
    etf_5min: dict[str, dict[str, list[dict]]] = {}
    for i, (code, info) in enumerate(etf_daily.items()):
        sina_sym = etf_to_sina_symbol(info["etf_code"])
        klines_5min = fetch_sina_5min_kline(sina_sym, datalen=datalen_5min)
        if klines_5min:
            etf_5min[code] = group_5min_by_day(klines_5min)
        if (i + 1) % 10 == 0:
            print(f"    5分K进度: {i+1}/{len(etf_daily)} ({len(etf_5min)} 有数据)")
    print(f"    5分K完成: {len(etf_5min)} 个 ETF")

    if not etf_5min:
        print("ERROR: 5 分钟 K 数据获取失败")
        sys.exit(1)

    # 4. 回测
    print("\n>>> 运行盘中策略回测...")
    trades = run_intraday_backtest(
        etf_daily, etf_5min,
        buy_up_pct=args.buy_up, buy_down_pct=args.buy_down,
        buy_rebound_pct=args.buy_rebound,
        stop_loss_pct=args.stop_loss,
        trail_trigger_pct=args.trail_trigger, trail_drop_pct=args.trail_drop,
        slippage_pct=args.slippage, fee_pct=args.fee,
    )

    # 5. 输出报告
    params = {
        "buy_up": args.buy_up, "buy_down": args.buy_down,
        "buy_rebound": args.buy_rebound, "stop_loss": args.stop_loss,
        "trail_trigger": args.trail_trigger, "trail_drop": args.trail_drop,
        "slippage": args.slippage, "fee": args.fee,
    }
    print()
    print_intraday_report(trades, params)

    # 6. 画图
    if not args.no_chart:
        chart_path = str(Path.home() / ".tradingagents" / "rotation" /
                         f"intraday_chart_{datetime.now().strftime('%Y%m%d_%H%M')}.png")
        plot_intraday_curve(trades, chart_path)

    # 7. 保存
    cache_dir = Path.home() / ".tradingagents" / "rotation"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"intraday_backtest_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump({"config": {**params, "lookback": args.lookback}, "trades": trades},
                  f, ensure_ascii=False, indent=2, default=str)
    print(f"\n回测数据已保存: {cache_file}")


if __name__ == "__main__":
    main()
