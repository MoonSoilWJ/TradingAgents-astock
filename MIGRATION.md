# 迁移指南：把整套交易系统搬到家用 MacBook Pro

> 目标读者：执行迁移的 agent / 本人。按顺序执行，每步都有验证点。
> 最后更新：2026-09-22。策略参数以本文为准（与 crontab 安装脚本一致）。

## 0. 这台机器上跑着什么

| 系统 | 进程/脚本 | 频率 | 状态文件 |
|---|---|---|---|
| 游资买侧（半路板） | `scripts/youzi_live.py --ai`（常驻）+ `scripts/youzi_watchdog.sh` | 60s 扫描 | `~/.tradingagents/youzi/` |
| 游资卖侧 AI | `scripts/youzi_sell_ai.py` | 早盘 5 分钟 + 盘中 1 分钟 | 同上 |
| 秒级盘口哨兵 | `scripts/youzi_fast_watch.py`（常驻，纯代码零 LLM） | 3 秒 | `fast_book.json` / `fast_watch.jsonl` |
| T0 ETF 系统 | `scripts/t0_monitor.py`、`t0_b_idle_shadow.py`、`t0_r3_monitor.py`、`t0_sell_watch.py` | 交易时段 | `~/.tradingagents/rotation/` |
| 收盘校准 | `youzi_calibrate.py`、`youzi_sell_calibrate.py` | 15:20 / 15:25 | 成绩单 txt |

**运行态全部在 `~/.tradingagents/`（不在仓库里、不在 git 里）**——迁移的本质就是把这套状态打包带走，代码本身 git clone 即可。

## 1. 迁移顺序（第一步！防双机并行）

`positions.json` / `youzi_sold.jsonl` / `signals.json` 是单一事实源，**两台机器同时跑会重复推送 + 额度错乱**。

```bash
# 在公司机上：
crontab -l | grep -vE "youzi_live|youzi_watchdog|youzi_sell_ai|youzi_fast_watch" | crontab -
pkill -f "youzi_live.py --ai"; pkill -f "youzi_fast_watch"
# t0 系统如一并迁移，同样方式移除 t0 相关行
```

## 2. 打包状态（公司机）

```bash
cd ~/文档/TradingAgents-astock
bash scripts/backup_state.sh          # 精简包 ≈161MB（实盘必需）
bash scripts/backup_state.sh --full   # 需要回测研究缓存时（追加 ≈3.4GB）
# 产物: state_backup/state_YYYYMMDD_HHMM.tar.gz
```

包含：游资全部状态与校准数据、mb_daily.pkl(全市场日K)、min1_cache.pkl(1分K缓存)、
t0 实盘缓存(aligned_live_4y/tdx_5min_2y/backfill/full_daily)、rotation 全部状态与 journal。
**不含**（可重建/研究用）：`min30.pkl`（新机跑 `scripts/fetch_min30.py`，6-12 分钟）、
planC/pre2024/auto 等回测缓存（`--full` 才含）。

把 `state_backup/state_*.tar.gz` 和整个仓库（git push 后 clone，或直接 scp 目录，**必须包含未提交的 .env**）传到 MacBook。

## 3. 新机环境

```bash
# Python 3.10+（建议 3.11）
git clone <repo> ~/TradingAgents-astock && cd ~/TradingAgents-astock
pip3 install -r requirements.txt
python3 -c "import pandas, pytdx, akshare, langchain_core; print('依赖OK')"
```

`.env` 放仓库根目录（git 里没有，从公司机单独拷）：LLM API key +
`DINGTALK_YOUZI_WEBHOOK` + `DINGTALK_YOUZI_KEYWORD=游资`。

系统设置（防盘中休眠/断电）：
```bash
sudo pmset -a sleep 0 disksleep 0 autorestart 1
```

## 4. 恢复状态

