from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Optional


@dataclass
class FillEvent:
ts: str
cond_id: str
code: str
side: str # BUY/SELL
qty: float
price: float
fee: float = 0.0


@dataclass
class PriceEvent:
ts: str
code: str
price: float


@dataclass
class PnLPortfolioSnapshot:
equity: float = 0.0
daily_pnl: float = 0.0
daily_pnl_pct: float = 0.0
cum_return_pct: float = 0.0
mdd_pct: float = 0.0
equity_curve: List[Dict] = field(default_factory=list) # [{"t","equity"}]
daily_hist: List[Dict] = field(default_factory=list) # [{"d","pnl"}]
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
positions: List[Dict] = field(default_factory=list) # [{code,qty,avg,last,unreal}]
equity_curve: List[Dict] = field(default_factory=list)
daily_hist: List[Dict] = field(default_factory=list)
symbol_count: int = 0


@dataclass
class PnLSnapshot:
ts: str
portfolio: PnLPortfolioSnapshot
by_condition: Dict[str, PnLConditionSnapshot]