#!/usr/bin/env python3
"""修复 pre2024 段 5min 的量级脏数据(整段 ×factor 残留)。

根因: pytdx 早期数据对个别标的(513100 的 2022-01、159941 的 2022-07)存在 ×5 左右
的整段量级残留; full_daily 同源脏, 无法用其修。

修复法(内部、零外部依赖): 脏是整段 ×factor, 段内日收益形态真实。用同家族
【干净兄弟标的】作基准:
  - r0 = median(干净期 code_close / anchor_close)   # 正常相对比值
  - 脏天真实 close = r0 * anchor_close[脏天]          # anchor 在脏天干净
  - factor = 真实close / 观测close, 缩放该天全部 5min bar

要求 anchor 在脏天有数据且其自身无脏。NASDAQ 家族用 513300(全年887天干净)。
GOLD/HSCEI 经诊断无脏日, 自动跳过。

用法:
    python scripts/fix_pre2024_scale.py            # 修复并落盘(覆盖原文件)
    python scripts/fix_pre2024_scale.py --dry-run  # 只报告不落盘
"""
from __future__ import annotations
import argparse, json, statistics, sys
from pathlib import Path

C = Path.home() / ".tradingagents/cache/t0_5min"
PRE = C / "tdx_5min_pre2024.json"

# 与条件化配对脚本一致
FAMILIES = {
    "GOLD":    ["518880", "159934", "518600", "518660", "518800", "517520", "159812"],
    "NASDAQ":  ["513100", "159941", "513300", "513400", "513850"],
    "HSCEI":   ["510900", "513600", "513630", "513730", "513900", "513750"],
}
CODE_FAM = {c: f for f, ms in FAMILIES.items() for c in ms}

DIRTY_RET = 0.15  # |相邻日收益|>15% 视为量级切换点(ETF 单日正常波动远小于此)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = json.loads(PRE.read_text(encoding="utf-8"))
    five = data["etf_5min"]
    codes = set(five.keys())

    # 预先算每个 code 的日 close 序列 + 脏天
    info = {}
    for code in codes:
        days = sorted(five[code].keys())
        closes = {d: (five[code][d][-1]["close"] if five[code][d] else None) for d in days}
        ds = [d for d in days if closes[d] is not None]
        dirty = set()
        for i in range(1, len(ds)):
            p, c = closes[ds[i - 1]], closes[ds[i]]
            if p and c and abs(c / p - 1) > DIRTY_RET:
                dirty.add(ds[i])  # 标记切换点当天(高量级侧)
        info[code] = {"closes": closes, "ds": ds, "dirty": dirty}

    n_fixed_days = 0
    report = []
    for code, fam in CODE_FAM.items():
        if code not in codes:
            continue
        if not info[code]["dirty"]:
            continue  # 无切换点, 整段大概率干净
        # 找同家族干净兄弟作 anchor(自身相邻 ret 均<DIRTY_RET)
        anchor = None
        for sib in FAMILIES[fam]:
            if sib == code or sib not in codes:
                continue
            if not info[sib]["dirty"]:
                anchor = sib
                break
        if anchor is None:
            report.append(f"  {code}: 有切换点但同家族无可信 anchor, 跳过!")
            continue
        # r0 = 中位数(code/anchor 比值), 对离群(脏段)鲁棒
        ratios = [info[code]["closes"][d] / info[anchor]["closes"][d]
                  for d in info[code]["ds"]
                  if d in info[anchor]["closes"] and info[anchor]["closes"][d]
                  and info[code]["closes"][d]]
        if not ratios:
            continue
        r0 = statistics.median(ratios)
        # 逐日 expected 校验: 偏离>0.5% 即缩放(覆盖整段高量级侧, 不误伤低侧)
        cnt = 0
        for d in info[code]["ds"]:
            oc = info[code]["closes"][d]
            ac = info[anchor]["closes"].get(d)
            if not oc or not ac:
                continue
            expected = r0 * ac
            factor = expected / oc
            if abs(factor - 1) < 0.005:
                continue
            for b in five[code][d]:
                for k in ("open", "high", "low", "close"):
                    if k in b and b[k] is not None:
                        b[k] = round(b[k] * factor, 4)
            n_fixed_days += 1
            cnt += 1
            if args.dry_run and len(report) < 40:
                report.append(f"  {code} {d}: obs={oc:.4f} anchor({anchor})={ac:.4f} "
                              f"expected={expected:.4f} factor={factor:.4f}")
        report.append(f"  >> {code}: r0={r0:.4f}, 已缩放 {cnt} 天")

    print("=== 修复报告 ===")
    print("\n".join(report))
    print(f"\n总计缩放 {n_fixed_days} 个 (code,day)")

    if args.dry_run:
        print("[dry-run] 未落盘")
        return
    PRE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f">>> 已落盘 {PRE} ({PRE.stat().st_size/1e6:.0f}MB)")


if __name__ == "__main__":
    main()
