#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 判断校准闭环 — 让 AI 用自己的实盘战绩校准 prob 标尺。

闭环三步:
  1. 记录(实时): youzi_live 每轮 AI 判定 → ai_judgements.jsonl
  2. 回填(每日收盘后): pytdx 日K → 当日是否封板 / 次日开盘与收盘溢价
  3. 报告(注入提示词): prob 分档 × 实际结果 — AI 据此校准自己的给分

为什么不用历史回测当锚: 历史统计有前视风险且是"平均规律";
本报告是 AI 自己判断记录的前瞻统计 — 无前视、持续更新、越跑越准。

crontab: 20 15 * * 1-5 (收盘后运行; 次日数据未到时只填当日封板状态)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

STATE = Path.home() / ".tradingagents" / "youzi"
JL = STATE / "ai_judgements.jsonl"
REPORT = STATE / "ai_calibration_report.txt"
TDX = ("180.153.18.170", 7709)


def load_recs() -> list[dict]:
    if not JL.exists():
        return []
    recs = []
    for line in JL.read_text().splitlines():
        try:
            recs.append(json.loads(line))
        except Exception:
            pass
    return recs


def fetch_daily(api: TdxHq_API, code: str, n: int = 15):
    """pytdx 优先, 失效回退新浪(date 列统一为字符串)。"""
    try:
        m = TDXParams.MARKET_SH if code[0] in "569" else TDXParams.MARKET_SZ
        bars = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, m,
                                     code.encode(), 0, n)
        if bars:
            d = api.to_df(bars)
            d["date"] = d["datetime"].str[:10]
            return d.sort_values("date").reset_index(drop=True)
    except Exception:
        pass
    try:
        import requests
        sym = ("sh" if code[0] in "569" else "sz") + code
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
        df["date"] = df["date"].astype(str)
        return df.sort_values("date").reset_index(drop=True)
    except Exception:
        return None


def tx_close(code: str) -> tuple[float, float]:
    """腾讯实时收盘价/昨收 — 当日K线未更新(新浪收盘后延迟)时的封板判定兜底。

    ⚠ 只能用于【当天】的记录: qt 返回的是最新收盘价, 对历史日期会误判。
    """
    pre = "sh" if code[0] in "569" else "sz"
    try:
        import requests
        qt = (requests.get(
            f"https://web.ifzq.gtimg.cn/appstock/app/day/query?code={pre}{code}",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
            proxies={"http": None, "https": None})
            .json()["data"][f"{pre}{code}"]["qt"][f"{pre}{code}"])
        return float(qt[3]), float(qt[4])       # 现价(收盘), 昨收
    except Exception:
        return 0.0, 0.0


def backfill_blocked() -> int:
    """被规则层拦截的候选(blocked_log.jsonl) → 回填当日封板状态。

    两周复盘(2026-09-28)的核心数据: 验证「被拦的确实差」——
    若被拦票封板率接近/超过通过的, 说明过滤误杀, 应撤。
    """
    bf = STATE / "blocked_log.jsonl"
    if not bf.exists():
        return 0
    recs = []
    for line in bf.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
            if isinstance(o, dict) and o.get("code"):
                recs.append(o)
        except Exception:
            pass
    if not recs:
        return 0
    n = 0
    for r in recs:
        if r.get("seal") is not None:
            continue
        code, dt = r.get("code", ""), r["ts"][:10]
        if dt >= datetime.now().strftime("%Y-%m-%d"):
            continue            # 当天的收盘后再判
        px, pv = tx_close(code)
        if px > 0 and pv > 0:
            # 拦截记录无 thr_pct, 主板默认10%; pct 字段是判定时涨幅
            r["seal"] = bool(px / pv - 1 >= 0.098)
            n += 1
    if n:
        bf.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                              for r in recs))
    return n


