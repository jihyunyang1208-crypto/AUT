"""
risk_management/risk_dashboard.py
리스크 대시보드 (JSON 갱신형 + CSV 동기화 + 자동감시 지원)
- CSV 변경 시: 실시간 JSON 갱신 및 UI 업데이트
- 수동 새로고침 시: CSV → JSON 동기화 후 UI 갱신
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
    QFrame, QTableWidget, QTableWidgetItem, QHeaderView
)

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    _HAS_MPL = True
except Exception:
    _HAS_MPL = False

# --------------------------------------------------
# CSV Watcher import
# --------------------------------------------------
try:
    from risk_management.orders_watcher import OrdersCSVWatcher, WatcherConfig
    from risk_management.trading_results import TradingResultStore
except Exception as e:
    print(f"[WARN] RiskDashboard CSV watcher unavailable: {e}")

# --------------------------------------------------
# Logger / Styles
# --------------------------------------------------
logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

COLORS = {
    "bg_dark": "#1a1d23",
    "bg_medium": "#24272e",
    "bg_light": "#2d3139",
    "accent": "#3b82f6",
    "success": "#10b981",
    "warning": "#f59e0b",
    "danger": "#ef4444",
    "text_primary": "#e5e7eb",
    "text_secondary": "#9ca3af",
    "border": "#374151",
    "chart_line": "#22c55e",
    "chart_bg": "#0f1419",
}

STYLES = {
    "groupbox": f"""
        QGroupBox {{
            font-size: 15px;
            font-weight: 600;
            color: {COLORS['text_primary']};
            background-color: {COLORS['bg_dark']};
            border: 1px solid {COLORS['border']};
            border-radius: 8px;
            padding: 8px;
        }}
    """,
    "table": f"""
        QTableWidget {{
            background-color: {COLORS['bg_medium']};
            alternate-background-color: {COLORS['bg_light']};
            color: {COLORS['text_primary']};
            gridline-color: {COLORS['border']};
            border: 1px solid {COLORS['border']};
            border-radius: 6px;
            font-size: 12px;
        }}
    """,
    "button": f"""
        QPushButton {{
            background-color: {COLORS['accent']};
            color: {COLORS['text_primary']};
            border: none;
            border-radius: 6px;
            padding: 6px 12px;
            font-size: 12px;
            font-weight: 600;
        }}
        QPushButton:hover {{
            background-color: #2563eb;
        }}
    """,
    "label_value": f"""
        QLabel {{
            color: {COLORS['success']};
            font-size: 18px;
            font-weight: bold;
        }}
    """,
}

# ==================================================
# RiskDashboard Main Class
# ==================================================
class RiskDashboard(QGroupBox):
    """CSV 감시 + JSON 기반 리스크 대시보드"""

    pnl_snapshot = Signal(dict)

    def __init__(
        self,
        *,
        json_path: str = "logs/results/trading_results.json",
        price_provider: Optional[Callable[[str], Optional[float]]] = None,
        on_daily_report: Optional[Callable[[], None]] = None,
        poll_ms: int = 60_000,
        parent: Optional[QWidget] = None,
    ):
        super().__init__("📊 트레이딩 리스크 대시보드", parent)
        self.setStyleSheet(STYLES["groupbox"])

        self.json_path = Path(json_path)
        self.result_dir = self.json_path.parent
        self.trades_dir = Path.cwd() / "logs" / "trades"
        self._poll_ms = poll_ms
        self._price_provider = price_provider
        self._on_daily_report = on_daily_report or (lambda: None)

        self._current_state: Dict[str, Any] = {}
        self._pnl_snapshots: List[float] = []

        self._init_ui()
        self._init_timer()
        self._init_csv_watcher()

        QTimer.singleShot(1500, self.refresh_json)
        logger.info(f"[RiskDashboard] Initialized (CSV watcher + JSON sync): {self.json_path}")

    # --------------------------------------------------
    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(4)

        # Summary cards
        self.card_pnl = self._create_info_card("💰 실현손익", "0원")
        self.card_roi = self._create_info_card("📈 ROI", "0.0%")
        self.card_trades = self._create_info_card("🔄 거래수", "0")
        self.card_symbols = self._create_info_card("📊 종목수", "0")

        cards = QHBoxLayout()
        for c in (self.card_pnl, self.card_roi, self.card_trades, self.card_symbols):
            cards.addWidget(c)
        layout.addLayout(cards)

        # Matplotlib ROI snapshot
        if _HAS_MPL:
            self._fig = Figure(figsize=(6, 2.5), facecolor=COLORS["bg_medium"])
            self._canvas = FigureCanvas(self._fig)
            self._ax = self._fig.add_subplot(111)
            layout.addWidget(self._canvas)

        # Table
        self.tbl_positions = QTableWidget(0, 5)
        self.tbl_positions.setStyleSheet(STYLES["table"])
        self.tbl_positions.setHorizontalHeaderLabels(
            ["종목코드", "보유수량", "평단가", "실현손익", "ROI%"]
        )
        self.tbl_positions.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.tbl_positions)

        # Buttons
        btn_bar = QHBoxLayout()
        self.btn_refresh = QPushButton("🔄 새로고침 (CSV→JSON)")
        self.btn_refresh.setStyleSheet(STYLES["button"])
        self.btn_refresh.clicked.connect(self.refresh_json)

        self.btn_report = QPushButton("📄 일일 리포트")
        self.btn_report.setStyleSheet(STYLES["button"])
        self.btn_report.clicked.connect(self._on_daily_report)

        btn_bar.addWidget(self.btn_refresh)
        btn_bar.addWidget(self.btn_report)
        btn_bar.addStretch()
        layout.addLayout(btn_bar)

        self.lbl_status = QLabel("● 대기 중")
        self.lbl_status.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 12px;"
        )
        layout.addWidget(self.lbl_status)

    def _create_info_card(self, title: str, initial: str) -> QFrame:
        frame = QFrame()
        v = QVBoxLayout(frame)
        v.setSpacing(4)
        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 12px;"
        )
        v.addWidget(title_label)
        val_label = QLabel(initial)
        val_label.setObjectName("value_label")
        val_label.setStyleSheet(STYLES["label_value"])
        v.addWidget(val_label)
        return frame

    # --------------------------------------------------
    def _init_timer(self):
        """주기적 새로고침 (fallback용)"""
        self._timer = QTimer(self)
        self._timer.setInterval(self._poll_ms)
        self._timer.timeout.connect(self.refresh_json)
        self._timer.start()

    def _init_csv_watcher(self):
        """CSV 변경 감시 스레드 초기화"""
        try:
            cfg = WatcherConfig(base_dir=Path.cwd() / "logs")
            self.store = TradingResultStore(str(self.json_path))
            self.csv_watcher = OrdersCSVWatcher(self.store, cfg, parent=self)
            self.csv_watcher.csv_updated.connect(self._on_csv_updated)
            self.csv_watcher.start()
            logger.info("[RiskDashboard] OrdersCSVWatcher started (auto-sync mode)")
        except Exception as e:
            logger.exception(f"[RiskDashboard] CSV watcher init failed: {e}")

    # --------------------------------------------------
    @Slot()
    def refresh_json(self):
        """CSV→JSON 동기화 후 UI 갱신"""
        try:
            today = datetime.now().date().isoformat()
            csv_path = self.trades_dir / f"orders_{today}.csv"
            if not csv_path.exists():
                self.lbl_status.setText("⚠️ CSV 파일 없음")
                self.lbl_status.setStyleSheet(
                    f"color: {COLORS['warning']}; font-size: 12px;"
                )
                return

            store = TradingResultStore(str(self.json_path))
            logger.info("[RiskDashboard] CSV→JSON 수동 동기화 완료")

            with self.json_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            self._current_state = data
            self._update_ui_from_json(data)

            self.lbl_status.setText(f"● 갱신 완료: {datetime.now().strftime('%H:%M:%S')}")
            self.lbl_status.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px;")

        except Exception as e:
            logger.exception(f"[RiskDashboard] refresh_json() failed: {e}")
            self.lbl_status.setText("⚠️ 동기화 실패")
            self.lbl_status.setStyleSheet(f"color: {COLORS['danger']}; font-size: 12px;")

    @Slot(list)
    def _on_csv_updated(self, all_rows: list):
        """CSV 변경 감지 시 즉시 UI 업데이트"""
        logger.info(f"[RiskDashboard] CSV 변경 감지 → {len(all_rows)}행")
        self.refresh_json()

    # --------------------------------------------------
    def _update_ui_from_json(self, data: Dict[str, Any]):
        summary = data.get("summary") or {}
        stocks = data.get("stocks") or {}

        realized = float(summary.get("realized_pnl_net", 0.0))
        roi = sum(s.get("roi_pct", 0.0) for s in stocks.values()) / max(len(stocks), 1)
        trades = int(summary.get("trades", 0))
        total_symbols = int(summary.get("total_symbols", len(stocks)))

        self._set_card_value(self.card_pnl, f"{realized:,.0f}원", realized)
        self._set_card_value(self.card_roi, f"{roi:.2f}%", roi)
        self._set_card_value(self.card_trades, f"{trades}", trades)
        self._set_card_value(self.card_symbols, f"{total_symbols}", total_symbols)

        tbl = self.tbl_positions
        tbl.setRowCount(len(stocks))
        for i, (code, s) in enumerate(stocks.items()):
            qty = int(s.get("qty", 0))
            avg_price = float(s.get("avg_price", 0.0))
            realized = float(s.get("realized", 0.0))
            roi_pct = float(s.get("roi_pct", 0.0))
            row = [
                code,
                f"{qty}",
                f"{avg_price:,.0f}",
                (f"{realized:,.0f}", realized),
                (f"{roi_pct:.2f}%", roi_pct),
            ]
            for col, cell in enumerate(row):
                if isinstance(cell, tuple):
                    text, val = cell
                    it = QTableWidgetItem(text)
                    it.setForeground(QColor(COLORS["success"] if val >= 0 else COLORS["danger"]))
                else:
                    it = QTableWidgetItem(str(cell))
                it.setTextAlignment(Qt.AlignCenter)
                tbl.setItem(i, col, it)

        if _HAS_MPL:
            self._update_chart(realized)

    def _set_card_value(self, card: QFrame, text: str, val: float):
        lbl = card.findChild(QLabel, "value_label")
        color = COLORS["success"] if val >= 0 else (COLORS["danger"] if val < 0 else COLORS["text_primary"])
        lbl.setText(text)
        lbl.setStyleSheet(f"color: {color}; font-size: 18px; font-weight: bold;")

    def _update_chart(self, realized_pnl: float):
        try:
            self._pnl_snapshots.append(realized_pnl)
            if len(self._pnl_snapshots) > 30:
                self._pnl_snapshots.pop(0)
            self._ax.clear()
            self._ax.plot(self._pnl_snapshots, color=COLORS["chart_line"], linewidth=2)
            self._ax.fill_between(range(len(self._pnl_snapshots)), self._pnl_snapshots, alpha=0.2, color=COLORS["chart_line"])
            self._ax.set_facecolor(COLORS["chart_bg"])
            self._ax.tick_params(colors=COLORS["text_secondary"], labelsize=9)
            self._canvas.draw_idle()
        except Exception:
            pass

    # --------------------------------------------------
    def stop_auto_refresh(self):
        """ui_main.closeEvent 호환용"""
        try:
            if hasattr(self, "_timer") and self._timer.isActive():
                self._timer.stop()
            if hasattr(self, "csv_watcher"):
                self.csv_watcher.stop()
            logger.info("[RiskDashboard] auto refresh stopped (timer+watcher)")
        except Exception:
            pass
