"""
SharedWalletPnL: PositionManager에서 포지션 변경 신호와 가격 업데이트를 받아 전체 포트폴리오의 미실현 손익을 계산합니다. 계산된 스냅샷을 pnl_snapshot_ready 신호로 내보내 UI가 바로 반영할 수 있습니다.

"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Any

try:
    from PySide6.QtCore import QObject, Signal
except ImportError:
    # Provide fallbacks if PySide6 is unavailable; signals will no-op.
    class QObject:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class Signal:
        def __init__(self, *types) -> None:
            pass
        def emit(self, *args, **kwargs) -> None:
            pass

from trade_pro.position_manager import PositionManager


class SharedWalletPnL(QObject):
    """Aggregates positions and computes unrealized PnL.

    ``SharedWalletPnL`` listens to position updates from a ``PositionManager``
    instance and to price updates via :meth:`on_price`.  It maintains an
    internal dictionary of the last seen prices for each symbol.  Whenever a
    position changes or a new price is received, it recalculates per‐symbol
    PnL and emits a new snapshot via :data:`pnl_snapshot_ready`.

    Parameters:
        position_mgr: A :class:`PositionManager` instance that emits
            ``position_changed`` signals.
    """

    pnl_snapshot_ready: Signal = Signal(dict)
    """Signal emitted whenever the PnL snapshot is updated.

    The argument is a dictionary with keys:

    ``positions``: mapping of symbol to a sub-dictionary containing qty and avg_price.
    ``prices``: mapping of symbol to the last received price (or ``None``).
    ``pnls``: mapping of symbol to its current unrealized PnL.
    ``total_pnl``: sum of all symbol PnL values.
    """

    def __init__(self, position_mgr: PositionManager) -> None:
        super().__init__()
        self.position_mgr = position_mgr
        self.prices: Dict[str, float] = {}
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.pnls: Dict[str, float] = {}
        # Connect to the position manager's change signal.
        try:
            self.position_mgr.position_changed.connect(self.on_position_changed)
        except Exception:
            # If signals are unavailable, ignore.
            pass

    # ---------------------------------------------------------------------
    # Signal handlers
    # ---------------------------------------------------------------------
    def on_position_changed(self, code: str, qty: int, avg_price: float) -> None:
        """Update internal positions when the PositionManager emits a change.

        This method updates the internal ``positions`` dictionary and recomputes the
        PnL for the affected symbol.  It then emits a new snapshot via the
        :data:`pnl_snapshot_ready` signal.

        Parameters:
            code: The symbol whose position changed.
            qty: The new executed quantity.
            avg_price: The new average buy price.
        """
        # Update stored position info
        self.positions[code] = {
            "qty": qty,
            "avg_price": avg_price,
        }
        # Recalculate PnL for this symbol if we have a price
        if code in self.prices:
            self._update_symbol_pnl(code)
        # Emit full snapshot
        self._emit_snapshot()

    def on_price(self, code: str, price: float, ts: str) -> None:
        """Handle incoming price updates.

        When a new price is received for a symbol, update the stored price and
        recalculate its unrealized PnL.  If the symbol is not yet known in
        ``positions``, it will still be stored so that the PnL can be
        calculated once a position becomes available.

        Parameters:
            code: The symbol for which a new price was received.
            price: The latest market price.
            ts: A timestamp string (ignored in current implementation).
        """
        # Store latest price
        self.prices[code] = price
        # Recalculate symbol PnL if we have a position
        if code in self.positions:
            self._update_symbol_pnl(code)
        # Emit full snapshot
        self._emit_snapshot()

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------
    def _update_symbol_pnl(self, code: str) -> None:
        """Compute the unrealized PnL for a single symbol.

        PnL is computed as ``(current_price - avg_price) * qty``.  If there is
        no position or no price, the PnL is set to 0.0.
        """
        pos = self.positions.get(code)
        price = self.prices.get(code)
        if not pos or price is None:
            self.pnls[code] = 0.0
            return
        qty = pos.get("qty", 0)
        avg = pos.get("avg_price") or 0.0
        if qty == 0:
            self.pnls[code] = 0.0
        else:
            self.pnls[code] = (price - avg) * qty

    def _emit_snapshot(self) -> None:
        """Emit a snapshot of the current positions, prices, and PnL."""
        # Compute total PnL
        total = sum(self.pnls.values()) if self.pnls else 0.0
        snapshot = {
            "positions": self.positions.copy(),
            "prices": self.prices.copy(),
            "pnls": self.pnls.copy(),
            "total_pnl": total,
        }
        try:
            self.pnl_snapshot_ready.emit(snapshot)
        except Exception:
            pass