def backfill() -> int:
    """未回填记录 → 补当日封板状态; 次日数据已生成则补次日溢价。"""
    # 盘中日K未定型(收盘价=当前价), 此时判定"是否封板"会污染数据 → 收盘后才回填
    if datetime.now().hour < 15:
        print("未收盘, 跳过回填(盘中封板判定会失真)")
        return 0
    nb = backfill_blocked()
    if nb:
        print(f"拦截记录回填封板状态: {nb} 条")
    recs = load_recs()
    if not recs:
        print("无判定记录")
        return 0
    api = TdxHq_API()
    if not api.connect(*TDX, time_out=5):
        print("! pytdx 不可用, 日K回退新浪源")
        api = None
    cache: dict[str, object] = {}
    n_done = 0
    try:
        for r in recs:
            if r.get("filled"):
                continue
            code, dt = r.get("code", ""), r["ts"][:10]
            thr = float(r.get("thr_pct") or 10.0)
            # 当天记录: 日K源(新浪)收盘后常延迟数小时甚至被限流 →
            # 直接用腾讯实时收盘价判定, 最快最稳(且不受日K可用性影响)
            if dt == datetime.now().strftime("%Y-%m-%d"):
                px, pv = tx_close(code)
                if px > 0 and pv > 0:
                    r["seal"] = bool(px / pv - 1 >= thr / 100 - 0.002)
                    r["seal_date"] = dt
                    n_done += 1
                continue
            if code not in cache:
                cache[code] = fetch_daily(api, code)
            d = cache[code]
            if d is None or len(d) < 2:
                continue
            dates = d["date"].tolist()
            if dt not in dates:
                continue
            i = dates.index(dt)
            if i < 1:
                continue
            thr = float(r.get("thr_pct") or 10.0)
            prev_close = float(d.iloc[i - 1]["close"])
            day_close = float(d.iloc[i]["close"])
            # 当日封板: 收盘涨幅达到该板涨停幅(容差0.2%)
            r["seal"] = bool(day_close / prev_close - 1 >= thr / 100 - 0.002)
            r["seal_date"] = dt
            if i + 1 < len(dates):
                nxt = d.iloc[i + 1]
                r["ret_open1"] = round(
                    (float(nxt["open"]) / day_close - 1) * 100, 2)
                r["ret_close1"] = round(
                    (float(nxt["close"]) / day_close - 1) * 100, 2)
                r["filled"] = True
            n_done += 1
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    JL.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                          for r in recs))
    return n_done


def _dedup(rs: list[dict]) -> list[dict]:
    key: dict = {}
    for r in rs:
        k = (r["ts"][:10], r["code"])
        if k not in key or r["ts"] > key[k]["ts"]:
            key[k] = r
    return sorted(key.values(), key=lambda r: r["ts"])


def _buckets() -> list:
    """70 以上细分到 75/80: 75 是实盘门槛, 且回测显示 70-74 与 75-79
    表现差异极大(同日 14% vs 75%), 合并显示会掩盖这个关键区分。"""
    return (("80+", 80, 101), ("75-79", 75, 80), ("70-74", 70, 75),
            ("65-69", 65, 70), ("60-64", 60, 65), ("<60", 0, 60))


