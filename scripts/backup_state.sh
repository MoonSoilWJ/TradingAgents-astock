#!/bin/bash
# 打包运行态状态 → 仓库内 state_backup/(已 gitignore), 供整机迁移到新电脑。
#
# 用法:
#   bash scripts/backup_state.sh              # 精简包: 实盘必需(约1.3GB)
#   bash scripts/backup_state.sh --full       # 追加回测研究缓存(约+3.4GB)
#
# 恢复(新电脑上): bash scripts/restore_state.sh state_backup/state_xxx.tar.gz
#
# 设计: 各脚本运行时读写 ~/.tradingagents/<...>(路径硬编码), 故不"移动"原文件
# (移动会弄断正在运行的系统), 而是打包→新机恢复到同一路径。

set -eu
SRC=${TRADINGAGENTS_HOME:-$HOME/.tradingagents}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/state_backup"
FULL=${1:-}
mkdir -p "$DEST"

if [ ! -d "$SRC" ]; then
    echo "❌ 找不到 $SRC"; exit 1
fi

LIST=$(mktemp)
add() { if [ -e "$SRC/$1" ]; then printf '%s\n' "$1" >> "$LIST"; fi; }

# ── 游资系统(状态/校准数据, 试运行期不可断) ──
for f in positions.json youzi_sold.jsonl signals.json \
         ai_judgements.jsonl ai_pushed.jsonl ai_obs_judgements.jsonl \
         blocked_log.jsonl ai_calibration_report.txt \
         youzi_sell_calibration_report.txt sell_backtest_results.jsonl \
         youzi_sell_ai_history.json fin_cache.json topics_cache.json \
         ai_ctx_cache.json pool.json pool_daily.pkl \
         mb_daily.pkl min1_cache.pkl; do
    add "youzi/$f"
done
add "youzi/logs/sell_backtest_results.jsonl"
for f in "$SRC"/youzi/limits_*.json; do
    if [ -e "$f" ]; then
        printf 'youzi/%s\n' "$(basename "$f")" >> "$LIST"
    fi
done

# ── T0 ETF 系统(实盘缓存 + 轮动状态) ──
for f in aligned_live_4y.json tdx_5min_2y.json \
         backfill_daily_1000.json backfill_daily_2015.json \
         full_daily_2015_2026.json; do
    add "cache/t0_5min/$f"
done
for f in t0_monitor_state.json t0_trade_journal.jsonl t0_trail_shadow.jsonl \
         b_idle_shadow_state.json b_idle_journal.jsonl \
         r3_shadow_state.json r3_journal.jsonl monitor_state.json; do
    add "rotation/$f"
done

# ── 框架级(tradingagents 记忆/守护状态) ──
add "memory"
add "intraday"
add "logs"

# ── --full: 追加回测研究缓存(不做研究可不迁, 新机可重跑脚本重建) ──
if [ "$FULL" = "--full" ]; then
    for f in cache/t0_5min/planC_1min_4y.json \
             cache/t0_5min/tdx_5min_pre2024.json \
             cache/t0_5min/tdx_5min_auto.json \
             cache/t0_5min/pool_20260721_days100_allmarket.json \
             cache/t0_5min/_161129_5min_extract.json \
             youzi/min30.pkl; do
        add "$f"
    done
fi

STAMP=$(date +%Y%m%d_%H%M)
OUT="$DEST/state_${STAMP}.tar.gz"
echo "打包 $(wc -l < "$LIST" | tr -d ' ') 项 → $OUT"
tar -czf "$OUT" -C "$SRC" -T "$LIST"
rm -f "$LIST"
SIZE=$(du -h "$OUT" | cut -f1)
echo "✅ 完成: $OUT ($SIZE)"
echo "   新机恢复: bash scripts/restore_state.sh $OUT"
echo "   未包含(可重建): youzi/min30.pkl → python3 scripts/fetch_min30.py (6-12分钟)"
if [ "$FULL" != "--full" ]; then
    echo "   回测研究缓存(planC/pre2024/auto等 约3.4G)未含, 需要: bash $0 --full"
fi
