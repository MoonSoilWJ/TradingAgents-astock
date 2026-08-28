# 计算 588000 日线 N12 簇 当前状态, 并推送到钉钉 (收盘集合竞价前信号).
# 复用 trix_n12_now 的簇计算; 通过 tradingagents.notify.dingtalk 发送 (自动 load_dotenv).
#
# 用法:
#   python3 scripts/push_588000_signal.py
import os
import sys
from pathlib import Path

# 让 import 能找到同目录的 trix_n12_now 与项目根的 tradingagents 包
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402
from trix_n12_now import COMB_N12, trix_series, fetch_day  # noqa: E402
from tradingagents.notify.dingtalk import send_markdown  # noqa: E402

COMB_N12 = [(10, 9), (10, 12), (12, 9), (12, 12), (14, 9), (14, 12)]


def build_message() -> tuple[str, str]:
    f = fetch_day()
    close = f["close"].values.astype(float)
    last_date = pd.to_datetime(f["date"].values[-1])
    last_close = float(close[-1])

    bull = 0
    rows = []
    for (n, m) in COMB_N12:
        tr, sig = trix_series(close, n, m)
        is_bull = float(tr[-1]) > float(sig[-1])
        bull += int(is_bull)
        rows.append(
            f"- ({n},{m}) TRIX `{float(tr[-1]):.3f}` / 信号 `{float(sig[-1]):.3f}` → "
            f"{'✅ 金叉(多)' if is_bull else '⛔ 死叉(空)'}"
        )
    ratio = bull / len(COMB_N12)
    pos = "持仓" if ratio > 0.5 else "空仓"
    suggestion = (
        "收盘集合竞价可**买入**(若尚未持仓)"
        if ratio > 0.5
        else "维持**空仓**, 不入场"
    )

    title = f"588000 日线N12簇信号 {last_date.date()} @14:55"
    text = "\n".join([
        f"### 科创50ETF(588000) 日线 N12 簇",
        "",
        f"**日期**: {last_date.date()} (盘中近似收盘 `{last_close:.4f}`)",
        f"**看多占比**: {bull}/{len(COMB_N12)} = `{ratio:.3f}` → **{pos}**",
        "",
        "**各组合状态**:",
        *rows,
        "",
        f"**建议**: {suggestion}",
        "",
        "> 日线收盘生成信号, T+1 执行; 此处为收盘集合竞价前的盘中快照。",
    ])
    return title, text


def main() -> int:
    webhook = (os.getenv("DINGTALK_ROTATION_WEBHOOK") or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    keyword = (os.getenv("DINGTALK_ROTATION_KEYWORD") or os.getenv("DINGTALK_KEYWORD") or "轮动").strip()
    if not webhook:
        print("! 钉钉未配置 (DINGTALK_ROTATION_WEBHOOK / DINGTALK_WEBHOOK), 跳过推送")
        return 0

    title, text = build_message()
    print(f"推送钉钉: {title}")
    print(text)
    ok = send_markdown(title, text, webhook=webhook, keyword=keyword)
    print(f"  -> {'成功' if ok else '失败'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
