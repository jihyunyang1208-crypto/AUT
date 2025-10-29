# -*- coding: utf-8 -*-
"""
CSV -> JSONL 스모크 테스트
- 가장 최신 orders_YYYY-MM-DD.csv를 찾아 TradingResultStore로 임포트
- daily/cumulative JSONL 파일 생성/append 확인
- trade/snapshot 이벤트 최소 개수 검증
"""

from __future__ import annotations
import sys
import json
import glob
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any

# === 경로 기본값 ===
ROOT = Path.cwd()
TRADES_DIR = ROOT / "logs" / "trades"
RESULTS_DIR = ROOT / "logs" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# === AutoTrader 내부 모듈 임포트 ===
try:
    from risk_management.trading_results import TradingResultStore, TradeRow
except Exception as e:
    print("[ERR] risk_management.trading_results import 실패:", e)
    sys.exit(1)

# 선택: 공식 임포터가 있다면 사용
def _import_with_official(store: TradingResultStore, csv_path: Path) -> int:
    try:
        from risk_management.bootstrap import import_orders_csv_to_jsonl
        return import_orders_csv_to_jsonl(store, csv_path)
    except Exception:
        # fallback으로 로컬 파서 사용
        return _import_fallback(store, csv_path)

# fallback 파서 (헤더 없는 형식 가정)
def _import_fallback(store: TradingResultStore, csv_path: Path, encoding: str = "utf-8") -> int:
    n = 0
    with csv_path.open("r", encoding=encoding) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            cols = [c.strip() for c in line.split(",")]
            if len(cols) < 7:
                continue
            try:
                time_iso = cols[0]
                side_raw = cols[2].lower()
                symbol   = cols[3]
                price    = float(cols[5])
                qty      = int(cols[6])
                strategy = cols[7] if len(cols) > 7 and cols[7] else "default"

                side = "buy" if side_raw.startswith("buy") else ("sell" if side_raw.startswith("sell") else "")
                if not side or not symbol or qty <= 0 or price <= 0:
                    continue

                tr = TradeRow(
                    time=time_iso or datetime.utcnow().isoformat() + "Z",
                    side=side,
                    symbol=symbol,
                    qty=qty,
                    price=price,
                    fee=0.0,
                    status="filled",
                    strategy=strategy,
                    meta={"source": "orders_csv_fallback"}
                )
                store.apply_trade(tr)
                n += 1
            except Exception:
                # 한 줄 오류는 무시하고 계속
                continue
    return n

def _pick_latest_orders_csv() -> Path | None:
    # 우선순위: orders_YYYY-MM-DD.csv 패턴 최신 → 그 외 orders_*.csv 최신
    candidates = sorted(TRADES_DIR.glob("orders_????-??-??.csv"), reverse=True)
    if not candidates:
        candidates = sorted(TRADES_DIR.glob("orders_*.csv"), reverse=True)
    return candidates[0] if candidates else None

def _tail(path: Path, n: int = 5) -> List[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[-n:]

def _count_types(path: Path) -> Dict[str, int]:
    counts = {"trade": 0, "snapshot": 0, "daily_close": 0, "alert": 0, "_total": 0}
    if not path.exists():
        return counts
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
            et = str(ev.get("type") or "").lower()
            if et in counts:
                counts[et] += 1
            counts["_total"] += 1
        except Exception:
            continue
    return counts

def main():
    print("=== JSONL Smoke Test ===")
    csv_path = _pick_latest_orders_csv()
    if not csv_path:
        print(f"[ERR] CSV 없음: {TRADES_DIR}/orders_*.csv")
        sys.exit(2)

    print(f"[INFO] 사용 CSV: {csv_path.name}")

    # TradingResultStore는 파일 경로 문자열에서 **폴더만** 사용 (호환 설계)
    jsonl_anchor = RESULTS_DIR / "trading_results.jsonl"
    store = TradingResultStore(json_path=str(jsonl_anchor))

    before_daily = _count_types(store.daily_jsonl)
    before_cumu  = _count_types(store.cumulative_jsonl)

    print(f"[INFO] 실행 전: daily={store.daily_jsonl.name} {before_daily} | cumulative={store.cumulative_jsonl.name} {before_cumu}")

    # 임포트 실행
    inserted = _import_with_official(store, csv_path)
    print(f"[INFO] 임포트 완료: {inserted} rows")

    after_daily = _count_types(store.daily_jsonl)
    after_cumu  = _count_types(store.cumulative_jsonl)

    print(f"[INFO] 실행 후: daily={store.daily_jsonl.name} {after_daily}")
    print(f"[INFO] 실행 후: cumulative={store.cumulative_jsonl.name} {after_cumu}")

    # 간단 검증 (trade ≥1, snapshot ≥1 증가)
    ok = True
    if after_daily["trade"] <= before_daily["trade"]:
        print("[FAIL] daily.jsonl trade 이벤트 증가 안함")
        ok = False
    if after_daily["snapshot"] <= before_daily["snapshot"]:
        print("[FAIL] daily.jsonl snapshot 이벤트 증가 안함")
        ok = False
    if after_cumu["snapshot"] <= before_cumu["snapshot"]:
        print("[FAIL] cumulative.jsonl snapshot 이벤트 증가 안함")
        ok = False

    # tail 출력
    print("\n--- tail(daily) ---")
    for l in _tail(store.daily_jsonl, 5):
        print(l)
    print("\n--- tail(cumulative) ---")
    for l in _tail(store.cumulative_jsonl, 5):
        print(l)

    print("\n=== RESULT:", "PASS ✅" if ok else "FAIL ❌", "===")
    sys.exit(0 if ok else 3)

if __name__ == "__main__":
    main()
