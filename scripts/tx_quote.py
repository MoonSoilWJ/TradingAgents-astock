#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""腾讯行情适配器 — TDX(pytdx) 不可用时的主数据源。

背景: 2026-09-10 起 pytdx 公共服务器集体失效(连接成功但协议无数据),
策略停摆。腾讯行情接口 qt.gtimg.cn 批量快(约0.1s/批60只)、字段全:
  现价/昨收/今开/最高/最低/成交量/成交额/换手率/量比/流通市值/
  涨停价/跌停价/买卖五档/均价 — 覆盖半路板选股与盘口判断所需。

字段索引(实测 2026-09-10):
  1名称 2代码 3现价 4昨收 5今开 33最高 34最低 36成交量(手)
  37成交额(万) 38换手率 44流通市值(亿) 47涨停价 48跌停价
  49量比 51均价 9/10买一价/量 19/20卖一价/量

用法:
  from tx_quote import snapshot, minute_bars
  snap = snapshot(["600737", "000988"])     # → {code: {...}}
  bars = minute_bars("600737")              # → DataFrame(datetime,price,vol)
"""
from __future__ import annotations

import os
import time
from typing import Iterable

import requests

os.environ.setdefault("no_proxy", "*")
os.environ.setdefault("NO_PROXY", "*")

URL = "http://qt.gtimg.cn/q="
MIN_URL = ("https://web.ifzq.gtimg.cn/appstock/app/minute/query"
           "?code={code}")
HEAD = {"Referer": "https://gu.qq.com", "User-Agent": "Mozilla/5.0"}
BATCH = 60


def _prefix(code: str) -> str:
    return ("sh" if code[0] in "569" else "sz") + code


def _f(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def snapshot(codes: Iterable[str], timeout: float = 8.0) -> dict:
    """批量拉快照 → {code: {...}}。失败返回已成功部分(部分可用即可用)。"""
    codes = list(codes)
    out: dict[str, dict] = {}
    for i in range(0, len(codes), BATCH):
        part = codes[i:i + BATCH]
        q = ",".join(_prefix(c) for c in part)
        try:
            r = requests.get(URL + q, headers=HEAD, timeout=timeout)
            r.encoding = "gbk"
            for line in r.text.split(";"):
                if "=" not in line or '"' not in line:
                    continue
                f = line.split('"')[1].split("~")
                if len(f) < 50:
                    continue
                code = f[2]
                price = _f(f[3])
                out[code] = {
                    "code": code, "name": f[1], "price": price,
                    "prev_close": _f(f[4]), "open": _f(f[5]),
                    "high": _f(f[33]), "low": _f(f[34]),
                    "vol": _f(f[36]),                    # 手
                    "amount": _f(f[37]) * 1e4,          # 万元 → 元
                    "turn": _f(f[38]),                  # 换手率%
                    "float_mv": _f(f[44]),              # 流通市值(亿)
                    "limit_up": _f(f[47]),
                    "limit_down": _f(f[48]),
                    "vr": _f(f[49]),                    # 量比
                    "vwap": _f(f[51]),                  # 均价
                    "bid1": _f(f[9]), "bid1_vol": _f(f[10]),
                    "ask1": _f(f[19]), "ask1_vol": _f(f[20]),
                    "pct": (price / _f(f[4]) - 1) if _f(f[4]) > 0 else 0.0,
                }
        except Exception:
            continue
        time.sleep(0.02)
    return out


def minute_bars(code: str, timeout: float = 8.0):
    """当日分时 → DataFrame(datetime, price, vol)。失败返回 None。

    用于: 首触形态(10:00 时点涨幅) / 盘中触板次数 / 近30分钟量能。
    """
    import pandas as pd
    try:
        r = requests.get(MIN_URL.format(code=_prefix(code)),
                         headers=HEAD, timeout=timeout)
        d = r.json()["data"][_prefix(code)]["data"]
        rows = d.get("data") or []
        pre = _f(d.get("pre_price") or 0) or _f(
            str(d.get("date", "")).split()[-1] if d.get("date") else 0)
        recs = []
        for line in rows:
            w = line.split()
            if len(w) < 3:
                continue
            t, p, v = w[0], _f(w[1]), _f(w[2])
            recs.append({"hm": t, "price": p, "vol": v})
        if not recs:
            return None
        df = pd.DataFrame(recs)
        df["datetime"] = df["hm"].str.zfill(4)
        df.attrs["prev_close"] = pre
        return df[["datetime", "price", "vol"]]
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    s = snapshot(sys.argv[1:] or ["600737", "000988", "600000"])
    for c, v in s.items():
        print(f"{c} {v['name']:<8} 价{v['price']:>7.2f} "
              f"涨{v['pct']*100:>+6.2f}% 量比{v['vr']:>5.2f} "
              f"换手{v['turn']:>5.2f}% 涨停{v['limit_up']:>7.2f} "
              f"流通{v['float_mv']:>8.1f}亿 额{v['amount']/1e8:>6.2f}亿")
    b = minute_bars("600737")
    print("分时:", len(b) if b is not None else 0, "根",
          f"| 昨收 {b.attrs.get('prev_close') if b is not None else '-'}")
