#!/usr/bin/env python3
"""板块轮动 TOP1 回测脚本

策略：
1. 每个交易日 14:50，根据 v6 公式选出 TOP1 板块的 ETF
2. 以当日收盘价买入
3. 次日盘中以最高价卖出（理想情况）
4. 同时计算次日收盘价卖出（保守情况）作为对照

回测指标：
- 累计收益率
- 胜率（正收益天数比例）
- 平均每笔收益
- 最大单笔亏损
- 年化收益率（按 250 交易日）
- 夏普比率

用法:
    python scripts/backtest_top1.py
    python scripts/backtest_top1.py --lookback 60 --top-n 1
    python scripts/backtest_top1.py --slippage 0.1 --fee 0.05
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 复用验证脚本的数据采集和公式逻辑
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from sector_etf_map import etf_to_sina_symbol, load_pingan_sectors  # noqa: E402

PROXY = os.environ.get("ROTATION_PROXY", "http://127.0.0.1:7890")
SINA_INTERVAL = 0.3
TIMEOUT = 15

from rotation_v6 import (  # noqa: E402
    SCORE_WINDOW,
    VOL_AVG_PERIOD,
    VOL_BASE,
    VOL_THRESHOLD,
    compute_v6_score,
)


def curl_get(url: str) -> str:
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


def fetch_sina_kline(symbol: str, datalen: int = 30) -> list[dict]:
    url = (
        f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
    )
    raw = curl_get(url)
    time.sleep(SINA_INTERVAL)
    if not raw or raw.strip() in ("null", "", "[]"):
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def fetch_mootdx_kline(symbol: str, offset: int = 800) -> list[dict]:
    """通过 mootdx TCP 拉取 K 线（支持长历史，最多 800 天约 3 年）。

    symbol: 6 位代码，如 159997, 515210
    返回: [{day, open, high, close, low, volume}, ...]（与新浪格式一致）
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingagents"))
        from mootdx.quotes import Quotes

        # 尝试内置服务器列表
        servers = [
            ("119.97.185.59", 7709), ("124.70.133.119", 7709),
            ("116.205.183.150", 7709), ("14.17.75.71", 7709),
            ("180.153.39.51", 7709),
        ]
        client = None
        for ip, port in servers:
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(2)
                s.connect((ip, port))
                s.close()
                client = Quotes.factory(market="std", server=(ip, port))
                break
            except Exception:
                continue
        if client is None:
            client = Quotes.factory(market="std", bestip=True)

        # mootdx 代码格式: 6开头→1(上海), 其他→0(深圳)
        market = 1 if symbol.startswith("6") else 0
        df = client.bars(symbol=symbol, category=4, offset=offset)

        if df is None or df.empty:
            return []

        df = df.drop(columns=["datetime", "year", "month", "day", "hour", "minute"],
                     errors="ignore")
        df = df.reset_index()
        if "datetime" in df.columns:
            df["datetime"] = df["datetime"].astype(str).str[:10]
            df = df.rename(columns={
                "datetime": "day", "open": "open", "high": "high",
                "low": "low", "close": "close", "volume": "volume",
            })

        result = []
        for _, row in df.iterrows():
            result.append({
                "day": str(row.get("day", "")),
                "open": float(row.get("open", 0)),
                "high": float(row.get("high", 0)),
                "low": float(row.get("low", 0)),
                "close": float(row.get("close", 0)),
                "volume": float(row.get("volume", 0)),
            })
        return result
    except Exception as e:
        print(f"    mootdx 获取失败 {symbol}: {e}")
        return []


def fetch_kline(symbol: str, lookback: int = 30) -> list[dict]:
    """获取 K 线，新浪 API 支持 datalen 最多约 1000 天。"""
    return fetch_sina_kline(etf_to_sina_symbol(symbol), datalen=lookback)


def compute_daily_data(klines: list[dict]) -> list[dict]:
    result = []
    for i, k in enumerate(klines):
        close = float(k.get("close", 0))
        high = float(k.get("high", close))
        try:
            volume = float(k.get("volume", 0))
        except (ValueError, TypeError):
            volume = 0.0
        if i == 0:
            ret = 0.0
        else:
            prev_close = float(klines[i - 1].get("close", 0))
            ret = ((close - prev_close) / prev_close * 100) if prev_close else 0.0
        result.append({
            "date": k.get("day", ""), "high": high, "close": close,
            "return_pct": ret, "volume": volume,
        })
    return result


