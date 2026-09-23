#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时刻分层分析 —— "几点推更赚"的每日复盘表(10分钟粒度)。

背景(2026-09-23):
  ai_judgements.jsonl 里的 ret_open1 是 unique-test 口径 = 次日开盘/【当日收盘】,
  与推送时刻无关(同票同日所有判定共用一个值), 度量不了时点优劣。故本脚本改用
  【入场口径】= 次日开盘 / 判定时刻价, 其中

      判定时刻价 = 昨收 × (1 + pct/100)        # pct = 判定瞬间涨幅(记录内已有)

  实测校验: 公式价与 youzi_sold.jsonl 的真实 buy_price 11/11 笔偏差 0.00%。

输出(三块):
  1) 10分钟格子表: n / 封板率 / 开-收盘 / 开-判定价   (prob≥75 与 全量 各一份)
  2) 大块汇总 + 截止X累计(停推时刻的边际价值)
  3) 结论行: 封板率坍缩点(唯一天然单调的维度)

用法: python3 scripts/youzi_time_slice.py [--days 10] [--min-prob 75]
cron: 10 16 * * 1-5 (须在 youzi_calibrate.py 回填 seal/ret 之后运行)
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

STATE = Path.home() / ".tradingagents" / "youzi"
JL = STATE / "ai_judgements.jsonl"
CACHE = STATE / "time_slice_daily.json"       # code -> {日期: [开, 收]}
REPORT = STATE / "time_slice_report.txt"

# 10 分钟格子: 09:35 ~ 15:00 (午休 11:35-12:55 空档自然跳过)
BUCKETS = [(575 + 10 * i, 585 + 10 * i) for i in range(33)]
BLOCKS = (("早盘 09:35-10:00", 575, 600),
          ("上午 10:00-11:30", 600, 691),
          ("午后 13:00-14:30", 780, 870),
          ("尾盘 14:30-15:00", 870, 900))


