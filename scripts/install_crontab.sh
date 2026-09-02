#!/bin/bash
# 安装/更新监控定时任务
#
# 板块轮动: rotation_monitor.py —— 已于 2026-09-02 下线，crontab 中不再保留，
#           且 --all 也不会再重装（如确需恢复用 --install-rotation）。
# T+0 ETF:  14:45信号/14:50买 + 09:40~11:05每50秒5分K TRIX(5,3)卖出检查
# 分钟K缓存: cache_min_data.py（15:10 小池子 + 15:35 全市场）
# Walk-Forward: 每月首个工作日 9:00 复核参数（仅「可考虑切换」时钉钉推送）
#
# 默认仅追加/更新 T+0 任务，保留 crontab 中已有 rotation_monitor 及其他任务。
#
# 用法:
#   bash scripts/install_crontab.sh              # 只安装 T+0（推荐，不碰已有轮动）
#   bash scripts/install_crontab.sh --all        # 重置 T+0（不再包含板块轮动）
#   bash scripts/install_crontab.sh --t0-only    # 同默认
#   bash scripts/install_crontab.sh --uninstall-rotation  # 卸载板块轮动监控（下线用）
#   bash scripts/install_crontab.sh --install-rotation    # 恢复板块轮动监控
#   bash scripts/install_crontab.sh --install-walk-forward  # 追加每月 walk-forward 任务
#   bash scripts/install_crontab.sh --install-b-idle-shadow # 追加 B+idle SHADOW（新策略影子, 不改实盘）
#   bash scripts/install_crontab.sh --install-pair-shadow  # 追加 配对收敛薄补充腿 SHADOW（核心B腿熄火时非趋势期点缀, 不下单）
#   bash scripts/install_crontab.sh --install-r3     # 追加 R3 月度轮动 SHADOW（本地实跑影子, 选股走月度轮动池, 不下单）
#   bash scripts/install_crontab.sh --install-pool-refresh  # 追加 池子月度维护流水线(refresh_t0_pool -> export_jq_pools, 每月1号)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON3="$(which python3)"

ROTATION_CMD="cd ${PROJECT_DIR} && ${PYTHON3} scripts/rotation_monitor.py"
T0_CMD="cd ${PROJECT_DIR} && ${PYTHON3} scripts/t0_monitor.py"

ROTATION_TIMES=(
    "30 09"   # 09:30
    "0 15"    # 15:00
)

T0_SELL_WATCH="40 9"        # 09:40 启动 t0_sell_watch.py，窗口内每 50 秒 --sell-check
T0_SIGNAL_CRON="45 14"      # 14:45 买入信号
T0_CACHE_CRON="10 15"       # 15:10 小池子 1分K/5分K
T0_CACHE_ALLMARKET_CRON="35 15"  # 15:35 全市场 ~1733 只
T0_WATCH_CMD="cd ${PROJECT_DIR} && ${PYTHON3} scripts/t0_sell_watch.py"
CACHE_CMD="cd ${PROJECT_DIR} && ${PYTHON3} scripts/cache_min_data.py >> ${HOME}/.tradingagents/rotation/min_cache.log 2>&1"
CACHE_ALLMARKET_CMD="cd ${PROJECT_DIR} && ${PYTHON3} scripts/cache_min_data.py --all-market --workers 8 >> ${HOME}/.tradingagents/rotation/min_cache_allmarket.log 2>&1"
WF_CMD="cd ${PROJECT_DIR} && ${PYTHON3} scripts/t0_walk_forward.py >> ${HOME}/.tradingagents/rotation/walk_forward.log 2>&1"
WF_CRON="0 9 1-7 * 1"   # 每月 1~7 日中的周一 9:00（首个工作日近似）

# 池子月度维护流水线: 先刷新 auto 候选池(新上市 T+0 漏收补齐), 再重新生成月度轮动池(R1~R6)
POOL_CMD="cd ${PROJECT_DIR} && ${PYTHON3} scripts/refresh_t0_pool.py && ${PYTHON3} scripts/export_jq_pools.py"
POOL_LOG=">> ${HOME}/.tradingagents/rotation/pool_refresh.log 2>&1"
POOL_CRON="0 9 1 * *"   # 每月 1 号 9:00

MODE="t0-only"
if [[ "${1:-}" == "--all" ]]; then
    MODE="all"
elif [[ "${1:-}" == "--install-rotation" ]]; then
    MODE="rotation-only"
elif [[ "${1:-}" == "--uninstall-rotation" ]]; then
    MODE="rotation-uninstall"