```bash
bash scripts/restore_state.sh <state_backup/state_xxx.tar.gz>
# 解包到 ~/.tradingagents/（脚本里路径硬编码为 $HOME/.tradingagents，勿改位置）
```

## 5. 重建可重建数据

```bash
python3 scripts/fetch_mainboard_daily.py   # 精简包已含 mb_daily.pkl, 可跳过; 建议每日收盘后跑
python3 scripts/fetch_min30.py             # 6-12 分钟, 重建 min30.pkl(707M, 未入包)
```

## 6. 安装 crontab

```bash
bash scripts/install_youzi_crontab.sh
# 仓库不在 ~/文档/TradingAgents-astock 时:
#   YOUZI_DIR=$HOME/TradingAgents-astock YOUZI_PYTHON=$(which python3) bash scripts/install_youzi_crontab.sh
# 幂等: 只增删 youzi 相关系目, 不碰其他任务
```

安装内容（即当前定档）：买侧 09:35 常驻 + 看门狗(每5分钟, 日志静默>10分钟重启) +
卖侧(早盘5分钟/盘中1分钟, SELL_THR=60) + 哨兵(每2分钟拉起, 3秒轮询) + 两个校准(15:20/15:25)。
建议手动追加：`10 16 * * 1-5 cd <仓库> && <python> scripts/fetch_mainboard_daily.py >> /tmp/youzi_mb.log 2>&1`

## 7. 验证

```bash
python3 scripts/youzi_live.py --once           # 不带 --push, 看池构建+扫描
python3 scripts/youzi_sell_ai.py --dry-run     # 卖侧只读
python3 scripts/youzi_fast_watch.py            # 非交易时段会自动退出=正常
```

交易日 09:35 后检查：`/tmp/youzi.log`（买侧）、`/tmp/youzi_sell_ai.log`（卖侧）、
`/tmp/youzi_fast_watch.log`（哨兵）。**钉钉收到推送 = 全链路通。**

## 8. 排障速查

| 症状 | 处理 |
|---|---|
| 买侧反复重启 | 看 `/tmp/youzi_watchdog.log`；确认磁盘没满、`.env` 存在 |
| 卖侧不触发 | `crontab -l | grep youzi_sell`；`YOUZI_SELL_THR` 拼写（曾拼错 YOUZU） |
| pytdx 拿不到报价 | 哨兵/卖侧不用 pytdx（用腾讯快照）；买侧 pytdx 单独连接正常，若返回空等 60s 自愈 |
| 行情全部为空 | 检查 7709 出站是否被路由器拦 |
| 想重置当日配额 | 删 `~/.tradingagents/youzi/signals.json` 的 `buy_today`（或整文件，冷却会丢） |

## 9. 非交易时段网络行为（审计视角）

- **夜间/周末凌晨：零请求**（所有任务 cron 限定 1-5 + 交易时段，哨兵/买侧收盘自退）
- 工作日 09:00-09:30：watchdog 会提前拉起买侧预热（行情返回空，纯轮询）
- 15:20/15:25：校准脚本拉 akshare（设计内）
- 节假日：cron 无节假日历，9:00-15:05 会空转轮询（无害，行情为空）
- 请求总量：TDX 长连接 ~1.2 万次查询/天 + 腾讯快照 3 秒一次 ~4800 次/天 + LLM HTTPS 200-400 次/天

## 10. 策略参数备忘（2026-09-22 定档）

- 买侧：扫描/推送 6%，prob≥75，盘口≥7，位置≥7；**总仓 3 / 每日新买 2**（梯队滚动）
- 卖侧：**T+1 14:30 强制了结**（封板豁免）；封死涨停代码级禁卖；早盘 10:00 前禁卖；SELL_THR=60
- 哨兵：封单 60s 衰减>40% / 炸板 0.3% / 摸板回落 1.5%；事件交给 AI（T+0 持仓只记录）
- 校准截止：2026-09-28（试运行两周），期间**不要再改阈值/提示词**
