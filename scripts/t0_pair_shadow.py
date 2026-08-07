#!/usr/bin/env python3
"""配对收敛 SHADOW 薄补充腿 —— 核心 B 腿熄火时的「非趋势期」点缀。

背景 / 动机(用户探索「非趋势期进攻其他领域」):
  hybrid-A 实盘 / B+确认+TRIX 在趋势期吃肉, 但非趋势期(震荡/中性)动量常熄火,
  idle 动量腿(反转/隔夜动量)经无偏数据重验为负期望已停用。条件化配对攻击
  (相对价值双胞胎收敛) 在「动量熄火 + 非趋势」日验证为正期望薄边缘:
    全4年干净数据(已修 pre2024 ×5 脏数据) NASDAQ 家族 +0.73%/笔, 胜率56%,
    GOLD 的 +4704% 属趋势期时代效应(与动量腿重叠), 不计入独立贡献。
  → 作为核心腿熄火日的低风险点缀, 填补 idle 资金窗口, 小仓位验证真实成交价。

与 t0_b_idle_shadow.py 平行, 但**完全独立**:
  - 不读写 B 核心 shadow state / 实盘 state。
  - 独立 STATE_FILE = ~/.tradingagents/rotation/pair_shadow_state.json
  - 独立 JOURNAL   = ~/.tradingagents/rotation/pair_shadow_journal.jsonl
  - shadow 模式: 绝不真下单, 只记录信号 + 推送(可选真下单由用户另行开启)。

逻辑对齐 backtest_rv_attack_conditional.py 的 D 双gate 方案:
  触发 = 动量熄火(全市场无 ETF 日内涨 ≥3%) 且 regime≠趋势(非趋势)
  信号 = 干净子宇宙(GOLD/NASDAQ/HSCEI, 剔除不回归的 HSTECH/HKINTERNET) 双胞胎
         在 14:55 的 log 比值 z-score 偏离 ≥ ENTRY_K(2.5) 时入场, 隔夜持有至
         比值回归(EXIT_K=0.3) 或 ≤ MAX_DAYS(3) 平。

crontab(与 B 核心 shadow 平行, 独立窗口):
  # 15:05 跑(确保当日1分K完整, 用 14:55 收盘价判定)
  05 15 * * 1-5  cd /path && python3 scripts/t0_pair_shadow.py --signal
  # 次日 14:55 后判定平仓(回归或超时)
  55 14 * * 1-5  cd /path && python3 scripts/t0_pair_shadow.py --sell-check
"""
from __future__ import annotations
import argparse, json, math, os, statistics, sys
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import t0_monitor as TM  # noqa: E402  复用行情/推送函数 (仅函数, 不读写其实盘状态)
from t0_regime import detect_regime  # noqa: E402
from t0_monitor import (  # noqa: E402
    fetch_tencent_quotes, rank_t0_by_today_gain, fetch_1min_today,
    price_1min_at_or_before, send_dingtalk, get_all_t0_etfs, MIN_GAIN,
)

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"
STATE_DIR = Path.home() / ".tradingagents" / "rotation"
STATE_FILE = STATE_DIR / "pair_shadow_state.json"
JOURNAL = STATE_DIR / "pair_shadow_journal.jsonl"
FILES = [CACHE / "tdx_5min_pre2024.json", CACHE / "tdx_5min_2y.json"]

# ── 参数(对齐 backtest_rv_attack_conditional.py D 方案) ──
ST = "14:55"
MIN_GAIN_MOM = 0.03      # 动量熄火阈值(日内涨幅 <3% = 无动量行情)
FEE = 0.0003
L = 30; MINL = 15; ENTRY_K = 2.5; EXIT_K = 0.3; MIN_ABS = 0.005; MAX_DAYS = 3
PAIR_CAPITAL_PCT = 0.15  # 薄补充腿小仓位
REGIME_PROXY = "501018"

# 干净子宇宙(剔除不回归的 HSTECH/HKINTERNET/SP500/NIKKEI/OIL)
FAMILIES = {
    "GOLD":    ["518880", "159934", "518600", "518660", "518800", "517520", "159812"],
    "NASDAQ":  ["513100", "159941", "513300", "513400", "513850"],
    "HSCEI":   ["510900", "513600", "513630", "513730", "513900", "513750"],
}
CODE_FAM = {c: f for f, ms in FAMILIES.items() for c in ms}
ALL_PAIR_CODES = list(CODE_FAM.keys())


