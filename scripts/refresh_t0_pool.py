"""自动刷新 T+0 候选池（零手工维护）。

设计：
- 不修改任何策略逻辑、不修改手工清单（t0_etf_list.py 的 4 个分类表保持原样）。
- 只把"全市场里漏收的真 T+0 标的"写入 auto_t0_etfs.json，由 t0_etf_list.load_auto_t0_etfs() 合并进候选池。
- 判定用【监管前缀法】（可靠、几乎零误判）：
    * 511/513/518 前缀 ETF = 债券/跨境/黄金商品，法定全 T+0；
    * 501/161/162 前缀 LOF = 跨境/债券/黄金，法定全 T+0；
    * 159xxx 与 T+1 共用前缀，仅靠【安全跨境/商品关键词】补抓（越南/亚太/中韩/中阿/德国/法国/印度/沙特/英国/东南亚/日经/东证/恒生/港股通/标普/纳指/中概/原油/黄金/豆粕/商品/油气/白银/稀土…）。
- 负向过滤：货币(货币/快线/保证金/现金/理财)、新能源/能源行业 ETF（A 股 T+1 行业 ETF，settlement_rule 误判的假阳性）一律排除。
- 每次全量重算写入，退市标的自动消失。

用法：
    python3 scripts/refresh_t0_pool.py            # 刷新并打印差异
    python3 scripts/refresh_t0_pool.py --dry-run  # 只打印会加哪些，不写文件
建议 crontab 每月 1 号运行一次（如 0 9 1 * *）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from t0_etf_list import get_all_t0_etfs, get_all_market_etf_lof, AUTO_T0_JSON  # noqa: E402

# 10 年日K缓存(backtest_10y_ab.py 构建), 用于读上市时长/流动性, 避免每次联网拉取
_FULL_DAILY = Path.home() / ".tradingagents/cache/t0_5min/full_daily_2015_2026.json"

# 可靠 T+0 前缀（监管法定）
_T0_PREFIXES = ("511", "513", "518", "501", "161", "162")

# 159xxx 与 T+1 共用前缀，仅靠这些【安全】跨境/商品关键词补抓真 T+0
_SAFE_T0_KEYWORDS = (
    "纳指", "纳斯达克", "NASDAQ", "Nasdaq", "恒生", "H股", "香港", "港股通", "港股", "HK",
    "中概", "标普", "S&P", "原油", "黄金ETF", "黄金基金", "上海金", "豆粕",
    "商品", "油气", "白银", "稀土", "南方原油", "原油基金", "越南", "亚太",
    "中韩", "中阿", "港股创新药", "德国", "法国", "印度", "沙特", "英国",
    "东南亚", "日经", "东证",
)

# 负向过滤：这些虽可能被前缀/关键词命中，但属货币(零波动)或 A 股 T+1 行业 ETF，排除
_NEGATIVE_KEYWORDS = ("货币", "快线", "保证金", "现金", "理财", "新能源", "能源")

# ============================================================================
# 质量门槛(2026-08-03 加入): 回测证明"无脑加所有 T+0"对 B 策略显著有害
# (池子从 103→402, B 收益腰斩 +63%→-65% 回撤)。根因: 自动发现层混进大量
# 【主题/行业型】跨境 ETF(港股创新药/中韩半导体/标普医药·消费·油气…),
# 它们高波动、均值回归, 被"当日涨幅 Top1≥3%"选中即亏。
# 手工 103 池全是【宽基】跨境/商品指数(恒生/纳指/标普/黄金/原油…), 有趋势、适合跳空动量。
# 故质量门槛 = 宽基正关键词 + 主题负关键词 + 上市时长 + 流动性。
# ============================================================================
# 主题/行业负关键词: 命中即视为"非宽基", 即便同时命中宽基正关键词也排除。
# (手工池里的 562990 稀土ETF 等由 refresh 跳过, 不受影响; 仅约束新发现标的)
_SECTOR_NEGATIVE_KEYWORDS = (
    "创新药", "医药", "生物", "医疗", "中药", "医", "新药", "生科", "生物科技",
    "疫苗", "制药", "半导体", "芯片", "消费", "信息科技",
    "油气", "机器人", "人工智能", "AI", "传媒", "游戏", "文娱", "军工", "有色",
    "煤炭", "钢铁", "化工", "农业", "食品", "饮料", "白酒", "汽车", "地产", "银行",
    "证券", "券商", "保险", "金融", "电力", "公用事业", "基建", "建材",
    "新能源", "光伏", "储能", "电池", "5G", "通信", "云计算", "大数据",
    "物联网", "智能", "高端装备", "软件", "电子", "机械", "航空", "航天",
    "航运", "物流", "旅游", "影视", "教育", "养殖", "畜牧", "稀土", "材料",
    "制造", "装备",
)
_MIN_LISTING_DAYS = 120          # 至少约 6 个月交易历史(避开上市即被选中、流动性未成形的新标的)
_MIN_AVG_TURNOVER = 30_000_000   # 日均成交金额 ≥ 3000 万(流动性门槛, 排除迷你主题 ETF)


# 宽基港股 ETF(159xxx 的港股通/恒生/H股/香港类)不是任何动量攻击策略的 alpha 源
# (单日暴涨但 T+1 回吐、挤占真正强票), 聚宽 R3 回测含 vs 不含净拖累 -177pp。
# 作为事前规则从 auto 层剔除(与 dynamic_pool 的 R3 exclude_hk_wide 一致), 防未来 refresh 自动加回。
_HK_WIDE_KEYWORDS = ("港股通", "港股", "H股", "香港", "HK")


def _is_hk_wide(code: str, name: str) -> bool:
    return code.startswith("159") and any(kw in name for kw in _HK_WIDE_KEYWORDS)


def _is_genuine_t0(code: str, name: str) -> bool:
    if any(kw in name for kw in _NEGATIVE_KEYWORDS):
        return False
    # 可靠 T+0 前缀（监管法定）：债券/跨境/黄金商品 ETF 与跨境/债券/黄金 LOF
    if code.startswith(_T0_PREFIXES):
        return True
    # 159xxx 与 T+1 共用前缀：仅当名称确属跨境/商品关键词时才认定为真 T+0，
    # 避免把 159xxx 的 A 股行业 ETF（半导体/新能源等）误收。
    # 其他前缀（510/512/515/516/560/561/562/563/588…）即便名称含关键词也属 T+1 行业 ETF，排除。
    if code.startswith("159"):
        clean = name.replace(" ", "")
        return any(kw in clean for kw in _SAFE_T0_KEYWORDS)
    return False


def _daily_metrics(code: str) -> tuple[int | None, float | None]:
    """从 full_daily 缓存读上市时长(交易日数)与近 250 日日均成交金额(成交量×收盘近似)。

    缓存的 auto ETF 日K可能缺 volume 字段(backfill 未存) → 全 0 时回退 sina 取真实成交量。
    返回 (listing_days, avg_turnover); 任一不可得则为 None(调用方按"不阻塞"处理)。
    """
    recs: list[dict] = []
    if _FULL_DAILY.exists():
        try:
            d = json.loads(_FULL_DAILY.read_text(encoding="utf-8"))
            recs = d.get(code, {}).get("returns", [])
        except Exception:
            recs = []
    if recs and all((r.get("volume", 0) or 0) <= 0 for r in recs):
        recs = []          # 缓存缺 volume → 强制走 sina 回退
    if not recs:
        try:
            import akshare as ak
            h = ak.fund_etf_hist_sina(symbol=("sh" if code[0] in "56" else "sz") + code)
            for _, row in h.iterrows():
                try:
                    recs.append({"date": str(row["date"])[:10],
                                 "close": float(row["close"]),
                                 "volume": float(row.get("volume") or 0)})
                except Exception:
                    continue
        except Exception:
            return None, None
    if not recs:
        return None, None
    listing = len(recs)
    win = recs[-250:]
    turns = [r.get("volume", 0) * r.get("close", 0) for r in win
             if r.get("volume", 0) > 0 and r.get("close", 0) > 0]
    avg = sum(turns) / len(turns) if turns else 0.0
    return listing, avg


def _passes_quality(code: str, name: str) -> tuple[bool, str]:
    """质量门槛: 宽基判定 + 主题负关键词 + 上市时长 + 流动性。

    宽基判定:
      · 可靠 T+0 前缀(511/513/518/501/161/162) → 监管法定跨境/债券/商品, 即宽基, 直接过;
      · 159xxx(与 T+1 共用前缀) → 必须含宽基跨境/商品正关键词才过;
      · 其他前缀 → 非 T+0, 不过。
    主题/行业负关键词对所有前缀一律拦截(命中即视为高波动均值回归型, 不适合跳空动量)。

    返回 (通过?, 原因/指标说明)。
    """
    hit_sector = [kw for kw in _SECTOR_NEGATIVE_KEYWORDS if kw in name]
    if hit_sector:
        return False, f"主题/行业ETF(命中{hit_sector})"
    if code.startswith(_T0_PREFIXES):
        pass                                    # 前缀保证跨境外/债券/商品, 宽基
    elif code.startswith("159"):
        if not any(kw in name for kw in _SAFE_T0_KEYWORDS):
            return False, "159xxx非宽基跨境/商品关键词"
    else:
        return False, "非T+0前缀"
    listing, turnover = _daily_metrics(code)
    if listing is None:
        # 全市场扫描含该 code 但 full_daily + sina 都拉不到行情 → 幽灵标的(数据源缺失),
        # 回测/实盘都交易不了, 直接否决避免虚增池子、浪费全市场扫描。
        return False, "无行情数据(不可达, 排除幽灵标的)"
    if listing is not None and listing < _MIN_LISTING_DAYS:
        return False, f"上市仅{listing}天(<{_MIN_LISTING_DAYS})"
    if turnover is not None and turnover < _MIN_AVG_TURNOVER:
        return False, f"日均成交{(turnover or 0)/1e6:.0f}M(<{_MIN_AVG_TURNOVER/1e6:.0f}M)"
    return True, f"listing={listing}天,turnover={(turnover or 0)/1e6:.0f}M"


def refresh(use_quality: bool = True) -> dict:
    current = get_all_t0_etfs()
    current_codes = {e["code"] for e in current}

    market = get_all_market_etf_lof()
    if not market:
        raise RuntimeError("全市场名单为空：mootdx 未连上行情服务器（需网络）。")

    added: list[dict] = []
    rejected: list[dict] = []   # 通过 _is_genuine_t0 但被质量门槛否决的(供诊断)
    for info in market:
        code = info["code"]
        name = info.get("name") or info.get("etf_name") or ""
        if code in current_codes:
            continue
        if not _is_genuine_t0(code, name):
            continue
        if _is_hk_wide(code, name):
            rejected.append({"code": code, "name": name, "why": "宽基港股(非alpha源,排除)"})
            continue
        if use_quality:
            ok, why = _passes_quality(code, name)
            if not ok:
                rejected.append({"code": code, "name": name, "why": why})
                continue
        added.append({
            "code": code,
            "name": name,
            "sina_symbol": info.get("sina_symbol", ""),
        })

    # 全量重算：仅保留当前市场真实存在的自动发现标的（退市自动消失）
    # 注意 current_codes 已含上一次 auto，这里 added 是不在手工清单+上次auto里的真T0
    # 但要合并"上次auto中仍在市场"的，避免每次把老auto丢掉重加导致抖动：
    prev_auto = {d["code"] for d in (json.loads(AUTO_T0_JSON.read_text(encoding="utf-8"))
                                     if AUTO_T0_JSON.exists() else [])}
    # 重新扫描：把 prev_auto 中仍在市场、且仍通过判定规则的也保留（用最新规则重新校验，
    # 避免旧规则漏网项(主题ETF)残留——质量门槛收紧后会自动把这些垃圾清出 auto 层）。
    kept_prev: list[dict] = []
    added_codes = {d["code"] for d in added}
    manual_codes = {e["code"] for e in current if e["type_name"] != "自动"}
    if AUTO_T0_JSON.exists():
        try:
            prev = json.loads(AUTO_T0_JSON.read_text(encoding="utf-8"))
            mkt = {info["code"]: info for info in market}
            for d in prev:
                c = d["code"]
                if c in added_codes or c in manual_codes:
                    continue
                # 用市场全名(若可得)覆盖可能截断的旧名, 提升质量判定准确度
                nm = (mkt.get(c, {}) or {}).get("name") or d.get("name", "")
                # 市场扫描对 LOF(161/501/162)可能不全 → 即便 c 不在 mkt 也保留
                # (旧 auto 已通过 genuine 校验, 仅按最新质量门槛重新过滤主题ETF)
                if _is_genuine_t0(c, nm) and not _is_hk_wide(c, nm):
                    if use_quality:
                        ok, _ = _passes_quality(c, nm)
                        if not ok:
                            continue            # 旧规则漏网的主题ETF → 清出
                    kept_prev.append({"code": c, "name": nm,
                                      "sina_symbol": d.get("sina_symbol", "")})
        except Exception:
            kept_prev = []
    merged = added + kept_prev
    merged.sort(key=lambda d: d["code"])
    return {
        "current_pool_size": len(current_codes),
        "market_size": len(market),
        "added": added,
        "rejected": rejected,
        "kept_prev": kept_prev,
        "merged_size": len(merged),
        "merged": merged,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--no-quality", action="store_true",
                    help="关闭质量门槛(仅前缀/关键词判定), 用于对照回测")
    args = ap.parse_args()

    try:
        res = refresh(use_quality=not args.no_quality)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"当前候选池: {res['current_pool_size']} 只 | 全市场: {res['market_size']} 只")
    print(f"本次新增(通过质量): {len(res['added'])} 只 | 质量否决: {len(res['rejected'])} 只 "
          f"| 保留上次自动: {len(res['kept_prev'])} 只 → auto 层合计 {res['merged_size']} 只")
    for d in res["added"]:
        print(f"  + {d['code']}  {d['name']}")
    if res["rejected"]:
        print("\n--- 质量门槛否决的样本(前30) ---")
        for d in res["rejected"][:30]:
            print(f"  - {d['code']}  {d['name']}  [{d['why']}]")

    if args.dry_run:
        print("\n[--dry-run] 未写入文件。")
        return 0

    AUTO_T0_JSON.write_text(
        json.dumps(res["merged"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n已写入: {AUTO_T0_JSON} (auto 层 {res['merged_size']} 只)")
    print("提示: t0_etf_list.get_all_t0_etfs() 下次调用即自动包含；回测按上市日展开无前视。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
