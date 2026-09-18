#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游资战法 · 卖出提醒 (v4 · 次日智能卖点推送)

★ 卖出纪律(用户定, 2026-09-17):
    次日: 高开>5% 竞价出, 平/低开 30分钟内出, 封板持有, 10:30 不封必走

状态机(简化):
  · 买入当日(T)        → HOLD_NEW    "今日建仓 → 明日按规则走"
  · 持有 1 交易日(T+1) → 按实时价+时间判定:
       封板(涨停)            → HOLD_SEAL  "持有不动"
       高开>5%               → SELL_AUCTION "竞价/开盘立即出(锁利)"
       平/低开(未封板)       → SELL_30MIN  "30分钟内(10:00前)出"
       10:00~10:30 未封板    → SELL_NOW    "盘中找高点出(10:30前)"
       ≥10:30 未封板         → SELL_1030   "10:30必走(清仓)"
  · 持有 ≥2 交易日       → OVERDUE     "持仓超1日, 立即清仓"

★ 推送策略(让用户不盯盘):
    · 每个卖点只推一次(按 (buy_date,code,阶段) 去重, 落盘 youzi_sell_alerts.json)
        - 开盘阶段(open): 高开>5%竞价出 / 平低开30分内出 / 盘中出 → 首次命中推一次
        - 强制阶段(force): 10:30不封必走 / 逾期 → 首次命中推一次
        - 持有阶段(hold): 封板持有 → 首次命中推一次(告知别卖)
    · 尾盘(≥14:50)那轮额外推一份「当日持仓全量汇总」, 方便盘后回看
    · 其余轮次静默(只写日志), 不刷屏

实时数据: pytdx 优先, 失效回退腾讯 qt(今开/昨收/现价/最高)。
涨停判定: 主板 10% / 创业板·科创板 20% (300/688/689 开头)。

用法:
  python3 scripts/youzi_sell.py             # 推卖点(触发才推)
  python3 scripts/youzi_sell.py --dry-run   # 只打印不推送
  crontab(建议, 覆盖竞价→10:30窗口 + 尾盘汇总):
    25,30,40,50 9 * * 1-5  .../youzi_sell.py >> /tmp/youzi_sell.log 2>&1
    0,10,20,30,35 10 * * 1-5 .../youzi_sell.py >> /tmp/youzi_sell.log 2>&1
    50 14 * * 1-5 .../youzi_sell.py >> /tmp/youzi_sell.log 2>&1
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
SELL_ALERTS = POSITIONS.parent / "youzi_sell_alerts.json"
MAX_HOLD = 5

# 状态
HOLD_NEW = "HOLD_NEW"
HOLD_SEAL = "HOLD_SEAL"
SELL_AUCTION = "SELL_AUCTION"
SELL_30MIN = "SELL_30MIN"
SELL_NOW = "SELL_NOW"
SELL_1030 = "SELL_1030"
OVERDUE = "OVERDUE"
DATA_SHORT = "DATA_SHORT"

STATUS_CN = {
    HOLD_NEW: "🟢今日建仓", HOLD_SEAL: "🟢封板持有",
    SELL_AUCTION: "🔴竞价出", SELL_30MIN: "🔴30分钟内出",
    SELL_NOW: "🔴盘中出", SELL_1030: "🔴10:30必走",
    OVERDUE: "🔴逾期未卖", DATA_SHORT: "⚪数据不足",
}
# 需要落盘成交 + 清仓的状态
SELL_SET = {SELL_AUCTION, SELL_30MIN, SELL_NOW, SELL_1030, OVERDUE}
# 推送阶段(同阶段只推一次)
STAGE = {
    SELL_AUCTION: "open", SELL_30MIN: "open", SELL_NOW: "open",
    SELL_1030: "force", OVERDUE: "force", HOLD_SEAL: "hold",
}


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


def load_alerts() -> dict:
    if not SELL_ALERTS.exists():
        return {}
    try:
        return json.loads(SELL_ALERTS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def fetch_quote(api, code: str) -> dict:
    """实时盘口: {open, prev_close, price, high}。pytdx 优先, 回退腾讯 qt。"""
    m = TDXParams.MARKET_SH if code[0] in "56" else TDXParams.MARKET_SZ
    pre = "sh" if code[0] in "569" else "sz"
    res = {"open": 0.0, "prev_close": 0.0, "price": 0.0, "high": 0.0}
    if api is not None:
        try:
            q = api.get_security_quotes([(m, code)])
            if q:
                o = q[0]
                res["open"] = float(o.get("open") or 0)
                res["prev_close"] = float(o.get("last_close") or 0)
                res["price"] = float(o.get("price") or 0)
                res["high"] = float(o.get("high") or 0)
                if res["price"]:
                    return res
        except Exception:
            pass
    # 腾讯 qt 兜底: qt[3]=现价, qt[4]=昨收, qt[5]=今开, qt[33]=最高
    try:
        import requests as _rq
        u = (f"https://web.ifzq.gtimg.cn/appstock/app/day/query"
             f"?code={pre}{code}")
        qt = (_rq.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
                     proxies={"http": None, "https": None})
              .json()["data"][f"{pre}{code}"].get("qt", {})
              .get(f"{pre}{code}") or [])
        if len(qt) > 34:
            res["price"] = float(qt[3] or 0)
            res["prev_close"] = float(qt[4] or 0)
            res["open"] = float(qt[5] or 0)
            res["high"] = float(qt[33] or 0)
    except Exception:
        pass
    return res


