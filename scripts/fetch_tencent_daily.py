"""用腾讯日K重建 mb_daily.pkl(全市场覆盖, 量比可用的估算成交额)。

背景(2026-09-16): 东财日K接口被限频, fetch_mainboard_daily 只拉到 51 只就
"完成", 把全市场 pkl 覆盖从 ~3000 砸到 51。但实时行情进程依赖 mb_daily 的
amount 列算量比(prev_amt) → 缺 amount 的票 vr=0 被量比过滤全砍, 重启会没信号。

腾讯 fqkline 不限频, 返回 [日期,开,收,高,低,成交量(手)], 缺成交额。本脚本:
  - 全市场主板代码(取自 mb_float.json, 3051 只)逐只拉腾讯日K → OHLC 全真实
  - amount 估算 = 成交量(手)×100 × 均价(开收高低的均值) → 同口径比值, 量比可用
  - 位置/减分(pct5/10/RSI/涨停天数/距60日高)只用 OHLC, 完全准确
  - 优先保留既有 EM pkl 中 51 只的真实 amount(若 amount 相近则覆盖估算)

跑完覆盖回到 ~3051, 重启实时进程即安全。后续可跑 fetch_mainboard_daily --force
把估算 amount 逐步升级为东财真实值(失败处保留估算)。
"""
import json
import os
import ssl
import time
import urllib.request
from pathlib import Path

import pandas as pd

STATE_DIR = Path.home() / ".tradingagents/youzi"
DAILY_PKL = STATE_DIR / "mb_daily.pkl"
FLOAT_JSON = STATE_DIR / "mb_float.json"

os.environ.update({"no_proxy": "*", "NO_PROXY": "*"})
ssl._create_default_https_context = ssl._create_unverified_context


def _prefix(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def fetch_one(code: str, n: int = 200) -> list | None:
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={_prefix(code)},day,,,{n},")
    for _ in range(3):
        try:
            d = json.loads(urllib.request.urlopen(
                url, timeout=8).read())
            node = d["data"][_prefix(code)]
            key = ("qfqday" if "qfqday" in node
                   else ("day" if "day" in node
                         else [k for k in node if k.endswith("day")][0]))
            return node[key]
        except Exception:
            time.sleep(0.3)
    return None


def main() -> int:
    if not FLOAT_JSON.exists():
        print("[fatal] mb_float.json 缺失, 无法获取代码宇宙")
        return 1
    codes = [k for k in json.loads(FLOAT_JSON.read_text()).keys()]
    print(f"[1/2] 主板代码 {len(codes)} 只 → 腾讯日K ...", flush=True)

    # 累加式: 载入既有 pkl 已完成标的, 本轮只补拉缺失的, 末尾合并写回
    # (腾讯也限频, 单轮只能拉到一部分, 整体覆盖会丢失此前进度 → 必须累加)
    existing_df = None
    existing_codes: set = set()
    real_amt = {}
    if DAILY_PKL.exists():
        try:
            existing_df = pd.read_pickle(DAILY_PKL)
            existing_codes = set(existing_df["code"].unique())
            real_amt = {c: g for c, g in existing_df.groupby("code")}
            print(f"[基线] 既有 pkl 含 {len(existing_codes)} 只, 本轮补拉缺失标的")
        except Exception:
            existing_df = None

    frames, t0 = [], time.time()
    skip = consec = 0
    for i, code in enumerate(codes):
        if code in existing_codes:      # 已完成 → 跳过, 保留既有(含真实 amount)
            skip += 1
            continue
        try:
            rows = fetch_one(code)
            if not rows:
                consec += 1
                if consec >= 10:        # 疑似限频 → 冷却后续拉
                    print(f"  [限频] 连续 {consec} 失败, 冷却 60s ...",
                          flush=True)
                    time.sleep(60)
                    consec = 0
                continue
            consec = 0
            out = []
            for r in rows:
                o, c, h, l, v = (float(r[1]), float(r[2]),
                                 float(r[3]), float(r[4]), float(r[5]))
                avg = (o + c + h + l) / 4.0
                amt_est = v * 100.0 * avg          # 手→股 × 均价 = 估算成交额(元)
                out.append({"date": r[0], "open": o, "close": c,
                            "high": h, "low": l, "amount": amt_est})
            if len(out) < 60:
                continue
            df = pd.DataFrame(out)
            # 优先用真实 amount: 取最后一根相同日期则覆盖
            if code in real_amt:
                rg = real_amt[code]
                rlast = rg.iloc[-1]["date"]
                if str(rlast) == str(out[-1]["date"]):
                    df.loc[df.index[-1], "amount"] = float(rg.iloc[-1]["amount"])
            df.insert(0, "code", code)
            frames.append(df)
        except Exception:
            consec += 1
            if consec >= 10:
                print(f"  [限频] 连续 {consec} 失败, 冷却 60s ...", flush=True)
                time.sleep(60)
                consec = 0
        if (i + 1) % 300 == 0:
            el = time.time() - t0
            print(f"  ...{i+1}/{len(codes)} 本轮新拉 {len(frames)} "
                  f"(跳过已有 {skip}) | 已用 {el/60:.1f}min", flush=True)
        time.sleep(0.015)
    if not frames:
        print("\n[跳过] 本轮无新增成功, 不写盘(保留既有 pkl)")
        return 0
    if existing_df is not None:        # 合并既有 + 本轮新增
        frames = [existing_df] + frames
    df = pd.concat(frames, ignore_index=True)
    df = df[(df["close"] > 0) & (df["high"] > 0)].drop_duplicates(["code", "date"])
    df.to_pickle(DAILY_PKL)
    print(f"\n[完成] 本轮新拉 {len(frames) - (1 if existing_df is not None else 0)}"
          f" 只(跳过已有 {skip}) → 合并后 {df['code'].nunique()} 只 | "
          f"{len(df):,} 行 | {df['date'].min()} ~ {df['date'].max()}")
    print(f"  日K → {DAILY_PKL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
