#!/bin/bash
# 在新电脑上恢复状态包: bash scripts/restore_state.sh state_backup/state_xxx.tar.gz
# 解包到 ~/.tradingagents/(与打包时的相对路径一致)。已有同名文件会被覆盖。
set -eu
SRC=${TRADINGAGENTS_HOME:-$HOME/.tradingagents}
PKG=${1:-}
if [ -z "$PKG" ] || [ ! -f "$PKG" ]; then
    echo "用法: bash scripts/restore_state.sh <state_backup/state_xxx.tar.gz>"; exit 1
fi
mkdir -p "$SRC"
if [ -e "$SRC/youzi/positions.json" ]; then
    echo "⚠ 目标已存在状态($SRC), 恢复将覆盖同名文件。5 秒内 Ctrl+C 取消..."
    sleep 5
fi
tar -xzf "$PKG" -C "$SRC"
echo "✅ 已恢复到 $SRC"
echo "   下一步: 见 MIGRATION.md 第 4~6 步(依赖/.env/数据重建/crontab)"
