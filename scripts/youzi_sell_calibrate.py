#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卖出 AI 校准闭环 — 与 youzi_calibrate.py 同构, 让卖出判定用实盘战绩校准 sell_score 标尺。

闭环三步(对齐买入 calibrate):
  1. 记录(实时): youzi_sell_ai 每轮判定 → sell_judgements.jsonl (已具备: action/sell_score/verdict/scores/快照)
  2. 回填(每日收盘后): 日K(腾讯兜底) → 该持仓评估日次日开盘/收盘溢价 + 反推"若当时卖"的近似价
  3. 报告(注入提示词): sell_score 分档 × 实际后续走势 / 六维子分校准 / SELL_THR 阈值敏感性

⚠ 安全边界(重要):
  - 本脚本**只生成报告**, 绝不自动热更 SELL_THR 或 SYSTEM_SELL 提示词。
  - 理由: v8 实验(2026-09-21)已证 "放开 sell_score 硬锁、让模型自由调分" → 卖飞赢家
    (603082 10:00 卖 -11.16% vs 持收 -1.73%; 600184 卖 +2.51% vs 持收 +11.09%)。
    故校准结论仅供人工/AI 参考, 参数调整由人决定, 脚本不代劳。
  - 与 youzi_calibrate 一致: 买入侧也只出 ai_calibration_report.txt, prob≥75 阈值仍固定。

回填口径说明:
  - youzi 纪律是"次日开盘走", 故 HOLD 的真实化现锚定【次日开盘/收盘溢价】(相对评估日收盘)。
  - HOLD 的反事实"若当时卖"价 = 评估日昨收 ×(1+cur_pct), 由记录内 cur_pct 反推(记录未存评估时刻现价)。
  - 盘中冲高回落(该次该盘中卖更好)需 1分K, 本版仅用日K近似, 报告标注此局限。

crontab: 25 15 * * 1-5 (在 youzi_calibrate 15:20 之后跑, 确保当日日K定型)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# 复用买入 calibrate 的日K回填(含 pytdx→新浪→腾讯三级兜底)
from youzi_calibrate import fetch_daily, TdxHq_API, TDX

STATE = Path.home() / ".tradingagents" / "youzi"
JL = STATE / "logs" / "sell_judgements.jsonl"
REPORT = STATE / "youzi_sell_calibration_report.txt"


def load_recs() -> list[dict]:
    if not JL.exists():
        return []
    recs = []
    for line in JL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except Exception:
            pass
    return recs


def backfill(force: bool = False) -> int:
    """未回填记录 → 补次日开盘/收盘溢价 + 反推若当时卖近似价。

    当天(date==今天)或次日数据未到的记录跳过(下次再填), 避免盘中数据失真。
    force=True 绕过 hour<15 检查(用于盘中补历史/验证; 当天记录仍跳过因次日未到)。
    """
    if not force and datetime.now().hour < 15:
        print("未收盘, 跳过回填(盘中数据会失真)")
        return 0
    api = TdxHq_API()
    if not api.connect(*TDX, time_out=5):
        print("! pytdx 不可用, 日K回退新浪/腾讯源")
        api = None
    try:
        recs = load_recs()
        if not recs:
            print("无判定记录")
            return 0
        today = datetime.now().strftime("%Y-%m-%d")
        n_done = 0
        for r in recs:
            if r.get("filled"):
                continue
            code = str(r.get("code", ""))
            dt = str(r.get("date", ""))
            if not code or not dt or dt >= today:
                continue
            d = fetch_daily(api, code)          # pytdx 直连, 失败兜底新浪/腾讯
            if d is None or len(d) < 2:
                continue
            dates = d["date"].tolist()
            if dt not in dates:
                continue
            i = dates.index(dt)
            if i < 1 or i + 1 >= len(dates):
                continue                        # 需昨收(反推卖价) + 次日(填结果)
            date_close = float(d.iloc[i]["close"])
            prev_close = float(d.iloc[i - 1]["close"])
            nxt = d.iloc[i + 1]
            next_open = float(nxt["open"])
            next_close = float(nxt["close"])
            r["next_open_ret"] = round((next_open / date_close - 1) * 100, 2)
            r["next_close_ret"] = round((next_close / date_close - 1) * 100, 2)
            cur_pct = r.get("cur_pct")
            if isinstance(cur_pct, (int, float)) and prev_close > 0:
                # 若当时(评估时刻)卖的近似价 = 昨收 ×(1+评估时现涨幅)
                r["eval_price"] = round(prev_close * (1 + cur_pct / 100), 3)
                r["eval_ret_vs_close"] = round(
                    (r["eval_price"] / date_close - 1) * 100, 2)
            r["filled"] = True
            n_done += 1
        if n_done:
            JL.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                  for r in recs), encoding="utf-8")
            print(f"回填 {n_done} 条")
        else:
            print("无新可回填记录(均当日/缺次日数据)")
        return n_done
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


BT_JL = STATE / "logs" / "sell_backtest_results.jsonl"