def report() -> str:
    """prob 分档 × 实际结果。同票同日多次判定取最后一次(时间最晚)。

    分两段: 封板率(当日收盘即可回填, 不用等 T+1) / 次日收益(需 T+1)。
    """
    all_recs = load_recs()
    sealed = _dedup([r for r in all_recs if r.get("seal") is not None])
    recs = _dedup([r for r in all_recs if r.get("filled")])

    out = [f"AI 判断校准报告 (生成 {datetime.now():%Y-%m-%d %H:%M})", ""]
    if sealed:                       # 封板率: 当日收盘即可验证
        out += [f"【封板率 · 当日验证 · {len(sealed)} 条】",
                f"{'prob档':<8}{'笔数':>5}{'封板率':>9}"]
        for name, lo, hi in _buckets():
            sub = [r for r in sealed if lo <= float(r.get("prob") or 0) < hi]
            if not sub:
                continue
            sr = sum(1 for r in sub if r.get("seal")) / len(sub) * 100
            flag = "*" if len(sub) < 5 else " "     # 小样本标注, 防噪声误导
            out.append(f"{name + flag:<8}{len(sub):>5}{sr:>8.1f}%")
        out.append("  *样本<5, 仅供参考(勿据此改判)")
        b = [r for r in sealed if r.get("action") == "BUY"]
        if b:
            out.append(f"BUY {len(b)} 笔封板率 "
                       f"{sum(1 for r in b if r.get('seal')) / len(b) * 100:.1f}%")
    else:
        out += ["【封板率】暂无(需当日收盘后回填)"]
    out.append("")

    # ── 近期 BUY 案例回顾: 判断理由+实际结果, 供 AI 对照自己的推理模式 ──
    buys_all = _dedup([r for r in all_recs if r.get("action") == "BUY"])
    cases = [r for r in buys_all if r.get("ret") is not None][-5:]
    if cases:
        out.append("【近期 BUY 案例回顾 · 你的判断理由与实际结果】")
        for r in cases:
            res = (f"{'封板' if r.get('seal') else '未封板'}"
                   f", 次日开盘 {r['ret']:+.2f}%")
            out.append(f"  {r['ts'][:10]} {r.get('name') or r.get('code')} "
                       f"prob={r.get('prob')} — {r.get('reason') or ''}"
                       f" → 结果: {res}")
        out.append("")
    elif buys_all:
        out += ["【近期 BUY 案例回顾】有 BUY 判定但尚未回填结果(需 T+1)", ""]

    # ── 维度子分校准: 各维度打分与封板率的关系 → 发现哪一维判断可靠 ──
    scored = [r for r in sealed if isinstance(r.get("scores"), dict)
              and r.get("scores")]
    if len(scored) >= 8:
        out += ["【维度子分校准 · 高分组 vs 低分组的封板率】",
                "(子分≥7 = 该维度证据被你评为强; ≤4 = 评为弱; 样本<5 的维度不列)",
                f"{'维度':<6}{'样本':>5}{'高分组封板率':>14}{'低分组封板率':>14}"]
        for dim in ("盘口", "资金", "题材", "基本面", "首触", "位置"):
            rs = [r for r in scored
                  if r["scores"].get(dim) is not None]
            if len(rs) < 5:
                continue
            hi = [r for r in rs if r["scores"][dim] >= 7]
            lo = [r for r in rs if r["scores"][dim] <= 4]
            hi_r = (f"{sum(1 for r in hi if r.get('seal'))/len(hi)*100:.0f}%"
                    f"({len(hi)})" if hi else "—")
            lo_r = (f"{sum(1 for r in lo if r.get('seal'))/len(lo)*100:.0f}%"
                    f"({len(lo)})" if lo else "—")
            out.append(f"{dim:<6}{len(rs):>5}{hi_r:>13}{lo_r:>13}")
        out.append("  (若某维度高分组封板率显著高于低分组 → 该维度判断有效;"
                   " 两档接近 → 该维度在瞎猜)")
        out.append("")
    elif scored:
        out += [f"【维度子分校准】已有 {len(scored)} 条, 样本满 8 条后出报告", ""]

    out.append(f"【次日收益 · {len(recs)} 条已回填】")
    if not recs:
        out.append("  暂无 — 判定后需 1 个交易日才能回填次日开盘/收盘结果")
        txt = "\n".join(out)
        REPORT.write_text(txt)
        return txt
    out.append(f"{'prob档':<8}{'笔数':>5}{'封板率':>8}{'次日开盘':>9}{'次日收盘':>9}")
    for name, lo, hi in _buckets():
        sub = [r for r in recs if lo <= float(r.get("prob") or 0) < hi]
        if not sub:
            out.append(f"{name:<8}{0:>5}{'—':>8}{'—':>9}{'—':>9}")
            continue
        seal = sum(1 for r in sub if r.get("seal")) / len(sub) * 100
        o = sum(r.get("ret_open1", 0) for r in sub) / len(sub)
        c = sum(r.get("ret_close1", 0) for r in sub) / len(sub)
        out.append(f"{name:<8}{len(sub):>5}{seal:>7.1f}%{o:>+8.2f}%{c:>+8.2f}%")

    buys = [r for r in recs if r.get("action") == "BUY"]
    out.append("")
    if buys:
        seal = sum(1 for r in buys if r.get("seal")) / len(buys) * 100
        o = sum(r.get("ret_open1", 0) for r in buys) / len(buys)
        c = sum(r.get("ret_close1", 0) for r in buys) / len(buys)
        out.append(f"BUY 判定 {len(buys)} 笔: 封板率 {seal:.1f}% | "
                   f"次日开盘 {o:+.2f}% | 次日收盘 {c:+.2f}%")
        if c < 0:
            out.append("⚠ BUY 整体次日收益为负 — 标尺过松, 需收紧")
    else:
        out.append("BUY 判定 0 笔")
    txt = "\n".join(out)
    REPORT.write_text(txt)
    return txt


def main() -> int:
    n = backfill()
    print(f"回填 {n} 条")
    print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
