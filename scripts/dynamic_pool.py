"""规则动态池：给定月份，返回当时通过 refresh_t0_pool 规则(真 T+0 + 质量门槛)的标的集合。

动机
----
聚宽代码把 AUTO_ETFS 写死成 59 只字面列表，但本地架构本就是规则维护的
(refresh_t0_pool.py 每月重扫全市场，产出 auto_t0_etfs.json)。本模块把同一套规则
"按日期回溯"：对每个历史月份，只有"已上市≥120天 + 近250日日均成交≥3000万 +
宽基(非主题/行业) + 真T+0前缀/关键词"的标的才进池。

规则与 refresh_t0_pool.py 完全一致，仅把"当前时点"换成"回溯时点"：
  · _is_genuine_t0   : 511/513/518/501/161/162 前缀 = 法定T+0；159xxx 需含安全跨境/商品关键词
  · 主题/行业负关键词: 命中即排除(创新药/半导体/消费/油气/军工/银行/证券…)
  · 上市≥120天       : 用 full_daily 上市日(首根bar日期) + 120天 与 月末比较
  · 流动性≥3000万    : 用 full_daily 截至月末近250交易日 volume*close 均值

依赖
----
  · ~/.tradingagents/cache/t0_5min/full_daily_2015_2026.json  (上市日 + 成交量)
  · get_all_market_etf_lof() 全市场名称(159xxx 关键词匹配用；网络，约6s)

用法
----
  from dynamic_pool import pool_as_of, month_pools_for_range
  s = pool_as_of("2024-06")        # 2024-06 月末时点规则池(code 集合)
  mp = month_pools_for_range("2015-01", "2026-08")  # {ym: set}
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import refresh_t0_pool as R  # noqa: E402
from t0_etf_list import get_all_market_etf_lof, get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents/cache/t0_5min"

# ============================================================
# 进攻腿宇宙边界 —— 与防守腿严格分离
# ============================================================
# 进攻腿 = 动量宽基(跨境/商品 ETF); 防守腿 = DEFENSE_POOL(黄金+国债+红利 等避险资产)。
# 二者是两套独立规则, 不可混用。refresh_t0_pool 的 auto 层通过祖父机制把 511xxx
# 国债/货币ETF 也保留进了 auto_t0_etfs.json(59只), 但国债/货币属防守类, 不应进
# 进攻动量池。故在此把以下标的从进攻 universe 中剔除:
#   · 511xxx 前缀 : 全部国债/地债/公司债/可转债/货币ETF/短融 → 防守类, 低波动非动量
#   · 518880      : 黄金ETF(DEFENSE_POOL 避险资产)
#   · 510880      : 红利ETF(DEFENSE_POOL 避险资产)
# 其他黄金/商品 ETF(518680/518860/518890/159830/161116 等) 不在 DEFENSE_POOL, 保留为
# 进攻动量商品标的。
_ATTACK_EXCLUDE_PREFIX = ("511",)
_ATTACK_EXCLUDE_CODE = {"518880", "510880"}


def _attack_filter(codes) -> set[str]:
    """从候选集中剔除防守类(国债/货币/红利/黄金-DEFENSE), 只留动量宽基。"""
    out: set[str] = set()
    for c in codes:
        if c[:3] in _ATTACK_EXCLUDE_PREFIX:
            continue
        if c in _ATTACK_EXCLUDE_CODE:
            continue
        out.add(c)
    return out


# 宽基港股 ETF(159xxx 的港股通/恒生/H股/香港类)不是 R3 动量攻击池的 alpha 源:
# 单日暴涨但 T+1 回吐、且会挤占真正强票(纳指/商品)席位。聚宽回测证明净拖累
# (含 vs 不含 -177pp)。故作为【事前设计规则】排除(非回测后补丁), 仅对 159xxx
# 港股宽基生效, 不动 513xxx 老恒生成分(它们在历史 R3 基准内已验证、属攻击池)。
_HK_WIDE_KEYWORDS = ("港股通", "港股", "H股", "香港", "HK")


def _hk_wide_filter(codes, enabled: bool) -> set[str]:
    """enabled 时剔除 159xxx 中的港股宽基(非攻击池 alpha 源)。"""
    if not enabled:
        return set(codes)
    out: set[str] = set()
    for c in codes:
        nm = _NAME_OF.get(c, "") if _NAME_OF else ""
        if c.startswith("159") and any(kw in nm for kw in _HK_WIDE_KEYWORDS):
            continue
        out.add(c)
    return out
_FULL_DAILY = CACHE / "full_daily_2015_2026.json"

_FULL: dict | None = None
_NAME_OF: dict | None = None


def _load() -> tuple[dict, dict]:
    global _FULL, _NAME_OF
    if _FULL is None:
        _FULL = json.loads(_FULL_DAILY.read_text(encoding="utf-8"))
    if _NAME_OF is None:
        nm: dict[str, str] = {}
        try:
            for m in get_all_market_etf_lof():
                nm[m["code"]] = m["name"]
        except Exception as e:  # 网络不可达时退回手工/auto 名称
            print(f"[dynamic_pool] 全市场名称获取失败({e}), 退回本地名称")
        for e in get_all_t0_etfs():
            nm.setdefault(e["code"], e["name"])
        _NAME_OF = nm
    return _FULL, _NAME_OF


def _month_end(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return (nxt - timedelta(days=1)).isoformat()


def _listing_turnover_asof(code: str, me: str) -> tuple[str | None, float | None]:
    """返回 (上市日, 截至 me 近250日日均成交额)。无数据则 (None, None)。"""
    rec = _FULL.get(code)
    if not rec:
        return None, None
    rets = rec.get("returns") or []
    if not rets:
        return None, None
    fd = rets[0]["date"]
    win = [r for r in rets if r["date"] <= me]
    if len(win) < 20:
        return fd, 0.0
    last = win[-250:]
    turns = [r.get("volume", 0) * r.get("close", 0)
             for r in last
             if (r.get("volume", 0) or 0) > 0 and (r.get("close", 0) or 0) > 0]
    avg = sum(turns) / len(turns) if turns else 0.0
    return fd, avg


def pool_as_of(ym: str, seed: set[str] | None = None,
               universe: set[str] | None = None,
               min_listing_days: int | None = None,
               min_avg_turnover: float | None = None,
               use_seed: bool | None = None,
               drop_sector: bool | None = None,
               exclude_hk_wide: bool = False) -> set[str]:
    """返回 ym(如 '2024-06') 月末时点的规则动态池(去后缀 code 集合)。

    universe: 扫描范围(默认 full_daily 全部 405 只)。回测/聚宽应传入其实际宇宙
              (get_all_t0_etfs() ∩ codes5), 使月度池与可交易标的对齐。
    seed: 祖父保留集。为 None 且 use_seed=True 时, 自动读 auto_t0_etfs.json 的 59 只。
    min_listing_days / min_avg_turnover: 上市天数 / 流动性门槛(覆盖 R 常量, 用于规则扫描)。
    use_seed: 是否启用祖父保留(放宽成交量门槛)。默认 True(忠实复现聚宽 59 演化)。
    drop_sector: 是否启用主题/行业负关键词拦截(宽基 vs 主题)。默认 True。
                  这是回测证明最致命的旋钮——关掉会引入主题ETF导致收益腰斩/回撤爆炸。
    """
    _FULL, _NAME_OF = _load()
    if universe is None:
        universe = set(_FULL.keys())
    # ★进攻腿宇宙: 剔除防守类(国债/货币/红利/黄金-DEFENSE), 与防守腿严格分离
    universe = _attack_filter(universe)
    universe = _hk_wide_filter(universe, exclude_hk_wide)
    me = _month_end(ym)
    med = date.fromisoformat(me)
    mld = R._MIN_LISTING_DAYS if min_listing_days is None else min_listing_days
    gate_days = timedelta(days=mld)
    mat = R._MIN_AVG_TURNOVER if min_avg_turnover is None else min_avg_turnover
    if use_seed is None:
        use_seed = True
    if seed is None and use_seed:
        try:
            seed = {d["code"] for d in json.loads(
                Path("scripts/auto_t0_etfs.json").read_text(encoding="utf-8"))}
        except Exception:
            seed = set()
    else:
        seed = seed or set()
    if drop_sector is None:
        drop_sector = True
    out: set[str] = set()
    for code in universe:
        if code not in _FULL:
            continue
        name = _NAME_OF.get(code, "")
        if not R._is_genuine_t0(code, name):
            continue
        if drop_sector and any(kw in name for kw in R._SECTOR_NEGATIVE_KEYWORDS):
            continue
        fd, turn = _listing_turnover_asof(code, me)
        if fd is None:
            continue
        if date.fromisoformat(fd) + gate_days > med:
            continue
        # 祖父保留: 满足真T+0+上市即可, 放宽成交量门槛
        if code in seed:
            out.add(code)
            continue
        if (turn or 0) < mat:
            continue
        out.add(code)
    return out


def month_pools_for_range(ym_start: str, ym_end: str,
                          universe: set[str] | None = None,
                          exclude_hk_wide: bool = False,
                          **kw) -> dict[str, set[str]]:
    """生成 [ym_start, ym_end] 每月月末规则池。kw 透传给 pool_as_of(规则扫描用)。"""
    y, m = map(int, ym_start.split("-"))
    ey, em = map(int, ym_end.split("-"))
    out: dict[str, set[str]] = {}
    while (y, m) <= (ey, em):
        ym = f"{y}-{m:02d}"
        out[ym] = pool_as_of(ym, universe=universe, exclude_hk_wide=exclude_hk_wide, **kw)
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01")
    ap.add_argument("--end", default="2026-08")
    ap.add_argument("--json", default=str(CACHE / "monthly_pool_report.json"))
    args = ap.parse_args()

    mp = month_pools_for_range(args.start, args.end)
    prev: set[str] = set()
    print(f"{'月份':8} {'池子':>4}  当月新增")
    for ym, s in mp.items():
        added = sorted(s - prev)
        print(f"{ym:8} {len(s):>4}  {','.join(added) if added else '-'}")
        prev = s

    payload = {ym: sorted(s) for ym, s in mp.items()}
    Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    print(f"\n已写入: {args.json}")
