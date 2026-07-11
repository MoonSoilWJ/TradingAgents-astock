#!/bin/bash
# 安装板块轮动监控定时任务
# 每个交易日 15:30（收盘后）自动运行，仅在有轮动信号时推送钉钉
#
# 用法: bash scripts/install_crontab.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MONITOR_CMD="cd ${PROJECT_DIR} && $(which python3) scripts/rotation_monitor.py --alert-only"
CRON_LINE="50 14 * * 1-5 ${MONITOR_CMD}"

echo "=== 安装板块轮动监控定时任务 ==="
echo ""
echo "定时计划: 每个交易日（周一至周五）14:50 盘中运行"
echo "运行模式: --alert-only（仅轮动信号时推送钉钉）"
echo "命令: ${MONITOR_CMD}"
echo ""

# 检查是否已存在
if crontab -l 2>/dev/null | grep -q "rotation_monitor"; then
    echo "⚠️ 已存在轮动监控定时任务，将更新"
    crontab -l 2>/dev/null | grep -v "rotation_monitor" | crontab -
fi

(crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -

echo "✅ 定时任务已安装"
echo ""
echo "当前 crontab:"
crontab -l | grep -A1 -B1 "rotation_monitor" || true
echo ""
echo "查看日志: tail -f ~/.tradingagents/rotation/monitor.log (如有)"
echo "手动运行: cd ${PROJECT_DIR} && python3 scripts/rotation_monitor.py --dry-run"
echo "卸载: crontab -l | grep -v rotation_monitor | crontab -"