def calc_trailing_stop_ret(buy_cost: float, sell_high: float, sell_close: float,
                            sell_low: float, trail_trigger: float, trail_drop: float,
                            stop_loss: float, slippage_pct: float, fee_pct: float
                            ) -> tuple[float, str]:
    """计算追踪止盈策略的收益。

    策略逻辑：
    1. 次日盘中如果涨幅达到 trail_trigger%（如+5%），开始追踪
    2. 从最高点回落 trail_drop%（如1%）时卖出
    3. 如果未达到触发价，收盘卖
    4. 同时设止损 stop_loss%（如-1%），先触发哪个就按哪个卖

    日 K 线近似：无法知道盘中精确走势，用以下假设：
    - 如果 high 达到触发价且 close 从 high 回落 > trail_drop%，
      按 (high × (1 - trail_drop%)) 卖出
    - 如果 high 达到触发价但 close 未从 high 回落 > trail_drop%，
      按 close 卖（说明尾盘没回落）
    - 如果 low 触及止损价，按止损价卖出（优先级高于追踪止盈）
    """
    def _net(price):
        return price * (1 - slippage_pct / 100) * (1 - fee_pct / 100)

    # 止损检查（优先）
    if stop_loss < 0:
        sl_price = buy_cost * (1 + stop_loss / 100)
        if _net(sell_low) <= sl_price:
            return stop_loss, "stop_loss"

    # 追踪止盈
    if trail_trigger > 0:
        trigger_price = buy_cost * (1 + trail_trigger / 100)
        if sell_high >= trigger_price:
            # 达到触发价，检查是否从高点回落
            trail_sell_price = sell_high * (1 - trail_drop / 100)
            # 如果回落后的价格仍高于 close，说明盘中确实回落了
            if trail_sell_price >= sell_close:
                ret = (_net(trail_sell_price) - buy_cost) / buy_cost * 100
                return ret, "trailing_stop"
            else:
                # 没有回落到触发线，按收盘卖
                ret = (_net(sell_close) - buy_cost) / buy_cost * 100
                return ret, "close"

    # 未触发，收盘卖
    ret = (_net(sell_close) - buy_cost) / buy_cost * 100
    return ret, "close"


# ── 回测引擎 ──────────────────────────────────────────

