#!/bin/bash
# youzi_live 看门狗 —— 进程掉线 **或** 日志静默超时 都拉起。
#
# 背景(2026-09-22 事故): youzi_live 卡在 "[AI] ... 调用模型" 12+ 分钟无输出,
# 进程还活着(pgrep 命中) → 旧 watchdog `pgrep || 拉起` 认为正常 → 全天空转。
# 根因: llm.invoke 无超时(已在 youzi_ai.invoke_llm 加 90s 硬超时); 本脚本是第二道防线:
# 只要日志超过 STALE 秒没更新, 就判定"假活"并重启。
#
# 用法: ./youzi_watchdog.sh [STALE_SEC=600]
# crontab: */5 9-14 * * 1-5 /Users/licheng/文档/TradingAgents-astock/scripts/youzi_watchdog.sh

STALE=${1:-600}
DIR=/Users/licheng/文档/TradingAgents-astock
LOG=/tmp/youzi.log
WLOG=/tmp/youzi_watchdog.log
PAT="youzi_live.py --ai"
# 不传 --trigger-times: 其 argparse 默认就是 ""(全天), 传空串在 $CMD 展开时会被当字面量 ""
CMD="/usr/local/bin/python3 scripts/youzi_live.py --ai --min-pct 6 --push-min-pct 6 --min-prob 75 --start-time 09:40"

NOW=$(date +%s)
MT=$(stat -f %m "$LOG" 2>/dev/null || echo 0)
AGE=$(( NOW - MT ))

ACTION=""
if ! pgrep -f "$PAT" >/dev/null 2>&1; then
    ACTION="进程不存在"
elif [ "$AGE" -gt "$STALE" ]; then
    ACTION="日志静默 ${AGE}s(>${STALE}s), 判定假活"
fi

# 重启冷却: youzi_live 启动后要先 build_pool(约 60s)才有第一条业务日志,
# 这期间 mtime 仍是旧的 → watchdog 会误判"假活"并反复 kill/重启, 永远起不来。
# 故: 距上次重启 <COOLDOWN 秒 且进程还在 → 一律不动。
COOLDOWN=180
MARK=/tmp/youzi_watchdog.last
LAST=$(cat "$MARK" 2>/dev/null || echo 0)
SINCE=$(( NOW - LAST ))
if [ -n "$ACTION" ] && [ "$SINCE" -lt "$COOLDOWN" ]; then
    echo "$(date '+%F %T') [watchdog] 距上次重启仅${SINCE}s(<${COOLDOWN}s), 跳过" >> "$WLOG"
    exit 0
fi

if [ -z "$ACTION" ]; then
    exit 0
fi

echo "$(date '+%F %T') [watchdog] $ACTION → 重启 youzi_live" >> "$WLOG"
pkill -f "$PAT" 2>/dev/null
sleep 2
cd "$DIR" || exit 1
nohup $CMD >> "$LOG" 2>&1 &
date +%s > "$MARK"
# 立刻写一行到业务日志: 避免 build_pool 期间 mtime 不更新被自己(或下一次 cron)误判假活
echo "$(date '+%F %T') [watchdog] 已拉起 pid=$!, 池构建中(约60s无业务日志)" >> "$LOG"
echo "$(date '+%F %T') [watchdog] 已拉起 pid=$!" >> "$WLOG"
