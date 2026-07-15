#!/usr/bin/env python3
"""T+0 ETF 当日涨幅动量监控 — 14:45 信号 / 14:50 买入 / 次日 1分K TRIX(5,3) 卖。

策略（与 1 分 K 回测候选组合一致）：
- 501018 近10日 MA20 穿越≥2 → 震荡期跳过买入
- 14:45 选 T+0 池当日涨幅最大且 ≥3% 的 ETF → 14:50 买入
- 次日 09:40~11:05 每 50 秒检查 1 分钟 TRIX(5,3) 死叉 → 卖出；无死叉则 11:05 定时卖

用法:
    python scripts/t0_monitor.py --signal          # 14:45 发买入信号
    python scripts/t0_monitor.py --sell-check      # 次日 TRIX 卖出检查
    python scripts/t0_sell_watch.py                # 09:40~11:05 每 50 秒循环检查
    python scripts/t0_monitor.py --dry-run --signal
    python scripts/t0_monitor.py --test-push

定时（install_crontab.sh）:
    09:40  t0_sell_watch.py（窗口内每 50 秒 --sell-check）
    14:45  --signal
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
from backtest_top1_minute import calc_trix, calc_trix_signal  # noqa: E402
from backtest_t0_etf import normalize_5min_bars, price_at_time  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    MIN_GAIN,
    TRIX_MIN_SELL,
    TRIX_PERIOD,
    bars_for_trix,
    time_to_min,
    bar_clock,
)
from rotation_monitor import fetch_tencent_quotes, send_dingtalk  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402
from t0_regime import CHOPPY_MA_CROSS, REGIME_PROXY, detect_regime, format_regime_block  # noqa: E402

try:
    from tradingagents.intraday.calendar import is_trading_day
except ImportError:
    def is_trading_day(day: date | None = None) -> bool:  # type: ignore[misc]
        day = day or date.today()
        return day.weekday() < 5

SINA_INTERVAL = 0.25
STATE_DIR = Path.home() / ".tradingagents" / "rotation"
STATE_FILE = STATE_DIR / "t0_monitor_state.json"

SIGNAL_TIME = "14:45"
BUY_TIME = "14:50"
TRIX_SIGNAL_PERIOD = 3
SELL_CUTOFF = "11:05"
FEE_NOTE = "手续费: 万3双边"
REGIME_RULE = f"501018近10日MA20穿越≥{CHOPPY_MA_CROSS}=震荡跳过"
BUY_RULE = f"{SIGNAL_TIME} 选当日涨幅≥{MIN_GAIN:.0f}% TOP1 → {BUY_TIME} 买入"
SELL_RULE = (
    f"次日 1分K TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD}) 死叉"
    f"(≥{TRIX_MIN_SELL}, ≤{SELL_CUTOFF}) / 无死叉 {SELL_CUTOFF} 定时卖"
)

SELL_CHECK_START = "09:40"   # 与 TRIX_MIN_SELL 一致
SELL_CHECK_END = SELL_CUTOFF


def fetch_1min_kline(sina_symbol: str, datalen: int = 1970) -> dict[str, list[dict]]:
    """新浪 jsonp 1 分 K，按交易日分组。"""
    import requests

    url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData"
    params = {"symbol": sina_symbol, "scale": "1", "ma": "no", "datalen": str(datalen)}
    try:
        r = requests.get(url, params=params, timeout=15)
        payload = r.text.split("=(")[1].split(");")[0]
        klines = json.loads(payload)
        if not klines:
            return {}
        return normalize_5min_bars(klines)
    except Exception as e:
        print(f"ERROR: 1分K获取失败 {sina_symbol}: {e}")
        return {}


def is_sell_check_window(now: datetime | None = None) -> bool:
    """卖出检查有效时段（交易日 09:40~11:05）。"""
    now = now or datetime.now()
    if not is_trading_day(now.date()):
        return False
    hm = now.hour * 60 + now.minute
    return time_to_min(SELL_CHECK_START) <= hm <= time_to_min(SELL_CHECK_END)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_regime() -> dict | None:
    """拉 501018 日 K 并识别震荡/中性/趋势。"""
    sym = f"sh{REGIME_PROXY}"
    klines = fetch_sina_kline(sym, datalen=60)
    if not klines:
        return None
    return detect_regime(klines, date.today().isoformat())


def strategy_header_lines() -> list[str]:
    return [
        f"**买入**: {BUY_RULE}",
        f"**卖出**: {SELL_RULE}",
        f"**过滤**: {REGIME_RULE}",
        f"**{FEE_NOTE}**",
        "",
    ]


def format_top5_lines(ranked: list[dict], highlight_code: str | None = None) -> list[str]:
    lines = ["**TOP5 涨幅**:"]
    for i, r in enumerate(ranked[:5], 1):
        tag = ""
        if highlight_code and r["code"] == highlight_code:
            tag = " ← TOP1"
        elif i == 1 and not highlight_code:
            tag = " ← 最高"
        lines.append(f"{i}. {r['name']} {r['code']} {r['today_gain']:+.2f}%{tag}")
    return lines


def rank_t0_by_today_gain(quotes: dict[str, dict]) -> list[dict]:
    """按腾讯实时涨跌幅排名（≈ 14:50 相对昨收当日涨幅）。"""
    rows: list[dict] = []
    for etf in get_all_t0_etfs():
        code = etf["code"]
        q = quotes.get(code)
        if not q:
            continue
        price = q.get("price", 0)
        last_close = q.get("last_close", 0)
        if not price or not last_close:
            continue
        gain = q.get("change_pct")
        if gain is None:
            gain = (price - last_close) / last_close * 100
        rows.append({
            **etf,
            "price": price,
            "last_close": last_close,
            "today_gain": round(float(gain), 2),
            "quote_time": q.get("quote_time", ""),
        })
    rows.sort(key=lambda x: x["today_gain"], reverse=True)
    return rows


def pick_signal_candidate(ranked: list[dict]) -> dict | None:
    for row in ranked:
        if row["today_gain"] >= MIN_GAIN:
            return row
    return None


def trix_death_cross_hit(
    buy_price: float,
    bars_yesterday: list[dict],
    bars_today: list[dict],
    cutoff_time: str,
) -> tuple[bool, float, str, str]:
    """检查截至 cutoff_time 是否已触发 TRIX 死叉（仅 09:40 后有效）。"""
    cutoff_min = time_to_min(cutoff_time)
    today_cut = []
    for b in bars_today:
        t = b.get("time", "")[:5]
        if t and time_to_min(t) <= cutoff_min:
            today_cut.append(b)
    if not today_cut:
        return False, 0.0, "", ""

    all_bars = bars_for_trix(bars_yesterday) + bars_for_trix(today_cut)
    min_warmup = TRIX_PERIOD * 3 + 5
    if len(all_bars) < min_warmup:
        return False, 0.0, "", ""

    warmup_len = len(bars_for_trix(bars_yesterday))
    closes = [float(b["close"]) for b in all_bars]
    trix = calc_trix(closes, TRIX_PERIOD)
    signal = calc_trix_signal(trix, TRIX_SIGNAL_PERIOD)
    min_sell_min = time_to_min(TRIX_MIN_SELL)
    search_start = max(warmup_len, min_warmup)

    for i in range(search_start, len(all_bars)):
        bar_t = bar_clock(all_bars[i])
        if time_to_min(bar_t) < min_sell_min:
            continue
        if trix[i - 1] >= signal[i - 1] and trix[i] < signal[i]:
            sell_price = closes[i]
            ret = (sell_price - buy_price) / buy_price * 100
            return True, sell_price, bar_t, f"{ret:+.2f}%"

    return False, 0.0, "", ""


def run_signal(dry_run: bool = False) -> int:
    print("=== T+0 ETF 动量监控 | 买入信号 ===")
    print(f"规则: {BUY_RULE} | {REGIME_RULE}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    if not is_trading_day():
        print("非交易日，跳过")
        return 0

    print(f">>> 拉取 {REGIME_PROXY} 日K 识别市场环境...")
    regime = fetch_regime()
    if regime:
        print(f"    环境: {regime['mode']} | 穿越{regime['ma_crosses']} | ADX {regime['adx']}")
    else:
        print("    环境: 数据不足")

    etf_list = get_all_t0_etfs()
    codes = [e["code"] for e in etf_list]
    print(f">>> 拉取 {len(codes)} 只 T+0 ETF 实时行情...")
    quotes = fetch_tencent_quotes(codes)
    if not quotes:
        print("ERROR: 无法获取实时行情")
        return 1

    ranked = rank_t0_by_today_gain(quotes)
    if len(ranked) < 2:
        print("ERROR: 有效 ETF 不足")
        return 1

    top = pick_signal_candidate(ranked)
    state = load_state()
    pos = state.get("position")

    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"### T+0 ETF 买入信号 | {run_ts}",
        "",
        *strategy_header_lines(),
        f"**扫描**: {len(ranked)} 只有效行情 / {len(codes)} 只 T+0 ETF",
        "",
        *format_regime_block(regime),
    ]

    if pos and not pos.get("sold"):
        lines.extend([
            f"⚠️ **持仓提醒**: 仍持有 {pos.get('name')} ({pos.get('etf')})",
            f"   买入日 {pos.get('buy_date')} @ {pos.get('buy_price')} — 请先处理卖出",
            "",
        ])
        print(f"⚠️  仍有持仓: {pos.get('name')} ({pos.get('etf')})")

    skip_choppy = bool(regime and regime.get("skip_choppy"))

    if skip_choppy:
        best = ranked[0]
        hypo = top or best
        print(f"震荡期跳过 | 若交易候选: {hypo['name']} {hypo['today_gain']:+.2f}%")
        lines.extend([
            "**信号**: ⛔ **震荡期跳过买入**",
            f"- 原因: 501018 近10日 MA20 穿越 {regime['ma_crosses']}次 ≥ {CHOPPY_MA_CROSS}",
            "",
            "**参考候选**（未执行）:",
            f"- {hypo['name']} ({hypo['code']}) 当日 {hypo['today_gain']:+.2f}%",
            f"- 现价 {hypo['price']:.4f}（昨收 {hypo['last_close']:.4f}）",
            "",
            *format_top5_lines(ranked, hypo["code"]),
        ])
        title = "T0轮动 震荡跳过"
        state["last_signal"] = {
            "timestamp": datetime.now().isoformat(),
            "skipped": True,
            "reason": "choppy",
            "regime": regime,
            "hypo_etf": hypo["code"],
            "hypo_gain": hypo["today_gain"],
        }
        save_state(state)
    elif not top:
        best = ranked[0]
        msg = f"今日无有效信号（最高 {best['name']} {best['today_gain']:+.2f}% < {MIN_GAIN:.0f}%）"
        print(msg)
        lines.extend([
            "**信号**: 无（涨幅过滤未通过）",
            f"- 最高: {best['name']} {best['code']} {best['today_gain']:+.2f}%",
            "",
            *format_top5_lines(ranked),
        ])
        title = "T0轮动 无买入信号"
        state["last_signal"] = {
            "timestamp": datetime.now().isoformat(),
            "skipped": True,
            "reason": "min_gain",
            "etf": best["code"],
            "today_gain": best["today_gain"],
        }
        save_state(state)
    else:
        print(f"TOP1: {top['name']} ({top['code']}) 当日{top['today_gain']:+.2f}%")
        print(f"      现价 {top['price']:.4f} → 建议 {BUY_TIME} 买入")
        lines.extend([
            f"**信号**: 买入 **{top['name']}** ({top['code']})",
            f"- 当日涨幅: **{top['today_gain']:+.2f}%**",
            f"- 现价: {top['price']:.4f}（昨收 {top['last_close']:.4f}）",
            f"- 操作: **{BUY_TIME} 买入**",
            f"- 类型: {top.get('type_name', '')}",
            "",
            *format_top5_lines(ranked, top["code"]),
        ])
        for i, r in enumerate(ranked[:5], 1):
            tag = " ← TOP1" if r["code"] == top["code"] else ""
            print(f"  {i}. {r['name']:14s} {r['code']} {r['today_gain']:+.2f}%{tag}")

        title = f"T0轮动 买入{top['name']}"
        if not (pos and not pos.get("sold")):
            state["position"] = {
                "etf": top["code"],
                "name": top["name"],
                "type": top.get("type_name", ""),
                "buy_date": date.today().isoformat(),
                "buy_price": top["price"],
                "today_gain": top["today_gain"],
                "sold": False,
            }
        state["last_signal"] = {
            "timestamp": datetime.now().isoformat(),
            "skipped": False,
            "etf": top["code"],
            "name": top["name"],
            "today_gain": top["today_gain"],
            "price": top["price"],
            "regime": regime,
        }
        save_state(state)

    if dry_run:
        print("\n>>> --dry-run，跳过推送")
        return 0

    webhook = (os.getenv("DINGTALK_ROTATION_WEBHOOK") or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    if not webhook:
        print("\n>>> 钉钉未配置，跳过推送")
        return 0

    print("\n>>> 推送钉钉...")
    ok = send_dingtalk(title, "\n".join(lines))
    print(f"    {'成功' if ok else '失败'}")
    return 0 if ok else 1


def run_sell_check(dry_run: bool = False) -> int:
    print("=== T+0 ETF 动量监控 | TRIX 卖出检查 ===")
    print(f"规则: {SELL_RULE}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    if not is_trading_day():
        print("非交易日，跳过")
        return 0

    if not is_sell_check_window():
        print(f"非卖出检查时段（{SELL_CHECK_START}~{SELL_CHECK_END}），跳过")
        return 0

    state = load_state()
    pos = state.get("position")
    if not pos or pos.get("sold"):
        print("无持仓，跳过")
        return 0

    if pos.get("buy_date") == date.today().isoformat():
        print("买入当日不可按隔夜规则卖出，跳过")
        return 0

    etf = pos["etf"]
    buy_price = float(pos["buy_price"])
    etf_info = next((e for e in get_all_t0_etfs() if e["code"] == etf), None)
    if not etf_info:
        print(f"ERROR: 未知 ETF {etf}")
        return 1

    sym = etf_info["sina_symbol"]
    buy_date = pos["buy_date"]
    today = date.today().isoformat()
    now_hm = datetime.now().strftime("%H:%M")

    print(f">>> 拉取 {REGIME_PROXY} 日K 识别市场环境...")
    regime = fetch_regime()

    print(f">>> 监控 {pos['name']} ({etf}) 买入@{buy_price:.4f} ({buy_date})")
    by_day = fetch_1min_kline(sym)
    time.sleep(SINA_INTERVAL)
    if not by_day:
        print("ERROR: 无法获取 1 分 K")
        return 1

    bars_buy_day = by_day.get(buy_date, [])
    bars_today = by_day.get(today, [])
    if not bars_today:
        if time_to_min(now_hm) < time_to_min(SELL_CUTOFF):
            print(f"WARN: 当日 1 分 K 尚未就绪（{etf}），下轮重试")
            return 0
        print("ERROR: 当日 1 分 K 为空且已过卖出截止")
        return 1

    hit, sell_price, sell_time, ret_str = trix_death_cross_hit(
        buy_price, bars_buy_day, bars_today, now_hm,
    )

    q = fetch_tencent_quotes([etf]).get(etf, {})
    cur = q.get("price", 0)
    float_ret = (cur - buy_price) / buy_price * 100 if cur and buy_price else 0

    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = [
        f"### T+0 ETF 卖出检查 | {run_ts}",
        "",
        *strategy_header_lines(),
        f"**检查时点**: {now_hm} | 1分K TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD}) "
        f"有效窗口 {TRIX_MIN_SELL}~{SELL_CUTOFF}",
        "",
        *format_regime_block(regime),
        f"**持仓**: {pos['name']} ({etf}) | 类型 {pos.get('type', '')}",
        f"- 买入: {buy_date} @ {buy_price:.4f}（信号日涨幅 {pos.get('today_gain', '—')}%）",
        f"- 现价: {cur:.4f} | 浮盈 **{float_ret:+.2f}%**",
        "",
    ]

    if hit:
        print(f"TRIX 死叉触发 @ {sell_time} 价 {sell_price:.4f} 收益 {ret_str}")
        title = f"T0 ⚠️ TRIX死叉卖出{pos['name']}"
        body = [
            *header,
            f"## ⚠️ 1分K TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD}) 死叉 — 请立即卖出",
            "",
            f"- 死叉时点: **{sell_time}**",
            f"- 卖出参考价: **{sell_price:.4f}**",
            f"- 预估收益: **{ret_str}**（{FEE_NOTE}）",
        ]
        if sell_time and sell_time != now_hm:
            body.append(f"- 检测时间: {now_hm}（死叉发生于 {sell_time}）")
        body.extend([
            f"- 操作: **尽快按市价或限价卖出 {pos['name']} ({etf})**",
            f"- 规则: {SELL_RULE}",
        ])
        text = "\n".join(body)
        pos["sold"] = True
        pos["sell_date"] = today
        pos["sell_price"] = sell_price
        pos["sell_time"] = sell_time
        pos["sell_reason"] = "trix_death_cross"
        pos["alert_pushed"] = True
        state["position"] = pos
        state["last_sell_alert"] = {
            "timestamp": datetime.now().isoformat(),
            "type": "trix_death_cross",
            "etf": etf,
            "sell_time": sell_time,
            "sell_price": sell_price,
            "return_pct": ret_str,
        }
        save_state(state)
    else:
        if time_to_min(now_hm) >= time_to_min(SELL_CUTOFF):
            cutoff_price = price_at_time(bars_today, SELL_CUTOFF) or cur
            cutoff_ret = (cutoff_price - buy_price) / buy_price * 100 if cutoff_price else float_ret
            print(f"无 TRIX 死叉，{SELL_CUTOFF} 定时卖 @ {cutoff_price:.4f} 收益 {cutoff_ret:+.2f}%")
            title = f"T0轮动 {SELL_CUTOFF}定时卖{pos['name']}"
            text = "\n".join([
                *header,
                f"**{SELL_CUTOFF} 定时卖出提醒**",
                "",
                f"- 截至 {now_hm} 未触发 1分K TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD}) 死叉",
                f"- **{SELL_CUTOFF} 定时卖出**",
                f"- 卖出参考价: **{cutoff_price:.4f}**",
                f"- 预估收益: **{cutoff_ret:+.2f}%**（{FEE_NOTE}）",
            ])
            pos["sold"] = True
            pos["sell_date"] = today
            pos["sell_price"] = cutoff_price
            pos["sell_reason"] = "time_sell"
            pos["alert_pushed"] = True
            state["position"] = pos
            state["last_sell_alert"] = {
                "timestamp": datetime.now().isoformat(),
                "type": "time_sell",
                "etf": etf,
                "sell_price": cutoff_price,
                "return_pct": f"{cutoff_ret:+.2f}%",
            }
            save_state(state)
        else:
            print(f"截至 {now_hm} 未触发 TRIX 死叉，继续持仓（不推送）")
            return 0

    if dry_run:
        print("\n>>> --dry-run，跳过推送")
        return 0

    webhook = (os.getenv("DINGTALK_ROTATION_WEBHOOK") or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    if not webhook:
        print("\n>>> 钉钉未配置，跳过推送")
        return 0

    print("\n>>> 推送钉钉...")
    ok = send_dingtalk(title, text)
    print(f"    {'成功' if ok else '失败'}")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 ETF 动量监控")
    parser.add_argument("--signal", action="store_true", help="14:45 买入信号")
    parser.add_argument("--sell-check", action="store_true", help="次日 TRIX 卖出检查")
    parser.add_argument("--dry-run", action="store_true", help="仅打印不推送")
    parser.add_argument("--test-push", action="store_true", help="测试钉钉推送")
    args = parser.parse_args()

    if args.test_push:
        regime = fetch_regime()
        ok = send_dingtalk(
            "T0轮动测试",
            "\n".join([
                "### T0轮动监控测试",
                "",
                *strategy_header_lines(),
                *format_regime_block(regime),
                "这是一条测试消息，确认 T+0 ETF 推送配置正确。",
            ]),
        )
        print("成功" if ok else "失败")
        sys.exit(0 if ok else 1)

    if args.signal and args.sell_check:
        print("ERROR: --signal 与 --sell-check 不能同时使用")
        sys.exit(1)
    if not args.signal and not args.sell_check:
        print("ERROR: 请指定 --signal 或 --sell-check")
        sys.exit(1)

    code = run_signal(dry_run=args.dry_run) if args.signal else run_sell_check(dry_run=args.dry_run)
    sys.exit(code)


if __name__ == "__main__":
    main()
