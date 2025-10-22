# risk_management/result_reader.py
from __future__ import annotations
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

def _code6(s: str) -> str:
    d = "".join(c for c in str(s) if c.isdigit())
    return d[-6:].zfill(6)

class TradingResultReader:
    """
    간결하고 안전한 읽기 전용 리더.
    - 파일 mtime이 바뀐 경우에만 리로드
    - 리로드 실패(파일 교체 중/파싱 오류 등) 시 기존 캐시를 유지 → 충돌/스파이크 회피
    - 'symbols[code].avg_price'를 표준 소스 오브 트루스로 사용
    """
    def __init__(self, json_path: str = "data/trading_result.json") -> None:
        self._path = Path(json_path)
        self._lock = threading.RLock()
        self._mtime: Optional[float] = None
        # 최소 형태의 캐시 (쓰기 측 표준 스키마 기준)
        self._cache: Dict[str, Any] = {
            "symbols": {},   # "005930": { "avg_price": 72100.0, ... }
            "summary": {},
            "strategies": {}
        }

    # 내부: 필요 시에만 리로드 (락 내부에서 호출)
    def _maybe_reload_locked(self) -> None:
        try:
            if not self._path.exists():
                return
            mtime = self._path.stat().st_mtime
            if self._mtime is not None and mtime == self._mtime:
                return  # 변경 없음

            # 전체 읽기 → 파싱 → 정상 시에만 캐시 교체
            text = self._path.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                return

            # 최소 키 보정
            symbols = data.get("symbols") or {}
            summary = data.get("summary") or {}
            strategies = data.get("strategies") or {}

            if not isinstance(symbols, dict):
                symbols = {}
            if not isinstance(summary, dict):
                summary = {}
            if not isinstance(strategies, dict):
                strategies = {}

            self._cache = {
                "symbols": symbols,
                "summary": summary,
                "strategies": strategies,
            }
            self._mtime = mtime
            logger.debug("[result_reader] reloaded: %s", self._path)
        except Exception:
            # 읽기/파싱 중 오류면 조용히 스킵 (이전 캐시 유지)
            logger.debug("[result_reader] reload skipped due to transient error", exc_info=True)

    # 공개 API ----------------------------------------------------------

    def get_avg_buy(self, symbol: str) -> Optional[float]:
        """symbols[code].avg_price 반환. 없거나 0 이하/형식 불량이면 None."""
        code = _code6(symbol)
        with self._lock:
            self._maybe_reload_locked()
            node = (self._cache.get("symbols") or {}).get(code)
            if not isinstance(node, dict):
                return None
            try:
                v = float(node.get("avg_price", 0.0))
                return v if v > 0 else None
            except Exception:
                return None

    def get_qty_and_avg_buy(self, symbol: str) -> Optional[tuple[int, float]]:
        """
        symbols[code]의 (qty, avg_price) 튜플 반환.
        - 값이 없거나 avg_price가 0 이하이면 None 반환.
        - qty는 0일 수도 있음.
        """
        code = _code6(symbol)
        with self._lock:
            self._maybe_reload_locked()
            node = (self._cache.get("symbols") or {}).get(code)
            if not isinstance(node, dict):
                return None
            try:
                qty = int(node.get("qty", 0))
                avg = float(node.get("avg_price", 0.0))
                if avg <= 0:
                    return None
                return qty, avg
            except Exception:
                return None

