#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 判断校准闭环 — 让 AI 用自己的实盘战绩校准 prob 标尺。

闭环三步:
  1. 记录(实时): youzi_live 每轮 AI 判定 → ai_judgements.jsonl
  2. 回填(每日收盘后): pytdx 日K → 当日是否封板 / 次日开盘与收盘溢价
  3. 报告(注入提示词): prob 分档 × 实际结果 — AI 据此校准自己的给分

为什么不用历史回测当锚: 历史统计有前视风险且是"平均规律";
本报告是 AI 自己判断记录的前瞻统计 — 无前视、持续更新、越跑越准。

crontab: 20 15 * * 1-5 (收盘后运行; 次日数据未到时只填当日封板状态)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

STATE = Path.home() / ".tradingagents" / "youzi"
JL = STATE / "ai_judgements.jsonl"
REPORT = STATE / "ai_calibration_report.txt"
TDX = ("180.153.18.170", 7709)


def load_recs() -> list[dict]:
    if not JL.exists():
        return []
    recs = []
    for line in JL.read_text().splitlines():
        try:
            recs.append(json.loads(line))
        except Exception:
            pass
    return recs


def fetch_daily(api: TdxHq_API, code: str, n: int = 15):
    m = TDXParams.MARKET_SH if code[0] in "569" else TDXParams.MARKET_SZ
    try:
        bars = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, m,
                                     code.encode(), 0, n)
        if not bars:
            return None
        d = api.to_df(bars)
        d["date"] = d["datetime"].str[:10]
        return d.sort_values("date").reset_index(drop=True)
    except Exception:
        return None


def backfill() -> int:
    """未回填记录 → 补当日封板状态; 次日数据已生成则补次日溢价。"""
    recs = load_recs()
    if not recs:
        print("无判定记录")
        return 0
    api = TdxHq_API()
    if not api.connect(*TDX, time_out=5):
        print("行情连接失败")
        return 0
    cache: dict[str, object] = {}
    n_done = 0
    try:
        for r in recs:
            if r.get("filled"):
                continue
            code, dt = r.get("code", ""), r["ts"][:10]
            if code not in cache:
                cache[code] = fetch_daily(api, code)
            d = cache[code]
            if d is None or len(d) < 2:
                continue
            dates = d["date"].tolist()
            if dt not in dates:
                continue
            i = dates.index(dt)
            if i < 1:
                continue
            thr = float(r.get("thr_pct") or 10.0)
            prev_close = float(d.iloc[i - 1]["close"])
            day_close = float(d.iloc[i]["close"])
            # 当日封板: 收盘涨幅达到该板涨停幅(容差0.2%)
            r["seal"] = bool(day_close / prev_close - 1 >= thr / 100 - 0.002)
            r["seal_date"] = dt
            if i + 1 < len(dates):
                nxt = d.iloc[i + 1]
                r["ret_open1"] = round(
                    (float(nxt["open"]) / day_close - 1) * 100, 2)
                r["ret_close1"] = round(
                    (float(nxt["close"]) / day_close - 1) * 100, 2)
                r["filled"] = True
            n_done += 1
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    JL.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                          for r in recs))
    return n_done


def report() -> str:
    """prob 分档 × 实际结果。同票同日多次判定取最后一次(时间最晚)。"""
    recs = [r for r in load_recs() if r.get("filled")]
    key: dict = {}
    for r in recs:
        k = (r["ts"][:10], r["code"])
        if k not in key or r["ts"] > key[k]["ts"]:
            key[k] = r
    recs = sorted(key.values(), key=lambda r: r["ts"])

    out = [f"AI 判断校准报告 (生成 {datetime.now():%Y-%m-%d %H:%M} | "
           f"已回填 {len(recs)} 条判定)", ""]
    if not recs:
        out.append("暂无已回填数据(判定后需 1 个交易日才能回填次日结果)")
        REPORT.write_text("\n".join(out))
        return "\n".join(out)

    out.append(f"{'prob档':<8}{'笔数':>5}{'封板率':>8}{'次日开盘':>9}{'次日收盘':>9}")
    for name, lo, hi in (("70+", 70, 101), ("65-70", 65, 70),
                         ("60-65", 60, 65), ("50-60", 50, 60), ("<50", 0, 50)):
        sub = [r for r in recs if lo <= float(r.get("prob") or 0) < hi]
        if not sub:
            out.append(f"{name:<8}{0:>5}{'—':>8}{'—':>9}{'—':>9}")
            continue
        seal = sum(1 for r in sub if r.get("seal")) / len(sub) * 100
        o = sum(r.get("ret_open1", 0) for r in sub) / len(sub)
        c = sum(r.get("ret_close1", 0) for r in sub) / len(sub)
        out.append(f"{name:<8}{len(sub):>5}{seal:>7.1f}%{o:>+8.2f}%{c:>+8.2f}%")

    buys = [r for r in recs if r.get("action") == "BUY"]
    out.append("")
    if buys:
        seal = sum(1 for r in buys if r.get("seal")) / len(buys) * 100
        o = sum(r.get("ret_open1", 0) for r in buys) / len(buys)
        c = sum(r.get("ret_close1", 0) for r in buys) / len(buys)
        out.append(f"BUY 判定 {len(buys)} 笔: 封板率 {seal:.1f}% | "
                   f"次日开盘 {o:+.2f}% | 次日收盘 {c:+.2f}%")
        if c < 0:
            out.append("⚠ BUY 整体次日收益为负 — 标尺过松, 需收紧")
    else:
        out.append("BUY 判定 0 笔")
    txt = "\n".join(out)
    REPORT.write_text(txt)
    return txt


def main() -> int:
    n = backfill()
    print(f"回填 {n} 条")
    print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