elif [[ "${1:-}" == "--install-walk-forward" ]]; then
    MODE="walk-forward-only"
elif [[ "${1:-}" == "--install-b-idle-shadow" ]]; then
    MODE="b-idle-shadow-only"
elif [[ "${1:-}" == "--install-pair-shadow" ]]; then
    MODE="pair-shadow-only"
elif [[ "${1:-}" == "--install-r3" ]]; then
    MODE="r3-shadow-only"
elif [[ "${1:-}" == "--install-pool-refresh" ]]; then
    MODE="pool-refresh-only"
fi

BIDLE_CMD="cd ${PROJECT_DIR} && ${PYTHON3} scripts/t0_b_idle_shadow.py"
BIDLE_LOG=">> ${HOME}/.tradingagents/rotation/b_idle_shadow.log 2>&1"
BIDLE_SIGNAL_CRON="45 14"   # 14:45 核心 B 信号
BIDLE_IDLE_SELL_CRON="49 14" # 14:49 idle 次日固定卖 (先平昨日持仓)
BIDLE_IDLE_BUY_CRON="50 14" # 14:50 idle 动量腿买入 (核心未命中时)
BIDLE_SELL_WATCH="40 9"     # 09:40 启动核心 B 的 hybrid 卖出监控 (09:40~11:05 每50秒循环)

# 配对收敛薄补充腿 SHADOW(独立, 不读写 B 核心 shadow state)
PAIR_CMD="cd ${PROJECT_DIR} && ${PYTHON3} scripts/t0_pair_shadow.py"
PAIR_LOG=">> ${HOME}/.tradingagents/rotation/pair_shadow.log 2>&1"
PAIR_SIGNAL_CRON="05 15"   # 15:05 判定配对信号(确保当日1分K完整, 用 14:55 收盘)
PAIR_SELL_CRON="55 14"     # 14:55 次日判定平仓(比值回归或≤MAX_DAYS)

# R3 月度轮动 SHADOW(本地实跑影子, 选股走月度轮动池, 不下单)
R3_CMD="cd ${PROJECT_DIR} && ${PYTHON3} scripts/t0_r3_monitor.py"
R3_LOG=">> ${HOME}/.tradingagents/rotation/r3_shadow.log 2>&1"
R3_SIGNAL_CRON="45 14"   # 14:45 R3 复核+成交信号
R3_PICK_CRON="40 14"     # 14:40 R3 选股锁定领头羊(对齐聚宽A: 14:40选)
R3_SELL_WATCH="40 9"     # 09:40 启动 TRIX(5,3)卖出监控 (09:40~11:05 每50秒循环)

echo "=== 安装监控定时任务 ==="
echo ""
echo "模式: ${MODE}"
echo ""

EXISTING="$(crontab -l 2>/dev/null || true)"

