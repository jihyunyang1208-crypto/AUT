# brokers/broker_live.py
from __future__ import annotations

import logging
from typing import Dict, List

from core.broker_base import ITradeBroker, Order, Fill, Position

logger = logging.getLogger(__name__)


class LiveBroker(ITradeBroker):
    """
    한국투자증권/키움 등 실매매 API 래핑을 위한 베이스 구현.
    기본값은 '보호 모드'로, 실제 주문을 던지지 않고 로그만 남깁니다.
    실거래를 켜려면 _send_order_* 메서드를 실제 API로 구현하세요.
    """

    def __init__(self, *, protect_mode: bool = True):
        self._positions: Dict[str, Position] = {}
        self._protect_mode = bool(protect_mode)

        # (옵션) 여기에 인증/토큰/세션 객체 주입
        # self._client = SomeApiClient(appkey=..., secret=..., token=...)

    # ---- 내부: 실거래 전송 루틴(구현 필요) ----
    def _send_order_market(self, order: Order) -> None:
        """
        TODO: 증권사 API 호출(시장가)
        """
        raise NotImplementedError("실거래 API 연결 전까지 보호 모드 유지")

    def _send_order_limit(self, order: Order) -> None:
        """
        TODO: 증권사 API 호출(지정가)
        """
        raise NotImplementedError("실거래 API 연결 전까지 보호 모드 유지")

    # ---- 체결/포지션 반영(간이) ----
    def _apply_fill_locally(self, order: Order, price: float, fee: float) -> Fill:
        code = order.code
        pos = self._positions.get(code, Position(code=code, qty=0, avg_price=0.0))

        if order.side == "BUY":
            new_qty = pos.qty + order.qty
            pos.avg_price = (pos.avg_price * pos.qty + price * order.qty) / new_qty if new_qty else 0.0
            pos.qty = new_qty
        else:
            pos.qty -= order.qty
            if pos.qty <= 0:
                pos.qty = 0
                pos.avg_price = 0.0

        self._positions[code] = pos
        f = Fill(code=code, side=order.side, qty=order.qty, price=price, fee=fee, ts=order.ts or self.now_ts())
        return f

    # ---- 인터페이스 구현 ----
    async def place_order(self, order: Order) -> List[Fill]:
        code = order.code
        logger.info("[LiveBroker] %s %s x%d (%s @ %s)",
                    order.side, code, order.qty, order.order_type, order.price if order.price else "MKT")

        if self._protect_mode:
            # 보호 모드: 실제 전송 금지, 로컬 가짜 체결(체크/리스크 검증용)
            px = float(order.price) if order.order_type == "LIMIT" and order.price else float("nan")
            fee = 0.0
            fill = self._apply_fill_locally(order, price=px, fee=fee)
            return [fill]

        # 실거래 모드: 실제 API 호출
        try:
            if order.order_type == "MARKET" or order.price is None:
                self._send_order_market(order)
            else:
                self._send_order_limit(order)
        except NotImplementedError as e:
            logger.error("🚫 실거래 API가 구현되지 않았습니다: %s", e)
            return []

        # 보통은 체결 이벤트/웹소켓으로 Fill을 나중에 받습니다.
        # 간단히는 '주문 접수' 의미로 빈 리스트 반환 or 낙관적 체결 처리
        return []

    async def close_all(self) -> List[Fill]:
        # 실제론 보유 목록을 조회해 청산 주문을 전송해야 합니다.
        outs: List[Fill] = []
        for code, pos in list(self._positions.items()):
            if pos.qty > 0:
                o = Order(code=code, side="SELL", qty=pos.qty, order_type="MARKET")
                outs.extend(await self.place_order(o))
        return outs

    def get_positions(self) -> Dict[str, Position]:
        return dict(self._positions)
