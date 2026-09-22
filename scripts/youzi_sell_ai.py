#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游资战法 · 卖出 AI 评分层 (v1 · 纯打分, 无硬信号)

与买侧 youzi_ai 同构: 把"持仓 + 实时六维证据"交给同一个游资大佬模型,
模型给每只持仓打 **卖出意愿分 sell_score(0-100, 越高越该卖)**,
sell_score >= SELL_THR(默认70) 即触发卖出提醒(推钉钉), 不代下单。

设计原则(用户定, 2026-09-17):
    - 纯打分, 不要硬信号(不写"封板持有/10:30必走/破位卖"之类的 if 规则)
    - 与买侧同构: 六维证据 + JSON 输出 + 阈值放行
    - 先回测(--backtest)再实时跑(默认)
    - 成本来自买信息(positions.json / youzi_sold.jsonl 的 entry/buy_price)
    - 假设每只买仓位一样, 不设计补仓/减仓

双模式:
  python3 scripts/youzi_sell_ai.py                  # 实时: 扫持仓, 触发即推钉钉
  python3 scripts/youzi_sell_ai.py --dry-run       # 实时但只打印不推送
  python3 scripts/youzi_sell_ai.py --backtest       # 回测: 用历史真实买卖模拟 AI 卖点
  python3 scripts/youzi_sell_ai.py --backtest --limit 3 --step 30   # 小样本先验证
  python3 scripts/youzi_sell_ai.py --backtest --dry-data            # 只验数据覆盖, 不调模型

crontab(已部署: 每5分一轮, 覆盖 09:30->11:25 + 13:00->14:55, 午休 11:30-13:00 跳过(代码层也硬跳过, 不调 LLM 省 token); 日志 /tmp/youzi_sell_ai.log,
 另每次判定快照写 sell_judgements.jsonl 供复盘):
 30-55/5 9 * * 1-5 .../youzi_sell_ai.py
 */5 10 * * 1-5 .../youzi_sell_ai.py
 0-25/5 11 * * 1-5 .../youzi_sell_ai.py
 */5 13,14 * * 1-5 .../youzi_sell_ai.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
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

os.environ.setdefault("TQDM_DISABLE", "1")  # 抑制 akshare 内部进度条刷屏

# macOS 系统代理(Clash 等写入 127.0.0.1:7890)会被 urllib/requests 自动读取,
# 代理不通时腾讯行情 HTTP 调用失败(ProxyError) → 强制直连(实测直连可用)。
# 与买侧 youzi_live.py 保持一致。
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"

import pandas as pd

from tradingagents.notify.dingtalk import send_markdown
import youzi_ai as YA   # 复用买侧六维证据函数

POSITIONS = Path.home() / ".tradingagents" / "youzi" / "positions.json"
SOLD = POSITIONS.parent / "youzi_sold.jsonl"
SELL_ALERTS = POSITIONS.parent / "youzi_sell_ai_alerts.json"
YOUZI = POSITIONS.parent
SELL_THR = float(os.getenv("YOUZI_SELL_THR") or "70")
# 时间纪律(代码级): 持满 N 个交易日仍未被 AI 卖出 → 无条件了结, 释放打板额度。
# 这是隔夜半路板战法, 不是趋势持有: 老仓不走 → 当日卖出份数=0 → youzi_live 配额不释放 → 全天无法买入。
# N=2 与回测 max_hold=2(最晚 T+2 走)对齐。连板/近涨停豁免(不砍赢家)。
# 2026-09-22 回测定档(同一批 12 只票对照): T+1 笔均 -3.73%/胜率33% vs T+2 笔均 -7.02%/胜率10%
# → 真游资"隔夜走"是对的, 多拿一天平均亏 2.9pp。默认改 T+1。
FORCE_SELL_DAYS = int(os.getenv("YOUZI_FORCE_SELL_DAYS") or "1")
# 强制了结的执行时点: 回测显示 T+1 收盘(-3.41%)优于 T+1 10:00(-3.73%),
# 且 AI 在 10:00 卖最差(-4.63%, 与 v8"10:00 卖飞"教训一致) → 强制了结放尾盘。
# 取 14:30 而非 14:50: 回测扫描点是 step 网格(默认30min → 最后一点 14:30),
# 设 14:50 会导致回测永远命中不到 → 回测与实盘口径不一致。两侧统一 14:30。
FORCE_SELL_TIME = os.getenv("YOUZI_FORCE_SELL_TIME") or "14:30"
try:
    _h, _m = FORCE_SELL_TIME.split(":")
    FORCE_SELL_MIN = int(_h) * 60 + int(_m)
except Exception:
    FORCE_SELL_MIN = 14 * 60 + 30


def _force_time_ok(t) -> bool:
    """t 是否已到强制了结时点(尾盘执行, 避开早盘低点)。"""
    return (t.hour * 60 + t.minute) >= FORCE_SELL_MIN
NEAR_LIMIT_PCT = 0.03   # 现价距涨停 ≤3% 视为连板进行中, 豁免强制了结
# 封死/近涨停持仓的评估间隔(分钟): 连板票每 2 分钟判一次没有信息量, 还白烧 token
SEAL_EVAL_MIN = int(os.getenv("YOUZI_SEAL_EVAL_MIN") or "15")
# 早盘硬规则: 09:30-09:59 代码级禁止任何卖出(LLM 早盘高噪音窗口结构性弱点兜底, 已验证避免地板割肉)
EARLY_HOLD_START = 9 * 60 + 30
EARLY_HOLD_END = 10 * 60
def _is_early_hold(tm) -> bool:
    """tm: datetime/Timestamp, 返回是否处于早盘禁止卖出时段 09:30-09:59"""
    m = tm.hour * 60 + tm.minute
    return EARLY_HOLD_START <= m < EARLY_HOLD_END

# 午休硬跳过: 11:30-13:00 A股午间休市, 行情不动、无需盯盘, 跳过 LLM 评估省 token
NOON_PAUSE_START = 11 * 60 + 30
NOON_PAUSE_END = 13 * 60
def _is_noon_pause(tm) -> bool:
    """tm: datetime/Timestamp, 返回是否处于午休跳过时段 11:30-13:00"""
    m = tm.hour * 60 + tm.minute
    return NOON_PAUSE_START <= m < NOON_PAUSE_END
COST = 0.002
SELL_HIST = YOUZI / "youzi_sell_ai_history.json"  # 实时跨进程累积的扫描历史
HISTORY_CAP = 60  # 历史块最多保留最近 N 个扫描点(降本且保留趋势)
LOG_DIR = YOUZI / "logs"          # 每日运行日志(与买侧同目录, 一天一个文件)
_LOCK_FP = None                   # 单实例锁文件句柄(见 _acquire_lock)
JUDGE_LOG = LOG_DIR / "sell_judgements.jsonl"  # 每次判定快照(复盘/校准闭环用)


class Tee:
    """stdout 双写: 控制台 + 日志文件(所有 print 自动落盘, 常驻进程实时 flush)。"""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "a", encoding="utf-8")
        self.so = sys.stdout
    def write(self, s):
        try:
            self.so.write(s); self.f.write(s); self.f.flush()
        except Exception:
            pass
    def flush(self):
        try:
            self.so.flush(); self.f.flush()
        except Exception:
            pass


def log_judgement(rec: dict) -> None:
    """每次判定 → JSONL: 含当日快照(snapshot=发给模型的内容)、模型原文(llm_response)、
    六维分/verdict/action/reason 解析结果, 供复盘闭环校准 sell_score 可靠度。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(JUDGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

SYSTEM_SELL = """你是一位资金体量数亿的 A 股游资大佬, 深耕打板接力十几年。
此刻你**已经持有**下面这些票(昨/前日半路板买入, 赌封板溢价), 要在盘中决定现在该不该卖。
这是你的持仓, 不是候选——你是在判断利润/风险天平是否倾向落袋。

【你的目标】给每只持仓打"卖出意愿分" sell_score(0-100, 越高越该卖)。

