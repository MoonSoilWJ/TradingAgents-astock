# -*- coding: utf-8 -*-
"""科创50 N12 + 防御组轮动 V3 —— 实盘信号推送 (14:55 尾盘口径)。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么需要这个脚本 (替换 push_588000_signal.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  原 scripts/push_588000_signal.py 只推【纯 N12 基线】: 输出"持 588000 / 空仓"两个状态,
  完全没有防御组轮动 —— 没有 511260/518880/510880/515080/512890, 没有防御腿金叉判断,
  没有 20 日动量选最强。而本地主策略是防御组轮动。
  照旧推送下单 = 跑纯 N12(本地 289.7%), 而不是防御组轮动(本地 655.8%, 未复权)。

  本脚本**直接复用 backtest_588000_n12.build_defensive_rotation**, 保证
  【实盘信号 == 回测逻辑】, 杜绝实现漂移。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
口径 (与分钟级回测验证出的最优配置一致, 别改)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  · 时点 14:55; 信号用【当日盘中价】(TDX 日线最后一根 bar 的 close ≈ 14:55 价)
  · 等价聚宽 USE_TODAY_PRICE=True + ORDER_TIME='14:55'
  · 分钟级验证 (2024-01-02~2026-09-01):
        14:55+当日价  累计 299.59%  Sharpe 2.03  回撤 19.03%   ← 本脚本采用
        09:31+昨收    累计 265.41%  Sharpe 1.85  回撤 20.17%
        14:55+昨收    累计 188.46%  Sharpe 1.41  回撤 21.65%   (信号滞后一日, 已淘汰)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
推送策略
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  · 只在【目标标的切换】时推送 (一年约 32 次), 不做每日噪音推送
  · 切换时附带"上一笔持仓的收益", 便于手动跟车与复盘
  · state 文件记录当前持仓 / 入场价 / 入场日

用法:
  python3 scripts/push_kc50_defensive_signal.py              # 正常 (14:55 cron 调用)
  python3 scripts/push_kc50_defensive_signal.py --dry-run    # 只打印, 不推送不写状态
  python3 scripts/push_kc50_defensive_signal.py --force      # 强制推送 (即使未切换)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_PROJECT_ROOT))

import backtest_588000_n12 as B  # noqa: E402  (复用生产逻辑, 保证与回测一致)
from tradingagents.notify.dingtalk import send_markdown  # noqa: E402

STATE_PATH = Path.home() / ".tradingagents" / "rotation" / "kc50_defensive_state.json"
START = "2020-11-16"
MIN_BARS = 90                      # 指标预热, 与回测 WARMUP 一致


# ── 状态存取 ──────────────────────────────────────────────────────────────
def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 计算今日信号 (复用生产 build_defensive_rotation) ────────────────────────
def build_signal():
    """返回 (today, target, diag)。target 为今日应持有的唯一标的。"""
    f = B.fetch_day(START)
    df = pd.DataFrame({"588000": f.set_index("date")["close"].astype(float)})
    missing = []
    for c in B.DEF:
        s = B.fetch_day_code(c, START)
        if s is None:
            missing.append(c)
        else:
            df[c] = s
    if missing:
        raise RuntimeError(f"防御组行情拉取失败: {missing}")
    df = df.dropna()
    if len(df) < MIN_BARS:
        raise RuntimeError(f"历史不足: {len(df)} < {MIN_BARS} 根, 指标未预热")

    core, kc_cross, def_cross, hold_spec, frac = B.build_defensive_rotation(df)

    w = hold_spec[-1]
    target = next(iter(w)) if w else B.BOND
    today = df.index[-1]

    diag = {
        "votes": int(round(frac[-1] * len(B.COMB_N12))),
        "n_combos": len(B.COMB_N12),
        "core_on": bool(core[-1]),
        "kc_golden": bool(kc_cross[-1]),
        "close": {c: float(df[c].iloc[-1]) for c in ["588000"] + list(B.DEF)},
        "def_state": {},
    }
    for c in B.DEF:
        mom = B._momentum(df[c].values.astype(float), 20)
        diag["def_state"][c] = {
            "golden": bool(def_cross[c][-1]),
            "mom20": None if np.isnan(mom[-1]) else float(mom[-1]),
        }
    return today, target, diag


def reason_of(diag: dict, target: str) -> str:
    if diag["core_on"]:
        return f"N12 簇 {diag['votes']}/{diag['n_combos']} 看多 → 持科创50"
    if diag["kc_golden"]:
        return f"科创50金叉但簇未喊多 ({diag['votes']}/{diag['n_combos']}) → 保守持国债"
    g = [c for c in B.DEF if diag["def_state"][c]["golden"]]
    if not g:
        return "科创50死叉 · 防御组全死叉 → 兜底国债"
    best = max(g, key=lambda c: (diag["def_state"][c]["mom20"]
                                 if diag["def_state"][c]["mom20"] is not None else -1e9))
    return f"科创50死叉 → 防御组动量最强 [{B.NAMES.get(best, best)}]"


def pool_snapshot(diag: dict, target: str) -> str:
    lines = [f"- **{B.NAMES.get('588000','科创50ETF')}(588000)**: "
             f"簇 {diag['votes']}/{diag['n_combos']}"
             f"{'　← 选中' if target == '588000' else ''}"]
    for c in B.DEF:
        st = diag["def_state"][c]
        flag = "🟢金叉" if st["golden"] else "⚪死叉"
        mom = "　　—　" if st["mom20"] is None else f"{st['mom20'] * 100:+.2f}%"
        mark = "　← 选中" if c == target else ""
        lines.append(f"- {B.NAMES.get(c, c)}({c}): {flag}　20日动量 {mom}{mark}")
    return "\n".join(lines)


# ── 主流程 ────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印, 不推送不写状态")
    ap.add_argument("--force", action="store_true", help="即使未切换也推送")
    ap.add_argument("--state", default=str(STATE_PATH), help="状态文件路径")
    args = ap.parse_args()

    state_path = Path(args.state)
    state = load_state(state_path)

    today, target, diag = build_signal()
    today_s = today.strftime("%Y-%m-%d")
    price_t = diag["close"][target]
    prev = state.get("target")
    changed = (prev is None) or (prev != target)

    # 数据新鲜度检查: 14:55 口径依赖当日盘中价, 拿不到必须明确告警
    stale = ""
    now_s = datetime.now().strftime("%Y-%m-%d")
    if today_s != now_s:
        stale = (f"\n\n> ⚠️ **数据日期 {today_s} ≠ 今日 {now_s}**, 当日行情未更新。\n"
                 f"> 本次信号退化为「用 {today_s} 收盘」口径, 与验证过的 14:55 当日价口径不同, 请谨慎。")

    # 无切换 → 静默 (除非 --force)
    if not changed and not args.force:
        print(f"[无切换] {today_s} 继续持有 {B.NAMES.get(target, target)}({target}) — 不推送")
        state["last_run"] = today_s
        if not args.dry_run:
            save_state(state_path, state)
        return 0

    # 上一笔收益
    pnl_line = ""
    if prev is not None and prev in diag["close"] and state.get("entry_price"):
        px_in = float(state["entry_price"]) * (1 + B.SLIP)
        px_out = diag["close"][prev] * (1 - B.SLIP)
        ret = (px_out / px_in - 1) * 100
        pnl_line = (f"**上一笔**: {state.get('entry_date', '?')} 买入 "
                    f"{B.NAMES.get(prev, prev)}({prev}) @ {float(state['entry_price']):.4f} "
                    f"→ 今日卖出 @ {diag['close'][prev]:.4f}　收益 **{ret:+.2f}%**\n")

    if prev is None:
        op = f"**首次建仓**: 买入 {B.NAMES.get(target, target)}({target})"
    elif prev == target:
        op = f"**维持持有**: {B.NAMES.get(target, target)}({target})（未切换）"
    else:
        op = (f"**卖出** {B.NAMES.get(prev, prev)}({prev}) → "
              f"**买入** {B.NAMES.get(target, target)}({target})")

    title = f"科创50防御轮动 {'换仓' if changed else '持仓'} {today_s} @14:55"
    text = "\n".join([
        f"### 科创50 N12 + 防御组轮动 · 信号",
        "",
        f"**日期**: {today_s} 14:55",
        f"**操作**: {op}",
        f"**参考价**: {price_t:.4f}（14:55 盘中价, 收盘集合竞价下单）",
        "",
        pnl_line,
        f"**原因**: {reason_of(diag, target)}",
        "",
        "**标的池快照**:",
        pool_snapshot(diag, target),
        "",
        "> 每次只持 1 只, 100% 仓位。未收到换仓推送即维持原持仓。",
        stale,
    ])

    print("=" * 70)
    print(title)
    print("-" * 70)
    print(text)
    print("=" * 70)

    if args.dry_run:
        print("\n[dry-run] 不推送, 不写状态")
        return 0

    webhook = (os.getenv("DINGTALK_ROTATION_WEBHOOK") or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    keyword = (os.getenv("DINGTALK_ROTATION_KEYWORD") or os.getenv("DINGTALK_KEYWORD") or "轮动").strip()
    if not webhook:
        print("! 钉钉未配置 (DINGTALK_ROTATION_WEBHOOK / DINGTALK_WEBHOOK), 跳过推送")
    else:
        ok = send_markdown(title, text, webhook=webhook, keyword=keyword)
        print(f"推送钉钉: {'成功' if ok else '失败'}")

    state.update({"target": target, "entry_price": price_t,
                  "entry_date": today_s, "last_run": today_s})
    save_state(state_path, state)
    print(f"状态已更新: {state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
