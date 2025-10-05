# utils/results_store.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import json
import threading
import pandas as pd

# ============================================================================
# 기본(호환) 설정 - 리팩토링 전 코드와 동일 인터페이스 보장
# ============================================================================
RESULTS_DIR = Path("logs/trades")  # 기존 results_path_for()가 참조
_KST = "Asia/Seoul"

_cache_lock = threading.RLock()
_cache_by_date: Dict[str, List[dict]] = {}

# ----------------------------------------------------------------------------
# 공용 유틸
# ----------------------------------------------------------------------------
def _code6(x: Any) -> str:
    """심볼을 6자리 숫자 문자열로 정규화."""
    s = "" if x is None else str(x)
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""

def _safe_to_ts(val: Any, *, assume_utc: bool = False) -> Optional[pd.Timestamp]:
    """ISO/epoch/등을 tz-aware KST로 변환(실패시 None)."""
    if val is None or val == "":
        return None
    try:
        ts = pd.to_datetime(val, utc=assume_utc)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC" if assume_utc else _KST)
        return ts.tz_convert(_KST)
    except Exception:
        return None

def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                # 손상 라인은 스킵
                continue
    return rows

# ============================================================================
# (A) 리팩토링 전 API 100% 호환 영역
# ============================================================================

def today_str(tz: str = _KST) -> str:
    return pd.Timestamp.now(tz=tz).strftime("%Y-%m-%d")

def results_path_for(date_str: str) -> Path:
    """기존 API 그대로: logs/trades/orders_YYYY-MM-DD.jsonl"""
    return RESULTS_DIR / f"orders_{date_str}.jsonl"

def clear_cache(date_str: Optional[str] = None) -> None:
    """캐시 무효화. date_str 미지정 시 전체 캐시 삭제."""
    with _cache_lock:
        if date_str is None:
            _cache_by_date.clear()
        else:
            _cache_by_date.pop(date_str, None)

def _normalize_row_for_legacy(obj: dict) -> dict:
    """
    JSONL 한 줄(dict)을 기존 스키마로 보정.
    - 과거 포맷: ts, side, symbol, price, reason
    - 신규 필드가 있으면 보존: source, condition_name, return_msg
    - 주문로그(orders_*.jsonl)에서 오는 경우에도 최대한 맞춰서 반환
    """
    # 주문로그일 수도 있으므로 code/stk_cd 등 다양한 키에서 symbol 보정
    symbol = obj.get("symbol")
    if not symbol:
        symbol = obj.get("stk_cd") or obj.get("code") or obj.get("stkcd")

    return {
        "ts": obj.get("ts"),
        "side": obj.get("side"),
        "symbol": symbol,
        "price": obj.get("price"),
        "reason": obj.get("reason"),
        # 확장 필드
        "source": obj.get("source", "bar"),
        "condition_name": obj.get("condition_name", ""),
        "return_msg": obj.get("return_msg", None),
    }

def load_results_for_date(
    date_str: Optional[str] = None,
    *,
    tz: str = _KST,
) -> List[dict]:
    """
    당일(기본) 또는 지정 날짜의 결과 파일(JSONL)을 파싱해 리스트(dict)로 반환.
    - 기존 호출부와 100% 호환
    - 파일 없음/비었음 → 빈 리스트
    """
    date_str = date_str or today_str(tz)
    with _cache_lock:
        if date_str in _cache_by_date:
            return _cache_by_date[date_str]

    path = results_path_for(date_str)
    rows: List[dict] = []

    if path.exists():
        for obj in _read_jsonl(path):
            rows.append(_normalize_row_for_legacy(obj))

    with _cache_lock:
        _cache_by_date[date_str] = rows
    return rows

def filter_by_symbol(rows: List[dict], symbol: str) -> List[dict]:
    """
    리팩토링 전 함수와 동일 시그니처.
    - 보완: rows의 각 항목에서 symbol/stk_cd/code/stkcd를 모두 검사하여 6자리 비교
    """
    target = _code6(symbol)
    out: List[dict] = []
    for r in rows:
        # 우선순위: symbol, stk_cd, code, stkcd
        cand = r.get("symbol") or r.get("stk_cd") or r.get("code") or r.get("stkcd")
        if _code6(cand) == target:
            out.append(r)
    return out

def to_dataframe(rows: List[dict]) -> pd.DataFrame:
    """
    rows(list[dict]) → pandas.DataFrame
    - 기존 컬럼 유지: ["ts","side","symbol","price","reason","source","condition_name","return_msg"]
    - ts는 datetime으로 변환(가능하면)
    """
    cols = ["ts", "side", "symbol", "price", "reason", "source", "condition_name", "return_msg"]
    if not rows:
        return pd.DataFrame(columns=cols)

    # 누락 필드 보정
    normed = []
    for r in rows:
        normed.append(_normalize_row_for_legacy(r))
    df = pd.DataFrame(normed)[cols]

    # ts → datetime (로컬/UTC 불문, 변환 실패 시 NaT)
    try:
        df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    except Exception:
        pass
    return df