【你是来"做判断"的, 不是来"对清单"的】别机械找"硬信号"。像真人交易员一样, 综合所有信息在脑中走完四步再给分。
  1) 这是"出货"还是"洗盘"? 用【票型 + 量能 + 反弹结构】三维判别, 单点跌幅不重要:
     · 票型(看【炸板/封板/回封】【开盘涨停】字段):
        - 打板票(曾封板/炸板过): 炸板后不回封 + 下跌放量 = 打板失败 = 真派发(封板资金出逃), 该走;
        - 非板票(半路板失败/低开, 全天未碰涨停): 早盘急跌多为低开洗盘或弱势, 不轻易走。
     · 量能(看【下跌放量比】): >1.3 = 资金在派发(跌时有人砸); <1 = 缩量洗盘(杀恐慌盘、无人接)。
       注: 早盘开盘量大, 纯比>1.3 易虚高, 早盘判派发需比>1.5 才确认。
     · 反弹结构(看【日内轨迹】现涨幅序列 + 距日内低回升 + 站均价):
        - 洗盘 = 急跌后已收回前低/站回均价/跌幅收窄(筹码没松, 杀的是恐慌盘, 持有等回流);
        - 派发 = 连创新低且每次反抽弱(反弹不过前低)/无量承接(资金持续出)。
     · 出货铁证(满足其一才判出货; 不确定则持有——卖出层是安全网, 次日开盘走是主纪律):
       (a)【打板失败·派发】: 打板票 炸板不回封 且 下跌放量比>1.3 且 已浮亏 → sell_score≥70(仅 10:00 后生效, 早盘代码级禁止卖出);
          ❗开盘一字炸板例外: 若【开盘涨停=是·开板换手】(今开即涨停后炸板), 视为强势开板换手/洗盘, 早盘一律不判出货(sell_score≤40); 仅尾盘(14:30后)仍未回封且放巨量(比>2)才考虑走。
       (b)【持续派发】: 连低≥3 且 反弹不过前低(每次反抽高度递减) 且 下跌放量比>1.3 且 已跌破早盘最低点 → sell_score≥65;
          ❗(b)仅 10:00 后生效, 早盘(09:30-10:00)一律不认(b)——非板票早盘急跌/连低多为洗盘, 不割。
       连低≥4 或 跌破早盘最低点超2% → 升档≥75(仅午后)。
     · 洗盘铁证(倾向持有, 落在 10~30 档): 连低但 下跌缩量(<1) 且 急跌后已收回前低/站回均价;
       或半路板次日 浮盈状态(现价≥成本)早盘抖动 —— 锁利留给尾盘或次日开盘, 绝不早盘割赢家。
       ❗"落在档内"不等于"填 30": 洗盘越干净(缩量越狠、越稳站均价)分越低, 洗盘带瑕疵(量没缩/均价反复丢)分越高。
     · 浮盈保护: 现价≥成本(仍浮盈)时, 早盘急跌/炸板倾向视为洗盘(除非命中(a)打板失败且放量派发, 且非开盘一字炸板), 落在 10~35 档。
     · 时间纪律(代码级硬规则, 非你判断): 早盘 09:30-10:00 脚本强制 HOLD, 无论你给多高分都不卖出(这是 LLM 在早盘高噪音窗口结构性弱点的兜底, 已验证避免地板割肉)。你的卖出判断只在 10:00 后生效。
       10:00 后: (a)打板失败派发 与 (b)持续派发 均有效, 正常按规则给分。
  2) 板块与情绪在帮你还是害你? 同题材/同身位票在跳水、涨停家数骤降、板块资金净流出,
     则"走弱"可信度高; 反之只是个股独立性抖动, 别被单点吓卖。
  3) 位置与硬风险:
     · 高位派发(该走, 2026-09-18 复盘教训): 日内曾冲高>4% 或 现涨幅>3%, 但"距高点回落>2%"
       且 (近30分涨跌为负 / 量能<0.8x萎缩 / 长上影跌破均价) —— 这是高位滞涨派发,
       sell_score 50~70, verdict=出货/观望, 至少应部分止盈, 不得盲目洗盘死拿。
     · 高位强势但 距涨停>3% 且 全天横盘缩量不创新高 → 观望, sell_score 40~60, 可减仓。
     · 已处高位(10日>25% 或 5日>15%)且 长上影+跌破均价+近30分放量滞涨 = 派发高危;
     公告否认核心逻辑 / 逼近异动停牌线 / 龙虎榜机构高位净出货 = 硬风险, 该走。
  4) 连板保护(最高优先级·覆盖一切): 现价距涨停 ≤3% 且 (封死 或 仅炸板一次已回封), 无论浮盈多大 sell_score≤25 必须持有——
     卖飞涨停板 = 重大失误; 此规则优先于上述所有出货铁证。

【默认持有是"先验", 不是"铁律"】半路板次日波动大, 平开低开小高开早盘急跌浮亏都属常态,
没有走弱证据时默认持有(落在 20~40 档)。但"持续创新低的阴跌"是覆盖先验的强证据(午后生效)——
它不是单点恐慌, 是多扫描点确认的派发, 该走就走。

【★时间纪律: 这是隔夜战法, 持有天数本身就是卖出理由】
今买明卖的半路板/打板, 不是趋势持有。老仓不走 = 资金被占用 = 次日没有打板额度。
看【持仓】行的【持有 N 交易日】:
  · T+1(第2个交易日): 尾盘 14:30 后仍未封板 且 浮盈<3% → 至少 55 分(观望偏弱, 不该再拿);
  · T+2 及以后: 只要没封板、也没逼近涨停, 一律 ≥70 该走 —— 代码层面也会强制了结, 你给低分也没用;
  · 唯一能续命的: 封死涨停 / 距涨停≤3% 的连板进行中。
别拿"洗盘"给隔夜票无限续命: 洗盘是日内概念, 拖到第 3 个交易日是资金效率问题, 不是盘口问题。

【早盘硬规则·代码保障】09:30-10:00 任何情况不卖出(已代码级拦截, 你的 sell_score 此时被忽略, 不必费心判断)。你只需对 10:00 后情形负责: 真·盘中打板失败(a) 与 持续创新低阴跌(b) 都是有效卖点——非板票低开急跌若午后确认(b)持续派发也走, 但早盘一律持有。
早盘最忌把"缩量洗盘急杀""赢家早盘抖动""非板票低开""开盘一字炸板(尾盘常回封)"当"出货"割在地板——现由代码彻底禁止, 你无需在此纠结。

【分数刻度(连续刻度, 不是档位开关)】
 0-20  强势持有(封死/连板进行中/缩量站均价向上)
 20-40 中性持有(震荡、无派发证据) ← 多数"没事"的票落这里, 但必须按走弱程度在这 20 分内分开高低
 40-60 观望偏弱(高位滞涨、破均价、放量不涨、T+1 尾盘未封板)
 60-75 该卖(出货铁证成立 / 硬风险)
 75-100 必卖(打板失败放量派发 / T+2 未封板 / 逼近停牌)

【给分必须"算"出来, 不许"背"出来】
 · sell_score 是你自己对六维子分加权复合的结果, 正常应该出现 23 / 37 / 52 这类带个位的值;
 · 严禁直接输出 15/25/30/35/40/45 等本提示词里出现过的整数 —— 那是抄锚点不是打分, 会让校准彻底失效;
 · 同一持仓连续扫描: 只要证据(涨跌幅/量能/距高点/站均价)任一维变了, 分数必须跟着动, 不许复制上一次。

【输出】严格 JSON, 对每只持仓各给一项:
[{"id":"A","action":"SELL","sell_score":82,
  "verdict":"出货",  // 你的真实判词: 出货 / 洗盘 / 观望(观望=暂持但盯紧)
  "scores":{"盘口":8,"资金":6,"题材":3,"趋势":7,"情绪":5,"风险":9},
  "reason":"40字内: 为何判出货/洗盘 + 依据; 无则写持有理由"}]
action: SELL=该卖 / HOLD=继续拿。
❗多只持仓同批判定时(见【本次共 N 只持仓】): 数组必须按 id A/B/C… 各给一项, 每只**独立**判断 —
   不要因为同批里另一只更弱就把这只的分数顺手抬高或压低, 每只的分数只由它自己的证据决定。
