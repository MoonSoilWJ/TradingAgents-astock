#!/usr/bin/env python3
"""板块轮动监控系统（v6 公式：3日涨幅 × 量能因子）

板块池: 平安证券 79 个（行业 + 概念），数据 scripts/pingan_sector_etf.json

操作流程：
1. 信号时点（09:40/11:00/13:00/14:50）跑 v6 排名，推送 TOP1 ETF
2. 买入 TOP1 ETF：上涨 1.0% 或 下跌 2% 再回弹 0.3%
3. 次日卖出：追踪触 +3% 回落 0.5% 止盈，或 T+1 收盘卖（无固定止损）

v6 选股（与 backtest_rotation_8way 一致，见 rotation_v6.py）：
- 09:25 等开盘前：T-1 日完整 v6
- 11:00 及以后：盘中实时 partial v6

用法:
    python scripts/rotation_monitor.py              # 定时/推送钉钉
    python scripts/rotation_monitor.py --dry-run    # 手动查看（不推送）
    python scripts/rotation_monitor.py --alert-only  # 仅在有轮动信号时推送

定时运行（crontab）:
    bash scripts/install_crontab.sh
    # 09:25 / 11:00 / 13:00 / 14:50 各跑一次
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from sector_etf_map import (  # noqa: E402
    etf_to_sina_symbol,
    load_pingan_sectors,
)
from rotation_v6 import (  # noqa: E402
    SCORE_WINDOW,
    VOL_AVG_PERIOD,
    VOL_BASE,
    VOL_THRESHOLD,
    compute_v6_metrics,
)

try:
    from tradingagents.intraday.calendar import is_trading_day
except ImportError:
    def is_trading_day(day: date | None = None) -> bool:  # type: ignore[misc]
        day = day or date.today()
        return day.weekday() < 5

# ── 配置 ──────────────────────────────────────────────

PROXY = os.environ.get("ROTATION_PROXY", "http://127.0.0.1:7890")
SINA_INTERVAL = 0.3
TIMEOUT = 15
STATE_DIR = Path.home() / ".tradingagents" / "rotation"
STATE_FILE = STATE_DIR / "monitor_state.json"
SECTOR_POOL = "pingan"
LOOKBACK = 30  # K 线天数
TOP_N = 5

BUY_STRATEGY = "上涨1.0%追涨（不抄底）"
SELL_STRATEGY = "追踪触+3%落0.5%止盈 / T+1收盘卖（无固定止损）"
FILTER_RULE = "前一日涨幅>7%则跳过（防追高次日暴跌）"

# ── 数据采集 ──────────────────────────────────────────

def curl_get(url: str) -> str:
    """获取数据，先不走代理（新浪国内直连），失败再走代理。"""
    for use_proxy in [False, True]:
        cmd = ["curl", "-s", "--connect-timeout", str(TIMEOUT)]
        if use_proxy:
            cmd += ["-x", PROXY]
        cmd.append(url)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 10)
            for enc in ["gbk", "utf-8"]:
                try:
                    text = r.stdout.decode(enc)
                    if text and len(text) > 10:
                        return text
                except (UnicodeDecodeError, AttributeError):
                    continue
        except subprocess.TimeoutExpired:
            continue
    return ""


def fetch_sina_kline(symbol: str, datalen: int = 30) -> list[dict]:
    url = (
        f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
    )
    raw = curl_get(url)
    time.sleep(SINA_INTERVAL)
    if not raw or raw.strip() in ("null", "", "[]"):
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def fetch_tencent_quotes(codes: list[str]) -> dict[str, dict]:
    """批量拉腾讯实时行情（qt.gtimg.cn），用于合成当日未收盘 Bar。"""
    uniq: list[str] = []
    seen: set[str] = set()
    for code in codes:
        c = code.strip()
        if not c or c in seen:
            continue
        seen.add(c)
        uniq.append(c)
    if not uniq:
        return {}

    prefixed = [f"{'sh' if c.startswith(('5', '6')) else 'sz'}{c}" for c in uniq]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    raw = curl_get(url)
    if not raw:
        return {}

    result: dict[str, dict] = {}
    for line in raw.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 35:
            continue
        code = key[2:] if len(key) > 2 else key
        try:
            vol_lots = float(vals[6]) if vals[6] else 0.0
            result[code] = {
                "price": float(vals[3]) if vals[3] else 0.0,
                "last_close": float(vals[4]) if vals[4] else 0.0,
                "open": float(vals[5]) if vals[5] else 0.0,
                "change_pct": float(vals[32]) if vals[32] else 0.0,
                "high": float(vals[33]) if vals[33] else 0.0,
                "low": float(vals[34]) if vals[34] else 0.0,
                "volume": vol_lots * 100,
                "quote_time": vals[30],
            }
        except (ValueError, IndexError):
            continue
    return result


def _format_quote_time(raw: str) -> str:
    """20260713113138 → 2026-07-13 11:31"""
    if len(raw) < 12:
        return raw
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}"


def merge_intraday_bar(returns: list[dict], quote: dict | None) -> tuple[list[dict], bool]:
    """日K未含今日时，用实时行情追加当日 Bar（v6 动量需要当日涨幅+成交量）。"""
    if not returns or not quote:
        return returns, False

    today = date.today().isoformat()
    last_date = returns[-1]["date"][:10]
    if last_date >= today or not is_trading_day():
        return returns, False

    qt = quote.get("quote_time", "")
    if len(qt) < 12 or qt[:8] != today.replace("-", ""):
        return returns, False

    try:
        hh, mm = int(qt[8:10]), int(qt[10:12])
        if hh < 9 or (hh == 9 and mm < 30):
            return returns, False
    except ValueError:
        return returns, False

    price = quote.get("price", 0)
    last_close = quote.get("last_close", 0)
    if not price or not last_close:
        return returns, False

    ret = quote.get("change_pct")
    if ret is None:
        ret = (price - last_close) / last_close * 100

    merged = list(returns)
    merged.append({
        "date": today,
        "high": quote.get("high") or price,
        "low": quote.get("low") or price,
        "close": price,
        "return_pct": ret,
        "volume": quote.get("volume", 0),
        "intraday": True,
    })
    return merged, True


def compute_daily_data(klines: list[dict]) -> list[dict]:
    result = []
    for i, k in enumerate(klines):
        close = float(k.get("close", 0))
        high = float(k.get("high", close))
        low = float(k.get("low", close))
        try:
            volume = float(k.get("volume", 0))
        except (ValueError, TypeError):
            volume = 0.0
        if i == 0:
            ret = 0.0
        else:
            prev_close = float(klines[i - 1].get("close", 0))
            ret = ((close - prev_close) / prev_close * 100) if prev_close else 0.0
        result.append({
            "date": k.get("day", ""), "high": high, "low": low,
            "close": close, "return_pct": ret, "volume": volume,
        })
    return result


# ── 状态管理 ──────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 钉钉推送 ──────────────────────────────────────────

def _rotation_dingtalk_config() -> tuple[str, str]:
    webhook = (
        os.getenv("DINGTALK_ROTATION_WEBHOOK")
        or os.getenv("DINGTALK_WEBHOOK")
        or ""
    ).strip()
    keyword = (os.getenv("DINGTALK_ROTATION_KEYWORD") or "轮动").strip()
    return webhook, keyword


def send_dingtalk(title: str, text: str) -> bool:
    try:
        from tradingagents.notify.dingtalk import send_markdown

        webhook, keyword = _rotation_dingtalk_config()
        return send_markdown(title, text, webhook=webhook or None, keyword=keyword)
    except ImportError:
        print("(tradingagents.notify.dingtalk 不可用，跳过推送)")
        return False


# ── 主流程 ────────────────────────────────────────────

def run_monitor(dry_run: bool = False, alert_only: bool = False) -> int:
    print(f"=== 板块轮动监控 v6（ETF 优化版）===")
    print(
        f"公式: {SCORE_WINDOW}日涨幅 × 量能因子(阈{VOL_THRESHOLD} 均{VOL_AVG_PERIOD} 底{VOL_BASE})"
    )
    print("选股: 开盘前 T-1 v6 | 盘中 partial v6（与回测一致）")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    print(">>> 获取板块列表（平安证券）...")
    sectors = load_pingan_sectors()
    print(f"    {len(sectors)} 个板块（均有 ETF）")
    if not sectors:
        print("ERROR: 无法加载平安板块列表 (pingan_sector_etf.json)")
        return 1

    intraday_count = 0
    print(f">>> 获取 K 线数据 ({LOOKBACK} 日)...")
    pending: list[dict] = []
    quote_codes: list[str] = []
    for i, sec in enumerate(sectors):
        etf_raw = sec["etf_raw"]
        etf_code = sec["etf_code"]
        etf_name = sec["etf_name"]
        sina_sym = etf_to_sina_symbol(etf_raw)
        klines = fetch_sina_kline(sina_sym, datalen=LOOKBACK)
        if not klines or len(klines) <= SCORE_WINDOW:
            continue

        pending.append({
            "sec": sec,
            "klines": klines,
            "etf_code": etf_code,
            "etf_name": etf_name,
            "quote_code": etf_code,
        })
        quote_codes.append(etf_code)
        if (i + 1) % 10 == 0:
            print(f"    进度: {i+1}/{len(sectors)}")

    quotes = fetch_tencent_quotes(quote_codes)
    if quotes:
        print(f">>> 接入盘中实时 ({len(quotes)} 个标的，腾讯行情)")

    scored = []
    for item in pending:
        returns = compute_daily_data(item["klines"])
        quote = quotes.get(item["quote_code"])
        returns, merged = merge_intraday_bar(returns, quote)
        quote_ts = ""
        if merged and quote:
            quote_ts = _format_quote_time(quote.get("quote_time", ""))
            returns[-1]["quote_time"] = quote_ts
            intraday_count += 1

        metrics = compute_v6_metrics(returns)
        if not metrics:
            continue
        if quote_ts:
            metrics["quote_time"] = quote_ts
        sec = item["sec"]
        scored.append({
            "code": sec["code"],
            "name": sec["name"],
            "type_name": sec.get("type_name", ""),
            "symbol": item["etf_code"],
            "source": f"ETF {item['etf_code']} {item['etf_name']}",
            "etf_code": item["etf_code"],
            "etf_name": item["etf_name"],
            **metrics,
        })

    print(f"    完成: {len(scored)}/{len(sectors)} 个板块有数据"
          f"{f', 盘中:{intraday_count}' if intraday_count else ''}")

    if len(scored) < TOP_N * 2:
        print(f"ERROR: 有效板块数不足（{len(scored)} < {TOP_N * 2}）")
        return 1

    scored.sort(key=lambda x: x["score"], reverse=True)
    top5 = scored[:TOP_N]
    bottom5 = scored[-TOP_N:]
    current_top5_codes = {s["code"] for s in top5}

    prev_state = load_state()
    if prev_state.get("sector_pool") != SECTOR_POOL:
        if prev_state.get("top5_codes"):
            print(">>> 板块池已切换为平安，忽略旧 TOP5 状态（避免误报轮动）")
        prev_state = {}
    prev_top5_codes = set(prev_state.get("top5_codes", []))

    new_entries = [s for s in top5 if s["code"] not in prev_top5_codes]
    exits = [s["code"] for s in scored[TOP_N:] if s["code"] in prev_top5_codes]
    exit_names = [s["name"] for s in scored if s["code"] in exits]

    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    data_date = top5[0]["date"]
    has_intraday = any(s.get("intraday") for s in top5)
    quote_ts = next((s.get("quote_time") for s in top5 if s.get("quote_time")), "")
    last_bar_label = "盘中涨" if has_intraday else "今日"
    data_note = f"含盘中实时 {quote_ts or data_date}" if has_intraday else "日K最近交易日"

    print()
    print("=" * 60)
    print(f"  板块轮动监控报告 | {run_ts}")
    print(f"  数据截止: {data_date} ({data_note})")
    print("=" * 60)
    print(f"  买入: {BUY_STRATEGY}")
    print(f"  卖出: {SELL_STRATEGY}")
    print(f"  过滤: {FILTER_RULE}")
    print()

    if new_entries:
        print(f"\n  NEW  轮动信号: {len(new_entries)} 个板块新进入 TOP5")
        for s in new_entries:
            print(f"       + {s['name']:10s} 得分={s['score']:.2f} "
                  f"3日涨幅={s['ret_3d']:+.1f}% 量比={s['vol_ratio']:.1f}x")
    else:
        print("\n  无轮动信号（TOP5 无变化）" if prev_top5_codes else "\n  首次运行")

    if exit_names:
        print(f"\n  OUT  退出 TOP5: {', '.join(exit_names)}")

    print(f"\n  {'─' * 56}")
    print(f"  TOP {TOP_N} 强势板块:")
    print(f"  {'排名':>4} {'板块':12s} {'标的':>10s} {'V6得分':>8} {'3日涨幅':>8} {'量比':>6} {last_bar_label:>7}")
    for i, s in enumerate(top5):
        etf_str = s.get("etf_code") or s["symbol"]
        type_tag = f"[{s['type_name']}]" if s.get("type_name") else ""
        marker = " NEW" if s["code"] not in prev_top5_codes else ""
        print(f"  {i+1:4d} {s['name']+type_tag:12s} {etf_str:>10s} {s['score']:8.2f} {s['ret_3d']:+7.1f}% "
              f"{s['vol_ratio']:5.1f}x {s['last_bar_ret']:+6.2f}%{marker}")

    print(f"\n  BOTTOM {TOP_N} 弱势板块:")
    for i, s in enumerate(bottom5):
        etf_str = s.get("etf_code") or s["symbol"]
        type_tag = f"[{s['type_name']}]" if s.get("type_name") else ""
        print(f"  {i+1:4d} {s['name']+type_tag:12s} {etf_str:>10s} {s['score']:8.2f} {s['ret_3d']:+7.1f}% "
              f"{s['vol_ratio']:5.1f}x {s['last_bar_ret']:+6.2f}%")

    print()

    should_push = True
    if alert_only and not new_entries:
        should_push = False
        print(">>> alert-only 模式：无轮动信号，跳过推送")

    if should_push and not dry_run:
        webhook, _ = _rotation_dingtalk_config()
        if not webhook:
            print("\n>>> 钉钉未配置，跳过推送")
            print("    配置方法:")
            print("    1. 钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义")
            print("    2. 安全设置选「自定义关键词」填: 轮动")
            print("    3. 复制 Webhook 地址，填入 .env 的 DINGTALK_ROTATION_WEBHOOK=")
        else:
            title = f"板块轮动{'信号' if new_entries else '日报'}"
            lines = [
                f"### 板块轮动监控 | {run_ts}",
                f"数据截止: {data_date} ({data_note})",
                "",
                f"**买入**: {BUY_STRATEGY}",
                f"**卖出**: {SELL_STRATEGY}",
                "",
            ]
            if new_entries:
                lines.append(f"**轮动信号**: {len(new_entries)} 个板块新进 TOP5")
                for s in new_entries:
                    etf_str = f" | {s.get('etf_code') or s['symbol']}"
                    lines.append(
                        f"- **{s['name']}** 得分{s['score']:.1f} | "
                        f"3日{s['ret_3d']:+.1f}% | 量比{s['vol_ratio']:.1f}x{etf_str}"
                    )
                lines.append("")

            lines.append(f"**TOP {TOP_N} 强势**:")
            for i, s in enumerate(top5):
                etf_str = f" | {s.get('etf_code') or s['symbol']}"
                tag = " NEW" if s["code"] not in prev_top5_codes else ""
                lines.append(
                    f"{i+1}. {s['name']} — 得分{s['score']:.1f} "
                    f"(3日{s['ret_3d']:+.1f}%, 量比{s['vol_ratio']:.1f}x){etf_str}{tag}"
                )

            lines.append("")
            lines.append(f"**BOTTOM {TOP_N} 弱势**:")
            for i, s in enumerate(bottom5):
                etf_str = f" | {s.get('etf_code') or s['symbol']}"
                lines.append(
                    f"{i+1}. {s['name']} — 得分{s['score']:.1f} "
                    f"(3日{s['ret_3d']:+.1f}%){etf_str}"
                )

            text = "\n".join(lines)
            print("\n>>> 推送钉钉...")
            ok = send_dingtalk(title, text)
            print(f"    {'成功' if ok else '失败'}")
    elif dry_run:
        print("\n>>> --dry-run 模式，跳过推送")

    new_state = {
        "sector_pool": SECTOR_POOL,
        "date": top5[0]["date"],
        "timestamp": datetime.now().isoformat(),
        "top5_codes": list(current_top5_codes),
        "top5_names": [s["name"] for s in top5],
        "top5_scores": [{k: v for k, v in s.items() if k in
                         ("code", "name", "score", "ret_3d", "vol_ratio", "etf_code")}
                        for s in top5],
    }
    save_state(new_state)
    print(f"\n>>> 状态已保存: {STATE_FILE}")

    return 0 if new_entries else 2


def main():
    parser = argparse.ArgumentParser(description="板块轮动监控 v6（ETF 优化版）")
    parser.add_argument("--dry-run", action="store_true", help="仅打印不推送")
    parser.add_argument("--alert-only", action="store_true",
                        help="仅在有轮动信号时推送")
    parser.add_argument("--test-push", action="store_true",
                        help="发送测试消息验证钉钉配置")
    args = parser.parse_args()

    if args.test_push:
        webhook, _ = _rotation_dingtalk_config()
        if not webhook:
            print("DINGTALK_ROTATION_WEBHOOK 未配置")
            print("配置方法:")
            print("  1. 钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义")
            print("  2. 安全设置选「自定义关键词」填: 轮动")
            print("  3. 复制 Webhook 地址，填入 .env 的 DINGTALK_ROTATION_WEBHOOK=")
            sys.exit(1)
        print(">>> 发送测试消息...")
        ok = send_dingtalk("轮动监控测试", "### 轮动监控测试\n\n这是一条测试消息，确认钉钉机器人配置正确。")
        print(f"    {'成功' if ok else '失败'}")
        sys.exit(0 if ok else 1)

    code = run_monitor(dry_run=args.dry_run, alert_only=args.alert_only)
    sys.exit(code)


if __name__ == "__main__":
    main()
