#!/usr/bin/env python3
"""根据聚宽导出的真实交易明细 (transaction.csv) 重建 R3 策略回测数据.

与本地无偏回测无关 —— 直接以聚宽实盘回测的真实成交为准, 保证网页 R3 回测
曲线/逐笔/摘要全部同源、与聚宽一致(此前本地 22-26 重放 +182% 与聚宽 +1861%
差异巨大, 故改用聚宽真实成交明细).

处理逻辑:
- 跳过 已撤单 行; 成交数量带"股"单位需清洗; 卖出成交数量为负需取绝对值.
- 按标的 FIFO 配对买卖, 净仓位归零(含精确归零 <=1e-6)即闭合为一笔完整往返,
  合并同笔往返内的部分成交(部成部撤), 避免把一笔拆成多笔导致复利失真.
- 每笔 return_pct = 净盈亏 / 买入成本(含佣金), 以百分比存储.
- 净值曲线 = 各笔 return_pct 按 sell_date 排序后复利累乘.

输出: ~/.tradingagents/rotation/r3_jq_backtest.json
  { window, startDate, endDate, trading_days, source, csv_path,
    trades:[...],  # 对齐 export_to_web.py 的 _backtest_trades 格式
  }

用法:
  python3 scripts/build_r3_jq_backtest.py [--csv /path/to/transaction.csv]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

ROTATION_DIR = Path.home() / ".tradingagents" / "rotation"
DEFAULT_CSV_CANDIDATES = [
    Path.home() / "Downloads" / "transaction.csv",
    Path.home() / ".tradingagents" / "cache" / "t0_5min" / "r3_jq_transaction.csv",
]
OUT_PATH = ROTATION_DIR / "r3_jq_backtest.json"


def _fnum(s: str) -> float:
    """清洗含单位的数值 (如 '5000股', '--', '-')."""
    s = (s or "").strip()
    if s in ("", "-", "--"):
        return 0.0
    s = re.sub(r"[^0-9.\-]", "", s)
    return float(s) if s not in ("", "-") else 0.0


def _parse_inst(s: str):
    """ '黄金主题LOF(161116.XSHE)' -> ('161116', '黄金主题LOF') """
    m = re.search(r"\(([0-9]{6})\.[A-Z]+\)", s)
    code = m.group(1) if m else s
    name = s.split("(")[0]
    return code, name


def _read_rows(csv_path: Path):
    # 聚宽导出常见 GBK 编码
    for enc in ("gbk", "gb18030", "utf-8-sig", "utf-8"):
        try:
            with open(csv_path, encoding=enc) as f:
                rows = list(csv.DictReader(f))
            if rows:
                return rows
        except (UnicodeDecodeError, OSError):
            continue
    raise RuntimeError(f"无法读取 {csv_path} (编码识别失败)")


def _sell_reason(sell_time: str) -> str:
    """根据卖出委托时间推断卖出原因标签 (对齐前端 REASON_LABELS)."""
    try:
        hhmm = sell_time[:5]
        if hhmm <= "11:05":
            return "trix_death_cross"   # 09:40~11:05 TRIX 死叉/兜底
        return "time_sell"
    except Exception:
        return "trix_death_cross"


def reconstruct(csv_path: Path) -> dict:
    """完整实现: 同时保留买卖委托时间."""
    rows = _read_rows(csv_path)
    state = defaultdict(lambda: {"qty": 0.0, "buys": [], "sells": []})
    trades: list[dict] = []

    for r in rows:
        if r.get("状态") == "已撤单":
            continue
        qty = _fnum(r.get("成交数量", ""))
        if qty == 0:
            continue
        price = _fnum(r.get("成交价", ""))
        amount = abs(_fnum(r.get("成交额", "")))
        comm = _fnum(r.get("手续费", ""))
        d = r.get("日期", "")
        otime = r.get("委托时间", "")
        code, name = _parse_inst(r.get("标的", ""))
        typ = r.get("交易类型", "")

        st = state[code]
        if typ == "买":
            st["qty"] += qty
            st["buys"].append((qty, price, d, amount, comm, otime))
        else:
            sq = abs(qty)
            st["qty"] -= sq
            st["sells"].append((sq, price, d, amount, comm, otime))
            if st["qty"] <= 1e-6:
                bq = sum(b[0] for b in st["buys"])
                sq_ = sum(s[0] for s in st["sells"])
                if bq <= 1e-9 or sq_ <= 1e-9:
                    st.update({"qty": 0.0, "buys": [], "sells": []})
                    continue
                buy_amt = sum(b[3] for b in st["buys"])
                buy_comm = sum(b[4] for b in st["buys"])
                sell_amt = sum(s[3] for s in st["sells"])
                sell_comm = sum(s[4] for s in st["sells"])
                net = (sell_amt - sell_comm) - (buy_amt + buy_comm)
                bcost = buy_amt + buy_comm
                r_pct = (net / bcost * 100.0) if bcost > 1e-9 else 0.0
                eq_price = sum(b[0] * b[1] for b in st["buys"]) / bq
                ex_price = sum(s[0] * s[1] for s in st["sells"]) / sq_
                entry_d = st["buys"][0][2]
                exit_d = st["sells"][-1][2]
                entry_time = st["buys"][0][5]
                exit_time = st["sells"][-1][5]
                trades.append({
                    "signal_date": entry_d,
                    "sell_date": exit_d,
                    "etf": code,
                    "name": name,
                    "buy_price": round(eq_price, 4),
                    "sell_price": round(ex_price, 4),
                    "return_pct": round(r_pct, 4),
                    "buy_time": entry_time,
                    "sell_time": exit_time,
                    "signal_time": entry_time,
                    "sell_reason": _sell_reason(exit_time),
                    "today_gain": None,
                })
                st.update({"qty": 0.0, "buys": [], "sells": []})

    # 未平仓(文件末尾仍持有的头寸) -> 不参与收益统计, 仅记录
    open_positions = {
        c: {"qty": round(st["qty"], 0), "buys": len(st["buys"])}
        for c, st in state.items() if st["qty"] > 1e-6
    }

    # 排序(按卖出日)
    trades.sort(key=lambda t: t["sell_date"])

    # 摘要统计
    n = len(trades)
    wins = sum(1 for t in trades if t["return_pct"] > 0)
    win_rate = round(wins / n * 100, 1) if n else 0.0

    # 净值曲线 (复利), 与 export_to_web._backtest_curve 同口径
    curve: list[list[float]] = []
    nav = 1.0
    peak = 1.0
    mdd = 0.0
    for t in trades:
        nav *= 1 + t["return_pct"] / 100.0
        if nav > peak:
            peak = nav
        dd = (nav - peak) / peak
        if dd < mdd:
            mdd = dd
        try:
            from datetime import datetime as _dt
            ts = int(_dt.strptime(t["sell_date"], "%Y-%m-%d").timestamp() * 1000)
        except ValueError:
            continue
        curve.append([ts, round((nav - 1) * 100, 4)])

    total_return = round((nav - 1) * 100, 2)
    start_d = trades[0]["signal_date"]
    end_d = trades[-1]["sell_date"]
    yrs = max((date.fromisoformat(end_d) - date.fromisoformat(start_d)).days / 365.25, 1e-6)
    annual = round(((nav) ** (1 / yrs) - 1) * 100, 2)
    trading_days = round(yrs * 252)

    return {
        "source": "聚宽 joinquant_unified_single.py R3 交易明细导出 (transaction.csv)",
        "csv_path": str(csv_path),
        "window": f"{start_d}~{end_d}",
        "startDate": start_d,
        "endDate": end_d,
        "trading_days": trading_days,
        "open_positions": open_positions,
        "stats": {
            "totalReturn": total_return,
            "annualReturn": annual,
            "maxDrawdown": round(mdd * 100, 2),
            "winRate": win_rate,
            "tradeCount": n,
        },
        "trades": trades,
        "curve": curve,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="由聚宽交易明细重建 R3 回测")
    ap.add_argument("--csv", default=None, help="transaction.csv 路径")
    ap.add_argument("--out", default=str(OUT_PATH), help="输出 JSON 路径")
    args = ap.parse_args()

    csv_path = None
    if args.csv:
        csv_path = Path(args.csv)
    else:
        for c in DEFAULT_CSV_CANDIDATES:
            if c.exists():
                csv_path = c
                break
    if csv_path is None or not csv_path.exists():
        print("! 找不到 transaction.csv, 请用 --csv 指定")
        return 1

    print(f"读取聚宽交易明细: {csv_path}")
    data = reconstruct(csv_path)
    st = data["stats"]
    print(f"  往返笔数={st['tradeCount']}  总收益={st['totalReturn']}%  "
          f"年化={st['annualReturn']}%  MDD={st['maxDrawdown']}%  胜率={st['winRate']}%")
    print(f"  窗口={data['window']}  未平仓头寸={list(data['open_positions'].keys())}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已写出: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