def limit_pct_of(code: str) -> float:
    """主板 10%, 创业板/科创板 20%。"""
    return 0.20 if code[:3] in ("300", "688", "689") else 0.10


def analyze(pos: dict, q: dict, now: datetime) -> dict:
    """次日智能卖点判定。q 为实时盘口(见 fetch_quote)。"""
    bd = pd.Timestamp(pos["buy_date"]).date()
    today = now.date()
    days_held = (today - bd).days
    code = pos["code"]
    lpct = limit_pct_of(code)
    open_ = q.get("open") or q.get("price") or 0.0   # 竞价阶段 open 可能为0 → 用现价近似
    prev = q.get("prev_close") or 0.0
    price = q.get("price") or 0.0
    high = q.get("high") or 0.0
    hm = now.hour * 60 + now.minute

    if days_held <= 0:
        return {**pos, "days_held": 0, "status": HOLD_NEW,
                "msg": "今日建仓 → 明日按规则走(高开>5%竞价出/平低开30分内出/"
                       "封板持有/10:30不封必走)"}

    # ── 开盘前(09:30 前)盘口未定: 等真实开盘价再判定, 不提前推卖点,
    #    否则 stale 价会误判"平/低开"并占用去重位, 09:30 真实高开时不再更正 ──
    if hm < 9 * 60 + 30:
        return {**pos, "days_held": days_held, "status": "PRE_OPEN",
                "msg": "开盘前, 等待 09:30 真实开盘价再判定卖点"}

    # ── 次日及以后 ──
    limit_price = round(prev * (1 + lpct), 2) if prev else 0.0
    sealed = price >= limit_price - 0.01 if limit_price else False
    gap = (open_ - prev) / prev if (open_ > 0 and prev > 0) else None

    if sealed:
        return {**pos, "days_held": days_held, "status": HOLD_SEAL,
                "msg": f"已封板(涨停{lpct*100:.0f}%), 持有不动"}
    if days_held >= 2:
        return {**pos, "days_held": days_held, "status": OVERDUE,
                "msg": "持仓超1交易日, 立即清仓"}
    if gap is not None and gap > 0.05:
        return {**pos, "days_held": days_held, "status": SELL_AUCTION,
                "msg": f"高开{gap*100:.1f}%>5%, 竞价/开盘立即出(锁利)"}
    # 平/低开 或 高开≤5% 且未封板 → 尽早出
    if hm < 10 * 60:
        return {**pos, "days_held": days_held, "status": SELL_30MIN,
                "msg": "平/低开, 30分钟内(10:00前)出"}
    if hm < 10 * 60 + 30:
        return {**pos, "days_held": days_held, "status": SELL_NOW,
                "msg": "未封板, 盘中找高点出(10:30前)"}
    return {**pos, "days_held": days_held, "status": SELL_1030,
            "msg": "10:30未封板, 必走(清仓)"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    _dry = getattr(args, "dry_run", False)
    now = datetime.now()

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
            q = fetch_quote(api, pos["code"])
            if q.get("price", 0) == 0 and q.get("prev_close", 0) == 0:
                rows.append({**pos, "status": DATA_SHORT,
                             "msg": "无行情", "price": 0.0})
                continue
            r = analyze(pos, q, now)
            r["price"] = q.get("price", 0.0)
            if r.get("entry"):
                try:
                    r["pnl"] = (r["price"] / float(r["entry"]) - 1) * 100
                except Exception:
                    r["pnl"] = None
            rows.append(r)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass

    # ── 卖出成交落盘(供网站实盘区展示) ──
    if not _dry:
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
                    if r.get("status") in SELL_SET and key not in have:
                        have.add(key)
                        f.write(json.dumps({
                            "code": r.get("code"), "name": r.get("name"),
                            "buy_date": str(r.get("buy_date", "")),
                            "buy_price": float(r.get("entry") or 0),
                            "sell_date": now.strftime("%Y-%m-%d"),
                            "sell_price": float(r.get("price") or 0),
                            "status": r.get("status"),
                            "msg": r.get("msg", ""),
                        }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ── 推送去重(每 (buy_date,code,阶段) 只推一次) ──
    alerts = load_alerts() if not _dry else {}
    pending = []          # 本次需要立即推送的卖点
    for r in rows:
        st = r.get("status")
        if st not in STAGE:
            continue
        key = f'{r.get("buy_date")}|{r.get("code")}'
        stage = STAGE[st]
        done = alerts.get(key, [])
        if stage not in done:
            pending.append(r)
            if not _dry:
                done = list(done)
                if stage not in done:
                    done.append(stage)
                alerts[key] = done
    if not _dry and pending:
        SELL_ALERTS.write_text(json.dumps(alerts, ensure_ascii=False),
                               encoding="utf-8")

    # ── 已卖出 → 持仓移除(仅尾盘 ≥14:50 那轮清仓) ──
    _settle = now.hour * 60 + now.minute >= 14 * 60 + 50
    try:
        raw = json.loads(POSITIONS.read_text(encoding="utf-8"))
        removed = []
        for r in rows:
            if r.get("status") not in SELL_SET:
                continue
            day, code = str(r.get("buy_date", "")), str(r.get("code", ""))
            if code in (raw.get(day) or {}):
                removed.append(f"{r.get('name', code)}({code})")
                if _settle and not _dry:
                    raw[day].pop(code)
                    if not raw[day]:
                        raw.pop(day, None)
        if removed:
            if _settle and not _dry:
                POSITIONS.write_text(json.dumps(raw, ensure_ascii=False),
                                     encoding="utf-8")
                print(f"[清仓] 已移出持仓: {', '.join(removed)}")
            else:
                print(f"[待清仓] 今日应走, 尾盘再提醒后清仓: "
                      f"{', '.join(removed)}")
    except Exception as exc:
        print(f"[warn] 持仓清理失败: {exc}")

    # ── 打印当日全量(日志) ──
    rows.sort(key=lambda r: 0 if r["status"] in SELL_SET else 1)
    title = f"游资持仓跟踪 {len(rows)} 笔 {now.strftime('%m-%d %H:%M')}"
    lines = [f"### 游资持仓跟踪 · {now.strftime('%Y-%m-%d %H:%M')}", ""]
    for r in rows:
        pnl = ""
        if r.get("pnl") is not None:
            pnl = f"　浮盈 {r['pnl']:+.2f}%"
        lines.append(
            f"- {STATUS_CN.get(r['status'], r['status'])} "
            f"**{r.get('name', r['code'])}({r['code']})**{pnl}\n"
            f"　买入 {r.get('buy_date', '?')} @ {r.get('entry', '?')}"
            f"　现价 {r.get('price', '?')}　持有 {r.get('days_held', '?')} 天\n"
            f"　**{r['msg']}**")
    lines += ["", "> 卖出纪律: 高开>5%竞价出 / 平低开30分内出 / 封板持有 / "
              "10:30不封必走。", "> 卖点命中即通过钉钉推送, 无需盯盘。"]
    text = "\n".join(lines)
    print("=" * 70)
    print(title)
    print(text)
    print("=" * 70)

    if _dry:
        print("\n[dry-run] 不推送")
        if pending:
            print(f"[dry-run] 本应推送卖点 {len(pending)} 笔:")
            for r in pending:
                print(f"  · {r.get('name')}({r.get('code')}) "
                      f"{STATUS_CN.get(r['status'])} — {r['msg']}")
        return 0

    webhook = (os.getenv("DINGTALK_YOUZI_WEBHOOK")
               or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    keyword = (os.getenv("DINGTALK_YOUZI_KEYWORD")
               or os.getenv("DINGTALK_KEYWORD") or "游资").strip()
    if not webhook:
        print("! 钉钉未配置")
        return 0

    # ① 触发式卖点推送(命中即推, 去重)
    if pending:
        at = f"游资卖点提醒 · {now.strftime('%m-%d %H:%M')}"
        al = [f"### 游资卖点提醒 · {now.strftime('%Y-%m-%d %H:%M')}", "",
              "> 以下持仓到达卖点, 请处理(脚本不代下单):", ""]
        for r in pending:
            pnl = f"　浮盈 {r['pnl']:+.2f}%" if r.get("pnl") is not None else ""
            al.append(
                f"- {STATUS_CN.get(r['status'], r['status'])} "
                f"**{r.get('name', r['code'])}({r['code']})**{pnl}\n"
                f"　买入 @ {r.get('entry', '?')}　现价 {r.get('price', '?')}\n"
                f"　**{r['msg']}**")
        ok = send_markdown(at, "\n".join(al), webhook=webhook, keyword=keyword)
        print(f"卖点推送: {'成功' if ok else '失败'} ({len(pending)} 笔)")

    # ② 尾盘全量汇总(盘后回看)
    if _settle:
        ok = send_markdown(title, text, webhook=webhook, keyword=keyword)
        print(f"尾盘汇总推送: {'成功' if ok else '失败'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
