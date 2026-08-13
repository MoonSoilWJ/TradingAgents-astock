'''导出 R1~R6 月度轮动池, 供 joinquant_unified_strategy.py 使用。

★未来函数修正: 字典键 = 使用月(当月交易), 值 = 上月末 pool_as_of(无未来函数)。
  即 JSON["2024-07"] = pool_as_of("2024-06")(基于截至 6 月末的历史数据)。
  聚宽侧每月取 context.current_dt 的当月键即可, 严格只用上月及之前数据。

产出两份:
  1) scripts/jq_pools/jq_attack_R{1..6}.json  (本地核对用)
  2) scripts/jq_attack_pools.py               (内含 JQ_ATTACK_POOLS 字面量字典, 上传聚宽免 JSON)
对齐 backtest_unified_2022_2026.py --sweep-pool 的 universe = etf_daily.keys()。
'''
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(HERE))
import dynamic_pool as DP  # noqa: E402

CACHE = Path.home() / ".tradingagents/cache/t0_5min"
ALIGNED = CACHE / "aligned_live_4y.json"
FULL = CACHE / "full_daily_2015_2026.json"
OUTJSON = HERE / "jq_pools"
OUTJSON.mkdir(exist_ok=True)
OUTPY = HERE / "jq_attack_pools.py"

ed = json.load(open(ALIGNED, encoding="utf-8"))["etf_daily"]
full = json.load(open(FULL, encoding="utf-8"))
for c in full:
    ed.setdefault(c, full[c])
universe = set(ed.keys())
print(f"[export] universe={len(universe)}只 (对齐 sweep etf_daily)", flush=True)


def to_jq(code: str) -> str:
    base = code.split(".")[0]
    return f"{base}.XSHG" if base[0] in "56" else f"{base}.XSHE"


def prev_month(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    m -= 1
    if m == 0:
        m = 12
        y -= 1
    return f"{y}-{m:02d}"


# 起始提前到 2013-01, 让回测首月(2014-01)也有上月(2013-12)数据可用
RULES = {
    "R1": dict(drop_sector=False, min_avg_turnover=10_000_000, use_seed=True, min_listing_days=120),
    "R2": dict(drop_sector=False, min_avg_turnover=30_000_000, use_seed=True, min_listing_days=120),
    "R3": dict(drop_sector=True,  min_avg_turnover=10_000_000, use_seed=True, min_listing_days=120, exclude_hk_wide=True),
    "R4": dict(drop_sector=True,  min_avg_turnover=30_000_000, use_seed=True, min_listing_days=120),
    "R5": dict(drop_sector=False, min_avg_turnover=100_000_000, use_seed=True, min_listing_days=120),
    "R6": dict(drop_sector=True,  min_avg_turnover=100_000_000, use_seed=True, min_listing_days=120),
}

USE_START, USE_END = "2014-01", "2026-12"  # 回测使用月范围
RAW_START = prev_month(USE_START)           # 原始 pool_as_of 起点(上月)


def main():
    shifted_all = {}
    for name, cfg in RULES.items():
        mp = DP.month_pools_for_range(RAW_START, USE_END, universe=universe, **cfg)
        # 平移: 使用月 -> 上月末池
        shifted = {}
        y, m = map(int, USE_START.split("-"))
        ey, em = map(int, USE_END.split("-"))
        while (y, m) <= (ey, em):
            use_ym = f"{y}-{m:02d}"
            shifted[use_ym] = sorted(mp.get(prev_month(use_ym), set()))
            m += 1
            if m > 12:
                m = 1
                y += 1
        shifted_all[name] = shifted
        sizes = [len(v) for v in shifted.values()]
        avg = sum(sizes) / len(sizes) if sizes else 0
        p = OUTJSON / f"jq_attack_{name}.json"
        p.write_text(json.dumps(shifted, ensure_ascii=False), encoding="utf-8")
        print(f"[export] {name}: {len(shifted)}个月 | 平均池 {avg:.0f}只 | {p}", flush=True)

    # 生成 py 模块(字面量字典, 免上传 JSON)
    L = ["'''自动生成: R1~R6 月度轮动池。键=使用月(当月交易), 值=上月末 pool_as_of, 无未来函数。'",
         "由 scripts/export_jq_pools.py 生成, 勿手改。聚宽侧 from jq_attack_pools import JQ_ATTACK_POOLS'''",
         "",
         "JQ_ATTACK_POOLS = {"]
    for name in RULES:
        L.append(f'    "{name}": {{')
        for ym, codes in shifted_all[name].items():
            jq = [to_jq(c) for c in codes]
            L.append(f'        "{ym}": {jq!r},')
        L.append('    },')
    L.append('}')
    OUTPY.write_text("\n".join(L) + "\n", encoding="utf-8")
    import os
    print(f"[export] 写模块 {OUTPY} ({os.path.getsize(OUTPY)/1024:.0f} KB)", flush=True)
    print("[export] 完成。聚宽侧设 ATTACK_POOL_RULE='R3' 即可, 无需上传 JSON。")

    # 同步单文件内联版 joinquant_unified_single.py(用户直接粘贴到聚宽的版本,
    # 末尾自带 JQ_ATTACK_POOLS 字面量)。仅替换从 'JQ_ATTACK_POOLS = {' 到文件末尾的块,
    # 保留其上方策略代码与注释, 确保单次粘贴即可用、且与模块始终一致。
    SINGLE = Path(__file__).resolve().parent / "joinquant_unified_single.py"
    if SINGLE.exists():
        txt = SINGLE.read_text(encoding="utf-8")
        marker = "JQ_ATTACK_POOLS = {"
        i = txt.index(marker)
        head = txt[:i]  # 保留 marker 之前的注释/空行
        L2 = ["JQ_ATTACK_POOLS = {"]
        for name in RULES:
            L2.append(f'    "{name}": {{')
            for ym, codes in shifted_all[name].items():
                jq = [to_jq(c) for c in codes]
                L2.append(f'        "{ym}": {jq!r},')
            L2.append('    },')
        L2.append('}')
        SINGLE.write_text(head + "\n".join(L2) + "\n", encoding="utf-8")
        import os as _os
        print(f"[export] 同步单文件内联 {SINGLE} ({_os.path.getsize(SINGLE)/1024:.0f} KB)", flush=True)


if __name__ == "__main__":
    main()
