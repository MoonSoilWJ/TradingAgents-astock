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
# macOS 系统代理(Clash 等写入 127.0.0.1:7890)会被 urllib/requests 自动读取,
# 代理不通时所有 HTTP(S) 调用失败(ProxyError) → 强制直连(实测直连可用)
import os as _os
_os.environ["no_proxy"] = _os.environ["NO_PROXY"] = "*"
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


def _atomic_json(path: Path, obj) -> None:
    """原子写 JSON(2026-09-22 新增): 状态文件被 sell_ai(每分钟)与本进程并发读写,
    write_text 直接覆盖会让并发读者拿到半截 JSON(11:16 卖侧 days_held=2 误判
    疑似此竞态)。tmp + os.replace: 读者要么看到旧完整版, 要么新完整版。"""
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    _os.replace(tmp, path)
POSITIONS = STATE_DIR / "positions.json"      # 已推送信号 → 次日卖出提醒用
LOG_DIR = STATE_DIR / "logs"                  # 每日运行日志(一天一个文件)

# 交易时段
AM = (dtime(9, 30), dtime(11, 30))
PM = (dtime(13, 0), dtime(15, 0))
TOTAL_MIN = 240.0

# 量比早盘失真保护: 开盘 EARLY_MIN 分钟内, 分母(昨日全天额×时间进度)极小,
# 量比系统性虚高(2026-09-11 实测 09:31 云煤543x/中百181x/国光41x)。
# 该窗口内量比钳到 VR_CAP 并标记 vr_na → 不参与评分、prompt 标注失真。
EARLY_MIN = 15.0
VR_CAP = 10.0
# 资金强度下限(预估全天换手率): 回测 30 天对照, 加此过滤 AI 增益 +0.06%→+1.47%
MIN_STRENGTH = 0.10
PROGRESS_CAP = 0.33        # 进度封顶(2026-09-16): vr/est_turn 均 ÷progress, 午后progress≈0.85
                         # 把等效门槛抬到不可达→午后0候选进AI; 封顶后上午(progress<cap)不变,
                         # 午后等效门槛降到约"1.3倍昨日量", 活票可进AI被判(推送仍由模型把关)
# ── 配额体系已移除(2026-09-23 用户批准): 不再设 每日3笔/每日新买2 上限,
#    有判定过闸就推 —— 靠 闸门(prob≥75/盘口≥7/位置≥7) + 推送窗口 + 大面日闸 质控。
#    原 quota_state/positions_held_count/sells_today_count/影子模式/[额度恢复]解冻
#    一并移除。positions.json/youzi_sold.jsonl 记账保留(卖侧仍依赖)。
_REGIME_SERIES = None       # 当日情绪周期序列 [(ts, rate, avg)](30分钟斜率用)
_REGIME_FILE = STATE_DIR / "fast_regime.json"   # 序列落盘(重启不丢当日斜率)

# 不再作为"每轮候选上限"(2026-09-22 去掉, 避免漏票), 只作为整批失败时的拆分重试粒度
AI_CHUNK = int(_os.getenv("YOUZI_AI_CHUNK") or "8")
AI_BATCH_CAP = 8            # 兼容旧引用; 判定分批粒度见 AI_CHUNK
                         # 候选过多→生成超时/限流→整批丢判定(实测午后14只→全None); 主推优先, 观测按分截断

# 秒级盘口(fast_book.json)新鲜度上限(2026-09-22 复盘修订): 哨兵 cron 每 2 分钟一轮,
# 旧值 30s 只覆盖 ~1/4 判定轮 → 75% 的轮次 AI 根本看不到秒级证据。
# 放宽到覆盖整个哨兵周期(150s), 数据龄随注入透出, 让模型与复盘都能感知证据新旧。
FAST_BOOK_MAX_AGE = int(_os.getenv("YOUZI_FAST_BOOK_MAX_AGE") or "150")  # 秒
# 落盘候选后等哨兵扫一轮再读 fast_book(哨兵 3s 轮询, 给 4s 冗余)
FAST_BOOK_WAIT = int(_os.getenv("YOUZI_FAST_BOOK_WAIT") or "4")          # 秒
# ── 2026-09-14 实盘149笔复盘定档, 试运行两周(至2026-09-28): ──
# 量比≥4:  封板率 14%(vr<2)→46%(vr>8) 单调升, 低量比段拦掉
# 成交额上限: 判定时已成交>12亿 封板率仅13%(全场明牌/抛压最大) — 反直觉但数据硬
# 复盘要点: ①被拦的BUY封板率是否仍低(是=过滤正确) ②通过的封板率应≈45% ③prob区分度
MAX_AMT_YI = 12.0          # 成交额上限(亿); 0=关闭

# 买入主信号(2026-09-18 校准结论: prob>75 且 盘口≥7 且 位置≥7)
# 双高(盘口+位置)是封板率最强正向维度, prob 仅作辅助卡线; 候选池只推 3 笔,
# 放宽 prob 不会增加推送数, 故定 75 而非 70。
MIN_PAN = 7   # 盘口子分下限(高分组封板率 51% vs 低分组 40%)
MIN_POS = 7   # 位置子分下限(高分组 51% vs 低分组 37%)

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


def _codes_fallback() -> list[tuple]:
    """pytdx 代码列表不可用时的本地兜底: 主板日K缓存里的代码(不依赖外网)。"""
    try:
        d = pd.read_pickle(STATE_DIR / "mb_daily.pkl")
        cs = sorted({str(c) for c in d["code"].unique()})
        return [(TDXParams.MARKET_SH if c[0] in "56"
                 else TDXParams.MARKET_SZ, c, "")
                for c in cs if c.startswith(("60", "00"))]
    except Exception:
        return []


def _quotes_fallback(codes: list[str]) -> list[dict]:
    """TDX 快照失效时的回退: 腾讯行情(2026-09-10 起 pytdx 公共服务器集体失效)。

    字段映射成 pytdx 快照格式, 调用方无需改动。
    """
    try:
        from tx_quote import snapshot as tx_snap
    except Exception:
        return []
    out = []
    for c, v in tx_snap(codes).items():
        nm = str(v.get("name") or "")
        # ST/退市/次新/除权日: 涨跌幅失真(与主路径同口径)
        if "ST" in nm or "退" in nm or nm.startswith(("N", "XD", "XR", "DR")):
            continue
        out.append({"code": c, "name": nm, "price": v["price"],
                    "last_close": v["prev_close"], "open": v["open"],
                    "high": v["high"], "low": v["low"],
                    "amount": v["amount"], "vol": v["vol"],
                    "ask1": v["ask1"], "bid1": v["bid1"],
                    "ask_vol": v["ask1_vol"], "bid_vol": v["bid1_vol"]})
    return out


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

    try:
        api = _tdx_connect()
    except Exception:
        api = None
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
        if not codes:                      # TDX 列表不可用 → 本地代码缓存
            codes = _codes_fallback()
            print(f"[池] TDX 列表不可用, 改用本地代码 {len(codes)} 只",
                  flush=True)
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
        if not rows:                       # TDX 快照失效 → 腾讯行情回退
            print("[池] TDX 快照空, 回退腾讯行情源 ...", flush=True)
            for q in _quotes_fallback([c for _, c, _ in codes]):
                amt = float(q.get("amount") or 0)
                prev = float(q.get("last_close") or 0)
                if prev > 0:
                    rows[str(q.get("code", ""))] = (amt, prev, q)
        if not rows:
            # 不再抛异常退出: 崩溃会被看门狗反复拉起一个必死的进程。
            # 返回空池 → 主循环每轮重试重建, 行情恢复后自动接管。
            print("[warn] 全市场快照为空(行情源不可用), 返回空池, 下轮重试",
                  flush=True)
            return []

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
def log_blocked(blocked: list[dict], now: datetime) -> None:
    """被规则层拦截的候选 → jsonl(两周复盘用: 回填其当日封板状态,
    验证「被拦的确实差」; 若被拦的封板率高 = 误杀, 需撤过滤)。"""
    if not blocked:
        return
    try:
        f = STATE_DIR / "blocked_log.jsonl"
        seen_today = set()
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    o = json.loads(line)
                    if o.get("ts", "").startswith(now.strftime("%Y-%m-%d")):
                        seen_today.add(o["code"])
                except Exception:
                    pass
        with open(f, "a", encoding="utf-8") as fp:
            for b in blocked:
                if b["code"] in seen_today:
                    continue                    # 同日同票只记首次拦截
                seen_today.add(b["code"])
                b["ts"] = now.isoformat(timespec="seconds")
                fp.write(json.dumps(b, ensure_ascii=False) + "\n")
    except Exception:
        pass


