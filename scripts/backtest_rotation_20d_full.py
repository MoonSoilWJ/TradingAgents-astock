"""全场 ETF 20日动量轮动回测（top1 换仓才换，修正版）。

数据：akshare fund_etf_spot_em 全场清单 -> fund_etf_hist_sina 全量日K（缓存本地）。
策略：每日在全场 ETF 中选 20 日涨幅最高者；首仓/换仓日收盘买入（不计当日收益、
      仅扣手续费）；持有日吃当日涨幅；仅当 top1 易主才卖出旧仓、买入新仓。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import akshare as ak

CACHE_DIR = Path.home() / ".tradingagents" / "cache" / "etf_full"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "etf_daily.json"

FEE = 0.05  # 单边手续费 %（ETF 免五标准）
MOM_WIN = 20  # 动量窗口（交易日）
WINDOW_START = "2022-07-01"  # 只测最近 4 年


# ----------------------------- 数据抓取 -----------------------------------
def to_sina(code: str) -> str:
    c0 = code[0]
    if c0 in "56" or c0 == "9":
        return "sh" + code
    if c0 == "1":
        return "sh" + code if code[:2] == "11" else "sz" + code
    return "sz" + code


def fetch_universe() -> dict[str, list]:
    """返回 {code: [[date, close], ...]} 按日期升序，缓存本地。"""
    if CACHE_FILE.exists():
        print(f"[缓存] 命中 {CACHE_FILE}")
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))

    print("[清单] fund_etf_spot_em 获取全场...")
    spot = ak.fund_etf_spot_em()
    etfs = spot[spot["名称"].str.contains("ETF", na=False)]
    etfs = etfs[~etfs["名称"].str.contains("联接|LOF", na=False)]
    codes = etfs["代码"].astype(str).tolist()
    print(f"[清单] 过滤后 ETF 数量: {len(codes)}")

    data: dict[str, list] = {}
    ok = fail = 0
    t0 = time.time()
    for i, code in enumerate(codes):
        s = to_sina(code)
        try:
            h = ak.fund_etf_hist_sina(symbol=s)
        except Exception:
            h = None
        if h is None or len(h) == 0:
            fail += 1
            continue
        rows = [[str(r["date"]), float(r["close"])] for _, r in h.iterrows()]
        if len(rows) >= 21:  # 至少够算一次动量
            data[code] = rows
            ok += 1
        if (i + 1) % 100 == 0:
            print(f"  [{i+1}/{len(codes)}] ok={ok} fail={fail} "
                  f"耗时{time.time()-t0:.0f}s")
    print(f"[抓取] 完成 ok={ok} fail={fail} 总耗时{time.time()-t0:.0f}s")
    CACHE_FILE.write_text(
        json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return data


# ----------------------------- 回测 ---------------------------------------
def run(data: dict[str, list]):
    # 建索引 code -> {date: idx}
    idx_of = {c: {r[0]: i for i, r in enumerate(rows)} for c, rows in data.items()}
    # 全体交易日并集，按日期升序（仅最近4年窗口）
    all_dates = sorted({r[0] for rows in data.values() for r in rows
                        if r[0] >= WINDOW_START})

    equity = 1.0
    held = None
    switches = 0
    trades: list[dict] = []
    equity_curve: list[tuple[str, float]] = []
    yearly: dict[str, float] = {}

    for di, d in enumerate(all_dates):
        # 候选：当日及 20 日前都有数据的 ETF
        cands = []
        for c, rows in data.items():
            ii = idx_of[c].get(d)
            if ii is None or ii < MOM_WIN:
                continue
            p = rows[ii - MOM_WIN][1]
            q = rows[ii][1]
            if p > 0 and q > 0:
                cands.append((q / p - 1, c))
        if not cands:
            if held is not None:
                ii = idx_of[held].get(d)
                if ii and ii > 0:
                    p = data[held][ii - 1][1]
                    q = data[held][ii][1]
                    if p > 0 and q > 0:
                        equity *= (1 + (q / p - 1))
            continue

        top_m, top_code = max(cands)
        if held is None:
            equity *= (1 - FEE / 100)
            held = top_code
            switches += 1
            trades.append({"date": d, "action": "BUY", "code": top_code,
                           "mom": round(top_m * 100, 2), "equity": round(equity, 6)})
        elif top_code != held:
            ii = idx_of[held].get(d)
            if ii and ii > 0:
                p = data[held][ii - 1][1]
                q = data[held][ii][1]
                if p > 0 and q > 0:
                    equity *= (1 + (q / p - 1))
            equity *= (1 - FEE / 100)
            trades.append({"date": d, "action": "SELL", "code": held,
                           "mom": None, "equity": round(equity, 6)})
            equity *= (1 - FEE / 100)
            held = top_code
            switches += 1
            trades.append({"date": d, "action": "BUY", "code": top_code,
                           "mom": round(top_m * 100, 2), "equity": round(equity, 6)})
        else:
            ii = idx_of[held].get(d)
            if ii and ii > 0:
                p = data[held][ii - 1][1]
                q = data[held][ii][1]
                if p > 0 and q > 0:
                    equity *= (1 + (q / p - 1))

        equity_curve.append((d, equity))
        y = d[:4]
        yearly.setdefault(y, equity)

    # 年度收益（基于年末净值 / 年初净值）
    years = sorted(yearly)
    year_ret: dict[str, float] = {}
    if years:
        base = 1.0
        yprev = None
        for y in years:
            if yprev is None:
                year_ret[y] = yearly[y] / base - 1
            else:
                # 用上一年末净值作基数
                year_ret[y] = yearly[y] / yearly[yprev] - 1
            yprev = y

    # 最大回撤
    peak = 0.0
    mdd = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)

    return {
        "config": {"fee": FEE, "mom_win": MOM_WIN, "universe_size": len(data)},
        "final_equity": round(equity, 6),
        "final_pct": round((equity - 1) * 100, 2),
        "switches": switches,
        "last_held": held,
        "max_drawdown_pct": round(mdd * 100, 2),
        "yearly": {y: round(v * 100, 2) for y, v in year_ret.items()},
        "trades": trades,
        "equity_curve": [(d, round(e, 6)) for d, e in equity_curve],
    }


def benchmark_ew(data: dict[str, list]):
    """等权买入持有（每日等权再平衡近似：各 ETF 当日收益等权平均复利）。"""
    all_dates = sorted({r[0] for rows in data.values() for r in rows
                        if r[0] >= WINDOW_START})
    idx_of = {c: {r[0]: i for i, r in enumerate(rows)} for c, rows in data.items()}
    eq = 1.0
    for d in all_dates:
        rs = []
        for c, rows in data.items():
            ii = idx_of[c].get(d)
            if ii and ii > 0:
                p = rows[ii - 1][1]
                q = rows[ii][1]
                if p > 0 and q > 0:
                    rs.append(q / p - 1)
        if rs:
            eq *= (1 + sum(rs) / len(rs))
    return round((eq - 1) * 100, 2), round(eq, 4)


if __name__ == "__main__":
    print("=== 全场 ETF 20日动量轮动（修正版）===")
    data = fetch_universe()
    print(f"[数据] ETF 数: {len(data)}  样本区间: "
          f"{min(r[0][0] for r in data.values())} ~ "
          f"{max(r[-1][0] for r in data.values())}")
    res = run(data)
    ew_pct, ew_eq = benchmark_ew(data)

    print(f"\n[策略] 累计收益: {res['final_pct']:+.2f}%  (净值 {res['final_equity']:.4f}x)")
    print(f"[策略] 换仓次数: {res['switches']}  末持仓: {res['last_held']}")
    print(f"[策略] 最大回撤: {res['max_drawdown_pct']}%")
    print(f"[基准] 等权买入持有全场: {ew_pct:+.2f}%  (净值 {ew_eq:.4f}x)")
    print("\n[逐年收益]")
    for y, v in res["yearly"].items():
        print(f"  {y}: {v:+.2f}%")
    print(f"\n[对比] 之前 106 只 T+0 子集结果: -72.88%")

    out = CACHE_DIR / "rotation_20d_full_result.json"
    res["benchmark_ew_pct"] = ew_pct
    out.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    print(f"\n[保存] {out}")
