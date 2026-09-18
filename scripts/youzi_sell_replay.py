#!/usr/bin/env python3
"""回放 sell_judgements.jsonl, 量化 SELL_THR 抬升对"卖飞"的影响。

设计: journal 每次扫描都记录了 AI 给的 sell_score + action, 真实卖出触发条件为
      action=="SELL" 且 sell_score >= SELL_THR (youzi_sell_ai.py:629)。
      因此回放只需对同一条 journal 重放不同阈值判定, 不需要再调模型。

用法:
  python3 scripts/youzi_sell_replay.py            # 一级: 纯 journal 阈值重放(快, 免费)
  python3 scripts/youzi_sell_replay.py --price    # 二级: 对被挽救笔拉历史1分算"多留利润"
"""
import os
import json
import sys
from pathlib import Path
import pandas as pd

JUDGE = Path.home() / ".tradingagents" / "youzi" / "logs" / "sell_judgements.jsonl"
THRS = [60, 70, 75]
BASE = 60  # 当前运行基线(与 crontab YOUZU_SELL_THR=60 一致)


def load():
    out = []
    if not JUDGE.exists():
        print("! 找不到 %s" % JUDGE)
        return out
    for ln in JUDGE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def ptime(r):
    try:
        return pd.Timestamp(r["date"] + " " + r["time"])
    except Exception:
        return pd.Timestamp.min


def first_trigger(group, thr):
    """该 (date,code) 当日首次满足卖出判定的扫描记录。"""
    for r in group:
        if r.get("action") == "SELL" and float(r.get("sell_score", 0) or 0) >= thr:
            return r
    return None


# ---------------- 二级取价(复制自 youzi_sell_ai.fetch_hist_min_1, 加内存缓存) ----------------
_hist_cache = {}


def fetch_hist_min_1(code, ds):
    if (code, ds) in _hist_cache:
        return _hist_cache[(code, ds)]
    saved = {k: os.environ.pop(k) for k in list(os.environ) if "proxy" in k.lower()}
    pre = "sh" if code[0] in "569" else "sz"
    sym = pre + code
    df = None
    try:
        import akshare as ak
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"
        d = ak.stock_zh_a_minute(symbol=sym, period="1", adjust="")
        if d is not None and len(d):
            d["datetime"] = pd.to_datetime(d["day"])
            d = d[d["datetime"].dt.strftime("%Y-%m-%d") == ds]
            if len(d):
                d = d.rename(columns={"open": "open", "high": "high", "low": "low",
                                      "close": "close", "volume": "vol", "amount": "amt"})
                d = d.reset_index(drop=True)
                for c in ("open", "high", "low", "close", "vol", "amt"):
                    d[c] = pd.to_numeric(d[c], errors="coerce")
                d["date"] = ds
                df = d[["datetime", "date", "open", "high", "low", "close", "vol", "amt"]]
    except Exception as exc:
        print("    [warn] %s %s 1分拉取失败: %s" % (code, ds, exc))
    finally:
        os.environ.update(saved)
    _hist_cache[(code, ds)] = df
    return df


def classify(r):
    """被挽救笔若属'出货/破位'则抬阈值有害; 属'洗盘/高位强势'则抬阈值有用。"""
    verdict = str(r.get("verdict", ""))
    shape = str(r.get("shape", ""))
    if verdict == "出货" or "破位" in shape or "新低" in shape or "持续新低" in shape:
        return "weak"
    if "洗盘" in verdict or "高位" in shape or "强势" in shape or "涨停" in shape or "封" in shape:
        return "strong"
    return "ambiguous"


