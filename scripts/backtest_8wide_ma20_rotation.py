"""8 只宽基 ETF 20日均线动量轮动策略 — 精确复现 & 真实性核查回测。

【网上声称】
  标的池(8只): 中证1000 / 创业板50 / 黄金 / 纳指 / 恒生 / 国证2000 / 沪深300 / 科创100
  规则:
    - 入场: 收盘价有效站上 20 日均线 且 动量强度(相对20日均线涨跌幅)排第一 → 买入
    - 出场: 持仓跌破 20 日均线 或 动量排名跌至第二及以下 → 清仓空仓
    - 每日收盘排序
  声称结果(2017-2026): 累计 +5215.88%, 同期沪深300 +63.15%, MDD 22.45%, 无年度亏损, 2024/2025 均>100%

【本脚本】
  - 数据: akshare fund_etf_hist_em(adjust="qfq") 前复权日K（避免分红跳空假跌破）
  - 动量强度定义: mom = close / MA20 - 1（"相对20日均线涨跌幅"，严格按用户描述）
  - 每标的多候选取历史最长者（自动暴露上市日期偏差）
  - 两种成交口径:
      A: 信号日收盘成交（轮动策略通用口径，可能对齐声称数字）
      B: 信号次日收盘成交（无前视，稳健对照）
  - 单边费率 0.05%（ETF 免五），可调
  - 报告: 累计/逐年/MDD/换仓次数/沪深300基准/与声称数字对比/标的可用起始日审计
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import akshare as ak

CACHE_DIR = Path.home() / ".tradingagents" / "cache" / "etf_full"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "etf_daily_qfq8.json"

# 8 只宽基标的：每只给候选代码，回测时取历史最长者（暴露后上市标的）
UNIVERSE: dict[str, list[str]] = {
    "中证1000": ["512100", "560010"],
    "创业板50": ["159949"],
    "黄金ETF": ["518880", "159934"],
    "纳指ETF": ["513100", "159941"],
    "恒生ETF": ["159920"],
    "国证2000": ["159628", "159521", "159522", "159523"],
    "沪深300": ["510300"],
    "科创100": ["588220", "588800"],
}
BENCHMARK_CODE = "510300"  # 沪深300ETF 作为"同期沪深300"基准代理


def to_sina(code: str) -> str:
    c0 = code[0]
    if c0 in "56" or c0 == "9":
        return "sh" + code
    if c0 == "1":
        return "sh" + code if code[:2] == "11" else "sz" + code
    return "sz" + code


def fetch_em_qfq(code: str, tries: int = 5) -> list | None:
    """东财前复权日K。ETF 的份额折算/拆分必须用复权数据, 否则出现假涨假跌。"""
    for k in range(tries):
        try:
            df = ak.fund_etf_hist_em(symbol=code, period="daily", adjust="qfq")
        except Exception:  # noqa: BLE001
            time.sleep(1.0 * (k + 1))
            continue
        if df is None or len(df) == 0:
            return None
        rows = [[str(r["日期"]), float(r["收盘"])] for _, r in df.iterrows()
                if r["收盘"] and float(r["收盘"]) > 0]
        rows.sort(key=lambda x: x[0])
        return rows
    return None


def fix_splits(rows: list, thr: float = 0.25) -> tuple:
    """对不复权数据自动做"拆分/份额折算"接续复权。

    ETF 单日理论最大波动 20%(创业板/科创板), 超过 thr=25% 必是折算/拆分断点。
    做法: 在断点处把此前全部历史价按 后价/前价 缩放, 得到连续的前复权序列。
    """
    n = len(rows)
    prices = [r[1] for r in rows]
    out = [0.0] * n
    factor, fixed = 1.0, 0
    for i in range(n - 1, -1, -1):
        out[i] = prices[i] * factor
        if i > 0:
            p, q = prices[i - 1], prices[i]
            if p > 0 and abs(q / p - 1) > thr:
                factor *= q / p
                fixed += 1
    return [[rows[i][0], out[i]] for i in range(n)], fixed


def fetch_sina_raw(code: str) -> list | None:
    """新浪日K(不复权) — 仅作东财失败时的回退, 存在拆分未复权风险。"""
    try:
        h = ak.fund_etf_hist_sina(symbol=to_sina(code))
    except Exception:  # noqa: BLE001
        return None
    if h is None or len(h) == 0:
        return None
    rows = [[str(r["date"]), float(r["close"])] for _, r in h.iterrows()
            if r["close"] and float(r["close"]) > 0]
    rows.sort(key=lambda x: x[0])
    return rows


def count_jumps(rows: list, thr: float = 0.15) -> int:
    """单日涨跌幅超过 thr 的次数 — 用于暴露未复权的拆分/折算断点。"""
    n = 0
    for i in range(1, len(rows)):
        p, q = rows[i - 1][1], rows[i][1]
        if p > 0 and abs(q / p - 1) > thr:
            n += 1
    return n


def fetch_universe(fresh: bool = False) -> dict:
    """返回 {name: {"code":..., "rows":[[date, close], ...]}} 前复权日K。缓存本地。"""
    if CACHE_FILE.exists() and not fresh:
        print(f"[缓存] 命中 {CACHE_FILE}")
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))

    out: dict[str, dict] = {}
    t0 = time.time()
    for name, codes in UNIVERSE.items():
        best_code, best_rows, best_src = None, None, ""
        for code in codes:
            rows = fetch_em_qfq(code)
            src = "em-qfq"
            if rows is None:
                rows = fetch_sina_raw(code)
                src = "sina-raw"
                if rows:
                    rows, nfix = fix_splits(rows)
                    if nfix:
                        src = f"sina+splitfix({nfix})"
                        print(f"  [复权修正] {name} {code}: 自动接续 {nfix} 处折算/拆分断点")
            if not rows:
                print(f"  [拉取失败] {name} {code}")
                continue
            if best_rows is None or len(rows) > len(best_rows):
                best_code, best_rows, best_src = code, rows, src
        if best_rows is None:
            print(f"  [全部失败] {name}")
            continue
        out[name] = {"code": best_code, "rows": best_rows, "src": best_src}
        jm = count_jumps(best_rows)
        flag = f"  ⚠异常跳变{jm}" if jm else ""
        print(f"  [OK] {name:8s} {best_code} [{best_src}] 起{best_rows[0][0]} "
              f"止{best_rows[-1][0]} K线{len(best_rows)}{flag}")

    # 基准(沪深300指数 sh000300, 新浪)
    try:
        h = ak.stock_zh_index_daily(symbol="sh000300")
        rows = [[str(r["date"]), float(r["close"])] for _, r in h.iterrows()
                if r["close"] and float(r["close"]) > 0]
        rows.sort(key=lambda x: x[0])
        out["__benchmark"] = {"code": "sh000300", "rows": rows}
    except Exception as e:  # noqa: BLE001
        print(f"  [基准拉取失败] sh000300: {e}")

    CACHE_FILE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"[抓取] 完成 {len(out)} 项  耗时{time.time()-t0:.0f}s  -> {CACHE_FILE}")
    return out


def _build_idx(data: dict) -> dict:
    """name -> {date: idx} 索引 + name -> rows 列表。"""
    idx_of = {}
    rows_of = {}
    for name, info in data.items():
        if name == "__benchmark":
            continue
        rows = info["rows"]
        idx_of[name] = {r[0]: i for i, r in enumerate(rows)}
        rows_of[name] = rows
    return idx_of, rows_of


def run_backtest(data: dict, *, mom_win: int = 20, fee_pct: float = 0.05,
                 start: str = "2017-01-01", next_day_exec: bool = False,
                 momentum_def: str = "ma20_dev") -> dict:
    """严格复现策略。
    next_day_exec=False → 口径A(信号日收盘成交); True → 口径B(次日收盘成交)。
    momentum_def: 'ma20_dev'=相对MA20偏离率(用户文字口径); 'ret20'=过去20日涨幅(常见轮动口径)。
    """
    idx_of, rows_of = _build_idx(data)
    all_dates = sorted({r[0] for rows in rows_of.values() for r in rows
                        if r[0] >= start})

    def ma20_mom(name: str, d: str):
        """返回 (MA20, mom) 或 (None, None)。MA20 用于"站上均线"判定(两种def共用)。"""
        ii = idx_of[name].get(d)
        if ii is None or ii < mom_win:
            return None, None
        rows = rows_of[name]
        # 标准 MA20: 含当日在内的最近 20 根收盘均值
        ma = sum(rows[ii - mom_win + 1 + k][1] for k in range(mom_win)) / mom_win
        close = rows[ii][1]
        if ma <= 0:
            return None, None
        if momentum_def == "ret20":
            base = rows[ii - mom_win][1]  # 20 个交易日前收盘
            mom = close / base - 1 if base > 0 else 0.0
        else:
            mom = close / ma - 1  # 相对 20 日均线涨跌幅
        return ma, mom

    equity = 1.0
    held: str | None = None
    switches = 0
    trades: list[dict] = []
    equity_curve: list[tuple] = []
    # pending: 口径B下, d 日产生的信号在 d+1 日收盘执行
    pending_target: str | None = None

    def _ret(name: str, d: str, di: int):
        """持仓 name 从 di-1 到 di 的收益率。"""
        rows = rows_of[name]
        if di <= 0:
            return 0.0
        p, q = rows[di - 1][1], rows[di][1]
        return q / p - 1 if p > 0 else 0.0

    for di, d in enumerate(all_dates):
        # 1. 计算当日各标的 MA20 / mom / 是否站上均线
        mom_map: dict[str, float] = {}
        above: set[str] = set()
        for name in rows_of:
            ma, mom = ma20_mom(name, d)
            if ma is None:
                continue
            close = rows_of[name][idx_of[name][d]][1]
            mom_map[name] = mom
            if close > ma:  # "有效站上20日均线"
                above.add(name)

        # target = 站上均线的标的中 mom 最大者
        if above:
            target = max(above, key=lambda n: mom_map[n])
            target_mom = mom_map[target]
        else:
            target = None
            target_mom = None

        # 2. 执行（口径A: 当日成交; 口径B: 用 pending 在今日成交）
        if next_day_exec:
            exec_target = pending_target  # d-1 日产生的信号，今日收盘执行
            # d 日产生的新信号 -> pending, 明日执行
            pending_target = target
            # 今日还要用当日 mom 判断"是否跌破/排名下降"？不——
            # 口径B严格: d 日收盘看信号，d+1 收盘成交。出场信号同样次日执行。
            # 即 exec_target 是昨日 d-1 算出的目标。
        else:
            exec_target = target

        # 3. 结算
        if exec_target is None:
            # 清仓（若持有）
            if held is not None:
                equity *= 1 + _ret(held, d, idx_of[held][d])
                equity *= 1 - fee_pct / 100
                trades.append({"date": d, "act": "SELL", "code": held,
                               "equity": round(equity, 6)})
                held = None
        else:
            if held is None:
                # 开仓
                equity *= 1 - fee_pct / 100
                held = exec_target
                switches += 1
                trades.append({"date": d, "act": "BUY", "code": held,
                               "mom": round(mom_map.get(held, 0) * 100, 2),
                               "equity": round(equity, 6)})
            elif exec_target == held:
                # 继续持有，吃当日收益
                equity *= 1 + _ret(held, d, idx_of[held][d])
            else:
                # 换仓
                equity *= 1 + _ret(held, d, idx_of[held][d])
                equity *= 1 - fee_pct / 100  # 卖
                equity *= 1 - fee_pct / 100  # 买
                old = held
                held = exec_target
                switches += 1
                trades.append({"date": d, "act": "SWITCH", "code": f"{old}->{held}",
                               "mom": round(mom_map.get(held, 0) * 100, 2),
                               "equity": round(equity, 6)})

        equity_curve.append((d, round(equity, 6)))

    # 年度收益
    yearly_eq: dict[str, float] = {}
    for d, eq in equity_curve:
        yearly_eq[d[:4]] = eq
    years = sorted(yearly_eq)
    year_ret: dict[str, float] = {}
    prev = None
    for y in years:
        if prev is None:
            year_ret[y] = yearly_eq[y] - 1
        else:
            year_ret[y] = yearly_eq[y] / yearly_eq[prev] - 1
        prev = y

    # MDD
    peak, mdd = 0.0, 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)

    return {
        "final_pct": round((equity - 1) * 100, 2),
        "final_equity": round(equity, 6),
        "switches": switches,
        "mdd_pct": round(mdd * 100, 2),
        "yearly": {y: round(v * 100, 2) for y, v in year_ret.items()},
        "trades": trades,
        "equity_curve": equity_curve,
    }


def benchmark_hs300(data: dict, start: str = "2017-01-01") -> dict:
    """沪深300ETF 买入持有累计收益(含起点对齐)。"""
    b = data.get("__benchmark")
    if not b:
        return {}
    rows = [r for r in b["rows"] if r[0] >= start]
    if len(rows) < 2:
        return {}
    eq = rows[-1][1] / rows[0][1]
    # 逐年
    by_year: dict[str, float] = {}
    for r in rows:
        by_year[r[0][:4]] = r[1]  # 取每年末收盘(后续覆盖)
    years = sorted(by_year)
    yr = {}
    prev = None
    for y in years:
        if prev is None:
            yr[y] = by_year[y] / rows[0][1] - 1
        else:
            yr[y] = by_year[y] / by_year[prev] - 1
        prev = y
    return {"final_pct": round((eq - 1) * 100, 2),
            "yearly": {y: round(v * 100, 2) for y, v in yr.items()}}


def audit_universe(data: dict) -> list:
    """每标的真实可用起始日 — 暴露 2017 年时哪些标的不存在。"""
    audit = []
    for name, info in data.items():
        if name == "__benchmark":
            continue
        rows = info["rows"]
        audit.append({
            "name": name, "code": info["code"],
            "start": rows[0][0], "end": rows[-1][0], "bars": len(rows),
            "exists_in_2017": rows[0][0] <= "2017-01-01",
        })
    return audit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--fee", type=float, default=0.05, help="单边费率% (默认0.05 ETF免五)")
    ap.add_argument("--fresh", action="store_true", help="强制重抓数据")
    args = ap.parse_args()

    print("=" * 70)
    print("8 只宽基 ETF 20日均线动量轮动 — 精确复现 & 真实性核查")
    print("=" * 70)

    data = fetch_universe(fresh=args.fresh)

    # ---- 审计: 标的可用起始日 ----
    audit = audit_universe(data)
    print("\n[标的可用性审计]")
    print(f"  {'标的':<10}{'代码':<10}{'起始日':<14}{'结束日':<14}{'K线':<8}{'2017时存在'}")
    missing_2017 = []
    for a in audit:
        flag = "YES" if a["exists_in_2017"] else "NO ←后上市"
        print(f"  {a['name']:<10}{a['code']:<10}{a['start']:<14}{a['end']:<14}"
              f"{a['bars']:<8}{flag}")
        if not a["exists_in_2017"]:
            missing_2017.append(f"{a['name']}({a['code']}, {a['start'][:7]})")
    if missing_2017:
        print(f"\n  ⚠ 提示: 声称'2017起8只回测', 实际以下标的不在2017年上市:")
        for m in missing_2017:
            print(f"      - {m}")
        print("    ⇒ 2017年标的池不含这些标的, 它们上市后(国证2000 2022-07 / 科创100 2023-09)才加入。")

    # ---- 回测: 仅用户指定口径 (相对MA20偏离率 + 当日收盘成交) ----
    rA = run_backtest(data, fee_pct=args.fee, start=args.start, next_day_exec=False)
    bm = benchmark_hs300(data, args.start)

    print("\n" + "=" * 70)
    print(f"[逐年盈利]  起点 {args.start}  单边费 {args.fee}%  "
          f"动量=相对MA20偏离率  当日收盘成交")
    print("=" * 70)
    bm_yearly = bm.get("yearly", {})
    print(f"  {'年份':<8}{'策略收益':<14}{'沪深300基准':<14}")
    for y in sorted(rA["yearly"]):
        s = "{:+.2f}%".format(rA["yearly"][y])
        b = "{:+.2f}%".format(bm_yearly[y]) if y in bm_yearly else "  -"
        print(f"  {y:<8}{s:<14}{b:<14}")
    print("-" * 70)
    print(f"  {'累计':<8}"
          f"{'{:+.2f}%'.format(rA['final_pct']):<14}"
          f"{'{:+.2f}%'.format(bm.get('final_pct', 0)):<14}")
    print(f"\n  最大回撤: {rA['mdd_pct']:.2f}%   换仓次数: {rA['switches']}")

    negA = [y for y, v in rA["yearly"].items() if v < 0]
    print(f"  策略亏损年份({len(negA)}): {negA if negA else '无'}")

    out = {
        "audit": audit, "missing_in_2017": missing_2017,
        "result": rA, "benchmark_hs300": bm,
        "config": {"start": args.start, "fee": args.fee,
                   "momentum_def": "ma20_dev", "exec": "same_day_close"},
    }
    op = CACHE_DIR / "rotation_8wide_ma20_result.json"
    op.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[保存] {op}")
    tp = CACHE_DIR / "rotation_8wide_trades.csv"
    with tp.open("w", encoding="utf-8") as f:
        f.write("date,action,code,mom,equity\n")
        for t in rA["trades"]:
            f.write(f"{t['date']},{t['act']},{t['code']},{t.get('mom','')},"
                    f"{t['equity']}\n")
    print(f"[保存] {tp}  ({len(rA['trades'])} 笔)")


if __name__ == "__main__":
    main()
