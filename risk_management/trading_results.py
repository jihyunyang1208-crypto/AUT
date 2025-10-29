# risk_management/trading_results.py
"""
이중 저장 구조 트레이딩 결과 관리 (JSONL 이벤트 기반, UI 자동 갱신)
- trading_results_YYYY-MM-DD.jsonl : 데일리 이벤트 로그 (trade/snapshot/daily_close/alert)
- trading_results.jsonl                   : 누적 포지션/요약 스냅샷 이벤트 로그 (snapshot)
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

def now_iso() -> str:
    return datetime.now(KST).isoformat()

def get_today_str() -> str:
    return datetime.now(KST).date().isoformat()

# ==================== 데이터 모델 ====================

@dataclass
class TradeRow:
    """거래 레코드"""
    time: str
    side: str        # "buy" | "sell"
    symbol: str
    qty: int
    price: float
    fee: float = 0.0
    status: str = "filled"
    strategy: Optional[str] = "default"
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
    code: str
    qty: int = 0
    avg_price: float = 0.0
    total_buy_amt: float = 0.0

    buy_history: List[Dict[str, Any]] = field(default_factory=list)

    cumulative_realized_gross: float = 0.0
    cumulative_realized_net: float = 0.0
    cumulative_fees: float = 0.0

    last_buy_price: float = 0.0
    last_buy_date: str = ""     # YYYY-MM-DD
    last_buy_time: str = ""     # ISO8601 (예: 2025-10-27T01:50:00+00:00)

    last_sell_price: float = 0.0
    last_sell_date: str = ""    # YYYY-MM-DD
    last_sell_time: str = ""    # ISO8601

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

# ==================== 메인 스토어 (JSONL 이벤트 로그) ====================

class TradingResultStore(QObject):
    """
    JSONL 기반 이중 저장 구조:
    1) daily_jsonl: trading_results_YYYY-MM-DD.jsonl (전략/거래/일일 이벤트)
    2) cumulative_jsonl: trading_results.jsonl (누적 포지션/요약 스냅샷)
    """

    store_updated = Signal()  # 저장/append 후 UI 자동 리프레시

    def __init__(
        self,
        json_path: Optional[str | Path] = None,
        alert_config: Optional[AlertConfig] = None,
        *,
        filename_prefix: str = "trading_results",   # ✅ 복수형 파일명
    ) -> None:
        super().__init__()  # QObject 초기화

        # 2) 경로 설정 (json_path가 파일이면 폴더만 사용)
        if json_path is None:
            json_path = Path("logs/results/trading_results.jsonl")
        base_dir = Path(json_path).parent
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self._filename_prefix = filename_prefix
        self._current_date = get_today_str()
        self.daily_jsonl = self._daily_path(self._current_date)        # logs/results/trading_results_YYYY-MM-DD.jsonl
        self.cumulative_jsonl = self.base_dir / f"{filename_prefix}.jsonl"  # logs/results/trading_results.jsonl

        # 나머지 상태/부트스트랩 그대로...
        self._positions: Dict[str, SymbolPosition] = {}
        self._daily_strategies: Dict[str, StrategyState] = {}
        self._lock = threading.RLock()
        self._alert_config = alert_config or AlertConfig()
        self._replay_bootstrap()

        logger.info(
            f"[TradingResultStore] initialized | "
            f"daily_jsonl={self.daily_jsonl.name} | cumulative_jsonl={self.cumulative_jsonl.name}"
        )

    # --------------------------------------------------
    # 경로 & 부트스트랩
    # --------------------------------------------------
    def _daily_path(self, date_str: str) -> Path:
        return self.base_dir / f"{self._filename_prefix}_{date_str}.jsonl"

    def _replay_bootstrap(self) -> None:
        """누적 → 오늘자 순으로 JSONL을 리플레이하여 메모리 상태 복구"""
        with self._lock:
            # 1) 누적 스냅샷 로그 리플레이 (positions snapshot 우선)
            if self.cumulative_jsonl.exists():
                self._replay_file(self.cumulative_jsonl, kind="cumulative")

            # 2) 오늘자 데일리 로그 리플레이
            if self.daily_jsonl.exists():
                self._replay_file(self.daily_jsonl, kind="daily")

    def _replay_file(self, path: Path, kind: str) -> None:
        """JSONL 파일 리플레이 (메모리 상태만 갱신, 파일에 다시 쓰지 않음)"""
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        et = str(ev.get("type") or "").lower()
                        if kind == "cumulative" and et == "snapshot":
                            # 누적 스냅샷: positions 복원
                            self._apply_cumulative_snapshot(ev)
                        elif kind == "daily":
                            if et == "trade":
                                self._apply_trade_to_state(self._trade_from_event(ev))
                            elif et == "snapshot":
                                # 데일리 스냅샷: 전략 메트릭스를 최신으로 덮어쓰기(선택)
                                self._apply_daily_snapshot(ev)
                            elif et == "daily_close":
                                # 정보용: 별도 처리 불필요
                                pass
                    except Exception:
                        continue
        except Exception:
            logger.exception(f"[Store] Failed to replay {path.name}")

    def _is_filled(self, t: TradeRow) -> bool:
        # 1) 명시적 체결 상태 우선
        s = str(t.status or "").strip().upper()
        if s in {"FILLED", "EXECUTED", "PARTIALLY_FILLED"}:
            return True

        # 2) 브로커별 응답 규칙(HTTP_200 + resp_code=0 + 오류메시지 없음)
        meta = t.meta or {}
        code = str(meta.get("resp_code", "")).strip()
        msg  = str(meta.get("resp_msg", "")).strip()
        # 흔한 오류 키워드
        err_hints = ("정의되어 있지 않습니다", "오류", "에러", "invalid", "unauthorized", "토큰", "token")

        if s in {"HTTP_200", "OK", "SUCCESS"}:
            if (not code) or code in {"0", "200"}:
                if not any(h.lower() in msg.lower() for h in err_hints):
                    return True

        return False

    # --------------------------------------------------
    # 퍼블릭 유틸
    # --------------------------------------------------

    def set_alert_callback(self, callback: Callable[[str, str, Dict[str, Any]], None]) -> None:
        self._alert_config.on_alert = callback


    # --------------------------------------------------
    # 거래 반영 (오버로드 지원)
    # --------------------------------------------------

    def apply_trade(self, *args, **kwargs) -> None:
        """
        거래 적용 + JSONL append + UI emit

        사용 가능한 서명:
        - apply_trade(TradeRow)
        - apply_trade(symbol, side, qty, price, strategy=None, time=None, fee=0.0, status="")
        """
        # 1) TradeRow 생성
        if len(args) == 1 and isinstance(args[0], TradeRow):
            t: TradeRow = args[0]
        else:
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
                time=kwargs.get("time") or now_iso(),
                side=str(side).lower(),
                symbol=str(symbol),
                qty=int(qty),
                price=float(price),
                fee=float(kwargs.get("fee", 0.0)),
                status=str(kwargs.get("status") or "filled"),
                strategy=(kwargs.get("strategy") or "default"),
                meta=kwargs.get("meta"),
            )

        if t.qty <= 0 or t.price <= 0:
            return

        # 2) 날짜 롤오버 체크
        self.check_date_rollover()

        with self._lock:
            # 0) 주문 이벤트는 항상 기록 (CSV 소스 그대로 보존)
            self._append_jsonl(self.daily_jsonl, {
                "type": "order",              # ← 추가: 주문 원장
                "time": t.time,
                "side": t.side,
                "symbol": t.symbol,
                "qty": t.qty,
                "price": t.price,
                "status": t.status,
                "strategy": t.strategy,
                "meta": t.meta or {}
            })

            # 1) 체결이 아니면 손익/포지션은 건드리지 않음 (CSV 기준 ‘주문 기록’만 남김)
            # if not self._is_filled(t) or t.qty <= 0 or t.price <= 0:
                # 그래도 UI는 변경 사실 알림(주문 로그 테이블 등)
                # self.store_updated.emit()
                # return

            # 2) 체결인 경우에만 기존 로직 그대로
            self._append_jsonl(self.daily_jsonl, {
                "type": "trade",
                "time": t.time,
                "side": t.side,
                "symbol": t.symbol,
                "qty": t.qty,
                "price": t.price,
                "fee": t.fee,
                "status": t.status,
                "strategy": t.strategy,
                "meta": t.meta or {}
            })

            self._apply_trade_to_state(t)
            self._append_daily_snapshot_event()
            self._append_cumulative_snapshot_event()
            self._check_alerts(t.strategy or "default", self._daily_strategies.get(t.strategy or "default"))

        self.store_updated.emit()

    # --------------------------------------------------
    # 스냅샷/조회 API (UI 호환)
    # --------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """(호환) 현재 상태의 데일리 payload"""
        with self._lock:
            return self._build_daily_payload()

    def get_daily_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._build_daily_payload()

    def get_cumulative_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._build_cumulative_payload()

    def get_symbol_position(self, code: str) -> Optional[Dict[str, Any]]:
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
        """메모리 상태 전체 재계산 + 스냅샷 append (파일은 보존)"""
        with self._lock:
            self._positions.clear()
            self._daily_strategies.clear()

            # 날짜별 그룹핑
            trades_by_date: Dict[str, List[TradeRow]] = {}
            for tr in sorted(trades, key=lambda x: x.time):
                date = self._extract_date(tr.time)
                trades_by_date.setdefault(date, []).append(tr)

            # 날짜 순으로 메모리 재구성
            for date in sorted(trades_by_date.keys()):
                self._current_date = date
                self.daily_jsonl = self._daily_path(date)
                for tr in trades_by_date[date]:
                    if tr.qty <= 0 or tr.price <= 0:
                        continue
                    self._apply_trade_to_state(tr)
                # 각 날짜별 스냅샷 append
                self._append_daily_snapshot_event()

            # 누적 스냅샷 append
            self._current_date = get_today_str()
            self.daily_jsonl = self._daily_path(self._current_date)
            self._append_cumulative_snapshot_event()

        self.store_updated.emit()

    def reset(self) -> None:
        """상태 초기화 + 0 스냅샷 append (파일은 truncate하지 않음)"""
        with self._lock:
            self._positions.clear()
            self._daily_strategies.clear()
            self._append_daily_snapshot_event()
            self._append_cumulative_snapshot_event()
        self.store_updated.emit()

    # ==================== 내부 로직 ====================

    # ---- 상태 갱신 (거래 적용) ----
    def _apply_trade_to_state(self, t: TradeRow) -> None:
        strategy_key = (t.strategy or "default").strip() or "default"
        trade_date = self._extract_date(t.time)

        pos = self._positions.setdefault(t.symbol, SymbolPosition(code=t.symbol))
        daily = self._daily_strategies.setdefault(strategy_key, StrategyState(name=strategy_key, date=self._current_date))

        if t.side == "buy":
            self._apply_buy(pos, daily, strategy_key, t, trade_date)
        elif t.side == "sell":
            self._apply_sell(pos, daily, t, trade_date)

        self._recalc_daily_metrics(daily)

    def _apply_buy(
        self,
        pos: SymbolPosition,
        daily: StrategyState,
        strategy: str,
        t: TradeRow,
        trade_date: str
    ) -> None:
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
        pos.last_buy_time  = t.time
        pos.total_trades += 1

        pos.buy_history.append({
            "strategy": strategy,
            "qty": t.qty,
            "price": t.price,
            "fee": t.fee,
            "time": t.time,
            "date": trade_date
        })

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
        remaining_qty = t.qty
        total_realized = 0.0
        strategies_involved: Dict[str, Dict[str, Any]] = {}

        while remaining_qty > 0 and pos.buy_history:
            lot = pos.buy_history[0]
            lot_qty = lot["qty"]
            lot_price = lot["price"]
            lot_strategy = lot["strategy"]

            consume_qty = min(remaining_qty, lot_qty)
            realized = (t.price - lot_price) * consume_qty
            total_realized += realized

            if lot_strategy not in strategies_involved:
                strategies_involved[lot_strategy] = {"realized": 0.0, "qty": 0, "buy_date": lot["date"]}
            strategies_involved[lot_strategy]["realized"] += realized
            strategies_involved[lot_strategy]["qty"] += consume_qty

            if consume_qty >= lot_qty:
                pos.buy_history.pop(0)
            else:
                pos.buy_history[0]["qty"] = lot_qty - consume_qty

            remaining_qty -= consume_qty

        pos.cumulative_realized_gross += total_realized
        pos.cumulative_fees += t.fee
        pos.cumulative_realized_net = pos.cumulative_realized_gross - pos.cumulative_fees

        pos.qty = max(0, pos.qty - t.qty)
        if pos.qty == 0:
            pos.avg_price = 0.0

        pos.last_sell_price = t.price
        pos.last_sell_date = trade_date
        pos.last_sell_time  = t.time
        pos.total_trades += 1
        if total_realized > 0:
            pos.total_wins += 1

        # 데일리 전략(오늘자만)
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
                    daily.max_consecutive_losses = max(daily.max_consecutive_losses, daily.consecutive_losses)
                    daily.daily_loss += pnl  # 음수일 때 누적

                self._update_time_slot(daily, t.time, pnl, is_win)

    # ---- 시간대/지표/알림 ----
    def _update_time_slot(self, daily: StrategyState, time_str: str, pnl: float, is_win: bool) -> None:
        slot = self._get_time_slot(time_str)
        if slot is None:
            return
        stats = daily.morning_stats if slot == "morning" else daily.afternoon_stats
        stats.realized_pnl += pnl
        stats.trades += 1
        if is_win:
            stats.wins += 1
        stats.win_rate = (stats.wins / stats.trades * 100.0) if stats.trades > 0 else 0.0

    def _get_time_slot(self, time_str: str) -> Optional[str]:
        try:
            if 'T' in time_str:
                dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                t = dt.time()
            else:
                t = datetime.strptime(time_str.split()[0] if ' ' in time_str else time_str, "%H:%M:%S").time()
            if dt_time(9, 0) <= t < dt_time(12, 0):
                return "morning"
            elif dt_time(12, 0) <= t < dt_time(15, 30):
                return "afternoon"
        except Exception:
            pass
        return None

    def _extract_date(self, time_str: str) -> str:
        try:
            if 'T' in time_str:
                dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                return dt.date().isoformat()
        except Exception:
            pass
        return get_today_str()

    def _recalc_daily_metrics(self, daily: StrategyState) -> None:
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

    def _check_alerts(self, strategy_key: str, daily: Optional[StrategyState]) -> None:
        if not self._alert_config.on_alert or not daily:
            return
        alerts = []
        # PF 경고 (간이)
        if self._alert_config.enable_pf_alert and daily.sells >= 5:
            total_wins_amt = daily.avg_win * daily.wins if daily.wins > 0 else 0.0
            total_losses_amt = abs(daily.avg_loss) * (daily.sells - daily.wins) if (daily.sells - daily.wins) > 0 else 0.0
            pf = (total_wins_amt / total_losses_amt) if total_losses_amt > 0 else (999.0 if total_wins_amt > 0 else 0.0)
            if pf < 1.0:
                alerts.append({"type": "PROFIT_FACTOR_LOW", "message": f"전략 '{strategy_key}': PF {pf:.2f}", "data": {"pf": pf}})
        # 연속 손실
        if self._alert_config.enable_consecutive_loss_alert and daily.consecutive_losses >= self._alert_config.consecutive_loss_threshold:
            alerts.append({"type": "CONSECUTIVE_LOSSES", "message": f"전략 '{strategy_key}': 연속 {daily.consecutive_losses}회 손실", "data": {"losses": daily.consecutive_losses}})
        # 일일 손실 한도
        if self._alert_config.enable_daily_loss_alert and daily.daily_loss <= self._alert_config.daily_loss_limit:
            alerts.append({"type": "DAILY_LOSS_LIMIT", "message": f"전략 '{strategy_key}': 일일 손실 {daily.daily_loss:,.0f}원", "data": {"loss": daily.daily_loss}})
        # 이벤트 + 콜백
        for alert in len(alerts) and alerts or []:
            try:
                # 1) JSONL 알림 이벤트 (daily)
                self._append_jsonl(self.daily_jsonl, {"type": "alert", "time": now_iso(), **alert})
                # 2) 외부 콜백
                self._alert_config.on_alert(alert["type"], alert["message"], alert["data"])
            except Exception:
                logger.exception("on_alert callback failed")

    # ---- 스냅샷 빌드/append ----
    def _build_daily_payload(self) -> Dict[str, Any]:
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
        traded_symbols = {sym.code for sym in self._positions.values() if sym.total_trades > 0}
        total_trades = len(traded_symbols)
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
            }
        }

    def _build_cumulative_payload(self) -> Dict[str, Any]:
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
                "last_buy_time": pos.last_buy_time,      # ★ 추가

                "last_sell_price": round(pos.last_sell_price, 2),
                "last_sell_date": pos.last_sell_date,
                "last_sell_time": pos.last_sell_time,    # ★ 추가

                "total_trades": pos.total_trades,
                "total_wins": pos.total_wins,
            }
        return {"last_updated": now_iso(), "symbols": symbols}

    # 5) 종목별 합산(rollup) payload 빌더
    def _build_symbols_rollup(self) -> Dict[str, Any]:
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
            }
        return {
            "time": now_iso(),
            "date": self._current_date,
            "symbols": symbols
        }

    # 6) 스냅샷 append 직후, 같은 파일에 rollup 이벤트도 추가로 기록
    def _append_daily_snapshot_event(self) -> None:
        payload = self._build_daily_payload()
        ev = {"type": "snapshot", "time": now_iso(), **payload}
        self._append_jsonl(self.daily_jsonl, ev)

        # ✅ 같은 일별 JSONL에 rollup 이벤트 추가
        try:
            roll = self._build_symbols_rollup()
            self._append_jsonl(self.daily_jsonl, {"type": "rollup", **roll})
        except Exception:
            logger.exception("[Store] write daily rollup failed")

    def _append_cumulative_snapshot_event(self) -> None:
        payload = self._build_cumulative_payload()
        ev = {"type": "snapshot", "time": now_iso(), **payload}
        self._append_jsonl(self.cumulative_jsonl, ev)

        # ✅ 같은 누적 JSONL에 rollup 이벤트 추가
        try:
            roll = self._build_symbols_rollup()
            self._append_jsonl(self.cumulative_jsonl, {"type": "rollup", **roll})
        except Exception:
            logger.exception("[Store] write cumulative rollup failed")

    def _append_daily_close(self, date_str: str) -> None:
        """전일 파일에 일마감 이벤트 기록"""
        try:
            daily_path = self._daily_path(date_str)
            summary = self._build_daily_payload()["summary"] if date_str == self._current_date else {}
            self._append_jsonl(daily_path, {
                "type": "daily_close",
                "time": now_iso(),
                "date": date_str,
                "summary": summary
            })
        except Exception:
            logger.exception("[Store] Failed to append daily_close")

    # ---- 스냅샷 적용 (리플레이 시) ----
    def _apply_cumulative_snapshot(self, ev: Dict[str, Any]) -> None:
        symbols = ev.get("symbols", {}) or {}
        self._positions.clear()
        for code, s in symbols.items():
            self._positions[code] = SymbolPosition(
                code=code,
                qty=int(s.get("qty", 0)),
                avg_price=float(s.get("avg_price", 0.0)),
                total_buy_amt=float(s.get("total_buy_amt", 0.0)),
                buy_history=[],
                cumulative_realized_gross=float(s.get("cumulative_realized_gross", 0.0)),
                cumulative_realized_net=float(s.get("cumulative_realized_net", 0.0)),
                cumulative_fees=float(s.get("cumulative_fees", 0.0)),
                last_buy_price=float(s.get("last_buy_price", 0.0)),
                last_buy_date=str(s.get("last_buy_date", "")),
                last_buy_time=str(s.get("last_buy_time", "")),      # ★ 추가
                last_sell_price=float(s.get("last_sell_price", 0.0)),
                last_sell_date=str(s.get("last_sell_date", "")),
                last_sell_time=str(s.get("last_sell_time", "")),    # ★ 추가
                total_trades=int(s.get("total_trades", 0)),
                total_wins=int(s.get("total_wins", 0)),
            )

    def _apply_daily_snapshot(self, ev: Dict[str, Any]) -> None:
        """daily snapshot 이벤트로 전략 메트릭스 최신화(선택적 사용)"""
        strategies = ev.get("strategies", {}) or {}
        self._daily_strategies.clear()
        for name, s in strategies.items():
            morning = s.get("morning", {}) or {}
            afternoon = s.get("afternoon", {}) or {}
            self._daily_strategies[name] = StrategyState(
                name=name,
                date=str(ev.get("date") or self._current_date),
                buy_notional=float(s.get("buy_notional", 0.0)),
                buy_qty=int(s.get("buy_qty", 0)),
                fees=float(s.get("fees", 0.0)),
                realized_pnl_gross=float(s.get("realized_pnl_gross", 0.0)),
                realized_pnl_net=float(s.get("realized_pnl_net", 0.0)),
                wins=int(s.get("wins", 0)),
                sells=int(s.get("sells", 0)),
                win_rate=float(s.get("win_rate", 0.0)),
                roi_pct=float(s.get("roi_pct", 0.0)),
                avg_win=float(s.get("avg_win", 0.0)),
                avg_loss=float(s.get("avg_loss", 0.0)),
                morning_stats=TimeSlotStats(
                    realized_pnl=float(morning.get("realized_pnl", 0.0)),
                    trades=int(morning.get("trades", 0)),
                    wins=int(morning.get("wins", 0)),
                    win_rate=float(morning.get("win_rate", 0.0))
                ),
                afternoon_stats=TimeSlotStats(
                    realized_pnl=float(afternoon.get("realized_pnl", 0.0)),
                    trades=int(afternoon.get("trades", 0)),
                    wins=int(afternoon.get("wins", 0)),
                    win_rate=float(afternoon.get("win_rate", 0.0))
                ),
                consecutive_losses=int(s.get("consecutive_losses", 0)),
                max_consecutive_losses=int(s.get("max_consecutive_losses", 0)),
                daily_loss=float(s.get("daily_loss", 0.0))
            )

    # ---- 파일 IO ----
    # 날짜 롤오버 시 일별 파일 경로 갱신
    def check_date_rollover(self) -> bool:
        today = get_today_str()
        if today != self._current_date:
            with self._lock:
                prev_date = self._current_date
                logger.info(f"[Store] Date rollover: {prev_date} -> {today}")
                self._append_daily_close(prev_date)
                self._current_date = today
                self.daily_jsonl = self._daily_path(today)  # e.g., trading_results_YYYY-MM-DD.jsonl
                self._daily_strategies.clear()
            return True
        return False

    def _append_jsonl(self, path: Path, obj: Dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception(f"[Store] Failed to append JSONL: {path.name}")

    # ---- 헬퍼 ----
    @staticmethod
    def _trade_from_event(ev: Dict[str, Any]) -> TradeRow:
        # 1) 기본 필드 꺼내기
        raw_side = str(ev.get("side") or "").strip().lower()
        action = str(ev.get("action") or "").strip().lower()   # ← CSV/주문로그 호환
        qty = int(ev.get("qty") or 0)

        # 2) action → side 매핑 (우선순위: action > side > qty 부호 추정)
        if action in {"buy", "sell"}:
            raw_side = action
        elif not raw_side:
            if qty < 0:
                raw_side = "sell"
            elif qty > 0:
                raw_side = "buy"

        # 3) side 동의어 정규화
        if raw_side in {"exit", "sell_short", "close"}:
            raw_side = "sell"
        elif raw_side in {"enter", "open", "buy_long"}:
            raw_side = "buy"

        # 4) qty 정규화 (SELL인데 음수로 오는 소스도 대비)
        if qty < 0 and raw_side != "sell":
            raw_side = "sell"
            qty = abs(qty)

        # 5) 시간 키 호환: ts → time
        time_str = ev.get("time") or ev.get("ts") or now_iso()

        # 6) 심볼 키 호환: symbol / stk_cd
        symbol = str(ev.get("symbol") or ev.get("stk_cd") or "")

        return TradeRow(
            time=time_str,
            side=raw_side or "buy",   # 최후 fallback (비정상 입력 보호)
            symbol=symbol,
            qty=qty,
            price=float(ev.get("price") or 0.0),
            fee=float(ev.get("fee") or 0.0),
            status=str(ev.get("status") or "filled"),
            strategy=str(ev.get("strategy") or "default"),
            meta=ev,
        )
