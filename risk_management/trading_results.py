# risk_management/trading_results.py
from __future__ import annotations

import json
import os
import threading
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional, Any, List

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
from utils.result_paths import path_today

@dataclass
class SymbolState:
    qty: int = 0
    avg_price: float = 0.0
    fees: float = 0.0
    realized_pnl_gross: float = 0.0
    realized_pnl_net: float = 0.0
    last_buy_price: float = 0.0
    last_sell_price: float = 0.0
    trades: int = 0
    wins: int = 0  # 매도 체결 시 realized>0 이면 win

@dataclass
class StrategyState:
    # 전략 단위 집계
    buy_notional: float = 0.0        # ∑(BUY price*qty) — ROI 분모로 사용
    fees: float = 0.0
    realized_pnl_gross: float = 0.0
    realized_pnl_net: float = 0.0
    wins: int = 0                    # 매도에서 realized>0
    sells: int = 0                   # 승률 분모(매도 건수)
    # 파생 KPI (스냅샷시에 계산해 저장)
    win_rate: float = 0.0
    roi_pct: float = 0.0

@dataclass
class TradeRow:
    time: str
    side: str         # "buy" | "sell"
    symbol: str
    qty: int
    price: float
    fee: float = 0.0
    status: str = ""
    strategy: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None

