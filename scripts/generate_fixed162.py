#!/usr/bin/env python3
"""生成固定162质量池(jq后缀), 供 joinquant_unified_strategy.py 的 FIXED 模式使用。

固定162池 = get_all_t0_etfs() 全量 = 手工T0(含主题) + auto质量宽基(refresh_t0_pool.py剔除主题),
复现本地 backtest_10y_ab.py 的 B 策略(全池当日涨幅Top1, 不区分regime)。

产出 scripts/jq_attack_pool_fixed162.py (内含 FIXED_162_POOL 字面量, 上传聚宽免JSON)。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from t0_etf_list import get_all_t0_etfs  # noqa: E402


def to_jq(code: str) -> str:
    base = code.split(".")[0]
    return f"{base}.XSHG" if base[0] in "56" else f"{base}.XSHE"


def main():
    etfs = get_all_t0_etfs()
    manual = [e for e in etfs if e.get("type_name") != "自动"]
    auto = [e for e in etfs if e.get("type_name") == "自动"]
    # ★对齐本地 backtest_10y_ab.py: 手工层靠 codes5(5min覆盖)过滤掉 3 只退市虚标,
    #   这里等价排除(不动 t0_etf_list.py 清单本身, 遵守"不删3只"决定)。
    DELISTED = {"159833", "513680", "513960"}
    codes = sorted({to_jq(e["code"]) for e in etfs if e["code"] not in DELISTED})
    print(f"[gen] 固定162池: {len(codes)} 只 "
          f"(手工 {len(manual)-len(DELISTED)} 有效 + auto质量 {len(auto)})", flush=True)

    out = HERE / "jq_attack_pool_fixed162.py"
    L = [
        "'''自动生成: 固定162质量池(手工T0 + auto质量宽基), 复现本地 backtest_10y_ab.py B策略。'",
        "由 scripts/generate_fixed162.py 生成, 勿手改。聚宽侧 "
        "from jq_attack_pool_fixed162 import FIXED_162_POOL'''",
        "",
        "FIXED_162_POOL = [",
    ]
    L += [f"    {c!r}," for c in codes]
    L.append("]")
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"[gen] 写 {out} ({out.stat().st_size // 1024} KB)", flush=True)


if __name__ == "__main__":
    main()
