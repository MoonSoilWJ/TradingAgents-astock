"""
本地「通信达」回测：大一统策略 · 进攻腿资金曲线自适应降险(equity-curve stop)
=================================================================================
目标：验证「弱市段避免负收益」的真实解法 —— 不是 regime 切换（滞后、主升浪空仓），
      而是基于进攻腿【自身近期盈亏】自动降权：

  维护进攻腿独立净值；当其从峰值回撤超过阈值(如-20%)→ 暂停新开仓，资金全留防守腿；
  回正后立刻重开。强市进攻赚钱→净值创新高→dd≈0→永不误关；弱市/崩塌月进攻连亏→
  自动收手让防守腿满仓兜底。零前视、不牺牲强市。

子逻辑来源（均已独立验证）：
  进攻腿  = B 全市场 Top1≥3% + 14:40 双时点确认 + 次日 TRIX(5,3)死叉卖/11:05收盘fallback/-5%硬止损
           （回测口径：全4年 +613.46%/429笔/MDD-33.2%，近390OOS +319.16%，稳健广谱）
           本脚本直接复用 backtest_recent100_live_vs_b_idle.py 的同一管道，保证口径一致。
  防守腿  = 等权(黄金518880 + 国债类) 月度再平衡，无动量过滤（本地代理，方向同用户剔除可转债后的4只）

数据（无偏）：
  5min  = tdx_5min_pre2024.json + tdx_5min_2y.json 合并
  日线  = aligned_live_4y.json 的 etf_daily / all_dates / proxy_klines
  防守资产日线 = full_daily_2015_2026.json

⚠ 本地 B 选股数据窗口起点为 2022-11-15（lb=30 warmup），无法复现 2022 上半年。
  故本脚本验证的是「2022-11~2026-07 区间内弱市/崩塌月（如 2025-02）的改善」，
  2022 全年 -7.7% 的验证请以用户聚宽(A选股)为准。

用法：
  python3 scripts/backtest_unified_local.py
"""
import json, sys
from pathlib import Path
from collections import defaultdict

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from backtest_t0_hybrid_sell import run_strategy, SIGNAL_TIME  # noqa: E402
from backtest_t0_today1 import FEE_PCT, MIN_GAIN, gain_at_time  # noqa: E402
from backtest_b_idle_merge import build_picks_B  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402


def apply_confirm(picks, etf_daily, etf_5min, confirm_time, min_gain=MIN_GAIN):
    """双时点确认: confirm_time 时刻涨幅也须 ≥ min_gain (对齐实盘 t0_monitor CONFIRM_TIME)。"""
    out, rejected = {}, 0
    for key, val in picks.items():
        if not val:
            out[key] = val
            continue
        code = val[0]
        g = gain_at_time(etf_daily, etf_5min, code, key[1], confirm_time)
        if g is not None and g < min_gain:
            out[key] = None
            rejected += 1
        else:
            out[key] = val
    return out, rejected

CACHE = Path.home() / '.tradingagents/cache/t0_5min'
PRE2024_FILE = CACHE / 'tdx_5min_pre2024.json'
TWOY_FILE    = CACHE / 'tdx_5min_2y.json'
ALIGNED      = CACHE / 'aligned_live_4y.json'
FULL_DAILY  = CACHE / 'full_daily_2015_2026.json'

# 本地缓存: 黄金 + 纯国债(低相关防守代理)，方向同用户剔除可转债后的「弱市抗跌」组合
DEFENSE = ['518880', '511260', '511010', '511030', '511060']
START = '2022-11-15'
END   = '2026-07-31'
FEE   = FEE_PCT
CONFIRM = '14:40'
LB    = 30

# ----------------------------------------------------------------------------
# 1) 载入：无偏 5min（pre2024 + 2y）+ 日线/代理
# ----------------------------------------------------------------------------
cache = json.loads(ALIGNED.read_text(encoding='utf-8'))
etf_daily = cache['etf_daily']
proxy = cache['proxy_klines']

