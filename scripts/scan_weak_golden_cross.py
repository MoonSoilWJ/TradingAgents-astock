#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N12 弱金叉选股 + 收盘买/次日收盘卖回放

筛选条件（与 2026-11-12 实盘口径逐条一致）:
  1. 样本 >= 180 根
  2. 120 日年化波动 > 60%
  3. D-1 日 pos = 0  (frac <= 0.5)
  4. D 日涨幅 < 9.5%  (排除涨停买不到)
  5. D 日 pos 0 -> 1  (金叉)
  6. 簇分 score in [0.60, 0.75)
  7. frac < 1        (排除满票)
  8. ret20 < 10%
  9. bias20 < 5%

簇分定义:
  对 6 组 (N,M), diff = TRIX - signal,
  score_g = 1 / (1 + exp(-diff / rolling60_std(diff)))
  score   = mean(score_g)

用法:
  python3 scripts/scan_weak_golden_cross.py --scan 2026-11-12
  python3 scripts/scan_weak_golden_cross.py --replay 2025-06-01 2026-11-11
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import backtest_588000_n12 as B  # noqa: E402

MB = Path.home() / ".tradingagents/youzi/mb_daily.pkl"
OUT = Path.home() / ".tradingagents/rotation/weak_gc_replay.json"

MIN_BARS = 180
MIN_VOL = 60.0
LIMIT = 9.5
SC_LO, SC_HI = 0.60, 0.75
SC_STRONG = 0.75
RET20_MAX = 10.0
BIAS20_MAX = 5.0


def score_series(close: np.ndarray) -> np.ndarray:
    """6 组 (TRIX-signal) 用 60 日滚动标准差归一化后过 sigmoid，再平均。"""
    s = []
    for n, m in B.COMB_N12:
        tr, sig = B.trix_series(close, n, m)
        diff = tr - sig
        scale = pd.Series(diff).rolling(60, min_periods=20).std().values
        s.append(1.0 / (1.0 + np.exp(-diff / (scale + 1e-12))))
    return np.nanmean(np.array(s), axis=0)


PANEL_CACHE = Path.home() / ".tradingagents/cache/wgc_panel.pkl"


def build_panel(use_cache: bool = True) -> pd.DataFrame:
    """对每只股票一次性算好全序列指标（结果缓存）。"""
    if use_cache and PANEL_CACHE.exists():
        age = pd.Timestamp.now() - pd.Timestamp(PANEL_CACHE.stat().st_mtime, unit="s")
        if age.days < 3:
            print("使用缓存 panel: %s" % PANEL_CACHE, flush=True)
            return pd.read_pickle(PANEL_CACHE)
    d = pd.read_pickle(MB)
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values(["code", "date"])
    frames = []
    for code, g in d.groupby("code", sort=False):
        close = g["close"].astype(float).to_numpy()
        if len(close) < MIN_BARS or not np.isfinite(close).all():
            continue
        n = len(close)
        pct = np.zeros(n)
        pct[1:] = close[1:] / close[:-1] - 1.0
        vol = pd.Series(pct).rolling(120, min_periods=60).std().values * np.sqrt(252) * 100
        pos, frac = B.vote_from(close, B.COMB_N12, 0.5)
        sc = score_series(close)
        s = pd.Series(close)
        ret20 = (close / s.shift(20).values - 1.0) * 100
        ma20 = s.rolling(20).mean().values
        bias20 = (close / ma20 - 1.0) * 100
        frames.append(pd.DataFrame({
            "date": g["date"].to_numpy(), "code": code, "close": close,
            "pct": pct * 100, "vol": vol, "pos": pos, "frac": frac,
            "score": sc, "ret20": ret20, "bias20": bias20,
        }))
    panel = pd.concat(frames, ignore_index=True)
    PANEL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    panel.to_pickle(PANEL_CACHE)
    print("panel 已缓存: %s" % PANEL_CACHE, flush=True)
    return panel