class TradingResultStore:
    """
    CSV로부터 들어오는 체결(TradeRow)을 받아:
      - 종목별 qty/avg/fees/realized 갱신
      - 전략별 집계(실현손익, 승률, ROI%) 갱신
      - 포트폴리오 summary 갱신
      - trading_result.json 을 원자적으로 저장
    *개선점*
      - RLock 사용 (재진입 안전)
      - 스냅샷 생성은 락 안, 파일 I/O는 락 밖 (UI 블로킹 방지)
    """
    def __init__(self, json_path: Optional[str | Path] = None) -> None:
        self._json_path: Path = Path(json_path) if json_path is not None else path_today()
        self._symbols: Dict[str, SymbolState] = {}
        self._strategies: Dict[str, StrategyState] = {}
        self._summary: Dict[str, Any] = {
            "realized_pnl_gross": 0.0,
            "fees": 0.0,
            "realized_pnl_net": 0.0,
            "trades": 0.0,
            "win_rate": 0.0,  # 포트폴리오 레벨 승률(모든 전략 합산, 매도 기준)
        }
        # ★ 재진입 안전 락
        self._lock = threading.RLock()

        # 기존 결과 로드(있으면)
        try:
            if self._json_path.exists():
                self._load_existing()
                logger.info("[store] loaded existing state from %s", self._json_path)
        except Exception:
            logger.exception("[store] failed to load existing json; continue with empty state")

    # ---------- Public API ----------

    def apply_trade(self, t: TradeRow) -> None:
        """BUY/SELL 공통 적용 후 저장 (파일 I/O는 락 밖)"""
        if t.qty <= 0 or t.price <= 0:
            return

        strategy_key = (t.strategy or "default").strip() or "default"

        with self._lock:
            st = self._symbols.setdefault(t.symbol, SymbolState())
            sg = self._strategies.setdefault(strategy_key, StrategyState())

            if t.side == "buy":
                self._apply_buy_symbol(st, t.qty, t.price, t.fee)
                self._apply_buy_strategy(sg, t.qty, t.price, t.fee)
            elif t.side == "sell":
                realized = self._apply_sell_symbol(st, t.qty, t.price, t.fee)
                self._apply_sell_strategy(sg, t.qty, t.price, t.fee, realized)
            else:
                return

            self._rebuild_summary_locked()
            payload = self._snapshot_locked()

        # 락 해제 후 디스크 기록
        self._atomic_write(payload)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def reset(self) -> None:
        """전수 재계산 전용: 내부 상태 초기화(파일은 즉시 삭제하지 않음)."""
        with self._lock:
            self._reset_locked()

    def rebuild_from_trades(self, trades: List[TradeRow]) -> None:
        """
        모든 체결을 시간순으로 재적용하여 상태 재구성 후 저장.
        파일 I/O는 락 밖에서 실행.
        """
        # 빠른 경로: 0건이면 빈 스냅샷 바로 기록
        if not trades:
            logger.info("[store] rebuild_from_trades: empty trades → write empty snapshot")
            payload = self._empty_snapshot()
            self._atomic_write(payload)
            return

        with self._lock:
            self._reset_locked()

            def _key(t: TradeRow):
                # time이 ISO 문자열이면 정렬 안정적, 없으면 그대로
                return (t.time or "",)

            applied = 0
            for t in sorted(trades, key=_key):
                if t.qty <= 0 or t.price <= 0:
                    continue
                st = self._symbols.setdefault(t.symbol, SymbolState())
                sg = self._strategies.setdefault((t.strategy or "default").strip() or "default", StrategyState())
                if t.side == "buy":
                    self._apply_buy_symbol(st, t.qty, t.price, t.fee)
                    self._apply_buy_strategy(sg, t.qty, t.price, t.fee)
                elif t.side == "sell":
                    realized = self._apply_sell_symbol(st, t.qty, t.price, t.fee)
                    self._apply_sell_strategy(sg, t.qty, t.price, t.fee, realized)
                applied += 1

            logger.info("[store] rebuild_from_trades: applied=%d", applied)

            self._rebuild_summary_locked()
            payload = self._snapshot_locked()

        # 락 해제 후 디스크 기록
        self._atomic_write(payload)

    # ---------- Internals: per-symbol ----------

    def _apply_buy_symbol(self, s: SymbolState, qty: int, price: float, fee: float) -> None:
        new_qty = s.qty + qty
        if s.qty > 0 and new_qty > 0:
            s.avg_price = ((s.avg_price * s.qty) + (price * qty)) / new_qty
        else:
            s.avg_price = float(price)
        s.qty = new_qty
        s.fees += float(fee or 0.0)
        s.last_buy_price = float(price)
        s.trades += 1

    def _apply_sell_symbol(self, s: SymbolState, qty: int, price: float, fee: float) -> float:
        # 보유수량보다 큰 매도 요청은 보유수량까지만 실현
        sell_qty = min(qty, s.qty) if s.qty > 0 else 0
        realized = (price - s.avg_price) * sell_qty if (sell_qty > 0 and s.avg_price > 0) else 0.0

        s.realized_pnl_gross += realized
        s.fees += float(fee or 0.0)
        s.realized_pnl_net = s.realized_pnl_gross - s.fees

        s.qty = max(0, s.qty - qty)  # 요청 수량만큼 차감(음수 방지)
        if s.qty == 0:
            s.avg_price = 0.0

        s.last_sell_price = float(price)
        s.trades += 1
        if realized > 0:
            s.wins += 1
        return realized

    # ---------- Internals: per-strategy ----------

    def _apply_buy_strategy(self, g: StrategyState, qty: int, price: float, fee: float) -> None:
        g.buy_notional += (price * qty)
        g.fees += float(fee or 0.0)

    def _apply_sell_strategy(self, g: StrategyState, qty: int, price: float, fee: float, realized: float) -> None:
        g.realized_pnl_gross += realized
        g.fees += float(fee or 0.0)
        g.realized_pnl_net = g.realized_pnl_gross - g.fees
        g.sells += 1
        if realized > 0:
            g.wins += 1

    # ---------- Summary/Snapshot ----------

    def _rebuild_summary_locked(self) -> None:
        # 전략 파생 KPI
        for g in self._strategies.values():
            g.win_rate = (g.wins / g.sells * 100.0) if g.sells > 0 else 0.0
            denom = g.buy_notional if g.buy_notional > 0 else 0.0
            g.roi_pct = (g.realized_pnl_net / denom * 100.0) if denom > 0 else 0.0

        gross = sum(s.realized_pnl_gross for s in self._symbols.values())
        fees = sum(s.fees for s in self._symbols.values())
        net = gross - fees

        total_sells = sum(g.sells for g in self._strategies.values())
        total_wins = sum(g.wins for g in self._strategies.values())
        win_rate = (total_wins / total_sells * 100.0) if total_sells > 0 else 0.0
        trades = sum(s.trades for s in self._symbols.values())

        self._summary.update({
            "realized_pnl_gross": float(gross),
            "fees": float(fees),
            "realized_pnl_net": float(net),
            "trades": float(trades),
            "win_rate": float(win_rate),
        })

    def _snapshot_locked(self) -> Dict[str, Any]:
        return {
            "symbols": {sym: asdict(st) for sym, st in self._symbols.items()},
            "summary": dict(self._summary),
            "strategies": {
                name: {
                    "buy_notional": round(st.buy_notional, 6),
                    "fees": round(st.fees, 6),
                    "realized_pnl_gross": round(st.realized_pnl_gross, 6),
                    "realized_pnl_net": round(st.realized_pnl_net, 6),
                    "wins": int(st.wins),
                    "sells": int(st.sells),
                    "win_rate": round(st.win_rate, 4),
                    "roi_pct": round(st.roi_pct, 4),
                }
                for name, st in self._strategies.items()
            }
        }

    def _empty_snapshot(self) -> Dict[str, Any]:
        return {
            "symbols": {},
            "summary": {
                "realized_pnl_gross": 0.0,
                "fees": 0.0,
                "realized_pnl_net": 0.0,
                "trades": 0.0,
                "win_rate": 0.0,
            },
            "strategies": {}
        }

    # ---------- Reset (internal) ----------

    def _reset_locked(self) -> None:
        self._symbols.clear()
        self._strategies.clear()
        self._summary.update({
            "realized_pnl_gross": 0.0,
            "fees": 0.0,
            "realized_pnl_net": 0.0,
            "trades": 0.0,
            "win_rate": 0.0,
        })

    # ---------- Flush (I/O out of lock) ----------

    def _atomic_write(self, payload: Dict[str, Any]) -> None:
        """tmp→replace 원자적 저장 (락 밖에서 호출)"""
        try:
            self._json_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._json_path.with_suffix(self._json_path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(str(tmp), str(self._json_path))
            logger.debug("[store] wrote json → %s", self._json_path)
        except Exception:
            logger.exception("[store] failed to write json")

    def _load_existing(self) -> None:
        data = json.loads(self._json_path.read_text(encoding="utf-8"))
        # symbols
        symbols = data.get("symbols") or {}
        for sym, d in symbols.items():
            self._symbols[sym] = SymbolState(**d)
        # strategies
        strategies = data.get("strategies") or {}
        for name, d in strategies.items():
            self._strategies[name] = StrategyState(
                buy_notional=float(d.get("buy_notional", 0.0)),
                fees=float(d.get("fees, 0.0")) if isinstance(d.get("fees, 0.0"), (int, float)) else float(d.get("fees", 0.0)),
                realized_pnl_gross=float(d.get("realized_pnl_gross", 0.0)),
                realized_pnl_net=float(d.get("realized_pnl_net", 0.0)),
                wins=int(d.get("wins", 0)),
                sells=int(d.get("sells", 0)),
            )
        # summary
        summary = data.get("summary") or {}
        self._summary.update(summary)
