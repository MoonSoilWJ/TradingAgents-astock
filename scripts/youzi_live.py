#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游资战法 · 实时「半路板」扫描 → 钉钉推送  (独立于科创50体系, 全新策略)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么不推"已涨停"的票
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  封死的板 → ask1=0(无卖单) → 散户排队买不到; 能成交的多是即将炸板的烂板
  (逆向选择)。scripts/daban_backtest.py 已量化: 盈亏平衡只隔着 1.37%/天 的
  烂板折价。所以本脚本【只推未封板】的票 —— ask1>0 说明还有卖单, 你真能买到。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
信号定义: 半路板 (首板预判)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1. 涨幅 ≥ MIN_PCT (默认 6%) 且 < 涨停阈值   ← 已启动, 但还没封
  2. ask1 > 0                                  ← 未封板, 能成交(核心!)
  3. 量比 ≥ MIN_VR (默认 2.0)                  ← 放量, 有资金进场
     量比 = 当前成交额 / (昨日成交额 × 当日已交易时间占比)
  4. 非一字开盘 (open 涨幅 < 涨停阈值)
  5. 非ST/非次新/非北交所 (池子构建时已过滤)

  买入 = 现价挂单(能成交) → 赌封板 → 次日择机卖
  ★ 这是"半路打板", 不是"涨停板排队" —— 唯一的散户可执行版本

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
用法
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python3 scripts/youzi_live.py --once              # 扫一轮, 只打印(测试用, 随时可跑)
  python3 scripts/youzi_live.py --once --push       # 扫一轮并推送
  python3 scripts/youzi_live.py                     # 常驻: 盘中每 60s 扫一次, 15:05 退出
  python3 scripts/youzi_live.py --interval 30       # 30 秒一轮

  部署(crontab, 每交易日 9:25 拉起, 脚本自行跑到收盘):
    25 9 * * 1-5 cd /path/to/proj && python3 scripts/youzi_live.py >> /tmp/youzi.log 2>&1

环境变量(.env):  DINGTALK_WEBHOOK / DINGTALK_SECRET / DINGTALK_KEYWORD
                 (或专用: DINGTALK_YOUZI_WEBHOOK / DINGTALK_YOUZI_KEYWORD)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, date, timedelta, time as dtime
from pathlib import Path

import pandas as pd
import requests

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

from pytdx.hq import TdxHq_API          # noqa: E402
from pytdx.params import TDXParams      # noqa: E402

from tradingagents.notify.dingtalk import send_markdown  # noqa: E402

TDX_HOST, TDX_PORT = "180.153.18.170", 7709
STATE_DIR = Path.home() / ".tradingagents" / "youzi"
POOL_CACHE = STATE_DIR / "pool.json"
POOL_DAILY = STATE_DIR / "pool_daily.pkl"     # 池内日K(回退用)
MB_DAILY = STATE_DIR / "mb_daily.pkl"         # 全市场主板日K(连板数/距高点/昨日额)
MB_FLOAT = STATE_DIR / "mb_float.json"        # 全市场流通股本
STATE_FILE = STATE_DIR / "signals.json"
POSITIONS = STATE_DIR / "positions.json"      # 已推送信号 → 次日卖出提醒用
LOG_DIR = STATE_DIR / "logs"                  # 每日运行日志(一天一个文件)

# 交易时段
AM = (dtime(9, 30), dtime(11, 30))
PM = (dtime(13, 0), dtime(15, 0))
TOTAL_MIN = 240.0

EXCLUDE_KW = ("ST", "退", "N ", "*")


# ── 股票池: 活跃股 (游资战场) ───────────────────────────────────────────────
def _col(df, *cands):
    for c in cands:
        if c in df.columns:
            return c
    return None


def _tdx_connect() -> TdxHq_API:
    api = TdxHq_API()
    for _ in range(3):
        try:
            if api.connect(TDX_HOST, TDX_PORT, time_out=5):
                return api
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("pytdx 连接失败")


