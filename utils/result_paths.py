from __future__ import annotations
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ----------------------------
# 🕒 KST 기준 경로 및 날짜 유틸
# ----------------------------
KST = timezone(timedelta(hours=9))
BASE_DIR = Path("data").resolve()
BASE_DIR.mkdir(parents=True, exist_ok=True)


def today_str() -> str:
    """오늘 날짜 문자열 (예: 2025-10-27)"""
    return datetime.now(KST).date().isoformat()


def path_today() -> Path:
    # ✅ 일별 JSONL
    return BASE_DIR / f"trading_results_{today_str()}.jsonl"

def path_cumulative() -> Path:
    # ✅ 누적 JSONL
    return BASE_DIR / "trading_results.jsonl"

def ensure_data_dir() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
