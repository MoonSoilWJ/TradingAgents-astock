#!/usr/bin/env bash
#
# 每日数据同步脚本（在 Mac mini 上运行，配合 crontab）
#
# 功能：
#   调用 export_to_web.py 从本地 journal/state 生成 strategies.json，
#   并直接 scp 上传到 ECS 站点目录。
#
# export_to_web.py 原生支持 --scp，因此无需本脚本再做 scp。
#
# 部署到 ECS 的目标：
#   etf.duwenjie.site  ->  /var/www/strategy-web/
#
# crontab 示例（工作日 16:00，TradingAgents 收盘后）：
#   0 16 * * 1-5 /bin/bash /Users/licheng/Documents/TradingAgents-astock/scripts/sync_to_web.sh >> /tmp/sync_to_web.log 2>&1
#
set -euo pipefail

# ========== 按需修改 ==========
# ECS 目标（user@host:远端目录）
SCP_TARGET="root@39.105.204.66:/var/www/strategy-web/"
# 本地 strategy-web 的 public 目录（生成的 strategies.json 落这里，并作为 --scp 的本地源）
# ⚠️ 必须放在 ~/ 根目录下，不能放 ~/Documents —— macOS TCC 会拦截 cron 写入 Documents，
#    导致 PermissionError: Operation not permitted。家目录根不受此限制。
WEB_PUBLIC="$HOME/strategy-web/public"
# 本脚本所在目录（用来定位 export_to_web.py）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# ==============================

OUT_JSON="$WEB_PUBLIC/strategies.json"

mkdir -p "$WEB_PUBLIC"

echo "==> 生成 strategies.json"
python3 "$SCRIPT_DIR/export_to_web.py" --out "$OUT_JSON" --scp "$SCP_TARGET"

echo "==> 完成。数据已更新到 https://etf.duwenjie.site"
