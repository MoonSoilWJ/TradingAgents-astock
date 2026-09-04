#!/usr/bin/env python3
"""生成聚宽「卖点对照」验证代码 — 供用户在聚宽上验证 TRIX vs 固定11:05。

产出两个单文件(可直接粘贴到聚宽, 无需上传任何其他文件):
  1. scripts/jq_sellmode_ab.py  — A / B 策略通用(信号内联), 开关 STRATEGY + SELL_MODE
  2. scripts/jq_sellmode_r3.py  — R3 canonical(月度轮动池内联), 开关 SELL_MODE

SELL_MODE 三档:
  "trix"       现状: 09:40~11:05 TRIX(5,3)死叉卖, 11:05 强制 fallback
  "fixed_1105" 建议: 不做任何提前离场判断, 无条件等 11:05 卖出
  "prev_low"   备选: 跌破前一根 5minK 低点即卖(注意聚宽未完成 bar 与本地已完成 K 的差异)

用法:
    python3 scripts/gen_jq_sellmode.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

AB_SRC = SCRIPT_DIR / "joinquant_b_strategy.py"
AB_A_SRC = SCRIPT_DIR / "joinquant_a_strategy.py"
R3_SRC = SCRIPT_DIR / "joinquant_unified_single.py"

AB_OUT = SCRIPT_DIR / "jq_sellmode_ab.py"
R3_OUT = SCRIPT_DIR / "jq_sellmode_r3.py"


def sub_once(text: str, old: str, new: str, where: str) -> str:
    """精确替换一次, 失败立即报错(防止静默不匹配导致生成错误代码)。"""
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"[生成失败] {where}: 期望匹配 1 次, 实际 {n} 次\n---\n{old[:300]}")
    return text.replace(old, new, 1)


# ══════════════════════════════════════════════════════════════════
# 1) A/B 通用文件
# ══════════════════════════════════════════════════════════════════
def gen_ab() -> None:
    b = AB_SRC.read_text(encoding="utf-8")
    a = AB_A_SRC.read_text(encoding="utf-8")

    # 提取 A 信号字典那一行
    m = re.search(r"^JQ_LOCAL_A_PICKS = \{.*\}$", a, re.M)
    if not m:
        raise SystemExit("[生成失败] 在 joinquant_a_strategy.py 中找不到 JQ_LOCAL_A_PICKS")
    a_picks_line = m.group(0)

    # ① 头部 docstring 整块替换
    b = sub_once(
        b,
        '"""\nB 策略 · 聚宽回测 — 严格等价本地 22-26 回测版',
        '"""\nA/B 策略 · 聚宽【卖点对照】验证版  (TRIX vs 固定11:05)',
        "AB 头部起始",
    )
    end = b.index('"""', b.index("A/B 策略 · 聚宽【卖点对照】"))
    new_doc = '''"""
A/B 策略 · 聚宽【卖点对照】验证版   (TRIX  vs  固定11:05  vs  跌破前低)
================================================================================
目的: 验证本地回测发现的结构 —— 「必须上午了结, 指标是负贡献」。

本地无偏5min实测(2022-11-15~2026-07-31, 896天, 费率万3):
    B 买入端 441笔: TRIX死叉 +571.86%  |  固定11:05 +982.84%  |  固定14:50 -4.09%
    A 买入端 338笔: TRIX死叉 +426.05%  |  固定11:05 +744.11%  |  固定14:50 +27.63%
    R3买入端 255笔: TRIX死叉 +255.50%  |  固定11:05 +344.51%  |  固定14:50 -4.71%
    → 三个买入端「上午溢价」分年同号均为 4/4, 结构一致。

★ 关键机制(平均日内收益路径, 毛收益%, 相对 14:50 买入价):
    买入端   09:35   10:00   11:05   11:30   13:05   14:00   14:50
    A       +0.579  +0.651  +0.755  +0.710  +0.698  +0.481  +0.218
    B       +0.464  +0.565  +0.658  +0.616  +0.597  +0.405  +0.125
    R3      +0.625  +0.680  +0.694  +0.604  +0.609  +0.430  +0.094
                              ▲峰值            └── 单调回吐 ──┘
    隔夜情绪溢价在次日开盘兑现, 上午见顶, 下午全部回吐 → 必须上午了结。

--------------------------------------------------
【两个开关】(改这两行即可, 其余不要动)
    STRATEGY  = "A" 或 "B"          # 选股信号(A=hybrid-A滚动优质池 / B=全市场Top1)
    SELL_MODE = "trix"              # 现状基线
             = "fixed_1105"         # 建议方案: 无条件 11:05 卖
             = "prev_low"           # 备选: 跌破前一根 5minK 低点卖

★ 建议跑法(共 4 次回测, 每次只改一行):
    ① STRATEGY="B", SELL_MODE="trix"        → 基线, 应对齐本地 +571.86%
    ② STRATEGY="B", SELL_MODE="fixed_1105"  → 验证增益, 本地 +982.84%
    ③ STRATEGY="A", SELL_MODE="trix"        → 基线, 对应实盘 t0_monitor
    ④ STRATEGY="A", SELL_MODE="fixed_1105"  → 实盘改动依据

--------------------------------------------------
【回测设置】★三项必须正确, 否则数字无效★
    频率: 【分钟】  (日线会摧毁隔夜跳空edge并禁用TRIX卖点;
                    本地同数据实测: 分钟+472% vs 日线收盘-44.48%)
    起始/结束: 2022-06-15 ~ 2026-07-31 (与内联信号窗口对齐)
    资金: 20000
    滑点: 默认 FixedSlippage(0) 零滑点(对齐本地口径);
          想看含真实滑点的保守数字, 注释掉 set_slippage 那行重跑。

【验收】回测日志里搜 [DIAG], 必须看到:
    handle_data触发 > 0 次   (若=0 说明频率是日线, 数字作废, 改分钟重跑)
    SELL_MODE=xxx            (确认模式已生效)
    买入笔数 ≈ 441(B) / ≈ 338(A)  (本地笔数; 差异主要来自停牌)
"""
'''
    b = new_doc + b[end + 3:]

    # ② 在 B picks 之后插入 A picks
    b = sub_once(
        b,
        "\n_HAVE_LOCAL = True\n",
        "\n" + a_picks_line + "\n\n_HAVE_LOCAL = True\n",
        "AB 插入 A_PICKS",
    )

    # ③ 常量区加两个开关
    b = sub_once(
        b,
        'ALL_ETFS = list(JQ_LOCAL_CANDIDATE.keys())\n',
        'ALL_ETFS = list(JQ_LOCAL_CANDIDATE.keys())\n'
        '\n'
        '# ===== 卖点对照开关(本验证脚本唯一需要手动改的地方) =====\n'
        'STRATEGY = "B"            # "A" = hybrid-A 滚动优质池选股 | "B" = 全市场Top1≥3%\n'
        'SELL_MODE = "fixed_1105"  # "trix"(现状) / "fixed_1105"(建议) / "prev_low"(备选)\n',
        "AB 开关常量",
    )

    # ④ prepare_from_local 按 STRATEGY 取信号
    b = sub_once(
        b,
        '    code = to_jq_code(JQ_LOCAL_B_PICKS.get(day))',
        '    PICKS = JQ_LOCAL_A_PICKS if STRATEGY.upper() == "A" else JQ_LOCAL_B_PICKS\n'
        '    code = to_jq_code(PICKS.get(day))',
        "AB 选股字典切换",
    )

    # ⑤ sell_monitor 插入 SELL_MODE 分支(在 TRIX 检测之前)
    b = sub_once(
        b,
        '    # --- TRIX(5,3) 死叉检测 ---',
        '    # --- SELL_MODE=fixed_1105: 不做任何提前离场判断, 等 11:05 fallback 无条件卖 ---\n'
        '    if SELL_MODE == "fixed_1105":\n'
        '        return\n'
        '\n'
        '    # --- SELL_MODE=prev_low: 跌破前一根 5minK 低点即卖(按当前价成交) ---\n'
        '    if SELL_MODE == "prev_low":\n'
        '        try:\n'
        '            df5 = get_price(code, end_date=context.current_dt, count=5,\n'
        '                            frequency="5m", fields=["low"], skip_paused=True)\n'
        '            if len(df5) >= 2 and float(df5["low"].iloc[-1]) < float(df5["low"].iloc[-2]):\n'
        '                if _do_sell(context, code, "SELL_PREVLOW", g.buy_price):\n'
        '                    g.hold_code = None\n'
        '                    g.buy_date = None\n'
        '                    g.sold_today = True\n'
        '        except Exception as e:\n'
        '            log.error(f"prev_low 计算异常 {code}: {e}")\n'
        '        return\n'
        '\n'
        '    # --- TRIX(5,3) 死叉检测 ---',
        "AB sell_monitor 分支",
    )

    # ⑥ initialize 日志加开关回显
    b = sub_once(
        b,
        'log.info(f"[DIAG] 版本=verified_sell_v2 | _HAVE_LOCAL={_HAVE_LOCAL} | 信号数={_nsig} | 候选池{len(ALL_ETFS)} | 窗口{JQ_BACKTEST_WINDOW}")',
        'log.info(f"[DIAG] 版本=verified_sell_v2 | STRATEGY={STRATEGY} | SELL_MODE={SELL_MODE} | "\n'
        '             f"_HAVE_LOCAL={_HAVE_LOCAL} | 信号数={_nsig} | 候选池{len(ALL_ETFS)} | 窗口{JQ_BACKTEST_WINDOW}")',
        "AB DIAG 日志",
    )

    # ⑦ 信号数统计也按 STRATEGY
    b = sub_once(
        b,
        '    _nsig = len([d for d in JQ_LOCAL_B_PICKS if JQ_LOCAL_B_PICKS[d]])',
        '    _PK = JQ_LOCAL_A_PICKS if STRATEGY.upper() == "A" else JQ_LOCAL_B_PICKS\n'
        '    _nsig = len([d for d in _PK if _PK[d]])',
        "AB 信号计数",
    )

    # ⑧ 末尾使用说明替换
    tail_at = b.index("# ============================================================\n# 使用说明")
    b = b[:tail_at] + '''# ============================================================
# 使用说明
# ============================================================
# 1. 聚宽 → 新建策略 → 粘贴本文件(信号已内联, 无需上传任何其他文件)
# 2. 改两行开关: STRATEGY = "A"/"B" ; SELL_MODE = "trix"/"fixed_1105"/"prev_low"
# 3. 回测设置: ★频率必须=【分钟】★ | 2022-06-15~2026-07-31 | 资金 20000
# 4. 看日志 [DIAG] 确认 STRATEGY/SELL_MODE 生效; 看 [SUMMARY] 确认:
#      · handle_data触发 0 次 => 频率是日线, 数字作废
#      · 买入笔数 ≈ 441(B) / 338(A)
# 5. 四次回测对比表填这里:
#      ┌──────────┬────────────┬──────────────┬─────────┐
#      │ STRATEGY │ SELL_MODE  │ 聚宽收益/笔数 │ 本地回测 │
#      ├──────────┼────────────┼──────────────┼─────────┤
#      │ B        │ trix       │              │ +571.86% │
#      │ B        │ fixed_1105 │              │ +982.84% │
#      │ A        │ trix       │              │ +426.05% │
#      │ A        │ fixed_1105 │              │ +744.11% │
#      └──────────┴────────────┴──────────────┴─────────┘
#    ★ 判据: 只要聚宽侧 fixed_1105 也显著优于 trix, 即证明本地结论非引擎假象,
#      可据此改实盘/影子盘卖点。
# ============================================================
'''

    AB_OUT.write_text(b, encoding="utf-8")
    print(f"✅ 生成 {AB_OUT.name}  ({len(b.splitlines())} 行)")


