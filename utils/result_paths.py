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
    """오늘 일자별 결과 파일 경로"""
    return BASE_DIR / f"trading_result_{today_str()}.json"


def path_cumulative() -> Path:
    """누적 관리용 trading_result.json 경로"""
    return BASE_DIR / "trading_result.json"


def ensure_data_dir() -> None:
    """data 폴더 보장 (존재하지 않으면 생성)"""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