def fmt(mm: int) -> str:
    return "%02d:%02d" % (mm // 60, mm % 60)


def load_judgements(days: list) -> dict:
    """按 (日, 代码) 去重取 prob 最大的一条 —— 与 calibrate 同口径。"""
    seen = {}
    if not JL.exists():
        return seen
    for line in JL.read_text(encoding="utf-8").splitlines():
        try:
            o = json.loads(line)
        except Exception:
            continue
        ds = str(o.get("ts"))[:10]
        if ds not in days:
            continue
        k = (ds, str(o.get("code")))
        if k not in seen or (o.get("prob") or -1) > (seen[k].get("prob") or -1):
            seen[k] = o
    return seen


def refresh_daily(codes: list, upto: str) -> dict:
    """增量更新日K缓存: 只补 缓存缺失/落后于 upto 的代码。失败静默用旧缓存。"""
    cache = {}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    need = [c for c in codes
            if c not in cache or not cache[c] or max(cache[c]) < upto]
    if not need:
        return cache
    print("  日K缓存: 需补 %d/%d 只 ..." % (len(need), len(codes)), flush=True)
    try:
        from youzi_calibrate import fetch_daily, TdxHq_API, TDX
    except Exception as exc:
        print("  [warn] 无法引入日K接口(%s), 用旧缓存" % exc)
        return cache
    api = TdxHq_API()
    try:
        if not api.connect(*TDX):
            return cache
    except Exception:
        return cache
    for i, c in enumerate(need):
        d = None
        for _ in range(2):
            try:
                d = fetch_daily(api, c, 25)
            except Exception:
                d = None
            if d is not None and len(d):
                break
            time.sleep(0.4)
        if d is not None and len(d):
            cache[c] = {str(x)[:10]: [float(o), float(cl)]
                        for x, o, cl in zip(d["date"], d["open"], d["close"])}
        else:
            cache.setdefault(c, {})          # 记账: 拉不到的别每轮重试
        if (i + 1) % 100 == 0:
            print("    ...%d/%d" % (i + 1, len(need)), flush=True)
    try:
        api.disconnect()
    except Exception:
        pass
    try:
        CACHE.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass
    return cache


def build_rows(jd: dict, daily: dict) -> tuple:
    """→ (rows, skipped)。两道数据校验:

    ① pct 合法性: 打板场景 pct 必在 (0, 21] 内(20cm 板上限)。历史上有几天记录成
       double-scale 的坏值(2026-09-10 记录 pct=800~930), 直接算会得出 -89% 的假收益
       (16 笔异常全来自那天), 故越界一律剔除。
    ② 相邻交易日: 昨收/次日开盘必须取自"紧邻"的交易日(日历跨度 ≤5 天), 否则说明
       该票中途停牌或日K缺口 → 昨收不是真昨收, 判定价算错。
    """
    rows, skipped = [], 0
    for (ds, code), o in jd.items():
        b = daily.get(code) or {}
        pds = [d for d in b if d < ds]
        nds = [d for d in b if d > ds]
        if not pds or not nds:
            skipped += 1
            continue
        prev_d, nxt_d = max(pds), min(nds)
        t0 = datetime.strptime(ds, "%Y-%m-%d")
        if (t0 - datetime.strptime(prev_d, "%Y-%m-%d")).days > 5 or \
                (datetime.strptime(nxt_d, "%Y-%m-%d") - t0).days > 5:
            skipped += 1
            continue
        pct = float(o.get("pct") or 0)
        if not (0 < pct <= 21):
            skipped += 1
            continue
        prevc = b[prev_d][1]
        dayc = b[ds][1] if ds in b else None
        nxto = b[nxt_d][0]
        price = prevc * (1 + pct / 100)
        if price <= 0:
            skipped += 1
            continue
        rows.append({
            "d": ds, "code": code, "name": o.get("name", ""),
            "prob": float(o.get("prob") or 0),
            "m": int(str(o["ts"])[11:13]) * 60 + int(str(o["ts"])[14:16]),
            "pct": pct, "price": round(price, 2),
            "seal": 1 if o.get("seal") else 0,
            "ent": (nxto / price - 1) * 100,
            "old": ((nxto / dayc - 1) * 100 if dayc else None),
        })
    return rows, skipped


def _avg(xs, f):
    v = [x[f] for x in xs if x.get(f) is not None]
    return sum(v) / len(v) if v else 0.0


def render(rows: list, title: str) -> list:
    out = ["", "=== 10分钟格子 · %s (n=%d) ===" % (title, len(rows)),
           "%-13s %4s %8s %11s %12s" % ("时段", "n", "封板率", "开/收盘", "开/判定价")]
    for a, b in BUCKETS:
        s = [r for r in rows if a <= r["m"] < b]
        if not s:
            continue
        out.append("%-13s %4d %7.0f%% %+10.2f%% %+11.2f%%" % (
            "%s-%s" % (fmt(a), fmt(b - 1)), len(s),
            100 * sum(x["seal"] for x in s) / len(s), _avg(s, "old"), _avg(s, "ent")))
    out.append("  -- 大块 --")
    for nm, a, b in BLOCKS:
        s = [r for r in rows if a <= r["m"] < b]
        if not s:
            continue
        out.append("  %-16s n=%3d 封板%5.1f%% | 开/收%+.2f%% | 开/判定价%+.2f%%" % (
            nm, len(s), 100 * sum(x["seal"] for x in s) / len(s),
            _avg(s, "old"), _avg(s, "ent")))
    return out


def cumulative(rows: list) -> list:
    """截止X时刻累计: 停推时刻的边际价值(只对配额没打满的日子起作用)。"""
    out = ["", "=== 截止X累计(全部候选, 入场口径) ===",
           "%-9s %5s %8s %10s %10s" % ("截止X", "n", "封板率", "开/判定价", "累计")]
    picks = []
    for cut in (600, 660, 720, 780, 840, 870, 900):
        s = [r for r in rows if r["m"] < cut]
        if not s:
            continue
        picks.append(cut)
        avg = _avg(s, "ent")
        out.append("%-9s %5d %7.1f%% %+9.2f%% %+9.1f%%" % (
            fmt(cut - 1), len(s), 100 * sum(x["seal"] for x in s) / len(s),
            avg, avg * len(s)))
    return out


def conclusion(rows: list, min_prob: float) -> list:
    """封板率是唯一天然单调的维度 → 用它定位坍缩点。"""
    out = ["", "=== 结论 ==="]
    sub = [r for r in rows if r["prob"] >= min_prob]
    for nm, a, b in BLOCKS:
        s = [r for r in sub if a <= r["m"] < b]
        if s:
            out.append("  prob≥%.0f %-16s 封板率 %5.1f%% (n=%d)" % (
                min_prob, nm, 100 * sum(x["seal"] for x in s) / len(s), len(s)))
    late = [r for r in sub if r["m"] >= 870]
    if late:
        out.append("  → 14:30 后封板率 %.1f%%(n=%d), 建议 PUSH_END=14:30" % (
            100 * sum(x["seal"] for x in late) / len(late), len(late)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="回看最近N个有判定的交易日")
    ap.add_argument("--min-prob", type=float, default=75.0)
    args = ap.parse_args()

    jd_all = {}
    if JL.exists():
        for line in JL.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
            except Exception:
                continue
            jd_all[str(o.get("ts"))[:10]] = 1
    days = sorted(jd_all)[-args.days:]
    if not days:
        print("无判定记录"); return 0
    print("回看 %d 个交易日: %s ~ %s" % (len(days), days[0], days[-1]))

    jd = load_judgements(set(days))
    print("判定去重 %d 笔, 唯一代码 %d 只" % (len(jd), len({c for _, c in jd})))
    daily = refresh_daily(sorted({c for _, c in jd}), days[-1])
    rows, skipped = build_rows(jd, daily)
    print("入场口径可用 %d/%d 笔 (剔除 %d: 无日K/pct越界/停牌)" % (len(rows), len(jd), skipped))
    if not rows:
        return 0

    L = ["youzi 时刻分层复盘 · 生成 %s · 回看 %s~%s · 可用 %d/%d 笔(剔除%d)"
         % (datetime.now().strftime("%Y-%m-%d %H:%M"), days[0], days[-1],
            len(rows), len(jd), skipped)]
    L += render([r for r in rows if r["prob"] >= args.min_prob],
                "prob≥%.0f" % args.min_prob)
    L += render(rows, "全部候选")
    L += cumulative(rows)
    L += conclusion(rows, args.min_prob)
    text = "\n".join(L)
    print(text)
    try:
        REPORT.write_text(text, encoding="utf-8")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