def load_bt() -> list[dict]:
    """读 youzi_sell_ai --backtest 落盘的回测逐笔汇总(真实盘中卖点价反事实)。"""
    if not BT_JL.exists():
        return []
    recs = []
    for line in BT_JL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except Exception:
            pass
    return recs


def _dedup(recs: list[dict]) -> list[dict]:
    """同票同日多次盘中评估 → 取最后一条(代表当日最终判定)。"""
    key: dict = {}
    for r in recs:
        k = (r.get("date"), r.get("code"))
        if k not in key or str(r.get("time", "")) > str(key[k].get("time", "")):
            key[k] = r
    return list(key.values())


def _buckets() -> list:
    """sell_score 分档: <40=持有先验(洗盘/连板保护); 40-59=观望; 60-69=疑似派发;
    70-79=打板失败/持续派发门槛; 80+=强派发。"""
    return (("80+", 80, 101), ("70-79", 70, 80), ("60-69", 60, 70),
            ("40-59", 40, 60), ("<40", 0, 40))


def _render_backtest_section() -> list[str]:
    """回测反事实数据源区块(1分K重放, 最权威)。实盘为空时也照常渲染。"""
    bt = load_bt()
    if not bt:
        return ["【回测反事实数据源】暂无(运行 python3 youzi_sell_ai.py --backtest "
                "--days 2 累积样本)", ""]
    out: list[str] = ["【回测反事实数据源 · youzi_sell_ai --backtest 产出(1分K重放)】",
                      "  ★最权威: 真实盘中卖点价 vs 持有收盘, 非日K近似",
                      f"{'thr':<5}{'笔数':>5}{'AI卖均':>9}{'AI胜率':>7}"
                      f"{'持收均':>9}{'持收胜率':>8}{'增益':>8}"]
    for thr in sorted({r.get("thr") for r in bt}):
        sub = [r for r in bt if r.get("thr") == thr]
        ai = [r for r in sub if r.get("ai_pnl") is not None]
        a_mean = (sum(r["ai_pnl"] for r in ai) / len(ai)) if ai else float("nan")
        a_win = (sum(1 for r in ai if r["ai_pnl"] > 0) / len(ai) * 100) if ai else 0
        cp = [r for r in sub if r.get("close_pnl") is not None]
        c_mean = (sum(r["close_pnl"] for r in cp) / len(cp)) if cp else 0
        c_win = (sum(1 for r in cp if r["close_pnl"] > 0) / len(cp) * 100) if cp else 0
        gain = (a_mean - c_mean) if a_mean == a_mean else float("nan")
        a_s = f"{a_mean:>+8.2f}%" if a_mean == a_mean else "    —  "
        g_s = f"{gain:>+7.2f}%" if gain == gain else "    —  "
        out.append(f"{thr:<5}{len(sub):>5}{a_s}{a_win:>6.0f}%{c_mean:>+8.2f}%"
                   f"{c_win:>7.0f}%{g_s}")
    out.append("  AI卖均=AI触发的真实盘中卖点收益; 持收均=未触发持有到收盘; 增益=AI相对持收")
    out.append("")
    out.append("  回测 sell_score 分档 × AI 卖收益(仅触发组):")
    out.append(f"  {'score档':<8}{'笔数':>5}{'AI卖均':>9}{'持收均':>9}")
    for name, lo, hi in _buckets():
        sub = [r for r in bt
               if r.get("sell_score") is not None
               and lo <= float(r["sell_score"]) < hi]
        if not sub:
            continue
        ai = [r for r in sub if r.get("ai_pnl") is not None]
        a = (sum(r["ai_pnl"] for r in ai) / len(ai)) if ai else 0
        cp = [r for r in sub if r.get("close_pnl") is not None]
        c = (sum(r["close_pnl"] for r in cp) / len(cp)) if cp else 0
        out.append(f"  {name:<8}{len(sub):>5}{a:>+8.2f}%{c:>+8.2f}%")
    out.append("  说明: 回测样本按 thr 累积(python3 youzi_sell_ai.py --backtest "
               "--sell-thr N); 改 thr 重跑自动去重更新")
    out.append("")
    return out


