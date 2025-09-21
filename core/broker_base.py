# core/broker_base.py
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional

Side = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT"]


@dataclass
class Order:
    code: str               # 6자리 종목코드 (예: "005930")
    side: Side              # "BUY" | "SELL"
    qty: int                # 수량(주)
    order_type: OrderType = "MARKET"  # 기본 시장가
    price: Optional[float] = None     # 지정가일 때만 사용
    ts: float = 0.0                   # epoch seconds (0.0이면 내부에서 time.time() 사용)


@dataclass
class Fill:
    code: str
    side: Side
    qty: int
    price: float            # 체결가
    fee: float              # 수수료/세금 포함 총 비용(+는 비용)
    ts: float               # epoch seconds


@dataclass
class Position:
    code: str
    qty: int
    avg_price: float        # 보유평단 (0이면 미보유)


class ITradeBroker(ABC):
    """
    모든 주문은 이 인터페이스만 통해 처리합니다.
    실거래/시뮬레이션 브로커를 교체 주입하여 안전하게 분리합니다.
    """

    @abstractmethod
    async def place_order(self, order: Order) -> List[Fill]:
        """
        주문 전송 → (가능하면 즉시 체결) Fill 목록 반환.
        - 실거래: API 전송 → 체결 결과 polling/stream으로 확정 후 Fill 생성/반환(또는 빈 리스트 반환 후 별도 이벤트로 통지해도 됨)
        - 시뮬: 내부 룰로 즉시 Fill 생성/반환
        """
        raise NotImplementedError

    @abstractmethod
    async def close_all(self) -> List[Fill]:
        """
        전 종목 포지션 강제 청산(현물 기준: 보유수량 매도)
        """
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> Dict[str, Position]:
        """
        현재 포지션 스냅샷 반환.
        """
        raise NotImplementedError

    # ---- 선택 인터페이스 (시뮬용 가격공급) ----
    def set_price(self, code: str, price: float) -> None:
        """
        필요 시 최신가격을 공급(시뮬에서 사용).
        실거래 브로커는 무시해도 됨.
        """
        return

    # ---- 유틸 ----
    @staticmethod
    def now_ts() -> float:
        return time.time()
