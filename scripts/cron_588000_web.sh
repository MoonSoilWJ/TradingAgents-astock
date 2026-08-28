#!/usr/bin/env bash
#
# 588000 日线 N12 簇 投票策略 —— 14:55 定时任务 (收盘集合竞价前)
#
# 职责：
#   1) 14:55 重算 588000 信号 (此时日线为接近收盘的近似价, 用于收盘集合竞价下单)
#   2) 重新导出 strategies.json 并 scp 上传到站点 (含最新 588000 信号)
#
# crontab (工作日 14:55，A股收盘前 5 分钟)：
#   55 14 * * 1-5 /bin/bash /Users/licheng/Documents/TradingAgents-astock/scripts/cron_588000_web.sh >> /tmp/588000_web.log 2>&1
#
set -euo pipefail

SCP_TARGET="root@39.105.204.66:/var/www/strategy-web/"
WEB_PUBLIC="$HOME/strategy-web/public"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_JSON="$WEB_PUBLIC/strategies.json"

mkdir -p "$WEB_PUBLIC"

echo "[$(date '+%F %T')] ==> 14:55 重算 588000 N12 簇信号"
python3 "$SCRIPT_DIR/backtest_588000_n12.py" 2>&1 \
  || { echo "  ! 588000 回测失败, 终止本次上传(保留上次数据)"; exit 1; }

echo "[$(date '+%F %T')] ==> 导出并上传 strategies.json"
python3 "$SCRIPT_DIR/export_to_web.py" --out "$OUT_JSON" --scp "$SCP_TARGET"

echo "[$(date '+%F %T')] ==> 推送 14:55 信号到钉钉"
python3 "$SCRIPT_DIR/push_588000_signal.py" 2>&1 \
  || echo "  ! 钉钉推送失败, 不影响网站更新"

echo "[$(date '+%F %T')] ==> 完成。588000 信号已更新到 https://etf.duwenjie.site 并推送钉钉"
