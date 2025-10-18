# broker/simulator.py
from __future__ import annotations
import time, uuid
from typing import Optional, Dict, Any
from .base import Broker, OrderRequest, OrderResponse

class SimulatorBroker(Broker):
    """간단 체결 로직: 시장가=즉시, 지정가=주어진 가격으로 즉시 가정."""
    def __init__(self, *, fee_bps: float = 0.0, slippage_ticks: int = 0):
        self.fee_bps = float(fee_bps)
        self.slippage_ticks = int(slippage_ticks)

    def name(self) -> str:
        return "sim"

    def place_order(self, req: OrderRequest) -> OrderResponse:
        # 체결가 계산(아주 단순): 시장가면 0, 지정가면 ord_uv
        px = 0 if req.ord_uv is None or req.trde_tp == "3" else int(req.ord_uv or 0)
        oid = uuid.uuid4().hex[:16]
        body = {
            "simulated": True,
            "order_id": oid,
            "fills": [{"qty": int(req.ord_qty), "price": px, "side": req.side}],
            "fee": round((px * req.ord_qty) * self.fee_bps / 10000.0, 4),
            "ts": time.time(),
        }
        return OrderResponse(status_code=999, header={"sim": "1"}, body=body)
