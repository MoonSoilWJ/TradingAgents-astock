"""平安证券板块 + ETF 数据源（轮动监控板块池）。

数据来源: scripts/pingan_sector_etf.json
"""

from __future__ import annotations

import json
from pathlib import Path

_DATA_FILE = Path(__file__).resolve().parent / "pingan_sector_etf.json"


def etf_to_sina_symbol(etf_code: str) -> str:
    """ETF 代码转新浪格式，支持 SH512200 / SZ159697 或纯数字。"""
    code = etf_code.strip().upper()
    if code.startswith("SH"):
        return f"sh{code[2:]}"
    if code.startswith("SZ"):
        return f"sz{code[2:]}"
    if code.startswith("5") or code.startswith("6"):
        return f"sh{code}"
    if code.startswith("1") or code.startswith("0"):
        return f"sz{code}"
    return f"sh{code}"


def normalize_etf_code(etf_code: str) -> str:
    """展示用纯数字代码。"""
    code = etf_code.strip().upper()
    if code.startswith(("SH", "SZ")):
        return code[2:]
    return code


def _is_listed_etf(fund_code: str) -> bool:
    return fund_code.strip().upper().startswith(("SH", "SZ"))


def _load_pingan_rows() -> list[dict]:
    if not _DATA_FILE.exists():
        return []
    data = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    return data.get("rows", [])


def load_pingan_sectors() -> list[dict]:
    """加载平安板块列表，每项均带场内 ETF。"""
    sectors: list[dict] = []
    for row in _load_pingan_rows():
        name = (row.get("sectorName") or "").strip()
        etf_raw = (row.get("fund1Code") or "").strip()
        etf_name = (row.get("fund1Abbrname") or "").strip()
        if not name or not etf_raw or not _is_listed_etf(etf_raw):
            continue
        sectors.append({
            "code": (row.get("sectorCode") or row.get("code") or name),
            "name": name,
            "etf_raw": etf_raw,
            "etf_code": normalize_etf_code(etf_raw),
            "etf_name": etf_name,
            "type_name": (row.get("typeName") or "").strip(),
            "sector_type": (row.get("sectorType") or "").strip(),
        })
    return sectors


# 兼容旧引用：平安板块名 → ETF
def build_sector_etf_map() -> dict[str, tuple[str, str]]:
    return {s["name"]: (s["etf_raw"], s["etf_name"]) for s in load_pingan_sectors()}


SECTOR_ETF_MAP: dict[str, tuple[str, str]] = build_sector_etf_map()
