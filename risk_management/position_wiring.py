# risk_management/position_wiring.py 

from __future__ import annotations
import logging
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QObject, QTimer, Slot

if TYPE_CHECKING:
    from trade_pro.position_manager import PositionManager
    from core.qt_bridge import QtBridge
from dataclasses import asdict

logger = logging.getLogger(__name__)


class PositionWiring(QObject):
    """
    PositionManager의 데이터를 UI(QtBridge)로 연결(wiring)하는 클래스.
    주기적으로 PnL 스냅샷을 생성하여 브리지에 시그널을 보낸다.
    """

    def __init__(
        self,
        pos_manager: PositionManager,
        bridge: QtBridge,
        parent: Optional[QObject] = None
    ):
        super().__init__(parent)
        self.pnl = pos_manager
        self.bridge = bridge
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._emit_pnl_snapshot)
        logger.info("PositionWiring 초기화 완료")

    @Slot()
    def _emit_pnl_snapshot(self):
        """타이머에 의해 주기적으로 호출되어 PnL 스냅샷을 브리지로 전송합니다."""
        try:
            snapshot = self.pnl.get_snapshot()
            if snapshot:
                snapshot_dict = asdict(snapshot)          # ✅ 직렬화
                self.bridge.pnl_snapshot_ready.emit(snapshot_dict)  # ✅ dict로 emit
        except Exception as e:
            logger.error("PnL 스냅샷 전송 중 오류: %s", e, exc_info=True)

    def setup_pnl_snapshot_flow(self, interval_sec: int = 3):
        """PnL 스냅샷 흐름을 설정하고 시작합니다."""
        if interval_sec > 0:
            self.timer.start(interval_sec * 1000)
            logger.info(f"{interval_sec}초 간격으로 PnL 스냅샷 전송을 시작합니다.")
        else:
            self.timer.stop()
            logger.info("PnL 스냅샷 전송을 중지합니다.")

    # -------------------------
    # 데이터 수신 슬롯 (이벤트 기반 업데이트)
    # -------------------------
    @Slot(object)
    def on_fill_or_trade(self, payload: Any):
        """체결 또는 거래와 유사한 페이로드를 처리합니다."""
        try:
            self._on_fill_payload(payload)
        except Exception as e:
            logger.error("체결/거래 처리 중 오류: %s payload=%r", e, payload, exc_info=True)

    @Slot(object)
    def on_price_update(self, payload: Any):
        """가격 업데이트 페이로드를 처리합니다."""
        try:
            self._on_price_payload(payload)
        except Exception as e:
            logger.error("가격 업데이트 처리 중 오류: %s payload=%r", e, payload, exc_info=True)

    def _on_fill_payload(self, payload: Any, *args, **kwargs):
        data = self._normalize_fill(payload, *args, **kwargs)
        if not data:
            return
        self.pnl.on_fill(
            cond_id=data.get("cond_id") or "default",
            code=str(data.get("code")),
            side=str(data.get("side")),
            qty=float(data.get("qty", 0) or 0),
            price=float(data.get("price", 0) or 0),
            ts=data.get("ts"),
            fee=float(data.get("fee", 0) or 0),
        )

    def _on_price_payload(self, payload: Any, *args, **kwargs):
        data = self._normalize_price(payload, *args, **kwargs)
        if not data:
            return
        self.pnl.on_price(
            code=str(data.get("code")),
            price=float(data.get("price", 0) or 0),
            ts=data.get("ts"),
        )

    # -------------------------
    # 데이터 정규화 유틸리티
    # -------------------------
    def _normalize_fill(self, payload: Any, *args, **kwargs) -> Optional[dict]:
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
        if isinstance(payload, dict):
            return {
                "ts": payload.get("ts") or payload.get("time") or payload.get("timestamp"),
                "code": payload.get("code") or payload.get("stock_code") or payload.get("ticker"),
                "price": payload.get("price") or payload.get("last") or payload.get("stck_prpr"),
            }
        return None