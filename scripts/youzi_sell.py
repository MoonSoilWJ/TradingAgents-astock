#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游资战法 · 卖出提醒 (v3 · 次日开盘卖)

★ 卖出纪律(2026-09-11 定): 【买入次日开盘卖】
  与 AI 层回测口径严格一致 —— backtest_ai_rolling.py:
    ret = nxt_open / close - 1 - COST    (docstring: 开盘卖 +0.28% > 收盘卖 +0.04%)

【为什么废弃 v2 的"封板持有·断板卖"】
  v2 依据的回测(B 方案 笔均+1.90% vs A 次日收盘+1.49%)是【规则层无差别买入】
  口径, 从未与 AI 选股层叠加验证过。本策略的 edge 全部来自 AI 选股
  (回测中把封板率从 44% 提到 82%), 卖出口径必须与其验证口径一致, 否则实盘
  跑的是一个没被回测过的组合。封板信息仍计算, 但只作展示参考, 不作持有依据。

状态机(简化):
  · 买入当日(T)     → HOLD       "今日建仓 → 明日开盘走"
  · 持有 ≥1 交易日   → SELL_TODAY "纪律: 次日开盘走"
  · 无行情数据       → DATA_SHORT

用法:
  python3 scripts/youzi_sell.py             # 推持仓状态
  python3 scripts/youzi_sell.py --dry-run   # 只打印
  crontab: 31 9 * * 1-5 与 50 14 * * 1-5 各一次
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:
    pass

import pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

from tradingagents.notify.dingtalk import send_markdown

TDX_HOST, TDX_PORT = "180.153.18.170", 7709
POSITIONS = Path.home() / ".tradingagents" / "youzi" / "positions.json"
MAX_HOLD = 5
SEALED = 0.098
STATUS_ORDER = {"OVERDUE": 0, "SELL_TODAY": 1, "BREAK_NOW": 2,
                "FORCE": 3, "HOLD": 4, "DATA_SHORT": 5}
STATUS_CN = {"OVERDUE": "🔴逾期未卖", "SELL_TODAY": "🔴今日必走",
             "BREAK_NOW": "🟠盘中炸板", "FORCE": "🟠到期清仓",
             "HOLD": "🟢今日建仓", "DATA_SHORT": "⚪数据不足"}


def load_positions() -> list[dict]:
    if not POSITIONS.exists():
        return []
    try:
        raw = json.loads(POSITIONS.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for day, holds in raw.items():
        for code, meta in holds.items():
            out.append({"buy_date": day, "code": code, **meta})
    return out


def fetch_daily_tx(code: str, n: int = 15):
    """新浪日K兜底(盘中不含当日根, 收盘后更新) — TDX 2026-09-10 起失效。"""
    sym = ("sh" if code[0] in "569" else "sz") + code
    try:
        import requests
        u = (f"https://quotes.sina.cn/cn/api/json_v2.php/"
             f"CN_MarketDataService.getKLineData?symbol={sym}"
             f"&scale=240&ma=no&datalen={n}")
        r = requests.get(u, headers={"User-Agent": "Mozilla/5.0",
                                     "Referer": "https://finance.sina.com.cn"},
                         timeout=10, proxies={"http": None, "https": None})
        d = r.json()
        if not isinstance(d, list) or not d:
            return None
        df = pd.DataFrame(d).rename(columns={"day": "date"})
        for c in ("open", "high", "low", "close"):
            df[c] = df[c].astype(float)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df[["date", "open", "close", "high", "low"]].sort_values(
            "date").reset_index(drop=True)
    except Exception:
        return None


def fetch_daily(api, code: str, n: int = 15):
    """日线: pytdx 优先, 失效自动回退腾讯。"""
    if api is not None:
        try:
            m = TDXParams.MARKET_SH if code[0] in "56" else TDXParams.MARKET_SZ
            bars = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, m,
                                         code.encode(), 0, n)
            if bars:
                d = api.to_df(bars)
                d["date"] = pd.to_datetime(d["datetime"]).dt.date
                return d.sort_values("date").reset_index(drop=True)
        except Exception:
            pass
    return fetch_daily_tx(code, n)


