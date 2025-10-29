#  broker/base.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any, Protocol, Literal

OrderSide = Literal["BUY", "SELL"]

@dataclass
class OrderRequest:
    dmst_stex_tp: str
    stk_cd: str
    ord_qty: int
    ord_uv: Optional[int] = None      # 시장가면 None
    trde_tp: str = "0"                # '0': 지정가, '3': 시장가 등
    side: OrderSide = "BUY"
    cond_uv: str = ""
    account_id: Optional[str] = None    


@dataclass
class OrderResponse:
    status_code: int
    header: Dict[str, Any]
    body: Dict[str, Any]

class Broker(Protocol):
    def name(self) -> str: ...
    def place_order(self, req: OrderRequest) -> OrderResponse: ...
