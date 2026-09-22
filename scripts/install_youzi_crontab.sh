#!/bin/bash
# 游资系统(youzi) crontab 一键安装 —— 幂等: 只增删 youzi 相关系目, 不碰其他任务(t0 等)。
#
# 用法(在仓库根目录):
#   bash scripts/install_youzi_crontab.sh
# 迁移到其他电脑时指定路径/Python:
#   YOUZI_DIR=/home/xx/TradingAgents-astock YOUZI_PYTHON=/usr/bin/python3 \
#     bash scripts/install_youzi_crontab.sh
#
# 当前策略参数(2026-09-22 定档):
#   买侧: 扫描6%/推送6% prob≥75 总仓3 每日新买2(梯队) 影子模式(额度0降频5分钟)
#   卖侧: SELL_THR=60 T+1 14:30强制了结(封板豁免) 封死禁卖(代码级) 早盘10:00前禁卖
#   哨兵: 3秒轮询 封单衰减/炸板/摸板回落 → 事件触发AI(不推人)

set -u
PYTHON=${YOUZI_PYTHON:-/usr/local/bin/python3}
DIR=${YOUZI_DIR:-$(cd "$(dirname "$0")/.." && pwd)}

if [ ! -f "$DIR/scripts/youzi_live.py" ]; then
    echo "❌ 找不到 $DIR/scripts/youzi_live.py, 请用 YOUZI_DIR 指定仓库根目录"
    exit 1
fi
if [ ! -x "$PYTHON" ]; then
    echo "❌ Python 不存在: $PYTHON (可用 YOUZI_PYTHON 指定, 需 3.10+)"
    exit 1
fi

TMP=$(mktemp)
# 1) 保留所有非 youzi 的既有任务
crontab -l 2>/dev/null | grep -v -E "youzi_live|youzi_watchdog|youzi_sell_ai|youzi_fast_watch|youzi_calibrate|youzi_sell_calibrate" > "$TMP"

# 2) 追加 youzi 系统任务
cat >> "$TMP" <<EOF

# ── 游资系统(youzi) · 由 scripts/install_youzi_crontab.sh 管理 ──
# 买侧常驻: 09:35 启动(全天扫描, 总仓3/每日新买2/梯队)
35 9 * * 1-5 cd $DIR && $PYTHON scripts/youzi_live.py --ai --min-pct 6 --push-min-pct 6 --min-prob 75 --start-time 09:40 >> /tmp/youzi.log 2>&1
# 买侧看门狗: 进程死/日志静默>10分钟 → 重启(脚本内自带180s冷却)
*/5 9-14 * * 1-5 YOUZI_DIR=$DIR YOUZI_PYTHON=$PYTHON $DIR/scripts/youzi_watchdog.sh
# 卖侧AI: 早盘5分钟(10:00前代码禁卖只攒趋势基线) + 盘中1分钟批量评估
30-59/5 9 * * 1-5 cd $DIR && YOUZI_SELL_THR=60 $PYTHON scripts/youzi_sell_ai.py >> /tmp/youzi_sell_ai.log 2>&1
* 10,11,13,14 * * 1-5 cd $DIR && YOUZI_SELL_THR=60 $PYTHON scripts/youzi_sell_ai.py >> /tmp/youzi_sell_ai.log 2>&1
# 秒级盘口哨兵: 封单衰减/炸板/摸板回落 → 事件触发AI(纯代码零LLM, 自带锁+非交易时段自退)
*/2 9-14 * * 1-5 YOUZI_PYTHON=$PYTHON $PYTHON $DIR/scripts/youzi_fast_watch.py >> /tmp/youzi_fast_watch.log 2>&1
# 收盘校准: 买侧成绩单(封板率/prob分档) 15:20
20 15 * * 1-5 cd $DIR && $PYTHON scripts/youzi_calibrate.py >> /tmp/youzi_calibrate.log 2>&1
# 收盘校准: 卖侧成绩单(卖点 vs 持收) 15:25
25 15 * * 1-5 cd $DIR && $PYTHON scripts/youzi_sell_calibrate.py >> /tmp/youzi_sell_calibrate.log 2>&1
EOF

crontab "$TMP" && rm -f "$TMP"
echo "✅ 已安装 youzi crontab(仓库: $DIR, Python: $PYTHON)"
echo "   查看: crontab -l | grep youzi"
echo
echo "⚠ 可选但建议追加(手动): 每日收盘后刷新全市场日K(买侧 streak/情绪周期的底座)"
echo "   10 16 * * 1-5 cd $DIR && $PYTHON scripts/fetch_mainboard_daily.py >> /tmp/youzi_mb.log 2>&1"
