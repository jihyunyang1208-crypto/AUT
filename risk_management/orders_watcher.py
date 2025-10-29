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
            full_path.write_text(
                "ts,strategy,action,stk_cd,order_type,price,qty,status,resp_code,resp_msg\n",
                encoding="utf-8"
            )

        return full_path


# =========================================================
# Orders CSV Watcher
# =========================================================
class OrdersCSVWatcher(QObject):
    """orders_YYYY-MM-DD.csv 감시 → TradingResultStore 자동 반영"""

    new_trade_detected = Signal(dict)
    csv_updated = Signal(list)      
    watcher_stopped = Signal()

    def __init__(self, store: TradingResultStore, config: WatcherConfig, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.store = store
        self.config = config
        self._stop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_offset = 0
        self.csv_path = self.config.resolve_today_path()
        self._last_offset = 0
        self._fieldnames: Optional[List[str]] = None
        self._last_path: Optional[Path] = None
        self._last_mtime: int = 0

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
        """파일 감시 루프 (롤오버/축소 처리 + 실시간 스냅샷 emit)"""

        while not self._stop_flag.is_set():
            try:
                # 0) 오늘 파일 경로 재해석 (자정 롤오버 대비)
                today_path = self.config.resolve_today_path()
                if getattr(self, "csv_path", None) is None:
                    self.csv_path = today_path

                # 파일이 바뀌었으면 상태 리셋
                if today_path != self.csv_path:
                    self.csv_path = today_path
                    self._last_offset = 0
                    self._fieldnames = None
                    self._last_mtime = 0

                # 1) 파일 존재 없으면 다음 루프
                if not self.csv_path.exists():
                    # 부트스트랩 옵션이면 헤더 파일 생성
                    if getattr(self.config, "bootstrap_if_missing", False):
                        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
                        with self.csv_path.open("w", encoding="utf-8", newline="") as f:
                            f.write("ts,strategy,action,stk_cd,order_type,price,qty,status,resp_code,resp_msg\n")

                        self._last_offset = 0
                        self._fieldnames = None
                    time.sleep(self.config.poll_ms / 1000)
                    continue

                # 2) 파일 축소/재작성 감지 (예: 로그 로테이션)
                stat = self.csv_path.stat()
                cur_mtime = int(stat.st_mtime)
                cur_size = stat.st_size

                if cur_size < getattr(self, "_last_offset", 0):
                    # 파일이 줄어들었으면 처음부터 다시
                    self._last_offset = 0
                    self._fieldnames = None

                # 3) 읽기
                any_new = False
                with self.csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                    if self._last_offset == 0:
                        # 헤더부터 다시
                        reader = csv.DictReader(f)
                        # 필드명 캐싱
                        if reader.fieldnames:
                            self._fieldnames = reader.fieldnames
                        for row in reader:
                            any_new = True
                            self._process_row(row)
                        self._last_offset = f.tell()
                    else:
                        # 헤더 건너뛰고 새로운 텍스트만 읽어서 파싱
                        f.seek(self._last_offset)
                        chunk = f.read()
                        if chunk:
                            # 새로 append된 라인들만 대상으로 DictReader 구성
                            lines = chunk.splitlines()
                            # fieldnames 강제 지정해 헤더 없이도 Dict 만들기
                            reader = csv.DictReader(lines, fieldnames=self._fieldnames)
                            first = True
                            for row in reader:
                                # 혹시 첫 줄이 헤더가 또 들어왔다면 스킵
                                if first and self._fieldnames and set(row.keys()) == set(self._fieldnames):
                                    # DictReader가 첫 줄을 데이터로 처리했더라도
                                    # 실제 값이 필드명과 동일하면 헤더로 간주하고 스킵
                                    if all((row[k] == k) for k in self._fieldnames):
                                        first = False
                                        continue
                                first = False
                                any_new = True
                                self._process_row(row)
                            self._last_offset = f.tell()

                # 4) 변경이 있었다면, 전체 스냅샷 emit (포지션 테이블 재사용용)
                if any_new and hasattr(self, "csv_updated"):
                    with self.csv_path.open("r", encoding="utf-8-sig", newline="") as f_all:
                        all_rows = list(csv.DictReader(f_all))
                    try:
                        self.csv_updated.emit(all_rows)  # RiskDashboard에서 테이블 리빌드
                    except Exception:
                        logger.exception("csv_updated emit failed")

                # 5) 타이밍 갱신
                self._last_mtime = cur_mtime

            except Exception:
                logger.exception("CSV Watcher error")

            time.sleep(self.config.poll_ms / 1000)

    # =========================================================
    # CSV 라인 처리
    # =========================================================
    def _safe_int(v, d=0):
        try: return int(str(v).strip())
        except: return d

    def _safe_float(v, d=0.0):
        try: return float(str(v).strip())
        except: return d

    def _process_row(self, row: Dict[str, str]) -> None:
        try:
            symbol  = row.get("symbol") or row.get("stk_cd") or row.get("종목코드")
            side    = (row.get("side") or row.get("action") or "").strip().lower()
            qty     = _safe_int(row.get("qty") or row.get("수량"), 0)
            price   = _safe_float(row.get("price") or row.get("단가"), 0.0)
            strategy= (row.get("strategy") or "default").strip()

            # 동의어 정규화
            if side in {"enter","open","buy_long"}: side="buy"
            elif side in {"exit","sell_short","close"}: side="sell"

            # CSV 원문 상태/코드/메시지를 meta에 모두 담음
            status_raw = (row.get("status") or "").strip()
            meta = {
                "status": status_raw,
                "resp_code": (row.get("resp_code") or "").strip(),
                "resp_msg": (row.get("resp_msg") or "").strip(),
                **row,  # 필요시 원문 전부 보존
            }

            # 공개 API 유지: apply_trade(...) 그대로 호출
            # (status와 meta를 전달하면 스토어 내부에서 'order' vs 'trade'를 구분)
            self.store.apply_trade(
                symbol, side, qty, price,
                strategy=strategy,
                status=status_raw,
                meta=meta,
                time=row.get("ts")  # 있으면 사용
            )

            self.new_trade_detected.emit(row)
        except Exception as e:
            logger.exception(f"Failed to process row: {row} ({e})")

# =========================================================
# CSV 전체 재적용 (옵션 기능)
# =========================================================
def rebuild_store_from_all_csv(store: TradingResultStore, base_dir: Path) -> int:
    total = 0
    trades_dir = Path(base_dir) / "trades"
    trades_dir.mkdir(parents=True, exist_ok=True)

    for path in sorted(trades_dir.glob("orders_*.csv")):
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total += 1
                store.apply_trade(
                    row.get("stk_cd") or row.get("symbol"),
                    (row.get("action") or row.get("side")),
                    _safe_int(row.get("qty") or 0),
                    _safe_float(row.get("price") or 0.0),
                    strategy=(row.get("strategy") or "default"),
                    status=(row.get("status") or ""),
                    meta={
                        "resp_code": (row.get("resp_code") or "").strip(),
                        "resp_msg":  (row.get("resp_msg")  or "").strip(),
                        **row,
                    },
                    time=row.get("ts")
                )

    logger.info(f"[rebuild] Reapplied {total} rows (orders+trades) from CSV history")
    return total
