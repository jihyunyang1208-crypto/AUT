# bootstrap.py 
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json, os

KST = timezone(timedelta(hours=9))
base = Path("C:/trade/AutoTrader/logs/results")
base.mkdir(parents=True, exist_ok=True)

# 누적 스냅샷(비어있는 기본 구조)
cum = base / "trading_results.jsonl"
if not cum.exists():
    payload = {
        "type": "snapshot",
        "time": datetime.now(KST).isoformat(),
        "last_updated": datetime.now(KST).isoformat(),
        "symbols": {}
    }
    with cum.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

# 오늘 일별 스냅샷(선택)
today = datetime.now(KST).date().isoformat()
daily = base / f"trading_results_{today}.jsonl"
if not daily.exists():
    payload = {
        "type": "snapshot",
        "time": datetime.now(KST).isoformat(),
        "date": today,
        "strategies": {},
        "summary": {
            "realized_pnl_gross": 0.0, "fees": 0.0, "realized_pnl_net": 0.0,
            "trades": 0.0, "win_rate": 0.0, "morning_pnl": 0.0, "afternoon_pnl": 0.0
        }
    }
    with daily.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

print("bootstrap ok")