etf_5min = {}
for part in (PRE2024_FILE, TWOY_FILE):
    dense = json.loads(Path(part).read_text(encoding='utf-8'))['etf_5min']
    for c, days in dense.items():
        etf_5min.setdefault(c, {}).update(days)
ALL_DATES = sorted({d for days in etf_5min.values() for d in days})
CODES5 = set(etf_5min.keys())
etf_list = [e for e in get_all_t0_etfs() if e['code'] in CODES5]
print(f'候选池 {len(etf_list)} 只 | 5min 交易日 {ALL_DATES[0]}~{ALL_DATES[-1]} '
      f'({len(ALL_DATES)}天) | 费率 {FEE*100:.3f}%')

# ----------------------------------------------------------------------------
# 2) 进攻腿 B + 确认 + TRIX（与 backtest_recent100 同一管道）
# ----------------------------------------------------------------------------
def run_attack_B():
    test_dates = [d for d in ALL_DATES if START <= d <= END]
    picks_b = build_picks_B(test_dates, etf_list, etf_daily, etf_5min, 0)
    picks_b_cf, n_rej = apply_confirm(picks_b, etf_daily, etf_5min, CONFIRM)
    print(f'  B 选股双时点确认({CONFIRM}≥{MIN_GAIN:.0f}%): 否决 {n_rej} 天')
    res = run_strategy('trix', test_dates, ALL_DATES, picks_b_cf, etf_5min, FEE)
    trades = res['trades'] if res else []
    return trades

# ----------------------------------------------------------------------------
# 3) 防守腿：等权资产，月度再平衡（买权归一 + 分红再投资近似）
# ----------------------------------------------------------------------------
def run_defense():
    d = json.load(open(FULL_DAILY))
    closes = {}
    for code in DEFENSE:
        recs = d[code]['returns']
        closes[code] = {r['date']: r['close'] for r in recs}
    n = len(DEFENSE)
    w = {c: 1.0 / n for c in DEFENSE}
    last_month = None
    daily_ret = {}
    idx_map = {d_: i for i, d_ in enumerate(ALL_DATES)}
    for day in ALL_DATES:
        if day < START or day > END:
            continue
        md = day[:7]
        r = {}
        ok = True
        for c in DEFENSE:
            s = closes.get(c)
            if s is None or day not in s:
                ok = False
                break
            i = idx_map[day] - 1
            while i >= 0:
                if ALL_DATES[i] in s:
                    r[c] = s[day] / s[ALL_DATES[i]] - 1
                    break
                i -= 1
            else:
                ok = False
                break
        if not ok:
            daily_ret[day] = 0.0
            continue
        if last_month is None or md != last_month:
            w = {c: 1.0 / n for c in DEFENSE}
            last_month = md
        dr = sum(w[c] * r[c] for c in DEFENSE)
        # sanity clip: 单日 >10% 视为数据异常(拆分/分红未复权)跳过
        if abs(dr) > 0.10:
            dr = 0.0
        daily_ret[day] = dr
        for c in DEFENSE:
            w[c] = w[c] * (1 + r[c])
        tot = sum(w.values())
        w = {c: w[c] / tot for c in DEFENSE}
    return daily_ret

