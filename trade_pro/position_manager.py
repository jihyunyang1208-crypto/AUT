from __future__ import annotations

import json
import threading
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional, Tuple

# Configure logging at the module level for consistency
# In a real application, this configuration might be done in a main entry point.
# Here, we set it up to ensure output.
logger = logging.getLogger(__name__)
if not logger.handlers:
    # Set level to DEBUG to capture all position manager events
    logger.setLevel(logging.DEBUG) 
    # Create a console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    # Define a simple format
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)

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
    
    def __str__(self) -> str:
        """String representation for logging."""
        # Use simple string formatting for logging
        return (f"Qty={self.qty}, AvgPrice={self.avg_price:.4f}, "
                f"PendingBuys={self.pending_buys}, PendingSells={self.pending_sells}")


class PositionManager(QObject):
    """Manages holdings and emits signals on changes."""

    position_changed: Signal = Signal(str, int, float)

    def __init__(self, store_path: str = "data/positions.json") -> None:
        super().__init__()
        self._path = Path(store_path)
        self._lock = threading.Lock()
        self._pos: Dict[str, Position] = {}
        self._log = logger
        self._load()

    # ---------- Logging Helper ----------
    def _log_status(self, event: str, symbol: Optional[str] = None, detail: str = "") -> None:
        """Helper to log the current position status."""
        log_message = f"[{event}]"
        
        if symbol is not None:
            # Check the status under the lock to ensure data consistency in log
            with self._lock:
                p = self._pos.get(symbol)
                status_str = str(p) if p else "Symbol not found/initialized"
            
            log_message = f"[{event}] Symbol: {symbol} | Status: {status_str}"
        
        if detail:
             log_message += f" | Detail: {detail}"

        self._log.debug(log_message)


    # ---------- Persistence ----------
    def _load(self) -> None:
        """Load position data from the JSON file."""
        self._log.debug("[Load] Attempting to load positions.")
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for sym, d in data.items():
                    self._pos[sym] = Position(**d)
                self._log.info(f"[Load] Successfully loaded {len(self._pos)} symbols.")
            else:
                 self._log.info("[Load] Position file not found. Starting with empty positions.")
        except Exception as e:
            self._log.error(f"[Load] Error during loading: {e}. Positions initialized to empty.", exc_info=True)
            pass

    def _save(self) -> None:
        """Persist position data to the JSON file."""
        # This is called inside the lock in the update methods
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: asdict(v) for k, v in self._pos.items()}
            self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            # Log save event, but not every single save to avoid excessive logging
            # self._log.debug("[Save] Positions persisted to file.") 
        except Exception as e:
            self._log.warning(f"[Save] Error during saving: {e}", exc_info=True)
            pass

    # ---------- Helpers ----------
    def _get(self, symbol: str) -> Position:
        """Return the Position object for the given symbol, creating it if necessary."""
        if symbol not in self._pos:
            self._pos[symbol] = Position()
            self._log_status("Init", symbol, "New position created.")
        return self._pos[symbol]

    # ---------- Queries ----------
    def get_qty(self, symbol: str) -> int:
        """Return the executed quantity of the given symbol."""
        with self._lock:
            qty = self._get(symbol).qty
        self._log_status("Query", symbol, f"Requested Qty: {qty}")
        return qty

    def get_avg_price(self, symbol: str) -> Optional[float]:
        """Return the average buy price if quantity > 0, else None."""
        with self._lock:
            p = self._get(symbol)
            avg_price = p.avg_price if p.qty > 0 else None
        self._log_status("Query", symbol, f"Requested Avg Price: {avg_price}")
        return avg_price

    def get_avg_buy(self, symbol: str) -> Optional[float]:
        """Official alias for get_avg_price()."""
        return self.get_avg_price(symbol)

    def get_pending(self, symbol: str) -> Tuple[int, int]:
        """Return a tuple (pending_buys, pending_sells)."""
        with self._lock:
            p = self._get(symbol)
            pending = (p.pending_buys, p.pending_sells)
        self._log_status("Query", symbol, f"Requested Pending: {pending}")
        return pending

    # ---------- Internal helper for signaling ----------
    def _emit_change(self, symbol: str) -> None:
        """Emit the position_changed signal for a symbol."""
        # Retrieve current qty and avg_price under lock to ensure consistency
        with self._lock:
            p = self._get(symbol)
            qty = p.qty
            avg = p.avg_price if p.qty > 0 else 0.0
            
        # Log the state just before signal emission
        self._log_status("SignalEmit", symbol, f"Emitting signal (Qty: {qty}, Avg: {avg:.4f})")
        
        try:
            self.position_changed.emit(symbol, qty, avg)
        except Exception as e:
            # If signals are disabled (e.g., no Qt event loop), log a warning
            self._log.warning(f"Failed to emit signal for {symbol}: {e}")
            pass

    # ---------- Reservations (on submit/cancel) ----------
    def reserve_buy(self, symbol: str, qty: int) -> None:
        """Reserve a quantity for a pending buy order and emit a change."""
        if qty <= 0:
            return
            
        self._log_status("ReserveBuy", symbol, f"Attempting to reserve buy qty: {qty}")
        
        with self._lock:
            p = self._get(symbol)
            
            # Log pre-change status
            old_pending_buys = p.pending_buys
            
            p.pending_buys += qty
            self._save()
            
            # Log post-change status
            self._log_status("ReserveBuy", symbol, 
                             f"Update: PendingBuys {old_pending_buys} -> {p.pending_buys}")
            
        self._emit_change(symbol)

    def reserve_sell(self, symbol: str, qty: int) -> None:
        """Reserve a quantity for a pending sell order and emit a change."""
        if qty <= 0:
            return
            
        self._log_status("ReserveSell", symbol, f"Attempting to reserve sell qty: {qty}")
        
        with self._lock:
            p = self._get(symbol)
            
            # Log pre-change status
            old_pending_sells = p.pending_sells
            
            p.pending_sells += qty
            self._save()
            
            # Log post-change status
            self._log_status("ReserveSell", symbol, 
                             f"Update: PendingSells {old_pending_sells} -> {p.pending_sells}")
            
        self._emit_change(symbol)

    def release_buy(self, symbol: str, qty: int) -> None:
        """Reduce pending buys by qty and emit a change."""
        if qty <= 0:
            return
            
        self._log_status("ReleaseBuy", symbol, f"Attempting to release buy qty: {qty}")
        
        with self._lock:
            p = self._get(symbol)
            
            # Log pre-change status
            old_pending_buys = p.pending_buys
            
            p.pending_buys = max(0, p.pending_buys - qty)
            self._save()
            
            # Log post-change status
            self._log_status("ReleaseBuy", symbol, 
                             f"Update: PendingBuys {old_pending_buys} -> {p.pending_buys}")
            
        self._emit_change(symbol)

    def release_sell(self, symbol: str, qty: int) -> None:
        """Reduce pending sells by qty and emit a change."""
        if qty <= 0:
            return
            
        self._log_status("ReleaseSell", symbol, f"Attempting to release sell qty: {qty}")
        
        with self._lock:
            p = self._get(symbol)
            
            # Log pre-change status
            old_pending_sells = p.pending_sells
            
            p.pending_sells = max(0, p.pending_sells - qty)
            self._save()
            
            # Log post-change status
            self._log_status("ReleaseSell", symbol, 
                             f"Update: PendingSells {old_pending_sells} -> {p.pending_sells}")
            
        self._emit_change(symbol)

    # ---------- Fills (on execution) ----------
    def apply_fill_buy(self, symbol: str, qty: int, price: float) -> None:
        """Apply an executed buy and update quantity/avg_price, emitting a change."""
        if qty <= 0:
            return
            
        self._log_status("FillBuy", symbol, f"Applying fill: Qty={qty}, Price={price:.4f}")
        
        with self._lock:
            p = self._get(symbol)
            
            # Log pre-change status
            old_qty, old_avg, old_pending = p.qty, p.avg_price, p.pending_buys
            
            new_qty = p.qty + qty
            if new_qty > 0:
                # Weighted average price calculation
                p.avg_price = (
                    (p.avg_price * p.qty + float(price) * qty) / new_qty
                    if p.qty > 0 else float(price)
                )
            p.qty = new_qty
            # Remove from pending buys
            p.pending_buys = max(0, p.pending_buys - qty)
            self._save()
            
            # Log post-change status
            self._log_status("FillBuy", symbol, 
                             f"Change: Qty {old_qty}->{p.qty}, Avg {old_avg:.4f}->{p.avg_price:.4f}, Pending {old_pending}->{p.pending_buys}")
            
        self._emit_change(symbol)

    def apply_fill_sell(self, symbol: str, qty: int, price: float) -> None:
        """Apply an executed sell and update quantity/avg_price, emitting a change."""
        if qty <= 0:
            return
            
        self._log_status("FillSell", symbol, f"Applying fill: Qty={qty}, Price={price:.4f}")
        
        with self._lock:
            p = self._get(symbol)
            
            # Log pre-change status
            old_qty, old_avg, old_pending = p.qty, p.avg_price, p.pending_sells
            
            p.qty = max(0, p.qty - qty)
            p.pending_sells = max(0, p.pending_sells - qty)
            
            if p.qty == 0:
                # Reset average price on full exit
                p.avg_price = 0.0
                
            self._save()
            
            # Log post-change status
            self._log_status("FillSell", symbol, 
                             f"Change: Qty {old_qty}->{p.qty}, Avg {old_avg:.4f}->{p.avg_price:.4f}, Pending {old_pending}->{p.pending_sells}")
            
        self._emit_change(symbol)