# ══════════════════════════════════════════════════════════════════
# 2) R3 文件
# ══════════════════════════════════════════════════════════════════
def gen_r3() -> None:
    s = R3_SRC.read_text(encoding="utf-8")

    # ① 头部: 在 docstring 之前插入验证说明块
    banner = '''"""
================================================================================
R3 · 聚宽【卖点对照】验证版   (TRIX  vs  固定11:05  vs  跌破前低)
================================================================================
本文件 = canonical R3 (joinquant_unified_single.py) + 一个 SELL_MODE 开关,
其余逻辑(月度轮动池/14:40选股/14:45复核/满仓进攻)【一字未改】。

目的: 本地 R3 口径与聚宽 canonical 存在差异(本地引擎复刻不了聚宽的
      sell_monitor 每分钟未完成bar现价触发), 故 R3 的卖点裁决必须在聚宽侧做。

本地 R3 口径实测(2022-11-15~2026-07-31, 255笔):
    TRIX死叉 +255.50% | 跌破前低 +376.20% | 固定11:05 +344.51% | 固定14:50 -4.71%
    上午溢价 +0.600pp, 分年同号 4/4, 滚动60笔恒正率 100%

★ 唯一需要改的一行:
    SELL_MODE = "trix"        # 现状基线(R3 canonical), 应对齐 +1861% 那版
             = "fixed_1105"  # 建议方案: 跳过止损与TRIX, 无条件 11:05 卖
             = "prev_low"    # 备选: 跌破前一根 5minK 低点卖

★ 建议跑法(共 2~3 次回测, 每次只改一行):
    ① SELL_MODE="trix"        → 基线, 应对齐 R3 canonical
    ② SELL_MODE="fixed_1105"  → 验证增益
    ③ SELL_MODE="prev_low"    → 备选对照(可选)

★ 回测设置: 频率【分钟】| 资金 100000 | 窗口建议 2022-06-15~2026-07-31
   (R3 canonical 全周期是 2014-2026 +1861%, 可先用 22-26 段与 A/B 对照)

★ 注意: R3 现状带 STOP_LOSS_PCT=-5% 止损。SELL_MODE="fixed_1105"/"prev_low"
   会【跳过止损】(本地口径无止损)。若要纯净对照, 可把 STOP_LOSS_PCT 设为
   -999 让止损永不触发, 再跑 "trix" 基线 —— 这样两组差异只有 TRIX 本身。
================================================================================
"""

'''
    s = banner + s

    # ② 常量区加 SELL_MODE
    s = sub_once(
        s,
        'SELECT_MODE = "a_top1"\n',
        'SELECT_MODE = "a_top1"\n'
        '\n'
        '# ===== 卖点对照开关(本验证脚本唯一需要手动改的地方) =====\n'
        '#   "trix"       = 现状: 止损-5% > TRIX(5,3)死叉 > 11:05 强制\n'
        '#   "fixed_1105" = 跳过止损与TRIX, 无条件等 11:05 卖\n'
        '#   "prev_low"   = 跳过止损与TRIX, 跌破前一根 5minK 低点即卖\n'
        'SELL_MODE = "fixed_1105"\n',
        "R3 开关常量",
    )

    # ③ sell_monitor 插入分支(在止损之前, 11:05强制/09:40之前 之后)
    s = sub_once(
        s,
        '    current_price = get_current_data()[code].last_price\n'
        '    ret_now = (current_price - g.buy_price) / g.buy_price if g.buy_price else 0\n'
        '    if ret_now <= STOP_LOSS_PCT:\n'
        '        _close("止损", current_price)\n'
        '        return\n',
        '    # --- SELL_MODE=fixed_1105: 跳过止损与TRIX, 无条件等 11:05 强制卖 ---\n'
        '    if SELL_MODE == "fixed_1105":\n'
        '        return\n'
        '\n'
        '    # --- SELL_MODE=prev_low: 跌破前一根 5minK 低点即卖 ---\n'
        '    if SELL_MODE == "prev_low":\n'
        '        try:\n'
        '            df5 = get_price(code, end_date=context.current_dt, count=5,\n'
        '                            frequency="5m", fields=["low"], skip_paused=True)\n'
        '            if len(df5) >= 2 and float(df5["low"].iloc[-1]) < float(df5["low"].iloc[-2]):\n'
        '                _close("跌破前低", get_current_data()[code].last_price)\n'
        '        except Exception as e:\n'
        '            log.error(f"prev_low计算异常 {code}: {e}")\n'
        '        return\n'
        '\n'
        '    current_price = get_current_data()[code].last_price\n'
        '    ret_now = (current_price - g.buy_price) / g.buy_price if g.buy_price else 0\n'
        '    if ret_now <= STOP_LOSS_PCT:\n'
        '        _close("止损", current_price)\n'
        '        return\n',
        "R3 sell_monitor 分支",
    )

    # ④ initialize 日志加回显
    s = sub_once(
        s,
        'log.info(f"[init] SELECT_MODE={SELECT_MODE} ATTACK_POOL_RULE={ATTACK_POOL_RULE} "',
        'log.info(f"[init] ★SELL_MODE={SELL_MODE}★ | SELECT_MODE={SELECT_MODE} ATTACK_POOL_RULE={ATTACK_POOL_RULE} "',
        "R3 init 日志",
    )

    R3_OUT.write_text(s, encoding="utf-8")
    print(f"✅ 生成 {R3_OUT.name}  ({len(s.splitlines())} 行)")


if __name__ == "__main__":
    gen_ab()
    gen_r3()
    print("\n两个文件已生成, 语法校验:")
    for f in (AB_OUT, R3_OUT):
        import py_compile
        try:
            py_compile.compile(str(f), doraise=True)
            print(f"  ✅ {f.name} 语法通过 (聚宽 API 在本地不可用属正常, 仅校验语法)")
        except Exception as e:
            print(f"  ❌ {f.name} 语法错误: {e}")
            sys.exit(1)
