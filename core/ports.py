# core/ports.py
from typing import Protocol, Optional
from dataclasses import dataclass

@dataclass
class OrderResult:
    order_id: str
    accepted: bool
    message: str = ""

class TradeAPIPort(Protocol):
    async def place_order(self, side: str, code: str, qty: int,
                          order_type: str, limit_price: Optional[float] = None,
                          tag: Optional[str] = None) -> OrderResult: ...
    async def get_position(self, code: str) -> int: ...
    async def get_cash(self) -> float: ...

class NotifierPort(Protocol):
    def info(self, msg: str) -> None: ...
    def warn(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...