# ----------------------------------------------------------------------------
# 4) 组合模拟
#    stop_kind=None        → 进攻常开(Overlay, 等同用户C方案)
#    stop_kind='drawdown'  → 进攻独立净值回撤 < stop_param(负) 则暂停新开仓
#    stop_kind='rolling'   → 进攻近 stop_lookback 天累计收益 < stop_param(负) 则暂停
#    attack_split          → 信号日进攻占用资金比例；其余(1-split)永久留防守底仓。
#                             attack_split=1.0 即原 Overlay（进攻日防守=0）；
#                             attack_split<1.0 即「防守常驻底仓 + 进攻叠加」，
#                             旨在用防守正收益 cushion 进攻的弱市负月。
# ----------------------------------------------------------------------------
def simulate(atk, defense_ret, stop_kind=None, stop_param=None, stop_lookback=60,
              no_defense=False, attack_split=1.0,
              dyn_kind=None, dyn_base=0.4, dyn_lookback=20, dyn_thr=0.0):
    trade_by_sig = {t['signal_date']: t for t in atk}
    sell_map = defaultdict(list)
    for t in atk:
        sell_map[t['sell_date']].append(t['return_pct'])

    idx_map = {d_: i for i, d_ in enumerate(ALL_DATES)}
    held = None
    attack_w = 0.0
    eq = 1.0
    eq_attack = 1.0          # 进攻腿独立净值（仅 sell_date 跳变）
    peak_attack = 1.0
    curve = []
    n_stop_blocked = 0
    n_dyn_cushioned = 0
    for day in ALL_DATES:
        if day < START or day > END:
            continue
        day_ret = 0.0
        # 1) 持仓到卖出日 -> 了结进攻收益
        if held is not None and held['sell_date'] == day:
            r = held['return_pct'] / 100.0
            day_ret += attack_w * r
            eq_attack *= (1 + r)
            peak_attack = max(peak_attack, eq_attack)
            held = None
            attack_w = 0.0
        # 2) 是否允许新开仓（gate）
        take = True
        if stop_kind == 'off':
            take = False
        elif stop_kind == 'drawdown':
            dd = eq_attack / peak_attack - 1
            if dd < stop_param:
                take = False
        elif stop_kind == 'rolling':
            idx = idx_map[day]
            lo = max(0, idx - stop_lookback)
            wp = 0.0
            for d_ in ALL_DATES[lo:idx + 1]:
                for r_ in sell_map.get(d_, []):
                    wp += r_
            if wp < stop_param:
                take = False
        elif stop_kind == 'recent_neg':
            idx = idx_map[day]
            lo = max(0, idx - stop_lookback)
            wp = 0.0
            for d_ in ALL_DATES[lo:idx + 1]:
                for r_ in sell_map.get(d_, []):
                    wp += r_
            if wp < stop_param:
                take = False
        # 3) 今日新信号 -> 建仓(收盘)
        t = trade_by_sig.get(day)
        if t is not None:
            if take:
                eff = attack_split
                if dyn_kind == 'recent':
                    idx = idx_map[day]
                    lo = max(0, idx - dyn_lookback)
                    wp = 0.0
                    for d_ in ALL_DATES[lo:idx + 1]:
                        for r_ in sell_map.get(d_, []):
                            wp += r_
                    if wp < dyn_thr:
                        eff = dyn_base
                        n_dyn_cushioned += 1
                attack_w = eff
                held = t
            else:
                n_stop_blocked += 1
        # 4) 防守权重（闲置资金 + 信号日底仓）
        def_w = 0.0 if no_defense else (1.0 - attack_w)
        day_ret += def_w * defense_ret.get(day, 0.0)
        eq *= (1 + day_ret)
        curve.append((day, round(eq, 4)))
    return curve, n_stop_blocked, n_dyn_cushioned


def monthly(curve):
    """返回 [(YYYY-MM, 当月收益), ...] 月度收益序列（对齐用户看的聚宽表）。"""
    d = {}
    for day, v in curve:
        d[day[:7]] = v
    months = sorted(d)
    out = []
    for i, m in enumerate(months):
        if i == 0:
            out.append((m, d[m] - 1))
        else:
            out.append((m, d[m] / d[months[i - 1]] - 1))
    return out


def stats(curve, label):
    eq = [v for _, v in curve]
    total = eq[-1] / eq[0] - 1
    peak = eq[0]; mdd = 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    year_end = {}
    for d, v in curve:
        year_end[d[:4]] = v
    ys = sorted(year_end)
    yearly = []
    for i, y in enumerate(ys):
        if i == 0:
            yearly.append((y, year_end[y] - 1))
        else:
            yearly.append((y, year_end[y] / year_end[ys[i - 1]] - 1))
    years = len(eq) / 252.0
    cagr = eq[-1] ** (1 / years) - 1 if years > 0 else 0
    return {'label': label, 'total_pct': round(total * 100, 2),
            'cagr_pct': round(cagr * 100, 2), 'mdd_pct': round(mdd * 100, 2),
            'yearly': [(y, round(r * 100, 2)) for y, r in yearly]}