def append_result_line(
    payload: Dict[str, Any],
    date_str: Optional[str] = None,
    *,
    tz: str = _KST,
) -> Path:
    """
    JSONL 한 줄 append (UI/디버그용)
    - 캐시는 자동 무효화
    """
    date_str = date_str or today_str(tz)
    path = results_path_for(date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    clear_cache(date_str)
    return path

# ============================================================================
# (B) 확장 API - 선택 사용 (UI에서 당일자 파일을 쉽게 DataFrame으로 파싱)
# ============================================================================

def load_orders_jsonl(path: str | Path) -> pd.DataFrame:
    """
    주문 로그: logs/trades/orders_YYYY-MM-DD.jsonl → DataFrame
    - code 컬럼(6자리) 추가
    - ts_kst(KST tz-aware) 추가
    - 수치 컬럼은 안전 변환
    """
    p = Path(path)
    rows = _read_jsonl(p)
    if not rows:
        return pd.DataFrame()

    for r in rows:
        r["code"] = _code6(r.get("stk_cd") or r.get("symbol") or r.get("code") or r.get("stkcd"))
        r["ts_kst"] = _safe_to_ts(r.get("ts"), assume_utc=True)

    df = pd.DataFrame(rows)
    for col in ["status_code", "cur_price", "limit_price", "qty", "tick_used",
                "duration_ms", "unit_amount", "notional", "slice_idx", "slice_total"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "ts_kst" in df.columns:
        df = df.sort_values("ts_kst", kind="mergesort").reset_index(drop=True)
    return df

def load_system_results_jsonl(path: str | Path) -> pd.DataFrame:
    """
    시스템 신호: data/results/system_results_YYYY-MM-DD.jsonl → DataFrame
    - ts_kst(KST tz-aware) 추가
    - code(6자리) 보조 컬럼 추가
    """
    p = Path(path)
    rows = _read_jsonl(p)
    if not rows:
        return pd.DataFrame()

    for r in rows:
        r["ts_kst"] = _safe_to_ts(r.get("ts"))  # 기록이 로컬일 수 있어 assume_utc=False
        r["code"] = _code6(r.get("symbol") or r.get("code"))
        if "price" in r:
            try:
                r["price"] = float(r["price"])
            except Exception:
                r["price"] = float("nan")

    df = pd.DataFrame(rows)
    if "ts_kst" in df.columns:
        df = df.sort_values("ts_kst", kind="mergesort").reset_index(drop=True)
    return df

# ----------------------------------------------------------------------------
# 캐시형 스토어(선택 사용) - 다이얼로그 닫혀도 상위에서 인스턴스 잡고 있으면 데이터 유지
# ----------------------------------------------------------------------------
@dataclass
class _CacheItem:
    path: Path
    mtime_ns: int
    df: pd.DataFrame

class ResultsStore:
    def __init__(self):
        self._orders: Dict[Path, _CacheItem] = {}
        self._signals: Dict[Path, _CacheItem] = {}

    def get_orders(self, path: str | Path) -> pd.DataFrame:
        p = Path(path)
        mtime = p.stat().st_mtime_ns if p.exists() else -1
        item = self._orders.get(p)
        if item and item.mtime_ns == mtime:
            return item.df
        df = load_orders_jsonl(p)
        self._orders[p] = _CacheItem(p, mtime, df)
        return df

    def get_orders_by_code(self, path: str | Path, code6: str) -> pd.DataFrame:
        df = self.get_orders(path)
        if "code" in df.columns:
            c6 = _code6(code6)
            return df[df["code"] == c6].copy()
        return df.iloc[0:0]

    def get_signals(self, path: str | Path) -> pd.DataFrame:
        p = Path(path)
        mtime = p.stat().st_mtime_ns if p.exists() else -1
        item = self._signals.get(p)
        if item and item.mtime_ns == mtime:
            return item.df
        df = load_system_results_jsonl(p)
        self._signals[p] = _CacheItem(p, mtime, df)
        return df

    def get_signals_by_code(self, path: str | Path, code6: str) -> pd.DataFrame:
        df = self.get_signals(path)
        c6 = _code6(code6)
        if "symbol" in df.columns:
            tmp = df.copy()
            tmp["_c6_"] = tmp["symbol"].map(_code6)
            out = tmp[tmp["_c6_"] == c6].drop(columns=["_c6_"])
            return out
        if "code" in df.columns:
            return df[df["code"] == c6].copy()
        return df.iloc[0:0]
