# risk_management/models.py 

from __future__ import annotations
import datetime as dt
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Union
import pandas as pd 
# ===============================================================
#  Core Data Models
# ===============================================================

@dataclass
class FillEvent:
    """체결 이벤트 데이터 모델"""
    ts: Union[str, dt.datetime]
    cond_id: str
    code: str
    side: str  # 'BUY' 또는 'SELL'
    qty: float
    price: float
    fee: float = 0.0

@dataclass
class PriceEvent:
    """현재가 업데이트 이벤트 데이터 모델"""
    ts: Union[str, dt.datetime]
    code: str
    price: float

@dataclass
class Position:
    """단일 종목의 포지션 상태를 관리하는 데이터 클래스"""
    code: str
    cond_id: str = "default"
    
    # 포지션 상태
    qty: float = 0.0
    avg_buy_price: float = 0.0
    avg_sell_price: float = 0.0
    
    # 가치 평가
    last_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    
    # 누적 손익
    realized_pnl: float = 0.0
    
    # 거래 비용 및 수량
    total_buy_qty: float = 0.0
    total_sell_qty: float = 0.0
    total_buy_value: float = 0.0
    total_sell_value: float = 0.0
    total_fee: float = 0.0
    
    # 타임스탬프
    last_updated_ts: Optional[dt.datetime] = None

    def _to_kst_dt(self, ts_any) -> dt.datetime:
        """입력 ts를 Asia/Seoul tz-aware python datetime으로 변환."""
        t = pd.Timestamp(ts_any)
        if t.tzinfo:
            t = t.tz_convert("Asia/Seoul")
        else:
            t = t.tz_localize("Asia/Seoul")
        return t.to_pydatetime()

    def _update_on_fill(self, fill: FillEvent):
        side = str(fill.side).upper()
        if side == 'BUY':
            new_total_qty = self.total_buy_qty + fill.qty
            self.total_buy_value += fill.qty * fill.price
            self.avg_buy_price = self.total_buy_value / new_total_qty if new_total_qty > 0 else 0
            self.total_buy_qty = new_total_qty
            self.qty += fill.qty
        elif side == 'SELL':
            cost_basis = self.avg_buy_price * fill.qty
            proceeds   = fill.qty * fill.price
            self.realized_pnl += proceeds - cost_basis
            new_total_qty = self.total_sell_qty + fill.qty
            self.total_sell_value += proceeds
            self.avg_sell_price = self.total_sell_value / new_total_qty if new_total_qty > 0 else 0
            self.total_sell_qty = new_total_qty
            self.qty -= fill.qty

        self.total_fee += fill.fee
        self.last_price = fill.price
        self._update_market_value()

        # ✅ tz-aware/naive 모두 안전
        self.last_updated_ts = self._to_kst_dt(fill.ts)

    def _update_on_price(self, price: PriceEvent):
        self.last_price = price.price
        self._update_market_value()
        # ✅ tz-aware/naive 모두 안전
        self.last_updated_ts = self._to_kst_dt(price.ts)

    def _update_market_value(self):
        """현재가를 기반으로 시장 가치와 미실현 손익을 업데이트합니다."""
        self.market_value = self.qty * self.last_price
        if self.qty > 0:
            cost_basis = self.qty * self.avg_buy_price
            self.unrealized_pnl = self.market_value - cost_basis
        else:
            self.unrealized_pnl = 0

# ===============================================================
#  Snapshot Data Models (UI 전달용)
# ===============================================================

@dataclass
class PnLPortfolioSnapshot:
    equity: float = 0.0
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    cum_return_pct: float = 0.0
    mdd_pct: float = 0.0
    equity_curve: List[Dict] = field(default_factory=list)
    daily_hist: List[Dict] = field(default_factory=list)
    gross_exposure_pct: float = 0.0
    cash: float = 0.0
    realized: float = 0.0

@dataclass
class PnLConditionSnapshot:
    equity: float = 0.0
    weight_pct: float = 0.0
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    cum_return_pct: float = 0.0
    mdd_pct: float = 0.0
    positions: List[Dict] = field(default_factory=list)
    equity_curve: List[Dict] = field(default_factory=list)
    daily_hist: List[Dict] = field(default_factory=list)
    symbol_count: int = 0

@dataclass
class PnLSnapshot:
    ts: str
    portfolio: PnLPortfolioSnapshot
    by_condition: Dict[str, PnLConditionSnapshot]
    # by_symbol 추가
    by_symbol: Dict[str, Dict] = field(default_factory=dict)