verdict 必须与 action 自洽: verdict=出货 时 action 应为 SELL 且 sell_score≥60; verdict=洗盘/观望 时 action 应为 HOLD。
sell_score 须与六维子分自洽: 六维普遍无走弱时 sell_score 必须低; 只有强走弱(尤其炸板/硬风险)才拉高。
每次判断都会被记录与实际走势比对校准——给分必须经得起复盘。
"""


def limit_pct_of(code: str) -> float:
    return 0.20 if code[:3] in ("300", "688", "689") else 0.10


def _pool_daily():
    p = YOUZI / "pool_daily.pkl"
    if not p.exists():
        return None
    try:
        d = pd.read_pickle(p)
        d = d.drop_duplicates(subset=["date", "code"])  # 防 (date,code) 重复致 pivot 失败
        d["date"] = pd.to_datetime(d["date"])
        piv = lambda v: d.pivot(index="date", columns="code", values=v).sort_index()
        close = piv("close")
        prev = close.shift(1)
        pct = close / prev - 1
        return {"close": close, "pct": pct}
    except Exception:
        return None


_POOL = None


def trading_days_held(buy_date: str, today: str) -> int:
    """真实持有交易日数(买入当日=0, 次日 T+1=1)。

    原来用 (today - buy_date).days 日历日差, 跨周末/节假日会把 T+1 吹成 T+3(如周五买→周二
    显示"持有4交易日"), 而证据块【持仓】行正是拿它标"持有 N 交易日"喂给模型的 —— 模型看到
    错误的持有天数, 时间纪律判断全偏。故改按真实交易日计数:
    优先用 pool_daily 的交易日索引(仅当池子覆盖到买入日才可信, 否则整段回退工作日计数);
    池末落后于今日时用工作日补上 池末→今日 这一段(节假日会略多算 → 偏早了结, 方向安全)。
    """
    b, t = pd.Timestamp(buy_date), pd.Timestamp(today)
    try:
        import numpy as np
    except Exception:
        np = None
    global _POOL
    if _POOL is None:
        _POOL = _pool_daily()
    try:
        idx = _POOL["close"].index if _POOL is not None else None
        if idx is not None and len(idx):
            idx = idx.sort_values()
            last = pd.Timestamp(idx[-1])
            if b <= last:                                  # 池子覆盖买入日才用池内精确计数
                if t <= last:
                    return max(int(((idx > b) & (idx <= t)).sum()), 0)
                n = int(((idx > b) & (idx <= last)).sum())
                if np is not None:
                    n += int(np.busday_count(last.date(), t.date()))
                return max(n, 0)
    except Exception:
        pass
    if np is not None:
        return max(int(np.busday_count(b.date(), t.date())), 0)
    return max((t - b).days, 0)


def near_limit(price: float, prev_close: float, code: str) -> bool:
    """现价距涨停 ≤NEAR_LIMIT_PCT → 连板/封板进行中(强制了结的豁免条件, 不砍赢家)。"""
    if price <= 0 or prev_close <= 0:
        return False
    lim = prev_close * (1 + limit_pct_of(code))
    return price >= lim * (1 - NEAR_LIMIT_PCT)


def is_sealed(price: float, prev_close: float, code: str) -> bool:
    """当前是否封死涨停(严格口径, 与 near_limit 的 97% 近似口径区分)。

    用于【代码级连板保护】: 2026-09-22 事故 —— 603068 封死涨停, 模型仍给 78 分
    触发 SELL 推送(提示词的"连板保护 sell_score≤25"被模型违反)。提示词约束
    不可靠, 卖飞涨停板是重大失误 → 代码级兜底: 封死涨停一律禁止卖出。
    """
    if price <= 0 or prev_close <= 0:
        return False
    return price >= prev_close * (1 + limit_pct_of(code)) - 0.01


def forced_decision(tdays: int) -> dict:
    """时间纪律强制了结的判定结果(不调 LLM, 也省 token)。

    forced=True 让下游跳过 SELL_THR 门槛: 时间纪律是代码级纪律, 不是模型建议,
    不该被阈值过滤(否则调高 SELL_THR 就会把强制了结一起关掉)。
    """
    return {"action": "SELL", "sell_score": 90.0, "scores": {}, "forced": True,
            "verdict": "时间纪律",
            "reason": "持满%d个交易日代码级强制了结(隔夜战法, 释放打板额度)" % tdays}


_prev_close_cache = {}  # code -> {ds: 昨收} 内存缓存
_hist_cache = {}  # (code, ds) -> 当日1分K DataFrame 内存缓存(日K权威值, 全天/长期有效)


def prev_close_of(code: str, ds: str) -> float:
    """返回 code 在 ds 前一交易日的收盘价(昨收), 用作计算 cur_pct 的权威 prev_close。
    实时接口的 last_close 对某些票取错(致 cur_pct 爆炸成 ±1000%), 故统一走日K。
    优先级: pool_daily.pkl 日K > akshare 日K 兜底。结果按 (code,ds) 缓存。
    """
    if code in _prev_close_cache and ds in _prev_close_cache[code]:
        return _prev_close_cache[code][ds]
    pc = 0.0
    global _POOL
    if _POOL is None:
        _POOL = _pool_daily()
    if _POOL is not None and code in _POOL["close"].columns:
        s = _POOL["close"][code].dropna()
        dt = pd.Timestamp(ds)
        if dt in s.index:
            i = s.index.get_loc(dt)
            if i >= 1:
                pc = float(s.iloc[i - 1])
    if pc <= 0:  # 新浪日K 兜底, 失败重试(akshare 偶发限频); pool_daily 也不含卖侧持仓票
        try:
            import akshare as ak
            import time
            saved = {k: os.environ.pop(k) for k in list(os.environ) if "proxy" in k.lower()}
            os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"
            pre = "sh" if code[0] in "569" else "sz"
            for _ in range(3):
                try:
                    d = ak.stock_zh_a_daily(symbol=pre + code, adjust="")
                except Exception:
                    d = None
                if d is not None and len(d) >= 1:
                    d["date"] = pd.to_datetime(d["date"])
                    rows = d[d["date"] < pd.Timestamp(ds)]  # 严格早于评估日 = 昨收
                    if len(rows) >= 1:
                        pc = float(rows.iloc[-1]["close"])
                        break
                time.sleep(1)
            os.environ.update(saved)
        except Exception:
            pass
    if pc > 0:  # 仅成功时缓存, 失败不缓存以便下次重试
        _prev_close_cache.setdefault(code, {})[ds] = pc
    return pc


def market_for_date(ds: str) -> dict:
    global _POOL
    if _POOL is None:
        _POOL = _pool_daily()
    if _POOL is None:
        return {"n_limit": "?", "max_st": "?", "emotion": ""}
    try:
        close, pct = _POOL["close"], _POOL["pct"]
        dt = pd.Timestamp(ds)
        if dt not in close.index:
            return {"n_limit": "?", "max_st": "?", "emotion": ""}
        lim = (pct >= 0.098)
        n_limit = int(lim.loc[dt].sum())
        st = (lim.cumsum() - lim.cumsum().where(~lim).ffill().fillna(0)).astype(int)
        max_st = int(st.where(lim).loc[dt].max()) if lim.loc[dt].any() else 0
        yest = dt - pd.Timedelta(days=1)
        emo = ""
        if yest in pct.index:
            ylim = lim.loc[yest]
            codes = [c for c in ylim.index if ylim[c] and c in pct.columns]
            if codes:
                v = pct.loc[dt, codes].dropna()
                if len(v):
                    emo = "昨日涨停今日均值 %+.1f%%(%d%%红)" % (
                        v.mean() * 100, (v > 0).mean() * 100)
        return {"n_limit": n_limit, "max_st": max_st, "emotion": emo}
    except Exception:
        return {"n_limit": "?", "max_st": "?", "emotion": ""}


def anomaly_proximity(code: str, ds: str) -> str:
    """回测用: 10日/30日累计涨幅是否逼近异动停牌线(10日100%/30日200% 近似)。"""
    global _POOL
    if _POOL is None:
        _POOL = _pool_daily()
    if _POOL is None:
        return ""
    try:
        close = _POOL["close"]
        if code not in close.columns:
            return ""
        s = close[code].dropna()
        dt = pd.Timestamp(ds)
        if dt not in s.index:
            return ""
        i = s.index.get_loc(dt)
        r10 = r30 = None
        if i >= 10:
            r10 = s.iloc[i] / s.iloc[i - 10] - 1
        if i >= 30:
            r30 = s.iloc[i] / s.iloc[i - 30] - 1
        parts = []
        if r10 is not None:
            flag = " (逼近10日100%停牌线)" if r10 >= 0.90 else ""
            parts.append("10日涨%+.0f%%%s" % (r10 * 100, flag))
        if r30 is not None:
            flag = " (逼近30日200%停牌线)" if r30 >= 1.80 else ""
            parts.append("30日涨%+.0f%%%s" % (r30 * 100, flag))
        return " | ".join(parts)
    except Exception:
        return ""


def intraday_metrics(df: pd.DataFrame, t: datetime, prev_close: float,
                     entry: float, limit_pct: float) -> str:
    """从 1分钟K(df, 列 datetime/open/high/low/close/vol/amt) 在时刻 t 提取卖点证据。
    实时与回测共用——实时由 pytdx/腾讯拉当日1分, 回测由 akshare 拉历史1分。"""
    if df is None or len(df) == 0 or prev_close <= 0:
        return "盘中数据缺失"
    d = df[df["datetime"] <= t]
    if len(d) == 0:
        return "时刻 t 无盘中数据(未开盘?)"
    last = d.iloc[-1]
    price = float(last["close"])
    day_high = float(d["high"].max())
    day_low = float(d["low"].min())
    limit_up = round(prev_close * (1 + limit_pct), 2)
    sealed = price >= limit_up - 0.01
    gap = (float(d.iloc[0]["open"]) - prev_close) / prev_close if d.iloc[0]["open"] else 0
    chg = price / prev_close - 1
    from_high = (price / day_high - 1) if day_high > 0 else 0
    touched = (d["high"] >= limit_up - 0.005).any()
    # —— 炸板/封板结构(打板接力核心信号, 从1分K逐根判定) ——
    is_seal = (d["close"] >= limit_up - 0.01)
    blast = 0
    prev_s = None
    last_blast_i = -1
    for i in range(len(d)):
        s = bool(is_seal.iloc[i])
        if prev_s is not None and prev_s and not s:  # 封→开 = 炸板
            blast += 1
            last_blast_i = i
        prev_s = s
    if touched and not sealed:
        zha = "是(炸板%d次)" % blast if blast else "是(曾触板后开板)"
    elif not sealed:
        zha = "否"
    else:
        zha = "封死"
    seal_txt = "封死" if sealed else "未封"
    # 末尾连续封板分钟数(封板时长近似)
    sealed_run = 0
    for i in range(len(d) - 1, -1, -1):
        if bool(is_seal.iloc[i]):
            sealed_run += 1
        else:
            break
    blast_ago = "%d分前" % (len(d) - 1 - last_blast_i) if last_blast_i >= 0 else "—"
    re_sealed = ("是" if (blast > 0 and sealed)
                 else ("否" if blast > 0 else "无炸板"))

    def _ago(minutes):
        tt = t - timedelta(minutes=minutes)
        dd = df[df["datetime"] <= tt]
        return float(dd.iloc[-1]["close"]) if len(dd) else None

    a5, a15, a30 = _ago(5), _ago(15), _ago(30)
    d5 = (price / a5 - 1) if a5 else None
    d15 = (price / a15 - 1) if a15 else None
    d30 = (price / a30 - 1) if a30 else None
    rec = d.tail(30)
    pre = d.iloc[:-30] if len(d) > 30 else d
    vr = (rec["amt"].sum() / pre["amt"].sum()) if pre["amt"].sum() > 0 else None
    # 下跌段量能结构(出货 vs 洗盘判别): 近30分 下跌Bar量 / 上涨Bar量
    r0 = rec["close"].shift(1)
    up_vol = float(rec.loc[rec["close"] >= r0, "vol"].sum())
    dn_vol = float(rec.loc[rec["close"] < r0, "vol"].sum())
    dv_ratio = (dn_vol / up_vol) if up_vol > 0 else (9.9 if dn_vol > 0 else 1.0)
    pnl = (price / entry - 1) if entry > 0 else None
    d5s = "%+.1f%%" % (d5 * 100) if d5 is not None else "?"
    d15s = "%+.1f%%" % (d15 * 100) if d15 is not None else "?"
    d30s = "%+.1f%%" % (d30 * 100) if d30 is not None else "?"
    vrs = "%.1fx" % vr if vr is not None else "?"
    dvrs = "%.1fx%s" % (dv_ratio, " (下跌放量·派发嫌疑)" if dv_ratio > 1.3 else "")
    # 均价与反弹结构(洗盘/派发判别核心): 急跌后站回均价/距日内低回升 = 洗盘; 一路新低 = 派发
    _vol = d["vol"].astype(float)
    vwap = float((d["close"] * _vol).sum()) / max(float(_vol.sum()), 1e-9)
    vs_vwap = (price / vwap - 1) if vwap > 0 else 0.0
    reb = (price / day_low - 1) if day_low > 0 else 0.0
    pnls = "%+.1f%%" % (pnl * 100) if pnl is not None else "?"
    parts = [
        "现价 %.2f | 今开 %+.1f%% | 现涨幅 %+.1f%%" % (price, gap * 100, chg * 100),
        "日内高 %.2f 低 %.2f | 距高点 %+.1f%% | 距日内低回升 %+.1f%%" % (day_high, day_low, from_high * 100, reb * 100),
        "涨停价 %.2f %s | 炸板 %s | 开盘涨停:%s" % (limit_up, seal_txt, zha, ("是·开板换手" if (float(d.iloc[0]["open"]) >= limit_up - 0.01 and blast > 0) else "否")),
        "封板时长 %d分 | 末次炸板 %s | 回封:%s | 站均价 %+.1f%%" % (sealed_run, blast_ago, re_sealed, vs_vwap * 100),
        "delta 5/15/30分: %s/%s/%s" % (d5s, d15s, d30s),
        "近30分量能 %s | 下跌放量比 %s" % (vrs, dvrs),
        "浮盈亏 %s (成本 %.2f)" % (pnls, entry),
    ]
    return " | ".join(parts)


def local_fund_proxy(df: pd.DataFrame, t: datetime) -> str:
    """本地资金代理(绕开东方财富限频/墙): 从1分钟K算收盘位置/量能/相对均价/近30分涨跌。
    与 youzi_ai 的 money_proxy_block 同源, 实时与回测通用。"""
    d = df[df["datetime"] <= t]
    if len(d) < 3:
        return ""
    hi, lo = float(d["high"].max()), float(d["low"].min())
    close = float(d["close"].iloc[-1])
    pos = (close - lo) / (hi - lo) if hi > lo else 0.5
    amt = d["amt"].astype(float)
    vr30 = float(amt.iloc[-1]) / max(float(amt.iloc[:-1].mean()), 1e-9) if len(d) > 1 else 1.0
    vol = d["vol"].astype(float)
    vwap = float((d["close"] * vol).sum()) / max(float(vol.sum()), 1e-9)
    vs = close / vwap - 1 if vwap > 0 else 0.0
    chg30 = (close / float(d["close"].iloc[-30]) - 1) if len(d) >= 30 else 0.0
    return ("资金代理(本地): 收盘位置 %.0f%% | 近30分量能 %.1fx | 相对均价 %+.1f%%"
            " | 近30分涨跌 %+.1f%%" % (pos * 100, vr30, vs * 100, chg30 * 100))


def intraday_trajectory(df, t, prev_close, limit_pct):
    """截至时刻 t 的日内轨迹摘要(多快照记忆): 让模型看趋势而非单点瞬间。"""
    if df is None or len(df) == 0 or prev_close <= 0:
        return "轨迹缺失"
    d = df[df["datetime"] <= t]
    if len(d) < 2:
        return "轨迹数据不足"
    open0 = float(d.iloc[0]["open"])
    gap = (open0 / prev_close - 1) * 100
    cur = float(d.iloc[-1]["close"])
    cur_pct = (cur / prev_close - 1) * 100
    hi = float(d["high"].max()); lo = float(d["low"].min())
    hi_pct = (hi / prev_close - 1) * 100
    lo_pct = (lo / prev_close - 1) * 100
    from_high = (cur / hi - 1) * 100 if hi > 0 else 0
    hi_t = d.loc[d["high"].idxmax(), "datetime"]
    lo_t = d.loc[d["low"].idxmin(), "datetime"]

    def _ago(m):
        tt = t - timedelta(minutes=m)
        dd = df[df["datetime"] <= tt]
        return float(dd.iloc[-1]["close"]) if len(dd) else None
    a30 = _ago(30)
    slope = (cur / a30 - 1) * 100 if a30 else 0.0
    limit_up = prev_close * (1 + limit_pct)
    touched = bool((d["high"] >= limit_up - 0.005).any())
    sealed = cur >= limit_up - 0.01
    if sealed:
        shape = "封死涨停(连板进行中)"
    elif touched and not sealed:
        shape = "曾触板后开板(炸板)"
    elif cur_pct >= 0 and from_high > -2 and hi_pct >= 0:
        shape = "高位强势(在高位/近涨停)"
    elif lo_pct < -3 and cur_pct > lo_pct + 3 and cur >= 0:
        shape = "下探后回升(疑似洗盘)"
    elif cur_pct < lo_pct + 1.5 and cur_pct < 0:
        shape = "低开低走/持续新低(破位风险)"
    elif hi_pct > 3 and from_high < -3:
        shape = "冲高回落"
    else:
        shape = "震荡"
    traj = ("日内轨迹(截至%s): 今开%+.1f%% | 最高%+.1f%%(约%s) | 最低%+.1f%%(约%s) | 当前%+.1f%%"
            " | 距日内高%+.1f%% | 近30分%+.1f%% | 曾触板:%s | 形态:%s"
            % (t.strftime("%H:%M"), gap, hi_pct, hi_t.strftime("%H:%M"),
               lo_pct, lo_t.strftime("%H:%M"), cur_pct, from_high, slope,
               "是" if touched else "否", shape))
    return traj, cur_pct, from_high, shape


def build_evidence(code: str, name: str, ds: str, t: datetime,
                   df: pd.DataFrame, prev_close: float, entry: float,
                   days_held: int, live_api=None) -> str:
    lp = limit_pct_of(code)
    intra = intraday_metrics(df, t, prev_close, entry, lp)
    res = intraday_trajectory(df, t, prev_close, lp)
    if isinstance(res, tuple):
        traj, cur_pct, from_high, shape = res
    else:  # 数据不足(如 09:30 仅 1 根 Bar): 仍按现价算 cur_pct, 让快照/趋势可跑
        traj = res
        cur = 0.0
        if df is not None:
            d0 = df[df["datetime"] <= t]
            cur = float(d0.iloc[-1]["close"]) if len(d0) else 0.0
        cur_pct = (cur / prev_close - 1) * 100 if prev_close > 0 else 0.0
        from_high, shape = 0.0, "开盘数据不足"
    topics = YA.market_snapshot(ds).get("topics", {}).get(code, "")
    cb = YA._tool("get_concept_blocks")
    concept = ""
    if cb is not None:
        try:
            concept = str(cb.invoke({"ticker": code}))[:200]
        except Exception:
            pass
    fund = local_fund_proxy(df, t)   # 本地代理, 不依赖东方财富(墙/限频)
    fin = YA.finance_block(code)
    notice = YA.notice_block(code)
    lhb = YA.lhb_block(code)
    ma = YA.ma_block(code)
    mkt = market_for_date(ds)
    anom = anomaly_proximity(code, ds) if _POOL is not None else ""
    ob = ""
    if live_api is not None:
        try:
            ob = YA.orderbook_block(live_api, code, lp * 100)
        except Exception:
            pass
    lines = [
        "【持仓】%s(%s) | 买入 %.2f | 持有 %d 交易日 | 评估日 %s %s"
        % (name, code, entry, days_held, ds, t.strftime("%H:%M")),
        "【盘中指标】%s" % intra,
        "【日内轨迹】%s" % traj,
    ]
    if ob:
        lines.append("【实时盘口】%s" % ob)
    lines += [
        "【题材】%s | 概念: %s" % (topics or "无标注", concept or "缺失"),
        "【资金面】%s" % (fund or "缺失"),
        "【基本面】%s" % (fin or "缺失"),
        "【均线】%s" % (ma or "缺失"),
        "【公告异动】%s" % (notice or "无"),
        "【龙虎榜】%s" % (lhb or "近两周未上榜"),
        "【异动停牌 proximity】%s" % (anom or "数据缺失"),
        "【市场情绪】涨停 %s 只 / 最高连板 %s 板 | %s"
        % (mkt.get("n_limit"), mkt.get("max_st"), mkt.get("emotion", "")),
    ]
    return "\n".join(lines), (cur_pct, from_high, shape)


def _snap_dict(t: datetime, cur: float, fh: float, shape: str, dec: dict) -> dict:
    """把一个扫描点的关键字段压缩成结构化历史, 供后续扫描做趋势统计。"""
    return {"t": t.strftime("%H:%M"), "cur": round(float(cur), 2),
            "fh": round(float(fh), 2), "shape": shape,
            "score": dec.get("sell_score"), "verdict": str(dec.get("verdict", ""))[:4],
            "reason": str(dec.get("reason", ""))[:60]}


def _snap_to_line(h: dict) -> str:
    vt = h.get("verdict", "")
    return "%s | 现%+.1f%% 距高%+.1f%% 形态:%s | %s score=%s %s" % (
        h["t"], h["cur"], h["fh"], h.get("shape", ""), vt, h.get("score"), h.get("reason", ""))


def _compact_history_lines(history: list) -> list:
    """把逐点历史压缩为少量锚点(首点 + 等距抽样 + 末点 + 极值),
    保留趋势信号但大幅削减 token(原每点一行≈47行 → 约8-10行)。"""
    n = len(history)
    if n <= 8:
        return [_snap_to_line(h) for h in history]
    idxs = {0, n - 1}
    step = max(1, n // 6)          # 等距抽样 ~6 个
    for i in range(step, n - 1, step):
        idxs.add(i)
    curs = [h.get("cur", 0) for h in history]
    if curs:
        idxs.add(int(min(range(n), key=lambda i: curs[i])))   # 最低点
        idxs.add(int(max(range(n), key=lambda i: curs[i])))   # 最高点
    return [_snap_to_line(history[i]) for i in sorted(idxs)]


def _trend_summary(history: list, cur_now, fh_now) -> str:
    """从结构化历史算客观趋势统计, 让模型用趋势而非单点判破位。"""
    scores = [h["score"] for h in history if h.get("score") is not None]
    curs = [h["cur"] for h in history]
    n = len(history)
    if n == 0:
        return "无此前扫描"
    s_path = "→".join(str(int(s)) for s in scores) if scores else "无"
    if len(scores) >= 2:
        s_trend = "升" if scores[-1] >= scores[0] + 10 else ("降" if scores[-1] <= scores[0] - 10 else "平")
    else:
        s_trend = "平"
    # 末尾连续创日内新低次数
    run_min = float("inf")
    trail_low = 0
    for c in curs:
        if c < run_min:
            run_min = c
            trail_low += 1
        else:
            trail_low = 0
    new_low = "是" if (cur_now is not None and float(cur_now) <= min(curs)) else "否"
    return ("共%d点 | 分数%s(%s) | 现%+.1f%%(较首点%+.1f%%, 创日内新低:%s, 连低%d次) | 距高%+.1f%%"
            % (n, s_path, s_trend, float(cur_now) if cur_now is not None else curs[-1],
               (float(cur_now) - curs[0]) if cur_now is not None else 0.0,
               new_low, trail_low, float(fh_now) if fh_now is not None else history[-1]["fh"]))


def _client():
    return YA._client()


def decide_sell(evidence: str, verbose: bool = True,
                history: list = None, cur_now=None, fh_now=None) -> tuple[dict, str, dict]:
    content = evidence  # 实际发给模型的内容(早盘含此前扫描记录), 用于日志复盘
    text = ""           # 模型原始返回, 用于日志复盘
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        if history:
            # 压缩历史: 原每扫描点一行(可达47行)既费 token 又无增量信息(分数常恒定),
            # 改为"首点+等距抽样+末点+极值"锚点(~8-10行), 趋势信号由【趋势统计】承载。
            hist_lines = _compact_history_lines(history)
            trend = _trend_summary(history, cur_now, fh_now)
            hb = ("【此前扫描记录(关键锚点, 早→晚)】\n" + "\n".join(hist_lines)
                  + "\n【趋势统计】" + trend + "\n\n")
            content = hb + evidence
        else:
            content = evidence
        content += _sell_report_block()   # 校准闭环: 注入历史卖点成绩单
        llm = _client().get_llm()
        # 硬超时(daemon 线程 + join): 旧实现用 ThreadPoolExecutor + result(timeout),
        # 退出 with 块时 shutdown(wait=True) 仍会阻塞到线程结束, 超时形同虚设。
        resp = YA.invoke_llm(llm, [SystemMessage(content=SYSTEM_SELL),
                                   HumanMessage(content=content)])
        text = getattr(resp, "content", None) or str(resp)
    except Exception as exc:
        print("[AI] 调用失败(%s: %s)" % (type(exc).__name__, str(exc)[:100]))
        return {}, "failed", {"prompt": content, "response": text}
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        print("[AI] 原始输出: %s" % text[:300])
        return {}, "failed", {"prompt": content, "response": text}
    try:
        data = json.loads(m.group(0))
        d = data[0] if isinstance(data, list) and data else {}
        return {
            "action": str(d.get("action", "HOLD")).upper(),
            "sell_score": float(d.get("sell_score", d.get("score", 0)) or 0),
            "scores": d.get("scores", {}) or {},
            "verdict": str(d.get("verdict", ""))[:4],
            "reason": str(d.get("reason", ""))[:120],
        }, "ok", {"prompt": content, "response": text}
    except Exception as exc:
        print("[AI] JSON 解析失败: %s | %s" % (exc, text[:200]))
        return {}, "failed", {"prompt": content, "response": text}


def decide_sell_batch(items: list, now=None) -> dict:
    """一次 LLM 调用判定多只持仓 → {code: (dec, status, raw)}。

    items: [(pos, evidence, meta, hkey, history, days_held, price, entry), ...]
    实盘常驻 2~3 只持仓, 原实现逐只调用: 同一份 SYSTEM_SELL 被重复发送 N 遍,
    且单轮耗时 = N × 单次延迟, 被 1 分钟 cron 的单实例锁跳过。
    合并后 token ≈ 1/N, 单轮 ~35s。回测仍走单票 decide_sell(逐票逐时点, 缓存键独立)。
    """
    if not items:
        return {}
    ids = [chr(ord("A") + i) for i in range(len(items))]
    parts = ["【本次共 %d 只持仓, 请逐只判定, id 依次为 %s】"
             % (len(items), "/".join(ids))]
    for i, (pos, ev, meta, hkey, h, days_held, price, entry) in enumerate(items):
        block = ""
        if h:
            block += ("【%s · 此前扫描记录(关键锚点, 早→晚)】\n"
                      % ids[i]) + "\n".join(_compact_history_lines(h)) \
                     + "\n【%s · 趋势统计】" % ids[i] \
                     + _trend_summary(h, meta[0], meta[1]) + "\n\n"
        block += ev
        parts.append("════ 持仓 %s (%s) ════\n%s"
                     % (ids[i], pos.get("code", ""), block))
    content = "\n\n".join(parts) + _sell_report_block()
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        llm = _client().get_llm()
        resp = YA.invoke_llm(llm, [SystemMessage(content=SYSTEM_SELL),
                                   HumanMessage(content=content)])
        text = getattr(resp, "content", None) or str(resp)
    except Exception as exc:
        print("[AI] 批量调用失败(%s: %s)" % (type(exc).__name__, str(exc)[:100]))
        return {}
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        print("[AI] 批量原始输出: %s" % text[:300])
        return {}
    try:
        data = json.loads(m.group(0))
        if not isinstance(data, list):
            data = [data]
    except Exception as exc:
        print("[AI] 批量 JSON 解析失败: %s | %s" % (exc, text[:200]))
        return {}
    id2item = {}
    for i, it in enumerate(items):
        id2item[str(ids[i]).upper()] = it
        id2item[str(i + 1)] = it          # 模型偶尔用序号当 id
    out = {}
    for d in data:
        key = str(d.get("id", "")).strip().upper()
        it = id2item.get(key)
        if it is None:
            continue
        pos = it[0]
        out[pos["code"]] = ({
            "action": str(d.get("action", "HOLD")).upper(),
            "sell_score": float(d.get("sell_score", d.get("score", 0)) or 0),
            "scores": d.get("scores", {}) or {},
            "verdict": str(d.get("verdict", ""))[:4],
            "reason": str(d.get("reason", ""))[:120],
        }, "ok", {"prompt": content, "response": text})
    return out


# ---------------- 实时模式 ----------------
def fetch_live_df(api, code: str):
    lp = limit_pct_of(code)
    m = 1 if code[0] in "56" else 0
    df = None
    price = prev = 0.0
    if api is not None:
        try:
            bars = api.get_security_bars(8, m, code.encode(), 0, 240)
            q = api.get_security_quotes([(m, code)])
            if bars:
                d = api.to_df(bars)
                d["datetime"] = pd.to_datetime(d["datetime"])
                d["date"] = d["datetime"].dt.strftime("%Y-%m-%d")
                df = d[["datetime", "date", "open", "high", "low", "close", "vol", "amount"]]
                df = df.rename(columns={"vol": "vol", "amount": "amt"})
            if q:
                price = float(q[0].get("price") or 0)
                prev = float(q[0].get("last_close") or 0)
        except Exception:
            pass
    if df is None or price == 0:
        try:
            from tx_quote import minute_bars as tx_min, snapshot as tx_snap
            # 先取快照价/昨收(关键: 防止 pytdx/分钟K 任一失败把价格也吞掉 → DATA_SHORT)
            v = tx_snap([code]).get(code) or {}
            if v:
                price = float(v.get("price") or 0)
                prev = float(v.get("prev_close") or 0)
            # 再取当日1分K(证据用, 失败仅降级证据, 不影响价格判定)
            dd = tx_min(code)
            if dd is not None and len(dd):
                d = dd.copy()
                d["datetime"] = pd.to_datetime(d["datetime"], format="%H%M")
                d["date"] = d["datetime"].dt.strftime("%Y-%m-%d")
                # 腾讯分钟K仅 price/vol(无 OHLC): 以 close 重建 open/high/low/close,
                # amt≈vol*price*100(股), 实时证据降级但 intraday_metrics 可用
                d["close"] = d["price"]
                d["open"] = d["price"]
                d["high"] = d["price"]
                d["low"] = d["price"]
                d["amt"] = d["vol"] * d["price"] * 100.0
                df = d[["datetime", "date", "open", "high", "low", "close", "vol", "amt"]]
        except Exception:
            pass
    return df, price, prev


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
    try:
        return json.loads(SELL_ALERTS.read_text(encoding="utf-8")) if SELL_ALERTS.exists() else {}
    except Exception:
        return {}


SELL_REPORT = YOUZI / "youzi_sell_calibration_report.txt"


def _sell_report_block() -> str:
    """卖出判断的历史成绩单(校准闭环) — 与买侧 `ai_calibration_report.txt` 同构。

    买侧把成绩单注入 prompt, 卖侧 prompt 却写着"每次判断都会被记录与实际走势比对校准"
    却从不给模型看结果 —— 承诺了反馈但闭环断开, 模型无法自己修正 sell_score 标尺。
    样本不足(文件缺失/过小)时不注入: 注入空表反而误导。
    """
    if os.getenv("YOUZI_SELL_NO_REPORT"):
        return ""
    try:
        if SELL_REPORT.exists() and SELL_REPORT.stat().st_size > 80:
            txt = SELL_REPORT.read_text(encoding="utf-8").strip()
            if txt:
                return ("\n\n【你截至当前的历史卖出判断成绩单 — 据此校准你的 sell_score 标尺】\n"
                        + txt[:900])
    except Exception:
        pass
    return ""


def _acquire_lock() -> bool:
    """单实例锁(2026-09-22 新增): 卖侧 cron 已改为 1 分钟/轮, 而单轮要跑 60~90s
    (每只持仓一次 LLM 调用), 无锁必然进程重叠 → 并发写 SELL_HIST / positions
    互相覆盖、重复推送、token 翻倍。用 flock: 进程崩溃时内核自动释放, 不留死锁。
    """
    global _LOCK_FP
    try:
        import fcntl
        _LOCK_FP = open("/tmp/youzi_sell_ai.lock", "w")
        fcntl.flock(_LOCK_FP, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        print("[锁] 上一轮卖出评估仍在跑(单轮>60s), 本次跳过(防并发覆盖状态)")
        return False
    except Exception:
        return True      # 拿锁机制本身失败 → 不阻断业务


def realtime(dry_run: bool = False) -> int:
    now = datetime.now()
    if _is_noon_pause(now):
        print("[跳过] 午休 11:30-13:00, 不评估卖出(省 token)")
        return 0
    if not _acquire_lock():
        return 0
    holds = load_positions()
    if not holds:
        print("[跳过] 无持仓记录")
        return 0
    api = None
    try:
        from pytdx.hq import TdxHq_API
        api = TdxHq_API()
        if not api.connect("180.153.18.170", 7709, time_out=5):
            api = None
    except Exception:
        api = None
    if api is None:
        print("! pytdx 不可用, 行情回退腾讯源")

    try:
        hist_all = json.loads(SELL_HIST.read_text(encoding="utf-8")) if SELL_HIST.exists() else {}
    except Exception:
        hist_all = {}
    rows = []
    need_ai = []          # 需要模型判定的持仓(证据先攒好, 循环后一次性批量调用)

    def _finalize(pos, meta, hkey, h, days_held, dec, stt, raw, forced,
                  price, entry, prev):
        """落盘判定 + 计算是否触发卖出 + 汇总到 rows(单票路径与批量路径共用)。"""
        code = pos["code"]
        snap = _snap_dict(now, meta[0], meta[1], meta[2], dec)
        h.append(snap)
        if len(h) > HISTORY_CAP:
            del h[:-HISTORY_CAP]
        hist_all[hkey] = h
        log_judgement({
            "mode": "live", "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"), "code": code, "name": pos.get("name", code),
            "buy_date": str(pos.get("buy_date")), "days_held": days_held,
            "cur_pct": meta[0], "from_high": meta[1], "shape": meta[2],
            "sell_score": dec.get("sell_score"), "verdict": dec.get("verdict", ""),
            "action": dec.get("action"), "reason": dec.get("reason", ""),
            "scores": dec.get("scores", {}), "forced": forced,
            "snapshot": raw.get("prompt", ""), "llm_response": raw.get("response", ""),
        })
        if stt != "ok":
            rows.append({**pos, "status": "AI_FAIL", "msg": "模型调用失败"})
            return
        pnl = (price / entry - 1) * 100 if price > 0 and entry > 0 else None
        # 早盘硬规则: 09:30-10:00 代码级禁止卖出, 不论模型给多高分
        # forced(时间纪律)直接放行, 不受 SELL_THR 约束
        # 连板保护(代码级): 封死涨停一律不卖 —— 提示词的"≤25"被模型违反过(2026-09-22
        # 603068 封死被推 SELL 78), 卖飞涨停是重大失误, 必须代码兜底
        sell = ((not _is_early_hold(now)) and dec["action"] == "SELL"
                and (forced or dec["sell_score"] >= SELL_THR)
                and not is_sealed(price, prev, code))
        rows.append({**pos, "status": "SELL" if sell else "HOLD",
                     "sell_score": dec["sell_score"], "reason": dec["reason"],
                     "price": price, "pnl": pnl, "days_held": days_held,
                     "scores": dec["scores"], "forced": forced})

    try:
        for pos in holds:
            code = pos["code"]
            entry = float(pos.get("entry") or 0)
            if entry <= 0:
                continue
            df, price, prev = fetch_live_df(api, code)
            # 昨收用日K权威值(prev_close_of); 实时接口 last_close 对某些票取错 → cur_pct 失真
            pc = prev_close_of(code, now.strftime("%Y-%m-%d"))
            if pc > 0:
                prev = pc
            if prev <= 0:
                rows.append({**pos, "status": "DATA_SHORT", "msg": "无行情"})
                continue
            bd = pd.Timestamp(pos["buy_date"]).date()
            # 持有交易日数(非日历日): 原口径跨周末会把 T+1 吹成 T+3, 时间纪律判断全偏
            days_held = trading_days_held(pos["buy_date"], now.strftime("%Y-%m-%d"))
            # ── T+1 硬约束(2026-09-22): 当日买入的票当日不可卖 ──
            # A 股 T+1: 今天买的份额今天不能卖。原逻辑不区分, 当日买入的票同样进 AI 评估,
            # 一旦给到 sell_score≥阈值 就会推卖出、写 youzi_sold.jsonl、从 positions 里
            # 剔除 → 记一笔根本无法成交的"卖出", 还凭空释放打板额度。故代码级跳过评估。
            if days_held <= 0:
                rows.append({**pos, "status": "T1_LOCK",
                             "msg": "T+1 当日买入不可卖", "days_held": 0})
                continue
            # hkey 含评估日: 原 key 只有 (buy_date, code) → 历史跨日无限累积,
            # 今天 09:30 的评估会把昨天 14:58 的扫描点当"此前扫描记录"喂给模型
            # (趋势统计算"较首点"跨日变化毫无意义), 且降频判断会跨日误判。
            ds = now.strftime("%Y-%m-%d")
            hkey = "%s|%s|%s" % (pos["buy_date"], code, ds)
            h = hist_all.get(hkey, [])
            # ── 封死涨停 → 降频评估(省 token): 严格封死才降频, +7~9% 未封死的票
            #    恰恰是炸板高发区, 仍按正常频率评估 ──
            if is_sealed(price, prev, code) and h:
                _lt = str(h[-1].get("t") or "")
                try:
                    _lts = now.replace(hour=int(_lt[:2]), minute=int(_lt[3:5]),
                                       second=0, microsecond=0)
                    _gap = (now - _lts).total_seconds()
                except Exception:
                    _gap = SEAL_EVAL_MIN * 60
                # _gap<0 = 跨日残留(昨日 14:58 的记录), 不节流
                if 0 <= _gap < SEAL_EVAL_MIN * 60:
                    rows.append({**pos, "status": "SKIP_SEALED", "days_held": days_held,
                                 "price": price, "sell_score": None,
                                 "pnl": (price / entry - 1) * 100 if price > 0 else None,
                                 "reason": "封板中·降频(上次评估 %s, 每 %d 分钟一次)"
                                           % (_lt, SEAL_EVAL_MIN)})
                    continue
            forced = False
            if (days_held >= FORCE_SELL_DAYS and _force_time_ok(now)
                    and not near_limit(price, prev, code)):
                # 时间纪律命中 → 不调 LLM(省 token), 直接判定卖出, 当日释放打板额度
                dec = forced_decision(days_held)
                stt = "ok"
                raw = {"prompt": "[时间纪律] 持有%d个交易日, 未调用模型" % days_held,
                       "response": ""}
                cur = float(df.iloc[-1]["close"]) if df is not None and len(df) else price
                meta = ((cur / prev - 1) * 100 if prev > 0 else 0.0, 0.0, "强制了结")
                forced = True
                _finalize(pos, meta, hkey, h, days_held, dec, stt, raw, forced,
                          price, entry, prev)
            else:
                # 需要 AI 判定的: 先攒证据, 循环结束后**一次性**调模型(见下方批量调用)
                ev, meta = build_evidence(code, pos.get("name", code), now.strftime("%Y-%m-%d"),
                                          now, df, prev, entry, days_held, live_api=api)
                need_ai.append((pos, ev, meta, hkey, h, days_held, price, entry))

        # ── 批量调用(2026-09-22): 所有需判定的持仓合并成 1 次 LLM ──
        # 原实现每只票单独调一次: 3 只持仓 = 3 次调用, 且同一份 SYSTEM_SELL(~1200字)
        # 被重复发送 3 遍。合并后: 1 次调用, token 约降到 1/3, 单轮从 75~105s 降到 ~35s,
        # 1 分钟 cron 才不会被单实例锁跳过。
        if need_ai:
            bres = decide_sell_batch(need_ai, now)
            for (pos, ev, meta, hkey, h, days_held, price, entry) in need_ai:
                dec, stt, raw = bres.get(pos["code"], (
                    {"action": "HOLD", "sell_score": -1}, "failed",
                    {"prompt": "", "response": "批量调用未返回该票"}))
                _finalize(pos, meta, hkey, h, days_held, dec, stt, raw, False,
                          price, entry, prev)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    try:
        # 只保留当日的扫描历史: hkey 已含评估日, 旧 key 自然被丢弃(防跨日污染 + 防文件膨胀)
        _ds = now.strftime("%Y-%m-%d")
        hist_all = {k: v for k, v in hist_all.items() if k.endswith("|" + _ds)}
        SELL_HIST.write_text(json.dumps(hist_all, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    alerts = load_alerts() if not dry_run else {}
    pending = [r for r in rows if r.get("status") == "SELL"]
    for r in pending:
        key = "%s|%s" % (r.get("buy_date"), r.get("code"))
        if key not in alerts:
            if not dry_run:
                alerts[key] = True
    if not dry_run and pending:
        SELL_ALERTS.write_text(json.dumps(alerts, ensure_ascii=False), encoding="utf-8")

    _settle = now.hour * 60 + now.minute >= 14 * 60 + 50
    # 强制了结(时间纪律)不等 14:50: 当日必走, 命中即记账 → 立刻把打板额度还给你。
    # 否则"10:00 判定卖、14:50 才写 youzi_sold.jsonl" → youzi_live 的 quota=sells_today_count
    # 全天为 0 → 当天根本买不进新票(这正是 09-21 全天 0 成交的根因)。
    if not dry_run and any(r.get("forced") for r in pending):
        _settle = True
    if not dry_run and _settle and pending:
        have = set()
        if SOLD.exists():
            for line in SOLD.read_text(encoding="utf-8").splitlines():
                try:
                    o = json.loads(line)
                    have.add((str(o.get("buy_date")), str(o.get("code"))))
                except Exception:
                    pass
        with open(SOLD, "a", encoding="utf-8") as f:
            for r in pending:
                key = (str(r.get("buy_date")), str(r.get("code")))
                if key not in have:
                    have.add(key)
                    f.write(json.dumps({
                        "code": r["code"], "name": r.get("name"),
                        "buy_date": str(r.get("buy_date")),
                        "buy_price": float(r.get("entry") or 0),
                        "sell_date": now.strftime("%Y-%m-%d"),
                        "sell_price": float(r.get("price") or 0),
                        "status": "SELL_FORCED" if r.get("forced") else "SELL_AI",
                        "sell_score": r.get("sell_score"),
                        "reason": r.get("reason", ""),
                    }, ensure_ascii=False) + "\n")
        try:
            raw = json.loads(POSITIONS.read_text(encoding="utf-8"))
            for r in pending:
                day, code = str(r.get("buy_date", "")), str(r.get("code", ""))
                if code in (raw.get(day) or {}):
                    raw[day].pop(code)
                    if not raw[day]:
                        raw.pop(day, None)
            POSITIONS.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    print("=" * 70)
    print("游资卖出AI · %s · 阈值 sell_score>=%.0f" % (now.strftime("%Y-%m-%d %H:%M"), SELL_THR))
    for r in rows:
        if r.get("status") == "T1_LOCK":
            print("  [T+1] %s(%s) 当日买入, 今日不可卖(不评估)" % (
                r.get("name", r["code"]), r["code"]))
            continue
        if r.get("status") == "SKIP_SEALED":
            pnl = " 浮盈%+.2f%%" % r["pnl"] if r.get("pnl") is not None else ""
            print("  [封板·降频] %s(%s)%s · %s" % (
                r.get("name", r["code"]), r["code"], pnl, r.get("reason", "")))
            continue
        sc = r.get("sell_score")
        tag = "SELL" if r.get("status") == "SELL" else "HOLD"
        if r.get("forced"):
            tag += "*"     # *=代码级时间纪律强制, 非模型判定
        pnl = " 浮盈%+.2f%%" % r["pnl"] if r.get("pnl") is not None else ""
        dh = " 持%d日" % r["days_held"] if r.get("days_held") is not None else ""
        print("  [%s] %s(%s) score=%s%s%s\n     %s" % (
            tag, r.get("name", r["code"]), r["code"], sc, pnl, dh, r.get("reason", "")))
    print("=" * 70)

    webhook = (os.getenv("DINGTALK_YOUZI_WEBHOOK") or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    keyword = (os.getenv("DINGTALK_YOUZI_KEYWORD") or os.getenv("DINGTALK_KEYWORD") or "游资").strip()
    if dry_run:
        print("[dry-run] 本应推送 %d 笔" % len(pending))
        return 0
    if not webhook:
        print("! 钉钉未配置")
        return 0
    if pending:
        al = ["### 游资卖点提醒 · %s" % now.strftime("%Y-%m-%d %H:%M"),
              "> 以下持仓 AI 评分达卖出阈值, 请处理(脚本不代下单):", ""]
        for r in pending:
            pnl = " 浮盈%+.2f%%" % r["pnl"] if r.get("pnl") is not None else ""
            al.append("- SELL **%s(%s)** score=%s%s\n  买入@%s 现价%s\n  %s" % (
                r.get("name", r["code"]), r["code"], r.get("sell_score"),
                pnl, r.get("entry"), r.get("price"), r.get("reason", "")))
        ok = send_markdown("游资卖点提醒 · %s" % now.strftime("%m-%d %H:%M"),
                           "\n".join(al), webhook=webhook, keyword=keyword)
        print("卖点推送: %s (%d 笔)" % ("成功" if ok else "失败", len(pending)))
    return 0


# ---------------- 回测模式 ----------------
def fetch_hist_min_1(code: str, ds: str):
    if (code, ds) in _hist_cache and _hist_cache[(code, ds)] is not None:
        return _hist_cache[(code, ds)]
    import time
    saved = {k: os.environ.pop(k) for k in list(os.environ) if "proxy" in k.lower()}
    pre = "sh" if code[0] in "569" else "sz"
    sym = pre + code
    df = None
    try:
        import akshare as ak
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"
        for attempt in range(3):
            d = ak.stock_zh_a_minute(symbol=sym, period="1", adjust="")
            if d is None or len(d) == 0:
                time.sleep(1.5)
                continue
            d["datetime"] = pd.to_datetime(d["day"])
            d = d[d["datetime"].dt.strftime("%Y-%m-%d") == ds]
            if len(d) == 0:
                time.sleep(1.5)
                continue
            d = d.rename(columns={"open": "open", "high": "high", "low": "low",
                                 "close": "close", "volume": "vol", "amount": "amt"})
            d = d.reset_index(drop=True)
            for c in ("open", "high", "low", "close", "vol", "amt"):
                d[c] = pd.to_numeric(d[c], errors="coerce")
            d["date"] = ds
            df = d[["datetime", "date", "open", "high", "low", "close", "vol", "amt"]]
            # 合理性校验: 1分K收盘应贴近昨收(±30% 容错, 覆盖新股/极端)。
            # akshare 偶发限频会返回脏数据(价格错乱), 需重试。prev=0 时也按 df 自身跨度拦截。
            prev = prev_close_of(code, ds)
            span = (df["close"].max() - df["close"].min()) / max(df["close"].min(), 1e-9)
            dirty = (prev > 0 and (df["close"].min() < prev * 0.7 or df["close"].max() > prev * 1.3)) or (prev <= 0 and span > 0.5)
            if dirty:
                print("    [warn] %s %s 1分K疑似脏数据(close %.2f~%.2f vs 昨收%.2f), 重试%d/3"
                      % (code, ds, df["close"].min(), df["close"].max(), prev, attempt + 1))
                df = None
                time.sleep(1.5)
                continue
            break
        if df is None:
            print("    [warn] %s %s 1分K拉取失败/脏数据" % (code, ds))
    except Exception as exc:
        print("    [warn] %s %s 1分钟拉取失败: %s" % (code, ds, exc))
    finally:
        os.environ.update(saved)
    if df is not None:  # 仅成功时缓存, 脏/失败不缓存以便重试
        _hist_cache[(code, ds)] = df
    return df


def _bt_cache():
    p = Path("/tmp/youzi_sell_ai_bt_cache.json")
    return p, (json.loads(p.read_text()) if p.exists() else {})


def backtest(limit: int = 0, step: int = 10, max_hold: int = 2,
             dry_data: bool = False, thr: float = None, days: int = 0,
             date: str = None, show_io: bool = False, code: str = None) -> int:
    global _POOL
    _POOL = _pool_daily()
    thr = SELL_THR if thr is None else thr
    buys = []
    if SOLD.exists():
        for line in SOLD.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
                buys.append({"code": o["code"], "name": o.get("name", o["code"]),
                             "buy_date": o["buy_date"], "entry": float(o["buy_price"]),
                             "sell_date": o.get("sell_date")})
            except Exception:
                pass
    for pos in load_positions():
        buys.append({"code": pos["code"], "name": pos.get("name", pos["code"]),
                     "buy_date": pos["buy_date"], "entry": float(pos.get("entry") or 0),
                     "sell_date": None})
    if limit:
        buys = buys[:limit]
    if code:
        allowed = set(code.split(","))
        buys = [b for b in buys if b["code"] in allowed]
    if days:
        dts = [pd.Timestamp(b["buy_date"]) for b in buys]
        if dts:
            cut = max(dts) - pd.Timedelta(days=days - 1)
            buys = [b for b in buys if pd.Timestamp(b["buy_date"]) >= cut]

    cache_path, cache = _bt_cache()
    rows = []
    for b in buys:
        code, entry, bd = b["code"], b["entry"], b["buy_date"]
        if entry <= 0:
            continue
        cur = pd.Timestamp(bd)
        eval_dates = []
        for _ in range(max_hold):
            cur = cur + pd.Timedelta(days=1)
            while cur.weekday() >= 5:
                cur = cur + pd.Timedelta(days=1)
            eval_dates.append(cur.strftime("%Y-%m-%d"))
        if date:
            eval_dates = [d for d in eval_dates if d == date]
            if not eval_dates:
                continue
        first_sell = None
        last_ok = None  # (评估日, 当日收盘) 最近一个有数据的评估日, 用于末日无数据时落盘
        for di, ds in enumerate(eval_dates):
            df = fetch_hist_min_1(code, ds)
            if df is None or len(df) == 0:
                print("  [warn] %s %s 无1分钟数据, 跳过该日" % (code, ds))
                continue
            last_ok = (ds, float(df.iloc[-1]["close"]))
            prev = prev_close_of(code, ds)
            if prev <= 0:  # 最后兜底: 当日开盘
                prev = float(df.iloc[0]["open"])
            # 全天扫描(对齐实时 crontab: 09:30-14:55, 跳过午休 12:00-13:00),
            # 频率由 --step 控制(默认 5 分钟, 与实时一致)
            m1 = pd.date_range("%s 09:30" % ds, "%s 11:30" % ds, freq="%dmin" % step)
            m2 = pd.date_range("%s 13:00" % ds, "%s 14:55" % ds, freq="%dmin" % step)
            scans = m1.union(m2)
            history = []  # 该股该日的扫描历史(逐点累积, 传给模型看趋势)
            for t in scans:
                sub = df[df["datetime"] <= t]
                if len(sub) == 0:
                    continue  # 该时刻无 Bar(如开盘前), 跳过此扫描点
                key = "%s|%s|%s" % (code, ds, t.strftime("%H%M"))
                # 时间纪律(与实盘同口径): 持满 FORCE_SELL_DAYS 个交易日且未封板 → 强制了结
                _px = float(sub.iloc[-1]["close"])
                _frc = ((di + 1) >= FORCE_SELL_DAYS and _force_time_ok(t)
                        and not near_limit(_px, prev, code))
                if _frc:
                    dec = forced_decision(di + 1)
                    snap = _snap_dict(t, (_px / prev - 1) * 100 if prev > 0 else 0.0,
                                      0.0, "强制了结", dec)
                    log_judgement({
                        "mode": "backtest", "date": ds, "time": t.strftime("%H:%M"),
                        "code": code, "name": b["name"], "buy_date": bd,
                        "days_held": di + 1,
                        "cur_pct": snap["cur"], "from_high": 0.0, "shape": "强制了结",
                        "sell_score": dec.get("sell_score"), "verdict": dec.get("verdict", ""),
                        "action": dec.get("action"), "reason": dec.get("reason", ""),
                        "scores": {}, "forced": True,
                        "snapshot": "[时间纪律] 持有%d个交易日, 未调用模型" % (di + 1),
                        "llm_response": "",
                    })
                elif dry_data:
                    dec = {"action": "HOLD", "sell_score": 0}
                    snap = {"t": t.strftime("%H:%M"), "cur": 0, "fh": 0,
                            "shape": "dry", "score": 0, "reason": ""}
                elif key in cache:
                    dec = cache[key]["dec"]
                    snap = cache[key]["snap"]
                else:
                    ev, meta = build_evidence(code, b["name"], ds, t, df, prev,
                                              entry, di + 1, live_api=None)
                    dec, stt, raw = decide_sell(ev, verbose=False, history=history,
                                               cur_now=meta[0], fh_now=meta[1])
                    if stt != "ok":
                        dec = {"action": "HOLD", "sell_score": -1}
                    snap = _snap_dict(t, meta[0], meta[1], meta[2], dec)
                    log_judgement({
                        "mode": "backtest", "date": ds, "time": t.strftime("%H:%M"),
                        "code": code, "name": b["name"], "buy_date": bd,
                        "days_held": di + 1,
                        "cur_pct": meta[0], "from_high": meta[1], "shape": meta[2],
                        "sell_score": dec.get("sell_score"),
                        "verdict": dec.get("verdict", ""),
                        "action": dec.get("action"), "reason": dec.get("reason", ""),
                        "scores": dec.get("scores", {}),
                        "snapshot": raw["prompt"], "llm_response": raw["response"],
                    })
                    cache[key] = {"dec": dec, "snap": snap}
                    if show_io:
                        print("\n" + "=" * 72)
                        print("[LLM-IO] %s %s %s  days_held=%d  cur=%.2f%%  fh=%.2f%%"
                              % (code, b["name"], t.strftime("%H:%M"), di + 1,
                                 meta[0], meta[1]))
                        print("--- PROMPT ---")
                        print(raw["prompt"])
                        print("--- RESPONSE ---")
                        print(raw["response"])
                        print("=" * 72 + "\n")
                    if len(cache) % 20 == 0:
                        cache_path.write_text(json.dumps(cache, ensure_ascii=False))
                # 早盘硬规则: 09:30-10:00 代码级禁止卖出, 不论 AI 给多高分
                # forced(时间纪律)不受 thr 约束(与实盘一致); 封死涨停代码级禁止卖
                if ((not _is_early_hold(t)) and dec.get("action") == "SELL"
                        and (dec.get("forced") or dec.get("sell_score", 0) >= thr)
                        and not is_sealed(_px, prev, code)):
                    price = float(sub.iloc[-1]["close"])
                    first_sell = {"t": t, "price": price,
                                  "score": dec.get("sell_score"),
                                  "verdict": dec.get("verdict", ""),
                                  "reason": dec.get("reason", "")}
                    break
                history.append(snap)
                if len(history) > HISTORY_CAP:
                    history = history[-HISTORY_CAP:]
            if first_sell:
                close_px = float(df.iloc[-1]["close"])
                ai_pnl = (first_sell["price"] / entry - 1) * 100
                close_pnl = (close_px / entry - 1) * 100
                rows.append({"code": code, "name": b["name"], "buy_date": bd,
                             "eval": ds, "ai_pnl": ai_pnl, "close_pnl": close_pnl,
                             "sell_time": first_sell["t"].strftime("%H:%M"),
                             "sell_score": first_sell["score"],
                             "verdict": first_sell["verdict"], "reason": first_sell["reason"]})
                print("  %s %s: AI于 %s %s 卖(score=%s, %s) P&L %+.2f%% | 持收 %+.2f%%" % (
                    code, b["name"], ds, first_sell["t"].strftime("%H:%M"),
                    first_sell["score"], first_sell["verdict"], ai_pnl, close_pnl))
                break
        # 全程未触发且至少有一天有数据 → 按最近一个有数据的评估日收盘落盘
        # (修复: 末日若为未来交易日无数据被跳过, 旧逻辑不落盘 → 报"无回测结果")
        if not first_sell and last_ok is not None:
            ds, close_px = last_ok
            close_pnl = (close_px / entry - 1) * 100
            rows.append({"code": code, "name": b["name"], "buy_date": bd,
                         "eval": ds, "ai_pnl": None, "close_pnl": close_pnl,
                         "sell_time": "未触发(持有收盘)", "sell_score": None,
                         "reason": ""})
            print("  %s %s: %s 未触发(持收 %+.2f%%)" % (code, b["name"], ds, close_pnl))

    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    # 落盘回测逐笔汇总(供 youzi_sell_calibrate 作为'回测反事实'数据源, 比日K近似精确)
    _bt_file = LOG_DIR / "sell_backtest_results.jsonl"
    _bt_old = []
    if _bt_file.exists():
        for _l in _bt_file.read_text(encoding="utf-8").splitlines():
            _l = _l.strip()
            if _l:
                try:
                    _bt_old.append(json.loads(_l))
                except Exception:
                    pass
    _bt_seen = {(r.get("code"), r.get("buy_date"), r.get("eval"), r.get("thr"))
                for r in _bt_old}
    for r in rows:
        _rec = {**r, "thr": thr, "step": step, "max_hold": max_hold}
        _key = (r.get("code"), r.get("buy_date"), r.get("eval"), thr)
        if _key in _bt_seen:
            for _i, _o in enumerate(_bt_old):
                if (_o.get("code"), _o.get("buy_date"), _o.get("eval"), _o.get("thr")) == _key:
                    _bt_old[_i] = _rec
                    break
        else:
            _bt_old.append(_rec)
            _bt_seen.add(_key)
    _bt_file.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in _bt_old),
                         encoding="utf-8")
    print("  回测汇总落盘(累计 %d 笔) → %s" % (len(_bt_old), _bt_file.name))
    df = pd.DataFrame(rows)
    if len(df) == 0:
        print("无回测结果")
        return 0
    ai = df[df["ai_pnl"].notna()]
    print("\n" + "=" * 80)
    print("  卖出AI回测 · 样本 %d 笔 · 阈值 sell_score>=%.0f · step=%dmin · max_hold=%d日"
          % (len(buys), thr, step, max_hold))
    print("=" * 80)
    if len(ai):
        print("  AI卖点命中 %d/%d 笔" % (len(ai), len(df)))
        print("  AI卖 P&L:    笔均 %+.2f%%  胜率 %d%%"
              % (ai["ai_pnl"].mean(), (ai["ai_pnl"] > 0).mean() * 100))
        print("  对比持收:    笔均 %+.2f%%  胜率 %d%%"
              % (ai["close_pnl"].mean(), (ai["close_pnl"] > 0).mean() * 100))
    notrig = df[df["ai_pnl"].isna()]
    if len(notrig):
        print("  未触发(自动持收): %d 笔, 笔均 %+.2f%%" % (len(notrig), notrig["close_pnl"].mean()))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true", help="回测模式")
    ap.add_argument("--dry-run", action="store_true", help="实时模式只打印不推送")
    ap.add_argument("--dry-data", action="store_true", help="回测只验数据覆盖, 不调模型")
    ap.add_argument("--limit", type=int, default=0, help="回测样本数上限")
    ap.add_argument("--step", type=int, default=5, help="回测扫描间隔(分钟), 默认5(与实时crontab一致)")
    ap.add_argument("--days", type=int, default=0, help="回测只取最近N天买入(0=全部), 降成本用")
    ap.add_argument("--date", type=str, default=None, help="回测只评估该卖出日(YYYY-MM-DD), 单日验证用")
    ap.add_argument("--max-hold", type=int, default=2, help="回测最多持有交易日")
    ap.add_argument("--sell-thr", type=float, default=None, help="卖出阈值(覆盖默认70)")
    ap.add_argument("--code", type=str, default=None, help="回测只跑指定代码(逗号分隔, 如 000700,002774)")
    ap.add_argument("--show-io", action="store_true", help="回测时控制台打印每次大模型入参(prompt)/出参(response)")
    args = ap.parse_args()
    global SELL_THR
    if args.sell_thr:
        SELL_THR = args.sell_thr
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if args.backtest:
        log_name = "sell_bt_%s.log" % (args.date or datetime.now().strftime("%Y-%m-%d"))
    else:
        log_name = "sell_%s.log" % datetime.now().strftime("%Y-%m-%d")
    _tee = Tee(LOG_DIR / log_name)
    _old_stdout = sys.stdout
    sys.stdout = _tee
    try:
        if args.backtest:
            rc = backtest(limit=args.limit, step=args.step, max_hold=args.max_hold,
                          dry_data=args.dry_data, thr=args.sell_thr, days=args.days,
                          date=args.date, show_io=args.show_io, code=args.code)
        else:
            rc = realtime(dry_run=args.dry_run)
    finally:
        sys.stdout = _old_stdout
        try:
            _tee.f.close()
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
