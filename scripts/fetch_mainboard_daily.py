#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取【全市场沪深主板(60/00)】日K + 流通股本 → 本地缓存。

用途(严格版回测的数据底座):
  1. 严格版半路板回测: 逐日用"截至 T-1 的成交额排名"动态选池(无前视)
  2. 实盘选股指标: 连板数 / 流通市值 / 换手率 / 距 60 日高点 / RSI / 5日10日涨幅

数据源(2026-09-15 重写):
  · 日K: 东财 kline API(前复权 fqt=1, 含成交额, 一次取 2022 起全部)
    — pytdx get_security_bars 已失效(2026-09 实测返回 0 根);
      前复权天然消除送转跳空, 不再需要 fix_splits。
  · 标的清单/流通股本: pytdx(get_security_list / get_finance_info 仍可用);
    流通股本失败时沿用既有 mb_float.json, 不清空。

输出:
  ~/.tradingagents/youzi/mb_daily.pkl   长表 DataFrame[code,date,open,high,low,close,amount]
  ~/.tradingagents/youzi/mb_float.json  {code: 流通股本(股)}

用法: python3 scripts/fetch_mainboard_daily.py [--start 2022-01-01]
      (约 3052 只 × 1 次HTTP/只, ~15-25 分钟)
"""
from __future__ import annotations

import argparse
import json
import os as _os
_os.environ["no_proxy"] = _os.environ["NO_PROXY"] = "*"
import ssl
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

from pytdx.hq import TdxHq_API          # noqa: E402
from pytdx.params import TDXParams      # noqa: E402

TDX_HOST, TDX_PORT = "180.153.18.170", 7709
OUT_DIR = Path.home() / ".tradingagents" / "youzi"
DAILY_PKL = OUT_DIR / "mb_daily.pkl"
FLOAT_JSON = OUT_DIR / "mb_float.json"


def connect() -> TdxHq_API:
    api = TdxHq_API()
    for _ in range(5):
        try:
            if api.connect(TDX_HOST, TDX_PORT, time_out=5):
                return api
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("pytdx 连接失败")


def list_mainboard(api) -> list[tuple[int, str, str]]:
    """全市场 → 沪深主板(60/00), 排除 ST/退市/次新。"""
    out = []
    for market in (TDXParams.MARKET_SH, TDXParams.MARKET_SZ):
        cnt = api.get_security_count(market)
        for st in range(0, cnt, 1000):
            try:
                lst = api.get_security_list(market, st)
            except Exception:
                lst = None
            if not lst:
                continue          # 部分服务器 start=0 返回空, 不能 break
            for it in lst:
                code = str(it.get("code", ""))
                name = str(it.get("name", "")).strip()
                if not code.startswith(("60", "00")):
                    continue
                if "ST" in name or "退" in name or name.startswith("N"):
                    continue
                out.append((market, code, name))
    return out


def _em_get(url: str) -> dict:
    """东财 GET: 浏览器 UA(裸 urllib UA 会被批量拒绝) + 失败重试。"""
    hdr = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36",
           "Referer": "https://quote.eastmoney.com/"}
    last = None
    for wait in (1.0, 3.0, 8.0):     # 限频恢复需要时间, 退避加长
        try:
            req = urllib.request.Request(url, headers=hdr)
            return json.loads(urllib.request.urlopen(req, timeout=12).read())
        except Exception as exc:
            last = exc
            time.sleep(wait)
    raise last if last else RuntimeError("em get failed")


def fetch_one_em(code: str, start: str) -> pd.DataFrame | None:
    """东财日K(前复权)单只 → DataFrame[code,date,open,high,low,close,amount]。"""
    secid = ("1." if code[0] in "569" else "0.") + code
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1,f3"
           "&fields2=f51,f52,f53,f54,f55,f56,f57"
           f"&klt=101&fqt=1&beg={start.replace('-', '')}&end=20500101")
    d = _em_get(url)
    k = (d.get("data") or {}).get("klines") or []
    if len(k) < 60:                            # 上市不足 60 日: 指标不可靠
        return None
    recs = [s.split(",") for s in k]
    out = pd.DataFrame({
        "date": pd.to_datetime([r[0] for r in recs]),
        "open": [float(r[1]) for r in recs],
        "close": [float(r[2]) for r in recs],
        "high": [float(r[3]) for r in recs],
        "low": [float(r[4]) for r in recs],
        "amount": [float(r[6]) for r in recs],   # 成交额(不随复权缩放)
    })
    out = out[(out["close"] > 0) & (out["high"] > 0)].drop_duplicates("date")
    if len(out) < 60:
        return None
    out.insert(0, "code", code)
    return out.reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--limit", type=int, default=0, help="只拉前 N 只(测试)")
    ap.add_argument("--offset", type=int, default=0, help="跳过前 N 只(续传)")
    ap.add_argument("--force", action="store_true",
                    help="强制重拉全部(不跳过已有), 合并时以东财真实值覆盖估算值")
    ap.add_argument("--out", default="", help="输出 pkl 路径(默认 mb_daily.pkl)")
    args = ap.parse_args()

    ssl._create_default_https_context = ssl._create_unverified_context
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_pkl = Path(args.out) if args.out else DAILY_PKL

    # 累加式: 载入既有 pkl, 按【每股最后日期】判定增量, 末尾合并写回
    # (东财限频下每轮只能拉到一部分, 整体覆盖会丢失此前进度 → 必须累加)
    # ⚠️ 2026-09-22 修复: 原逻辑"code 已存在即跳过"只补缺股不补缺日期,
    #    日K 永不前进 (9/22 实测绝大多数股停在 9/18)。改为: 最后日期已含
    #    今日(或 --as-of 指定日)才跳过, 否则重拉全史由合并去重兜底。
    existing_df = None
    existing_codes: set = set()
    existing_last: dict = {}
    trade_idx: dict = {}
    have_sets: dict = {}
    if out_pkl.exists() and not args.offset:
        try:
            existing_df = pd.read_pickle(out_pkl)
            existing_codes = set(existing_df["code"].unique())
            existing_last = existing_df.groupby("code")["date"].max().to_dict()
            # 缺口感知: 全市场覆盖率>95% 的日子视为"必须有的交易日";
            # 个股缺其中任一天 → 判为有洞 → 重拉全史补齐 (停牌股重拉幂等无害)
            day_counts = existing_df["date"].value_counts()
            must_days = sorted(
                day_counts[day_counts > 0.95 * len(existing_codes)].index)
            trade_idx = {d: i for i, d in enumerate(must_days)}
            have_sets = existing_df.groupby("code")["date"].agg(set).to_dict()
            print(f"[基线] 既有 pkl 含 {len(existing_codes)} 只, "
                  f"最后日期 {existing_df['date'].max().date()}, 补拉落后/有洞标的")
        except Exception:
            existing_df = None
    # 增量基准日: 当天 0 点 (当日收盘K拉过即视为已最新); 盘前跑时自然无一股达标 → 全量补
    as_of = pd.Timestamp.now().normalize()

    api = connect()
    try:
        uni = list_mainboard(api)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    if args.offset:
        uni = uni[args.offset:]
    if args.limit:
        uni = uni[:args.limit]
    print(f"[1/2] 主板标的 {len(uni)} 只 → 东财拉日K ...", flush=True)

    # 限频预检: 东财对连发限IP(冷却期未知) → 开跑前先试 3 只, 全失败即退出
    probe_ok = 0
    for code in ("000001", "600000", "000002"):
        try:
            if fetch_one_em(code, args.start) is not None:
                probe_ok += 1
        except Exception:
            pass
        time.sleep(1.0)
    if probe_ok == 0:
        print("[abort] 东财限频冷却中(预检 3 只全失败), 稍后再跑", flush=True)
        return 1

    # 流通股本: 以既有缓存为底, pytdx 能拉到的覆盖更新(拉不到不清空)
    floats: dict[str, float] = {}
    if FLOAT_JSON.exists():
        try:
            floats = {k: float(v) for k, v in
                      json.loads(FLOAT_JSON.read_text()).items()}
        except Exception:
            floats = {}

    api = connect()
    frames, t0 = [], time.time()
    consec_fail = 0
    skip = 0
    try:
        for i, (m, code, name) in enumerate(uni):
            last = existing_last.get(code)
            if not args.force and last is not None and pd.Timestamp(last) >= as_of:
                have = have_sets.get(code) or set()
                if all(d in have for d in trade_idx):
                    skip += 1             # 最后日期已含今日 且 必须交易日无洞 → 跳过
                    continue
            try:
                d = fetch_one_em(code, args.start)
                consec_fail = 0
                if d is not None:
                    frames.append(d)
                try:
                    fi = api.get_finance_info(m, code)
                    lb = float((fi or {}).get("liutongguben") or 0)
                    if lb > 0:
                        floats[code] = lb
                except Exception:
                    pass
            except Exception as exc:
                consec_fail += 1
                if consec_fail >= 8:      # 疑似被限频 → 冷却后继续
                    print(f"  [限频] 连续 {consec_fail} 失败, 冷却 30s ...",
                          flush=True)
                    time.sleep(30)
                    consec_fail = 0
                print(f"  [warn] {code}: {exc}")
            time.sleep(0.5)               # 单客户端节流: 2req/s, 防限频
            if (i + 1) % 200 == 0:
                el = time.time() - t0
                eta = el / (i + 1) * (len(uni) - i - 1) / 60
                print(f"  ...{i+1}/{len(uni)} 成功 {len(frames)} | "
                      f"已用 {el/60:.1f}min 预计还剩 {eta:.1f}min", flush=True)
            if (i + 1) % 300 == 0:            # 定期落盘, 中断可续
                if frames:
                    pd.concat(frames, ignore_index=True).to_pickle(
                        out_pkl.with_suffix(".part.pkl"))
                FLOAT_JSON.with_suffix(f".{args.offset}.json").write_text(
                    json.dumps(floats), encoding="utf-8")
    finally:
        try:
            api.disconnect()
        except Exception:
            pass

    if not frames:
        print("\n[跳过] 本轮无新增成功, 不写盘(保留既有 pkl)")
    else:
        if existing_df is not None:        # 合并既有 + 本轮新增
            frames = [existing_df] + frames
        df = pd.concat(frames, ignore_index=True).drop_duplicates(
            ["code", "date"])
        df.to_pickle(out_pkl)
        print(f"\n[完成] 本轮新拉 {len(frames) - (1 if existing_df is not None else 0)}"
              f" 只(跳过已有 {skip} 只) → 合并后 {df['code'].nunique()} 只 | "
              f"{len(df):,} 行 | {df['date'].min().date()}~{df['date'].max().date()}")
    fj = FLOAT_JSON if not args.offset else FLOAT_JSON.with_suffix(
        f".{args.offset}.json")
    fj.write_text(json.dumps(floats), encoding="utf-8")
    print(f"  日K  → {out_pkl}")
    print(f"  流通股本 {len(floats)} 只 → {fj}")
    part = out_pkl.with_suffix(".part.pkl")
    if part.exists():
        part.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
