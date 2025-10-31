"""
risk_management/trading_results.py
갱신형(JSON 기반) 트레이딩 결과 관리
- trading_results_YYYY-MM-DD.json : 일별 누적 상태 (overwrite 저장)
- trading_results.json            : 전체 누적 상태 (overwrite 저장)
"""
from __future__ import annotations

import json
import csv
import threading
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Any, List, Callable
from datetime import datetime, timezone, timedelta

from PySide6.QtCore import QObject, Signal

# ---------------------------------------------------------------------
# 기본 설정
# ---------------------------------------------------------------------
logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    )

KST = timezone(timedelta(hours=9))
def now_iso() -> str:
    return datetime.now(KST).isoformat()
def get_today_str() -> str:
    return datetime.now(KST).date().isoformat()

# ---------------------------------------------------------------------
# 데이터 모델
# ---------------------------------------------------------------------
@dataclass
class TradeRow:
    time: str
    side: str
    symbol: str
    qty: int
    price: float
    fee: float = 0.0
    status: str = "filled"
    strategy: Optional[str] = "default"
    meta: Optional[Dict[str, Any]] = None


@dataclass
class SymbolPosition:
    code: str
    qty: int = 0
    avg_price: float = 0.0
    total_buy_amt: float = 0.0
    cumulative_realized: float = 0.0
    total_cost_sold: float = 0.0
    realized_roi_pct: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    buy_history: List[Dict[str, Any]] = field(default_factory=list)

