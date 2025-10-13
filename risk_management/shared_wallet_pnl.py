# risk_management/shared_wallet_pnl.py 
from __future__ import annotations
import logging
import datetime as dt
from collections import defaultdict
from typing import Dict, Optional, List

import pandas as pd

from .models import (
    Position, FillEvent, PriceEvent,
    PnLSnapshot, PnLPortfolioSnapshot, PnLConditionSnapshot
)

logger = logging.getLogger(__name__)

class SharedWalletPnL:
    """
    공유 현금 지갑 기반으로, 종목 및 전략별 손익을 집계하고 관리합니다.
    Position 객체를 사용하여 개별 포지션의 상세 상태를 추적합니다.
    """
    def __init__(self, *, base_cash: float = 100_000_000, tz: str = "Asia/Seoul"):
        self.tz = tz
        self.base_cash = float(base_cash)
        self.cash = float(base_cash)

        self.positions: Dict[str, Position] = {}

        # ---- 포트폴리오 집계 상태 ----
        now = pd.Timestamp.now(tz=self.tz)
        # Matplotlib과 잘 맞도록 tz-naive 파이썬 datetime(현지시각)으로 저장
        self.port_equity_curve: List[Dict] = [{
            "t": now.tz_convert(self.tz).to_pydatetime().replace(tzinfo=None),
            "equity": self.base_cash
        }]
        self.port_daily_hist: Dict[str, float] = defaultdict(float)  # {"YYYY-MM-DD": pnl}
        self._last_equity = self.base_cash          # 직전 스냅샷 기준
        self._prev_equity_for_hist = self.base_cash # 일별 누적용 시작 기준(자정 또는 첫 스냅샷)
        self._peak_equity = self.base_cash          # MDD 계산용 피크

    # --------------- 내부 유틸 ---------------
    def _to_local_dt(self, ts) -> dt.datetime:
        t = pd.Timestamp(ts) if ts is not None else pd.Timestamp.utcnow()
        if t.tzinfo:
            t = t.tz_convert(self.tz)
        else:
            t = t.tz_localize(self.tz)
        return t.to_pydatetime()

    def _now_kst_naive(self) -> dt.datetime:
        return pd.Timestamp.now(tz=self.tz).to_pydatetime().replace(tzinfo=None)

    def _update_equity_curve_and_hist(self):
        """가격/체결 이후 현재 equity를 곡선/일일 히스토그램에 반영."""
        total_mkt_val = sum(p.market_value for p in self.positions.values())
        equity = self.cash + total_mkt_val

        # 1) 에쿼티 곡선(최근 200개만 유지)
        self.port_equity_curve.append({"t": self._now_kst_naive(), "equity": equity})
        if len(self.port_equity_curve) > 200:
            self.port_equity_curve = self.port_equity_curve[-200:]

        # 2) 일일 히스토그램 누적
        dkey = pd.Timestamp.now(tz=self.tz).strftime("%Y-%m-%d")
        # 오늘의 PnL = 현재 equity - '오늘 0시' 또는 '첫 스냅샷 시점'의 equity
        # (_prev_equity_for_hist는 자정 교체되면 재설정하는 식으로 더 다듬을 수 있음)
        self.port_daily_hist[dkey] = float(equity - self._prev_equity_for_hist)

        # 3) MDD 계산용 피크 갱신
        if equity > self._peak_equity:
            self._peak_equity = equity

        return equity, total_mkt_val

    # --------------- 이벤트 핸들러 ---------------
    def on_fill(self, cond_id: str, code: str, side: str, qty: float, price: float, ts=None, fee: float = 0.0):
        ts_local = self._to_local_dt(ts)
        fill = FillEvent(ts=ts_local, cond_id=cond_id, code=code, side=side.upper(), qty=qty, price=price, fee=fee)

        if code not in self.positions:
            self.positions[code] = Position(code=code, cond_id=cond_id)

        if fill.side == 'BUY':
            self.cash -= (fill.qty * fill.price) + fill.fee
        elif fill.side == 'SELL':
            self.cash += (fill.qty * fill.price) - fill.fee

        self.positions[code]._update_on_fill(fill)
        self._update_equity_curve_and_hist()

    def on_price(self, code: str, price: float, ts=None):
        if code not in self.positions:
            return

        ts_local = self._to_local_dt(ts)
        price_event = PriceEvent(ts=ts_local, code=code, price=price)

        pos = self.positions[code]
        pos._update_on_price(price_event)
        self._update_equity_curve_and_hist()

    # --------------- 스냅샷 ---------------
    def get_snapshot(self) -> Optional[PnLSnapshot]:
        now = pd.Timestamp.now(tz=self.tz)

        by_symbol_snap = {code: pos.__dict__ for code, pos in self.positions.items()}

        total_mkt_val = sum(p.market_value for p in self.positions.values())
        realized = sum(p.realized_pnl for p in self.positions.values())
        equity = self.cash + total_mkt_val

        # intraday 변화(직전 스냅샷 대비)
        daily_pnl = equity - self._last_equity
        daily_pnl_pct = (daily_pnl / self._last_equity * 100) if self._last_equity else 0.0

        # 누적 수익률(초기자본 대비)
        cum_return_pct = ((equity / self.base_cash) - 1.0) * 100 if self.base_cash else 0.0

        # MDD 계산: (피크 - 현재)/피크
        mdd_pct = 0.0
        if self._peak_equity > 0:
            mdd_pct = -((self._peak_equity - equity) / self._peak_equity) * 100  # 음수값로 표현(예: -5.2)

        port_snap = PnLPortfolioSnapshot(
            equity=equity,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            cum_return_pct=cum_return_pct,
            mdd_pct=mdd_pct,
            cash=self.cash,
            realized=realized,
            equity_curve=self.port_equity_curve,
            daily_hist=[{"d": d, "pnl": v} for d, v in sorted(self.port_daily_hist.items())],
            gross_exposure_pct=(total_mkt_val / equity * 100) if equity else 0.0,
        )

        by_cond_snap = defaultdict(PnLConditionSnapshot)
        for code, pos in self.positions.items():
            cond = by_cond_snap[pos.cond_id]
            cond.positions.append(pos.__dict__)
            cond.symbol_count += 1
            cond.equity += pos.market_value

        snap = PnLSnapshot(
            ts=now.isoformat(),
            portfolio=port_snap,
            by_condition=dict(by_cond_snap),
            by_symbol=by_symbol_snap,
        )

        # 다음 스냅샷을 위한 기준 갱신
        self._last_equity = equity
        return snap
