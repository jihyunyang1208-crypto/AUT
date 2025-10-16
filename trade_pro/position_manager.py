"""
PositionManager: 종목별 보유 수량, 평균 매수가, 미체결 수량만 관리하며, 포지션 변화 시 Qt 신호를 발행합니다.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional, Tuple

try:
    # Import QtCore modules for signals and QObject.  If PySide6 is not installed,
    # importing these will raise ImportError.  Consumers of this class should
    # ensure PySide6 is available when using signal functionality.
    from PySide6.QtCore import QObject, Signal
except ImportError:
    # Provide fallbacks to allow this module to be imported in non-GUI environments.
    class QObject:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class Signal:
        def __init__(self, *types) -> None:
            pass
        def emit(self, *args, **kwargs) -> None:
            pass

@dataclass
class Position:
    """Holds per‐symbol position data.

    Attributes:
        qty (int): The executed (filled) quantity.
        avg_price (float): The average buy price based on executed buys.
        pending_buys (int): Quantity reserved for submitted but unfilled buy orders.
        pending_sells (int): Quantity reserved for submitted but unfilled sell orders.
    """

    qty: int = 0
    avg_price: float = 0.0
    pending_buys: int = 0
    pending_sells: int = 0


class PositionManager(QObject):
    """Manages holdings and emits signals on changes.

    The PositionManager class maintains a dictionary of positions keyed by
    symbol.  It loads and saves this dictionary from/to a JSON file for
    persistence.  Whenever a position is updated—whether by a reservation,
    release, or fill—the manager emits a `position_changed` signal with the
    symbol, current quantity, and average price.

    Parameters:
        store_path (str): Path to the JSON file used for persisting positions.

    Signals:
        position_changed (str, int, float): Emitted whenever a symbol's
            quantity, average price, or pending quantities change.  The
            arguments are (symbol, qty, avg_price).
    """

    # Define the signal type.  It will be a no-op if PySide6 is not installed.
    position_changed: Signal = Signal(str, int, float)

    def __init__(self, store_path: str = "data/positions.json") -> None:
        # Initialize QObject first to ensure signal infrastructure is ready.
        super().__init__()
        self._path = Path(store_path)
        self._lock = threading.Lock()
        self._pos: Dict[str, Position] = {}
        self._load()

    # ---------- Persistence ----------
    def _load(self) -> None:
        """Load position data from the JSON file.

        If the file does not exist or cannot be parsed, the internal position
        dictionary remains empty.
        """
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for sym, d in data.items():
                    self._pos[sym] = Position(**d)
        except Exception:
            # Silently ignore errors to maintain backward compatibility.
            pass

    def _save(self) -> None:
        """Persist position data to the JSON file.

        Errors during saving are silently ignored to avoid disrupting the
        application flow.  Consumers may override this method for custom
        persistence logic.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: asdict(v) for k, v in self._pos.items()}
            self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ---------- Helpers ----------
    def _get(self, symbol: str) -> Position:
        """Return the Position object for the given symbol, creating it if necessary."""
        if symbol not in self._pos:
            self._pos[symbol] = Position()
        return self._pos[symbol]

    # ---------- Queries ----------
    def get_qty(self, symbol: str) -> int:
        """Return the executed quantity of the given symbol."""
        with self._lock:
            return self._get(symbol).qty

    def get_avg_price(self, symbol: str) -> Optional[float]:
        """Return the average buy price if quantity > 0, else None."""
        with self._lock:
            p = self._get(symbol)
            return p.avg_price if p.qty > 0 else None

    def get_avg_buy(self, symbol: str) -> Optional[float]:
        """Official alias for get_avg_price()."""
        return self.get_avg_price(symbol)

    def get_pending(self, symbol: str) -> Tuple[int, int]:
        """Return a tuple (pending_buys, pending_sells)."""
        with self._lock:
            p = self._get(symbol)
            return p.pending_buys, p.pending_sells

    # ---------- Internal helper for signaling ----------
    def _emit_change(self, symbol: str) -> None:
        """Emit the position_changed signal for a symbol."""
        # Retrieve current qty and avg_price under lock to ensure consistency
        with self._lock:
            p = self._get(symbol)
            qty = p.qty
            avg = p.avg_price if p.qty > 0 else 0.0
        try:
            self.position_changed.emit(symbol, qty, avg)
        except Exception:
            # If signals are disabled (e.g., no Qt event loop), ignore errors
            pass

    # ---------- Reservations (on submit/cancel) ----------
    def reserve_buy(self, symbol: str, qty: int) -> None:
        """Reserve a quantity for a pending buy order and emit a change."""
        if qty <= 0:
            return
        with self._lock:
            self._get(symbol).pending_buys += qty
            self._save()
        # Emit change outside the lock
        self._emit_change(symbol)

    def reserve_sell(self, symbol: str, qty: int) -> None:
        """Reserve a quantity for a pending sell order and emit a change."""
        if qty <= 0:
            return
        with self._lock:
            self._get(symbol).pending_sells += qty
            self._save()
        self._emit_change(symbol)

    def release_buy(self, symbol: str, qty: int) -> None:
        """Reduce pending buys by qty and emit a change."""
        if qty <= 0:
            return
        with self._lock:
            p = self._get(symbol)
            p.pending_buys = max(0, p.pending_buys - qty)
            self._save()
        self._emit_change(symbol)

    def release_sell(self, symbol: str, qty: int) -> None:
        """Reduce pending sells by qty and emit a change."""
        if qty <= 0:
            return
        with self._lock:
            p = self._get(symbol)
            p.pending_sells = max(0, p.pending_sells - qty)
            self._save()
        self._emit_change(symbol)

    # ---------- Fills (on execution) ----------
    def apply_fill_buy(self, symbol: str, qty: int, price: float) -> None:
        """Apply an executed buy and update quantity/avg_price, emitting a change."""
        if qty <= 0:
            return
        with self._lock:
            p = self._get(symbol)
            new_qty = p.qty + qty
            if new_qty > 0:
                # Weighted average price if existing position, else set to price
                p.avg_price = (
                    (p.avg_price * p.qty + float(price) * qty) / new_qty
                    if p.qty > 0 else float(price)
                )
            p.qty = new_qty
            # Remove from pending buys
            p.pending_buys = max(0, p.pending_buys - qty)
            self._save()
        self._emit_change(symbol)

    def apply_fill_sell(self, symbol: str, qty: int, price: float) -> None:
        """Apply an executed sell and update quantity/avg_price, emitting a change."""
        if qty <= 0:
            return
        with self._lock:
            p = self._get(symbol)
            p.qty = max(0, p.qty - qty)
            p.pending_sells = max(0, p.pending_sells - qty)
            if p.qty == 0:
                # Reset average price on full exit
                p.avg_price = 0.0
            self._save()
        self._emit_change(symbol)
