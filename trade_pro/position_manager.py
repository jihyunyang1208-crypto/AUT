# position_manager.py
from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional, Tuple
import json
import threading

@dataclass
class Position:
    qty: int = 0                  # 체결 기준 보유 수량
    avg_price: float = 0.0        # 체결 기준 평단
    pending_buys: int = 0         # 미체결/대기 매수 수량
    pending_sells: int = 0        # 미체결/대기 매도 수량

class PositionManager:
    """
    - 종목별 보유/평단/대기수량 관리
    - 단순 파일 지속성(JSON)
    - 체결 이벤트에서 qty/avg 반영, 주문 제출 시 pending 증감
    - ✔ get_avg_buy(symbol) 정식 제공 (get_avg_price 별칭)
    """
    def __init__(self, store_path: str = "data/positions.json"):
        self._path = Path(store_path)
        self._lock = threading.Lock()
        self._pos: Dict[str, Position] = {}
        self._load()

    # ---------- Persistence ----------
    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for sym, d in data.items():
                    self._pos[sym] = Position(**d)
        except Exception:
            # 읽기 실패시 조용히 무시 (호환성 유지)
            pass

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: asdict(v) for k, v in self._pos.items()}
            self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            # 저장 실패시 조용히 무시 (호환성 유지)
            pass

    # ---------- Helpers ----------
    def _get(self, symbol: str) -> Position:
        if symbol not in self._pos:
            self._pos[symbol] = Position()
        return self._pos[symbol]

    # ---------- Queries ----------
    def get_qty(self, symbol: str) -> int:
        with self._lock:
            return self._get(symbol).qty

    def get_avg_price(self, symbol: str) -> Optional[float]:
        """
        보유 수량이 0이면 None 반환.
        """
        with self._lock:
            p = self._get(symbol)
            return p.avg_price if p.qty > 0 else None

    # ✔ 신규: 전략/모니터에서 쓰기 좋은 공식 API (평균 매수가)
    def get_avg_buy(self, symbol: str) -> Optional[float]:
        """
        평균 매수가(원화)를 반환. 보유 수량이 0이거나 데이터 없으면 None.
        get_avg_price와 동일 동작 (공식 별칭).
        """
        return self.get_avg_price(symbol)

    def get_pending(self, symbol: str) -> Tuple[int, int]:
        with self._lock:
            p = self._get(symbol)
            return p.pending_buys, p.pending_sells

    # ---------- Reservations (on submit/cancel) ----------
    def reserve_buy(self, symbol: str, qty: int) -> None:
        if qty <= 0:
            return
        with self._lock:
            self._get(symbol).pending_buys += qty
            self._save()

    def reserve_sell(self, symbol: str, qty: int) -> None:
        if qty <= 0:
            return
        with self._lock:
            self._get(symbol).pending_sells += qty
            self._save()

    def release_buy(self, symbol: str, qty: int) -> None:
        if qty <= 0:
            return
        with self._lock:
            p = self._get(symbol)
            p.pending_buys = max(0, p.pending_buys - qty)
            self._save()

    def release_sell(self, symbol: str, qty: int) -> None:
        if qty <= 0:
            return
        with self._lock:
            p = self._get(symbol)
            p.pending_sells = max(0, p.pending_sells - qty)
            self._save()

    # ---------- Fills (on execution) ----------
    def apply_fill_buy(self, symbol: str, qty: int, price: float) -> None:
        if qty <= 0:
            return
        with self._lock:
            p = self._get(symbol)
            new_qty = p.qty + qty
            if new_qty > 0:
                # 기존 보유가 있을 때만 가중평균, 없으면 체결가로 세팅
                p.avg_price = (
                    (p.avg_price * p.qty + float(price) * qty) / new_qty
                    if p.qty > 0 else float(price)
                )
            p.qty = new_qty
            p.pending_buys = max(0, p.pending_buys - qty)
            self._save()

    def apply_fill_sell(self, symbol: str, qty: int, price: float) -> None:
        if qty <= 0:
            return
        with self._lock:
            p = self._get(symbol)
            p.qty = max(0, p.qty - qty)
            p.pending_sells = max(0, p.pending_sells - qty)
            # 전량 청산 시 평단 초기화
            if p.qty == 0:
                p.avg_price = 0.0
            self._save()
