#!/bin/bash
# 安装/更新监控定时任务
#
# 板块轮动: rotation_monitor.py（09:30/15:00，仅 --install-rotation 时写入）
# T+0 ETF:  14:45信号/14:50买 + 09:40~11:05每50秒5分K TRIX(5,3)卖出检查
# 分钟K缓存: cache_min_data.py（15:10 小池子 + 15:35 全市场）
# Walk-Forward: 每月首个工作日 9:00 复核参数（仅「可考虑切换」时钉钉推送）
#
# 默认仅追加/更新 T+0 任务，保留 crontab 中已有 rotation_monitor 及其他任务。
#
# 用法:
#   bash scripts/install_crontab.sh              # 只安装 T+0（推荐，不碰已有轮动）
#   bash scripts/install_crontab.sh --all        # 同时重置板块轮动 + T+0
#   bash scripts/install_crontab.sh --t0-only    # 同默认
#   bash scripts/install_crontab.sh --install-walk-forward  # 追加每月 walk-forward 任务
#   bash scripts/install_crontab.sh --install-b-idle-shadow # 追加 B+idle SHADOW（新策略影子, 不改实盘）

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

MODE="t0-only"
if [[ "${1:-}" == "--all" ]]; then
    MODE="all"
elif [[ "${1:-}" == "--install-rotation" ]]; then
    MODE="rotation-only"
elif [[ "${1:-}" == "--install-walk-forward" ]]; then
    MODE="walk-forward-only"
elif [[ "${1:-}" == "--install-b-idle-shadow" ]]; then
    MODE="b-idle-shadow-only"
fi

BIDLE_CMD="cd ${PROJECT_DIR} && ${PYTHON3} scripts/t0_b_idle_shadow.py"
BIDLE_LOG=">> ${HOME}/.tradingagents/rotation/b_idle_shadow.log 2>&1"
BIDLE_SIGNAL_CRON="45 14"   # 14:45 核心 B 信号
BIDLE_IDLE_SELL_CRON="49 14" # 14:49 idle 次日固定卖 (先平昨日持仓)
BIDLE_IDLE_BUY_CRON="50 14" # 14:50 idle 动量腿买入 (核心未命中时)
BIDLE_SELL_WATCH="40 9"     # 09:40 启动核心 B 的 hybrid 卖出监控 (09:40~11:05 每50秒循环)

echo "=== 安装监控定时任务 ==="
echo ""
echo "模式: ${MODE}"
echo ""

EXISTING="$(crontab -l 2>/dev/null || true)"

case "${MODE}" in
    t0-only)
        echo "保留已有 crontab（含 rotation_monitor），仅更新 t0_monitor.py 条目"
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
        echo "重置板块轮动 + T+0（移除旧 rotation/t0 条目后重装）"
        FILTERED="$(echo "${EXISTING}" | grep -v "rotation_monitor.py" | grep -v "t0_monitor.py" | grep -v "t0_sell_watch.py" | grep -v "cache_min_data.py" || true)"
        {
            echo "${FILTERED}"
            for sched in "${ROTATION_TIMES[@]}"; do
                echo "${sched} * * 1-5 ${ROTATION_CMD}"
            done
            echo "${T0_SELL_WATCH} * * 1-5 ${T0_WATCH_CMD}"
            echo "${T0_SIGNAL_CRON} * * 1-5 ${T0_CMD} --signal"
            echo "${T0_CACHE_CRON} * * 1-5 ${CACHE_CMD}"
            echo "${T0_CACHE_ALLMARKET_CRON} * * 1-5 ${CACHE_ALLMARKET_CMD}"
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