def run_backtest(etf_data: dict, top_n: int = 1,
                 slippage_pct: float = 0.0, fee_pct: float = 0.0,
                 take_profit: float = 0.0, stop_loss: float = 0.0,
                 trail_trigger: float = 0.0, trail_drop: float = 0.0) -> list[dict]:
    """回测：每日选 TOP1 ETF，收盘买入，次日卖出。

    支持三种卖出策略：
    1. 固定止盈止损（take_profit/stop_loss）
    2. 追踪止盈（trail_trigger: 触发涨幅, trail_drop: 回落幅度）
    3. 无策略（收盘卖）
    """
    all_dates = set()
    for info in etf_data.values():
        for r in info["returns"]:
            all_dates.add(r["date"])
    all_dates = sorted(all_dates)

    eval_dates = all_dates[SCORE_WINDOW:-1]

    trades = []
    for date in eval_dates:
        date_idx = all_dates.index(date)
        next_date = all_dates[date_idx + 1] if date_idx + 1 < len(all_dates) else None
        if not next_date:
            continue

        scores = []
        for code, info in etf_data.items():
            returns = info["returns"]
            idx_map = {r["date"]: i for i, r in enumerate(returns)}
            if date not in idx_map:
                continue
            idx = idx_map[date]
            if idx < SCORE_WINDOW:
                continue
            score = compute_v6_score(returns, idx)
            scores.append((code, info["name"], info["etf_code"], score,
                          returns[idx]["close"], returns[idx]))

        if len(scores) < top_n * 2:
            continue

        scores.sort(key=lambda x: x[3], reverse=True)

        for rank in range(top_n):
            code, name, etf_code, score, buy_close, buy_bar = scores[rank]
            sell_high = None
            sell_close = None
            sell_low = None
            for r in etf_data[code]["returns"]:
                if r["date"] == next_date:
                    sell_high = r["high"]
                    sell_close = r["close"]
                    sell_low = r.get("low", r["close"])
                    break
            if sell_high is None or buy_close <= 0:
                continue

            buy_cost = buy_close * (1 + slippage_pct / 100) * (1 + fee_pct / 100)
            sell_high_income = sell_high * (1 - slippage_pct / 100) * (1 - fee_pct / 100)
            sell_close_income = sell_close * (1 - slippage_pct / 100) * (1 - fee_pct / 100)
            sell_low_income = sell_low * (1 - slippage_pct / 100) * (1 - fee_pct / 100)

            ret_high = (sell_high_income - buy_cost) / buy_cost * 100
            ret_close = (sell_close_income - buy_cost) / buy_cost * 100
            ret_low = (sell_low_income - buy_cost) / buy_cost * 100

            # 策略选择
            if trail_trigger > 0:
                # 追踪止盈策略
                ret_strategy, sell_reason = calc_trailing_stop_ret(
                    buy_cost, sell_high, sell_close, sell_low,
                    trail_trigger, trail_drop, stop_loss,
                    slippage_pct, fee_pct)
            else:
                # 固定止盈止损策略
                ret_tp = None
                ret_sl = None
                sell_reason = "close"

                if take_profit > 0:
                    tp_price = buy_cost * (1 + take_profit / 100)
                    if sell_high * (1 - slippage_pct / 100) * (1 - fee_pct / 100) >= tp_price:
                        ret_tp = take_profit
                        sell_reason = "take_profit"

                if stop_loss < 0:
                    sl_price = buy_cost * (1 + stop_loss / 100)
                    if sell_low * (1 - slippage_pct / 100) * (1 - fee_pct / 100) <= sl_price:
                        ret_sl = stop_loss
                        sell_reason = "stop_loss"

                if sell_reason == "take_profit":
                    ret_strategy = ret_tp
                elif sell_reason == "stop_loss":
                    ret_strategy = ret_sl
                else:
                    ret_strategy = ret_close

            trades.append({
                "date": date,
                "next_date": next_date,
                "rank": rank + 1,
                "sector": name,
                "etf_code": etf_code,
                "score": score,
                "buy_price": buy_close,
                "sell_high": sell_high,
                "sell_close": sell_close,
                "sell_low": sell_low,
                "ret_high": ret_high,
                "ret_close": ret_close,
                "ret_low": ret_low,
                "ret_strategy": ret_strategy,
                "sell_reason": sell_reason,
            })

    return trades