def build_pool(n: int = 600, use_cache: bool = True, strict_prev: bool = True):
    """pytdx 全市场快照 → 成交额最活跃的 n 只(非ST/主板+创业板)。

    不用 akshare: 外网走代理不稳定。pytdx 直连行情服务器, 全市场约 4000 只
    只需 ~50 次批量请求(15~30s), 且 last_close 直接可用作量比基准。
    """
    if use_cache and POOL_CACHE.exists():
        try:
            data = json.loads(POOL_CACHE.read_text(encoding="utf-8"))
            if data.get("date") == datetime.now().strftime("%Y-%m-%d"):
                print(f"[池] 复用今日缓存 {len(data['pool'])} 只")
                return data["pool"]
        except Exception:
            pass

    api = _tdx_connect()
    try:
        print("[池] 拉取全市场代码 ...", flush=True)
        codes = []
        for market in (TDXParams.MARKET_SH, TDXParams.MARKET_SZ):
            cnt = api.get_security_count(market)
            # 注意: 部分服务器 start=0 返回空页 → 不能 break, 必须 continue 继续翻页
            for st in range(0, cnt, 1000):
                try:
                    lst = api.get_security_list(market, st)
                except Exception:
                    lst = None
                if not lst:
                    continue
                for it in lst:
                    code = str(it.get("code", ""))
                    name = str(it.get("name", "")).strip()
                    # 只做沪深主板(60/00): 排除创业板30(20%幅)、科创688、北证8/4
                    if not code.startswith(("60", "00")):
                        continue
                    # XD/XR/DR = 除权除息日, 当日涨跌幅失真, 必须排除
                    if "ST" in name or "退" in name or name.startswith(
                            ("N", "XD", "XR", "DR")):
                        continue
                    codes.append((market, code, name))
        print(f"[池] 候选 {len(codes)} 只 → 拉实时快照排序 ...", flush=True)

        rows = {}
        for i in range(0, len(codes), 80):
            batch = [(m, c) for m, c, _ in codes[i:i + 80]]
            try:
                qs = api.get_security_quotes(batch) or []
            except Exception:
                qs = []
            for q in qs:
                code = str(q.get("code", ""))
                amt = float(q.get("amount") or 0)
                prev = float(q.get("last_close") or 0)
                if prev <= 0:
                    continue
                rows[code] = (amt, prev, q)
        if not rows:
            raise RuntimeError("全市场快照为空(可能是非交易时段且服务器未保留行情)")

        # 量比基准: 盘前快照 amount=上一交易日全额 → 直接用;
        #           盘中/收盘后 amount=当日累计 → 按时间进度折算为全天, 口径统一
        prog = elapsed_min(datetime.now().time()) / TOTAL_MIN
        scale = 1.0 / prog if prog > 0.02 else 1.0

        # 全市场扫描(改进2): 不再按成交额截断 — 实测 600 池只覆盖当日
        # 涨幅≥9% 票的 52.2%(大量涨停票是小盘冷门股), 漏掉近一半信号
        names = {c: nm for _, c, nm in codes}
        if n and n > 0:
            ordered = sorted(rows.items(), key=lambda kv: -kv[1][0])[:n]
        else:
            ordered = list(rows.items())
        pool = []
        mb_float = {}
        if MB_FLOAT.exists():
            try:
                mb_float = {k: float(v) for k, v in
                            json.loads(MB_FLOAT.read_text()).items()}
            except Exception:
                mb_float = {}
        mb_last = None
        if MB_DAILY.exists():
            try:                       # 昨日成交额用本地全市场日K(比快照折算准)
                mbd = pd.read_pickle(MB_DAILY)
                mbd["date"] = pd.to_datetime(mbd["date"])
                mbd = mbd[mbd["date"] < pd.Timestamp(datetime.now().date())]
                mb_last = mbd.groupby("code")["amount"].last()
            except Exception:
                mb_last = None
        for code, (amt, prev, q) in ordered:
            pa = amt * scale           # 折算为"全天成交额"基准
            if mb_last is not None:
                v = float(mb_last.get(code, 0) or 0)
                if v > 0:
                    pa = v
            pool.append({
                "code": code,
                "name": names.get(code, code),
                "prev_close": prev,
                "prev_amt": pa,
                "float_shares": mb_float.get(code),
                "turnover": None,
                "thr": 0.098,              # 主板统一 ±10%
            })
        print(f"[池] 构建完成 {len(pool)} 只 (成交额前 {n})")
    finally:
        try:
            api.disconnect()
        except Exception:
            pass

    if strict_prev and not MB_DAILY.exists():
        print("[池] 本地全市场日K缺失 → 回退拉取池内日K+流通股本(约5~8分钟) ...",
              flush=True)
        fetch_daily_stats(pool)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    POOL_CACHE.write_text(json.dumps(
        {"date": datetime.now().strftime("%Y-%m-%d"), "pool": pool},
        ensure_ascii=False), encoding="utf-8")
    return pool


