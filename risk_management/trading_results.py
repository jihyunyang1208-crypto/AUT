# risk_management/trading_results.py
"""
이중 저장 구조 트레이딩 결과 관리 (UI 자동 갱신 + 알림)
- trading_result_YYYY-MM-DD.json: 데일리 전략 성과
- trading_result.json: 누적 포지션 추적
"""
from __future__ import annotations

import json
import os
import threading
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Any, List, Callable, Union, Tuple
from datetime import datetime, time as dt_time, timezone, timedelta

from PySide6.QtCore import QObject, Signal  # UI 자동 반영용

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")

KST = timezone(timedelta(hours=9))

def get_today_str() -> str:
    return datetime.now(KST).date().isoformat()

# ==================== 데이터 모델 ====================

@dataclass
class TradeRow:
    """거래 레코드"""
    time: str
    side: str
    symbol: str
    qty: int
    price: float
    fee: float = 0.0
    status: str = ""
    strategy: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None

@dataclass
class AlertConfig:
    """알림 설정"""
    enable_pf_alert: bool = True
    enable_consecutive_loss_alert: bool = True
    consecutive_loss_threshold: int = 3
    enable_daily_loss_alert: bool = True
    daily_loss_limit: float = -500000.0
    on_alert: Optional[Callable[[str, str, Dict[str, Any]], None]] = None

@dataclass
class TimeSlotStats:
    """시간대별 통계"""
    realized_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    win_rate: float = 0.0

@dataclass
class SymbolPosition:
    """종목별 누적 포지션"""
    code: str
    qty: int = 0
    avg_price: float = 0.0
    total_buy_amt: float = 0.0

    # FIFO 큐
    buy_history: List[Dict[str, Any]] = field(default_factory=list)

    # 누적 손익
    cumulative_realized_gross: float = 0.0
    cumulative_realized_net: float = 0.0
    cumulative_fees: float = 0.0

    # 최신 거래
    last_buy_price: float = 0.0
    last_buy_date: str = ""
    last_sell_price: float = 0.0
    last_sell_date: str = ""

    # 통계
    total_trades: int = 0
    total_wins: int = 0

@dataclass
class StrategyState:
    """전략별 데일리 집계"""
    name: str
    date: str

    buy_notional: float = 0.0
    buy_qty: int = 0
    fees: float = 0.0
    realized_pnl_gross: float = 0.0
    realized_pnl_net: float = 0.0
    wins: int = 0
    sells: int = 0
    win_rate: float = 0.0
    roi_pct: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0

    # 시간대별
    morning_stats: TimeSlotStats = field(default_factory=TimeSlotStats)
    afternoon_stats: TimeSlotStats = field(default_factory=TimeSlotStats)

    # 알림용
    consecutive_losses: int = 0
    max_consecutive_losses: int = 0
    daily_loss: float = 0.0

# ==================== 메인 스토어 ====================