def main():
    with_price = "--price" in sys.argv[1:]
    recs = load()
    if not recs:
        return
    modes = {}
    for r in recs:
        modes[r.get("mode", "?")] = modes.get(r.get("mode", "?"), 0) + 1
    print("journal 记录总数: %d (区间 %s ~ %s)" % (
        len(recs), recs[0]["date"], recs[-1]["date"]))
    print("  mode 分布: %s" % modes)

    groups = {}
    for r in recs:
        groups.setdefault((r.get("date"), r.get("code")), []).append(r)
    for k in groups:
        groups[k].sort(key=ptime)

    trig = {thr: {} for thr in THRS}
    for k, g in groups.items():
        for thr in THRS:
            t = first_trigger(g, thr)
            if t:
                trig[thr][k] = t

    print("\n=== 一、各阈值档卖出笔数 (首次 action==SELL 且 sell_score>=thr) ===")
    for thr in THRS:
        print("  SELL_THR=%-3d : %d 笔" % (thr, len(trig[thr])))

    saved70 = set(trig[BASE]) - set(trig[70])
    saved75 = set(trig[BASE]) - set(trig[75])

    print("\n=== 二、抬到 70 被挽救的笔 (基线60卖出但70不卖): %d 笔 ===" % len(saved70))
    if saved70:
        weak = strong = amb = 0
        for k in sorted(saved70):
            r = trig[BASE][k]
            c = classify(r)
            weak += c == "weak"
            strong += c == "strong"
            amb += c == "ambiguous"
            print("  %s %s %s score=%.0f cur=%+.2f%% fh=%+.2f%% [%s|%s] %s" % (
                r["date"], r["code"], r.get("name", ""), r["sell_score"],
                r["cur_pct"], r["from_high"], r["shape"], r["verdict"],
                r.get("reason", "")[:40]))
        print("  -> 误杀(洗盘/高位强势,抬阈值有用): %d | 真该卖(出货/破位,抬阈值有害): %d | 模糊: %d"
              % (strong, weak, amb))
    else:
        print("  (无)")

    print("\n=== 三、抬到 75 被挽救的笔: %d 笔 ===" % len(saved75))
    if saved75:
        weak = strong = amb = 0
        for k in sorted(saved75):
            r = trig[BASE][k]
            c = classify(r)
            weak += c == "weak"
            strong += c == "strong"
            amb += c == "ambiguous"
            print("  %s %s %s score=%.0f cur=%+.2f%% fh=%+.2f%% [%s|%s]" % (
                r["date"], r["code"], r.get("name", ""), r["sell_score"],
                r["cur_pct"], r["from_high"], r["shape"], r["verdict"]))
        print("  -> 误杀: %d | 真该卖: %d | 模糊: %d" % (strong, weak, amb))
    else:
        print("  (无)")

    if with_price and saved70:
        print("\n=== 四、二级: 被挽救笔若未卖、持有到当日收盘的 pnl 改善 ===")
        gains = []
        for k in sorted(saved70):
            r = trig[BASE][k]
            code, ds = r["code"], r["date"]
            tsell = ptime(r)
            df = fetch_hist_min_1(code, ds)
            if df is None or not len(df):
                print("  %s %s 无1分数据, 跳过" % (ds, code))
                continue
            sub = df[df["datetime"] <= tsell]
            if not len(sub):
                continue
            price_sell = float(sub.iloc[-1]["close"])
            after = df[df["datetime"] > tsell]
            if not len(after):
                print("  %s %s 卖出时刻后无数据" % (ds, code))
                continue
            last = float(after.iloc[-1]["close"])
            hi = float(after["high"].max())
            pnl_close = (last / price_sell - 1) * 100
            pnl_hi = (hi / price_sell - 1) * 100
            gains.append(pnl_close)
            print("  %s %s 卖@%.3f 持到收盘%+.2f%% 后续最高%+.2f%%" % (
                ds, code, price_sell, pnl_close, pnl_hi))
        if gains:
            print("  -> 被挽救笔平均'持有到收盘'改善: %+.2f%%/笔 (正=抬阈值多留利润, 有用)"
                  % (sum(gains) / len(gains)))
            print("  -> 其中正收益(确实多留) %d/%d 笔" % (
                sum(1 for g in gains if g > 0), len(gains)))


if __name__ == "__main__":
    main()
