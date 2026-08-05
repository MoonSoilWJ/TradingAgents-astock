"""
实验：进攻腿入场范式根本性重构 —— 时间序列动量(TSMOM) vs 现状单日截面尖峰
=================================================================================
核心论点：现状 B 的"今天谁涨最多≥3%就买、明天卖"是【单日截面噪声信号】，
          加 MA 门禁只是打补丁。本脚本用【时间序列动量 + 多日持有】这一
          根本不同的范式替换它，验证是否真能打。

三种引擎同台（同一份无偏数据 2022-11-15~2026-07-31）：
  B   : 单日涨幅 Top1≥3% → 次日 TRIX 卖 (1日持仓, 现状)
  X   : 候选中【过去L日动量最强 + 收盘>MA】者，多日持有，
        跌破MA / 触及trailing stop / 到最大持仓日才卖  (TSMOM)
  Y   : 同上但 L=60 (更慢趋势)

数据：etf_daily 日线（可靠），用日收盘进出场（标准趋势跟随口径）。
用法：python3 scripts/exp_tsmom.py
"""
import json, sys
from pathlib import Path
from collections import defaultdict

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from backtest_t0_today1 import FEE_PCT  # noqa: E402
from backtest_b_idle_merge import build_picks_B  # noqa: E402
from backtest_t0_hybrid_sell import run_strategy  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / '.tradingagents/cache/t0_5min'
PRE2024_FILE = CACHE / 'tdx_5min_pre2024.json'
TWOY_FILE    = CACHE / 'tdx_5min_2y.json'
ALIGNED      = CACHE / 'aligned_live_4y.json'
START = '2022-11-15'
END   = '2026-07-31'
FEE   = FEE_PCT


def load():
    cache = json.loads(ALIGNED.read_text(encoding='utf-8'))
    etf_daily = cache['etf_daily']
    etf_5min = {}
    for part in (PRE2024_FILE, TWOY_FILE):
        dense = json.loads(Path(part).read_text(encoding='utf-8'))['etf_5min']
        for c, days in dense.items():
            etf_5min.setdefault(c, {}).update(days)
    ALL_DATES = sorted({d for days in etf_5min.values() for d in days})
    CODES5 = set(etf_5min.keys())
    etf_list = [e for e in get_all_t0_etfs() if e['code'] in CODES5]
    return etf_daily, etf_5min, ALL_DATES, etf_list


def build_series(etf_daily, etf_list):
    """code -> (sorted_dates, close_list) 日线序列。"""
    series = {}
    for etf in etf_list:
        code = etf['code']
        recs = etf_daily.get(code, {}).get('returns', [])
        dmap = {r['date']: r['close'] for r in recs if r.get('close')}
        if len(dmap) < 60:
            continue
        ds = sorted(dmap)
        series[code] = (ds, [dmap[d] for d in ds])
    return series


def run_tsmom(series, L, MA, HMAX, STOP, label):
    """时间序列动量 + 多日持有。单标的、满仓、一次一个仓位。"""
    # 全局交易日历 = 所有标的日期并集
    cal = sorted({d for ds, _ in series.values() for d in ds})
    # 每个标的的 date->idx
    idx_of = {code: {d: i for i, d in enumerate(ds)}
              for code, (ds, _) in series.items()}
    trades = []
    pos = None  # {code, entry_close, entry_cal}
    for ci, day in enumerate(cal):
        if day < START or day > END:
            continue
        # 1) 持仓处理：检查退出
        if pos is not None:
            code = pos['code']
            ds, cl = series[code]
            i = idx_of[code].get(day)
            if i is None:
                # 数据缺口：强制按上一已知收盘了结（近似）
                trades.append(_mk(pos, pos['last_close'], day))
                pos = None
            else:
                close = cl[i]
                pos['last_close'] = close
                held_days = ci - pos['entry_cal']
                ma = sum(cl[max(0, i - MA + 1):i + 1]) / min(i + 1, MA)
                exit_sig = False
                if close <= ma:                      # 趋势破：跌破MA
                    exit_sig = True
                elif close <= pos['entry_close'] * (1 - STOP):  # trailing stop
                    exit_sig = True
                elif held_days >= HMAX:              # 到最大持仓天数
                    exit_sig = True
                if exit_sig:
                    ret = (close / pos['entry_close'] - 1) * 100 - 2 * FEE * 100
                    trades.append(_mk(pos, close, day, ret))
                    pos = None
        # 2) 空仓则找最强趋势标的入场
        if pos is None:
            best = None
            for code, (ds, cl) in series.items():
                i = idx_of[code].get(day)
                if i is None or i < L or i < MA:
                    continue
                close = cl[i]
                mom = close / cl[i - L] - 1
                if mom <= 0:
                    continue
                ma = sum(cl[max(0, i - MA + 1):i + 1]) / min(i + 1, MA)
                if close <= ma:
                    continue
                score = mom
                if best is None or score > best[0]:
                    best = (score, code, close, ci)
            if best is not None:
                _, code, close, ci_entry = best
                pos = {'code': code, 'entry_close': close, 'entry_cal': ci_entry,
                       'last_close': close, 'entry_day': day}
    return trades