def fetch_daily_stats(pool: list[dict], verbose: bool = True) -> None:
    """拉池内个股 80 根日K + 流通股本 → 缓存 pkl, 并回填 prev_amt / float_shares。

    ⚠ 为什么不能用快照的 amount 当量比基准: 快照语义随时段变化
    (盘前=昨全日 / 盘中=今累计 / 收盘后=今全日)。收盘后构建会出现"自证循环"
    (基准=当日额 → 量比恒=1 → 信号全灭)。日K取"日期<今日的最后一根"才是确定的。

    日K同时服务于选股指标: 连板数 / 距60日高点 / 换手率(需流通股本)。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    api = _tdx_connect()
    frames, ok, okf = [], 0, 0
    try:
        for i, p in enumerate(pool):
            code = p["code"]
            m = (TDXParams.MARKET_SH if code[0] in "56"
                 else TDXParams.MARKET_SZ)
            try:
                bars = api.get_security_bars(
                    TDXParams.KLINE_TYPE_DAILY, m, code.encode(), 0, 80)
            except Exception:
                bars = None
            if bars:
                d = pd.DataFrame([{
                    "date": str(b.get("datetime", ""))[:10],
                    "close": float(b.get("close") or 0),
                    "high": float(b.get("high") or 0),
                    "amount": float(b.get("amount") or 0),
                } for b in bars])
                d = d[d["close"] > 0].sort_values("date").reset_index(drop=True)
                if len(d) > 5:
                    d.insert(0, "code", code)
                    frames.append(d)
                    hist = d[d["date"] < today]          # 昨日 = 最后一根历史
                    if len(hist):
                        p["prev_amt"] = float(hist["amount"].iloc[-1])
                        ok += 1
            try:
                fi = api.get_finance_info(m, code)
                lb = float((fi or {}).get("liutongguben") or 0)
                if lb > 0:
                    p["float_shares"] = lb
                    okf += 1
            except Exception:
                pass
            if verbose and (i + 1) % 200 == 0:
                print(f"    ...{i+1}/{len(pool)} (日K {len(frames)} "
                      f"流通股本 {okf})", flush=True)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    if frames:
        POOL_DAILY.parent.mkdir(parents=True, exist_ok=True)
        pd.concat(frames, ignore_index=True).to_pickle(POOL_DAILY)
    print(f"[池] 日K {len(frames)} | 昨日成交额基准 {ok}/{len(pool)} | "
          f"流通股本 {okf}/{len(pool)}")


# ── 时间工具 ────────────────────────────────────────────────────────────────
def in_session(t: dtime) -> bool:
    return (AM[0] <= t <= AM[1]) or (PM[0] <= t <= PM[1])


def elapsed_min(t: dtime) -> float:
    """当日已交易分钟数(用于量比的时间进度折算)。"""
    if t < AM[0]:
        return 0.0
    if t <= AM[1]:
        return (t.hour * 60 + t.minute) - (9 * 60 + 30)
    if t < PM[0]:
        return 120.0
    if t <= PM[1]:
        return 120.0 + (t.hour * 60 + t.minute) - (13 * 60)
    return TOTAL_MIN


# ── 实时扫描 ────────────────────────────────────────────────────────────────
def scan(api: TdxHq_API, pool: list[dict], progress: float,
         min_pct: float, min_vr: float, min_ratio: float = 0.6,
         hist: dict | None = None) -> tuple[list[dict], int]:
    """拉实时报价 → 返回 (信号列表, 池内涨停家数)。"""
    sigs, n_limit = [], 0
    stat = {"got": 0, "limit": 0, "sealed": 0, "lowpct": 0, "lowvr": 0, "yizi": 0,
            "x_high": 0, "x_mv": 0, "x_turn": 0}
    groups: dict[int, list] = {}
    for p in pool:
        m = TDXParams.MARKET_SH if p["code"][0] in "56" else TDXParams.MARKET_SZ
        groups.setdefault(m, []).append(p)

    quotes = []
    for m, ps in groups.items():
        for i in range(0, len(ps), 80):
            batch = [(m, x["code"]) for x in ps[i:i + 80]]
            try:
                q = api.get_security_quotes(batch)
                if q:
                    quotes.extend(q)
            except Exception as exc:
                print(f"[warn] 行情拉取失败: {exc}")

    meta = {p["code"]: p for p in pool}
    for q in quotes or []:
        code = str(q.get("code", ""))
        p = meta.get(code)
        if not p:
            continue
        price = float(q.get("price") or 0)
        prev = float(q.get("last_close") or 0) or (p["prev_close"] or 0)
        if price <= 0 or prev <= 0:
            continue
        stat["got"] += 1
        pct = price / prev - 1
        thr = p["thr"]
        if pct >= thr:                      # 已涨停
            n_limit += 1
            stat["limit"] += 1
            continue
        op = float(q.get("open") or 0)
        if op > 0 and op / prev - 1 >= thr:  # 一字开盘(虽未封但不该追)
            stat["yizi"] += 1
            continue
        ask1 = float(q.get("ask1") or 0)
        if ask1 <= 0:                       # 无卖单 = 封死/停牌, 买不到
            stat["sealed"] += 1
            continue
        # 启动阈值按各板涨跌幅缩放: 主板 10%→6%, 创业板 20%→12%(否则创业板 6% 只是噪音)
        if pct < max(min_pct, thr * min_ratio):
            stat["lowpct"] += 1
            continue
        amt = float(q.get("amount") or 0)
        exp_amt = p["prev_amt"] * max(progress, 1 / TOTAL_MIN)
        vr = amt / exp_amt if exp_amt > 0 else 0
        if vr < min_vr:
            stat["lowvr"] += 1
            continue
        # ── 游资五维评分(把"无脑选涨幅"升级为有结构的选股) ──
        streak = n20 = None
        mv = turn = dd = None
        h = hist.get(code) if hist else None
        if h is not None and len(h) >= 20:
            cl = h["close"].values.astype(float)
            ret = pd.Series(cl).pct_change().values
            lim = [bool(x) for x in (ret >= 0.098)] if ret[0] == ret[0] else []
            lim = [False] + [bool(x) for x in (ret[1:] >= 0.098)]
            streak = 0
            for v in reversed(lim[-10:]):        # 截至昨日的连续涨停天数
                if v:
                    streak += 1
                else:
                    break
            n20 = int(sum(lim[-20:]))            # 近20日涨停次数(股性)
            high60 = float(h["high"].values[-60:].max())
            dd = (high60 - price) / high60 * 100 if high60 > 0 else None
            fs = float(p.get("float_shares") or 0)
            if fs > 0:
                mv = fs * price / 1e8            # 流通市值(亿)
                turn = amt / (fs * price) * 100  # 换手率(%)

        # 硬排除: 高位板 / 盘子极端 / 换手爆表(出货)
        if streak is not None and streak >= 4:
            stat["x_high"] += 1
            continue
        if mv is not None and (mv < 15 or mv > 800):
            stat["x_mv"] += 1
            continue
        if turn is not None and turn > 45:
            stat["x_turn"] += 1
            continue

        score = 0
        score += {0: 30, 1: 32, 2: 24, 3: 8}.get(streak, 12)   # 连板位置(首板/2板最优)
        if mv is not None:
            score += 25 if 30 <= mv <= 200 else (15 if 200 < mv <= 500 else
                                                 (10 if 15 <= mv < 30 else 0))
        if turn is not None:
            score += 20 if 8 <= turn <= 25 else (10 if 25 < turn <= 35 else
                                                 (8 if 3 <= turn < 8 else 0))
        if dd is not None:
            score += 15 if dd <= 5 else (10 if dd <= 15 else (4 if dd <= 25 else 0))
        score += 10 if vr >= 5 else (8 if vr >= 3 else 5)
        if n20 is not None and n20 >= 2:
            score += 5                                          # 股性活跃加分

        sigs.append({
            "code": code, "name": p["name"], "price": price, "pct": pct * 100,
            "vr": vr, "amt_yi": amt / 1e8, "ask1": ask1, "thr": thr * 100,
            "streak": streak, "mv": mv, "turn": turn, "dd": dd, "n20": n20,
            "score": score,
        })
    sigs.sort(key=lambda x: -x["score"])
    return sigs, {"limit": n_limit, **stat}


class Tee:
    """stdout 双写: 控制台 + 当日日志文件(所有 print 自动落盘)。"""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "a", encoding="utf-8")
        self.so = sys.stdout

    def write(self, s):
        try:
            self.so.write(s)
            self.f.write(s)
            self.f.flush()      # 常驻进程: 实时落盘, 否则滞留缓冲区看不到
        except Exception:
            pass

    def flush(self):
        try:
            self.so.flush()
            self.f.flush()
        except Exception:
            pass


def nearest_slot(now: datetime, times: str, tol: int = 4) -> str | None:
    """当前时间是否落在某个触发时点附近(±tol 分钟)。空串=全天不限。"""
    if not (times or "").strip():
        return "ALL"
    cur = now.hour * 60 + now.minute
    for t in times.split(","):
        t = t.strip()
        if not t:
            continue
        try:
            hh, mm = t.split(":")
            v = int(hh) * 60 + int(mm)
        except Exception:
            continue
        if abs(cur - v) <= tol:
            return t
    return None


def load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


# ── 推送 ────────────────────────────────────────────────────────────────────
def fmt(sigs: list[dict], n_limit: int, t: datetime) -> tuple[str, str]:
    ts = t.strftime("%H:%M")
    title = f"游资半路板 {len(sigs)} 只 {t.strftime('%m-%d')} {ts}"
    lines = [f"### 游资半路板信号 · {t.strftime('%Y-%m-%d')} {ts}",
             f"**涨停家数(池内)**: {n_limit}　|　**触发**: {len(sigs)} 只", ""]
    if not sigs:
        lines.append("当前无符合条件的半路板。")
    for s in sigs[:8]:
        sc = s["score"]
        lvl = "S" if sc >= 80 else ("A" if sc >= 65 else ("B" if sc >= 50 else "C"))
        st = s.get("streak")
        board = "首板" if st == 0 else (f"{st}板后" if st else "—")
        mv = f"{s['mv']:.0f}亿" if s.get("mv") else "—"
        tn = f"{s['turn']:.1f}%" if s.get("turn") else "—"
        dd = f"{s['dd']:.0f}%" if s.get("dd") is not None else "—"
        lines.append(
            f"- **[{lvl}{sc}] {s['name']}({s['code']})**　**{s['pct']:+.2f}%**　"
            f"量比 {s['vr']:.1f}\n"
            f"　现价 {s['price']:.2f}　额 {s['amt_yi']:.2f}亿　| {board}　| {mv}　"
            f"| 换手 {tn}　| 距高 {dd}")
        a = s.get("ai") or {}
        if a:
            lines.append(f"　**AI: {a.get('action')} 概率 {a.get('prob', 0):.0f}%**"
                         f"　{a.get('reason', '')}")
            jm = a.get("judgement") or {}
            if jm:
                lines.append("　" + "　".join(
                    f"{k}:{str(v)[:22]}" for k, v in jm.items()))
    lines += ["", "> 半路板=未封板(ask1>0, 能成交), 赌封板次日溢价。",
              "> 评分=连板位置+盘子+换手+距60日高+量比; 优先做 S/A 级。",
              "> 次日: 高开>5%竞价出, 平/低开30分钟内出, 封板持有, 10:30不封必走。"]
    return title, "\n".join(lines)


def push(title: str, text: str, dry: bool = False) -> bool:
    import os
    webhook = (os.getenv("DINGTALK_YOUZI_WEBHOOK")
               or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    keyword = (os.getenv("DINGTALK_YOUZI_KEYWORD")
               or os.getenv("DINGTALK_KEYWORD") or "游资").strip()
    if not webhook:
        print("! 钉钉未配置 (DINGTALK_YOUZI_WEBHOOK / DINGTALK_WEBHOOK)")
        return False
    if dry:
        return True
    return send_markdown(title, text, webhook=webhook, keyword=keyword)


# ── 主流程 ──────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只扫一轮")
    ap.add_argument("--push", action="store_true", help="--once 时是否推送")
    ap.add_argument("--interval", type=int, default=60, help="扫描间隔(秒)")
    ap.add_argument("--pool", type=int, default=0,
                    help="池大小(0=全市场主板, 实测600池漏掉48%的9%信号)")
    # 默认口径来自 backtest_banlu_intraday.py 的验证结论:
    #   10:30 时点 + 涨幅≥9% 且未涨停 → 年化 +225%(0.2%成本), 1%滑点下仍 +35%
    #   量比条件已证伪(前视且无效) → 默认关闭
    ap.add_argument("--min-pct", type=float, default=8.0, help="最小涨幅(%)")
    ap.add_argument("--min-ratio", type=float, default=0.0,
                    help="涨停幅度比例阈值(0=禁用, 主板统一10%)")
    ap.add_argument("--min-vr", type=float, default=0.0,
                    help="最小量比(默认0=不过滤, 已验证为前视无效条件)")
    # 触发时点依据"首次触及9%"分时统计(2888笔): 10:30(+1.66%)/11:00(+1.90%)
    # 两段最优且笔均不降; 开盘半小时内首触仅+0.17%(强势未确认), 尾盘14:00后失效
    ap.add_argument("--trigger-times", default="",
                    help="触发时点(逗号分隔); 空=全天")
    ap.add_argument("--ai", action="store_true", help="启用 AI 判断层(否决/确认)")
    ap.add_argument("--min-prob", type=float, default=65.0,
                    help="AI 放行硬阈值: prob≥此值才推送(回测基线封板率63.4%)")
    ap.add_argument("--daily-max", type=int, default=3,
                    help="每日最多推送笔数(全仓纪律: 分散≤3只)")
    ap.add_argument("--cooldown", type=int, default=20, help="同票推送间隔(分钟)")
    ap.add_argument("--no-cache", action="store_true", help="强制重建股票池")
    ap.add_argument("--no-prev-amt", action="store_true",
                    help="跳过昨日成交额基准拉取(快速测试用, 量比可能失真)")
    args = ap.parse_args()

    # 每日运行日志: ~/.tradingagents/youzi/logs/YYYY-MM-DD.log
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    sys.stdout = Tee(log_path)
    print(f"───── youzi_live 启动 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
          f"(interval={args.interval}s, 触发={args.trigger_times or '全天'}, "
          f"阈值={args.min_pct}%, prob≥{args.min_prob}, 配额={args.daily_max}) ─────")

    pool = build_pool(args.pool, use_cache=not args.no_cache,
                      strict_prev=not args.no_prev_amt)
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state = {"date": today, "sent": {}}

    hist: dict = {}
    if MB_DAILY.exists():        # 全市场日K: 连板数/距60日高/换手率指标底座
        try:
            h = pd.read_pickle(MB_DAILY)[["code", "date", "close", "high",
                                          "amount"]].sort_values(["code", "date"])
            hist = {c: g for c, g in h.groupby("code")}
            last_d = h["date"].max().date()
            fresh = "OK" if last_d >= date.today() - timedelta(days=4) else "⚠过期, 建议跑 scripts/fetch_mainboard_daily.py"
            print(f"[池] 载入全市场日K {len(hist)} 只 (数据至 {last_d} {fresh})")
        except Exception as exc:
            print(f"[warn] 全市场日K载入失败: {exc}")
    elif POOL_DAILY.exists():
        try:
            h = pd.read_pickle(POOL_DAILY)
            hist = {c: g.sort_values("date").reset_index(drop=True)
                    for c, g in h.groupby("code")}
            print(f"[池] 载入池内日K {len(hist)} 只 (降级)")
        except Exception as exc:
            print(f"[warn] 日K载入失败(指标降级): {exc}")

    api = TdxHq_API()
    if not api.connect(TDX_HOST, TDX_PORT, time_out=5):
        print("! pytdx 连接失败")
        return 1
    print(f"已连接行情服务器 | 池 {len(pool)} 只 | "
          f"阈值 涨幅≥{args.min_pct}% 量比≥{args.min_vr}\n")

    def one_round(now: datetime, do_push: bool):
        if not pool:            # 池为空(如盘前启动行情未就绪) → 每轮重试重建
            print("[warn] 池为空, 尝试重建 ...")
            pool.extend(build_pool(args.pool, use_cache=False))
            return
        prog = elapsed_min(now.time()) / TOTAL_MIN
        sigs, st = scan(api, pool, prog, args.min_pct / 100, args.min_vr,
                        args.min_ratio, hist)
        print(f"[{now.strftime('%H:%M:%S')}] 有效 {st['got']:>4} | 涨停 {st['limit']:>3} | "
              f"封死 {st['sealed']:>3} | 一字 {st['yizi']} | 涨幅不足 {st['lowpct']} | "
              f"量比不足 {st['lowvr']} | 排(高位{st['x_high']} 盘{st['x_mv']} "
              f"换手{st['x_turn']}) → **半路板 {len(sigs)} 只**")
        for s in sigs[:10]:
            stk = s.get("streak")
            mv = f"{s['mv']:.0f}亿" if s.get("mv") else "—"
            tn = f"{s['turn']:.1f}%" if s.get("turn") else "—"
            dd = f"{s['dd']:.0f}%" if s.get("dd") is not None else "—"
            print(f"    [{s['score']:>3}分] {s['code']} {s['name']:<8} {s['pct']:+6.2f}%  "
                  f"量比{s['vr']:5.1f}  额{s['amt_yi']:>6.2f}亿  "
                  f"{('首板' if stk == 0 else str(stk) + '板后') if stk is not None else '—':<6}"
                  f" {mv:>7}  换手{tn:>6}  距高{dd:>4}")

        # 触发时点控制: 只在该下手的时点推(避免全天噪音)
        slot = nearest_slot(now, args.trigger_times)
        if slot is None:
            print(f"    (非触发时点 {args.trigger_times}, 仅记录)")
            return
        fired = state.setdefault("fired", [])
        key = f"{now.strftime('%Y-%m-%d')}_{slot}"
        if slot != "ALL" and key in fired:
            print(f"    (时点 {slot} 今日已触发)")
            return

        # 冷却去重
        fresh = []
        for s in sigs:
            last = state["sent"].get(s["code"])
            if last and (now - datetime.fromisoformat(last)).total_seconds() < args.cooldown * 60:
                continue
            fresh.append(s)
        if not fresh:
            return

        # ── AI 决策层: 规则负责召回, 模型负责判断"能不能封住板" ──
        if args.ai:
            try:
                from youzi_ai import decide
                decs, aist = decide(fresh, {"n_limit": st["limit"],
                                            "pool_n": len(pool), "max_st": 3},
                                    now, api=api, thr=args.min_pct,
                                    min_prob=args.min_prob)
                by = {d["code"]: d for d in decs}
                for s in fresh:
                    s["ai"] = by.get(s["code"])
                for s in fresh:
                    a = s.get("ai") or {}
                    act = a.get("action", "无判定")
                    head = (f"    [AI] {s['code']} {s['name']}: {act} "
                            f"prob={a.get('prob', '—')}")
                    jm = a.get("judgement") or {}
                    if jm:
                        head += "\n        " + " | ".join(
                            f"{k}:{v}" for k, v in jm.items())
                    if a.get("reason"):
                        head += f"\n        → {a['reason']}"
                    print(head)
                if aist == "ok":
                    # AI 正常判定(含"全放弃"=空列表) → 严格执行三道闸
                    before = len(fresh)
                    fresh = [s for s in fresh
                             if (s.get("ai") or {}).get("action") == "BUY"
                             and float((s.get("ai") or {}).get("prob", 0) or 0)
                             >= args.min_prob]
                    quota = args.daily_max - int(state.get("buy_today", 0))
                    if len(fresh) > max(quota, 0):
                        fresh = fresh[:max(quota, 0)]
                    state["buy_today"] = int(state.get("buy_today", 0)) + len(fresh)
                    print(f"    → AI 过滤: {before} → {len(fresh)} 只 "
                          f"(prob≥{args.min_prob:.0f}, 今日配额剩 "
                          f"{args.daily_max - int(state.get('buy_today', 0))})")
                else:
                    print("    → AI 调用失败, 本次不推送(宁可错过)")
                    fresh = []
            except Exception as exc:
                print(f"    [AI] 异常, 降级为规则: {exc}")
        if fresh and do_push:
            title, text = fmt(fresh, st["limit"], now)
            ok = push(title, text)
            print(f"    → 推送 {len(fresh)} 只: {'成功' if ok else '失败'}")
            for s in fresh:
                state["sent"][s["code"]] = now.isoformat()
            if slot != "ALL":
                fired.append(key)
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            if ok:                                   # 记录持仓 → 次日卖出提醒
                pos = load_json(POSITIONS)
                day = now.strftime("%Y-%m-%d")
                pos.setdefault(day, {})
                for s in fresh:
                    pos[day][s["code"]] = {
                        "name": s["name"], "entry": s["price"],
                        "pct": s["pct"], "score": s["score"],
                        "vr": s["vr"], "time": now.strftime("%H:%M"),
                    }
                POSITIONS.write_text(json.dumps(pos, ensure_ascii=False),
                                     encoding="utf-8")
        elif fresh:
            print(f"    → {len(fresh)} 只待推送 (加 --push 发送)")

    try:
        if args.once:
            one_round(datetime.now(), args.push)
            return 0
        # 常驻模式
        while True:
            now = datetime.now()
            t = now.time()
            if t > dtime(15, 5):
                print("收盘, 退出")
                break
            if not in_session(t):
                if t < dtime(9, 25):
                    pass
                time.sleep(30)
                continue
            try:
                one_round(now, True)
            except Exception as exc:
                print(f"[err] 扫描异常: {exc}")
                try:
                    api.disconnect()
                except Exception:
                    pass
                if not api.connect(TDX_HOST, TDX_PORT, time_out=5):
                    print("重连失败, 30s 后重试")
            time.sleep(args.interval)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