def _calc_stats(rets: list[float]) -> dict:
    """计算收益统计（含最大回撤）。"""
    if not rets:
        return {}
    wins = sum(1 for r in rets if r > 0)
    cum = 1.0
    for r in rets:
        cum *= (1 + r / 100)
    cum_pct = (cum - 1) * 100
    avg = sum(rets) / len(rets)
    if len(rets) > 1:
        ann = (cum ** (250 / len(rets)) - 1) * 100
        var = sum((r - avg) ** 2 for r in rets) / len(rets)
        std = var ** 0.5
        sharpe = (avg / std * (250 ** 0.5)) if std > 0 else 0
    else:
        ann = 0
        sharpe = 0

    # 最大回撤：从累计收益的峰值到后续最低点的跌幅
    peak = 1.0
    equity = 1.0
    max_dd = 0.0
    for r in rets:
        equity *= (1 + r / 100)
        if equity > peak:
            peak = equity
        dd = (equity - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd

    return {
        "wins": wins, "total": len(rets), "win_rate": wins / len(rets) * 100,
        "cum": cum_pct, "ann": ann, "avg": avg,
        "max_loss": min(rets), "max_gain": max(rets), "sharpe": sharpe,
        "max_drawdown": max_dd,
    }


def _print_stats(label: str, stats: dict):
    print("─" * 50)
    print(f"{label}")
    print("─" * 50)
    print(f"  胜率: {stats['wins']}/{stats['total']} = {stats['win_rate']:.1f}%")
    print(f"  累计收益率: {stats['cum']:+.2f}%")
    print(f"  年化收益率: {stats['ann']:+.2f}%")
    print(f"  平均每笔: {stats['avg']:+.3f}%")
    print(f"  最大单笔亏损: {stats['max_loss']:+.3f}%")
    print(f"  最大单笔盈利: {stats['max_gain']:+.3f}%")
    print(f"  最大回撤: {stats['max_drawdown']:+.2f}%")
    print(f"  夏普比率: {stats['sharpe']:.2f}")
    print()


def plot_equity_curve(trades: list[dict], output_path: str):
    """绘制收益曲线图。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime as dt
    except ImportError:
        print("⚠️ matplotlib 未安装，跳过图表生成")
        return False

    # 设置中文字体（按优先级尝试）
    import matplotlib.font_manager as fm
    chinese_fonts = ["PingFang SC", "Heiti SC", "STHeiti", "Arial Unicode MS",
                     "SimHei", "Microsoft YaHei", "Noto Sans CJK SC",
                     "WenQuanYi Micro Hei", "Source Han Sans CN"]
    for font_name in chinese_fonts:
        try:
            fm.findfont(font_name, fallback_to_default=False)
            plt.rcParams["font.sans-serif"] = [font_name]
            break
        except Exception:
            continue
    else:
        # macOS 自带 PingFang SC
        plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "STHeiti",
                                           "Arial Unicode MS", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    dates = [t["date"] for t in trades]
    dates_parsed = [dt.strptime(d, "%Y-%m-%d") for d in dates]

    # 三条曲线的累计收益
    curves = {
        "理想（盘中最高卖）": [t["ret_high"] for t in trades],
        "止盈止损策略": [t["ret_strategy"] for t in trades],
        "保守（收盘卖）": [t["ret_close"] for t in trades],
    }

    fig, ax = plt.subplots(figsize=(14, 7))

    colors = {"理想（盘中最高卖）": "#2ecc71", "止盈止损策略": "#e74c3c", "保守（收盘卖）": "#3498db"}
    for label, rets in curves.items():
        # 复利累计
        cum_pct = [0.0]
        base = 1.0
        for r in rets:
            base *= (1 + r / 100)
            cum_pct.append((base - 1) * 100)
        ax.plot([dates_parsed[0]] + dates_parsed, cum_pct,
                label=f"{label} ({cum_pct[-1]:+.1f}%)",
                color=colors.get(label, "gray"), linewidth=2)

    ax.set_xlabel("日期", fontsize=12)
    ax.set_ylabel("累计收益率 (%)", fontsize=12)
    ax.set_title("板块轮动 TOP1 回测收益曲线", fontsize=14, fontweight="bold")
    ax.legend(fontsize=11, loc="upper left")
    ax.grid(True, alpha=0.3)
    ax.axhline(y=0, color="black", linewidth=0.5)

    # 标注关键点
    for label, rets in curves.items():
        if not rets:
            continue
        max_idx = max(range(len(rets)), key=lambda i: rets[i])
        cum_pct = []
        base = 1.0
        for r in rets:
            base *= (1 + r / 100)
            cum_pct.append((base - 1) * 100)
        if max_idx < len(dates_parsed):
            ax.annotate(f"{cum_pct[max_idx]:+.1f}%",
                        xy=(dates_parsed[max_idx], cum_pct[max_idx]),
                        fontsize=9, color="gray")

    # 格式化 x 轴
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=3))
    fig.autofmt_xdate(rotation=45)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"📊 收益曲线图已保存: {output_path}")
    return True


def print_backtest_report(trades: list[dict], top_n: int,
                          take_profit: float = 0.0, stop_loss: float = 0.0,
                          chart_path: str = ""):
    print("=" * 70)
    print("          板块轮动 TOP1 回测报告")
    print("=" * 70)
    print(f"策略: 每日 14:50 选 v6 TOP1 ETF，收盘买入，次日卖出")
    print(f"公式: {SCORE_WINDOW}日涨幅 × 量能因子(阈{VOL_THRESHOLD} 均{VOL_AVG_PERIOD} 底{VOL_BASE})")
    tp_label = f"止盈+{take_profit}% " if take_profit > 0 else ""
    sl_label = f"止损{stop_loss}% " if stop_loss < 0 else ""
    trail_label = ""
    if take_profit == 0 and stop_loss == 0:
        trail_label = "（收盘卖）"
    print(f"止盈止损: {tp_label}{sl_label}{trail_label}{'无' if not tp_label and not sl_label and not trail_label else ''}")
    print(f"交易天数: {len(trades)}")
    print()

    if not trades:
        print("无交易记录")
        return

    # A. 盘中最高价卖出
    stats_high = _calc_stats([t["ret_high"] for t in trades])
    _print_stats("A. 次日盘中最高价卖出（理想情况）", stats_high)

    # B. 止盈止损策略
    stats_strategy = _calc_stats([t["ret_strategy"] for t in trades])
    tp_hits = sum(1 for t in trades if t["sell_reason"] == "take_profit")
    sl_hits = sum(1 for t in trades if t["sell_reason"] == "stop_loss")
    close_hits = sum(1 for t in trades if t["sell_reason"] == "close")
    _print_stats(f"B. 止盈止损策略 (止盈+{take_profit}%/止损{stop_loss}%)", stats_strategy)
    print(f"  止盈触发: {tp_hits} 次 | 止损触发: {sl_hits} 次 | 收盘卖: {close_hits} 次")
    print()

    # C. 收盘价卖出
    stats_close = _calc_stats([t["ret_close"] for t in trades])
    _print_stats("C. 次日收盘价卖出（保守情况）", stats_close)

    # D. 逐笔明细
    print("─" * 50)
    print("D. 逐笔交易明细")
    print("─" * 50)
    print(f"  {'日期':>12s} {'板块':10s} {'ETF':>8s} {'买入':>8s} {'次日高':>8s} "
          f"{'次日收':>8s} {'盘中收益':>8s} {'策略收益':>8s} {'收盘收益':>8s} {'原因':>10s}")
    for t in trades:
        reason_cn = {"take_profit": "止盈", "stop_loss": "止损", "close": "收盘"}.get(t["sell_reason"], t["sell_reason"])
        print(f"  {t['date']:>12s} {t['sector']:10s} {t['etf_code']:>8s} "
              f"{t['buy_price']:8.3f} {t['sell_high']:8.3f} {t['sell_close']:8.3f} "
              f"{t['ret_high']:+7.2f}% {t['ret_strategy']:+7.2f}% {t['ret_close']:+7.2f}% {reason_cn:>10s}")

    print()
    print("=" * 70)
    print("回测结论:")
    print(f"  理想（盘中最高卖）: 累计 {stats_high['cum']:+.2f}%, 年化 {stats_high['ann']:+.2f}%, "
          f"胜率 {stats_high['win_rate']:.1f}%")
    print(f"  止盈止损策略:       累计 {stats_strategy['cum']:+.2f}%, 年化 {stats_strategy['ann']:+.2f}%, "
          f"胜率 {stats_strategy['win_rate']:.1f}%")
    print(f"  保守（收盘卖）:     累计 {stats_close['cum']:+.2f}%, 年化 {stats_close['ann']:+.2f}%, "
          f"胜率 {stats_close['win_rate']:.1f}%")
    print("=" * 70)

    # 绘制收益曲线图
    if chart_path:
        plot_equity_curve(trades, chart_path)


# ── 主流程 ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="板块轮动 TOP1 回测")
    parser.add_argument("--lookback", type=int, default=30, help="历史天数（默认 30）")
    parser.add_argument("--top-n", type=int, default=1, help="买 TOP N（默认 1）")
    parser.add_argument("--slippage", type=float, default=0.0,
                        help="滑点百分比（默认 0）")
    parser.add_argument("--fee", type=float, default=0.0,
                        help="手续费百分比（默认 0，ETF 免五可填 0.05）")
    parser.add_argument("--take-profit", type=float, default=2.0,
                        help="止盈百分比（默认 2.0，0=不设止盈）")
    parser.add_argument("--stop-loss", type=float, default=-1.0,
                        help="止损百分比（默认 -1.0，0=不设止损）")
    parser.add_argument("--trail-trigger", type=float, default=0.0,
                        help="追踪止盈触发涨幅（如 5.0 表示涨到+5%开始追踪，默认 0=不启用）")
    parser.add_argument("--trail-drop", type=float, default=1.0,
                        help="追踪止盈回落幅度（如 1.0 表示从最高点回落1%卖出，默认 1.0）")
    parser.add_argument("--no-chart", action="store_true",
                        help="不生成收益曲线图")
    parser.add_argument("--compare", action="store_true",
                        help="止盈止损参数对比模式（跑多组参数并输出对比表+对比图）")
    args = parser.parse_args()

    print(f"=== 板块轮动 TOP{args.top_n} 回测 ===")
    print(f"公式: {SCORE_WINDOW}日涨幅 × 量能因子(阈{VOL_THRESHOLD} 均{VOL_AVG_PERIOD} 底{VOL_BASE})")
    tp = args.take_profit
    sl = args.stop_loss
    print(f"历史天数: {args.lookback}, 滑点: {args.slippage}%, 手续费: {args.fee}%")
    print(f"止盈: +{tp}%, 止损: {sl}%")
    print()

    # 1. 获取板块列表（平安证券）
    print(">>> 获取板块列表（平安证券）...")
    sectors = load_pingan_sectors()
    print(f"    {len(sectors)} 个板块（均有 ETF）")

    # 2. 获取 ETF K 线
    etf_sectors = sectors
    print(f">>> 获取 {len(etf_sectors)} 个板块的 ETF K 线 ({args.lookback} 日)...")
    if args.lookback > 90:
        print(f"    新浪 API 分批拉取（每批 90 天）")
    etf_data = {}
    for i, sec in enumerate(etf_sectors):
        etf_code, etf_name = sec["etf_code"], sec["etf_name"]
        klines = fetch_kline(etf_code, lookback=args.lookback)
        if klines and len(klines) > SCORE_WINDOW + 1:
            returns = compute_daily_data(klines)
            etf_data[sec["code"]] = {
                "name": sec["name"],
                "etf_code": etf_code,
                "etf_name": etf_name,
                "returns": returns,
            }
        if (i + 1) % 10 == 0:
            print(f"    进度: {i+1}/{len(etf_sectors)} ({len(etf_data)} 有数据)")

    print(f"    完成: {len(etf_data)} 个 ETF 有数据")
    if len(etf_data) < args.top_n * 2:
        print("ERROR: ETF 数据不足")
        sys.exit(1)

    # 3. 回测
    if args.compare:
        run_param_compare(etf_data, args)
        return

    print("\n>>> 运行回测...")
    if args.trail_trigger > 0:
        print(f"    追踪止盈: 触发+{args.trail_trigger}% 回落{args.trail_drop}% 止损{sl}%")
    trades = run_backtest(etf_data, top_n=args.top_n,
                          slippage_pct=args.slippage, fee_pct=args.fee,
                          take_profit=tp, stop_loss=sl,
                          trail_trigger=args.trail_trigger, trail_drop=args.trail_drop)

    # 4. 输出报告
    chart_path = ""
    if not args.no_chart:
        chart_path = str(Path.home() / ".tradingagents" / "rotation" /
                         f"backtest_chart_{datetime.now().strftime('%Y%m%d_%H%M')}.png")

    print()
    print_backtest_report(trades, args.top_n,
                          take_profit=tp, stop_loss=sl,
                          chart_path=chart_path)

    # 5. 保存
    cache_dir = Path.home() / ".tradingagents" / "rotation"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"backtest_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump({
            "config": {
                "lookback": args.lookback,
                "top_n": args.top_n,
                "slippage": args.slippage,
                "fee": args.fee,
                "take_profit": tp,
                "stop_loss": sl,
                "score_window": SCORE_WINDOW,
                "vol_threshold": VOL_THRESHOLD,
                "vol_avg_period": VOL_AVG_PERIOD,
                "vol_base": VOL_BASE,
            },
            "trades": trades,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n回测数据已保存: {cache_file}")


def run_param_compare(etf_data: dict, args):
    """止盈止损参数对比：跑多组参数，输出对比表 + 对比图。"""

    # 参数组合: (止盈, 止损, 追踪触发, 追踪回落)
    param_sets = [
        # 基线
        (0, 0, 0, 0),              # 无止盈止损（收盘卖）
        (4.0, -1.0, 0, 0),         # 固定止盈最优
        # 追踪止盈 — 回落幅度对比
        (0, -1.0, 3.0, 0.5),       # 触3% 落0.5%
        (0, -1.0, 3.0, 1.0),       # 触3% 落1%（上轮最优）
        (0, -1.0, 3.0, 2.0),       # 触3% 落2%
        (0, -1.0, 5.0, 0.5),       # 触5% 落0.5%
        (0, -1.0, 5.0, 1.0),       # 触5% 落1%
        # 追踪止盈 — 触发更低
        (0, -1.0, 2.0, 0.5),       # 触2% 落0.5%
        (0, -1.0, 2.0, 1.0),       # 触2% 落1%
        # 追踪止盈 — 止损更紧
        (0, -0.5, 3.0, 0.5),       # 触3% 落0.5% 止-0.5%
        (0, -0.5, 3.0, 1.0),       # 触3% 落1% 止-0.5%
        # 追踪止盈 — 止损更宽
        (0, -2.0, 3.0, 1.0),       # 触3% 落1% 止-2%
    ]

    print(f"\n>>> 运行 {len(param_sets)} 组策略参数对比...")
    all_results = []

    for tp, sl, tt, td in param_sets:
        trades = run_backtest(etf_data, top_n=args.top_n,
                              slippage_pct=args.slippage, fee_pct=args.fee,
                              take_profit=tp, stop_loss=sl,
                              trail_trigger=tt, trail_drop=td)
        if not trades:
            continue
        stats_strat = _calc_stats([t["ret_strategy"] for t in trades])
        tp_hits = sum(1 for t in trades if t["sell_reason"] == "take_profit")
        sl_hits = sum(1 for t in trades if t["sell_reason"] == "stop_loss")
        close_hits = sum(1 for t in trades if t["sell_reason"] == "close")
        trail_hits = sum(1 for t in trades if t["sell_reason"] == "trailing_stop")

        rets = [t["ret_strategy"] for t in trades]
        all_results.append({
            "tp": tp, "sl": sl, "tt": tt, "td": td,
            "stats": stats_strat,
            "tp_hits": tp_hits, "sl_hits": sl_hits, "close_hits": close_hits,
            "trail_hits": trail_hits,
            "rets": rets,
            "dates": [t["date"] for t in trades],
        })

    # 输出对比表
    print("\n" + "=" * 110)
    print("  策略参数对比（含追踪止盈 + 最大回撤）")
    print("=" * 110)
    print(f"  {'策略':>22} {'累计%':>10} {'年化%':>9} {'胜率%':>6} "
          f"{'夏普':>5} {'均笔':>7} {'最大亏':>7} {'最大回撤':>8} {'止盈':>4} {'追踪':>4} {'止损':>4} {'收盘':>4}")
    print("  " + "─" * 106)

    for r in sorted(all_results, key=lambda x: x["stats"]["cum"], reverse=True):
        s = r["stats"]
        # 策略标签
        if r["tt"] > 0:
            label = f"追踪 触{r['tt']}% 落{r['td']}% 止{r['sl']}"
        elif r["tp"] > 0:
            label = f"固定 止盈+{r['tp']}% 止损{r['sl']}%"
        else:
            label = "无（收盘卖）"
        print(f"  {label:>22} {s['cum']:+9.2f}% {s['ann']:+8.1f}% "
              f"{s['win_rate']:5.1f}% {s['sharpe']:4.2f} {s['avg']:+6.3f}% "
              f"{s['max_loss']:+6.2f}% {s['max_drawdown']:+7.2f}% "
              f"{r['tp_hits']:4d} {r['trail_hits']:4d} {r['sl_hits']:4d} {r['close_hits']:4d}")

    best = max(all_results, key=lambda x: x["stats"]["cum"])
    best_sharpe = max(all_results, key=lambda x: x["stats"]["sharpe"])
    best_dd = min(all_results, key=lambda x: x["stats"]["max_drawdown"])
    print()
    if best["tt"] > 0:
        best_label = f"追踪止盈 触{best['tt']}% 落{best['td']}% 止{best['sl']}"
    elif best["tp"] > 0:
        best_label = f"固定止盈+{best['tp']}% 止损{best['sl']}"
    else:
        best_label = "无（收盘卖）"
    print(f"  ★ 累计收益最优: {best_label} → "
          f"累计{best['stats']['cum']:+.2f}% 年化{best['stats']['ann']:+.1f}% 回撤{best['stats']['max_drawdown']:+.2f}%")
    print(f"  ★ 夏普比率最优: 累计{best_sharpe['stats']['cum']:+.2f}% "
          f"夏普{best_sharpe['stats']['sharpe']:.2f} 回撤{best_sharpe['stats']['max_drawdown']:+.2f}%")
    print(f"  ★ 最大回撤最小: 回撤{best_dd['stats']['max_drawdown']:+.2f}% "
          f"累计{best_dd['stats']['cum']:+.2f}%")
    print("=" * 110)

    # 画对比图
    chart_path = str(Path.home() / ".tradingagents" / "rotation" /
                     f"backtest_compare_{datetime.now().strftime('%Y%m%d_%H%M')}.png")
    plot_param_compare(all_results, chart_path)

    # 保存
    cache_dir = Path.home() / ".tradingagents" / "rotation"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"backtest_compare_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    save_data = []
    for r in all_results:
        save_data.append({
            "tp": r["tp"], "sl": r["sl"], "trail_trigger": r["tt"], "trail_drop": r["td"],
            "stats": {k: v for k, v in r["stats"].items()},
            "tp_hits": r["tp_hits"], "trail_hits": r["trail_hits"],
            "sl_hits": r["sl_hits"], "close_hits": r["close_hits"],
        })
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n对比数据已保存: {cache_file}")


def plot_param_compare(all_results: list[dict], output_path: str):
    """绘制多组参数的收益曲线对比图。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime as dt
    except ImportError:
        print("⚠️ matplotlib 未安装，跳过图表生成")
        return False

    import matplotlib.font_manager as fm
    chinese_fonts = ["PingFang SC", "Heiti SC", "STHeiti", "Arial Unicode MS",
                     "SimHei", "Microsoft YaHei", "Noto Sans CJK SC"]
    for font_name in chinese_fonts:
        try:
            fm.findfont(font_name, fallback_to_default=False)
            plt.rcParams["font.sans-serif"] = [font_name]
            break
        except Exception:
            continue
    else:
        plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "STHeiti",
                                           "Arial Unicode MS", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    # 按累计收益排序，取前 6 条 + 基线
    sorted_results = sorted(all_results, key=lambda x: x["stats"]["cum"], reverse=True)
    # 确保基线（0,0）在图中
    baseline = next((r for r in all_results if r["tp"] == 0 and r["sl"] == 0), None)
    show_results = sorted_results[:6]
    if baseline and baseline not in show_results:
        show_results.append(baseline)

    fig, ax = plt.subplots(figsize=(14, 8))

    colors = ["#e74c3c", "#2ecc71", "#3498db", "#f39c12", "#9b59b6", "#1abc9c", "#95a5a6"]
    for idx, r in enumerate(sorted_results):
        if r not in show_results:
            continue
        rets = r["rets"]
        dates = r["dates"]
        if not dates:
            continue
        dates_parsed = [dt.strptime(d, "%Y-%m-%d") for d in dates]

        # 复利累计
        cum_pct = [0.0]
        base = 1.0
        for ret in rets:
            base *= (1 + ret / 100)
            cum_pct.append((base - 1) * 100)

        if r["tt"] > 0:
            label = f"追踪 触{r['tt']}% 落{r['td']}% ({cum_pct[-1]:+.1f}%)"
        elif r["tp"] > 0:
            label = f"止盈+{r['tp']}% 止损{r['sl']}% ({cum_pct[-1]:+.1f}%)"
        else:
            label = f"收盘卖 ({cum_pct[-1]:+.1f}%)"

        ax.plot([dates_parsed[0]] + dates_parsed, cum_pct,
                label=label, color=colors[idx % len(colors)], linewidth=1.8)

    ax.set_xlabel("日期", fontsize=12)
    ax.set_ylabel("累计收益率 (%)", fontsize=12)
    ax.set_title(f"止盈止损参数对比 ({len(all_results[0]['dates'])} 笔交易)", fontsize=14, fontweight="bold")
    ax.legend(fontsize=9, loc="upper left", ncol=2)
    ax.grid(True, alpha=0.3)
    ax.axhline(y=0, color="black", linewidth=0.5)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    fig.autofmt_xdate(rotation=45)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"📊 参数对比图已保存: {output_path}")
    return True


if __name__ == "__main__":
    main()
