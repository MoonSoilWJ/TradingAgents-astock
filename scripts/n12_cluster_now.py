#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 查看【任意 A股标的】此刻的 N12 结果簇 状态(与 588000 科创50 N12 策略同口径).
# N12 结果簇 = 6 个 TRIX 组合 [(10,9),(10,12),(12,9),(12,12),(14,9),(14,12)] 的每日投票:
#   每组合 TRIX > 其 M 日均线 = 该组合看多;  簇看多票数 /6 > 0.5(= 4/6 以上) → 看多持仓, 否则看空.
#
# 用法(标的可用 6 位代码, 或股票名称):
#   python3 scripts/n12_cluster_now.py 湖南白银
#   python3 scripts/n12_cluster_now.py 002716
#   python3 scripts/n12_cluster_now.py 588000
#   python3 scripts/n12_cluster_now.py 600519
#
# 盘中运行 -> 日线最后一根为当日盘中近似; 收盘后运行 -> 为当日定稿收盘.
import argparse
import contextlib
import datetime as dt
import io
import re
import sys

import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

COMB_N12 = [(10, 9), (10, 12), (12, 9), (12, 12), (14, 9), (14, 12)]  # N12 结果簇
THR = 0.5  # 簇看多占比阈值(>0.5 → 持仓, 同 backtest_588000_n12.py)
SERVERS = [
    ("180.153.18.170", 7709), ("115.238.56.198", 7709),
    ("115.238.90.165", 7709), ("218.108.98.244", 7709),
    ("123.125.108.14", 7709), ("60.28.23.80", 7709),
]

MARKET_TAG = {0: "深圳", 1: "上海"}


def trix_series(c, N, M):
    """与 trix_n12_now.py / gen_vote_n12.py 完全一致: TRIX=EMA3(N) 的 pct_change, 信号=滚动 M 均值."""
    s = pd.Series(np.asarray(c, dtype=float))
    e1 = s.ewm(span=N, adjust=False).mean()
    e2 = e1.ewm(span=N, adjust=False).mean()
    e3 = e2.ewm(span=N, adjust=False).mean()
    tr = e3.pct_change() * 100
    sig = tr.rolling(M).mean()
    return tr.to_numpy(), sig.to_numpy()


def connect_tdx():
    api = TdxHq_API()
    for ip, port in SERVERS:
        try:
            if api.connect(ip, port, time_out=4):
                return api
        except Exception:
            continue
    raise RuntimeError("pytdx 行情服务器全部连接失败, 请检查网络")


# ---------------- 代码 / 名称 解析 ----------------
def _ak_codebook():
    """A股代码->名称, 名称->代码. 一次性拉取(失败返回空)."""
    try:
        import akshare as ak
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            df = ak.stock_info_a_code_name()
        df["name"] = df["name"].astype(str).str.replace(" ", "").str.replace("\u3000", "")
        code2name = dict(zip(df["code"].astype(str), df["name"]))
        return code2name, {v: k for k, v in code2name.items()}
    except Exception:
        return {}, {}


def _tdx_scan_names(api, name):
    """兜底: 直接扫 pytdx 证券列表找名称(该服务器通常只回深圳表, 上海常为空)."""
    found = []
    for market in (0, 1):
        try:
            total = api.get_security_count(market)
        except Exception:
            continue
        start = 0
        while start < total:
            lst = api.get_security_list(market, start)
            if not lst:
                break
            for r in lst:
                nm = str(r.get("name", "")).replace(" ", "").replace("\u3000", "")
                if nm == name:
                    found.append((str(r.get("code")), MARKET_TAG[market]))
            start += len(lst)
    return found


def resolve_symbol(raw):
    """返回 (code, name, exchange_label). 支持 6位代码 / sh/sz前缀 / .SH 后缀 / 中文名称."""
    raw = raw.strip()
    m = re.search(r"(\d{6})", raw)
    code2name, name2code = _ak_codebook()
    if m:  # 代码直给
        code = m.group(1)
        return code, code2name.get(code, ""), None
    # 名称解析: akshare 全 A 股优先
    exact = name2code.get(raw)
    if exact:
        return exact, raw, None
    cands = [(c, n) for n, c in name2code.items() if raw in n]
    if len(cands) == 1:
        return cands[0][0], cands[0][1], None
    if len(cands) > 1:
        print("名称 '%s' 匹配多只, 请用 6 位代码指定: %s" % (raw, [c for c, _ in cands]))
        raise SystemExit(1)
    # 兜底: pytdx 证券表扫描
    api = connect_tdx()
    try:
        hits = _tdx_scan_names(api, raw)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    if len(hits) == 1:
        return hits[0][0], raw, hits[0][1]
    if len(hits) > 1:
        print("名称 '%s' 匹配多只, 请用 6 位代码指定: %s" % (raw, hits))
        raise SystemExit(1)
    print("找不到标的 '%s'. 请用 6 位代码(如 002716) 或正确股票名称" % raw)
    raise SystemExit(1)


def _guess_market(code):
    if code[0] in "569":     # 6xxxxx 沪股 / 5xxxxx 沪基金ETF / 9xxxxx 沪B
        return 1
    return 0                  # 0/1/2/3xxxxx → 深(含 15/16 基金、30 创业板); 8/4/92 北交所不在本脚本范围


