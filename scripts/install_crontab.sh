#!/bin/bash
# 安装板块轮动监控定时任务
# 每个交易日在 09:25 / 11:00 / 13:00 / 14:50 自动运行并推送钉钉日报
#
# 用法: bash scripts/install_crontab.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MONITOR_CMD="cd ${PROJECT_DIR} && $(which python3) scripts/rotation_monitor.py"
CRON_TIMES=(
    "25 09"   # 09:25
    "0 11"    # 11:00
    "0 13"    # 13:00
    "50 14"   # 14:50
)

echo "=== 安装板块轮动监控定时任务 ==="
echo ""
echo "定时计划: 每个交易日（周一至周五）"
echo "  - 09:25"
echo "  - 11:00"
echo "  - 13:00"
echo "  - 14:50"
echo "运行模式: 每次推送 TOP5 日报（无 --alert-only）"
echo "命令: ${MONITOR_CMD}"
echo ""

# 检查是否已存在
if crontab -l 2>/dev/null | grep -q "rotation_monitor"; then
    echo "⚠️ 已存在轮动监控定时任务，将更新"
    crontab -l 2>/dev/null | grep -v "rotation_monitor" | crontab -
fi

{
    crontab -l 2>/dev/null || true
    for sched in "${CRON_TIMES[@]}"; do
        echo "${sched} * * 1-5 ${MONITOR_CMD}"
    done
} | crontab -

echo "✅ 定时任务已安装"
echo ""
echo "当前 crontab:"
crontab -l | grep "rotation_monitor" || true
echo ""
echo "查看日志: tail -f ~/.tradingagents/rotation/monitor.log (如有)"
echo "手动运行: cd ${PROJECT_DIR} && python3 scripts/rotation_monitor.py --dry-run"
echo "卸载: crontab -l | grep -v rotation_monitor | crontab -"
