#!/bin/bash
# 安装/更新监控定时任务
#
# 板块轮动: rotation_monitor.py（09:40/11:00/13:00/14:50，仅 --install-rotation 时写入）
# T+0 ETF:  t0_monitor.py（震荡期501018跳过 + 09:40~14:55每3分卖出检查 + 14:50 买入）
#
# 默认仅追加/更新 T+0 任务，保留 crontab 中已有 rotation_monitor 及其他任务。
#
# 用法:
#   bash scripts/install_crontab.sh              # 只安装 T+0（推荐，不碰已有轮动）
#   bash scripts/install_crontab.sh --all        # 同时重置板块轮动 + T+0
#   bash scripts/install_crontab.sh --t0-only    # 同默认

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON3="$(which python3)"

ROTATION_CMD="cd ${PROJECT_DIR} && ${PYTHON3} scripts/rotation_monitor.py"
T0_CMD="cd ${PROJECT_DIR} && ${PYTHON3} scripts/t0_monitor.py"

ROTATION_TIMES=(
    "40 09"   # 09:40
    "0 11"    # 11:00
    "0 13"    # 13:00
    "50 14"   # 14:50
)

T0_SELL_CRON="*/3 9-14"   # 每3分钟检查；脚本内 09:40~14:55，仅有卖点才推送

MODE="t0-only"
if [[ "${1:-}" == "--all" ]]; then
    MODE="all"
elif [[ "${1:-}" == "--install-rotation" ]]; then
    MODE="rotation-only"
fi

echo "=== 安装监控定时任务 ==="
echo ""
echo "模式: ${MODE}"
echo ""

EXISTING="$(crontab -l 2>/dev/null || true)"

case "${MODE}" in
    t0-only)
        echo "保留已有 crontab（含 rotation_monitor），仅更新 t0_monitor.py 条目"
        FILTERED="$(echo "${EXISTING}" | grep -v "t0_monitor.py" || true)"
        {
            echo "${FILTERED}"
            echo "${T0_SELL_CRON} * * 1-5 ${T0_CMD} --sell-check"
            echo "50 14 * * 1-5 ${T0_CMD} --signal"
        } | sed '/^$/d' | crontab -
        ;;
    all)
        echo "重置板块轮动 + T+0（移除旧 rotation/t0 条目后重装）"
        FILTERED="$(echo "${EXISTING}" | grep -v "rotation_monitor.py" | grep -v "t0_monitor.py" || true)"
        {
            echo "${FILTERED}"
            for sched in "${ROTATION_TIMES[@]}"; do
                echo "${sched} * * 1-5 ${ROTATION_CMD}"
            done
            echo "${T0_SELL_CRON} * * 1-5 ${T0_CMD} --sell-check"
            echo "50 14 * * 1-5 ${T0_CMD} --signal"
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
echo ""
echo "仅卸载 T+0 任务:"
echo "  crontab -l | grep -v t0_monitor.py | crontab -"