case "${MODE}" in
    t0-only)
        echo "保留已有 crontab（板块轮动条目不再维护），仅更新 t0_monitor.py 条目"
        FILTERED="$(echo "${EXISTING}" | grep -v "t0_monitor.py" | grep -v "t0_sell_watch.py" | grep -v "cache_min_data.py" || true)"
        {
            echo "${FILTERED}"
            echo "${T0_SELL_WATCH} * * 1-5 ${T0_WATCH_CMD}"
            echo "${T0_SIGNAL_CRON} * * 1-5 ${T0_CMD} --signal"
            echo "${T0_CACHE_CRON} * * 1-5 ${CACHE_CMD}"
            echo "${T0_CACHE_ALLMARKET_CRON} * * 1-5 ${CACHE_ALLMARKET_CMD}"
        } | sed '/^$/d' | crontab -
        ;;
    all)
        echo "重置 T+0（移除旧 rotation/t0 条目后重装 T+0；板块轮动已下线，不重装）"
        FILTERED="$(echo "${EXISTING}" | grep -v "rotation_monitor.py" | grep -v "t0_monitor.py" | grep -v "t0_sell_watch.py" | grep -v "cache_min_data.py" || true)"
        {
            echo "${FILTERED}"
            echo "${T0_SELL_WATCH} * * 1-5 ${T0_WATCH_CMD}"
            echo "${T0_SIGNAL_CRON} * * 1-5 ${T0_CMD} --signal"
            echo "${T0_CACHE_CRON} * * 1-5 ${CACHE_CMD}"
            echo "${T0_CACHE_ALLMARKET_CRON} * * 1-5 ${CACHE_ALLMARKET_CMD}"
        } | sed '/^$/d' | crontab -
        ;;
    rotation-uninstall)
        echo "卸载板块轮动监控（移除 rotation_monitor.py 条目，其余保留）"
        FILTERED="$(echo "${EXISTING}" | grep -v "rotation_monitor.py" || true)"
        {
            echo "${FILTERED}"
        } | sed '/^$/d' | crontab -
        ;;
    rotation-only)
        echo "仅更新 rotation_monitor.py，保留 t0 及其他条目"
        FILTERED="$(echo "${EXISTING}" | grep -v "rotation_monitor.py" || true)"
        {
            echo "${FILTERED}"
            for sched in "${ROTATION_TIMES[@]}"; do
                echo "${sched} * * 1-5 ${ROTATION_CMD}"
            done
        } | sed '/^$/d' | crontab -
        ;;
    walk-forward-only)
        echo "追加 t0_walk_forward.py（每月首个工作日 9:00，建议切换时钉钉推送）"
        FILTERED="$(echo "${EXISTING}" | grep -v "t0_walk_forward.py" || true)"
        {
            echo "${FILTERED}"
            echo "${WF_CRON} ${WF_CMD}"
        } | sed '/^$/d' | crontab -
        ;;
    b-idle-shadow-only)
        echo "追加 t0_b_idle_shadow.py（B+idle 新策略影子，仅记录不下单，不改实盘）"
        FILTERED="$(echo "${EXISTING}" | grep -v "t0_b_idle_shadow.py" || true)"
        {
            echo "${FILTERED}"
            # 14:45 核心 B 信号
            echo "${BIDLE_SIGNAL_CRON} * * 1-5 ${BIDLE_CMD} --signal ${BIDLE_LOG}"
            # 14:50 idle 动量腿买入 (核心未命中时)
            echo "${BIDLE_IDLE_BUY_CRON} * * 1-5 ${BIDLE_CMD} --idle-buy ${BIDLE_LOG}"
            # 09:40~11:05 核心 B 的 hybrid 卖出监控 (TRIX死叉/追踪回落0.5%, 每50秒循环, 11:05收盘fallback)
            echo "${BIDLE_SELL_WATCH} * * 1-5 ${BIDLE_CMD} --sell-loop ${BIDLE_LOG}"
            # 14:50 idle 次日固定卖
            echo "${BIDLE_IDLE_SELL_CRON} * * 1-5 ${BIDLE_CMD} --sell-check --idle-sell ${BIDLE_LOG}"
        } | sed '/^$/d' | crontab -
        ;;
    pair-shadow-only)
        echo "追加 t0_pair_shadow.py（配对收敛薄补充腿影子，仅记录不下单，不改实盘）"
        FILTERED="$(echo "${EXISTING}" | grep -v "t0_pair_shadow.py" || true)"
        {
            echo "${FILTERED}"
            # 15:05 判定配对信号(动量熄火 + 非趋势)
            echo "${PAIR_SIGNAL_CRON} * * 1-5 ${PAIR_CMD} --signal ${PAIR_LOG}"
            # 14:55 次日判定平仓(比值回归或超时)
            echo "${PAIR_SELL_CRON} * * 1-5 ${PAIR_CMD} --sell-check ${PAIR_LOG}"
        } | sed '/^$/d' | crontab -
        ;;
    r3-shadow-only)
        echo "追加 t0_r3_monitor.py（R3 月度轮动影子，本地实跑仅记录不下单，不改实盘）"
        FILTERED="$(echo "${EXISTING}" | grep -v "t0_r3_monitor.py" || true)"
        {
            echo "${FILTERED}"
            # 14:40 R3 选股锁定领头羊 (对齐聚宽A: 14:40选)
            echo "${R3_PICK_CRON} * * 1-5 ${R3_CMD} --pick ${R3_LOG}"
            # 14:45 R3 复核+成交信号 (对齐聚宽A: 14:45复核)
            echo "${R3_SIGNAL_CRON} * * 1-5 ${R3_CMD} --signal ${R3_LOG}"
            # 09:40~11:05 核心 R3 的 TRIX(5,3)卖出监控 (每50秒循环, 11:05收盘fallback)
            echo "${R3_SELL_WATCH} * * 1-5 ${R3_CMD} --sell-loop ${R3_LOG}"
        } | sed '/^$/d' | crontab -
        ;;
    pool-refresh-only)
        echo "追加 池子月度维护流水线(refresh_t0_pool.py -> export_jq_pools.py, 每月1号)"
        FILTERED="$(echo "${EXISTING}" | grep -v "refresh_t0_pool.py" | grep -v "export_jq_pools.py" || true)"
        {
            echo "${FILTERED}"
            echo "${POOL_CRON} ${POOL_CMD} ${POOL_LOG}"
        } | sed '/^$/d' | crontab -
        ;;
