#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游资秒级盘口哨兵 —— 纯代码规则, 零 LLM 成本, 秒级轮询。

为什么存在: LLM 判定层最快 1 分钟一轮(成本约束), 但游资真正争分夺秒的是
**封单衰减/炸板瞬间** —— 这是盘口博弈, 纯代码规则就能盯, 秒级轮询零成本。
事件驱动: 平时只盯盘口(免费), 事件发生立即推钉钉提醒人工(不代下单)。

监控事件(仅持仓票):
  A. 封单快速衰减: 封死涨停中, 买一封单额在 SEAL_DECAY_WIN 秒内衰减 >SEAL_DECAY_PCT
     → 游资此刻的第一反应就是先减仓(封单被砸的速度=抛压真相)
  B. 炸板: 曾封死 → 现价跌破涨停价 BREAK_PCT% → 立即预警
  C. 摸板回落: 触及涨停但未封死, 自日内高点回落 >FADE_PCT% → 预警

数据: 腾讯快照(tx_quote.snapshot, ~0.1s/批, 有 bid1_vol/ask1_vol)。
  不用 pytdx: 买侧 youzi_live 常驻占用 TDX 服务器会话, 第二连接 get_security_quotes 返回空。
单实例: flock /tmp/youzi_fast_watch.lock。
拉起: cron `*/2 9-14 * * 1-5 .../youzi_fast_watch.py`(进程不存在才启动, 常驻)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, time as dtime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:
    pass

from tx_quote import snapshot as tx_snapshot          # noqa: E402
import youzi_sell_ai as SA                            # noqa: E402  (交易日口径复用)

STATE_DIR = Path.home() / ".tradingagents" / "youzi"
POSITIONS = STATE_DIR / "positions.json"
CAND_FILE = STATE_DIR / "candidates.json"     # 买侧落盘的当轮候选(哨兵顺带监控)
BOOK_FILE = STATE_DIR / "fast_book.json"      # 秒级盘口趋势摘要 → 买侧 AI 证据
LOG_DIR = STATE_DIR / "logs"
FAST_LOG = LOG_DIR / "fast_watch.jsonl"

POLL_SEC = 3                # 轮询间隔(腾讯快照实测 0.1s/批, 3s 足够)
SEAL_DECAY_PCT = 40         # 封单额 60s 内衰减超过此百分比 → 预警
SEAL_DECAY_WIN = 60         # 秒
BREAK_PCT = 0.3             # 跌破涨停价 0.3% → 炸板
FADE_PCT = 1.5              # 摸板后自日内高点回落超此百分比(未封死) → 摸板回落
EVENT_COOLDOWN = 600        # 同票同事件冷却(秒)
LOCK_PATH = "/tmp/youzi_fast_watch.lock"


def limit_price_of(prev_close: float, code: str) -> float:
    lp = 0.20 if code[:3] in ("300", "688", "689") else 0.10
    return round(prev_close * (1 + lp), 2)


