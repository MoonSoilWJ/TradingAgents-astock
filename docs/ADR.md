# ADR — TradingAgents-astock 架构决策记录

> 首次写入 2026-09-22。
> 本文件是 codebase-memory ADR 的 git 持久副本（MCP 侧 ADR 存在 `.codebase-memory/graph.db.zst` 内，该目录已被 gitignore，不入库，故镜像一份到仓库）。
> 更新任一侧时请同步另一侧。策略结论以 DEV_LOG.md 与各回测落盘 JSON 为准。

## 1. 项目定位：一个仓库、两套系统

本仓库实际包含**两个耦合极弱的系统**，这是最容易踩的认知坑：

| | `tradingagents/` | `scripts/` |
|---|---|---|
| 性质 | A股多智能体 LLM 分析框架（库） | ~240 个独立量化脚本（工具群） |
| 编排 | LangGraph 状态图 | 无编排，各自 `main()` |
| 规模 | 78 个 py，142~103 个高内聚符号簇 | 224 个 py，占索引绝大多数 |
| 依赖重心 | LLM provider / 数据源 vendor 路由 | T+0 ETF 选股、卖点、回测、实盘推送 |

`scripts/` 下大量脚本 `fan-in=0, fan-out=0`（一次性验证/回填/诊断脚本），彼此不互相 import；真正被复用的核心只集中在少数几个模块（见 §3）。

## 2. 目录分层

- `tradingagents/` — 核心库。`graph/trading_graph.py::TradingAgentsGraph` 是主入口（`propagate()` → `_run_graph()` → `finalize_graph_run()`），含 checkpoint 续跑（`prepare_graph_run` 返回 `initial_state=None` 走 LangGraph resume）。
- `cli/main.py` — CLI 入口（`init_for_analysis` / `run_analysis`）。
- `web/` — Streamlit WebUI（35 个 py）。策略页 `web/strategy/` 与 `scripts/` 的策略常量**必须同步**。
- `scripts/` — 回测、实盘监控、数据回填、聚宽导出。
- `strategies/` — 策略注册与状态。

## 3. 热点符号（改动前必须评估影响面）

- `scripts/t0_etf_list.get_all_t0_etfs` — **fan-in 104，全仓最高**。T+0 ETF 候选池根（手工 4 表 + `scripts/auto_t0_etfs.json` 自动层）。改动它等于改动所有策略的宇宙。
- `scripts/quality_pool.build_picks_hybrid` — fan-in 22，实盘 A 选股核心（regime → 优质滚动池 / 原 T0 池）。
- `scripts/search_t0_time_combo.{bars_until,precompute_picks}` — 参数搜索核心。
- `tradingagents` 簇：`route_to_vendor` / `settlement_rule` / `safe_ticker_component` / `_normalize_ticker`（cohesion 0.6385，跨 cli/scripts/tests/web 使用）。
- 日内簇（cohesion 0.7381）：`run_intraday_cycle` / `run_daemon` / `render_intraday_panel` / `send_markdown`。

## 4. 跨模块边界（少数真实耦合点）

- `push_kc50_defensive_signal` → `backtest_588000_n12`（科创50 防御信号推送依赖回测脚本）。
- `search_idle_window_wf` → `backtest_t0_today1`。
- `joinquant_kc50_defensive_v3_minute` — 纯出口（聚宽侧），只出不进。
- HTTP 出口仅 4 处：gtimg 日K / eastmoney 资金流 / alphavantage / openrouter。**数据源集中在少数 vendor 模块，换源改这里。**

## 5. 方法论纪律（本项目最重要的软约束）

1. **回测必须等价实盘**：选股/确认/卖点三段都要逐字对齐实盘代码（`build_picks_hybrid` vs 全市场 Top1；14:40 锁 + 14:45 复核；TRIX(5,3) 死叉 + 11:05 fallback）。
2. **成交价不得乐观假设**：追踪止盈类卖点若按精确止损价成交即虚增收益，必须用保守成交价（穿价按该 K 收盘）复核。
3. **禁止未来函数**：月度轮动池必须 `pool_as_of(上月)`，当月交易用上月末池。
4. **回测候选池 = `get_all_t0_etfs()` ∩ 5min 数据覆盖**，新标的上市前自动排除，天然无前视。
5. 负结果同样要记录（Overlay 防守腿弱市、Kronos 方向预测、过滤版选股均已证伪，勿重复探索）。

## 6. 在跑策略格局

- 实盘 A：`scripts/t0_monitor.py`（真下单）。
- SHADOW B：`scripts/t0_b_idle_shadow.py`（只记不下单，独立 state/journal）。
- SHADOW R3：`scripts/t0_r3_monitor.py`（本地，对齐聚宽 canonical R3；14:40 锁领头羊 + 14:45 复核）。
- 涨停板系统：`scripts/youzi_*.py`（买侧 `youzi_ai` / 卖侧 `youzi_sell_ai` 批量判定 + flock 单实例锁 + watchdog）。

## 7. 工具链

- `codegraph` — `codegraph_explore`（源码+调用链）／`impact`／`callers`。全量重建 1.6s，改完代码跑 `codegraph sync`。
- `codebase-memory-mcp` — 架构/依赖/语义查询，落盘 `.codebase-memory/graph.db.zst`。
- 两者目录均已加入 `.gitignore`（本地产物，可重建，不入库）。

## 8. 维护约定

- 改 `scripts/` 中策略常量（卖点、选股、确认时点、候选池）→ **同步 `web/strategy/` 下 WebUI**。
- 新增/删除脚本后重跑索引：`codegraph sync` + `index_repository(mode=fast)`。
- 本 ADR 随重大架构变化用 `manage_adr(mode=set_sections)` 增量更新，勿全量重写。