def add_signal_flags(p: pd.DataFrame) -> pd.DataFrame:
    p = p.sort_values(["code", "date"]).copy()
    p["pos_prev"] = p.groupby("code")["pos"].shift(1)
    p["sell"] = p.groupby("code")["close"].shift(-1)
    p["sell_date"] = p.groupby("code")["date"].shift(-1)
    for k in (1, 2, 3):
        p["score_m%d" % k] = p.groupby("code")["score"].shift(k)
        p["frac_m%d" % k] = p.groupby("code")["frac"].shift(k)
    p["frac_prev"] = p.groupby("code")["frac"].shift(1)
    p["strong"] = (
        (p["vol"] > MIN_VOL)
        & (p["frac"] >= 1.0 - 1e-9) & (p["frac_prev"] < 1.0 - 1e-9)
        & (p["pct"] < LIMIT)
        & (p["score"] > SC_STRONG)
    )
    p["signal"] = (
        (p["vol"] > MIN_VOL)
        & (p["pos_prev"] == 0)
        & (p["pos"] == 1)
        & (p["pct"] < LIMIT)
        & (p["score"] >= SC_LO) & (p["score"] < SC_HI)
        & (p["frac"] < 1.0 - 1e-9)
        & (p["ret20"] < RET20_MAX)
        & (p["bias20"] < BIAS20_MAX)
    )
    return p


def _seq3(r, key):
    vals = [r.get("%s_m3" % key), r.get("%s_m2" % key), r.get("%s_m1" % key), r.get(key)]
    return " ".join("%5.3f" % v if v is not None and np.isfinite(v) else "    -" for v in vals)


def print_band(band: pd.DataFrame, head: str) -> None:
    """输出落在簇分区间的候选，含前 3 日簇分/簇值。"""
    print(head)
    if band.empty:
        print("（无）")
        return
    print("%-8s%-9s%26s%24s%7s%8s%8s%8s%6s%6s" % (
        "代码", "名称", "簇分 D-3→D-1→今", "簇值 D-3→D-1→今",
        "现价", "今日涨", "ret20", "bias20", "波动", "标记"))
    for _, r in band.sort_values("score", ascending=False).iterrows():
        if r["frac"] >= 1.0 - 1e-9:
            ok = "满票"
        else:
            ok = "✓" if (r["ret20"] < RET20_MAX and r["bias20"] < BIAS20_MAX) else "✗"
        print("%-8s%-9s%26s%24s%7.2f%+8.2f%%%+8.1f%%%+8.1f%%%5.0f%%%5s" % (
            r["code"], r.get("name") or "", _seq3(r, "score"), _seq3(r, "frac"),
            r["px"], r["pct"], r["ret20"], r["bias20"], r["vol"], ok))


def cmd_scan(panel: pd.DataFrame, day: str, mode: str = "weak") -> int:
    p = add_signal_flags(panel)
    row = p[p["date"] == pd.Timestamp(day)]
    print("=== %s ===" % day)
    if mode == "strong":
        band = row[row["strong"]]
        print("高波动 + 满票切换(0.83→1.00) + 簇分>%.2f: %d 只" % (SC_STRONG, len(band)))
    else:
        band = row[(row["score"] >= SC_LO) & (row["score"] < SC_HI)]
        print("今日金叉+高波动+排涨停: %d 只 | 落在簇分 %.2f-%.2f: %d 只" % (
            int(row["signal"].sum()), SC_LO, SC_HI, len(band)))
    print_band(band, "")
    return 0


