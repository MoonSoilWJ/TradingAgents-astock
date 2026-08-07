#!/usr/bin/env python3
"""资金流入 Top1 隔夜策略回测（最近 N 日）。

选股：每个交易日「主力净流入」排名第一的 A 股，且
      - 非创业板(30x) / 非科创板(688x)
      - 未涨停（精确：当日 14:45 真实价 vs 前收×涨跌停幅度，主板 10% / 北交所 30%）
买入：当日 14:45 买入（5 分钟 bar 14:45 收盘价 = 14:45 时刻价）
卖出：次日 09:40 之后 TRIX(5,3) 死叉卖出；若 11:05 前无死叉，则 11:05 强制平仓。

数据来源与口径
  - 资金流排名：直连东财 push2his /api/qt/stock/fflow/daykline/get（逐股历史主力净流入，
    按日落盘缓存）。注意：东财 clist 的 date 参数对资金流排名「无效」（返回实时快照），
    故必须逐股取历史。默认 Universe = 沪深300+中证500+中证1000 成分股（约 1800 只，
    覆盖绝大多数每日主力净流入榜首；--universe all 可扩至全市场）。
  - 分钟行情 / 前收：pytdx 通达信 get_history_minute_time_data（与实盘同源，无东财依赖）。
  - 股票 1 分钟价量均为 1:1（×10 量纲偏差仅见于个别 ETF），故不做量纲缩放。
  - 卖点与现有 T0 实盘策略完全一致（simulate_trix_cross_after, 09:40~11:05）。
  - 手续费按 A 股真实成本：买入佣万 3 + 卖出佣万 3 + 印花税万 5。

用法
  python scripts/backtest_fundflow_top1.py --days 10 --save
  python scripts/backtest_fundflow_top1.py --days 10 --prefetch-only   # 仅建资金流缓存
  python scripts/backtest_fundflow_top1.py --days 10 --universe all     # 全市场(慢)
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import akshare as ak  # noqa: E402  (仅用于 load_universe 取成分股，结果已缓存)
import requests  # noqa: E402
from pytdx.hq import TdxHq_API  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

CACHE_DIR = Path.home() / ".tradingagents" / "cache" / "fundflow"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
FFLOW_DIR = CACHE_DIR / "fflow"
FFLOW_DIR.mkdir(parents=True, exist_ok=True)

SERVERS = [
    ("115.238.56.198", 7709), ("115.238.90.165", 7709),
    ("180.153.18.170", 7709), ("218.108.98.244", 7709),
    ("123.125.108.14", 7709), ("60.28.23.80", 7709),
]

TRIX_PERIOD = 5
TRIX_SIGNAL_PERIOD = 3
BUY_TIME = "14:45"
MIN_SELL_TIME = "09:40"
MAX_SELL_TIME = "11:05"

BUY_FEE = 0.0003   # 买入佣金 万3
SELL_FEE = 0.0008  # 卖出佣金 万3 + 印花税 万5

EM_FFLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"


# ---------- 通达信 1 分钟拉取 + 聚合 5 分钟 ----------
def connect() -> TdxHq_API:
    api = TdxHq_API()
    for h, p in SERVERS:
        try:
            if api.connect(h, p, time_out=5):
                return api
        except Exception:
            continue
    raise RuntimeError("无可用通达信服务器")


def reconnect_any() -> TdxHq_API:
    api = TdxHq_API()
    for h, p in SERVERS:
        try:
            if api.connect(h, p, time_out=5):
                return api
        except Exception:
            continue
    raise RuntimeError("无可用通达信服务器")


def market_of(code: str) -> int:
    return 1 if code[0] in ("5", "6") else 0


def date_int(s: str) -> int:
    return int(s.replace("-", ""))


def recon_5min(raw, day_str: str, scale: float = 1.0) -> list[dict] | None:
    if not raw:
        return None
    five: list[dict] = []
    for g in range(0, len(raw), 5):
        chunk = raw[g:g + 5]
        if not chunk:
            continue
        op = float(chunk[0]["price"]) * scale
        cl = float(chunk[-1]["price"]) * scale
        hi = max(float(b["price"]) for b in chunk) * scale
        lo = min(float(b["price"]) for b in chunk) * scale
        seg = g // 5
        if seg < 23:
            total = 9 * 60 + 30 + (seg + 1) * 5
        else:
            total = 13 * 60 + (seg - 23 + 1) * 5
        hh, mm = divmod(total, 60)
        t = f"{hh:02d}:{mm:02d}"
        five.append({
            "open": op, "high": hi, "low": lo, "close": cl,
            "time": t, "day": day_str, "datetime": f"{day_str} {t}:00",
        })
    return five


def safe_get_minute(api: TdxHq_API, code: str, day_str: str, timeout: float = 10.0) -> list | None:
    mkt = market_of(code)
    di = date_int(day_str)
    for _ in range(3):
        box: dict = {}
        def _call() -> None:
            try:
                box["v"] = api.get_history_minute_time_data(mkt, code, di)
            except Exception as e:  # noqa: BLE001
                box["e"] = e
        th = threading.Thread(target=_call, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            try:
                api.disconnect()
            except Exception:
                pass
            try:
                api = reconnect_any()
            except Exception:
                pass
            continue
        if box.get("v"):
            return box["v"]
        if "e" in box:
            try:
                api.disconnect()
            except Exception:
                pass
            try:
                api = reconnect_any()
            except Exception:
                pass
    return None


_minute_cache: dict = {}
_prevclose_cache: dict = {}


def get_5min(api, code: str, day_str: str) -> list | None:
    key = f"{code}_{date_int(day_str)}"
    if key in _minute_cache:
        return _minute_cache[key]
    raw = safe_get_minute(api, code, day_str)
    bars = recon_5min(raw, day_str, scale=1.0) if raw else None
    _minute_cache[key] = bars
    return bars


def get_prev_close(api, code: str, day_str: str) -> float | None:
    key = f"{code}_{date_int(day_str)}_prev"
    if key in _prevclose_cache:
        return _prevclose_cache[key]
    bars = get_5min(api, code, day_str)
    pc = float(bars[-1]["close"]) if bars else None
    _prevclose_cache[key] = pc
    return pc


# ---------- TRIX(5,3) 卖点（与实盘 t0_monitor 完全一致） ----------
def calc_trix(closes, n):
    closes = [c for c in closes if c and c > 0]
    if len(closes) < n:
        return [0.0] * len(closes)
    ema1 = [0.0] * len(closes)
    k = 2.0 / (n + 1)
    ema1[0] = closes[0]
    for i in range(1, len(closes)):
        ema1[i] = ema1[i - 1] + k * (closes[i] - ema1[i - 1])
    ema2 = [0.0] * len(closes)
    ema2[0] = ema1[0]
    for i in range(1, len(closes)):
        ema2[i] = ema2[i - 1] + k * (ema1[i] - ema2[i - 1])
    ema3 = [0.0] * len(closes)
    ema3[0] = ema2[0]
    for i in range(1, len(closes)):
        ema3[i] = ema3[i - 1] + k * (ema2[i] - ema3[i - 1])
    prev = ema3[0]
    trix = [0.0]
    for i in range(1, len(closes)):
        cur = ema3[i]
        trix.append((cur - prev) / prev * 100 if prev else 0.0)
        prev = cur
    return trix


def calc_trix_signal(trix, m):
    sig = [0.0] * len(trix)
    if len(trix) < m:
        return trix[:]
    for i in range(len(trix)):
        if i + 1 < m:
            sig[i] = sum(trix[:i + 1]) / (i + 1)
        else:
            sig[i] = sum(trix[i + 1 - m:i + 1]) / m
    return sig


def time_to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def bar_clock(bar: dict) -> str:
    day = bar.get("day", "")
    if " " in day:
        return day.split(" ")[1][:5]
    return bar.get("time", "00:00:00")[:5]


def bars_for_trix(bars: list[dict]) -> list[dict]:
    return [{"close": b["close"], "day": b.get("datetime", b["day"])} for b in bars]


def simulate_trix_cross_after(buy_cost, min_bars_today, min_bars_next,
                              trix_period=TRIX_PERIOD, trix_signal_period=None,
                              min_sell_time=MIN_SELL_TIME, max_sell_time=None):
    if trix_signal_period is None:
        trix_signal_period = max(trix_period // 2, 3)
    all_bars = min_bars_today + min_bars_next
    min_warmup = trix_period * 3 + 5
    if len(all_bars) < min_warmup:
        last_close = float(min_bars_next[-1].get("close", 0)) if min_bars_next else buy_cost
        return (last_close - buy_cost) / buy_cost * 100, "close", {"reason": "insufficient_data", "sell_price": last_close}
    warmup_len = len(min_bars_today)
    closes = [float(b.get("close", 0)) for b in all_bars]
    trix = calc_trix(closes, trix_period)
    signal = calc_trix_signal(trix, trix_signal_period)
    min_sell_min = time_to_min(min_sell_time)
    max_sell_min = time_to_min(max_sell_time) if max_sell_time else None
    search_start = max(warmup_len, min_warmup)
    for i in range(search_start, len(all_bars)):
        clock_min = time_to_min(bar_clock(all_bars[i]))
        if clock_min < min_sell_min:
            continue
        if max_sell_min is not None and clock_min > max_sell_min:
            break
        if trix[i - 1] >= signal[i - 1] and trix[i] < signal[i]:
            sell_price = closes[i]
            bar = all_bars[i]
            sell_date = str(bar.get("day", ""))[:10]
            if not sell_date and " " in str(bar.get("datetime", "")):
                sell_date = str(bar["datetime"]).split(" ", 1)[0]
            return (sell_price - buy_cost) / buy_cost * 100, "trix_death_cross", {
                "sell_price": sell_price, "bar": bar.get("day", ""),
                "sell_date": sell_date, "sell_time": bar_clock(bar),
            }
    if max_sell_min is not None:
        cutoff_idx = None
        for i in range(warmup_len, len(all_bars)):
            if time_to_min(bar_clock(all_bars[i])) <= max_sell_min:
                cutoff_idx = i
        if cutoff_idx is not None:
            bar = all_bars[cutoff_idx]
            sell_price = closes[cutoff_idx]
            sell_date = str(bar.get("day", ""))[:10]
            return (sell_price - buy_cost) / buy_cost * 100, "time_sell", {
                "reason": "cutoff_time_sell", "sell_price": sell_price,
                "bar": bar.get("day", ""), "sell_date": sell_date,
                "sell_time": bar_clock(bar),
            }
    last_close = closes[-1] if closes else buy_cost
    last_bar = all_bars[-1] if all_bars else {}
    sell_date = str(last_bar.get("day", ""))[:10]
    if not sell_date and " " in str(last_bar.get("datetime", "")):
        sell_date = str(last_bar["datetime"]).split(" ", 1)[0]
    return (last_close - buy_cost) / buy_cost * 100, "close", {
        "reason": "no_death_cross", "sell_price": last_close,
        "sell_date": sell_date, "sell_time": bar_clock(last_bar) if last_bar else "",
    }


# ---------- 资金流排名（直连东财历史 daykline） ----------
def _secid(code: str) -> str:
    return ("1." if code[0] in ("5", "6") else "0.") + code


def fflow_daykline(code: str) -> dict:
    """返回 {date: 主力净流入额(元)}，取自东财 push2his daykline（最近 ~120 日）。"""
    params = {
        "lmt": "0", "klt": "101",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "secid": _secid(code), "beg": "20250101", "end": "20300101",
    }
    for _ in range(5):
        try:
            r = requests.get(EM_FFLOW_URL, params=params, timeout=12)
            j = r.json()
            if j.get("data") and j["data"].get("klines"):
                out: dict[str, float] = {}
                for kl in j["data"]["klines"]:
                    parts = kl.split(",")
                    if len(parts) < 2:
                        continue
                    try:
                        out[parts[0]] = float(parts[1])  # f52 主力净流入额
                    except Exception:
                        pass
                return out
        except Exception:
            time.sleep(0.5)
    return {}


def load_universe(universe: str) -> dict:
    cache_file = CACHE_DIR / f"universe_{universe}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))
    codes: dict[str, str] = {}
    if universe == "all":
        df = ak.stock_info_a_code_name()
        for _, row in df.iterrows():
            c = str(row["code"]).zfill(6)
            codes[c] = row["name"]
    else:
        for idx in ("000300", "000905", "000852"):
            try:
                df = ak.index_stock_cons(symbol=idx)
            except Exception:
                continue
            col = "成分券代码" if "成分券代码" in df.columns else "证券代码"
            nmcol = "成分券名称" if "成分券名称" in df.columns else "证券名称"
            for _, row in df.iterrows():
                c = str(row[col]).zfill(6)
                codes[c] = row.get(nmcol, "")
    codes = {c: n for c, n in codes.items() if not (c.startswith("30") or c.startswith("688"))}
    cache_file.write_text(json.dumps(codes, ensure_ascii=False), encoding="utf-8")
    return codes


def ensure_fund_flow(universe_codes: dict) -> None:
    done = 0
    total = len(universe_codes)
    ok = 0
    for code in universe_codes:
        fpath = FFLOW_DIR / f"{code}.json"
        if fpath.exists():
            done += 1
            ok += 1
            continue
        val = fflow_daykline(code)  # 内部已重试
        if not val:
            # 空结果不缓存，便于重跑补齐（网络间歇封禁）
            done += 1
            if done % 50 == 0:
                print(f"  [资金流缓存] {done}/{total} (ok={ok})", flush=True)
            continue
        fpath.write_text(json.dumps(val, ensure_ascii=False), encoding="utf-8")
        done += 1
        ok += 1
        if done % 50 == 0:
            print(f"  [资金流缓存] {done}/{total} (ok={ok})", flush=True)
        time.sleep(0.03)
    print(f"  本轮资金流获取 ok={ok}/{total}")


def build_ranking(date_str: str, universe_codes: dict) -> list[dict]:
    out = []
    for code, name in universe_codes.items():
        fpath = FFLOW_DIR / f"{code}.json"
        if not fpath.exists():
            continue
        try:
            val = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        if date_str not in val:
            continue
        v = val[date_str]
        if v is None:
            continue
        out.append({"code": code, "name": name, "f62": float(v)})
    out.sort(key=lambda x: x["f62"], reverse=True)
    return out


def get_trade_dates(n_days: int) -> list[str]:
    for anchor in ("000001", "600519", "601318", "600036", "300750"):
        val = fflow_daykline(anchor)
        dates = sorted(val.keys())
        if len(dates) >= n_days + 1:
            return dates[-(n_days + 1):]
    return []


# ---------- 统计 ----------
def _calc_stats(rets: list[float]) -> dict:
    if not rets:
        return {"count": 0, "win_rate": 0.0, "avg": 0.0, "max_drawdown": 0.0, "sharpe": 0.0}
    win = sum(1 for r in rets if r > 0)
    avg = sum(rets) / len(rets)
    eq = [1.0]
    for r in rets:
        eq.append(eq[-1] * (1 + r / 100))
    peak = eq[0]
    mdd = 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak * 100)
    mean = avg / 100
    std = (sum((r / 100 - mean) ** 2 for r in rets) / len(rets)) ** 0.5
    sharpe = (mean / std * (252 ** 0.5)) if std > 0 else 0.0
    return {"count": len(rets), "win_rate": win / len(rets) * 100,
            "avg": avg, "max_drawdown": mdd, "sharpe": sharpe}


def net_return(buy: float, sell: float, buy_fee=BUY_FEE, sell_fee=SELL_FEE) -> float:
    return (sell * (1 - sell_fee) - buy * (1 + buy_fee)) / (buy * (1 + buy_fee)) * 100


def board_limit_ratio(code: str) -> float:
    return 0.30 if code.startswith("8") else 0.10


def main() -> None:
    ap = argparse.ArgumentParser(description="资金流入 Top1 隔夜策略回测")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--top-k", type=int, default=1)
    ap.add_argument("--universe", type=str, default="index",
                    help="index=沪深300+500+1000(默认) / all=全市场(慢)")
    ap.add_argument("--prefetch-only", action="store_true", help="仅建资金流缓存")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--buy-fee", type=float, default=BUY_FEE)
    ap.add_argument("--sell-fee", type=float, default=SELL_FEE)
    args = ap.parse_args()

    dates = get_trade_dates(args.days)
    if not dates:
        print("无法获取交易日期（资金流接口不可用）。请稍后重试。")
        return
    buy_dates = dates[:args.days]
    print(f"=== 资金流入 Top{args.top_k} 隔夜策略回测（最近 {len(buy_dates)} 日）===")
    print(f"买入: {BUY_TIME} | 卖出: 次日 TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD}) 死叉(≥{MIN_SELL_TIME}) 或 {MAX_SELL_TIME} 强平")
    print(f"Universe: {args.universe} | 费率: 买{args.buy_fee*1e4:.1f}‰ 卖{args.sell_fee*1e4:.1f}‰")
    print(f"买入日区间: {buy_dates[0]} ~ {buy_dates[-1]}\n")

    universe = load_universe(args.universe)
    print(f"Universe 规模: {len(universe)} 只（已排除创业/科创）")
    print(">>> 拉取历史主力净流入（已缓存则跳过）...")
    ensure_fund_flow(universe)
    if args.prefetch_only:
        print("资金流缓存完成。")
        return

    api = connect()
    trades: list[dict] = []
    skipped: list[str] = []
    for D in buy_dates:
        ranking = build_ranking(D, universe)
        if not ranking:
            skipped.append(D)
            continue
        pick = None
        buy_price = None
        for idx in range(args.top_k - 1, len(ranking)):
            cand = ranking[idx]
            day_bars = get_5min(api, cand["code"], D)
            if not day_bars:
                continue
            buy_bar = next((b for b in day_bars if b["time"] == BUY_TIME), None)
            if not buy_bar:
                continue
            bp = float(buy_bar["close"])
            # 前收（前一日 5 分钟收盘）
            prev_day = dates[dates.index(D) - 1] if dates.index(D) > 0 else None
            prev_close = get_prev_close(api, cand["code"], prev_day) if prev_day else None
            lim = board_limit_ratio(cand["code"])
            limit_up = round(prev_close * (1 + lim), 2) if prev_close else None
            chg = (bp - prev_close) / prev_close * 100 if prev_close else 0
            if limit_up is not None and (bp >= limit_up - 0.01 or chg >= (lim * 100 - 0.2)):
                continue
            pick = cand
            buy_price = bp
            break
        if pick is None:
            skipped.append(D)
            continue

        nxt = dates[dates.index(D) + 1]
        nxt_bars = get_5min(api, pick["code"], nxt)
        day_bars = get_5min(api, pick["code"], D)
        if not nxt_bars or not day_bars:
            skipped.append(D)
            continue

        _, sell_reason, detail = simulate_trix_cross_after(
            buy_price, bars_for_trix(day_bars), bars_for_trix(nxt_bars),
            trix_period=TRIX_PERIOD, trix_signal_period=TRIX_SIGNAL_PERIOD,
            min_sell_time=MIN_SELL_TIME, max_sell_time=MAX_SELL_TIME,
        )
        sell_price = detail.get("sell_price")
        if sell_price is None:
            sell_price = float(nxt_bars[-1]["close"])
        sell_time = detail.get("sell_time", "")
        sell_date = detail.get("sell_date", nxt)
        ret = net_return(buy_price, sell_price, args.buy_fee, args.sell_fee)
        trades.append({
            "buy_date": D, "code": pick["code"], "name": pick["name"],
            "inflow_yi": round(pick["f62"] / 1e8, 3),
            "buy_time": BUY_TIME, "buy_price": round(buy_price, 3),
            "sell_date": sell_date, "sell_time": sell_time,
            "sell_price": round(sell_price, 3),
            "sell_reason": sell_reason, "return_pct": round(ret, 3),
        })
        print(f"  {D} {pick['code']} {pick['name']:<8} 流入{pick['f62']/1e8:+.1f}亿 | "
              f"买{buy_price:.2f}→卖{sell_price:.2f}@{sell_time}({sell_reason}) {ret:+.2f}%")

    print()
    if not trades:
        print("无成交（资金流缓存未覆盖该日 / 候选均涨停或行情缺失）。可先 --prefetch-only 再跑。")
        return
    rets = [t["return_pct"] for t in trades]
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    stats = _calc_stats(rets)
    dc = sum(1 for t in trades if t["sell_reason"] == "trix_death_cross")
    tc = sum(1 for t in trades if t["sell_reason"] == "time_sell")
    print("─" * 100)
    print(f"  成交 {len(trades)} 笔 | 跳过 {len(skipped)} 天 | 累计 {(eq-1)*100:+.2f}%")
    print(f"  胜率 {stats['win_rate']:.1f}% | 均笔 {stats['avg']:+.2f}% | "
          f"最大回撤 {stats['max_drawdown']:+.2f}% | 夏普 {stats['sharpe']:.2f}")
    print(f"  卖点分布: TRIX死叉 {dc} 笔 / {MAX_SELL_TIME}强平 {tc} 笔")
    print("─" * 100)

    if args.save:
        out_dir = Path.home() / ".tradingagents" / "rotation"
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d_%H%M")
        payload = {
            "config": {
                "days": args.days, "top_k": args.top_k, "buy_time": BUY_TIME,
                "sell": f"TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD})>= {MIN_SELL_TIME} or {MAX_SELL_TIME}",
                "universe": args.universe, "buy_fee": args.buy_fee, "sell_fee": args.sell_fee,
                "buy_range": [buy_dates[0], buy_dates[-1]],
            },
            "summary": {
                "trades": len(trades), "skipped": len(skipped),
                "total_return_pct": round((eq - 1) * 100, 2),
                "stats": stats, "trix_sell": dc, "time_sell": tc,
            },
            "trades": trades,
            "skipped_dates": skipped,
        }
        path = out_dir / f"fundflow_top1_{tag}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已保存: {path}")


if __name__ == "__main__":
    main()