def log_judgements(sigs: list[dict], now: datetime,
                   market: dict | None = None) -> None:
    """AI 判定快照 → JSONL(校准闭环: 收盘后回填实际结果, 统计 prob 可靠度)。

    market: 当日基本盘(涨停家数/池大小/最高连板) — 将来可按市场环境分组校准。
    """
    try:
        m = market or {}
        with open(STATE_DIR / "ai_judgements.jsonl", "a") as f:
            for s in sigs:
                a = s.get("ai") or {}
                f.write(json.dumps({
                    "ts": now.isoformat(timespec="seconds"),
                    "code": s["code"], "name": s["name"],
                    "action": a.get("action"), "prob": a.get("prob"),
                    "reason": (a.get("reason") or "")[:120],
                    "scores": a.get("scores") or {},           # 维度子分(校准用)
                    "pct": round(float(s.get("pct", 0)), 2),   # 已是百分数
                    # 判定瞬间的绝对价格(2026-09-23 加): 校准"入场口径收益"必需 ——
                    # 旧口径 ret_open1=次日开盘/当日收盘, 与推送时刻无关, 度量不了
                    # "几点推更赚"(同票同日所有判定共用一个值)。有了它 + 日K昨收即可
                    # 精确算 次日开盘/判定价。实测追高成本 0.6~2.4pp。
                    "price": round(float(s.get("price") or 0), 2),
                    "thr_pct": s.get("thr"),
                    "amt_yi": s.get("amt_yi"), "vr": round(float(s.get("vr", 0)), 2),
                    "n_limit": m.get("n_limit"), "pool_n": m.get("pool_n"),
                    "max_st": m.get("max_st"),
                }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def log_obs_judgements(sigs: list[dict], now: datetime,
                       market: dict | None = None,
                       push_min_pct: float = 8.0) -> None:
    """观测带(6%~push_min_pct)判定快照 → ai_obs_judgements.jsonl(独立校准用)。

    与 ai_judgements.jsonl 同源同格式, 仅过滤 pct<push_min_pct 的 borderline 票,
    便于两周后单独统计"模型对 6~8% 票判定 vs 次日实际"的可靠度, 不污染主推校准。
    ai_judgements.jsonl 仍记全量(主推+观测), 本文件是其中的观测带切片。
    """
    try:
        obs = [s for s in sigs
               if float(s.get("pct", 0) or 0) < push_min_pct]
        if not obs:
            return
        m = market or {}
        with open(STATE_DIR / "ai_obs_judgements.jsonl", "a") as f:
            for s in obs:
                a = s.get("ai") or {}
                f.write(json.dumps({
                    "ts": now.isoformat(timespec="seconds"),
                    "code": s["code"], "name": s["name"],
                    "action": a.get("action"), "prob": a.get("prob"),
                    "reason": (a.get("reason") or "")[:120],
                    "scores": a.get("scores") or {},
                    "pct": round(float(s.get("pct", 0)), 2),
                    "price": round(float(s.get("price") or 0), 2),
                    "thr_pct": s.get("thr"),
                    "amt_yi": s.get("amt_yi"), "vr": round(float(s.get("vr", 0)), 2),
                    "n_limit": m.get("n_limit"), "pool_n": m.get("pool_n"),
                    "max_st": m.get("max_st"),
                }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def log_pushed(sigs: list[dict], now: datetime) -> None:
    """真正进入推送流程的 → ai_pushed.jsonl(线上展示用)。

    与 ai_judgements.jsonl 的区别: 后者记全部判定(BUY/SKIP, 含窗口外/闸门拦下的),
    用于校准; 本文件只记「实际会下单」的, 否则线上会显示一堆根本没买的"持仓中"。
    """
    try:
        with open(STATE_DIR / "ai_pushed.jsonl", "a") as f:
            for s in sigs:
                a = s.get("ai") or {}
                f.write(json.dumps({
                    "ts": now.isoformat(timespec="seconds"),
                    "code": s["code"], "name": s["name"],
                    "action": a.get("action"), "prob": a.get("prob"),
                    "pct": round(float(s.get("pct", 0)), 2),
                    "price": round(float(s.get("price") or 0), 2),   # 推送时价(入场口径)
                }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _fmt_fast_book(b: dict) -> str:
    """把哨兵的秒级盘口摘要压成一行中文(注入 AI 证据)。"""
    parts = []
    m = b.get("mom30")
    if m is not None:
        parts.append("30秒动量%+.2f%%" % m)
    a = b.get("ask_chg60")
    if a is not None:
        parts.append("卖一量60秒%+.0f%%(%s)" % (
            a, "卖压快速消化·临封" if a <= -30 else
            ("卖压堆积" if a >= 30 else "平稳")))
    t = int(b.get("touches") or 0)
    if t:
        parts.append("今日触板%d次" % t)
    if b.get("bid1_amt"):
        parts.append("封单%s" % ("%.1f亿" % (b["bid1_amt"] / 1e8)
                                 if b["bid1_amt"] >= 1e8
                                 else "%.0f万" % (b["bid1_amt"] / 1e4)))
    if b.get("sealed"):
        parts.append("已封死")
    return " | ".join(parts)


def _regime_data(now: datetime) -> dict:
    """情绪周期指标(纯代码, 每轮重算)——日内实时数据(_rd)缺失时的提示词兜底。

    数据: limits_{昨日}.json(save_limit_list 落盘的昨日涨停列表) + 腾讯快照(今日表现)。
    指标定义: 晋级率 = 昨日涨停今日仍封死占比; 均涨 = 昨日涨停股今日平均涨幅。
    ⚠ 2026-09-23 文案校准: 晋级率早盘天然偏低(多数板未封上), 原"退潮期→暂停推送"
    的指令性文案会天天压制早盘黄金时段, 已改为事实描述(见下方分类)。
    返回 {} (样本不足) 或 {n, promoted, touched, rate, avg, level, advice}。
    """
    import glob
    today = now.strftime("%Y-%m-%d")
    prev_path = None
    for f in sorted(glob.glob(str(STATE_DIR / "limits_*.json")), reverse=True):
        d = Path(f).stem.replace("limits_", "")
        if d < today:
            prev_path = Path(f)
            break
    if prev_path is None:
        return ""
    try:
        y_codes = [str(c) for c in json.loads(prev_path.read_text(encoding="utf-8"))]
    except Exception:
        return ""
    if not y_codes:
        return ""
    try:
        from tx_quote import snapshot as tx_snapshot
        quotes = tx_snapshot(y_codes[:80]) or {}
    except Exception:
        return ""
    promoted = touched = 0
    pcts: list = []
    for c in y_codes:
        q = quotes.get(c) or {}
        price = float(q.get("price") or 0)
        prev = float(q.get("prev_close") or 0)
        high = float(q.get("high") or 0)
        if price <= 0 or prev <= 0:
            continue
        lim = round(prev * (1 + (0.20 if c[:3] in ("300", "688", "689") else 0.10)), 2)
        pcts.append(price / prev - 1)
        if price >= lim - 0.01:
            promoted += 1
        elif high >= lim - 0.01:
            touched += 1
    n = len(pcts)
    if n < 5:                      # 样本太少不出结论
        return {}
    rate = promoted / n * 100
    avg = sum(pcts) / n * 100
    # 2026-09-23 文案校准: 原"退潮期/暂停推送"判法被 523 笔格子样本证伪 ——
    # 实时晋级率早盘天然 0~27%(板还没封上), rate<30 几乎每天早盘都触发, 等于
    # 天天给模型灌"退潮"指令、压制黄金时段。改为事实描述+偏差提示, 不再下令推送。
    if avg < -1.5:
        regime, advice = ("大面日",
                          "昨日涨停普遍大跌(均涨%+.1f%%), 接力风险极高; "
                          "此情形代码闸门已直接停推, 你只需对票本身从严。" % avg)
    elif rate < 30 and avg < 1.0:
        regime, advice = ("早盘弱(常态)",
                          "晋级率低迷在早盘属正常现象(多数板尚未封上), "
                          "勿仅因该数字恐慌; 结合均涨/斜率与个股位置判断。")
    elif rate < 40 and avg < 0:
        regime, advice = ("亏钱效应",
                          "昨日涨停股普遍下跌: 只给最强的核心票高分, 普通候选从严。")
    elif rate >= 50 and avg >= 3.0:
        regime, advice = ("情绪主升",
                          "可适度积极, 但高位票(3板+)注意兑现风险。")
    else:
        regime, advice = ("震荡", "情绪中性: 按正常标准判。")
    return {"n": n, "promoted": promoted, "touched": touched, "rate": rate,
            "avg": avg, "level": regime, "advice": advice}


def _regime_text(d: dict) -> str:
    """把 _regime_data 的结果压成注入 prompt 的文本块。"""
    if not d:
        return ""
    return ("昨日涨停 %d 只 → 今日晋级 %d(%.0f%%) | 触板未回封 %d 只 | "
            "昨日涨停股今日均涨 %+.2f%% → **周期: %s**"
            % (d["n"], d["promoted"], d["rate"], d["touched"], d["avg"],
               d["level"])) + "\n  %s" % d["advice"]


# (2026-09-23) 原 REGIME_BLOCK_LEVELS("退潮期","冰点期") 按周期停推的机制已删:
# 晋级率门槛被格子样本证伪, 推送纪律改由 推送窗口+大面日闸 执行(见 _gate_blocked)。


# 情绪快照 TTL 缓存(2026-09-23): 每轮(60s)都拉昨日涨停股实时价纯属浪费 ——
# 大面日闸 10 天样本 0 次触发, 真正需要新价的只有"推送前二次确认"。
# 改为: 常规轮次用 ≤TTL 缓存值(默认120s, YOUZI_REGIME_TTL 可调),
# 仅推送确认时 force 现拉。快照 320 次/天 → ~160 次/天。
REGIME_TTL = int(_os.getenv("YOUZI_REGIME_TTL") or "120")   # 秒
_REGIME_CACHE = {"ts": None, "data": {}}


def _regime_intraday(now: datetime, force: bool = False) -> dict:
    """30分钟级情绪周期 —— 大面日闸与提示词的数据源。

    对昨日涨停股做实时快照 → 当前晋级率/均涨; 并维护当日序列算 30 分钟斜率。
    2026-09-23 加 TTL 缓存: 常规轮次直接用 ≤REGIME_TTL 的缓存(大面日闸 10 天
    0 次触发, 不值得每 60s 现拉); 推送前二次确认传 force=True 现拉新价。
    序列落盘 fast_regime.json(按日), 重启不丢。
    """
    global _REGIME_SERIES
    if not force and _REGIME_CACHE["ts"] is not None and (
            now - _REGIME_CACHE["ts"]).total_seconds() < REGIME_TTL:
        return _REGIME_CACHE["data"]
    import glob
    today = now.strftime("%Y-%m-%d")
    prev_path = None
    for f in sorted(glob.glob(str(STATE_DIR / "limits_*.json")), reverse=True):
        dd = Path(f).stem.replace("limits_", "")
        if dd < today:
            prev_path = Path(f)
            break
    if prev_path is None:
        return {}
    try:
        y_codes = [str(c) for c in json.loads(prev_path.read_text(encoding="utf-8"))]
    except Exception:
        return {}
    if not y_codes:
        return {}
    try:
        from tx_quote import snapshot as tx_snapshot
        quotes = tx_snapshot(y_codes[:80]) or {}
    except Exception:
        return {}
    sealed = 0
    pcts: list = []
    for c in y_codes:
        q = quotes.get(c) or {}
        price = float(q.get("price") or 0)
        prev = float(q.get("prev_close") or 0)
        if price <= 0 or prev <= 0:
            continue
        lim = round(prev * (1 + (0.20 if c[:3] in ("300", "688", "689") else 0.10)), 2)
        pcts.append(price / prev - 1)
        if price >= lim - 0.01:
            sealed += 1
    n = len(pcts)
    if n < 5:
        return {}
    rate = sealed / n * 100
    avg = sum(pcts) / n * 100
    if _REGIME_SERIES is None or _REGIME_SERIES.get("date") != today:
        try:
            old = json.loads(_REGIME_FILE.read_text(encoding="utf-8")) \
                if _REGIME_FILE.exists() else {}
            _REGIME_SERIES = old if old.get("date") == today else {
                "date": today, "series": []}
        except Exception:
            _REGIME_SERIES = {"date": today, "series": []}
    series = _REGIME_SERIES.setdefault("series", [])
    ts = now.timestamp()
    series.append([ts, round(rate, 2), round(avg, 3)])
    while series and ts - series[0][0] > 4 * 3600:
        series.pop(0)
    try:
        _REGIME_FILE.write_text(json.dumps(_REGIME_SERIES), encoding="utf-8")
    except Exception:
        pass
    # 30 分钟斜率: 取 ≥25 分钟前最近样本作基准
    ref = next(((r, a) for t, r, a in reversed(series) if ts - t >= 25 * 60),
               None) or (series[0][1], series[0][2])
    out = {"n": n, "sealed": sealed, "rate": rate, "avg": avg,
           "rate_slope": rate - ref[0], "avg_slope": avg - ref[1]}
    _REGIME_CACHE["ts"] = now
    _REGIME_CACHE["data"] = out
    return out


def _gate_blocked(d: dict) -> tuple:
    """日内闸门判定 → (blocked, reason)。

    2026-09-23 大幅简化: 原"晋级率<30且均涨<1 或 <40且均涨<0"的退潮条款被
    523 笔 10 分钟格子样本证伪 —— 实时晋级率 6 天里从未超过 27%(rate<30/<40
    形同虚设), 实际起作用的只是均涨<1.0; 且因早盘晋级率天然低(板还没封上),
    该闸门专拦早盘黄金时段(封板率 50%+ 的时段), 分层后 lift 为负。故只保留
    唯一有价值的极端保护:
      大面日(昨日涨停均涨 < -1.5%)无条件拦 —— 样本内从未触发, 零成本保险。
    晋级率/均涨仍照常注入提示词(【情绪周期】行), 交给模型权衡, 不做代码拦截。
    """
    if not d:
        return False, ""
    avg = d["avg"]
    if avg < -1.5:
        return True, "大面日(昨日涨停均涨%+.1f%%)" % avg
    return False, ""


def market_max_streak(limit_codes: list, hist: dict):
    """市场最高连板(真实值) = 今日涨停股中"截至昨日的连续涨停天数"最大值 + 1。

    原代码把 `max_st: 3` 硬编码注入 prompt, 模型拿到的是假的市场情绪数据。
    hist 已按 `date < 今日` 过滤(见 build_pool), 故无前视。
    算不出(池缺数据/今日无涨停)则返回 None → prompt 显示 "?"。
    """
    if not limit_codes or not hist:
        return None
    best = 0
    for c in limit_codes:
        h = hist.get(c)
        if h is None or len(h) < 3:
            continue
        try:
            cl = h["close"].values.astype(float)
            ret = pd.Series(cl).pct_change().values
            lim = [False] + [bool(x) for x in (ret[1:] >= 0.098)]
            n = 0
            for v in reversed(lim[-10:]):
                if v:
                    n += 1
                else:
                    break
        except Exception:
            continue
        if n > best:
            best = n
    return (best + 1) if best >= 0 and limit_codes else None


def scan(api: TdxHq_API, pool: list[dict], progress: float,
         min_pct: float, min_vr: float, min_ratio: float = 0.6,
         hist: dict | None = None) -> tuple[list[dict], int]:
    """拉实时报价 → 返回 (信号列表, 池内涨停家数)。"""
    sigs, n_limit = [], 0
    stat = {"got": 0, "limit": 0, "sealed": 0, "lowpct": 0, "lowvr": 0, "yizi": 0,
            "lowst": 0, "hiamt": 0, "x_high": 0, "x_mv": 0, "x_turn": 0,
            "touch": 0}          # 触板未回封(盘中炸板率原料)
    blocked = []                    # 被规则层拦截的候选(复盘用: 验证拦截是否正确)
    # 进度封顶(2026-09-16): vr/est_turn 均 ÷progress, 午后 progress≈0.85 把等效门槛
    # 抬到不可达(正常节奏票 vr≈1 过不了 min_vr)→ 午后0候选进AI。封顶到 PROGRESS_CAP:
    # 上午(progress<cap)不变, 午后等效门槛≈降到"1.3倍昨日量", 活票可进AI被判。
    progress_eff = min(progress, PROGRESS_CAP)
    limit_codes = []                # 今日实时涨停列表 → 情绪指标(次日算溢价)
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
    if not quotes:                      # TDX 失效 → 腾讯行情回退
        quotes = _quotes_fallback([p["code"] for p in pool])
        if quotes:
            print(f"[行情] TDX 不可用, 本轮使用腾讯源 {len(quotes)} 只")

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
            limit_codes.append(code)
            continue
        op = float(q.get("open") or 0)
        if op > 0 and op / prev - 1 >= thr:  # 一字开盘(虽未封但不该追)
            stat["yizi"] += 1
            continue
        ask1 = float(q.get("ask1") or 0)
        if ask1 <= 0:                       # 无卖单 = 封死/停牌, 买不到
            stat["sealed"] += 1
            continue
        hi = float(q.get("high") or 0)
        if hi > 0 and hi >= prev * (1 + thr) - 0.01:
            stat["touch"] += 1              # 触及涨停但已回落 = 盘中炸板(未回封)
        # 启动阈值按各板涨跌幅缩放: 主板 10%→6%, 创业板 20%→12%(否则创业板 6% 只是噪音)
        if pct < max(min_pct, thr * min_ratio):
            stat["lowpct"] += 1
            continue
        amt = float(q.get("amount") or 0)
        exp_amt = p["prev_amt"] * max(progress_eff, 1 / TOTAL_MIN)
        vr = amt / exp_amt if exp_amt > 0 else 0
        # 早盘失真保护: 时间进度折算出的 vr 不可采信 → 钳制 + 标记
        vr_na = progress * TOTAL_MIN < EARLY_MIN
        if vr_na:
            vr = min(vr, VR_CAP)
        if vr < min_vr:
            stat["lowvr"] += 1
            blocked.append({"code": code, "name": p.get("name"),
                            "why": f"lowvr({vr:.1f})", "pct": round(pct*100, 2),
                            "vr": round(vr, 2), "amt_yi": round(amt/1e8, 2)})
            continue
        # ── 资金强度过滤(回测验证: 8%全量增益+0.06% vs +资金过滤+1.47%) ──
        # 预估全天换手率 = 当日累计成交额 / (流通股本×现价) / 时间进度
        # 回测口径: ≥10%(p88分位)才送 AI; 盘中实时可算, 无前视
        fs = float(p.get("float_shares") or 0)
        if fs > 0 and progress_eff > 0.06:
            est_turn = amt / (fs * price) / progress_eff
            if est_turn < MIN_STRENGTH:
                stat["lowst"] += 1
                blocked.append({"code": code, "name": p.get("name"),
                                "why": f"lowst({est_turn*100:.1f}%)",
                                "pct": round(pct*100, 2),
                                "vr": round(vr, 2),
                                "amt_yi": round(amt/1e8, 2)})
                continue
        # ── 成交额上限(实盘149笔: 12亿+ 封板率仅13% — 全场明牌, 抛压最大) ──
        if MAX_AMT_YI > 0 and amt / 1e8 > MAX_AMT_YI:
            stat["hiamt"] += 1
            blocked.append({"code": code, "name": p.get("name"),
                            "why": f"hiamt({amt/1e8:.1f}亿)",
                            "pct": round(pct*100, 2), "vr": round(vr, 2),
                            "amt_yi": round(amt/1e8, 2)})
            continue
        # ── 成交额上限(实盘149笔: 12亿+ 封板率仅13% — 全场明牌, 抛压最大) ──
        if MAX_AMT_YI > 0 and amt / 1e8 > MAX_AMT_YI:
            stat["hiamt"] += 1
            continue
        # ── 游资五维评分(把"无脑选涨幅"升级为有结构的选股) ──
        streak = n20 = None
        mv = turn = dd = None
        pct5 = pct10 = rsi14 = None
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
            # 高位减分项原料(截至昨日收盘, 无前视): 5日/10日涨幅 + RSI14
            # 2026-09-15 超声电子教训: 7天+56%妖股, AI 层当时完全看不到超买证据
            if len(cl) >= 11:
                pct5 = (cl[-1] / cl[-6] - 1) * 100
                pct10 = (cl[-1] / cl[-11] - 1) * 100
            if len(cl) >= 15:
                c15 = cl[-15:]
                diffs = [c15[k + 1] - c15[k] for k in range(14)]
                gains = sum(x for x in diffs if x > 0) / 14.0
                losses = sum(-x for x in diffs if x < 0) / 14.0
                rsi14 = 100.0 if losses <= 0 else 100 - 100 / (1 + gains / losses)
            fs = float(p.get("float_shares") or 0)
            if fs > 0:
                mv = fs * price / 1e8            # 流通市值(亿)
                turn = amt / (fs * price) * 100  # 换手率(%)

        # 极端样本不排除(判断权在 AI), 仅计数提示; 数据照常进入候选
        if streak is not None and streak >= 4:
            stat["x_high"] += 1
        if mv is not None and (mv < 15 or mv > 800):
            stat["x_mv"] += 1
        if turn is not None and turn > 45:
            stat["x_turn"] += 1

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
        # 早盘量比不可信 → 取中性档, 不因折算虚高给满分
        score += 8 if vr_na else (10 if vr >= 5 else (8 if vr >= 3 else 5))
        if n20 is not None and n20 >= 2:
            score += 5                                          # 股性活跃加分

        sigs.append({
            "code": code, "name": p["name"], "price": price, "pct": pct * 100,
            "vr": vr, "vr_na": vr_na, "amt_yi": amt / 1e8, "ask1": ask1,
            "thr": thr * 100,
            "streak": streak, "mv": mv, "turn": turn, "dd": dd, "n20": n20,
            "pct5": pct5, "pct10": pct10, "rsi": rsi14,
            "score": score,
        })
    sigs.sort(key=lambda x: -x["score"])
    return sigs, {"limit": n_limit, "blocked": blocked,
                  "limit_codes": limit_codes, **stat}


def save_limit_list(codes: list[str], now: datetime) -> None:
    """今日涨停代码列表落盘(增量合并) — 次日 emotion_block 用它算
    「昨日涨停今日平均溢价」= 打板接力赚钱效应(情绪核心指标)。"""
    if not codes:
        return
    try:
        f = STATE_DIR / f"limits_{now.strftime('%Y-%m-%d')}.json"
        old: list[str] = []
        if f.exists():
            try:
                old = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                old = []
        merged = sorted(set(old) | set(codes))
        if len(merged) != len(old):
            f.write_text(json.dumps(merged), encoding="utf-8")
    except Exception:
        pass


def emotion_block(now: datetime) -> str:
    """打板接力赚钱效应 → 注入 AI prompt(证据维度, 非硬规则)。

    指标: 昨日涨停股今日平均溢价。冰点(<-2%)= 接力必亏, 修复(>2%)= 情绪回暖。
    缓存 5 分钟(盘中每轮算会拖慢节奏); 无昨日数据返回空串。
    """
    yd = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    f = STATE_DIR / f"limits_{yd}.json"
    if not f.exists():
        return ""
    try:
        codes = json.loads(f.read_text(encoding="utf-8"))
        if not codes:
            return ""
        from tx_quote import snapshot as tx_snap
        q = tx_snap(codes)
        prems = []
        for c in codes:
            v = q.get(c)
            if not v or not v.get("price") or not v.get("prev_close"):
                continue
            prems.append(v["price"] / v["prev_close"] - 1)
        if len(prems) < 10:                 # 样本太少不可信
            return ""
        prem = sum(prems) / len(prems) * 100
        n_up = sum(1 for x in prems if x > 0)
        mood = ("冰点, 接力普遍亏损" if prem < -2
                else "回暖, 封板次日有溢价" if prem > 2 else "中性")
        return (f"昨日涨停{len(codes)}只, 今日平均溢价 {prem:+.1f}% "
                f"(红盘率 {n_up}/{len(prems)}) — 打板接力赚钱效应{mood}")
    except Exception:
        return ""


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


def parse_hhmm(s: str) -> dtime | None:
    try:
        hh, mm = s.strip().split(":")
        return dtime(int(hh), int(mm))
    except Exception:
        return None


# ── 推送窗口(2026-09-23 用户批准, "宁缺毋滥"): 只在窗口内推送, 窗口外判定照常
#    落盘但 不推送/不占配额。依据: 523 笔 10 分钟格子回放 ——
#      封板率 全天 55.5%(早盘)/43.9%(上午)/27.7%(午后)/6.3%(尾盘), 14:30 后 0%；
#      配额模拟: 09:40-11:00 20 笔 · 封板 70.0% · 入场 +1.19%/笔,
#               vs 全天 24 笔 · 66.7% · +0.58%/笔。
#    配置: 环境变量 YOUZI_PUSH_WINDOWS, 逗号分隔多段, 如 "09:40-11:00,13:00-14:30";
#          设为 "all" = 恢复全天推送。
PUSH_WINDOWS_RAW = _os.getenv("YOUZI_PUSH_WINDOWS") or "09:40-11:00"


def push_windows() -> list:
    if PUSH_WINDOWS_RAW.strip().lower() == "all":
        return []
    out = []
    for part in PUSH_WINDOWS_RAW.split(","):
        if "-" not in part:
            continue
        a, b = part.split("-", 1)
        ta, tb = parse_hhmm(a), parse_hhmm(b)
        if ta and tb:
            out.append((ta, tb))
    return out


def in_push_window(now: datetime) -> bool:
    ws = push_windows()
    if not ws:
        return True
    t = now.time()
    return any(a <= t < b for a, b in ws)


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


_LOCK_FP = None


def _acquire_lock(force: bool = False) -> bool:
    """单实例锁 — 防并发重复推送 (2026-09-11 事故修复)。

    事故: 旧进程崩溃后 watchdog 拉起新实例, 两个进程并发扫描 → 同一只票 9 秒
    内被推送两次(600184 光电股份), 当日配额 3 被超到 4 只, 网站出现两笔相同
    持仓。用 fcntl.flock(LOCK_EX|LOCK_NB): 进程退出(含崩溃)时内核自动释放,
    不会像 PID 文件那样留下死锁。
    """
    global _LOCK_FP
    import fcntl
    fp = None
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fp = open(STATE_DIR / "youzi_live.lock", "w")
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if fp is not None:
            try:
                fp.close()
            except Exception:
                pass
        if not force:
            print("[锁] 已有 youzi_live 实例在运行 → 本次退出"
                  "(防重复推送/超配额; 确需并发请加 --force)")
            return False
        print("[锁] ⚠ 已有实例运行, --force 强制启动(有重复推送风险)")
        return True
    except Exception:
        return True                 # 锁不可用(非 POSIX) → 不阻断主流程
    fp.write(f"{_os.getpid()} {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    fp.flush()
    _LOCK_FP = fp                   # 保持引用, 否则被 GC 回收 → 锁失效
    return True


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
    ap.add_argument("--min-pct", type=float, default=6.0,
                    help="扫描最小涨幅(%): 校准期降到6以扩大分析样本(打开+6~8%盲区)")
    ap.add_argument("--push-min-pct", type=float, default=8.0,
                    help="推送最低涨幅(%): 扫描阈值可更低(扩分析样本), "
                         "但仅≥此涨幅的 BUY 才进入推送(保护实盘质量)")
    ap.add_argument("--obs-low", type=float, default=6.0,
                    help="观测带下沿(%): 扫描下限, 独立于push-min-pct。 "
                         "[obs-low,push-min-pct)区间的票进'观测带'——仅AI判定+记录, "
                         "不推送/不占配额, 用于两周后校准分析(解决--min-pct被设高时盲区)")
    ap.add_argument("--min-ratio", type=float, default=0.0,
                    help="涨停幅度比例阈值(0=禁用, 主板统一10%)")
    ap.add_argument("--min-vr", type=float, default=4.0,
                    help="最小量比(默认0=不过滤, 已验证为前视无效条件)")
    # 触发时点依据"首次触及9%"分时统计(2888笔): 10:30(+1.66%)/11:00(+1.90%)
    # 两段最优且笔均不降; 开盘半小时内首触仅+0.17%(强势未确认), 尾盘14:00后失效
    ap.add_argument("--trigger-times", default="",
                    help="触发时点(逗号分隔); 空=全天")
    ap.add_argument("--start-time", default="",
                    help="起始推送时点(HH:MM); 之前判定照常记录但不推送/不占配额"
                         "(2026-09-15 复盘: 09:30-09:40 桶全面最差)")
    ap.add_argument("--ai", action="store_true", help="启用 AI 判断层(否决/确认)")
    ap.add_argument("--min-prob", type=float, default=65.0,
                    help="AI 放行硬阈值: prob≥此值才推送(回测基线封板率63.4%)")
    ap.add_argument("--daily-max", type=int, default=3,
                    help="总仓上限(同时持有的最大笔数)")
    ap.add_argument("--daily-new", type=int, default=2,
                    help="每日新买上限(梯队滚动: 必须 < daily-max, 否则隔天才能买)")
    ap.add_argument("--cooldown", type=int, default=10,
                    help="同票AI判定间隔(分钟): 仅去重防刷屏(骏亚式54次), "
                         "不冻结——超过该间隔的SKIP→BUY翻转(如603276隔11分钟)仍会重判")
    ap.add_argument("--no-cache", action="store_true", help="强制重建股票池")
    ap.add_argument("--no-prev-amt", action="store_true",
                    help="跳过昨日成交额基准拉取(快速测试用, 量比可能失真)")
    ap.add_argument("--force", action="store_true",
                    help="已有实例在运行时仍强制启动(有重复推送风险)")
    args = ap.parse_args()
    if not _acquire_lock(force=args.force):
        return 0

    # 每日运行日志: ~/.tradingagents/youzi/logs/YYYY-MM-DD.log
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    sys.stdout = Tee(log_path)
    print(f"───── youzi_live 启动 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
          f"(interval={args.interval}s, 触发={args.trigger_times or '全天'}, "
          f"扫描{args.obs_low}%/推送{args.push_min_pct}%, 观测带{args.obs_low}~{args.push_min_pct}%(不推送), "
          f"prob≥{args.min_prob}, 窗口{PUSH_WINDOWS_RAW}) ─────")

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
            h = pd.read_pickle(MB_DAILY)
            h["date"] = pd.to_datetime(h["date"])   # 兼容字符串日期(pkl 存为 str)
            # 只留今日之前(B8, 2026-09-23 补): 若 mb_daily 盘中更新含当日K,
            # streak/距60日高/RSI 会掺入当日数据 = 前视, 且与回测口径不一致。
            h = h[h["date"] < pd.Timestamp(today)]
            h = h[["code", "date", "close", "high",
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
            h["date"] = pd.to_datetime(h["date"])   # 同上, 兼容字符串日期
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
        # 扫描下限=obs_low(独立, 默认6%): 6~8% 进候选作为'观测带'(仅判定记录不推送);
        # push 仍要求 >= push_min_pct(8%) 保质量。不受 --min-pct 影响
        sigs, st = scan(api, pool, prog,
                        args.obs_low / 100, args.min_vr,
                        args.min_ratio, hist)
        log_blocked(st.get("blocked") or [], now)   # 拦截日志(复盘用)
        save_limit_list(st.get("limit_codes") or [], now)  # 涨停列表(次日情绪指标)
        # 拆分: 主推候选(>=push_min_pct) / 观测候选([obs_low,push_min_pct): 仅判定记录不推送)
        main_sig = [s for s in sigs if float(s.get("pct", 0) or 0) >= args.push_min_pct]
        obs_sig = [s for s in sigs
                   if args.obs_low <= float(s.get("pct", 0) or 0) < args.push_min_pct]
        print(f"[{now.strftime('%H:%M:%S')}] 有效 {st['got']:>4} | 涨停 {st['limit']:>3} | "
              f"封死 {st['sealed']:>3} | 一字 {st['yizi']} | 涨幅不足 {st['lowpct']} | "
              f"量比不足 {st['lowvr']} | 强度不足 {st['lowst']} "
              f"额超 {st['hiamt']} | "
              f"标(高位{st['x_high']} 盘{st['x_mv']} "
              f"换手{st['x_turn']}) → **半路板 {len(main_sig)} 只**"
              + (f" | 观测 {len(obs_sig)} 只({args.obs_low:.0f}%~{args.push_min_pct:.0f}%, 不推送)"
                 if obs_sig else ""))
        # 行情自愈: 交易时段内 0 只有效 = 连接已死(get_security_quotes 静默返回空)
        if st["got"] == 0 and in_session(now.time()):
            print("[warn] 行情断流(有效 0), 强制重连 ...")
            try:
                api.disconnect()
            except Exception:
                pass
            if not api.connect(TDX_HOST, TDX_PORT, time_out=5):
                print("[warn] 重连失败, 下轮重试")
            return
        # 候选标注 skip_bl/冷却状态(区分"仍满足过滤但已被AI闸挡掉的幽灵票")
        _today = now.strftime("%Y-%m-%d")
        _skip_bl = state.get("skip_blacklist", {}).get(_today, {})
        _last_judge = state.get("last_judge", {})
        def _tag(s):
            if s["code"] in _skip_bl:
                return " [拉黑]"
            _l = _last_judge.get(s["code"])
            # last_judge 兼容两种格式: 新 {"a","p","t"} / 旧 iso 字符串
            _lt = _l.get("t") if isinstance(_l, dict) else _l
            if _lt and (now - datetime.fromisoformat(_lt)).total_seconds() < args.cooldown * 60:
                return " [冷却]"
            return ""
        def _fmt(s, label):
            stk = s.get("streak")
            mv = f"{s['mv']:.0f}亿" if s.get("mv") else "—"
            tn = f"{s['turn']:.1f}%" if s.get("turn") else "—"
            dd = f"{s['dd']:.0f}%" if s.get("dd") is not None else "—"
            return (f"    [{label}{s['score']:>3}分] {s['code']} {s['name']:<8} "
                    f"{s['pct']:+6.2f}%  量比{s['vr']:5.1f}  额{s['amt_yi']:>6.2f}亿  "
                    f"{('首板' if stk == 0 else str(stk) + '板后') if stk is not None else '—':<6}"
                    f" {mv:>7}  换手{tn:>6}  距高{dd:>4}{_tag(s)}")
        for s in main_sig[:10]:
            print(_fmt(s, ""))
        for s in obs_sig[:10]:
            print(_fmt(s, "观测"))

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

        # 冷却去重 + 结构性SKIP当日拉黑(2026-09-15: 骏亚科技单日被重复判54次,
        # 其中50次SKIP理由全是"无题材+基本面暴雷"——结构性问题盘中不会变, 重判纯浪费)
        fresh = []
        skip_bl = state.setdefault("skip_blacklist", {}).setdefault(
            now.strftime("%Y-%m-%d"), {})
        for _old in [k for k in state["skip_blacklist"]
                     if k != now.strftime("%Y-%m-%d")]:
            del state["skip_blacklist"][_old]   # 只留当日, 防 state 膨胀
        last_judge = state.setdefault("last_judge", {})
        # ── 代码级退潮闸·30分钟级日内版(2026-09-23 简化): 只保留大面日拦截,
        #    原晋级率条款已被 523 笔格子样本证伪(见 _gate_blocked 注释)。
        #    闸门不再经过"额度", 直接作用到候选(fresh); 配额体系已整体移除。
        _rd = _regime_intraday(now)
        _blocked, _why = _gate_blocked(_rd)
        if _blocked:
            print("    [退潮闸·30分钟级] %s → 暂停推送, 只记判定" % _why)
        # ── 重启恢复(2026-09-22, 600640 事故): 进程挂掉时"已判 BUY 未推送"的判定
        #    随内存蒸发。last_judge 现为 {action, prob, ts} 且每轮原子落盘 →
        #    15 分钟内 BUY 且 prob≥门槛且从未推送过的票: 豁免冷却/拉黑, 本轮重判补推。
        pending = {}
        for _c, _r in last_judge.items():
            if not (isinstance(_r, dict) and _r.get("a") == "BUY"
                    and float(_r.get("p") or 0) >= args.min_prob
                    and _c not in state.get("sent", {})):
                continue
            try:
                if (now - datetime.fromisoformat(_r["t"])).total_seconds() < 900:
                    pending[_c] = _r
            except Exception:
                pass
        for s in (main_sig + obs_sig):
            last = last_judge.get(s["code"])
            _lt = last.get("t") if isinstance(last, dict) else last
            # 冷却仅去重(防骏亚式54次重复刷屏), 不冻结: 超过冷却的SKIP→BUY翻转仍会重判。
            # (2026-09-23 配额移除后不再有"影子模式期间记冷却/恢复解冻"的问题。)
            if _lt and s["code"] not in pending and (
                    (now - datetime.fromisoformat(_lt)).total_seconds()
                    < args.cooldown * 60):
                continue
            if s["code"] in skip_bl and s["code"] not in pending:
                continue                        # 仅静态硬伤(暴雷/ST)当日拉黑; 题材/盘口时变不拉黑
            fresh.append(s)
        if pending:
            _hit = [s["code"] for s in (main_sig + obs_sig) if s["code"] in pending]
            if _hit:
                print("    [恢复] 重启前 BUY 未推送: "
                      + ", ".join(f"{c}(prob={pending[c].get('p')})" for c in _hit)
                      + " → 豁免冷却重判补推")
        # ── 2026-09-22 修订: 不再截断候选数 ──
        # 原逻辑把候选砍到 AI_BATCH_CAP=8(主推优先+观测带补齐), 理由是单批过多会超时。
        # 但截断会漏票(午后候选常 14+ 只, 被砍掉的正是规则分靠后的潜在好票)。
        # 改为: 全部候选一次送判; 若整批失败/返回不全 → 自动拆成 AI_CHUNK 大小分批重试,
        # 保证既不漏票也不整批丢失。排序仍按规则分降序(模型看到的主次顺序不变)。
        fresh.sort(key=lambda s: -float(s.get("score", 0) or 0))
        if not fresh:
            return
        # 候选落盘 → 秒级哨兵(youzi_fast_watch)顺带监控, 产出 fast_book.json 趋势摘要
        try:
            (STATE_DIR / "candidates.json").write_text(json.dumps(
                {"ts": time.time(), "codes": [s["code"] for s in fresh]}),
                encoding="utf-8")
        except Exception:
            pass
        # ── 等哨兵扫一轮再读(2026-09-23 修): ──
        # 哨兵是 3 秒轮询; 若写完 candidates 立刻读 fast_book, 本轮新出现的票哨兵还没
        # 纳入监控 → fast_book 里没有它 → 注入命中 0(日志"注入 0/1"), 即"首次出现的票
        # 永远拿不到秒级证据"。这里等 FAST_BOOK_WAIT 秒(一轮 60s, 多等几秒无影响)。
        try:
            time.sleep(FAST_BOOK_WAIT)
        except Exception:
            pass
        # 注入秒级盘口摘要(30秒动量/卖一量变化/触板次数): 让 AI 看"变化率"而非单点快照
        # 2026-09-22 复盘修订: 新鲜度窗口 30s→FAST_BOOK_MAX_AGE, 且注入必须可观测 —
        # 快照龄/命中数落日志, 每只候选命中的证据原文随判定行落日志(复盘可见模型看到了什么)。
        _fb_hits, _fb_age = 0, None
        try:
            fb = json.loads((STATE_DIR / "fast_book.json").read_text(encoding="utf-8"))
            _fb_age = time.time() - float(fb.get("ts") or 0)
            if 0 <= _fb_age < FAST_BOOK_MAX_AGE:
                for s in fresh:
                    b = (fb.get("data") or {}).get(s["code"])
                    if b:
                        _fb_s = _fmt_fast_book(b)
                        if _fb_s:
                            s["fast_book"] = _fb_s
                            s["fast_book_age"] = int(_fb_age)
                            _fb_hits += 1
        except Exception:
            pass
        if fresh:
            if _fb_age is None:
                print("    [秒级盘口] fast_book.json 缺失/异常, 本轮无秒级注入")
            elif not (0 <= _fb_age < FAST_BOOK_MAX_AGE):
                print(f"    [秒级盘口] 快照龄{int(_fb_age)}s ≥ {FAST_BOOK_MAX_AGE}s, 本轮不注入(哨兵未跑?)")
            else:
                print(f"    [秒级盘口] 快照龄{int(_fb_age)}s, 注入 {_fb_hits}/{len(fresh)} 只候选")

        # ── AI 决策层: 规则负责召回, 模型负责判断"能不能封住板" ──
        if args.ai:
            try:
                from youzi_ai import decide
                # 情绪周期(日内30分钟级): 复用本轮闸门已算好的实时数据
                if _rd:
                    _reg = ("昨日涨停 %d 只 → 实时晋级 %d(%.0f%%) | 均涨 %+.2f%% | "
                            "30分钟斜率: 晋级率%+.0fpp/均涨%+.1fpp | 闸门: %s"
                            % (_rd["n"], _rd["sealed"], _rd["rate"], _rd["avg"],
                               _rd["rate_slope"], _rd["avg_slope"],
                               "拦" if _blocked else "放行"))
                else:
                    _reg = _regime_text(_regime_data(now))
                _t, _l = int(st.get("touch") or 0), int(st.get("limit") or 0)
                if _reg and _t + _l > 0:
                    _reg += ("\n  今日盘中: 触板未回封 %d 只 / 封死 %d 只 → 盘中炸板率 %.0f%%"
                             % (_t, _l, 100.0 * _t / (_t + _l)))
                mkt = {"n_limit": st["limit"], "pool_n": len(pool),
                       "max_st": market_max_streak(st.get("limit_codes") or [], hist),
                       "regime": _reg,
                       "emotion": emotion_block(now)}   # 接力赚钱效应(证据维度)
                decs, aist = decide(fresh, mkt, now, api=api,
                                    thr=args.obs_low,
                                    min_prob=args.min_prob)
                # 分批兜底: 不限候选数后, 单批过大可能生成超时/限流 → 整批返回空。
                # 此时按 AI_CHUNK 拆分重试未返回的候选, 宁可多调几次也不丢票。
                if aist != "ok" or len({d.get("code") for d in decs}) < len(fresh):
                    _by = {d.get("code"): d for d in decs if d.get("code")}
                    _miss = [s for s in fresh if s["code"] not in _by]
                    if _miss:
                        print(f"    [AI] 整批 {len(fresh)} 只仅回 {len(_by)} 只 "
                              f"→ 拆批重试 {len(_miss)} 只(每批 {AI_CHUNK})")
                        for i in range(0, len(_miss), AI_CHUNK):
                            _sub = _miss[i:i + AI_CHUNK]
                            try:
                                _d2, _st2 = decide(_sub, mkt, now, api=api,
                                                   thr=args.obs_low,
                                                   min_prob=args.min_prob)
                            except Exception as _e:
                                print(f"    [AI] 拆批 {i // AI_CHUNK + 1} 失败: {_e}")
                                continue
                            if _st2 == "ok":
                                aist = "ok"
                                for d in _d2:
                                    if d.get("code"):
                                        _by[d["code"]] = d
                        decs = list(_by.values())
                by = {d["code"]: d for d in decs}
                for s in fresh:
                    s["ai"] = by.get(s["code"])
                # 当日拉黑仅限静态硬伤(业绩暴雷/暴亏/ST/立案/退市等盘中不会变);
                # 题材/孤板/杂毛/无联动属板块共振(时变): 别的票封板即可翻转, 不拉黑,
                # 留待冷却后由模型重判(603276: 13:02 SKIP→13:13 板块联动成型→BUY且封板)
                _STRUCT = ("暴雷", "暴亏", "业绩暴", "ST", "退市",
                           "立案", "警示", "监管")
                for d in decs:
                    # last_judge 升级(2026-09-22): 存 {action, prob, ts} 供重启恢复;
                    # 配合下方每轮原子落盘, 进程挂掉不再丢判定(旧行为只推时写盘)
                    last_judge[d["code"]] = {"a": d.get("action", "SKIP"),
                                             "p": d.get("prob", 0),
                                             "t": now.isoformat()}
                    if d.get("action") != "SKIP":
                        continue
                    rs = str(d.get("reason", "")) + str(
                        (d.get("judgement") or {}).get("题材", ""))
                    _hit = [k for k in _STRUCT if k in rs]
                    if _hit:
                        # 2026-09-22 复盘修订: 拉黑必须留痕 — 落盘与日志都带命中的结构词。
                        # 否则无法事后复盘是否误伤(如 reason 干净但 runtime 题材文本
                        # 含结构词的情况, 今天根本查不出 002259 为何被拉黑)
                        skip_bl[d["code"]] = (str(d.get("reason", ""))[:45]
                                              + "｜命中:" + ",".join(_hit))[:60]
                        print(f"    [拉黑] {d['code']} 命中结构词: {','.join(_hit)}")
                log_judgements(fresh, now, mkt)
                log_obs_judgements(fresh, now, mkt, args.push_min_pct)
                _atomic_json(STATE_FILE, state)   # 每轮落盘 last_judge/sent(重启恢复依赖)
                for s in fresh:
                    a = s.get("ai") or {}
                    act = a.get("action", "无判定")
                    head = (f"    [AI] {s['code']} {s['name']}: {act} "
                            f"prob={a.get('prob', '—')}")
                    sc = a.get("scores") or {}
                    if sc:
                        head += "  [" + " ".join(
                            f"{k}{v}" for k, v in sc.items()) + "]"
                    jm = a.get("judgement") or {}
                    if jm:
                        head += "\n        " + " | ".join(
                            f"{k}:{v}" for k, v in jm.items())
                    if a.get("reason"):
                        head += f"\n        → {a['reason']}"
                    if s.get("fast_book"):
                        # 复盘可见性(2026-09-22): 展示实际注入 prompt 的秒级盘口证据原文
                        head += (f"\n        [秒级盘口·已注入prompt·数据龄"
                                 f"{int(s.get('fast_book_age') or 0)}s] {s['fast_book']}")
                    print(head)
                if aist == "ok":
                    # AI 正常判定(含"全放弃"=空列表) → 严格执行三道闸
                    before = len(fresh)
                    fresh = [s for s in fresh
                             if (s.get("ai") or {}).get("action") == "BUY"
                             and float((s.get("ai") or {}).get("prob", 0) or 0)
                             > args.min_prob
                             and (s.get("ai") or {}).get("scores", {}).get("盘口", 0) >= MIN_PAN
                             and (s.get("ai") or {}).get("scores", {}).get("位置", 0) >= MIN_POS
                             and float(s.get("pct", 0) or 0) >= args.push_min_pct
                             # ── 同票当日只推一次(修复 000700 重复推送):
                             #    state["sent"] 跨轮/跨重启持久化, 已推过的票直接剔除,
                             #    既不重复打扰也不占用每日配额 ──
                             and s["code"] not in state.get("sent", {})]
                    # ── 推送窗口(2026-09-23): 窗口外判定照常落盘, 不推送不占配额 ──
                    st_t = parse_hhmm(args.start_time)
                    if st_t and now.time() < st_t:
                        print(f"    → 未到起始推送时点 {args.start_time} "
                              f"(判定已记录, 不推送/不占配额)")
                        return
                    if not in_push_window(now):
                        print(f"    → 不在推送窗口 {PUSH_WINDOWS_RAW} "
                              f"(判定已记录, 不推送/不占配额)")
                        return
                    # ── 大面日闸直接作用候选(2026-09-23): 配额体系已移除, 不再设
                    #    每日3笔/每日新买2 上限 —— 有判定过闸就推, 质量靠前级闸门 ──
                    if _blocked:
                        print("    [退潮闸] %s → 本轮不推送" % _why)
                        fresh = []
                    # ── 推送前二次校验(2026-09-16 新增): 实时价已达涨停价=已封死,
                    #    半路打板必须封板前介入, 封死后散户买不到 → 推送纯浪费每日配额
                    #    → 直接跳过且不占配额(查询失败=未知则不拦截, 避免误杀正常票)
                    if fresh and do_push:
                        _q = {q["code"]: q for q in _quotes_fallback(
                            [s["code"] for s in fresh])}
                        _sc = {s["code"] for s in fresh
                               if (q := _q.get(s["code"])) is not None
                               and float(q["price"]) >= float(
                                   q["last_close"]) * 1.1 - 0.005}
                        if _sc:
                            for s in fresh:
                                if s["code"] in _sc:
                                    print(f"    → 已封死(涨停价 "
                                          f"{float(_q[s['code']]['price']):.2f}), "
                                          f"跳过 {s['code']} {s['name']} 不占配额")
                            fresh = [s for s in fresh if s["code"] not in _sc]
                    log_pushed(fresh, now)      # 实际推送的, 线上展示用
                    print(f"    → AI 过滤: {before} → {len(fresh)} 只 "
                          f"(prob≥{args.min_prob:.0f}, 窗口{PUSH_WINDOWS_RAW})")
                else:
                    print("    → AI 调用失败, 本次不推送(宁可错过)")
                    fresh = []
            except Exception as exc:
                # ⚠ 旧行为: 异常后 fresh 仍是规则层原始候选 → 跳出 if 后直接推送,
                #   既不判 prob 也不去重、不扣配额("降级即裸推")。AI 挂了宁可不推。
                print(f"    [AI] 异常, 不推送(宁可错过): {exc}")
                fresh = []
        st_t = parse_hhmm(args.start_time)
        if st_t and now.time() < st_t and fresh:
            print(f"    → 未到起始推送时点 {args.start_time}, 仅记录")
            return
        if fresh and not in_push_window(now):
            print(f"    → 不在推送窗口 {PUSH_WINDOWS_RAW}, 仅记录")
            fresh = []
        if fresh and do_push:
            # ── 推送前二次情绪确认(2026-09-23) ──
            # AI 批量判定耗时 15~30s, 判定时刻情绪正常、推送时刻可能已突变(炸板/跳水)。
            # 真正下单(推送)前再实时查一次, 若此刻已退潮且未修复 → 撤销, 只留判定不推。
            # 成本: 一次腾讯快照(0.1s), 与"用时再算"同源。
            try:
                _rd2 = _regime_intraday(datetime.now(), force=True)
                _b2, _w2 = _gate_blocked(_rd2)
                if _b2:
                    print(f"    [推送前二次确认] 情绪已转{_w2} → 撤销本次推送, 仅记录判定")
                    fresh = []
            except Exception:
                pass
        if fresh and do_push:
            title, text = fmt(fresh, st["limit"], now)
            ok = push(title, text)
            print(f"    → 推送 {len(fresh)} 只: {'成功' if ok else '失败'}")
            for s in fresh:
                state["sent"][s["code"]] = now.isoformat()
            if slot != "ALL":
                fired.append(key)
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            # 只在这里写一次(2026-09-22 起改原子写; 另 AI 判定轮也有落盘兜底):
            # 抛异常后中断 → sent/last_judge 都未落盘 → 冷却失效(同票重复推)
            _atomic_json(STATE_FILE, state)
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
                _atomic_json(POSITIONS, pos)   # 原子写: 防 sell_ai 每分钟轮询读到半截文件
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