# ---------------------------------------------------------------------
# 본체
# ---------------------------------------------------------------------
class TradingResultStore(QObject):
    """CSV 기반 → JSON 결과 누적 갱신 (overwrite 방식)"""
    store_updated = Signal()

    def __init__(self, json_path: Optional[str | Path] = None, *, filename_prefix="trading_results"):
        super().__init__()
        base_dir = Path(json_path).parent if json_path else Path("logs/results")
        base_dir.mkdir(parents=True, exist_ok=True)
        self.base_dir = base_dir
        self._filename_prefix = filename_prefix
        self._current_date = get_today_str()

        self.daily_json = base_dir / f"{filename_prefix}_{self._current_date}.json"
        self.cumulative_json = base_dir / f"{filename_prefix}.json"

        self._positions: Dict[str, SymbolPosition] = {}
        self._lock = threading.RLock()

        # 🚀 부트스트랩 실행 (오늘 CSV 존재 시 자동 반영)
        self._bootstrap_from_csv_if_exists()
        self._save_json_state()
        logger.info(f"[TradingResultStore] initialized | daily_json={self.daily_json.name}")

    # --------------------------------------------------
    def _bootstrap_from_csv_if_exists(self):
        """오늘 CSV(order) 로그에서 초기 상태 구성"""
        try:
            trades_dir = Path.cwd() / "logs" / "trades"
            csv_path = trades_dir / f"orders_{self._current_date}.csv"
            if not csv_path.exists():
                logger.info(f"[TradingResultStore] No CSV found for {self._current_date}")
                return

            logger.info(f"[TradingResultStore] Bootstrapping from CSV: {csv_path.name}")
            with csv_path.open(newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        side = str(row.get("action") or "").strip().lower()
                        symbol = str(row.get("stk_cd") or row.get("symbol") or "").strip()
                        qty = int(row.get("qty") or 0)
                        price = float(row.get("price") or 0.0)
                        if not symbol or qty <= 0 or price <= 0:
                            continue
                        self.apply_trade(
                            symbol=symbol,
                            side=side,
                            qty=qty,
                            price=price,
                            strategy=row.get("strategy") or "default",
                            status=row.get("status") or "HTTP_200",
                            meta=row,
                        )
                    except Exception as e:
                        logger.warning(f"[TradingResultStore] skip row: {e}")
            logger.info("[TradingResultStore] CSV bootstrap complete ✅")

        except Exception as e:
            logger.error(f"[TradingResultStore] bootstrap failed: {e}")

    # --------------------------------------------------
    def apply_trade(self, *args, **kwargs):
        """매수/매도 반영 후 JSON overwrite 저장"""
        if len(args) == 1 and isinstance(args[0], TradeRow):
            t: TradeRow = args[0]
        else:
            t = TradeRow(
                time=kwargs.get("time") or now_iso(),
                side=str(kwargs.get("side")).lower(),
                symbol=str(kwargs.get("symbol")),
                qty=int(kwargs.get("qty")),
                price=float(kwargs.get("price")),
                strategy=kwargs.get("strategy") or "default",
                fee=float(kwargs.get("fee", 0.0)),
                meta=kwargs.get("meta"),
            )

        if not t.symbol or t.qty <= 0 or t.price <= 0:
            return

        with self._lock:
            pos = self._positions.setdefault(t.symbol, SymbolPosition(code=t.symbol))
            if t.side == "buy":
                self._apply_buy(pos, t)
            elif t.side == "sell":
                self._apply_sell(pos, t)
            self._save_json_state()

        self.store_updated.emit()

    # --------------------------------------------------
    def _apply_buy(self, pos: SymbolPosition, t: TradeRow):
        """매수 반영"""
        new_qty = pos.qty + t.qty
        if pos.qty > 0:
            pos.avg_price = ((pos.avg_price * pos.qty) + (t.price * t.qty)) / new_qty
        else:
            pos.avg_price = t.price
        pos.qty = new_qty
        pos.total_buy_amt += (t.price * t.qty)
        pos.buy_count += 1
        pos.buy_history.append({"price": t.price, "qty": t.qty, "time": t.time})

    def _apply_sell(self, pos: SymbolPosition, t: TradeRow):
        """매도 반영 + 실현 손익 계산"""
        remaining = t.qty
        total_realized, total_cost = 0.0, 0.0
        while remaining > 0 and pos.buy_history:
            lot = pos.buy_history[0]
            consume = min(remaining, lot["qty"])
            realized = (t.price - lot["price"]) * consume
            total_realized += realized
            total_cost += lot["price"] * consume
            lot["qty"] -= consume
            if lot["qty"] == 0:
                pos.buy_history.pop(0)
            remaining -= consume

        pos.qty = max(0, pos.qty - t.qty)
        pos.cumulative_realized += total_realized
        pos.total_cost_sold += total_cost

        if pos.total_cost_sold > 0:
            pos.realized_roi_pct = (pos.cumulative_realized / pos.total_cost_sold) * 100.0

        pos.sell_count += 1

    # --------------------------------------------------
    def _save_json_state(self):
        """현재 상태를 JSON으로 overwrite 저장"""
        data = {
            "date": self._current_date,
            "time": now_iso(),
            "stocks": {
                code: {
                    "qty": pos.qty,
                    "avg_price": round(pos.avg_price, 2),
                    "realized": round(pos.cumulative_realized, 2),
                    "roi_pct": round(pos.realized_roi_pct, 2),
                    "buy_count": pos.buy_count,
                    "sell_count": pos.sell_count,
                    "total_cost_sold": round(pos.total_cost_sold, 2)
                }
                for code, pos in self._positions.items()
            },
            "summary": {
                "realized_pnl_net": round(sum(p.cumulative_realized for p in self._positions.values()), 2),
                "total_symbols": len(self._positions),
                "trades": sum(p.buy_count + p.sell_count for p in self._positions.values())
            }
        }

        try:
            # 일별 + 누적 동시 갱신
            with self.daily_json.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            with self.cumulative_json.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            logger.debug(f"[TradingResultStore] JSON updated → {self.daily_json.name}")
        except Exception:
            logger.exception("[TradingResultStore] Failed to write JSON state")

    # --------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        """현재 메모리 상태 반환"""
        return {
            "date": self._current_date,
            "positions": {
                code: vars(pos) for code, pos in self._positions.items()
            }
        }

    def reset(self):
        """상태 초기화 (파일 유지)"""
        with self._lock:
            self._positions.clear()
            self._save_json_state()
        self.store_updated.emit()
        logger.info("[TradingResultStore] store reset complete")
