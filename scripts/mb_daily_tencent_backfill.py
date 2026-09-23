#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""东财限频期间的主板日K兜底: 用腾讯 fqkline 补缺日 → 合并进 mb_daily.pkl.

背景: 2026-09-22 发现 mb_daily.pkl 绝大多数主板股停在 9/18 (东财限频 +
fetch_mainboard_daily 原增量逻辑"存在即跳过"只补缺股不补缺日期), 且东财
kline API 仍在限频冷却(预检 3 只全失败)。腾讯 fqkline (tx_quote 同源)
实测稳定, 用于补缺。

注意:
  · amount 腾讯不提供 → vol(手)×100 × (O+H+L+C)/4 近似 (成交额排名选池用途
    误差 ~1-2%, 对 TopN 排序影响可忽略; 东财恢复后 16:30 cron 重拉, 此后
    以东财真实值为准)
  · 腾讯补的行放前面合并: 与既有碎片(72 只的 9/21)重复时统一保腾讯口径
  · 原子写回 (tmp → replace), 中断不影响原 pkl

用法:
  python3 scripts/mb_daily_tencent_backfill.py                 # 补 < 9/21 的
  python3 scripts/mb_daily_tencent_backfill.py --since 2026-09-22
  python3 scripts/mb_daily_tencent_backfill.py --recent 8 --sleep 0.1
"""
from __future__ import annotations

import argparse
import os as _os

_os.environ["no_proxy"] = _os.environ["NO_PROXY"] = "*"

import time
from pathlib import Path

import pandas as pd
import requests

OUT_PKL = Path.home() / ".tradingagents" / "youzi" / "mb_daily.pkl"
URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
       "?param={sym},day,,,{recent},qfq")
SINA_URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php"
            "/CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no"
            "&datalen={recent}")
HEAD = {"Referer": "https://gu.qq.com", "User-Agent": "Mozilla/5.0"}


def _sym(code: str) -> str:
    return ("sh" if code[0] in "569" else "sz") + code


def fetch_recent(code: str, recent: int, source: str = "tencent"):
    """日K最近 recent 根 → DataFrame[code,date,open,close,high,low,amount] 或 None.

    tencent: fqkline qfq 前复权, volume=手, WAF 对高频敏感 (501).
    sina:    CN_MarketData 不复权, volume=股, 宽松; 补最近两日时与 qfq 等价
             (除非当日恰除权, 极少; 东财恢复后 cron 重拉覆盖).
    """
    try:
        if source == "sina":
            r = requests.get(SINA_URL.format(sym=_sym(code), recent=recent),
                             headers=HEAD, timeout=8)
            rows = r.json()
            if not isinstance(rows, list) or not rows:
                return None
            recs = []
            for w in rows:
                o, c, h, l = (float(w["open"]), float(w["close"]),
                              float(w["high"]), float(w["low"]))
                vol = float(w["volume"])               # 股
                recs.append({"code": code, "date": pd.Timestamp(w["day"]),
                             "open": o, "close": c, "high": h, "low": l,
                             "amount": vol * (o + h + l + c) / 4.0})
            return pd.DataFrame(recs) if recs else None
        r = requests.get(URL.format(sym=_sym(code), recent=recent),
                         headers=HEAD, timeout=8)
        node = (r.json().get("data") or {}).get(_sym(code), {})
        rows = node.get("qfqday") or node.get("day") or []
        recs = []
        for w in rows:
            if len(w) < 6:
                continue
            o, c, h, l = (float(w[1]), float(w[2]), float(w[3]), float(w[4]))
            vol = float(w[5])                          # 手
            recs.append({"code": code, "date": pd.Timestamp(w[0]),
                         "open": o, "close": c, "high": h, "low": l,
                         "amount": vol * 100.0 * (o + h + l + c) / 4.0})
        return pd.DataFrame(recs) if recs else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-21",
                    help="补该日期(含)之后缺的日K (默认 2026-09-21)")
    ap.add_argument("--recent", type=int, default=8, help="每股拉最近 N 根")
    ap.add_argument("--sleep", type=float, default=0.4, help="节流秒/只 (过快触发腾讯WAF 501)")
    ap.add_argument("--cooldown", type=float, default=90.0,
                    help="连续失败熔断后的冷却秒数")
    ap.add_argument("--source", choices=["tencent", "sina"], default="tencent",
                    help="日K源 (东财/WAF 受限时可切 sina)")
    ap.add_argument("--out", default="", help="输出 pkl 路径 (默认主 pkl, 可指到临时文件避免竞态)")
    args = ap.parse_args()
    since = pd.Timestamp(args.since)

    out_pkl = Path(args.out) if args.out else OUT_PKL
    # 基线: 优先用 --out (断点续跑), 否则主 pkl
    base_pkl = out_pkl if out_pkl.exists() else OUT_PKL
    if not base_pkl.exists():
        print(f"! 找不到 {base_pkl}")
        return 1
    base = pd.read_pickle(base_pkl)
    codes = sorted(base["code"].unique())
    last_map = base.groupby("code")["date"].max().to_dict()
    todo = [c for c in codes if pd.Timestamp(last_map[c]) < since]
    print(f"[基线] {len(codes)} 只, pkl 最后日期 {base['date'].max().date()}, "
          f"落后(< {since.date()}) {len(todo)} 只待补  [{args.source}]", flush=True)
    if not todo:
        print("无落后标的, 无需补")
        return 0

    frames, ok, fail = [], 0, 0
    consec_fail = 0
    t0 = time.time()
    for i, code in enumerate(todo):
        d = fetch_recent(code, args.recent, args.source)
        if d is None:
            fail += 1
            consec_fail += 1
            if consec_fail >= 10:          # 疑似 WAF/限流 → 熔断冷却后自愈
                print(f"  [限流?] 连续 {consec_fail} 失败, 冷却 {args.cooldown:.0f}s ...",
                      flush=True)
                time.sleep(args.cooldown)
                consec_fail = 0
        else:
            consec_fail = 0
            d = d[d["date"] >= since]
            if len(d):
                frames.append(d)
                ok += 1
        if (i + 1) % 200 == 0:
            el = time.time() - t0
            print(f"  ...{i+1}/{len(todo)} 成功 {ok} 失败 {fail} | "
                  f"已用 {el/60:.1f}min", flush=True)
        time.sleep(args.sleep)

    if not frames:
        print("! 无可补数据(全部失败)")
        return 1
    df = pd.concat(frames + [base], ignore_index=True).drop_duplicates(
        ["code", "date"], keep="first").reset_index(drop=True)
    tmp = out_pkl.with_suffix(".tmp.pkl")
    df.to_pickle(tmp)
    tmp.replace(out_pkl)
    print(f"[完成] 补 {ok} 只 / 失败 {fail} → 合并后 {df['code'].nunique()} 只 | "
          f"{len(df):,} 行 | {df['date'].min().date()}~{df['date'].max().date()} → {out_pkl}")
    print(f"  最新日期({df['date'].max().date()}) 个股数: "
          f"{(df['date'] == df['date'].max()).sum()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
