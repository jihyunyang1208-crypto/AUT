# risk_dashboard.py
from __future__ import annotations
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, QFileInfo, Signal
from PySide6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
    QFrame, QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView
)
from PySide6.QtGui import QGuiApplication, QColor

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    _HAS_MPL = True
except Exception:
    _HAS_MPL = False

# ▼ 외부 모듈(헬퍼 포함)
from risk_management.orders_watcher import (
    WatcherConfig, _pick_encoding, _sniff_delim, _best_header_map,
    _normalize_side, _to_int, _to_float, _infer_side, _pick_any, _to_float_soft
)
from risk_management.trading_results import TradingResultStore, TradeRow

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ---------- 데이터 모델 ----------
@dataclass
class StrategyRow:
    name: str
    realized_net: float
    win_rate: float
    roi_pct: float
    wins: int

class RiskDashboard(QGroupBox):
    """스레드 없이 협력형 펌프로 '오늘만 새로고침' 수행 + 단계별/배치 로깅 + 0건 우회 기록"""
    pnl_snapshot = Signal(dict)

    def __init__(
        self,
        *,
        json_path: str = "data/trading_result.json",
        price_provider: Optional[Callable[[str], Optional[float]]] = None,
        on_daily_report: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
        poll_ms: int = 1000
    ) -> None:
        super().__init__("전략별 수익률 대시보드", parent)

        self._json_path_obj = Path(json_path).resolve()
        self._json_path_obj.parent.mkdir(parents=True, exist_ok=True)
        self._json_path = str(self._json_path_obj)
        self._on_daily_report = on_daily_report or (lambda: None)
        self._poll_ms = max(300, int(poll_ms))
        self._last_mtime: Optional[int] = None

        self._store = TradingResultStore(self._json_path)
        self._watcher_cfg = WatcherConfig(json_path=Path(self._json_path))

        # 외부에서 붙일 수 있는 watcher 핸들(있으면 새로고침 동안 stop/start)
        self.watcher = None  # Optional[OrdersCSVWatcher]

        # 협력형 리빌드 상태
        self._rebuild_state: Optional[dict] = None

        self._init_ui()
        self._init_timer()
        self.refresh(force=True)


    # ---------------- UI ----------------
    def _init_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        self.setStyleSheet(
            "QGroupBox { border: 1px solid #3a414b; margin-top: 20px; border-radius: 5px; } "
            "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; "
            "padding: 0 3px 0 3px; background-color: transparent; color: #c7d0db; font-weight: bold; } "
            "QPushButton { background: #343a40; border: 1px solid #3d444c; border-radius: 4px; "
            "padding: 5px 10px; color: #e6edf3; } "
            "QPushButton:hover { background: #3d444c; } "
            "QPushButton:pressed { background: #2d333b; border-style: inset; } "
        )

        # 버튼행
        brow = QHBoxLayout()
        self.btn_refresh_today = QPushButton("⟳ 오늘만 새로고침")
        self.btn_refresh_today.setToolTip("오늘의 orders_*.csv만 읽어 trading_result.json을 재생성합니다.")
        self.btn_refresh_today.setFixedWidth(150)
        self.btn_refresh_today.clicked.connect(self._on_refresh_today_clicked)
        brow.addWidget(self.btn_refresh_today)

        self.btn_daily = QPushButton("📄 데일리 리포트")
        self.btn_daily.setFixedWidth(120)
        self.btn_daily.clicked.connect(self._on_daily_report)
        brow.addWidget(self.btn_daily)

        brow.addStretch(1)
        lay.addLayout(brow)

        # 구분선
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("QFrame { color:#3a414b; }")
        lay.addWidget(sep)

        # 본문
        body = QVBoxLayout()
        lay.addLayout(body)

        # 표
        self.tbl = QTableWidget(0, 5, self)
        self.tbl.setHorizontalHeaderLabels(["전략", "실현 손익(순)", "승률(%)", "수익률(%)", "승리 횟수"])
        hdr = self.tbl.horizontalHeader()
        hdr.setTextElideMode(Qt.ElideNone)
        hdr.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 5):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hdr.setMinimumSectionSize(80)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setSortingEnabled(True)
        self.tbl.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.tbl.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.tbl.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.tbl.setStyleSheet(
            "QTableWidget { background:#0f1216; color:#e6edf3; gridline-color:#2d333b; "
            "border: 1px solid #2d333b; border-radius: 5px; } "
            "QHeaderView::section { background:#282c34; color:#c7d0db; font-weight:600; "
            "padding: 5px; border-right: 1px solid #0f1216; } "
            "QTableWidget::item:selected { background:#3c5d79; color: white; }"
        )
        body.addWidget(self.tbl, 3)

        # 중간선
        mid_sep = QFrame()
        mid_sep.setFrameShape(QFrame.HLine)
        mid_sep.setStyleSheet("QFrame { color:#3a414b; }")
        body.addWidget(mid_sep)

        # 그래프
        if _HAS_MPL:
            chart_box = QVBoxLayout()
            self.fig = Figure(figsize=(6, 2.8), tight_layout=True, facecolor="#000000")
            self.canvas = FigureCanvas(self.fig)
            self.ax = self.fig.add_subplot(111)
            self.ax.set_facecolor("#000000")
            for s in self.ax.spines.values():
                s.set_color("#555")
            self.ax.tick_params(colors="#e9edf1")
            self.ax.title.set_color("#e9edf1")
            chart_box.addWidget(self.canvas)
            body.addLayout(chart_box, 2)
        else:
            body.addWidget(QLabel("(matplotlib 미탑재: 그래프 표시 불가)"), 2)

    # ---------------- 타이머 ----------------
    def _init_timer(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(self._poll_ms)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def stop_auto_refresh(self) -> None:
        if self._timer.isActive():
            self._timer.stop()

    def start_auto_refresh(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    # ---------------- 데이터 갱신 ----------------
    def refresh(self, force: bool = False) -> None:
        try:
            if not self._json_path_obj.exists():
                return
            fi = QFileInfo(str(self._json_path_obj))
            mtime = int(fi.lastModified().toSecsSinceEpoch())
            if (not force) and (self._last_mtime is not None) and (mtime == self._last_mtime):
                return
            self._last_mtime = mtime

            data = json.loads(self._json_path_obj.read_text(encoding="utf-8"))
            self._update_from_result(data)

            # ★ 메인 UI로 스냅샷 emit (리프레시 때마다)
            snap = self._build_snapshot_for_ui(data)
            self.pnl_snapshot.emit(snap)

        except Exception:
            logger.exception("RiskDashboard.refresh error")

    def _update_from_result(self, result: Dict[str, Any]) -> None:
        strategies = result.get("strategies") or {}

        rows: List[StrategyRow] = []
        for name, s in strategies.items():
            realized_net = float(s.get("realized_pnl_net", 0.0))
            win_rate = float(s.get("win_rate", 0.0))
            roi_pct = float(s.get("roi_pct", 0.0))
            wins = int(s.get("wins", 0))
            buy_notional = float(s.get("buy_notional", 0.0))
            if roi_pct == 0.0 and buy_notional > 0.0:
                roi_pct = (realized_net / buy_notional) * 100.0
            rows.append(StrategyRow(
                name=name,
                realized_net=realized_net,
                win_rate=win_rate,
                roi_pct=roi_pct,
                wins=wins
            ))

        self._paint_table(rows)
        if _HAS_MPL:
            self._paint_roi_chart(rows)

    def _paint_table(self, rows: List[StrategyRow]) -> None:
        def fmt_k(v: float) -> str:
            return f"{v:+,.0f}"

        def fmt_pct(v: float, digits: int = 2) -> str:
            sign = "+" if v >= 0 else ""
            return f"{sign}{v:.{digits}f}"

        def colorize(item: QTableWidgetItem, val: float):
            if val > 0:
                item.setForeground(QColor("#22c55e"))
            elif val < 0:
                item.setForeground(QColor("#ef4444"))

        self.tbl.setSortingEnabled(False)
        self.tbl.setRowCount(len(rows))

        rows_sorted = sorted(rows, key=lambda r: r.roi_pct, reverse=True)
        for r, row in enumerate(rows_sorted):
            items = [
                QTableWidgetItem(row.name),
                QTableWidgetItem(fmt_k(row.realized_net)),
                QTableWidgetItem(fmt_pct(row.win_rate, 1)),
                QTableWidgetItem(fmt_pct(row.roi_pct, 2)),
                QTableWidgetItem(str(row.wins)),
            ]
            items[1].setData(Qt.UserRole, row.realized_net)
            items[2].setData(Qt.UserRole, row.win_rate)
            items[3].setData(Qt.UserRole, row.roi_pct)
            items[4].setData(Qt.UserRole, row.wins)

            colorize(items[1], row.realized_net)
            colorize(items[3], row.roi_pct)

            for c, it in enumerate(items):
                it.setFlags(it.flags() ^ Qt.ItemIsEditable)
                it.setTextAlignment((Qt.AlignLeft if c == 0 else Qt.AlignRight) | Qt.AlignVCenter)
                self.tbl.setItem(r, c, it)

        self.tbl.setSortingEnabled(True)
        self.tbl.sortItems(3, Qt.DescendingOrder)

    def _paint_roi_chart(self, rows: List[StrategyRow], top_n: int = 20) -> None:
        self.ax.clear()
        if not rows:
            self.canvas.draw_idle()
            return
        items = sorted(rows, key=lambda r: r.roi_pct, reverse=True)[:top_n]
        labels = [r.name for r in items]
        vals = [r.roi_pct for r in items]
        colors = ["#22c55e" if v >= 0 else "#ef4444" for v in vals]

        self.ax.bar(labels, vals, color=colors)
        self.ax.set_title("전략별 수익률(%)", color="#e9edf1", fontsize=10, fontweight="bold")
        self.ax.set_ylabel("%", color="#cfd6df")
        self.ax.grid(True, alpha=0.2, color="#555")
        self.ax.tick_params(axis="x", labelsize=8, labelrotation=28, colors="#e9edf1")
        self.ax.tick_params(axis="y", labelsize=8, colors="#e9edf1")
        for s in self.ax.spines.values():
            s.set_color("#555")
        self.canvas.draw_idle()

    # ---------------- 오늘만 새로고침 (협력형 + 단계/배치 로깅 + 0건 우회) ----------------
    def _on_refresh_today_clicked(self) -> None:
        if getattr(self, "_rebuild_running", False):
            return
        self._rebuild_running = True

        logger.info("[rebuild] UI: start '오늘만 새로고침'")

        # UI/Watcher 일시 정지
        self.stop_auto_refresh()
        try:
            if getattr(self, "watcher", None):
                self.watcher.stop()
        except Exception:
            logger.exception("watcher.stop failed")

        self.btn_refresh_today.setEnabled(False)
        self.btn_daily.setEnabled(False)
        self.btn_refresh_today.setText("🔁 새로고침 중...")
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)

        # 협력형 펌프 시작
        self._run_rebuild_today_coop()

    def _run_rebuild_today_coop(self) -> None:
        from datetime import datetime, timezone, timedelta
        KST = timezone(timedelta(hours=9))
        today = datetime.now(KST).date().isoformat()

        base = (self._watcher_cfg.base_dir / self._watcher_cfg.subdir).resolve()
        base.mkdir(parents=True, exist_ok=True)
        target = base / self._watcher_cfg.file_pattern.format(date=today)

        self._rebuild_state = {
            "path": target,
            "enc": None,
            "delim": ",",
            "header": None,     # ★ 실제 파일 헤더 저장
            "hdr_map": None,    # 캐논→실제헤더 매핑
            "fh": None,
            "applied": 0,
            "invalid": 0,
            "inferred_side": 0,
            "invalid_samples": [],
            "MAX_INVALID_SAMPLES": 5,
            "batch": [],
            "BATCH_SIZE": 2000,   # 라인 파싱 배치 크기
            "STEP": "open",       # open -> scan -> rebuild -> finish
        }
        logger.info(f"[rebuild] step=open target={target}")
        QTimer.singleShot(0, self._rebuild_pump)

    def _rebuild_pump(self) -> None:
        st = self._rebuild_state
        try:
            if not st:
                return

            if st["STEP"] == "open":
                p: Path = st["path"]
                if not p.exists():
                    logger.info("[rebuild] open: file not found → write empty and finish")
                    self._atomic_write_result(self._empty_payload())
                    st["STEP"] = "finish"
                    logger.info("[rebuild] step=finish (empty)")
                    return QTimer.singleShot(0, self._rebuild_pump)

                st["enc"] = _pick_encoding(p)
                st["delim"] = _sniff_delim(p)
                st["fh"] = open(p, "r", encoding=st["enc"])
                first = st["fh"].readline()

                import csv, io
                reader = csv.reader(io.StringIO(first), delimiter=st["delim"])
                header = next(reader, None)
                st["header"] = header or []
                st["hdr_map"] = _best_header_map(header or [])
                st["STEP"] = "scan"
                logger.info(f"[rebuild] step=scan enc={st['enc']} delim={st['delim']!r}")
                return QTimer.singleShot(0, self._rebuild_pump)

            if st["STEP"] == "scan":
                # 라인을 BATCH_SIZE씩 읽어 TradeRow로 변환
                import csv, io
                lines: List[str] = []
                for _ in range(st["BATCH_SIZE"]):
                    ln = st["fh"].readline()
                    if not ln:
                        break
                    ln = ln.rstrip("\r\n")
                    if ln:
                        lines.append(ln)

                applied_before = st["applied"]
                inferred_before = st["inferred_side"]
                invalid_before = st["invalid"]

                for line in lines:
                    try:
                        # ★ 헤더는 실제 파일 헤더를 그대로 사용
                        dr = csv.DictReader(io.StringIO(line), fieldnames=st["header"], delimiter=st["delim"])
                        raw = next(dr)

                        qty_raw = _to_int(_pick_any(raw, st["hdr_map"], ["qty", "quantity", "filled_qty", "exec_qty", "수량"], "0"))
                        price_txt = _pick_any(raw, st["hdr_map"], ["price", "exec_price", "avg_price", "체결가", "가격"], "0")
                        price = _to_float_soft(price_txt)
                        symbol = _pick_any(raw, st["hdr_map"], ["symbol", "stk_cd", "ticker", "code", "종목코드"])
                        fee = _to_float(_pick_any(raw, st["hdr_map"], ["fee", "commission", "comm", "수수료"], "0"))
                        side_text = _pick_any(raw, st["hdr_map"], ["side", "action", "buy_sell", "bs", "direction", "매매구분"])

                        if not symbol or price <= 0.0:
                            st["invalid"] += 1
                            if len(st["invalid_samples"]) < st["MAX_INVALID_SAMPLES"]:
                                st["invalid_samples"].append({
                                    "symbol": symbol, "price_txt": price_txt, "line_preview": line[:120]
                                })
                            continue

                        side = _normalize_side(side_text)
                        if side is None:
                            side = _infer_side(side_text, qty_raw); st["inferred_side"] += 1

                        qty = abs(qty_raw) if qty_raw != 0 else 0
                        status = _pick_any(raw, st["hdr_map"], ["status", "state", "order_status", "exec_status", "상태"], "filled") or "filled"

                        st["batch"].append(TradeRow(
                            time=_pick_any(raw, st["hdr_map"], ["time", "ts", "order_time", "exec_time", "filled_at", "timestamp", "체결시각"]),
                            side=side, symbol=symbol, qty=qty, price=price, fee=fee, status=status,
                            strategy=_pick_any(raw, st["hdr_map"], ["strategy", "cond", "조건식"]) or None, meta=None
                        ))
                        st["applied"] += 1
                    except Exception:
                        st["invalid"] += 1
                        continue

                # 배치 로그 + invalid 샘플
                if lines:
                    logger.info(
                        f"[rebuild] scan-batch lines={len(lines)} "
                        f"applied+={st['applied']-applied_before} "
                        f"inferred_side+={st['inferred_side']-inferred_before} "
                        f"invalid+={st['invalid']-invalid_before} "
                        f"(cum applied={st['applied']}, invalid={st['invalid']}, inferred_side={st['inferred_side']})"
                    )
                    if st["invalid_samples"]:
                        logger.warning("[rebuild] invalid samples (first few): %s", st["invalid_samples"])
                        st["invalid_samples"].clear()

                # EOF 도달?
                if not lines:
                    try:
                        st["fh"].close()
                    except Exception:
                        pass
                    st["STEP"] = "rebuild"
                    logger.info("[rebuild] step=rebuild (final write)")
                    return QTimer.singleShot(0, self._rebuild_pump)

                # 다음 틱으로 이어서
                return QTimer.singleShot(0, self._rebuild_pump)

            if st["STEP"] == "rebuild":
                trades_cnt = len(st["batch"])
                logger.info(f"[rebuild] begin write trades={trades_cnt}")
                if trades_cnt == 0:
                    # 0건이면 스토어 호출 없이 빈 페이로드 기록(원자적)
                    self._atomic_write_result(self._empty_payload())
                    logger.info("[rebuild] wrote empty payload (0 trades)")
                else:
                    # 정상 경로: 스토어 한 번 호출
                    self._store.rebuild_from_trades(st["batch"])
                    logger.info("[rebuild] store.rebuild_from_trades done")
                st["STEP"] = "finish"
                logger.info("[rebuild] step=finish (notify)")
                return QTimer.singleShot(0, self._rebuild_pump)

            if st["STEP"] == "finish":
                self.refresh(force=True)
                msg = (
                    "오늘 CSV로 새로고침을 완료했습니다.\n\n"
                    f"✅ 적용된 체결: {st['applied']:,} 건\n"
                    f"ℹ️  추론된 사이드: {st['inferred_side']:,} 건\n"
                    f"⚠️  무효 라인(심볼/가격 부족): {st['invalid']:,} 건\n\n"
                    f"파일 경로:\n{st['path']}"
                )
                QMessageBox.information(self, "작업 완료", msg)
                self._rebuild_state = None
                self._finish_rebuild_ui_unlock()
                try:
                    if getattr(self, "watcher", None):
                        self.watcher.start()
                except Exception:
                    logger.exception("watcher.start failed")

        except Exception as e:
            logger.exception("rebuild_pump error")
            try:
                QMessageBox.warning(self, "오류", f"오늘 새로고침 중 오류가 발생했습니다.\n{e}")
            finally:
                self._rebuild_state = None
                self._finish_rebuild_ui_unlock()
                try:
                    if getattr(self, "watcher", None):
                        self.watcher.start()
                except Exception:
                    logger.exception("watcher.start failed")

    # ---------- JSON 기록 유틸 ----------
    def _empty_payload(self) -> dict:
        return {"strategies": {}, "summary": {"realized_pnl_gross": 0.0, "fees": 0.0, "realized_pnl_net": 0.0, "trades": 0.0, "win_rate": 0.0}, "symbols": {}}

    def _atomic_write_result(self, payload: dict) -> None:
        tmp = self._json_path_obj.with_suffix(self._json_path_obj.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self._json_path_obj)

    def _finish_rebuild_ui_unlock(self) -> None:
        logger.info("[rebuild] UI: finish/unlock")
        self.btn_refresh_today.setEnabled(True)
        self.btn_daily.setEnabled(True)
        self.btn_refresh_today.setText("⟳ 오늘만 새로고침")
        QGuiApplication.restoreOverrideCursor()
        self._rebuild_running = False
        try:
            self.start_auto_refresh()
        except Exception:
            logger.exception("Failed to restart auto refresh")

    def _build_snapshot_for_ui(self, result: Dict[str, Any]) -> Dict[str, Any]:
        symbols = result.get("symbols") or {}
        by_symbol: Dict[str, Dict[str, Any]] = {}

        for code6, s in symbols.items():
            if not isinstance(s, dict):
                continue
            avg_buy = float(s.get("avg_price", 0.0) or 0.0)
            last_sell = s.get("last_sell_price")  # 없으면 None
            try:
                avg_sell = float(last_sell) if last_sell is not None else None
            except Exception:
                avg_sell = None

            by_symbol[code6] = {
                "avg_buy_price": avg_buy if avg_buy > 0 else None,
                "avg_sell_price": avg_sell,        # 있으면 전달
                "qty": int(s.get("qty", 0) or 0),
                # 확장 여지: "fees": s.get("fees", 0.0), "realized_pnl_net": s.get(...)
            }

        # 포트폴리오/전략 요약은 필요 최소치만(원하면 확장)
        return {
            "portfolio": {},
            "by_symbol": by_symbol,
            "by_condition": {},   # 필요 시 조건식 집계 넣기
        }