def report() -> str:
    all_recs = load_recs()
    recs = _dedup([r for r in all_recs if r.get("filled")])
    out = [f"卖出 AI 校准报告 (生成 {datetime.now():%Y-%m-%d %H:%M})",
           f"样本: {len(recs)} 条已回填(同票同日取最终判定)", ""]

    if not recs:
        out.append("暂无已回填记录(实盘需收盘后 1+ 交易日回填次日结果)")
        out.extend(_render_backtest_section())
        txt = "\n".join(out)
        REPORT.write_text(txt, encoding="utf-8")
        return txt

    # ── 1) sell_score 分档 × 后续走势 ──
    out.append("【sell_score 分档 × 后续走势 · 验证 AI 给分方向性】")
    out.append(f"{'score档':<8}{'笔数':>5}{'次日开盘':>9}{'次日收盘':>9}"
               f"{'若当时卖*':>9}")
    out.append("  *若当时卖=评估时刻近似价相对评估日收盘(反推, 仅供参考)")
    for name, lo, hi in _buckets():
        sub = [r for r in recs if lo <= float(r.get("sell_score") or 0) < hi]
        if not sub:
            continue
        o = sum(r.get("next_open_ret", 0) for r in sub) / len(sub)
        c = sum(r.get("next_close_ret", 0) for r in sub) / len(sub)
        e = (sum(r.get("eval_ret_vs_close", 0) for r in sub) / len(sub)
             if all("eval_ret_vs_close" in r for r in sub) else float("nan"))
        flag = "*" if len(sub) < 5 else " "
        e_s = f"{e:>+8.2f}%" if e == e else "    —  "
        out.append(f"{name + flag:<8}{len(sub):>5}{o:>+8.2f}%{c:>+8.2f}%{e_s}")
    out.append("  *样本<5 仅供参考; 期望: <40档(持有先验)次日应正, "
               "70+档(派发)次日应负→说明给分方向有效")
    out.append("")

    # ── 2) 六维子分校准 ──
    scored = [r for r in recs if isinstance(r.get("scores"), dict) and r.get("scores")]
    if len(scored) >= 8:
        out.append("【六维子分校准 · 高分组(≥7) vs 低分组(≤4) 次日开盘收益】")
        out.append(f"{'维度':<6}{'样本':>5}{'高分组':>10}{'低分组':>10}")
        for dim in ("盘口", "资金", "题材", "趋势", "情绪", "风险"):
            rs = [r for r in scored if isinstance(r["scores"].get(dim), (int, float))]
            if len(rs) < 5:
                continue
            hi = [r for r in rs if r["scores"][dim] >= 7]
            lo = [r for r in rs if r["scores"][dim] <= 4]
            hi_r = (f"{sum(r['next_open_ret'] for r in hi)/len(hi):+.1f}%"
                    f"({len(hi)})" if hi else "—")
            lo_r = (f"{sum(r['next_open_ret'] for r in lo)/len(lo):+.1f}%"
                    f"({len(lo)})" if lo else "—")
            out.append(f"{dim:<6}{len(rs):>5}{hi_r:>10}{lo_r:>10}")
        out.append("  (高分组次日收益显著低于低分组→该维度判断有效; 两档接近→在瞎猜)")
        out.append("")
    elif scored:
        out.append(f"【六维子分校准】已有 {len(scored)} 条, 满 8 条后出报告")
        out.append("")

    # ── 3) SELL_THR 阈值敏感性模拟(仅报告, 不热更) ──
    out.append("【SELL_THR 阈值敏感性模拟 · 仅报告不热更(v8 教训)】")
    out.append("  模拟: sell_score≥thr 视为 SELL(卖在评估时刻近似价), 否则 HOLD(次日开盘化现)")
    out.append(f"{'thr':<5}{'SELL笔':>7}{'净均收益*':>10}{'卖飞数':>8}{'卖晚数':>8}")
    out.append("  *净均收益=SELL(eval_ret_vs_close)与HOLD(next_open_ret)的均值混合; "
               "卖飞=SELL后次日开盘涨; 卖晚=HOLD后次日开盘跌>2%")
    for thr in (60, 65, 70, 75, 80):
        sell_recs = [r for r in recs
                     if float(r.get("sell_score") or 0) >= thr
                     and "eval_ret_vs_close" in r]
        hold_recs = [r for r in recs
                     if float(r.get("sell_score") or 0) < thr]
        if not sell_recs and not hold_recs:
            out.append(f"{thr:<5}{0:>7}{'—':>10}{0:>8}{0:>8}")
            continue
        s_ret = (sum(r["eval_ret_vs_close"] for r in sell_recs) / len(sell_recs)
                 if sell_recs else 0)
        h_ret = (sum(r["next_open_ret"] for r in hold_recs) / len(hold_recs)
                 if hold_recs else 0)
        # 混合净均(等权每笔, 用各自收益)
        all_ret = ([r["eval_ret_vs_close"] for r in sell_recs]
                   + [r["next_open_ret"] for r in hold_recs])
        net = sum(all_ret) / len(all_ret) if all_ret else 0
        fly = sum(1 for r in sell_recs if r.get("next_open_ret", 0) > 0)
        late = sum(1 for r in hold_recs if r.get("next_open_ret", 0) < -2)
        out.append(f"{thr:<5}{len(sell_recs):>7}{net:>+9.2f}%{fly:>8}{late:>8}")
    # ── 4) 回测反事实数据源(1分K重放, 最权威) ──
    out.extend(_render_backtest_section())
    out.append("  ⚠ 当前生产 SELL_THR=70; 若某 thr 净均显著优于70且卖飞/卖晚可控, "
               "再由人决定是否调(脚本不自动改)")
    out.append("")
    out.append("【结论注入提示词用】将上述分档结果反馈给 AI: 让其知道自己的给分在"
               "哪些档位与实际走势吻合/背离, 自我校准 sell_score 标尺(同买入 calibrate 精神)。")

    txt = "\n".join(out)
    REPORT.write_text(txt, encoding="utf-8")
    return txt


def main() -> int:
    force = "--force" in sys.argv
    backfill(force=force)
    print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
