"""只读扫描：找出全市场里"可能该进 T+0 池、但当前清单未收录"的标的。

用途：辅助人工定期刷新候选池（季度扫一次，或看到新跨境/商品 ETF 上市时跑）。
- 不改 t0_etf_list.py / 不改任何策略代码 / 不做任何写入式修改。
- 只输出候选清单给你人工挑，挑中后再手动加进 t0_etf_list.py。

判定逻辑（两层 OR，宁可多列不漏）：
1) settlement_rule(code, name) == "T0"  → 交割规则已认定 T+0 但清单没收录；
2) 名称含"疑似 T+0 品类"关键词（跨境/商品类，含 settlement_rule 关键词表里缺失的
   印度/德国/法国/沙特/英国/东南亚/越南/亚太/中韩/中阿/港股创新药 等）→ 新发跨境常
   用这些名字，settlement_rule 因关键词缺口认不出，这里补上。

用法：
    python3 scripts/scan_new_t0_etfs.py
    python3 scripts/scan_new_t0_etfs.py --json ~/.tradingagents/cache/t0_5min/new_t0_candidates.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from t0_etf_list import get_all_t0_etfs, get_all_market_etf_lof  # noqa: E402
from tradingagents.dataflows.instrument import settlement_rule, _T0_ETF_NAME_KEYWORDS  # noqa: E402

# settlement_rule 关键词表缺失、但确属跨境/商品 T+0 的品类名（新发 ETF 常用）
_EXTRA_T0_KEYWORDS = (
    "印度", "德国", "法国", "沙特", "英国", "东南亚", "越南", "亚太",
    "中韩", "中阿", "港股创新药", "日经", "东证", "标普", "纳斯达克",
    "纳指", "恒生", "H股", "中概", "原油", "黄金", "豆粕", "商品",
    "油气", "能源", "可转债", "白银", "稀土", "南方原油",
)
_LIKELY_T0_KEYWORDS = set(_T0_ETF_NAME_KEYWORDS) | set(_EXTRA_T0_KEYWORDS)


def _has_likely_t0_keyword(name: str) -> bool:
    if not name:
        return False
    clean = name.replace(" ", "")
    return any(kw in clean for kw in _LIKELY_T0_KEYWORDS)


def scan() -> dict:
    current = get_all_t0_etfs()
    current_codes = {e["code"] for e in current}

    market = get_all_market_etf_lof()
    if not market:
        raise RuntimeError(
            "全市场名单为空：mootdx 可能未连上行情服务器（需网络）。\n"
            "可改用 akshare 离线名单，或联网后重试。"
        )

    candidates: list[dict] = []
    in_list_flagged_t1: list[dict] = []

    for info in market:
        code = info["code"]
        name = info.get("name") or info.get("etf_name") or ""
        if code in current_codes:
            # 透明性报告：清单内但 settlement_rule 判 T+1（已知漏网/误收）
            if settlement_rule(code, name) != "T0":
                in_list_flagged_t1.append({"code": code, "name": name})
            continue
        rule = settlement_rule(code, name)
        kw = _has_likely_t0_keyword(name)
        if rule == "T0" or kw:
            reason = []
            if rule == "T0":
                reason.append("settlement_rule=T0")
            if kw:
                hit = [kw for kw in _LIKELY_T0_KEYWORDS if kw in name.replace(" ", "")]
                reason.append("名称含T+0品类关键词:" + "/".join(hit))
            candidates.append({
                "code": code,
                "name": name,
                "sina_symbol": info.get("sina_symbol", ""),
                "reason": "; ".join(reason),
            })

    # 排序：settlement_rule 已认 T+0 的优先
    candidates.sort(key=lambda d: (d["reason"].startswith("settlement_rule=T0") is False, d["code"]))
    in_list_flagged_t1.sort(key=lambda d: d["code"])
    return {
        "current_pool_size": len(current_codes),
        "market_size": len(market),
        "candidates": candidates,
        "in_list_flagged_t1": in_list_flagged_t1,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="", help="可选：把结果写到该 JSON 路径")
    args = ap.parse_args()

    try:
        res = scan()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"当前 T+0 池: {res['current_pool_size']} 只 | 全市场扫描: {res['market_size']} 只")
    print(f"\n=== 建议人工复核的新候选 ({len(res['candidates'])} 只) ===")
    if res["candidates"]:
        for c in res["candidates"]:
            print(f"  {c['code']}  {c['name']}  [{c['reason']}]")
    else:
        print("  (无 — 当前清单已覆盖全市场所有可识别的 T+0 标的)")

    print(f"\n=== 参考：清单内但 settlement_rule 判 T+1 ({len(res['in_list_flagged_t1'])} 只) ===")
    for c in res["in_list_flagged_t1"]:
        print(f"  {c['code']}  {c['name']}")

    print("\n提示：挑中候选后，手动加进 scripts/t0_etf_list.py 对应分类表，"
          "并（如需回测对齐）补其 5min/日K 历史。脚本本身不做任何修改。")

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"\n结果已写入: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
