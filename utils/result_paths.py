# utils/result_paths.py
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from pathlib import Path


KST = timezone(timedelta(hours=9))
BASE_DIR = Path("data").resolve()
BASE_DIR.mkdir(parents=True, exist_ok=True)

def today_str() -> str:
    return datetime.now(KST).date().isoformat()

def path_today() -> Path:
    return BASE_DIR / f"trading_result_{today_str()}.json"