# ── 行情 / 历史 ──
def price_at(bars, target):
    best = None
    for b in bars:
        if b["time"] <= target:
            best = b["close"]
    return best


def code_to_sina(code: str) -> str:
    return ("sh" + code) if code.startswith(("5", "6")) else ("sz" + code)


def fetch_1455(code: str):
    """当日 14:55 收盘价 (sina 1分K)。"""
    bars = fetch_1min_today(code_to_sina(code))
    px, _ = price_1min_at_or_before(bars, ST)
    return px


def load_hist_closes() -> dict:
    """历史每个配对标的每个交易日的 14:55 收盘。只加载配对相关标的(快)。"""
    closes: dict = {}
    for fp in FILES:
        if not fp.exists():
            continue
        data = json.loads(fp.read_text(encoding="utf-8"))
        sub = data.get("etf_5min", data)
        for code in ALL_PAIR_CODES:
            if code in sub:
                for d, bars in sub[code].items():
                    if bars:
                        closes.setdefault(code, {})[d] = price_at(bars, ST)
    return closes


def mom_fired(replay_date: str | None = None, hist: dict | None = None) -> bool:
    """当日全市场是否有任一 T0 ETF 日内涨幅 ≥ MIN_GAIN_MOM (动量未熄火)。
    replay_date 提供时用历史 14:55 收盘近似(配对子宇宙代理, 仅供自测)。"""
    if replay_date and hist:
        best = 0.0
        for code in ALL_PAIR_CODES:
            ds = sorted(hist.get(code, {}).keys())
            if replay_date in ds and ds.index(replay_date) > 0:
                prev = ds[ds.index(replay_date) - 1]
                if hist[code].get(prev, 0) > 0:
                    best = max(best, hist[code][replay_date] / hist[code][prev] - 1)
        return best >= MIN_GAIN_MOM
    quotes = fetch_tencent_quotes([e["code"] for e in get_all_t0_etfs()])
    ranked = rank_t0_by_today_gain(quotes, get_all_t0_etfs())
    best = max((r["today_gain"] for r in ranked), default=0.0)
    return best >= MIN_GAIN_MOM


def build_twin(hist: dict):
    """基于历史 14:55 日收益相关性构建双胞胎对(corr≥0.90)。"""
    daily_ret = {}
    for code in ALL_PAIR_CODES:
        ds = sorted(hist.get(code, {}).keys())
        daily_ret[code] = [(ds[i], hist[code][ds[i]] / hist[code][ds[i - 1]] - 1)
                           for i in range(1, len(ds)) if hist[code].get(ds[i - 1], 0) > 0]

    def corr(c1, c2):
        s1 = dict(daily_ret[c1]); s2 = dict(daily_ret[c2])
        common = [d for d in s1 if d in s2]
        if len(common) < 150:
            return None
        xs = [s1[d] for d in common]; ys = [s2[d] for d in common]
        n = len(xs); mx = sum(xs) / n; my = sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        vx = sum((x - mx) ** 2 for x in xs) ** 0.5
        vy = sum((y - my) ** 2 for y in ys) ** 0.5
        return cov / (vx * vy) if vx > 1e-9 and vy > 1e-9 else None

    pairs = []
    for fam, members in FAMILIES.items():
        avail = [m for m in members if m in hist]
        for i in range(len(avail)):
            for j in range(i + 1, len(avail)):
                c = corr(avail[i], avail[j])
                if c is not None and c >= 0.90:
                    pairs.append((avail[i], avail[j], round(c, 3), fam))
    return pairs


def regime_mode(hist: dict, today_close: float | None) -> str:
    """501018 代理 regime, 含当日。"""
    ds = sorted(hist.get(REGIME_PROXY, {}).keys())
    daily = []
    for d in ds:
        daily.append({"day": d, "close": hist[REGIME_PROXY][d]})
    if today_close:
        daily.append({"day": date.today().isoformat(), "close": today_close})
    if len(daily) < 30:
        return "中性"
    return detect_regime(daily, daily[-1]["day"])["mode"]


