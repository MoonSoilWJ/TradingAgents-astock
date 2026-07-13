#!/usr/bin/env python3
"""板块轮动监控系统（v6 公式：3日涨幅 × 量能因子）

操作流程：
1. 信号时点（09:25/11:00/13:00/14:50）跑 v6 排名，推送 TOP1 ETF
2. 买入 TOP1 ETF：上涨 0.3% 或 下跌 2% 再回弹 0.3%
3. 次日卖出：追踪触 +3% 回落 0.5% 止盈，止损 -0.5%

用法:
    python scripts/rotation_monitor.py              # 每日报告（推送钉钉）
    python scripts/rotation_monitor.py --dry-run    # 仅打印不推送
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
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

# ── 配置 ──────────────────────────────────────────────

PROXY = os.environ.get("ROTATION_PROXY", "http://127.0.0.1:7890")
SINA_INTERVAL = 0.3
TIMEOUT = 15
STATE_DIR = Path.home() / ".tradingagents" / "rotation"
STATE_FILE = STATE_DIR / "monitor_state.json"
LOOKBACK = 30  # K 线天数
TOP_N = 5

BUY_STRATEGY = "上涨0.3%或下跌2%再回弹0.3%"
SELL_STRATEGY = "追踪触3%落0.5%止-0.5%"

# ── 板块 → 代表性 ETF 映射 ────────────────────────────
# 代码为场内 ETF 交易代码，可直接买卖
# 无直接对应 ETF 的板块标注 "—"
SECTOR_ETF_MAP: dict[str, tuple[str, str]] = {
    "电子信息": ("159997", "电子ETF"),
    "电子器件": ("512480", "半导体ETF"),
    "生物制药": ("512010", "医药ETF"),
    "医疗器械": ("159883", "医疗器械ETF"),
    "钢铁行业": ("515210", "钢铁ETF"),
    "煤炭行业": ("515220", "煤炭ETF"),
    "有色金属": ("512400", "有色金属ETF"),
    "电力行业": ("159611", "电力ETF"),
    "发电设备": ("159637", "电力设备ETF"),
    "电器行业": ("159996", "家电ETF"),
    "家电行业": ("159996", "家电ETF"),
    "酿酒行业": ("512690", "酒ETF"),
    "食品行业": ("515170", "食品ETF"),
    "化工行业": ("516020", "化工ETF"),
    "化纤行业": ("516020", "化工ETF"),
    "农药化肥": ("516020", "化工ETF"),
    "建筑建材": ("159745", "建材ETF"),
    "水泥行业": ("159745", "建材ETF"),
    "玻璃行业": ("159745", "建材ETF"),
    "陶瓷行业": ("159745", "建材ETF"),
    "机械行业": ("515970", "华夏机械"),
    "仪器仪表": ("515970", "华夏机械"),
    "汽车制造": ("516110", "汽车ETF"),
    "摩托车":   ("516110", "汽车ETF"),
    "金融行业": ("510230", "金融ETF"),
    "房地产":   ("512200", "房地产ETF"),
    "交通运输": ("159662", "交运ETF"),
    "公路桥梁": ("159662", "交运ETF"),
    "酒店旅游": ("159766", "旅游ETF"),
    "农林牧渔": ("159825", "农业ETF"),
    "环保行业": ("512580", "环保ETF"),
    "传媒娱乐": ("512980", "传媒ETF"),
    "船舶制造": ("512660", "军工ETF"),
    "飞机制造": ("512660", "军工ETF"),
    "石油行业": ("", "—"),
    "商业百货": ("159928", "消费ETF"),
    "服装鞋类": ("159928", "消费ETF"),
    # 以下板块无直接对应 ETF
    "供水供气": ("", "—"),
    "其它行业": ("", "—"),
    "塑料制品": ("", "—"),
    "家具行业": ("", "—"),
    "开发区":   ("", "—"),
    "纺织机械": ("", "—"),
    "纺织行业": ("", "—"),
    "综合行业": ("", "—"),
    "造纸行业": ("", "—"),
    "物资外贸": ("", "—"),
    "次新股":   ("", "—"),
    "印刷包装": ("", "—"),
}


def etf_to_sina_symbol(etf_code: str) -> str:
    """ETF 代码转新浪格式: 5开头→sh, 1开头→sz。"""
    if etf_code.startswith("5"):
        return f"sh{etf_code}"
    elif etf_code.startswith("1"):
        return f"sz{etf_code}"
    return f"sh{etf_code}"

# ── 数据采集（复用验证脚本逻辑） ──────────────────────

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


def fetch_sina_sectors() -> list[dict]:
    raw = curl_get("http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php")
    if not raw or "=" not in raw:
        return []
    json_str = raw.split("=", 1)[1].strip().rstrip(";")
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return []
    sectors = []
    for _, val in data.items():
        parts = val.split(",")
        if len(parts) < 13:
            continue
        try:
            sectors.append({
                "code": parts[0],
                "name": parts[1],
                "avg_chg_pct": float(parts[4]) if parts[4] else 0,
                "leader_code": parts[8],
                "leader_name": parts[12],
            })
        except (ValueError, IndexError):
            continue
    return sectors


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


# ── v6 得分计算（ETF 优化版） ─────────────────────────

# 公式: 3日累计涨幅 × 量能因子
# 量能因子 = 0.3 + 0.7 × min(量比 / 1.5, 1.0)
# 量比 = 今日成交量 / 过去5日平均成交量
# 验证结果(ETF): 收盘涨率52.6% / 盘中高77.2% / 持续性69.2% (57样本)

SCORE_WINDOW = 3
VOL_THRESHOLD = 1.5
VOL_AVG_PERIOD = 5
VOL_BASE = 0.3


def compute_v6_score(returns: list[dict]) -> dict:
    """计算 v6 综合得分（ETF 优化版，3日窗口 + 量价，无 CMF）。"""
    idx = len(returns) - 1
    if idx < SCORE_WINDOW:
        return {}

    # 3 日累计涨幅
    ret_w = sum(r["return_pct"] for r in returns[idx - SCORE_WINDOW + 1:idx + 1])

    # 量能因子
    vol_today = returns[idx].get("volume", 0)
    vol_prev = [returns[j].get("volume", 0)
                for j in range(max(0, idx - VOL_AVG_PERIOD), idx)]
    avg_vol = sum(vol_prev) / len(vol_prev) if vol_prev and sum(vol_prev) > 0 else vol_today
    vol_ratio = vol_today / avg_vol if avg_vol > 0 else 1.0
    vol_factor = VOL_BASE + (1 - VOL_BASE) * min(vol_ratio / VOL_THRESHOLD, 1.0)

    score = ret_w * vol_factor
    today_ret = returns[idx]["return_pct"]
    last_date = returns[idx]["date"]

    return {
        "score": score,
        "ret_3d": ret_w,
        "vol_ratio": vol_ratio,
        "vol_factor": vol_factor,
        "today_ret": today_ret,
        "date": last_date,
    }


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
    print(f"公式: {SCORE_WINDOW}日涨幅 × 量能因子(阈{VOL_THRESHOLD} 均{VOL_AVG_PERIOD} 底{VOL_BASE})")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 1. 获取板块列表
    print(">>> 获取板块列表...")
    sectors = fetch_sina_sectors()
    print(f"    {len(sectors)} 个行业板块")
    if not sectors:
        print("ERROR: 无法获取板块列表")
        return 1

    # 2. 获取 K 线 + 计算 v6 得分
    #    优先用 ETF，无 ETF 的板块回退到领涨股
    etf_count = 0
    leader_count = 0
    print(f">>> 获取 K 线数据 ({LOOKBACK} 日，优先 ETF)...")
    scored = []
    for i, sec in enumerate(sectors):
        etf_code, etf_name = SECTOR_ETF_MAP.get(sec["name"], ("", ""))
        symbol = None
        source = ""

        if etf_code:
            sina_sym = etf_to_sina_symbol(etf_code)
            klines = fetch_sina_kline(sina_sym, datalen=LOOKBACK)
            if klines and len(klines) > SCORE_WINDOW:
                symbol = etf_code
                source = f"ETF {etf_code} {etf_name}"
                etf_count += 1

        if not symbol:
            leader = sec["leader_code"]
            if leader and len(leader) >= 4:
                klines = fetch_sina_kline(leader, datalen=LOOKBACK)
                if klines and len(klines) > SCORE_WINDOW:
                    symbol = leader
                    source = f"领涨股 {leader} {sec['leader_name']}"
                    leader_count += 1

        if not symbol or not klines:
            continue

        returns = compute_daily_data(klines)
        metrics = compute_v6_score(returns)
        if not metrics:
            continue
        scored.append({
            "code": sec["code"],
            "name": sec["name"],
            "symbol": symbol,
            "source": source,
            "etf_code": etf_code,
            "etf_name": etf_name,
            **metrics,
        })
        if (i + 1) % 10 == 0:
            print(f"    进度: {i+1}/{len(sectors)}")

    print(f"    完成: {len(scored)} 个板块有数据 (ETF:{etf_count} 领涨股:{leader_count})")

    # 只对有 ETF 的板块排名（避免领涨股波动大导致不公平比较）
    scored_etf = [s for s in scored if s.get("etf_code")]
    scored_leader = [s for s in scored if not s.get("etf_code")]
    print(f"    ETF 板块: {len(scored_etf)} 个（参与排名）")
    print(f"    领涨股板块: {len(scored_leader)} 个（仅展示，不参与排名）")

    if len(scored_etf) < TOP_N * 2:
        print(f"ERROR: ETF 板块数不足（{len(scored_etf)} < {TOP_N * 2}）")
        return 1

    # 3. 排名（仅 ETF 板块）
    scored_etf.sort(key=lambda x: x["score"], reverse=True)
    top5 = scored_etf[:TOP_N]
    bottom5 = scored_etf[-TOP_N:]
    current_top5_codes = {s["code"] for s in top5}

    # 4. 加载上次状态
    prev_state = load_state()
    prev_top5_codes = set(prev_state.get("top5_codes", []))
    prev_date = prev_state.get("date", "")

    new_entries = [s for s in top5 if s["code"] not in prev_top5_codes]
    exits = [s["code"] for s in scored_etf[TOP_N:] if s["code"] in prev_top5_codes]
    exit_names = [s["name"] for s in scored_etf if s["code"] in exits]

    # 5. 控制台输出
    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    data_date = top5[0]["date"]
    print()
    print("=" * 60)
    print(f"  板块轮动监控报告 | {run_ts}")
    print(f"  数据截止: {data_date} (日K最近交易日)")
    print("=" * 60)
    print(f"  买入: {BUY_STRATEGY}")
    print(f"  卖出: {SELL_STRATEGY}")
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
    print(f"  {'排名':>4} {'板块':10s} {'标的':>10s} {'V6得分':>8} {'3日涨幅':>8} {'量比':>6} {'今日':>7}")
    for i, s in enumerate(top5):
        etf_str = s.get("etf_code") or s["symbol"]
        marker = " NEW" if s["code"] not in prev_top5_codes else ""
        print(f"  {i+1:4d} {s['name']:10s} {etf_str:>10s} {s['score']:8.2f} {s['ret_3d']:+7.1f}% "
              f"{s['vol_ratio']:5.1f}x {s['today_ret']:+6.2f}%{marker}")

    print(f"\n  BOTTOM {TOP_N} 弱势板块:")
    for i, s in enumerate(bottom5):
        etf_str = s.get("etf_code") or s["symbol"]
        print(f"  {i+1:4d} {s['name']:10s} {etf_str:>10s} {s['score']:8.2f} {s['ret_3d']:+7.1f}% "
              f"{s['vol_ratio']:5.1f}x {s['today_ret']:+6.2f}%")

    # 无 ETF 板块（仅展示，不参与排名）
    if scored_leader:
        scored_leader.sort(key=lambda x: x["score"], reverse=True)
        print(f"\n  无 ETF 板块（领涨股代理，仅供参考）:")
        for s in scored_leader[:5]:
            print(f"  {s['name']:10s} {s['symbol']:>10s} 得分={s['score']:.2f} "
                  f"3日={s['ret_3d']:+.1f}% 量比={s['vol_ratio']:.1f}x")

    print()

    # 6. 钉钉推送
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
                f"数据截止: {data_date} (日K最近交易日)",
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

    # 7. 保存状态
    new_state = {
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

    return 0 if new_entries else 2  # 0=有信号, 2=无信号


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