def _mk(pos, exit_close, exit_day, ret=None):
    if ret is None:
        ret = (exit_close / pos['entry_close'] - 1) * 100 - 2 * FEE * 100
    return {'signal_date': pos['entry_day'], 'sell_date': exit_day,
            'return_pct': ret, 'code': pos['code']}


def stats_of(trades):
    if not trades:
        return {'trades': 0, 'equity_pct': 0.0, 'win_rate': 0.0,
                'mdd_pct': 0.0, 'yearly': {}}
    eq = cur = 1.0
    peak = 1.0
    mdd = 0.0
    by = defaultdict(list)
    for t in sorted(trades, key=lambda x: x['sell_date']):
        r = t['return_pct']
        eq *= 1 + r / 100
        cur *= 1 + r / 100
        peak = max(peak, cur)
        mdd = min(mdd, (cur - peak) / peak * 100)
        by[t['sell_date'][:4]].append(r)
    win = sum(1 for t in trades if t['return_pct'] > 0) / len(trades) * 100
    yearly = {}
    for y in sorted(by):
        e = 1.0
        for x in by[y]:
            e *= 1 + x / 100
        yearly[y] = round((e - 1) * 100, 2)
    return {'trades': len(trades), 'equity_pct': round((eq - 1) * 100, 2),
            'win_rate': round(win, 1), 'mdd_pct': round(mdd, 1), 'yearly': yearly}


def main():
    etf_daily, etf_5min, ALL_DATES, etf_list = load()
    series = build_series(etf_daily, etf_list)
    print(f'候选池 {len(etf_list)} 只 | 有完整日线 {len(series)} 只 | '
          f'窗口 {START}~{END}')

    # 现状 B（1日持仓）作为基准
    test_dates = [d for d in ALL_DATES if START <= d <= END]
    picks_b = build_picks_B(test_dates, etf_list, etf_daily, etf_5min, 0)
    from backtest_unified_local import apply_confirm  # noqa
    picks_cf, _ = apply_confirm(picks_b, etf_daily, etf_5min, '14:40')
    b_res = run_strategy('trix', test_dates, ALL_DATES, picks_cf, etf_5min, FEE)
    b_trades = b_res['trades'] if b_res else []
    sb = stats_of(b_trades)

    variants = {
        'X TSMOM L20 MA20 H20 stop5%': run_tsmom(series, 20, 20, 20, 0.05, 'X'),
        'X TSMOM L20 MA20 H10 stop4%': run_tsmom(series, 20, 20, 10, 0.04, 'X'),
        'Y TSMOM L60 MA60 H60 stop8%': run_tsmom(series, 60, 60, 60, 0.08, 'Y'),
    }

    print('\n===== 进攻腿范式对比 (攻击-only, 无防守) =====')
    print(f"\n[基准 B 单日尖峰 1日持仓] 总 {sb['equity_pct']}%  "
          f"笔 {sb['trades']} 胜 {sb['win_rate']}% MDD {sb['mdd_pct']}%")
    for y, r in sorted(sb['yearly'].items()):
        print(f"   {y}: {r:+.2f}%")
    for name, tr in variants.items():
        s = stats_of(tr)
        rs = [t['return_pct'] for t in tr]
        import statistics as _st
        print(f"\n[{name}] 总 {s['equity_pct']}%  "
              f"笔 {s['trades']} 胜 {s['win_rate']}% MDD {s['mdd_pct']}%")
        if rs:
            print(f"   收益分布: 均 {_st.mean(rs):+.2f}% 中 {_st.median(rs):+.2f}% "
                  f"最小 {min(rs):+.2f}% 最大 {max(rs):+.2f}%")
            for t in tr[:8]:
                print(f"   e.g. {t['signal_date']}->{t['sell_date']} {t['code']} "
                      f"{t['return_pct']:+.2f}%")
        for y, r in sorted(s['yearly'].items()):
            print(f"   {y}: {r:+.2f}%")

    out = {'window': [START, END],
           'B': sb,
           'tsmom': {n: stats_of(tr) for n, tr in variants.items()}}
    json.dump(out, open(CACHE / 'exp_tsmom.json', 'w'),
              ensure_ascii=False, indent=2)
    print('\n落盘:', CACHE / 'exp_tsmom.json')


if __name__ == '__main__':
    main()