# ── 状态 ──
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"strategy": {"name": "配对收敛 SHADOW", "mode": "shadow",
                         "role": "核心B腿熄火时非趋势期点缀", "capital_pct": PAIR_CAPITAL_PCT},
            "position": None, "last_signal_date": None, "updated_at": None}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_journal(rec: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── 信号 ──
def run_signal(dry_run: bool = False, replay_date: str | None = None) -> int:
    today = replay_date or date.today().isoformat()
    state = load_state()
    if state.get("position") and not replay_date:
        print(f"[{today}] 已有未平配对仓位, 跳过信号判定")
        return 0

    hist = load_hist_closes()
    twin = build_twin(hist)
    print(f"[{today}] 双胞胎对: {len(twin)} -> {[f'{a}/{b}' for a,b,_,_ in twin]}")

    # gate 1: 动量熄火
    fired = mom_fired(replay_date, hist)
    # gate 2: regime 非趋势
    proxy_today = (hist.get(REGIME_PROXY, {}).get(today) if replay_date
                   else fetch_1455(REGIME_PROXY))
    mode = regime_mode(hist, proxy_today)

    if fired:
        print(f"[{today}] 动量未熄火(有 ETF 涨≥3%), 配对不触发")
        return 0
    if mode == "趋势":
        print(f"[{today}] regime=趋势, 配对不触发")
        return 0
    print(f"[{today}] 触发条件满足(动量熄火 + 非趋势), 判定配对信号...")

    # 当日 14:55 close
    if replay_date:
        today_closes = {c: hist[c][today] for c in ALL_PAIR_CODES
                        if today in hist.get(c, {})}
    else:
        today_closes = {}
        for code in ALL_PAIR_CODES:
            px = fetch_1455(code)
            if px:
                today_closes[code] = px
                hist.setdefault(code, {})[today] = px

    # 找最佳配对信号(z 绝对值最大)
    best_sig = None
    for (A, B, _, fam) in twin:
        if A not in today_closes or B not in today_closes:
            continue
        series = []
        for d in sorted(hist.get(A, {}).keys()):
            if d == today:
                ca = today_closes.get(A); cb = today_closes.get(B)
            else:
                ca = hist[A].get(d); cb = hist[B].get(d)
            if ca and cb and ca > 0 and cb > 0:
                series.append((d, math.log(ca / cb)))
        past = [s for _, s in series[:-1]]
        if len(past) < MINL:
            continue
        mu = statistics.mean(past); sd = statistics.pstdev(past)
        if sd <= 1e-9:
            continue
        d, s = series[-1]
        z = (s - mu) / sd
        if abs(s - mu) < MIN_ABS:
            continue
        if abs(z) < ENTRY_K:
            continue
        if best_sig is None or abs(z) > abs(best_sig["z"]):
            leg = "A" if z <= -ENTRY_K else "B"
            best_sig = {"A": A, "B": B, "fam": fam, "z": z, "s": s, "mu": mu,
                        "sd": sd, "leg": leg, "ca": today_closes[A], "cb": today_closes[B]}

    if not best_sig:
        print(f"[{today}] 无配对信号(z 未达标)")
        return 0

    pos = {
        "pair": f"{best_sig['A']}/{best_sig['B']}", "fam": best_sig["fam"],
        "leg": best_sig["leg"], "entry_date": today,
        "entry_ca": best_sig["ca"], "entry_cb": best_sig["cb"],
        "entry_z": best_sig["z"], "capital_pct": PAIR_CAPITAL_PCT,
    }
    print(f"[{today}] 配对信号: {pos['pair']} 买{best_sig['leg']} "
          f"z={best_sig['z']:.2f} cap={PAIR_CAPITAL_PCT*100:.0f}%")

    if dry_run or replay_date:
        print(">>> --dry-run/回放, 不落盘/不推送")
        return 1
    state["position"] = pos
    state["last_signal_date"] = today
    save_state(state)
    append_journal({"event": "signal", "date": today, **pos})
    _push("配对收敛 SHADOW 信号", _fmt_signal(pos))
    return 1


# ── 卖出判定 ──
def run_sell_check(dry_run: bool = False) -> int:
    state = load_state()
    pos = state.get("position")
    if not pos:
        print(f"[{date.today().isoformat()}] 无配对仓位")
        return 0

    today = date.today().isoformat()
    A, B = pos["pair"].split("/")
    hist = load_hist_closes()
    ca = fetch_1455(A); cb = fetch_1455(B)
    if not ca or not cb:
        print(f"[{today}] 当日行情缺失, 暂不判定平仓")
        return 0
    hist.setdefault(A, {})[today] = ca
    hist.setdefault(B, {})[today] = cb

    series = []
    for d in sorted(set(list(hist.get(A, {}).keys()) + [today])):
        ha = hist[A].get(d); hb = hist[B].get(d)
        if ha and hb and ha > 0 and hb > 0:
            series.append((d, math.log(ha / hb)))
    past = [s for _, s in series[:-1]]
    if len(past) < MINL:
        return 0
    mu = statistics.mean(past); sd = statistics.pstdev(past)
    if sd <= 1e-9:
        return 0
    _, s = series[-1]
    z = (s - mu) / sd
    held = (datetime.now().date() - datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()).days

    if pos["leg"] == "A":
        cond = z >= EXIT_K
    else:
        cond = z <= -EXIT_K
    if cond or held >= MAX_DAYS:
        ret = ( (ca if pos["leg"] == "A" else cb) /
                (pos["entry_ca"] if pos["leg"] == "A" else pos["entry_cb"]) - 1 ) - 2 * FEE
        print(f"[{today}] 配对平仓: {pos['pair']} leg={pos['leg']} 持有{held}天 "
              f"z={z:.2f} 模拟收益={ret*100:+.2f}%")
        rec = {"event": "exit", "date": today, "entry_date": pos["entry_date"],
               "pair": pos["pair"], "leg": pos["leg"], "held_days": held,
               "ret": ret, "exit_z": z}
        if dry_run:
            print(">>> --dry-run, 不落盘/不推送")
            return 1
        state["position"] = None
        save_state(state)
        append_journal(rec)
        _push("配对收敛 SHADOW 平仓", _fmt_exit(rec))
        return 1
    print(f"[{today}] 配对持有中: {pos['pair']} leg={pos['leg']} 持有{held}天 z={z:.2f} (未达平仓)")
    return 0


def _fmt_signal(pos: dict) -> str:
    return (f"配对收敛信号 (薄补充腿, 仓位 {pos['capital_pct']*100:.0f}%)\n"
            f"标的对: {pos['pair']} [{pos['fam']}]\n"
            f"方向: 买 {pos['leg']} (z={pos['entry_z']:.2f})\n"
            f"入场参考: A={pos['entry_ca']:.4f} / B={pos['entry_cb']:.4f}")


def _fmt_exit(rec: dict) -> str:
    return (f"配对收敛平仓\n标的对: {rec['pair']} [{rec['leg']}]\n"
            f"持有: {rec['held_days']} 天\n模拟收益: {rec['ret']*100:+.2f}%")


def _push(title: str, body: str) -> None:
    webhook = (os.getenv("DINGTALK_ROTATION_WEBHOOK") or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    if not webhook:
        print("\n>>> 钉钉未配置, 跳过推送")
        print(body)
        return
    try:
        send_dingtalk(webhook, f"## {title}\n{body}")
        print(">>> 已推送钉钉")
    except Exception as e:  # noqa: BLE001
        print(f"WARN: 推送失败 {e}")


def main() -> None:
    import os
    ap = argparse.ArgumentParser(description="配对收敛 SHADOW (薄补充腿, 不真下单)")
    ap.add_argument("--signal", action="store_true", help="15:05 判定配对信号")
    ap.add_argument("--sell-check", action="store_true", help="次日判定平仓")
    ap.add_argument("--sell-loop", action="store_true", help="卖出窗口循环(预留)")
    ap.add_argument("--replay-date", default=None, help="回放某日(YYYY-MM-DD)信号判定, 用历史收盘, 不落盘")
    ap.add_argument("--dry-run", action="store_true", help="仅打印不落盘/不推送")
    args = ap.parse_args()

    if args.signal or args.replay_date:
        run_signal(args.dry_run, args.replay_date)
    elif args.sell_check or args.sell_loop:
        run_sell_check(args.dry_run)
    else:
        print("用法: --signal [--replay-date YYYY-MM-DD] | --sell-check [--dry-run]")


if __name__ == "__main__":
    main()
