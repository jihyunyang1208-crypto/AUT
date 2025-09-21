# brokers/broker_sim.py
from __future__ import annotations

import math
import time
from typing import Dict, List, Optional

from core.broker_base import ITradeBroker, Order, Fill, Position


class SimBroker(ITradeBroker):
    """
    - 실 API 호출 없이 내부 규칙으로 즉시 체결
    - 시장가: last ± 슬리피지
    - 지정가: 현재가가 지정가에 도달했다고 가정(보수적으로 체결가=지정가, 옵션 가능)
    - 수수료/거래세 적용(현물 기본)
    """

    def __init__(
        self,
        *,
        slippage_bps: float = 2.0,       # 2bp = 0.02%
        fee_rate: float = 0.00015,       # 0.015% (수수료)
        tax_rate: float = 0.0023,        # 0.23%  (거래세, 매도만)
        starting_cash: float = math.inf, # 무제한 현금 가정(원하면 숫자로)
    ):
        self._positions: Dict[str, Position] = {}
        self._fills: List[Fill] = []
        self._price_cache: Dict[str, float] = {}
        self.slippage_bps = float(slippage_bps)
        self.fee_rate = float(fee_rate)
        self.tax_rate = float(tax_rate)
        self.cash = float(starting_cash)

    # ---- 시뮬 전용 가격 공급 ----
    def set_price(self, code: str, price: float) -> None:
        self._price_cache[str(code)[-6:].zfill(6)] = float(price)

    def _last_price(self, code: str) -> Optional[float]:
        return self._price_cache.get(str(code)[-6:].zfill(6))

    # ---- 체결 규칙 ----
    def _exec_price(self, order: Order, ref_px: float) -> float:
        if order.order_type == "MARKET" or order.price is None:
            slip = ref_px * (self.slippage_bps / 10_000.0)
            return ref_px + slip if order.side == "BUY" else ref_px - slip
        # LIMIT: 단순히 지정가 체결로 가정(보수적/낙관적 옵션 필요 시 확장)
        return float(order.price)

    def _fees(self, side: str, notional: float) -> float:
        fee = notional * self.fee_rate
        tax = notional * self.tax_rate if side == "SELL" else 0.0
        return fee + tax

    def _apply_position(self, code: str, side: str, qty: int, px: float) -> None:
        pos = self._positions.get(code, Position(code=code, qty=0, avg_price=0.0))
        if side == "BUY":
            new_qty = pos.qty + qty
            pos.avg_price = (pos.avg_price * pos.qty + px * qty) / new_qty if new_qty else 0.0
            pos.qty = new_qty
        else:  # SELL
            pos.qty -= qty
            if pos.qty <= 0:
                pos.qty = 0
                pos.avg_price = 0.0
        self._positions[code] = pos

    # ---- 인터페이스 구현 ----
    async def place_order(self, order: Order) -> List[Fill]:
        code = str(order.code)[-6:].zfill(6)
        now = order.ts or self.now_ts()

        last = self._last_price(code)
        if last is None and order.price is None:
            # 가격이 없고 지정가도 없으면 체결 불가
            return []

        ref_px = last if last is not None else float(order.price)
        exec_px = self._exec_price(order, ref_px)
        notional = exec_px * order.qty
        fee = self._fees(order.side, notional)

        # (선택) 현금/증거금 체크 — starting_cash가 유한이면 검사
        if math.isfinite(self.cash):
            cash_after = self.cash - notional - fee if order.side == "BUY" else self.cash + notional - fee
            # 현금부족이면 미체결 처리(원하면 부분체결 로직 추가 가능)
            if cash_after < 0 and order.side == "BUY":
                return []

            self.cash = cash_after

        fill = Fill(code=code, side=order.side, qty=order.qty, price=exec_px, fee=fee, ts=now)
        self._fills.append(fill)
        self._apply_position(code, order.side, order.qty, exec_px)
        return [fill]

    async def close_all(self) -> List[Fill]:
        outs: List[Fill] = []
        # 양수: 매도, 음수: 매수(공매도/대주가 아닌 현물 가정에선 음수 포지션은 예상치 못한 상태)
        for code, pos in list(self._positions.items()):
            if pos.qty > 0:
                o = Order(code=code, side="SELL", qty=pos.qty, order_type="MARKET")
                outs.extend(await self.place_order(o))
        return outs

    def get_positions(self) -> Dict[str, Position]:
        # 얕은 복사
        return dict(self._positions)