def _load_positions() -> dict:
    """{code: {name, buy_date, entry}}"""
    try:
        raw = json.loads(POSITIONS.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for day, holds in (raw or {}).items():
        for code, meta in (holds or {}).items():
            out[str(code)] = {"name": meta.get("name", code),
                              "buy_date": str(day),
                              "entry": float(meta.get("entry") or 0)}
    return out


def _acquire_lock() -> bool:
    global _LOCK_FP
    try:
        import fcntl
        _LOCK_FP = open(LOCK_PATH, "w")
        fcntl.flock(_LOCK_FP, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except Exception:
        return False


_LOCK_FP = None
_state: dict = {}      # code -> {"sealed_today":bool, "hist":deque[(ts,amt)], "ev":{event:ts}}


def _log_event(rec: dict) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(FAST_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


_AI_LAST_TS = 0.0        # 上次触发卖出 AI 的时刻(全局冷却, AI 本身有 flock 兜底)


def _trigger_ai(reason: str, now: datetime) -> bool:
    """事件 → 立即触发一次卖出 AI 评估(绕过 1 分钟 cron 等待)。

    用户定调: 事件不推给人, 交给 AI 判断 —— 哨兵只负责"发现得快",
    要不要卖由 AI 的 sell_score/连板保护/时间纪律决定, AI 结论才推送。
    卖出 AI 自带 flock 单实例锁, 重复触发会被锁跳过, 这里再加全局冷却省进程。
    """
    global _AI_LAST_TS
    if time.time() - _AI_LAST_TS < 120:
        return False
    _AI_LAST_TS = time.time()
    try:
        subprocess.Popen(
            ["/usr/local/bin/python3", str(_HERE / "youzi_sell_ai.py")],
            stdout=open("/tmp/youzi_sell_ai.log", "a"),
            stderr=subprocess.STDOUT,
            cwd=str(_ROOT))
        print("  [事件→AI] %s 已触发卖出AI评估(事件: %s)" % (now.strftime("%H:%M:%S"), reason),
              flush=True)
        _log_event({"t": now.strftime("%F %T"), "event": "trigger_ai",
                    "detail": [reason]})
        return True
    except Exception as exc:
        print("[warn] 触发AI失败: %s" % str(exc)[:80], flush=True)
        return False


def _handle_event(code: str, name: str, event: str, lines: list,
                  now: datetime, buy_date: str) -> None:
    """事件冷却 + 落盘 + 分流: T+1 持仓 → 触发 AI 判定; T+0 → 仅记录(当日不可卖)。"""
    st = _state.setdefault(code, {})
    ev = st.setdefault("ev", {})
    if time.time() - ev.get(event, 0) < EVENT_COOLDOWN:
        return
    ev[event] = time.time()
    _log_event({"t": now.strftime("%F %T"), "code": code, "name": name,
                "event": event, "detail": lines})
    tds = SA.trading_days_held(buy_date, now.strftime("%Y-%m-%d"))
    if tds <= 0:
        print("  [%s] %s %s (T+0 当日不可卖 → 仅记录, 明日此事件即卖点信号)"
              % (event, code, name), flush=True)
        return
    print("  [%s] %s %s (T+%d 持仓 → 交给 AI 判定)" % (event, code, name, tds),
          flush=True)
    _trigger_ai("%s %s(%s)" % (event, name, code), now)


def _fmt_amt(v: float) -> str:
    return "%.2f亿" % (v / 1e8) if v >= 1e8 else "%.0f万" % (v / 1e4)


def watch_once(now: datetime) -> None:
    pos = _load_positions()
    # 候选票(买侧当轮 fresh, 120s 内有效)也纳入秒级监控 → 产出趋势摘要给 AI
    cand: list = []
    try:
        cj = json.loads(CAND_FILE.read_text(encoding="utf-8"))
        if time.time() - float(cj.get("ts") or 0) < 120:
            cand = [str(c) for c in (cj.get("codes") or [])]
    except Exception:
        pass
    codes = list(dict.fromkeys(list(pos) + cand))[:40]
    if not codes:
        return
    try:
        quotes = tx_snapshot(codes) or {}
    except Exception as exc:
        print("[warn] 快照失败: %s" % str(exc)[:80], flush=True)
        return

    book = {}                      # 秒级趋势摘要(候选+持仓) → fast_book.json
    for code in codes:
        q = quotes.get(code) or {}
        price = float(q.get("price") or 0)
        prev = float(q.get("prev_close") or 0)
        high = float(q.get("high") or 0)
        b1v = float(q.get("bid1_vol") or 0)
        a1v = float(q.get("ask1_vol") or 0)
        if price <= 0 or prev <= 0:
            continue
        lim = limit_price_of(prev, code)
        sealed_now = price >= lim - 0.01
        st = _state.setdefault(code, {})
        st.setdefault("ev", {})
        h = st.setdefault("hist", deque())          # (t, price, ask1, bid1, touched?)
        touched = high >= lim - 0.01
        if h and not h[-1][4] and touched:
            st["touches"] = int(st.get("touches") or 0) + 1   # 触板次数(去重计数)
        h.append((time.time(), price, a1v, b1v, touched))
        while h and time.time() - h[0][0] > 90:
            h.popleft()

        # ── 趋势摘要: 30秒动量 / 卖一量60秒变化(卖压消化) / 触板次数 ──
        summ: dict = {"sealed": sealed_now, "touches": int(st.get("touches") or 0)}
        if len(h) >= 4:
            base = next((p for t, p, *_ in reversed(h) if time.time() - t >= 30),
                        h[0][1])
            summ["mom30"] = round((price / base - 1) * 100, 2) if base > 0 else None
            a60 = next((a for t, _, a, *_ in reversed(h) if time.time() - t >= 60),
                       h[0][2])
            if a60 and a60 > 0:
                summ["ask_chg60"] = round((a1v - a60) / a60 * 100, 1)
            if sealed_now:
                summ["bid1_amt"] = b1v * 100.0 * price
        book[code] = summ

        meta = pos.get(code)
        if not meta:
            continue
        entry = meta.get("entry") or 0
        pnl = (price / entry - 1) * 100 if entry > 0 else None
        pnl_txt = " 浮盈%+.1f%%" % pnl if pnl is not None else ""

        if sealed_now:
            st["sealed_today"] = True
            amt = b1v * 100.0 * price            # 买一封单额(手→股×价)
            seal = st.setdefault("seal_hist", deque())
            seal.append((time.time(), amt))
            while seal and time.time() - seal[0][0] > SEAL_DECAY_WIN:
                seal.popleft()
            st["peak"] = max(st.get("peak") or 0.0, amt)
            if len(seal) >= 6:                    # ~18s 数据才开始判, 避免抖动
                peak_win = max(a for _, a in seal)
                now_amt = seal[-1][1]
                if peak_win > 1e6 and (peak_win - now_amt) / peak_win * 100 >= SEAL_DECAY_PCT:
                    _handle_event(code, meta["name"], "seal_decay",
                                  ["封单额 %s → %s(%d秒内 -%.0f%%)"
                                   % (_fmt_amt(peak_win), _fmt_amt(now_amt),
                                      int(time.time() - seal[0][0]),
                                      (peak_win - now_amt) / peak_win * 100),
                                   "现价 %.2f%s | 涨停价 %.2f" % (price, pnl_txt, lim),
                                   "游资应对: 封单被砸速度=抛压真相, 此刻通常先减仓, 别等炸板"],
                                  now, meta["buy_date"])
            continue

        # 未封死: 若今天曾封死 → 炸板判定
        if st.get("sealed_today") and price < lim * (1 - BREAK_PCT / 100):
            _handle_event(code, meta["name"], "break",
                          ["曾封死涨停, 现价 %.2f 已跌回涨停价下方 %.1f%%"
                           % (price, (1 - price / lim) * 100),
                           "%s | 涨停价 %.2f" % (pnl_txt, lim),
                           "游资应对: 炸板不回封+放量=派发, 半路板战法此刻不留"],
                          now, meta["buy_date"])
        # 摸板回落(今天触及涨停价但没封住)
        if high >= lim - 0.01 and not sealed_now:
            fade = (high - price) / high * 100 if high > 0 else 0
            if fade >= FADE_PCT:
                _handle_event(code, meta["name"], "fade",
                              ["日内最高 %.2f(触涨停) → 现价 %.2f, 回落 %.1f%%"
                               % (high, price, fade),
                               "%s | 涨停价 %.2f" % (pnl_txt, lim)],
                              now, meta["buy_date"])
    # 落盘秒级趋势摘要 → 买侧 AI 评估时注入(变化率证据, 非单点快照)
    try:
        BOOK_FILE.write_text(json.dumps(
            {"ts": time.time(), "data": book}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def in_session(now: datetime) -> bool:
    t = now.time()
    return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(14, 57))


def main() -> int:
    now = datetime.now()
    if not in_session(now):
        print("[退出] 非交易时段 %s" % now.strftime("%H:%M"), flush=True)
        return 0
    if not _acquire_lock():
        print("[锁] 已有哨兵实例", flush=True)
        return 0
    print("───── 游资盘口哨兵启动 %s (每%ds轮询持仓盘口, 纯代码零LLM) ─────"
          % (now.strftime("%F %T"), POLL_SEC), flush=True)
    while True:
        now = datetime.now()
        if not in_session(now):
            print("[退出] 收盘", flush=True)
            return 0
        try:
            watch_once(now)
        except Exception as exc:
            print("[warn] %s: %s" % (type(exc).__name__, str(exc)[:100]),
                  flush=True)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
