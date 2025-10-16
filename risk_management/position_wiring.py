"""
PositionWiring: 이벤트(payload)를 받아 적절한 객체에 전달하는 어댑터 역할을 합니다. 체결 이벤트는 PositionManager에 적용하고, 가격 업데이트는 SharedWalletPnL로 전달하며, 손익 스냅샷을 UI 브리지로 그대로 전달합니다. 내부에서 PnL을 계산하거나 포지션을 직접 수정하지 않습니다.

"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Slot

from trade_pro.position_manager import PositionManager
from risk_management.shared_wallet_pnl import SharedWalletPnL

logger = logging.getLogger(__name__)


class PositionWiring(QObject):
    """Connects position and PnL components to the Qt UI bridge.

    Parameters:
        pos_manager: The :class:`PositionManager` instance that tracks
            executed quantities, average prices, and pending orders.
        bridge: An object (usually a QtBridge) that exposes a
            ``pnl_snapshot_ready`` signal.  The signal will be emitted with a
            dictionary payload whenever the PnL snapshot changes.
        parent: Optional parent QObject.
    """

    def __init__(
        self,
        pos_manager: PositionManager,
        bridge: QObject,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.pos_manager = pos_manager
        self.pnl = SharedWalletPnL(pos_manager)
        self.bridge = bridge

        # Forward PnL snapshots from SharedWalletPnL directly to the bridge.
        try:
            self.pnl.pnl_snapshot_ready.connect(self.bridge.pnl_snapshot_ready)
        except Exception:
            # If bridge does not provide the signal, log a warning.
            logger.warning(
                "Bridge does not expose pnl_snapshot_ready signal; PnL snapshots will not be forwarded."
            )

        # Optional periodic snapshot timer.  This can be used if the UI
        # requires regular updates even when there is no price or position change.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._emit_pnl_snapshot)
        logger.info("PositionWiring initialized")

    # ------------------------------------------------------------------
    # PnL snapshot timer
    # ------------------------------------------------------------------
    @Slot()
    def _emit_pnl_snapshot(self) -> None:
        """Emit a snapshot of the current PnL via the bridge.

        This method is called by the QTimer; it simply retrieves the latest
        snapshot from ``SharedWalletPnL`` and re-emits it via the bridge's
        ``pnl_snapshot_ready`` signal.  If no bridge signal is available, the
        snapshot is silently ignored.
        """
        # Build the snapshot from the current SharedWalletPnL state.
        snapshot = {
            "positions": self.pnl.positions.copy(),
            "prices": self.pnl.prices.copy(),
            "pnls": self.pnl.pnls.copy(),
            "total_pnl": sum(self.pnl.pnls.values()) if self.pnl.pnls else 0.0,
        }
        try:
            self.bridge.pnl_snapshot_ready.emit(snapshot)
        except Exception:
            pass

    def setup_pnl_snapshot_flow(self, interval_sec: int = 3) -> None:
        """Start or stop periodic PnL snapshot emission.

        If ``interval_sec`` is greater than zero, a QTimer will emit PnL
        snapshots at the given interval (in seconds).  If zero or negative,
        periodic emission is disabled.  Note that ``SharedWalletPnL`` already
        emits snapshots when positions or prices change, so periodic emission
        is optional.
        """
        if interval_sec > 0:
            self.timer.start(interval_sec * 1000)
            logger.info(f"Sending PnL snapshots every {interval_sec} seconds.")
        else:
            self.timer.stop()
            logger.info("Periodic PnL snapshot emission stopped.")

    # ------------------------------------------------------------------
    # Event handlers for trade and price updates
    # ------------------------------------------------------------------
    @Slot(object)
    def on_fill_or_trade(self, payload: Any) -> None:
        """Handle a fill or trade payload from the core engine.

        The payload is normalized and then used to update the ``PositionManager``.
        The ``SharedWalletPnL`` will react automatically via its connection
        to the ``position_changed`` signal.
        """
        try:
            data = self._normalize_fill(payload)
            if not data:
                return
            code = str(data.get("code"))
            side = str(data.get("side")).upper()
            qty = float(data.get("qty", 0) or 0)
            price = float(data.get("price", 0) or 0)
            # Apply the fill via PositionManager.  Pending quantities will be
            # decremented automatically inside apply_fill_*.  SharedWalletPnL
            # listens to the position_changed signal and will update the PnL.
            if side == "BUY":
                self.pos_manager.apply_fill_buy(code, qty, price)
            elif side == "SELL":
                self.pos_manager.apply_fill_sell(code, qty, price)
        except Exception as e:
            logger.error("Error handling fill/trade payload: %s payload=%r", e, payload, exc_info=True)

    @Slot(object)
    def on_price_update(self, payload: Any) -> None:
        """Handle a price update payload from the core engine.

        The payload is normalized and passed to ``SharedWalletPnL.on_price`` to
        update the latest price and recalculate PnL.  The updated snapshot is
        then emitted via the bridge's ``pnl_snapshot_ready`` signal.
        """
        try:
            data = self._normalize_price(payload)
            if not data:
                return
            code = str(data.get("code"))
            price = float(data.get("price", 0) or 0)
            ts = data.get("ts")
            self.pnl.on_price(code, price, ts)
        except Exception as e:
            logger.error("Error handling price update payload: %s payload=%r", e, payload, exc_info=True)

    # ------------------------------------------------------------------
    # Payload normalization utilities
    # ------------------------------------------------------------------
    def _normalize_fill(self, payload: Any, *args, **kwargs) -> Optional[dict]:
        """Normalize a fill/trade payload into a dictionary.

        The payload may be a dict with various key names.  This method extracts
        the timestamp, strategy/condition ID, code, side, quantity, price, and fee.
        If the payload is not a dict, it returns None.
        """
        if isinstance(payload, dict):
            return {
                "ts": payload.get("ts") or payload.get("time") or payload.get("timestamp"),
                "cond_id": payload.get("cond_id") or payload.get("condition_name") or payload.get("strategy"),
                "code": payload.get("code") or payload.get("stock_code") or payload.get("ticker"),
                "side": payload.get("side") or payload.get("ord_side") or payload.get("type"),
                "qty": payload.get("qty") or payload.get("quantity") or payload.get("filled"),
                "price": payload.get("price") or payload.get("fill_price") or payload.get("avg_px"),
                "fee": payload.get("fee") or payload.get("commission") or 0.0,
            }
        return None

    def _normalize_price(self, payload: Any, *args, **kwargs) -> Optional[dict]:
        """Normalize a price update payload into a dictionary.

        The payload may be a dict with various key names.  This method extracts
        the timestamp, code, and price.  If the payload is not a dict,
        it returns None.
        """
        if isinstance(payload, dict):
            return {
                "ts": payload.get("ts") or payload.get("time") or payload.get("timestamp"),
                "code": payload.get("code") or payload.get("stock_code") or payload.get("ticker"),
                "price": payload.get("price") or payload.get("last") or payload.get("stck_prpr"),
            }
        return None