esac

echo "✅ 定时任务已安装"
echo ""
echo "当前 crontab:"
crontab -l
echo ""
echo "手动测试:"
echo "  cd ${PROJECT_DIR} && python3 scripts/rotation_monitor.py --dry-run"
echo "  cd ${PROJECT_DIR} && python3 scripts/t0_monitor.py --dry-run --signal"
echo "  cd ${PROJECT_DIR} && python3 scripts/t0_monitor.py --dry-run --sell-check"
echo "  cd ${PROJECT_DIR} && python3 scripts/t0_sell_watch.py"
echo "  cd ${PROJECT_DIR} && python3 scripts/t0_walk_forward.py --test-push"
echo "  cd ${PROJECT_DIR} && python3 scripts/cache_min_data.py --dry-run"
echo "  cd ${PROJECT_DIR} && python3 scripts/cache_min_data.py --all-market --dry-run"
echo ""
echo "安装 B+idle SHADOW 定时 (新策略影子):"
echo "  bash scripts/install_crontab.sh --install-b-idle-shadow"
echo ""
echo "安装 配对收敛薄补充腿 SHADOW 定时:"
echo "  bash scripts/install_crontab.sh --install-pair-shadow"
echo ""
echo "仅卸载 板块轮动监控（已下线）:"
echo "  bash scripts/install_crontab.sh --uninstall-rotation"
echo "恢复 板块轮动监控:"
echo "  bash scripts/install_crontab.sh --install-rotation"
echo ""
echo "仅卸载 T+0 任务:"
echo "  crontab -l | grep -v t0_monitor.py | grep -v t0_sell_watch.py | grep -v cache_min_data.py | crontab -"
echo "仅卸载 B+idle SHADOW:"
echo "  crontab -l | grep -v t0_b_idle_shadow.py | crontab -"
echo ""
echo "B+idle SHADOW 手动测试:"
echo "  cd ${PROJECT_DIR} && python3 scripts/t0_b_idle_shadow.py --signal --dry-run"
echo "  cd ${PROJECT_DIR} && python3 scripts/t0_b_idle_shadow.py --idle-buy --dry-run"
echo "  cd ${PROJECT_DIR} && python3 scripts/t0_b_idle_shadow.py --sell-check --dry-run"
echo "  cd ${PROJECT_DIR} && python3 scripts/t0_b_idle_shadow.py --sell-check --idle-sell --dry-run"
echo "  cd ${PROJECT_DIR} && python3 scripts/t0_pair_shadow.py --signal --dry-run"
echo "  cd ${PROJECT_DIR} && python3 scripts/t0_pair_shadow.py --sell-check --dry-run"
echo ""
echo "安装 R3 月度轮动 SHADOW 定时 (本地实跑影子):"
echo "  bash scripts/install_crontab.sh --install-r3"
echo ""
echo "仅卸载 R3 SHADOW:"
echo "  crontab -l | grep -v t0_r3_monitor.py | crontab -"
echo ""
echo "R3 SHADOW 手动测试:"
echo "  cd ${PROJECT_DIR} && python3 scripts/t0_r3_monitor.py --pick --dry-run"
echo "  cd ${PROJECT_DIR} && python3 scripts/t0_r3_monitor.py --signal --dry-run"
echo "  cd ${PROJECT_DIR} && python3 scripts/t0_r3_monitor.py --sell-check --dry-run"
echo "  cd ${PROJECT_DIR} && python3 scripts/t0_r3_monitor.py --sell-loop --dry-run"
echo ""
echo "安装 池子月度维护流水线 定时 (refresh_t0_pool -> export_jq_pools, 每月1号):"
echo "  bash scripts/install_crontab.sh --install-pool-refresh"
echo ""
echo "仅卸载 池子维护:"
echo "  crontab -l | grep -v refresh_t0_pool.py | grep -v export_jq_pools.py | crontab -"
echo ""
echo "池子维护 手动测试（仅打印差异, 不写文件 / 不重生成）:"
echo "  cd ${PROJECT_DIR} && python3 scripts/refresh_t0_pool.py --dry-run"
echo "手动立即执行整条流水线（写文件 + 重生成 jq_attack_pools.py）:"
echo "  cd ${PROJECT_DIR} && python3 scripts/refresh_t0_pool.py && python3 scripts/export_jq_pools.py"
