from __future__ import annotations
import csv
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, Signal

from utils.result_paths import today_str
from risk_management.trading_results import TradingResultStore

logger = logging.getLogger(__name__)


# =========================================================
# Watcher 설정 구조체
# =========================================================
@dataclass
class WatcherConfig:
    base_dir: Path
    subdir: str = "trades"
    file_pattern: str = "orders_{date}.csv"
    poll_ms: int = 700
    bootstrap_if_missing: bool = True

    def resolve_today_path(self) -> Path:
        """오늘자 orders CSV 경로 계산"""
        f = self.file_pattern.format(date=today_str())
        full_path = self.base_dir / self.subdir / f
        full_path.parent.mkdir(parents=True, exist_ok=True)
        if self.bootstrap_if_missing and not full_path.exists():
            full_path.write_text("ts,strategy,side,symbol,qty,price\n", encoding="utf-8")
        return full_path


# =========================================================
# Orders CSV Watcher
# =========================================================
class OrdersCSVWatcher(QObject):
    """orders_YYYY-MM-DD.csv 감시 → TradingResultStore 자동 반영"""

    new_trade_detected = Signal(dict)
    watcher_stopped = Signal()

    def __init__(self, store: TradingResultStore, config: WatcherConfig, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.store = store
        self.config = config
        self._stop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_offset = 0
        self.csv_path = self.config.resolve_today_path()

    # =========================================================
    # 시작 / 종료
    # =========================================================
    def start(self) -> None:
        """백그라운드 감시 스레드 시작"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run_loop, name="OrdersCSVWatcher", daemon=True)
        self._thread.start()
        logger.info(f"OrdersCSVWatcher started → {self.csv_path}")

    def stop(self) -> None:
        """감시 종료"""
        self._stop_flag.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self.watcher_stopped.emit()
        logger.info("OrdersCSVWatcher stopped")

    # =========================================================
    # 내부 감시 루프
    # =========================================================
    def _run_loop(self) -> None:
        """파일 감시 루프"""
        while not self._stop_flag.is_set():
            try:
                if not self.csv_path.exists():
                    self.csv_path = self.config.resolve_today_path()
                    time.sleep(self.config.poll_ms / 1000)
                    continue

                # 새 라인 확인
                with self.csv_path.open("r", encoding="utf-8") as f:
                    f.seek(self._last_offset)
                    reader = csv.DictReader(f)
                    for row in reader:
                        self._process_row(row)
                    self._last_offset = f.tell()

            except Exception as e:
                logger.exception(f"CSV Watcher error: {e}")

            time.sleep(self.config.poll_ms / 1000)

    # =========================================================
    # CSV 라인 처리
    # =========================================================
    def _process_row(self, row: Dict[str, str]) -> None:
        """새로운 거래 감지 시 TradingResultStore 적용"""
        try:
            symbol = row.get("symbol") or row.get("stk_cd") or row.get("종목코드")
            side = (row.get("side") or row.get("action") or "").lower()
            qty = int(row.get("qty") or row.get("수량") or 0)
            price = float(row.get("price") or row.get("단가") or 0)
            strategy = row.get("strategy") or "default"

            if not symbol or not side or qty <= 0:
                return

            logger.info(f"📈 [Watcher] New trade: {symbol} {side} {qty}@{price} ({strategy})")
            self.store.apply_trade(symbol, side, qty, price, strategy)
            self.new_trade_detected.emit(row)

        except Exception as e:
            logger.exception(f"Failed to process row: {row} ({e})")


# =========================================================
# CSV 전체 재적용 (옵션 기능)
# =========================================================
def rebuild_store_from_all_csv(store: TradingResultStore, base_dir: Path) -> int:
    """모든 CSV 재계산 (대용량 백필 기능)"""
    total = 0
    all_trades = []
    trades_dir = Path(base_dir) / "trades"
    trades_dir.mkdir(parents=True, exist_ok=True)

    for path in sorted(trades_dir.glob("orders_*.csv")):
        with open(path, "r", encoding="utf-8") as f:
            next(f, None)  # skip header
            reader = csv.DictReader(f)
            for row in reader:
                all_trades.append(row)
                total += 1

    for row in all_trades:
        store.apply_trade(
            row.get("symbol"),
            row.get("side"),
            int(row.get("qty", 0)),
            float(row.get("price", 0)),
            row.get("strategy", "default")
        )

    logger.info(f"[rebuild] Reapplied {total} trades from CSV history")
    return total
