#risk_management/risk_dashboard.py
"""
이중 저장소 대시보드 + PnL Snapshot + 손익 히스토리 그래프 (JSONL 전환/CSV 제거 버전)
- trading_results.jsonl(누적) + trading_results_YYYY-MM-DD.jsonl(일별)을 tail하여 UI 갱신
- snapshot/ trade/ daily_close 이벤트 기반으로 퍼포먼스/포지션/히스토리/ROI 스냅샷 반영
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
    QFrame, QTableWidget, QTableWidgetItem, QHeaderView, QTabWidget, QSplitter
)
from PySide6.QtGui import QColor

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    _HAS_MPL = True
except Exception:
    _HAS_MPL = False


# =========================================================
# JSONL Watcher (append-only 파일을 tail 하며 신규 라인 이벤트 방출)
# =========================================================
class JSONLWatcher(QWidget):
    """JSONL 파일 tail - 신규 라인마다 dict 이벤트 방출"""
    new_event = Signal(dict)

    def __init__(self, jsonl_path: Path, poll_ms: int = 400, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.jsonl_path = Path(jsonl_path)
        self.poll_ms = max(100, int(poll_ms))
        self._offset = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self.poll_ms)
        self._timer.timeout.connect(self._poll)

    def start(self):
        # 파일 처음부터 재생 (초기 상태 재구축)
        self._offset = 0
        self._timer.start()
        logger.info(f"[JSONLWatcher] start → {self.jsonl_path}")

    def stop(self):
        self._timer.stop()
        logger.info("[JSONLWatcher] stop")

    @Slot()
    def _poll(self):
        try:
            if not self.jsonl_path.exists():
                return
            with self.jsonl_path.open("r", encoding="utf-8") as f:
                f.seek(self._offset)
                chunk = f.read()
                if not chunk:
                    return
                lines = chunk.splitlines()
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        evt = json.loads(line)
                        if isinstance(evt, dict):
                            self.new_event.emit(evt)
                    except Exception:
                        continue
                self._offset = f.tell()
        except Exception:
            logger.exception("[JSONLWatcher] poll error")


# ========================= 스타일 상수 =========================
COLORS = {
    'bg_dark': '#1a1d23',
    'bg_medium': '#24272e',
    'bg_light': '#2d3139',
    'accent': '#3b82f6',
    'success': '#10b981',
    'warning': '#f59e0b',
    'danger': '#ef4444',
    'text_primary': '#e5e7eb',
    'text_secondary': '#9ca3af',
    'border': '#374151',
    'chart_line': '#22c55e',
    'chart_bg': '#0f1419'
}

STYLES = {
    'groupbox': f"""
        QGroupBox {{
            font-size: 15px;
            font-weight: 600;
            color: {COLORS['text_primary']};
            background-color: {COLORS['bg_dark']};
            border: 1px solid {COLORS['border']};
            border-radius: 8px;
            margin-top: 4px;
            padding: 8px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 8px;
            padding: 0 4px;
        }}
    """,
    'button': f"""
        QPushButton {{
            background-color: {COLORS['accent']};
            color: {COLORS['text_primary']};
            border: none;
            border-radius: 6px;
            padding: 8px 16px;
            font-size: 13px;
            font-weight: 600;
        }}
        QPushButton:hover {{
            background-color: #2563eb;
        }}
        QPushButton:pressed {{
            background-color: #1d4ed8;
        }}
    """,
    'table': f"""
        QTableWidget {{
            background-color: {COLORS['bg_medium']};
            alternate-background-color: {COLORS['bg_light']};
            color: {COLORS['text_primary']};
            gridline-color: {COLORS['border']};
            border: 1px solid {COLORS['border']};
            border-radius: 6px;
            font-size: 12px;
        }}
        QTableWidget::item {{
            padding: 8px;
        }}
        QTableWidget::item:selected {{
            background-color: {COLORS['accent']};
        }}
        QHeaderView::section {{
            background-color: {COLORS['bg_light']};
            color: {COLORS['text_primary']};
            padding: 10px;
            border: none;
            border-bottom: 2px solid {COLORS['border']};
            font-weight: bold;
            font-size: 12px;
        }}
    """,
    'tab': f"""
        QTabWidget::pane {{
            border: 1px solid {COLORS['border']};
            border-radius: 6px;
            background-color: {COLORS['bg_medium']};
            padding: 12px;
        }}
        QTabBar::tab {{
            background-color: {COLORS['bg_light']};
            color: {COLORS['text_secondary']};
            border: 1px solid {COLORS['border']};
            border-bottom: none;
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
            padding: 10px 20px;
            margin-right: 4px;
            font-size: 13px;
            font-weight: 600;
        }}
        QTabBar::tab:selected {{
            background-color: {COLORS['bg_medium']};
            color: {COLORS['text_primary']};
            border-bottom: 2px solid {COLORS['accent']};
        }}
        QTabBar::tab:hover {{
            background-color: {COLORS['bg_medium']};
            color: {COLORS['text_primary']};
        }}
    """,
    'frame': f"""
        QFrame {{
            background-color: {COLORS['bg_medium']};
            border: 1px solid {COLORS['border']};
            border-radius: 6px;
            padding: 12px;
        }}
    """,
    'label_value': f"""
        QLabel {{
            color: {COLORS['success']};
            font-size: 18px;
            font-weight: bold;
        }}
    """
}


# ========================= 데이터 모델 =========================
@dataclass
class StrategyMetrics:
    name: str
    realized_net: float
    win_rate: float
    roi_pct: float
    wins: int
    loses: int
    total_trades: int
    avg_win: float
    avg_loss: float
    profit_factor: float
    sharpe_ratio: float
    max_drawdown: float
    buy_notional: float
    fees: float


@dataclass
class PositionInfo:
    code: str
    qty: int
    avg_price: float
    last_buy_price: float
    last_buy_date: str
    last_sell_price: float
    last_sell_date: str
    cumulative_pnl: float
    total_trades: int
    total_wins: int


# 내부 포지션 어그리게이터용 구조
@dataclass
class _PosAgg:
    qty: int = 0
    avg_price: float = 0.0
    last_buy_price: float = 0.0
    last_buy_date: str = ""
    last_sell_price: float = 0.0
    last_sell_date: str = ""
    realized_pnl: float = 0.0
    total_trades: int = 0
    total_wins: int = 0


# ========================= 메인 대시보드 =========================
class RiskDashboard(QGroupBox):
    """전략 성과 분석 대시보드 (JSONL 이벤트 기반) + ROI Snapshot + 손익 히스토리"""
    pnl_snapshot = Signal(dict)

    def __init__(
        self,
        *,
        json_path: str = None,             # 호환 유지: 폴더 기준만 사용
        price_provider: Optional[Callable[[str], Optional[float]]] = None,
        on_daily_report: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
        poll_ms: int = 60000
    ) -> None:
        super().__init__("📊 전략 성과 분석 대시보드", parent)

        # 경로/기본값
        if json_path is None:
            json_path = "logs/results/trading_results.jsonl"
        self.result_base_dir = Path(json_path).parent
        self.result_base_dir.mkdir(parents=True, exist_ok=True)
        self._on_daily_report = on_daily_report or (lambda: None)
        self._poll_ms = max(300, int(poll_ms))
        self._price_provider = price_provider

        # JSONL Watchers (일별, 누적)
        today = datetime.now().date().isoformat()
        # 복수형 우선, 없으면 단수형 fallback
        self.cum_jsonl = self._pick_existing([
            self.result_base_dir / "trading_results.jsonl",
            self.result_base_dir / "trading_results.jsonl",
        ])
        self.daily_jsonl = self._pick_existing([
            self.result_base_dir / f"trading_results_{today}.jsonl",
            self.result_base_dir / f"trading_results_{today}.jsonl",
        ], prefer=self.result_base_dir / f"trading_results_{today}.jsonl")

        self._jsonl_daily = JSONLWatcher(self.daily_jsonl, poll_ms=350, parent=self)
        self._jsonl_cum = JSONLWatcher(self.cum_jsonl, poll_ms=700, parent=self)
        self._jsonl_daily.new_event.connect(self._on_jsonl_event, Qt.QueuedConnection)
        self._jsonl_cum.new_event.connect(self._on_jsonl_event, Qt.QueuedConnection)
        self._jsonl_daily.start()
        self._jsonl_cum.start()

        # 내부 상태
        self._current_metrics: List[StrategyMetrics] = []
        self._current_positions: List[PositionInfo] = []
        self._pos_agg: Dict[str, _PosAgg] = {}  # trade 이벤트 누적용
        self._pnl_snapshots: List[Tuple[datetime, float]] = []
        self._last_daily_summary_pnl_by_day: Dict[str, float] = {}

        # UI
        self._apply_styles()
        self._init_ui()
        self._init_timer()
        self._paint_empty_state()

        try:
            QTimer.singleShot(1000, self._replay_recent_daily_jsonl)
            logger.info("✅ RiskDashboard 초기 로딩 시 JSONL 리플레이 완료")
        except Exception:
            logger.exception("리플레이 실패 (초기화 중)")


    # ========================= 스타일/레이아웃 =========================
    def _apply_styles(self) -> None:
        self.setStyleSheet(STYLES['groupbox'])

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 4, 8, 4)
        main_layout.setSpacing(3)

        # 1) 요약 카드 + ROI + 컨트롤
        self._create_summary_cards(main_layout)
        if _HAS_MPL:
            self._init_pnl_snapshot(main_layout)
        self._create_control_bar(main_layout)

        # 2) 탭
        splitter = QSplitter(Qt.Vertical)
        splitter.setHandleWidth(5)
        splitter.setStyleSheet("QSplitter::handle { background-color: #444; }")

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(STYLES['tab'])
        self.tab_performance = QWidget()
        self.tab_positions = QWidget()
        self.tab_risk = QWidget()
        self.tab_history = QWidget()
        self._create_performance_tab(self.tab_performance)
        self._create_positions_tab(self.tab_positions)
        self._create_risk_tab(self.tab_risk)
        self._create_history_tab(self.tab_history)
        self.tabs.addTab(self.tab_performance, "📊 오늘의 성과")
        self.tabs.addTab(self.tab_positions, "💼 포지션 현황")
        self.tabs.addTab(self.tab_risk, "⚠️ 리스크")
        self.tabs.addTab(self.tab_history, "📅 손익 히스토리")

        splitter.addWidget(self.tabs)
        splitter.setSizes([600, 100])
        main_layout.addWidget(splitter, 1)

    # -------------------- 요약 카드 --------------------
    def _create_summary_cards(self, layout: QVBoxLayout) -> None:
        cards_layout = QHBoxLayout()
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(6)
        self.card_pnl = self._create_info_card("💰 오늘 실현손익", "0원")
        self.card_roi = self._create_info_card("📈 ROI", "0.0%")
        self.card_winrate = self._create_info_card("🎯 승률", "0.0%")
        self.card_trades = self._create_info_card("🔄 총 거래", "0건")
        cards_layout.addWidget(self.card_pnl)
        cards_layout.addWidget(self.card_roi)
        cards_layout.addWidget(self.card_winrate)
        cards_layout.addWidget(self.card_trades)
        layout.addLayout(cards_layout)

    def _create_info_card(self, title: str, initial_value: str) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f"""
            QFrame {{
                background-color: {COLORS['bg_light']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                padding: 6px;
            }}
        """)
        v = QVBoxLayout(frame)
        v.setSpacing(6)
        t = QLabel(title)
        t.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        v.addWidget(t)
        val = QLabel(initial_value)
        val.setStyleSheet(STYLES['label_value'])
        val.setObjectName("value_label")
        v.addWidget(val)
        return frame

    def _update_summary_cards(self) -> None:
        if not self._current_metrics:
            return
        total_pnl = sum(m.realized_net for m in self._current_metrics)
        avg_roi = sum(m.roi_pct for m in self._current_metrics) / max(1, len(self._current_metrics))
        total_wins = sum(m.wins for m in self._current_metrics)
        total_trades = sum(m.total_trades for m in self._current_metrics)
        win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0.0

        pnl_label = self.card_pnl.findChild(QLabel, "value_label")
        pnl_label.setText(f"{total_pnl:,.0f}원")
        pnl_label.setStyleSheet(f"color: {COLORS['success'] if total_pnl>=0 else COLORS['danger']}; font-size: 18px; font-weight: bold;")

        roi_label = self.card_roi.findChild(QLabel, "value_label")
        roi_label.setText(f"{avg_roi:.2f}%")
        roi_label.setStyleSheet(f"color: {COLORS['success'] if avg_roi>=0 else COLORS['danger']}; font-size: 18px; font-weight: bold;")

        wr_label = self.card_winrate.findChild(QLabel, "value_label")
        wr_label.setText(f"{win_rate:.1f}%")
        wr_label.setStyleSheet(f"color: {COLORS['success'] if win_rate>=50 else COLORS['warning']}; font-size: 18px; font-weight: bold;")

        trades_label = self.card_trades.findChild(QLabel, "value_label")
        trades_label.setText(f"{total_trades}건")

    # -------------------- ROI Snapshot --------------------
    def _init_pnl_snapshot(self, layout: QVBoxLayout) -> None:
        self._fig_snapshot = Figure(figsize=(6, 2.8), facecolor=COLORS['bg_medium'])
        self._fig_snapshot.subplots_adjust(left=0.08, right=0.96, top=0.88, bottom=0.18)
        self._canvas_snapshot = FigureCanvas(self._fig_snapshot)
        self._canvas_snapshot.setMinimumHeight(160)
        self._ax_snapshot = self._fig_snapshot.add_subplot(111)
        self._ax_snapshot.set_title("ROI Snapshot (%)", fontsize=12, color=COLORS['text_primary'], pad=10)
        self._ax_snapshot.set_facecolor(COLORS['chart_bg'])
        self._ax_snapshot.tick_params(axis="x", colors=COLORS['text_secondary'], labelsize=9)
        self._ax_snapshot.tick_params(axis="y", colors=COLORS['text_secondary'], labelsize=9)
        self._ax_snapshot.spines['top'].set_visible(False)
        self._ax_snapshot.spines['right'].set_visible(False)
        self._ax_snapshot.spines['left'].set_color(COLORS['border'])
        self._ax_snapshot.spines['bottom'].set_color(COLORS['border'])

        frame = QFrame()
        frame.setStyleSheet(STYLES['frame'])
        vbox = QVBoxLayout(frame)
        vbox.setContentsMargins(8, 4, 8, 4)
        vbox.addWidget(self._canvas_snapshot)
        layout.addWidget(frame)

    def _update_pnl_snapshot(self, roi_value: float, trade_time: Optional[datetime] = None) -> None:
        if not _HAS_MPL:
            return
        ts = trade_time or datetime.now()
        self._pnl_snapshots.append((ts, roi_value))
        if len(self._pnl_snapshots) > 60:
            self._pnl_snapshots.pop(0)
        if not self._pnl_snapshots:
            return
        try:
            times = [t.strftime("%H:%M:%S") for t, _ in self._pnl_snapshots]
            rois = [r for _, r in self._pnl_snapshots]
            self._ax_snapshot.clear()
            self._ax_snapshot.plot(range(len(rois)), rois, color=COLORS['chart_line'], linewidth=2.5, marker='o', markersize=3)
            self._ax_snapshot.fill_between(range(len(rois)), rois, alpha=0.2, color=COLORS['chart_line'])
            self._ax_snapshot.set_ylabel("ROI (%)", color=COLORS['text_secondary'], fontsize=10)
            step = max(1, len(times)//5)
            idx = list(range(0, len(times), step))
            self._ax_snapshot.set_xticks(idx)
            self._ax_snapshot.set_xticklabels([times[i] for i in idx], rotation=0, fontsize=9)
            self._ax_snapshot.set_facecolor(COLORS['chart_bg'])
            self._ax_snapshot.grid(True, alpha=0.15, color=COLORS['text_secondary'])
            self._ax_snapshot.spines['top'].set_visible(False)
            self._ax_snapshot.spines['right'].set_visible(False)
            self._ax_snapshot.spines['left'].set_color(COLORS['border'])
            self._ax_snapshot.spines['bottom'].set_color(COLORS['border'])
            self._canvas_snapshot.draw_idle()
        except Exception as e:
            logger.debug(f"PnL snapshot update error: {e}")

    # -------------------- 컨트롤바 --------------------
    def _create_control_bar(self, layout: QVBoxLayout) -> None:
        bar = QHBoxLayout()
        bar.setSpacing(12)
        self.btn_refresh = QPushButton("🔄 새로고침")
        self.btn_refresh.setStyleSheet(STYLES['button'])
        self.btn_refresh.clicked.connect(self._replay_recent_daily_jsonl)
        bar.addWidget(self.btn_refresh)

        self.btn_report = QPushButton("📄 일일 리포트")
        self.btn_report.setStyleSheet(STYLES['button'])
        self.btn_report.clicked.connect(self._on_daily_report_clicked)
        bar.addWidget(self.btn_report)

        bar.addStretch()
        self.lbl_status = QLabel("● 준비")
        self.lbl_status.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px; margin-left: 12px;")
        bar.addWidget(self.lbl_status)
        layout.addLayout(bar)

    # -------------------- 탭 --------------------
    def _create_performance_tab(self, tab: QWidget) -> None:
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        self.tbl_performance = QTableWidget(0, 12)
        self.tbl_performance.setStyleSheet(STYLES['table'])
        self.tbl_performance.setAlternatingRowColors(True)
        self.tbl_performance.setHorizontalHeaderLabels([
            "전략", "실현손익", "ROI%", "승률%", "승/패", "평균익", "평균손",
            "PF", "Sharpe", "MDD%", "매수총액", "수수료"
        ])
        self.tbl_performance.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_performance.horizontalHeader().setStretchLastSection(True)
        self.tbl_performance.verticalHeader().setVisible(False)
        layout.addWidget(self.tbl_performance)

    def _create_positions_tab(self, tab: QWidget) -> None:
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        self.tbl_positions = QTableWidget(0, 10)
        self.tbl_positions.setStyleSheet(STYLES['table'])
        self.tbl_positions.setAlternatingRowColors(True)
        self.tbl_positions.setHorizontalHeaderLabels([
            "종목코드", "보유수량", "평단가", "마지막매수가", "마지막매도가",
            "누적손익", "매수시각", "매도시각", "총거래", "총승리"
        ])
        self.tbl_positions.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_positions.horizontalHeader().setStretchLastSection(True)
        self.tbl_positions.verticalHeader().setVisible(False)
        layout.addWidget(self.tbl_positions)

    def _create_risk_tab(self, tab: QWidget) -> None:
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        info_label = QLabel("⚠️ 리스크 관련 지표는 JSONL 이벤트로 확장 예정")
        info_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 13px; padding: 20px;")
        info_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(info_label)

    def _create_history_tab(self, tab: QWidget) -> None:
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        if not _HAS_MPL:
            layout.addWidget(QLabel("Matplotlib이 필요합니다."))
            return
        self._fig_history = Figure(figsize=(6, 3), facecolor=COLORS['bg_medium'])
        self._fig_history.subplots_adjust(left=0.12, right=0.96, top=0.92, bottom=0.12)
        self._canvas_history = FigureCanvas(self._fig_history)
        self._ax_history = self._fig_history.add_subplot(111)
        frame = QFrame()
        frame.setStyleSheet(STYLES['frame'])
        vbox = QVBoxLayout(frame)
        vbox.addWidget(self._canvas_history)
        layout.addWidget(frame)
        btn_reload = QPushButton("📈 최근 손익 히스토리 갱신")
        btn_reload.setStyleSheet(STYLES['button'])
        btn_reload.clicked.connect(self._update_history_chart_from_jsonl)
        layout.addWidget(btn_reload)

    # ========================= 타이머 =========================
    def _init_timer(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(self._poll_ms)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    @Slot()
    def _tick(self):
        # 주기적으로 최근 7일 차트 보강 갱신
        if _HAS_MPL:
            self._update_history_chart_from_jsonl()

    # ========================= JSONL 이벤트 처리 =========================
    @Slot(dict)
    def _on_jsonl_event(self, ev: Dict[str, Any]):
        """
        지원 이벤트 예:
        - {"type":"snapshot","date":"YYYY-MM-DD","summary":{...},"strategies":{...},"positions":{...}}
        - {"type":"daily_close","date":"YYYY-MM-DD","summary":{"realized_pnl_net": ...}}
        - {"type":"trade","time":"...","side":"buy/sell","symbol":"...", "qty":..., "price":..., "strategy":"...","roi_pct":...}
        - {"type":"alert","message":"..."}
        """
        et = str(ev.get("type") or "").lower()
        if et == "snapshot":
            self._apply_snapshot_event(ev)
            self.lbl_status.setText(f"● 갱신됨: snapshot {ev.get('date','')}")
            self.lbl_status.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px;")

        elif et == "trade":
            self._apply_trade_event(ev)     # ← 포지션 탭도 trade로 누적 반영
            roi = float(ev.get("roi_pct", 0.0))
            if _HAS_MPL:
                self._update_pnl_snapshot(roi, trade_time=self._parse_time(ev.get("time")))
            self._paint_positions_from_agg()

        elif et == "daily_close":
            day = str(ev.get("date") or "")
            pnl = float((ev.get("summary") or {}).get("realized_pnl_net", 0.0))
            if day:
                self._last_daily_summary_pnl_by_day[day] = pnl
            if _HAS_MPL:
                self._update_history_chart_from_jsonl()

        elif et == "alert":
            self.lbl_status.setText(f"● 알림: {ev.get('message','')}")
            self.lbl_status.setStyleSheet(f"color: {COLORS['warning']}; font-size: 12px;")

    # -------- 스냅샷 반영 (전략/포지션) --------
    def _apply_snapshot_event(self, ev: Dict[str, Any]):
        # 1) 전략 메트릭스
        strategies = ev.get("strategies") or {}
        metrics: List[StrategyMetrics] = []
        for name, s in strategies.items():
            realized_net = float(s.get("realized_pnl_net", 0.0))
            roi_pct = float(s.get("roi_pct", 0.0))
            win_rate = float(s.get("win_rate", 0.0))
            wins = int(s.get("wins", 0))
            sells = int(s.get("sells", 0))
            loses = max(0, sells - wins)
            avg_win = float(s.get("avg_win", 0.0))
            avg_loss = float(abs(s.get("avg_loss", 0.0)))
            buy_notional = float(s.get("buy_notional", 0.0))
            fees = float(s.get("fees", 0.0))
            total_win = avg_win * wins
            total_loss = avg_loss * loses
            profit_factor = (total_win / total_loss) if total_loss > 0 else (999.0 if total_win > 0 else 0.0)
            sharpe = (roi_pct / 20.0) if roi_pct > 0 else 0.0
            max_dd = (avg_loss * min(3, loses)) / buy_notional * 100 if (buy_notional > 0 and loses > 0) else 0.0
            metrics.append(StrategyMetrics(
                name=name,
                realized_net=realized_net,
                win_rate=win_rate,
                roi_pct=roi_pct,
                wins=wins,
                loses=loses,
                total_trades=sells,
                avg_win=avg_win,
                avg_loss=avg_loss,
                profit_factor=profit_factor,
                sharpe_ratio=sharpe,
                max_drawdown=max_dd,
                buy_notional=buy_notional,
                fees=fees
            ))
        self._current_metrics = metrics

        # 2) 포지션(스냅샷 우선 반영)
        positions = (ev.get("positions") or ev.get("symbols")) or {}
        if positions:
            self._pos_agg.clear()
            for code, s in positions.items():
                agg = _PosAgg(
                    qty=int(s.get("qty", 0)),
                    avg_price=float(s.get("avg_price", 0.0)),
                    last_buy_price=float(s.get("last_buy_price", 0.0)),
                    last_buy_date=str(s.get("last_buy_date", "")),
                    last_sell_price=float(s.get("last_sell_price", 0.0)),
                    last_sell_date=str(s.get("last_sell_date", "")),
                    realized_pnl=float(s.get("cumulative_realized_net", s.get("cumulative_pnl", 0.0))),
                    total_trades=int(s.get("total_trades", 0)),
                    total_wins=int(s.get("total_wins", 0)),
                )
                self._pos_agg[code] = agg

        # 3) UI 반영
        self._update_summary_cards()
        if _HAS_MPL:
            avg_roi = sum(m.roi_pct for m in self._current_metrics) / max(1, len(self._current_metrics))
            self._update_pnl_snapshot(avg_roi, trade_time=None)
        self._paint_performance()
        self._paint_positions_from_agg()

        # 4) 일별 요약 캐시 (히스토리 바차트용)
        day = str(ev.get("date") or "")
        if day and "summary" in ev and isinstance(ev["summary"], dict):
            self._last_daily_summary_pnl_by_day[day] = float(ev["summary"].get("realized_pnl_net", 0.0))

    # -------- 트레이드 반영 (어그리게이터 누적) --------
    def _apply_trade_event(self, ev: Dict[str, Any]):
        """
        trade 이벤트 스키마 예시:
        {
          "type":"trade","time":"2025-10-28T09:15:03+09:00",
          "side":"buy"|"sell","symbol":"005930","qty":10,"price":71200.0,
          "strategy":"MACD-X","fee":15.0,"tax":0.0,"pnl_on_fill":1234.5, "roi_pct": 0.32
        }
        """
        code = str(ev.get("symbol") or ev.get("stk_cd") or "").strip()
        if not code:
            return
        side = str(ev.get("side") or ev.get("action") or "").lower()
        qty = int(ev.get("qty") or 0)
        price = float(ev.get("price") or 0.0)
        ts = str(ev.get("time") or ev.get("ts") or "")

        if qty <= 0 or price <= 0:
            return

        agg = self._pos_agg.setdefault(code, _PosAgg())

        # 평균단가/수량/실현손익 누적
        if side == "buy":
            # 새 평균단가 = (기존평단*기보유 + 신규*수량) / (기보유+신규)
            if agg.qty + qty > 0:
                agg.avg_price = (agg.avg_price * agg.qty + price * qty) / (agg.qty + qty)
            else:
                agg.avg_price = price
            agg.qty += qty
            agg.last_buy_price = price
            agg.last_buy_date = self._time_only(ts)
        elif side == "sell":
            # 실현손익은 기존 평균단가 기준
            sell_qty = min(qty, max(0, agg.qty)) if agg.qty > 0 else qty
            pnl = (price - agg.avg_price) * sell_qty
            agg.realized_pnl += pnl
            if pnl > 0:
                agg.total_wins += 1
            agg.qty = max(0, agg.qty - qty)
            agg.last_sell_price = price
            agg.last_sell_date = self._time_only(ts)

        agg.total_trades += 1

    # ========================= 테이블 렌더링 =========================
    def _paint_performance(self) -> None:
        tbl = self.tbl_performance
        tbl.setRowCount(len(self._current_metrics))
        for i, m in enumerate(self._current_metrics):
            items = [
                (m.name, None),
                (f"{m.realized_net:,.0f}", m.realized_net),
                (f"{m.roi_pct:.2f}%", m.roi_pct),
                (f"{m.win_rate:.1f}%", m.win_rate),
                (f"{m.wins}/{m.loses}", None),
                (f"{m.avg_win:,.0f}", None),
                (f"{m.avg_loss:,.0f}", None),
                (f"{m.profit_factor:.2f}", m.profit_factor),
                (f"{m.sharpe_ratio:.2f}", m.sharpe_ratio),
                (f"{m.max_drawdown:.2f}%", m.max_drawdown),
                (f"{m.buy_notional:,.0f}", None),
                (f"{m.fees:,.0f}", None)
            ]
            for col, (text, value) in enumerate(items):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                if value is not None:
                    if col in (1, 2):  # 실현손익/ROI
                        item.setForeground(QColor(COLORS['success'] if value >= 0 else COLORS['danger']))
                    elif col == 3:    # 승률
                        item.setForeground(QColor(COLORS['success'] if value >= 50 else COLORS['warning']))
                    elif col == 7:    # PF
                        item.setForeground(QColor(COLORS['success'] if value >= 1.5 else COLORS['warning']))
                tbl.setItem(i, col, item)

    def _paint_positions_from_agg(self) -> None:
        tbl = self.tbl_positions
        tbl.setRowCount(len(self._pos_agg))
        for i, (code, p) in enumerate(sorted(self._pos_agg.items())):
            items = [
                code,
                str(p.qty),
                f"{p.avg_price:,.0f}",
                f"{p.last_buy_price:,.0f}",
                f"{p.last_sell_price:,.0f}",
                (f"{p.realized_pnl:,.0f}", p.realized_pnl),
                p.last_buy_date,
                p.last_sell_date,
                str(p.total_trades),
                str(p.total_wins)
            ]
            for col, data in enumerate(items):
                if isinstance(data, tuple):
                    text, val = data
                    it = QTableWidgetItem(text)
                    it.setForeground(QColor(COLORS['success'] if val >= 0 else COLORS['danger']))
                else:
                    it = QTableWidgetItem(data)
                it.setTextAlignment(Qt.AlignCenter)
                tbl.setItem(i, col, it)

    def _paint_empty_state(self) -> None:
        self.tbl_performance.setRowCount(0)
        self.tbl_positions.setRowCount(0)
        self.lbl_status.setText("⚠️ JSONL 이벤트 대기 중")
        self.lbl_status.setStyleSheet(f"color: {COLORS['warning']}; font-size: 12px;")

    # ========================= JSONL 리플레이 / 히스토리 =========================
    @Slot()
    def _replay_recent_daily_jsonl(self):
        """일별 JSONL을 처음부터 리플레이하여 현재 화면 재구축"""
        try:
            # 최근 2일만 빠르게 재생
            today = datetime.now().date()
            targets = [
                self.result_base_dir / f"trading_results_{(today - timedelta(days=d)).isoformat()}.jsonl"
                for d in range(0, 2)
            ]
            # 리플레이 전 내부 상태 초기화(포지션은 누적 jsonl도 반영되므로 완전 초기화 X → 일별만 재적용)
            self._current_metrics.clear()
            # 포지션은 누적 watcher가 계속 돌고 있어 자연 동기화됨. 일별도 반영
            for f in targets:
                if not f.exists():
                    continue
                with f.open("r", encoding="utf-8") as fp:
                    for line in fp:
                        try:
                            ev = json.loads(line)
                            if isinstance(ev, dict):
                                self._on_jsonl_event(ev)
                        except Exception:
                            continue
            self.lbl_status.setText("● 최근 JSONL 리플레이 완료")
            self.lbl_status.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px;")
        except Exception:
            logger.exception("JSONL replay error")
            self.lbl_status.setText("● 리플레이 오류")
            self.lbl_status.setStyleSheet(f"color: {COLORS['danger']}; font-size: 12px;")

    def _update_history_chart_from_jsonl(self) -> None:
        """최근 7일: 각 일별 JSONL에서 summary.realized_pnl_net(또는 daily_close)을 추출해 그린다"""
        if not _HAS_MPL:
            return
        try:
            self._ax_history.clear()
            # 최근 14일 스캔 → 데이터 부족 대비
            days = []
            pnls = []
            today = datetime.now().date()
            for d in range(13, -1, -1):
                day = (today - timedelta(days=d)).isoformat()
                path = self.result_base_dir / f"trading_results_{day}.jsonl"
                pnl_val = None
                # 1) 캐시 우선
                if day in self._last_daily_summary_pnl_by_day:
                    pnl_val = self._last_daily_summary_pnl_by_day[day]
                # 2) 파일 스캔
                elif path.exists():
                    with path.open("r", encoding="utf-8") as fp:
                        for line in fp:
                            try:
                                ev = json.loads(line)
                                if isinstance(ev, dict):
                                    et = str(ev.get("type") or "").lower()
                                    if et == "daily_close" and "summary" in ev:
                                        pnl_val = float(ev["summary"].get("realized_pnl_net", 0.0))
                                    elif et == "snapshot" and "summary" in ev:
                                        pnl_val = float(ev["summary"].get("realized_pnl_net", 0.0))
                            except Exception:
                                continue
                if pnl_val is not None:
                    days.append(day[5:])  # MM-DD 표기
                    pnls.append(pnl_val)

            if not days:
                self._ax_history.text(0.5, 0.5, '데이터 없음',
                                      ha='center', va='center',
                                      color=COLORS['text_secondary'],
                                      transform=self._ax_history.transAxes)
                self._canvas_history.draw_idle()
                return

            # 마지막 7개만 표시
            if len(days) > 7:
                days = days[-7:]
                pnls = pnls[-7:]

            colors = [COLORS['success'] if p >= 0 else COLORS['danger'] for p in pnls]
            self._ax_history.bar(range(len(pnls)), pnls, color=colors, alpha=0.8, edgecolor=COLORS['border'])
            self._ax_history.axhline(y=0, color=COLORS['text_secondary'], linestyle='--', linewidth=1, alpha=0.5)
            self._ax_history.set_xticks(range(len(days)))
            self._ax_history.set_xticklabels(days, fontsize=9)
            self._ax_history.set_title("최근 7일 손익 추이", color=COLORS['text_primary'], fontsize=12, pad=10)
            self._ax_history.set_ylabel("실현손익 (₩)", color=COLORS['text_secondary'], fontsize=10)
            self._ax_history.tick_params(colors=COLORS['text_secondary'], labelsize=9)
            self._ax_history.set_facecolor(COLORS['chart_bg'])
            self._ax_history.spines['top'].set_visible(False)
            self._ax_history.spines['right'].set_visible(False)
            self._ax_history.spines['left'].set_color(COLORS['border'])
            self._ax_history.spines['bottom'].set_color(COLORS['border'])
            self._ax_history.grid(True, alpha=0.15, axis='y', color=COLORS['text_secondary'])
            self._canvas_history.draw_idle()
        except Exception as e:
            logger.debug(f"History chart update error: {e}")

    # ========================= 기타 =========================
    @Slot()
    def _on_daily_report_clicked(self):
        try:
            self._on_daily_report()
        except Exception:
            pass

    def shutdown(self):
        try:
            if hasattr(self, "_timer") and self._timer.isActive():
                self._timer.stop()
        except Exception:
            pass
        try:
            if hasattr(self, "_jsonl_daily"):
                self._jsonl_daily.stop()
            if hasattr(self, "_jsonl_cum"):
                self._jsonl_cum.stop()
        except Exception:
            pass

    def closeEvent(self, e):
        self.shutdown()
        super().closeEvent(e)

    # ========================= 유틸 =========================
    @staticmethod
    def _parse_time(t: Optional[str]) -> Optional[datetime]:
        if not t:
            return None
        try:
            if 'T' in t:
                return datetime.fromisoformat(t.replace('Z', '+00:00'))
        except Exception:
            pass
        return None

    @staticmethod
    def _time_only(ts: str) -> str:
        try:
            if 'T' in ts:
                return ts.split('T')[-1][:8]
            if len(ts) >= 8 and ts[2] == ':' and ts[5] == ':':
                return ts[:8]
        except Exception:
            pass
        return ""

    def _pick_existing(self, candidates: list[Path], *, prefer: Optional[Path] = None) -> Path:
        """
        candidates 중 존재하는 첫 파일을 고르고, 아무 것도 없으면 prefer를 반환(없으면 첫 후보).
        존재하지 않아도 Watcher가 생성 시점 이후 append를 감지할 수 있으니 안전합니다.
        """
        for p in candidates:
            if p.exists():
                return p
        return prefer or candidates[0]
