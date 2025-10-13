# trade_pro/position_manager.py 
from __future__ import annotations
import logging
from typing import Optional

# risk_management 모듈의 구성요소들을 가져와 사용합니다.
from risk_management.shared_wallet_pnl import SharedWalletPnL
from risk_management.models import PnLSnapshot

logger = logging.getLogger(__name__)

class PositionManager:
    """
    애플리케이션의 포지션 관리를 총괄하는 중앙 클래스.
    내부적으로 SharedWalletPnL을 사용하여 실제 손익 계산을 수행하고,
    UI에 필요한 데이터 스냅샷을 제공하는 인터페이스 역할을 한다.
    """
    def __init__(self, *, base_cash: float = 100_000_000, tz: str = "Asia/Seoul"):
        """
        PositionManager를 초기화합니다.

        Args:
            base_cash (float): 초기 투자 원금.
            tz (str): 타임존 (예: "Asia/Seoul").
        """
        # 손익 계산의 핵심 로직을 담당하는 SharedWalletPnL 인스턴스를 생성합니다.
        self.pnl = SharedWalletPnL(base_cash=base_cash, tz=tz)
        logger.info(f"PositionManager 초기화 완료 (초기 현금: {base_cash:,.0f})")

    def on_fill(self, cond_id: str, code: str, side: str, qty: float, price: float, ts=None, fee: float = 0.0):
        """
        체결 이벤트를 내부 PnL 관리자에게 전달합니다.
        """
        try:
            self.pnl.on_fill(cond_id, code, side, qty, price, ts, fee)
        except Exception as e:
            logger.error(f"체결 처리 중 오류 발생: {e}", exc_info=True)

    def on_price(self, code: str, price: float, ts=None):
        """
        가격 업데이트 이벤트를 내부 PnL 관리자에게 전달합니다.
        """
        try:
            self.pnl.on_price(code, price, ts)
        except Exception as e:
            logger.error(f"가격 업데이트 처리 중 오류 발생: {e}", exc_info=True)

    def get_snapshot(self) -> Optional[PnLSnapshot]:
        """
        UI에 표시할 현재 포트폴리오의 전체 스냅샷을 반환합니다.
        이 함수는 PositionWiring에 의해 주기적으로 호출됩니다.
        """
        try:
            return self.pnl.get_snapshot()
        except Exception as e:
            logger.error(f"스냅샷 생성 중 오류 발생: {e}", exc_info=True)
            return None

    def get_avg_buy(self, symbol: str) -> Optional[float]:
        """
        특정 종목의 평균 매수 단가를 조회합니다.
        """
        pos = self.pnl.positions.get(symbol)
        return pos.avg_buy_price if pos and pos.qty > 0 else None

    def get_qty(self, symbol: str) -> float:
        """
        특정 종목의 현재 보유 수량을 조회합니다.
        """
        return self.pnl.positions.get(symbol).qty if symbol in self.pnl.positions else 0.0