def fetch_daily(api, code):
    """拉日线(尽量全), 返回升序 df[date, open, high, low, close, vol]; 失败返回 None."""
    primary = _guess_market(code)
    for market in (primary, 1 - primary):
        frames = []
        try:
            for pg in range(30):
                k = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, market,
                                          code.encode(), pg * 700, 700)
                if not k:
                    break
                d = api.to_df(k)
                if d is None or len(d) == 0:
                    break
                frames.append(d)
                if len(d) < 700:
                    break
        except Exception:
            continue
        if frames:
            f = pd.concat(frames, ignore_index=True)
            f["date"] = pd.to_datetime(f["datetime"]).dt.normalize()
            f = f.sort_values("date").drop_duplicates("date").reset_index(drop=True)
            if len(f) >= 60:   # TRIX(14,12) 需足够预热
                return f, MARKET_TAG[market]
            return None, MARKET_TAG[market]
    return None, None


def cluster_df(close):
    """返回 (bull_matrix[n,6], votes[n], ratio[n]) 基于 N12 簇."""
    bull = np.column_stack([trix_series(close, n, m)[0] > trix_series(close, n, m)[1]
                            for (n, m) in COMB_N12])
    votes = bull.sum(axis=1)
    return bull, votes, votes / len(COMB_N12)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", help="标的: 6位代码 或 股票名称, 如 湖南白银 / 002716 / 588000")
    ap.add_argument("--recent", type=int, default=6, help="显示最近多少个交易日的簇演化 (默认6)")
    args = ap.parse_args()

    code, name, _ = resolve_symbol(args.symbol)
    api = connect_tdx()
    try:
        f, mkt = fetch_daily(api, code)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    if f is None:
        print("拉取 %s 日线失败(退市/停牌过久/代码错误?)" % code)
        raise SystemExit(1)

    label = "%s (%s)" % (name, code) if name else code
    close = f["close"].values.astype(float)
    dates = pd.to_datetime(f["date"].values)
    now = dt.datetime.now()
    last = dates[-1]

    # ---- 时间 / 盘中标记 ----
    if last.date() == now.date():
        tag = "今日盘中近似(以现价计, 收盘后请复算)" if now.time() < dt.time(15, 0) else "今日定稿收盘"
    else:
        tag = "最近交易日 %s (非交易时段/今日尚无K)" % last.date()

    bull, votes, ratio = cluster_df(close)
    chg = (close[-1] / close[-2] - 1) * 100 if len(close) > 1 else np.nan

    # ---- 头部 ----
    print("=" * 66)
    print("N12 结果簇 当前状态  标的: %s  [%s]" % (label, (mkt or "?")))
    print("簇配置: %d 个 TRIX 组合 %s  看多阈值 > %.1f(= %d/6 看多)" %
          (len(COMB_N12), [list(c) for c in COMB_N12], THR, int(THR * len(COMB_N12)) + 1))
    print("K线数: %d   最后一根: %s   收盘(近似) %.3f   当日 %+.2f%%" %
          (len(close), last.date(), close[-1], chg))
    print("行情口径: %s" % tag)
    print("-" * 66)

    # ---- 逐组合明细 ----
    print("%-9s %10s %10s %9s %9s %8s %10s" % ("combo", "TRIX", "signal", "TRIX-sig", "看多?", "状态", "今日交叉"))
    prev_b = bull[:-1] if len(bull) > 1 else None
    for i, (n, m) in enumerate(COMB_N12):
        tr, sig = trix_series(close, n, m)
        tv, sv = float(tr[-1]), float(sig[-1])
        is_bull = bool(bull[-1, i])
        cross = ""
        if prev_b is not None:
            if is_bull and not prev_b[-1, i]:
                cross = "金叉日!"
            elif not is_bull and prev_b[-1, i]:
                cross = "死叉日!"
        print("(%2d,%2d)  %10.4f %10.4f %+9.4f %9s %8s %10s" %
              (n, m, tv, sv, tv - sv,
               "是" if is_bull else "否",
               "金叉(多)" if is_bull else "死叉(空)", cross))

    # ---- 汇总判定 ----
    v, r = int(votes[-1]), float(ratio[-1])
    pos = "看多(持仓)" if r > THR else "看空(空仓)"
    # 连续状态天数 & 最近翻转日
    state_seq = (ratio > THR)
    cur = state_seq[-1]
    k = 0
    for x in state_seq[::-1]:
        if x == cur:
            k += 1
        else:
            break
    flip_idx = None
    for i in range(len(state_seq) - 1, 0, -1):
        if state_seq[i] != state_seq[i - 1]:
            flip_idx = i
            break
    print("-" * 66)
    print("N12簇投票: 看多 %d/6,  看多占比 %.3f   →  判定: %s" % (v, r, pos))
    if flip_idx is not None:
        print("当前%s已持续 %d 个交易日(自 %s 翻%s)" %
              ("看多" if cur else "看空", k, dates[flip_idx].date(),
               "多" if state_seq[flip_idx] else "空"))
    else:
        print("全样本一直%s(%d 日)" % ("看多" if cur else "看空", k))

    # ---- 近端簇演化 ----
    n_show = min(args.recent, len(dates))
    print("-" * 66)
    print("最近 %d 个交易日簇演化:" % n_show)
    print("%-12s %9s %6s %7s   %s" % ("date", "close", "votes", "ratio", "判定"))
    for i in range(len(dates) - n_show, len(dates)):
        print("%s  %9.3f  %d/6   %.3f   %s" %
              (dates[i].date(), close[i], int(votes[i]), float(ratio[i]),
               "看多(持仓)" if ratio[i] > THR else "看空(空仓)"))
    print("=" * 66)
    print("口径说明: 与科创50 N12 策略同一套结果簇(6组合逐日投票)。TRIX 为 EMA3 差分率,")
    print("          signal 为 M 日均线; 盘中最后一根按现价近似, 判断请以收盘复算为准。")


if __name__ == "__main__":
    main()