class TradingResultStore(QObject):
    """
    이중 저장 구조:
    1. daily_path: trading_result_YYYY-MM-DD.json (전략 성과)
    2. cumulative_path: trading_result.json (포지션 추적)
    """

    # ✅ 저장 후 UI에서 자동 새로고침 받을 수 있도록 신호 제공
    store_updated = Signal()

    def __init__(
        self,
        json_path: Optional[str | Path] = None,
        alert_config: Optional[AlertConfig] = None
    ) -> None:
        super().__init__()  # QObject 초기화

        if json_path is None:
            json_path = Path("logs/results/trading_result.json")

        self.base_dir = Path(json_path).parent
        self.base_dir.mkdir(parents=True, exist_ok=True)

        # 경로
        self.cumulative_path = self.base_dir / "trading_result.json"
        self._current_date = get_today_str()
        self.daily_path = self._get_daily_path(self._current_date)

        # 상태
        self._positions: Dict[str, SymbolPosition] = {}  # 누적 포지션
        self._daily_strategies: Dict[str, StrategyState] = {}  # 오늘 전략

        self._lock = threading.RLock()
        self._alert_config = alert_config or AlertConfig()

        # 로드
        self._load_cumulative()
        self._load_or_init_daily()

        logger.info(
            f"[TradingResultStore] initialized | "
            f"daily={self.daily_path.name} | cumulative={self.cumulative_path.name}"
        )

    # --------------------------------------------------
    # 기본 유틸
    # --------------------------------------------------

    def _get_daily_path(self, date_str: str) -> Path:
        return self.base_dir / f"trading_result_{date_str}.json"

    def set_alert_callback(self, callback: Callable[[str, str, Dict[str, Any]], None]) -> None:
        """외부(UI 등) 알림 콜백 설정"""
        self._alert_config.on_alert = callback

    def check_date_rollover(self) -> bool:
        """날짜 전환 확인 (자정 이후)"""
        today = get_today_str()
        if today != self._current_date:
            with self._lock:
                logger.info(f"[Store] Date rollover: {self._current_date} -> {today}")
                self._finalize_daily()
                self._current_date = today
                self.daily_path = self._get_daily_path(today)
                self._daily_strategies.clear()
                self._load_or_init_daily()
            return True
        return False

    # --------------------------------------------------
    # 거래 반영 (오버로드 지원)
    # --------------------------------------------------

    def apply_trade(self, *args, **kwargs) -> None:
        """
        거래 적용 + 자동 저장 + UI emit

        사용 가능한 서명:
        - apply_trade(TradeRow)
        - apply_trade(symbol, side, qty, price, strategy=None, time=None, fee=0.0, status="")
        """
        # 1) 형태 식별
        if len(args) == 1 and isinstance(args[0], TradeRow):
            t: TradeRow = args[0]
        else:
            # 파라미터 버전
            symbol, side, qty, price = None, None, None, None
            if len(args) >= 4:
                symbol, side, qty, price = args[:4]
            else:
                symbol = kwargs.get("symbol")
                side = kwargs.get("side")
                qty = kwargs.get("qty")
                price = kwargs.get("price")

            if not symbol or not side or not qty or not price:
                return

            t = TradeRow(
                time=kwargs.get("time") or datetime.now(KST).isoformat(),
                side=str(side).lower(),
                symbol=str(symbol),
                qty=int(qty),
                price=float(price),
                fee=float(kwargs.get("fee", 0.0)),
                status=str(kwargs.get("status") or "") or "filled",
                strategy=(kwargs.get("strategy") or "default"),
                meta=None
            )

        # 2) 유효성
        if t.qty <= 0 or t.price <= 0:
            return

        # 3) 날짜 롤오버 체크
        self.check_date_rollover()

        # 4) 핵심 로직
        strategy_key = (t.strategy or "default").strip() or "default"
        trade_date = self._extract_date(t.time)

        with self._lock:
            pos = self._positions.setdefault(t.symbol, SymbolPosition(code=t.symbol))
            daily = self._daily_strategies.setdefault(
                strategy_key,
                StrategyState(name=strategy_key, date=self._current_date)
            )

            if t.side == "buy":
                self._apply_buy(pos, daily, strategy_key, t, trade_date)
            elif t.side == "sell":
                self._apply_sell(pos, daily, t, trade_date)

            self._recalc_daily_metrics(daily)
            self._check_alerts(strategy_key, daily)

            # 5) 저장 + UI 갱신
            self._save_both()

    # --------------------------------------------------
    # 스냅샷
    # --------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """현재 상태 스냅샷 (UI 호환성)"""
        with self._lock:
            return self._build_daily_payload()

    def get_daily_snapshot(self) -> Dict[str, Any]:
        """데일리 스냅샷"""
        with self._lock:
            return self._build_daily_payload()

    def get_cumulative_snapshot(self) -> Dict[str, Any]:
        """누적 스냅샷"""
        with self._lock:
            return self._build_cumulative_payload()

    def get_symbol_position(self, code: str) -> Optional[Dict[str, Any]]:
        """종목 포지션 조회"""
        with self._lock:
            pos = self._positions.get(code)
            if not pos:
                return None

            return {
                "code": code,
                "qty": pos.qty,
                "avg_price": pos.avg_price,
                "cumulative_pnl": pos.cumulative_realized_net,
                "last_buy": {"price": pos.last_buy_price, "date": pos.last_buy_date},
                "last_sell": {"price": pos.last_sell_price, "date": pos.last_sell_date}
            }

    def rebuild_from_trades(self, trades: List[TradeRow]) -> None:
        """전체 재계산"""
        with self._lock:
            self._positions.clear()
            self._daily_strategies.clear()

            # 날짜별 그룹핑
            trades_by_date: Dict[str, List[TradeRow]] = {}
            for tr in sorted(trades, key=lambda x: x.time):
                date = self._extract_date(tr.time)
                trades_by_date.setdefault(date, []).append(tr)

            # 날짜순 처리
            for date in sorted(trades_by_date.keys()):
                self._current_date = date
                self.daily_path = self._get_daily_path(date)

                for tr in trades_by_date[date]:
                    if tr.qty <= 0 or tr.price <= 0:
                        continue
                    strategy_key = (tr.strategy or "default").strip() or "default"
                    pos = self._positions.setdefault(tr.symbol, SymbolPosition(code=tr.symbol))
                    daily = self._daily_strategies.setdefault(
                        strategy_key,
                        StrategyState(name=strategy_key, date=date)
                    )
                    if tr.side == "buy":
                        self._apply_buy(pos, daily, strategy_key, tr, date)
                    elif tr.side == "sell":
                        self._apply_sell(pos, daily, tr, date)

                # 해당 날짜 저장
                for d in self._daily_strategies.values():
                    self._recalc_daily_metrics(d)

                self._atomic_write(self.daily_path, self._build_daily_payload())
                self._daily_strategies.clear()

            # 최종 누적 저장
            self._current_date = get_today_str()
            self.daily_path = self._get_daily_path(self._current_date)
            self._atomic_write(self.cumulative_path, self._build_cumulative_payload())

            logger.info(
                f"[Store] Rebuild: {len(trades)} trades, "
                f"{len(self._positions)} symbols, {len(trades_by_date)} days"
            )
            self.store_updated.emit()

    def reset(self) -> None:
        """리셋 (테스트용)"""
        with self._lock:
            self._positions.clear()
            self._daily_strategies.clear()
            self._save_both()

    # ==================== 내부 로직 ====================

    def _apply_buy(
        self,
        pos: SymbolPosition,
        daily: StrategyState,
        strategy: str,
        t: TradeRow,
        trade_date: str
    ) -> None:
        """매수 처리"""
        # 포지션 갱신
        new_qty = pos.qty + t.qty
        if pos.qty > 0:
            pos.avg_price = ((pos.avg_price * pos.qty) + (t.price * t.qty)) / new_qty
        else:
            pos.avg_price = t.price

        pos.qty = new_qty
        pos.total_buy_amt += (t.price * t.qty)
        pos.cumulative_fees += t.fee
        pos.last_buy_price = t.price
        pos.last_buy_date = trade_date
        pos.total_trades += 1

        # FIFO 큐
        pos.buy_history.append({
            "strategy": strategy,
            "qty": t.qty,
            "price": t.price,
            "fee": t.fee,
            "time": t.time,
            "date": trade_date
        })

        # 데일리 전략
        daily.buy_notional += (t.price * t.qty)
        daily.buy_qty += t.qty
        daily.fees += t.fee

    def _apply_sell(
        self,
        pos: SymbolPosition,
        daily: StrategyState,
        t: TradeRow,
        trade_date: str
    ) -> None:
        """매도 처리 (FIFO)"""
        remaining_qty = t.qty
        total_realized = 0.0
        strategies_involved: Dict[str, Dict[str, Any]] = {}

        # FIFO 소진
        while remaining_qty > 0 and pos.buy_history:
            lot = pos.buy_history[0]
            lot_qty = lot["qty"]
            lot_price = lot["price"]
            lot_strategy = lot["strategy"]

            consume_qty = min(remaining_qty, lot_qty)
            realized = (t.price - lot_price) * consume_qty
            total_realized += realized

            if lot_strategy not in strategies_involved:
                strategies_involved[lot_strategy] = {
                    "realized": 0.0,
                    "qty": 0,
                    "buy_date": lot["date"]
                }
            strategies_involved[lot_strategy]["realized"] += realized
            strategies_involved[lot_strategy]["qty"] += consume_qty

            if consume_qty >= lot_qty:
                pos.buy_history.pop(0)
            else:
                pos.buy_history[0]["qty"] = lot_qty - consume_qty

            remaining_qty -= consume_qty

        # 포지션 갱신
        pos.cumulative_realized_gross += total_realized
        pos.cumulative_fees += t.fee
        pos.cumulative_realized_net = pos.cumulative_realized_gross - pos.cumulative_fees

        pos.qty = max(0, pos.qty - t.qty)
        if pos.qty == 0:
            pos.avg_price = 0.0

        pos.last_sell_price = t.price
        pos.last_sell_date = trade_date
        pos.total_trades += 1
        if total_realized > 0:
            pos.total_wins += 1

        # 데일리 전략 (오늘 매도만 반영)
        for strat_name, info in strategies_involved.items():
            if strat_name == daily.name:
                pnl = info["realized"]
                is_win = pnl > 0

                daily.realized_pnl_gross += pnl
                fee_portion = t.fee * (pnl / total_realized if total_realized != 0 else 0)
                daily.fees += fee_portion
                daily.realized_pnl_net = daily.realized_pnl_gross - daily.fees
                daily.sells += 1

                if is_win:
                    daily.wins += 1
                    daily.consecutive_losses = 0
                else:
                    daily.consecutive_losses += 1
                    daily.max_consecutive_losses = max(
                        daily.max_consecutive_losses,
                        daily.consecutive_losses
                    )
                    daily.daily_loss += pnl  # pnl이 음수일 때 누적

                self._update_time_slot(daily, t.time, pnl, is_win)

    def _update_time_slot(
        self,
        daily: StrategyState,
        time_str: str,
        pnl: float,
        is_win: bool
    ) -> None:
        """시간대별 통계"""
        slot = self._get_time_slot(time_str)

        if slot == "morning":
            stats = daily.morning_stats
        elif slot == "afternoon":
            stats = daily.afternoon_stats
        else:
            return

        stats.realized_pnl += pnl
        stats.trades += 1
        if is_win:
            stats.wins += 1
        stats.win_rate = (stats.wins / stats.trades * 100.0) if stats.trades > 0 else 0.0

    def _get_time_slot(self, time_str: str) -> Optional[str]:
        """시간대 분류"""
        try:
            if 'T' in time_str:
                dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                t = dt.time()
            else:
                t = datetime.strptime(
                    time_str.split()[0] if ' ' in time_str else time_str,
                    "%H:%M:%S"
                ).time()

            if dt_time(9, 0) <= t < dt_time(12, 0):
                return "morning"
            elif dt_time(12, 0) <= t < dt_time(15, 30):
                return "afternoon"
        except Exception:
            pass
        return None

    def _extract_date(self, time_str: str) -> str:
        """날짜 추출"""
        try:
            if 'T' in time_str:
                dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                return dt.date().isoformat()
        except Exception:
            pass
        return get_today_str()

    def _recalc_daily_metrics(self, daily: StrategyState) -> None:
        """지표 재계산"""
        daily.win_rate = (daily.wins / daily.sells * 100.0) if daily.sells > 0 else 0.0

        if daily.buy_notional > 0:
            daily.roi_pct = (daily.realized_pnl_net / daily.buy_notional) * 100.0

        if daily.wins > 0:
            total_win_pnl = max(0, daily.realized_pnl_gross)
            daily.avg_win = total_win_pnl / daily.wins

        loses = daily.sells - daily.wins
        if loses > 0:
            total_loss_pnl = min(0, daily.realized_pnl_gross)
            daily.avg_loss = total_loss_pnl / loses

    def _check_alerts(self, strategy_key: str, daily: StrategyState) -> None:
        """알림 체크"""
        if not self._alert_config.on_alert:
            return

        alerts = []

        # Profit Factor (간단 추정치)
        if self._alert_config.enable_pf_alert and daily.sells >= 5:
            total_wins_amt = daily.avg_win * daily.wins if daily.wins > 0 else 0.0
            total_losses_amt = abs(daily.avg_loss) * (daily.sells - daily.wins) if (daily.sells - daily.wins) > 0 else 0.0
            pf = (total_wins_amt / total_losses_amt) if total_losses_amt > 0 else (999.0 if total_wins_amt > 0 else 0.0)
            if pf < 1.0:
                alerts.append({
                    "type": "PROFIT_FACTOR_LOW",
                    "message": f"전략 '{strategy_key}': PF {pf:.2f}",
                    "data": {"pf": pf}
                })

        # 연속 손실
        if self._alert_config.enable_consecutive_loss_alert:
            if daily.consecutive_losses >= self._alert_config.consecutive_loss_threshold:
                alerts.append({
                    "type": "CONSECUTIVE_LOSSES",
                    "message": f"전략 '{strategy_key}': 연속 {daily.consecutive_losses}회 손실",
                    "data": {"losses": daily.consecutive_losses}
                })

        # 일일 손실 한도
        if self._alert_config.enable_daily_loss_alert:
            if daily.daily_loss <= self._alert_config.daily_loss_limit:
                alerts.append({
                    "type": "DAILY_LOSS_LIMIT",
                    "message": f"전략 '{strategy_key}': 일일 손실 {daily.daily_loss:,.0f}원",
                    "data": {"loss": daily.daily_loss}
                })

        for alert in alerts:
            try:
                self._alert_config.on_alert(alert["type"], alert["message"], alert["data"])
            except Exception:
                logger.exception("on_alert callback failed")

    def _finalize_daily(self) -> None:
        """하루 종료 저장"""
        payload = self._build_daily_payload()
        self._atomic_write(self.daily_path, payload)
        logger.info(f"[Store] Finalized daily: {self.daily_path.name}")
        self.store_updated.emit()

    def _save_both(self) -> None:
        """양쪽 저장 + UI emit"""
        self._atomic_write(self.daily_path, self._build_daily_payload())
        self._atomic_write(self.cumulative_path, self._build_cumulative_payload())
        self.store_updated.emit()

    def _build_daily_payload(self) -> Dict[str, Any]:
        """데일리 JSON"""
        strategies = {}
        for name, st in self._daily_strategies.items():
            strategies[name] = {
                "buy_notional": round(st.buy_notional, 2),
                "buy_qty": st.buy_qty,
                "fees": round(st.fees, 2),
                "realized_pnl_gross": round(st.realized_pnl_gross, 2),
                "realized_pnl_net": round(st.realized_pnl_net, 2),
                "wins": st.wins,
                "sells": st.sells,
                "win_rate": round(st.win_rate, 2),
                "roi_pct": round(st.roi_pct, 2),
                "avg_win": round(st.avg_win, 2),
                "avg_loss": round(st.avg_loss, 2),
                "consecutive_losses": st.consecutive_losses,
                "max_consecutive_losses": st.max_consecutive_losses,
                "daily_loss": round(st.daily_loss, 2),
                "morning": {
                    "realized_pnl": round(st.morning_stats.realized_pnl, 2),
                    "trades": st.morning_stats.trades,
                    "wins": st.morning_stats.wins,
                    "win_rate": round(st.morning_stats.win_rate, 2)
                },
                "afternoon": {
                    "realized_pnl": round(st.afternoon_stats.realized_pnl, 2),
                    "trades": st.afternoon_stats.trades,
                    "wins": st.afternoon_stats.wins,
                    "win_rate": round(st.afternoon_stats.win_rate, 2)
                }
            }

        total_pnl = sum(s.realized_pnl_net for s in self._daily_strategies.values())
        total_trades = sum(s.sells for s in self._daily_strategies.values())
        total_wins = sum(s.wins for s in self._daily_strategies.values())
        morning_pnl = sum(s.morning_stats.realized_pnl for s in self._daily_strategies.values())
        afternoon_pnl = sum(s.afternoon_stats.realized_pnl for s in self._daily_strategies.values())

        return {
            "date": self._current_date,
            "strategies": strategies,
            "summary": {
                "realized_pnl_gross": round(sum(s.realized_pnl_gross for s in self._daily_strategies.values()), 2),
                "fees": round(sum(s.fees for s in self._daily_strategies.values()), 2),
                "realized_pnl_net": round(total_pnl, 2),
                "trades": float(total_trades),
                "win_rate": (total_wins / total_trades * 100.0) if total_trades > 0 else 0.0,
                "morning_pnl": round(morning_pnl, 2),
                "afternoon_pnl": round(afternoon_pnl, 2)
            },
            "symbols": {}  # (옵션) UI 호환성 슬롯
        }

    def _build_cumulative_payload(self) -> Dict[str, Any]:
        """누적 JSON"""
        symbols = {}
        for code, pos in self._positions.items():
            symbols[code] = {
                "qty": pos.qty,
                "avg_price": round(pos.avg_price, 2),
                "total_buy_amt": round(pos.total_buy_amt, 2),
                "cumulative_realized_gross": round(pos.cumulative_realized_gross, 2),
                "cumulative_realized_net": round(pos.cumulative_realized_net, 2),
                "cumulative_fees": round(pos.cumulative_fees, 2),
                "last_buy_price": round(pos.last_buy_price, 2),
                "last_buy_date": pos.last_buy_date,
                "last_sell_price": round(pos.last_sell_price, 2),
                "last_sell_date": pos.last_sell_date,
                "total_trades": pos.total_trades,
                "total_wins": pos.total_wins,
                "buy_history": pos.buy_history[-100:]  # 최근 100개만
            }

        return {
            "last_updated": datetime.now(KST).isoformat(),
            "symbols": symbols
        }

    def _load_cumulative(self) -> None:
        """누적 로드"""
        if not self.cumulative_path.exists():
            logger.info("[Store] No cumulative file")
            return

        try:
            data = json.loads(self.cumulative_path.read_text(encoding="utf-8"))
            symbols = data.get("symbols", {})

            for code, s in symbols.items():
                self._positions[code] = SymbolPosition(
                    code=code,
                    qty=s.get("qty", 0),
                    avg_price=s.get("avg_price", 0.0),
                    total_buy_amt=s.get("total_buy_amt", 0.0),
                    buy_history=s.get("buy_history", []),
                    cumulative_realized_gross=s.get("cumulative_realized_gross", 0.0),
                    cumulative_realized_net=s.get("cumulative_realized_net", 0.0),
                    cumulative_fees=s.get("cumulative_fees", 0.0),
                    last_buy_price=s.get("last_buy_price", 0.0),
                    last_buy_date=s.get("last_buy_date", ""),
                    last_sell_price=s.get("last_sell_price", 0.0),
                    last_sell_date=s.get("last_sell_date", ""),
                    total_trades=s.get("total_trades", 0),
                    total_wins=s.get("total_wins", 0)
                )

            logger.info(f"[Store] Loaded {len(self._positions)} symbols")
        except Exception:
            logger.exception("[Store] Failed to load cumulative")

    def _load_or_init_daily(self) -> None:
        """데일리 로드"""
        if not self.daily_path.exists():
            logger.info(f"[Store] No daily file for {self._current_date}")
            return

        try:
            data = json.loads(self.daily_path.read_text(encoding="utf-8"))
            strategies = data.get("strategies", {})

            for name, s in strategies.items():
                morning = s.get("morning", {})
                afternoon = s.get("afternoon", {})

                self._daily_strategies[name] = StrategyState(
                    name=name,
                    date=self._current_date,
                    buy_notional=s.get("buy_notional", 0.0),
                    buy_qty=s.get("buy_qty", 0),
                    fees=s.get("fees", 0.0),
                    realized_pnl_gross=s.get("realized_pnl_gross", 0.0),
                    realized_pnl_net=s.get("realized_pnl_net", 0.0),
                    wins=s.get("wins", 0),
                    sells=s.get("sells", 0),
                    win_rate=s.get("win_rate", 0.0),
                    roi_pct=s.get("roi_pct", 0.0),
                    avg_win=s.get("avg_win", 0.0),
                    avg_loss=s.get("avg_loss", 0.0),
                    morning_stats=TimeSlotStats(
                        realized_pnl=morning.get("realized_pnl", 0.0),
                        trades=morning.get("trades", 0),
                        wins=morning.get("wins", 0),
                        win_rate=morning.get("win_rate", 0.0)
                    ),
                    afternoon_stats=TimeSlotStats(
                        realized_pnl=afternoon.get("realized_pnl", 0.0),
                        trades=afternoon.get("trades", 0),
                        wins=afternoon.get("wins", 0),
                        win_rate=afternoon.get("win_rate", 0.0)
                    ),
                    consecutive_losses=s.get("consecutive_losses", 0),
                    max_consecutive_losses=s.get("max_consecutive_losses", 0),
                    daily_loss=s.get("daily_loss", 0.0)
                )

            logger.info(f"[Store] Loaded {len(self._daily_strategies)} strategies")
        except Exception:
            logger.exception("[Store] Failed to load daily")

    def _atomic_write(self, path: Path, payload: Dict[str, Any]) -> None:
        """원자적 쓰기"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(str(tmp), str(path))
            logger.debug(f"[Store] Wrote {path.name}")
        except Exception:
            logger.exception(f"[Store] Failed to write {path}")