def notify_band(band: pd.DataFrame, label: str) -> None:
    """有候选时推送钉钉（无候选静默）。"""
    lines = ["### N12 %s 候选（%d 只）" % (label, len(band)), ""]
    for _, r in band.sort_values("score", ascending=False).iterrows():
        lines.append("- **%s(%s)** 簇分 %.3f 簇值 %.2f | 现价 %.2f (%+.2f%%) | ret20 %+.1f%% bias20 %+.1f%%" % (
            r.get("name") or "", r["code"], r["score"], r["frac"],
            r["px"], r["pct"], r["ret20"], r["bias20"]))
    lines.append("")
    lines.append("时间: %s" % pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"))
    try:
        from tradingagents.notify.dingtalk import send_markdown
        ok = send_markdown("N12 %s候选" % label, "\n".join(lines))
        print("钉钉推送:", "成功" if ok else "失败")
    except Exception as e:  # noqa: BLE001
        print("钉钉推送异常:", e)


def cmd_live(mode: str = "weak", do_notify: bool = False) -> int:
    """用腾讯快照扫描今日（盘中）候选；上一交易日收盘由今日涨幅反推。"""
    from tx_quote import snapshot

    d = pd.read_pickle(MB)
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values(["code", "date"])
    codes = sorted(d["code"].astype(str).str.zfill(6).unique())

    live = {}
    for i in range(0, len(codes), 100):
        try:
            s = snapshot(codes[i:i + 100])
            for c, v in s.items():
                try:
                    if float(v["price"]) > 0:
                        live[str(c)] = (float(v["price"]), float(v.get("pct") or 0),
                                        str(v.get("name") or ""))
                except Exception:
                    pass
        except Exception:
            pass
    print("快照: %d 只" % len(live), flush=True)

    rows = []
    for code, g in d.groupby("code", sort=False):
        g = g[g["date"] >= "2025-06-01"]
        if len(g) < MIN_BARS:
            continue
        close = g["close"].astype(float).to_numpy()
        lp = live.get(code)
        if not lp:
            continue
        px, pct, name = lp
        if pct <= -1:
            continue
        prev = px / (1 + pct)
        hist = np.append(close, prev)
        ph = np.zeros(len(hist))
        ph[1:] = hist[1:] / hist[:-1] - 1
        vol = pd.Series(ph).rolling(120, min_periods=60).std().values[-1] * np.sqrt(252) * 100
        if not np.isfinite(vol) or vol <= MIN_VOL:
            continue
        if pct * 100 >= LIMIT:
            continue
        c2 = np.append(hist, px)
        pos2, frac2 = B.vote_from(c2, B.COMB_N12, 0.5)
        sc = score_series(c2)
        if len(sc) < 4:
            continue
        rows.append(dict(
            code=code, name=name, px=px, pct=pct * 100, vol=vol,
            pos=float(pos2[-1]), pos_m1=float(pos2[-2]),
            score=float(sc[-1]), score_m1=float(sc[-2]),
            score_m2=float(sc[-3]), score_m3=float(sc[-4]),
            frac=float(frac2[-1]), frac_m1=float(frac2[-2]),
            frac_m2=float(frac2[-3]), frac_m3=float(frac2[-4]),
            ret20=(c2[-1] / c2[-21] - 1) * 100,
            bias20=(c2[-1] / np.mean(c2[-20:]) - 1) * 100,
        ))

    df = pd.DataFrame(rows)
    print("高波动+排涨停: %d 只" % len(df))
    if df.empty:
        print("（无）")
        return 0
    if mode == "strong":
        band = df[(df["frac"] >= 1.0 - 1e-9) & (df["frac_m1"] < 1.0 - 1e-9)
                  & (df["score"] > SC_STRONG)]
        print("满票切换(0.83→1.00) + 簇分>%.2f: %d 只" % (SC_STRONG, len(band)))
        if band.empty:
            near = df[(df["frac"] >= 1.0 - 1e-9) & (df["frac_m1"] < 1.0 - 1e-9)]
            if len(near):
                print("\n今日满票切换但簇分未达标: %d 只（按簇分取前 8）" % len(near))
                for _, r in near.sort_values("score", ascending=False).head(8).iterrows():
                    print("  %-8s%-9s簇分%.3f(前值%.2f) 现价%.2f %+.2f%%" % (
                        r["code"], r["name"], r["score"], r["frac_m1"], r["px"], r["pct"]))
    else:
        cross = df[(df["pos"] == 1) & (df["pos_m1"] == 0)]
        band = cross[(cross["score"] >= SC_LO) & (cross["score"] < SC_HI)]
        print("今日金叉: %d 只 | 落在簇分 %.2f-%.2f: %d 只" % (
            len(cross), SC_LO, SC_HI, len(band)))
    print_band(band, "")
    if do_notify and len(band):
        notify_band(band, "强势延续" if mode == "strong" else "弱金叉")
    return 0


def cmd_replay(panel: pd.DataFrame, start: str, end: str) -> int:
    p = add_signal_flags(panel)
    m = p["signal"] & p["date"].between(pd.Timestamp(start), pd.Timestamp(end)) & p["sell"].notna()
    out = p[m].copy()
    out["ret"] = (out["sell"] / out["close"] - 1.0) * 100
    if out.empty:
        print("区间内无信号")
        return 0

    n = len(out)
    r = out["ret"]
    daily = out.groupby("date")["ret"].mean()
    eq = float((1 + daily / 100).prod())

    print("=== 回放 %s ~ %s ===" % (start, end))
    print("信号: %d 笔 / %d 个交易日 (日均 %.2f 只, 最大 %d 只)" % (
        n, out["date"].nunique(), n / out["date"].nunique(), out.groupby("date").size().max()))
    print("单笔: 均值 %+.3f%%  中位 %+.3f%%  胜率 %.1f%%" % (
        r.mean(), r.median(), (r > 0).mean() * 100))
    print("按日等权: 日均 %+.3f%%  正收益天数 %.1f%%" % (
        daily.mean(), (daily > 0).mean() * 100))
    print("等权复利: %+.2f%%  (%.3f 倍)" % ((eq - 1) * 100, eq))
    print("单笔分布: p10 %+.2f%%  p25 %+.2f%%  p75 %+.2f%%  p90 %+.2f%%" % tuple(
        r.quantile([0.1, 0.25, 0.75, 0.9])))

    print("\n按年:")
    for y, g in out.groupby(out["date"].dt.year):
        gd = g.groupby("date")["ret"].mean()
        e = float((1 + gd / 100).prod())
        print("  %d  %4d 笔  单笔均 %+.3f%%  胜率 %.1f%%  日均 %+.3f%%  等权复利 %+.1f%%" % (
            y, len(g), g["ret"].mean(), (g["ret"] > 0).mean() * 100, gd.mean(), (e - 1) * 100))

    out["ym"] = out["date"].dt.strftime("%Y-%m")
    monthly = out.groupby("ym")["ret"].agg(["count", "mean"])
    print("\n按月（前 20）:")
    for ym, row in monthly.tail(20).iterrows():
        print("  %s  %3d 笔  单笔均 %+.3f%%" % (ym, int(row["count"]), row["mean"]))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "range": [start, end], "trades": n,
        "mean": float(r.mean()), "median": float(r.median()),
        "win_rate": float((r > 0).mean()), "equity": eq,
        "trades_detail": out[["date", "code", "close", "sell", "sell_date", "ret",
                              "score", "frac", "vol", "ret20", "bias20"]]
        .assign(date=lambda x: x["date"].dt.strftime("%Y-%m-%d"),
                sell_date=lambda x: x["sell_date"].dt.strftime("%Y-%m-%d"))
        .to_dict("records"),
    }, ensure_ascii=False, default=float), encoding="utf-8")
    print("\n明细: %s" % OUT)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="用腾讯快照扫描今日（盘中）")
    ap.add_argument("--scan", default="", help="扫描指定日期的候选")
    ap.add_argument("--replay", nargs=2, metavar=("START", "END"), help="回放区间")
    ap.add_argument("--mode", default="weak", choices=["weak", "strong"],
                    help="weak=弱金叉(默认) / strong=强势延续(满票切换+簇分>0.75)")
    ap.add_argument("--notify", action="store_true", help="有候选时推送钉钉（无候选静默）")
    args = ap.parse_args()
    if args.live:
        return cmd_live(args.mode, args.notify)
    if not args.scan and not args.replay:
        ap.error("需要 --live / --scan / --replay")

    print("加载 mb_daily 并计算指标（3051 只，约 2-4 分钟）...", flush=True)
    panel = build_panel()
    print("完成: %d 行 / %d 只" % (len(panel), panel["code"].nunique()), flush=True)

    if args.scan:
        return cmd_scan(panel, args.scan, args.mode)
    return cmd_replay(panel, args.replay[0], args.replay[1])


if __name__ == "__main__":
    sys.exit(main())