def main():
    atk = run_attack_B()
    print(f'  进攻腿 B+确认+TRIX 成交 {len(atk)} 笔')
    defense_ret = run_defense()

    def neg_months(mon):
        return [(m, round(r * 100, 2)) for m, r in mon if r < 0]

    variants = {
        # 纯进攻(无防守)
        '①纯进攻(B+TRIX,无防守)':
            simulate(atk, defense_ret, stop_kind=None, no_defense=True),
        # Overlay(进攻100% + 闲置防守) — 等同用户C方案
        '②Overlay(攻100% / 闲置防守)':
            simulate(atk, defense_ret, stop_kind=None, attack_split=1.0),
        # 原 stop 方案（验证它是否真的触发）
        '③Overlay+滚动60天<-15%暂停(攻100%)':
            simulate(atk, defense_ret, stop_kind='rolling',
                      stop_param=-15.0, stop_lookback=60, attack_split=1.0),
        # —— 防守常驻底仓 新方案 ——
        '④Overlay+防守常驻30%(攻70%)':
            simulate(atk, defense_ret, stop_kind=None, attack_split=0.7),
        '⑤Overlay+防守常驻40%(攻60%)':
            simulate(atk, defense_ret, stop_kind=None, attack_split=0.6),
        '⑥Overlay+防守常驻50%(攻50%)':
            simulate(atk, defense_ret, stop_kind=None, attack_split=0.5),
        # —— 动态配比：进攻近20日累计亏 → 降为底仓(防40%) ——
        '⑧Overlay+动态:攻近20日亏→降40%底仓':
            simulate(atk, defense_ret, stop_kind=None, attack_split=1.0,
                      dyn_kind='recent', dyn_base=0.4, dyn_lookback=20, dyn_thr=0.0),
        # —— 动态配比：进攻近20日累计亏 → 全防守 ——
        '⑨Overlay+动态:攻近20日亏→全防守':
            simulate(atk, defense_ret, stop_kind=None, attack_split=1.0,
                      dyn_kind='recent', dyn_base=0.0, dyn_lookback=20, dyn_thr=0.0),
        # —— 近20日累计<0 → 暂停(更激进的 stop) ——
        '⑩Overlay+近20日<0暂停(攻100%)':
            simulate(atk, defense_ret, stop_kind='recent_neg',
                      stop_param=0.0, stop_lookback=20, attack_split=1.0),
    }

    print('\n===== 结果 (窗口 %s~%s, B选股) =====' % (START, END))
    results = {}
    for name, out in variants.items():
        curve, n_blocked, n_dyn = out
        s = stats(curve, name)
        results[name] = s
        nm = neg_months(monthly(curve))
        extra = f" | 被stop挡{n_blocked}天" if n_blocked else ""
        if n_dyn:
            extra += f" | 动态降仓{n_dyn}天"
        print(f"\n[{name}] 总 {s['total_pct']}%  CAGR {s['cagr_pct']}%  "
              f"MDD {s['mdd_pct']}%{extra}")
        for y, r in s['yearly']:
            print(f"   {y}: {r:+.2f}%")
        if nm:
            print(f"   ⚠负月({len(nm)}): " + ", ".join(
                f"{m}={r:+.2f}%" for m, r in nm))
        else:
            print("   ✓ 无负月")
    sd = stats(simulate(atk, defense_ret, stop_kind='off')[0], '纯防守(等权代理)')  # noqa
    results['纯防守(等权代理)'] = sd
    print(f"\n[{sd['label']}] 总 {sd['total_pct']}%  CAGR {sd['cagr_pct']}%  "
          f"MDD {sd['mdd_pct']}%")

    out = {'window': [START, END], 'results': results}
    json.dump(out, open(CACHE / 'unified_backtest.json', 'w'),
              ensure_ascii=False, indent=2)
    print('\n落盘:', CACHE / 'unified_backtest.json')

if __name__ == '__main__':
    main()