def analyze(pos: dict, daily: pd.DataFrame, price: float = 0,
            high: float = 0) -> dict:
    """单笔持仓状态。daily 需含 date/dclose/prev 列。

    纪律 = 买入【次日开盘卖】(v3, 与 AI 层回测口径一致)。
    封板情况仍计算, 但只进 msg 作参考展示, 不影响状态判定。
    """
    bd = pd.Timestamp(pos["buy_date"]).date()
    s = daily[daily["date"] >= bd].reset_index(drop=True)
    if len(s) < 1:
        return {**pos, "status": "DATA_SHORT", "msg": "数据不足"}
    days_held = max(len(s) - 1, 0)
    tag = ""                                  # 买入当日封板情况(仅参考)
    try:
        if s["prev"].iloc[0]:
            sealed = (float(s["dclose"].iloc[0]) / float(s["prev"].iloc[0]) - 1
                      >= SEALED)
            tag = "封板" if sealed else "未封板"
    except Exception:
        tag = ""
    if days_held >= 1:
        return {**pos, "days_held": days_held, "status": "SELL_TODAY",
                "msg": "纪律: 次日开盘走(与回测口径一致)"}
    return {**pos, "days_held": 0, "status": "HOLD",
            "msg": (f"买入当日{tag} → 明日开盘走" if tag
                    else "买入当日 → 明日开盘走")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    holds = load_positions()
    if not holds:
        print("[跳过] 无持仓记录")
        return 0

    api = None
    try:
        api = TdxHq_API()
        if not api.connect(TDX_HOST, TDX_PORT, time_out=5):
            api = None
    except Exception:
        api = None
    if api is None:
        print("! pytdx 不可用, 行情回退腾讯源")
    rows = []
    try:
        for pos in holds:
            d = fetch_daily(api, pos["code"])
            if d is None:
                rows.append({**pos, "status": "DATA_SHORT", "msg": "无行情"})
                continue
            # ── 实时价: pytdx 失效 → 腾讯 day/query(qt) 兜底 ──
            price = high = 0.0
            try:
                m = (TDXParams.MARKET_SH if pos["code"][0] in "56"
                     else TDXParams.MARKET_SZ)
                q = api.get_security_quotes([(m, pos["code"])]) if api else None
                if q:
                    price = float(q[0].get("price") or 0)
                    high = float(q[0].get("high") or 0)
            except Exception:
                pass
            if price == 0:                 # 腾讯 qt 实时兜底
                try:
                    import requests as _rq
                    pre = ("sh" if pos["code"][0] in "569" else "sz")
                    u = (f"https://web.ifzq.gtimg.cn/appstock/app/day/query"
                         f"?code={pre}{pos['code']}")
                    qt = (_rq.get(u, headers={"User-Agent": "Mozilla/5.0"},
                                  timeout=10,
                                  proxies={"http": None, "https": None})
                          .json()["data"][f"{pre}{pos['code']}"].get("qt", {})
                          .get(f"{pre}{pos['code']}") or [])
                    price = float(qt[3] or 0)
                    high = float(qt[33] or 0) if len(qt) > 33 else 0.0
                except Exception:
                    pass
            # ── 盘中日K缺当日根(新浪收盘后才更新) → 用实时价补临时根 ──
            if d is not None and price > 0:
                today = datetime.now().date()
                if not len(d[d["date"] >= today]):
                    d = pd.concat([d, pd.DataFrame([{
                        "date": today, "open": price, "close": price,
                        "high": max(high, price), "low": price}])],
                        ignore_index=True)
            d["prev"] = d["close"].shift(1)
            d = d.rename(columns={"close": "dclose"}).dropna(subset=["prev"])
            rows.append({**analyze(pos, d, price, high), "price": price})
    finally:
        try:
            api.disconnect()
        except Exception:
            pass

    # ── 卖出成交落盘(供网站实盘区展示, 不再等 T+1 回填) ──
    # 注意: dry-run 不写(测试价会污染真实成交); 同一(code,buy_date)只写一次
    if not getattr(args, "dry_run", False):
        try:
            sf = POSITIONS.parent / "youzi_sold.jsonl"
            have = set()
            if sf.exists():
                for line in sf.read_text(encoding="utf-8").splitlines():
                    try:
                        o = json.loads(line)
                        have.add((str(o.get("buy_date")), str(o.get("code"))))
                    except Exception:
                        pass
            with open(sf, "a", encoding="utf-8") as f:
                for r in rows:
                    key = (str(r.get("buy_date")), str(r.get("code")))
                    if (r.get("status") in ("SELL_TODAY", "BREAK_NOW",
                                            "FORCE", "OVERDUE")
                            and key not in have):
                        have.add(key)
                        f.write(json.dumps({
                            "code": r.get("code"), "name": r.get("name"),
                            "buy_date": str(r.get("buy_date", "")),
                            # buy_price 必须落盘: 清仓后该笔会从 positions.json
                            # 移除, 网站再算收益就拿不到买入价 → 会一直显示
                            # "持仓中"。卖出记录自带买入价, 不依赖持仓文件。
                            "buy_price": float(r.get("entry") or 0),
                            "sell_date": datetime.now().strftime("%Y-%m-%d"),
                            "sell_price": float(r.get("price") or 0),
                            "status": r.get("status"),
                            "msg": r.get("msg", ""),
                        }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ── 已卖出 → 从持仓移除(否则每天重复提醒"逾期未卖") ──
    # 只在【尾盘 ≥14:50】那轮清仓: 09:31 那轮只提醒不移除 —— 否则"提示开盘卖"
    # 之后持仓立刻消失, 用户若没及时卖就再也没有第二次提醒(尾盘兜底)。
    _dry = getattr(args, "dry_run", False)
    _now = datetime.now()
    _settle = _now.hour * 60 + _now.minute >= 14 * 60 + 50
    try:
        raw = json.loads(POSITIONS.read_text(encoding="utf-8"))
        removed = []
        for r in rows:
            if r.get("status") not in ("SELL_TODAY", "BREAK_NOW", "FORCE",
                                       "OVERDUE"):
                continue
            day, code = str(r.get("buy_date", "")), str(r.get("code", ""))
            if code in (raw.get(day) or {}):
                removed.append(f"{r.get('name', code)}({code})")
                if _settle:
                    raw[day].pop(code)
                    if not raw[day]:
                        raw.pop(day, None)
        if removed:
            if _dry:
                print(f"[dry-run] 将移出持仓: {', '.join(removed)}")
            elif _settle:
                POSITIONS.write_text(json.dumps(raw, ensure_ascii=False),
                                     encoding="utf-8")
                print(f"[清仓] 已移出持仓: {', '.join(removed)}")
            else:
                print(f"[待清仓] 今日应走, 尾盘再提醒一次后清仓: "
                      f"{', '.join(removed)}")
    except Exception as exc:
        print(f"[warn] 持仓清理失败: {exc}")

    rows.sort(key=lambda r: STATUS_ORDER.get(r["status"], 9))
    now = datetime.now()
    title = f"游资持仓跟踪 {len(rows)} 笔 {now.strftime('%m-%d %H:%M')}"
    lines = [f"### 游资持仓跟踪 · {now.strftime('%Y-%m-%d %H:%M')}", ""]
    for r in rows:
        pnl = ""
        try:
            if r.get("entry"):
                px = [x for x in rows if x["code"] == r["code"]]
                pnl = f"　浮盈参考见行情"
        except Exception:
            pass
        lines.append(
            f"- {STATUS_CN.get(r['status'], r['status'])} "
            f"**{r.get('name', r['code'])}({r['code']})**\n"
            f"　买入 {r.get('buy_date', '?')} @ {r.get('entry', '?')}"
            f"　持有 {r.get('days_held', '?')} 天\n"
            f"　**{r['msg']}**")
    lines += ["", "> 卖出纪律: 买入【次日开盘卖】(与 AI 层回测口径一致)。",
              "> 封板情况仅作参考, 不作为持有依据。"]
    text = "\n".join(lines)
    print("=" * 70)
    print(title)
    print(text)
    print("=" * 70)
    if args.dry_run:
        print("\n[dry-run] 不推送")
        return 0
    webhook = (os.getenv("DINGTALK_YOUZI_WEBHOOK")
               or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    keyword = (os.getenv("DINGTALK_YOUZI_KEYWORD")
               or os.getenv("DINGTALK_KEYWORD") or "游资").strip()
    if not webhook:
        print("! 钉钉未配置")
        return 0
    ok = send_markdown(title, text, webhook=webhook, keyword=keyword)
    print(f"推送: {'成功' if ok else '失败'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
