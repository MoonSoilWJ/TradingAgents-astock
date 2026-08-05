"""诊断：DEFENSE_POOL 各资产在弱市段(2022-06-15~2023-12-31)的真实表现。
目的：验证"弱市抗跌资产等权持有"假设是否成立。
"""
import json, sys
from datetime import datetime

PATH = "/Users/leshu/.tradingagents/cache/t0_5min/full_daily_2015_2026.json"

pool = {
    "511090.XSHG": "30年国债",
    "511260.XSHG": "10年国债",
    "518880.XSHG": "黄金",
    "510880.XSHG": "红利",
    "511380.XSHG": "可转债",
    "511360.XSHG": "短融(现金替代)",
}

def load():
    with open(PATH) as f:
        return json.load(f)

data = load()
print("顶层结构:", list(data.keys())[:10] if isinstance(data, dict) else type(data))
# 探测结构
sample_key = list(data.keys())[0] if isinstance(data, dict) else None
print("示例标的:", sample_key, "->", type(data[sample_key]))
if isinstance(data[sample_key], dict):
    print("  子键:", list(data[sample_key].keys())[:10])
    # 看其中一支的结构
    sk = list(data[sample_key].keys())[0]
    print("  子子键:", sk, "->", type(data[sample_key][sk]),
          str(data[sample_key][sk])[:120])
elif isinstance(data[sample_key], list):
    print("  列表长度:", len(data[sample_key]))
    print("  首元素:", str(data[sample_key][0])[:200])

# 尝试推断字段名
def get_series(code):
    """返回 [(date, close), ...] 按日期排序。
    backfill_daily_1000.json 结构: etf_daily[code_no_suffix]['returns'] = [{date,close,...}]
    """
    d = data.get(code.replace(".XSHG", "").replace(".XSHE", ""))
    if d is None:
        d = data.get("etf_daily", {}).get(code.replace(".XSHG", "").replace(".XSHE", ""))
    if d is None:
        return []
    rows = d.get("returns", []) if isinstance(d, dict) else d
    items = []
    for row in rows:
        dt = row.get("date")
        c = row.get("close")
        if dt and c is not None:
            items.append((str(dt), float(c)))
    items.sort()
    return items

# 探测一支
probe = get_series("511260.XSHG")
print("\n511260 提取到", len(probe), "行, 前2:", probe[:2], "后2:", probe[-2:])

START = "2022-06-15"
END = "2023-12-31"

def seg_ret(series):
    """区间内累计收益 %"""
    pts = [(d, c) for d, c in series if START <= d <= END]
    if len(pts) < 2:
        return None, 0, len(pts)
    c0 = pts[0][1]
    c1 = pts[-1][1]
    return (c1 - c0) / c0 * 100.0, len(pts), 0

print(f"\n=== 弱市段 {START} ~ {END} 各资产累计收益 ===")
rets = {}
for code, name in pool.items():
    s = get_series(code)
    r, n, _ = seg_ret(s)
    rets[code] = r
    if r is None:
        print(f"  {code} {name}: 无数据 (提取{n}行)")
    else:
        print(f"  {code} {name}: {r:+.2f}%  ({n}交易日)")

# 等权组合日收益
print(f"\n=== 等权组合日收益(弱市段) ===")
# 构建对齐的日收益序列
aligned = {}
for code, name in pool.items():
    s = get_series(code)
    dates = [d for d, c in s if START <= d <= END]
    closes = {d: c for d, c in s if START <= d <= END}
    # 日收益率
    daily = []
    prev = None
    for d in dates:
        if prev is not None:
            daily.append((d, (closes[d] - closes[prev]) / closes[prev] * 100.0))
        prev = d
    aligned[code] = daily

# 所有日期并集
alldates = sorted({d for code in pool for d, _ in aligned.get(code, [])})
eq_ret = []
for d in alldates:
    rs = [r for code in pool if (dd := dict(aligned[code]).get(d)) is not None for r in [dd]]
    # 只取有数据的
    rs = []
    for code in pool:
        m = {dd: rr for dd, rr in aligned[code]}
        if d in m:
            rs.append(m[d])
    if rs:
        eq_ret.append(sum(rs) / len(rs))

cum = 1.0
for r in eq_ret:
    cum *= (1 + r / 100.0)
print(f"  等权(债+金+红+转+短融) 弱市段累计: {(cum-1)*100:+.2f}%  ({len(eq_ret)}交易日)")

# 纯债券
bond_codes = ["511260.XSHG", "511090.XSHG"]
cum = 1.0
cnt = 0
for d in alldates:
    rs = []
    for code in bond_codes:
        m = {dd: rr for dd, rr in aligned[code]}
        if d in m:
            rs.append(m[d])
    if rs:
        cum *= (1 + sum(rs) / len(rs) / 100.0)
        cnt += 1
print(f"  纯债券(511260+511090) 弱市段累计: {(cum-1)*100:+.2f}%  ({cnt}交易日)")

# 债券+黄金
bg_codes = ["511260.XSHG", "511090.XSHG", "518880.XSHG"]
cum = 1.0
cnt = 0
for d in alldates:
    rs = []
    for code in bg_codes:
        m = {dd: rr for dd, rr in aligned[code]}
        if d in m:
            rs.append(m[d])
    if rs:
        cum *= (1 + sum(rs) / len(rs) / 100.0)
        cnt += 1
print(f"  债券+黄金 弱市段累计: {(cum-1)*100:+.2f}%  ({cnt}交易日)")
