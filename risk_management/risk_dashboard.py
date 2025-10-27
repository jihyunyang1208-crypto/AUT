"""
이중 저장소 대시보드 + PnL Snapshot + 손익 히스토리 그래프 (UI 개선 버전)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, QFileInfo, Signal, Slot, QMetaObject, Q_ARG
from PySide6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
    QFrame, QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget, QComboBox, QSizePolicy
)
from PySide6.QtGui import QColor, QFont

from .orders_watcher import WatcherConfig, OrdersCSVWatcher
from .trading_results import TradingResultStore

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    _HAS_MPL = True
except Exception:
    _HAS_MPL = False


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
            font-size: 16px;
            font-weight: bold;
            color: {COLORS['text_primary']};
            background-color: {COLORS['bg_dark']};
            border: 2px solid {COLORS['border']};
            border-radius: 8px;
            margin-top: 12px;
            padding: 16px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 16px;
            padding: 0 8px;
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
    'label_title': f"""
        QLabel {{
            color: {COLORS['text_primary']};
            font-size: 14px;
            font-weight: bold;
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


# ========================= 메인 대시보드 =========================

class RiskDashboard(QGroupBox):
    """전략 성과 분석 대시보드 + ROI Snapshot + 손익 히스토리"""
    pnl_snapshot = Signal(dict)

    def __init__(
        self,
        *,
        json_path: str = None,
        csv_base_dir: Path = None,
        price_provider: Optional[Callable[[str], Optional[float]]] = None,
        on_daily_report: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
        poll_ms: int = 60000
    ) -> None:
        super().__init__("📊 전략 성과 분석 대시보드", parent)

        if json_path is None:
            json_path = "logs/results/trading_result.json"
        if csv_base_dir is None:
            csv_base_dir = Path("logs")

        self.result_base_dir = Path(json_path).parent
        self.result_base_dir.mkdir(parents=True, exist_ok=True)
        self.csv_base_dir = Path(csv_base_dir)

        self._on_daily_report = on_daily_report or (lambda: None)
        self._poll_ms = max(300, int(poll_ms))
        self._price_provider = price_provider

        # 저장소 + Watcher
        self.store = TradingResultStore(json_path=json_path)

        self.watcher_cfg = WatcherConfig(base_dir=self.csv_base_dir)

        # ❌ parent=self 주지 마세요 (스레드 다름)
        self.watcher = OrdersCSVWatcher(store=self.store, config=self.watcher_cfg)

        # 전용 스레드 준비
        self.store.store_updated.connect(self._on_store_updated, Qt.QueuedConnection)
        # 내부 상태
        self._last_daily_mtime: Optional[int] = None
        self._last_cumulative_mtime: Optional[int] = None
        self._current_metrics: List[StrategyMetrics] = []
        self._current_positions: List[PositionInfo] = []
        self._alert_messages: List[str] = []
        self._rebuild_running = False
        self._pnl_snapshots: List[tuple[datetime, float]] = []

        self._apply_styles()
        self._init_ui()
        self._init_timer()
        self.store.set_alert_callback(self._handle_alert)
        self.refresh(force=True)
        self.watcher.start()

    # ========================= 스타일 적용 =========================

    def _apply_styles(self) -> None:
        # 1) 스타일 문자열을 딕셔너리에 넣고
        STYLES['groupbox'] = f"""
            QGroupBox {{
                font-size: 15px;            /* 살짝만 */
                font-weight: 600;
                color: {COLORS['text_primary']};
                background-color: {COLORS['bg_dark']};
                border: 1px solid {COLORS['border']};   /* 2px → 1px */
                border-radius: 8px;
                margin-top: 4px;            /* 12px → 4px : 상단 여백 확 줄임 */
                padding: 8px;               /* 16px → 8px */
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 8px;                  /* 16px → 8px */
                padding: 0 4px;             /* 8px → 4px */
            }}
        """

        # 2) setStyleSheet에는 문자열만 넘김
        self.setStyleSheet(STYLES['groupbox'])

    # ========================= UI 초기화 =========================

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 6, 8, 8)  # (16,20,16,16) → 상단 6
        main_layout.setSpacing(8)                   # 12 → 8

        # 요약 정보 카드
        self._create_summary_cards(main_layout)

        # ROI Snapshot
        if _HAS_MPL:
            self._init_pnl_snapshot(main_layout)

        # 컨트롤바
        self._create_control_bar(main_layout)

        # 탭 구성
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

        main_layout.addWidget(self.tabs, 1)  # ← 세로로 공간을 더 가져가게
        self.tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._create_alert_panel(main_layout)

    # ========================= 요약 카드 =========================

    def _create_summary_cards(self, layout: QVBoxLayout) -> None:
        cards_layout = QHBoxLayout()
        cards_layout.setContentsMargins(0, 0, 0, 0)

        cards_layout.setSpacing(6)
        # 실현손익 카드
        self.card_pnl = self._create_info_card("💰 오늘 실현손익", "0원")
        cards_layout.addWidget(self.card_pnl)

        # ROI 카드
        self.card_roi = self._create_info_card("📈 ROI", "0.0%")
        cards_layout.addWidget(self.card_roi)

        # 승률 카드
        self.card_winrate = self._create_info_card("🎯 승률", "0.0%")
        cards_layout.addWidget(self.card_winrate)

        # 총 거래 카드
        self.card_trades = self._create_info_card("🔄 총 거래", "0건")
        cards_layout.addWidget(self.card_trades)

        layout.addLayout(cards_layout)
        # 여백 더 줄이기
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(8)

    def _create_info_card(self, title: str, initial_value: str) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f"""
            QFrame {{
                background-color: {COLORS['bg_light']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                padding: 12px;   # 16 → 12
            }}
        """)


        
        layout = QVBoxLayout(frame)
        layout.setSpacing(8)
        
        lbl_title = QLabel(title)
        lbl_title.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        layout.addWidget(lbl_title)
        
        lbl_value = QLabel(initial_value)
        lbl_value.setStyleSheet(STYLES['label_value'])
        lbl_value.setObjectName("value_label")
        layout.addWidget(lbl_value)
        
        return frame

    def _update_summary_cards(self) -> None:
        if not self._current_metrics:
            return

        total_pnl = sum(m.realized_net for m in self._current_metrics)
        avg_roi = sum(m.roi_pct for m in self._current_metrics) / len(self._current_metrics)
        total_wins = sum(m.wins for m in self._current_metrics)
        total_trades = sum(m.total_trades for m in self._current_metrics)
        win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0.0

        # 실현손익 카드 업데이트
        pnl_label = self.card_pnl.findChild(QLabel, "value_label")
        pnl_label.setText(f"{total_pnl:,.0f}원")
        pnl_color = COLORS['success'] if total_pnl >= 0 else COLORS['danger']
        pnl_label.setStyleSheet(f"color: {pnl_color}; font-size: 18px; font-weight: bold;")

        # ROI 카드 업데이트
        roi_label = self.card_roi.findChild(QLabel, "value_label")
        roi_label.setText(f"{avg_roi:.2f}%")
        roi_color = COLORS['success'] if avg_roi >= 0 else COLORS['danger']
        roi_label.setStyleSheet(f"color: {roi_color}; font-size: 18px; font-weight: bold;")

        # 승률 카드 업데이트
        wr_label = self.card_winrate.findChild(QLabel, "value_label")
        wr_label.setText(f"{win_rate:.1f}%")
        wr_color = COLORS['success'] if win_rate >= 50 else COLORS['warning']
        wr_label.setStyleSheet(f"color: {wr_color}; font-size: 18px; font-weight: bold;")

        # 거래 카드 업데이트
        trades_label = self.card_trades.findChild(QLabel, "value_label")
        trades_label.setText(f"{total_trades}건")

    # ========================= ROI Snapshot =========================

    def _init_pnl_snapshot(self, layout: QVBoxLayout) -> None:
        self._fig_snapshot = Figure(figsize=(6, 3.6), facecolor=COLORS['bg_medium'])  # 세로 ↑
        self._fig_snapshot.subplots_adjust(left=0.08, right=0.96, top=0.88, bottom=0.18)
        self._canvas_snapshot = FigureCanvas(self._fig_snapshot)
        self._canvas_snapshot.setMinimumHeight(240)  # 눌림 방지
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
        vbox.setContentsMargins(8, 8, 8, 8)
        vbox.addWidget(self._canvas_snapshot)
        layout.addWidget(frame)

    def _update_pnl_snapshot(self, roi_value: float) -> None:
        """메인 스레드에서만 호출되어야 함"""
        if not _HAS_MPL:
            return
            
        now = datetime.now()
        self._pnl_snapshots.append((now, roi_value))
        if len(self._pnl_snapshots) > 60:
            self._pnl_snapshots.pop(0)

        if not self._pnl_snapshots:
            return

        try:
            times = [t.strftime("%H:%M:%S") for t, _ in self._pnl_snapshots]
            rois = [r for _, r in self._pnl_snapshots]

            self._ax_snapshot.clear()
            self._ax_snapshot.plot(times, rois, color=COLORS['chart_line'], linewidth=2.5, marker='o', markersize=3)
            self._ax_snapshot.fill_between(range(len(rois)), rois, alpha=0.2, color=COLORS['chart_line'])
            self._ax_snapshot.set_ylabel("ROI (%)", color=COLORS['text_secondary'], fontsize=10)
            
            # x축 레이블 간격 조정
            step = max(1, len(times)//5)
            indices = list(range(0, len(times), step))
            self._ax_snapshot.set_xticks(indices)
            self._ax_snapshot.set_xticklabels([times[i] for i in indices], rotation=0, fontsize=9)
            
            self._ax_snapshot.set_facecolor(COLORS['chart_bg'])
            self._ax_snapshot.grid(True, alpha=0.15, color=COLORS['text_secondary'])
            self._ax_snapshot.spines['top'].set_visible(False)
            self._ax_snapshot.spines['right'].set_visible(False)
            self._ax_snapshot.spines['left'].set_color(COLORS['border'])
            self._ax_snapshot.spines['bottom'].set_color(COLORS['border'])
            
            # draw_idle()만 호출 - tight_layout() 제거
            self._canvas_snapshot.draw_idle()
        except Exception as e:
            logger.debug(f"PnL snapshot update error: {e}")

    # ========================= 컨트롤바 =========================

    def _create_control_bar(self, layout: QVBoxLayout) -> None:
        bar = QHBoxLayout()
        bar.setSpacing(12)

        self.btn_refresh = QPushButton("🔄 데이터 새로고침")
        self.btn_refresh.setStyleSheet(STYLES['button'])
        self.btn_refresh.clicked.connect(self._on_refresh_clicked)
        bar.addWidget(self.btn_refresh)

        self.btn_report = QPushButton("📄 일일 리포트")
        self.btn_report.setStyleSheet(STYLES['button'])
        self.btn_report.clicked.connect(self._on_daily_report)
        bar.addWidget(self.btn_report)
        
        bar.addStretch()

        self.lbl_status = QLabel("● 준비")
        self.lbl_status.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px; margin-left: 12px;")
        bar.addWidget(self.lbl_status)

        layout.addLayout(bar)

    # ========================= 탭 구성 =========================

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
        # 스크롤 가능하도록 ResizeToContents로 변경
        self.tbl_performance.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_performance.horizontalHeader().setStretchLastSection(True)
        self.tbl_performance.verticalHeader().setVisible(False)
        self.tbl_performance.setMinimumWidth(800)  # 최소 너비 설정
        layout.addWidget(self.tbl_performance)

    def _create_positions_tab(self, tab: QWidget) -> None:
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        
        self.tbl_positions = QTableWidget(0, 10)
        self.tbl_positions.setStyleSheet(STYLES['table'])
        self.tbl_positions.setAlternatingRowColors(True)
        self.tbl_positions.setHorizontalHeaderLabels([
            "종목코드", "보유수량", "평단가", "마지막매수가", "마지막매도가",
            "누적손익", "매수일", "매도일", "총거래", "총승리"
        ])
        # 스크롤 가능하도록 ResizeToContents로 변경
        self.tbl_positions.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_positions.horizontalHeader().setStretchLastSection(True)
        self.tbl_positions.verticalHeader().setVisible(False)
        self.tbl_positions.setMinimumWidth(800)  # 최소 너비 설정
        layout.addWidget(self.tbl_positions)

    def _create_risk_tab(self, tab: QWidget) -> None:
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        
        info_label = QLabel("⚠️ 리스크 관련 지표는 추후 확장 예정")
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
        btn_reload.clicked.connect(self._update_history_chart)
        layout.addWidget(btn_reload)

    # ========================= 히스토리 차트 =========================

    def _update_history_chart(self) -> None:
        """메인 스레드에서만 호출되어야 함"""
        if not _HAS_MPL:
            return
            
        try:
            self._ax_history.clear()
            files = sorted(self.result_base_dir.glob("trading_result_*.json"))
            recent = files[-7:] if len(files) > 7 else files
            days, pnls = [], []

            for f in recent:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    pnl = data.get("summary", {}).get("realized_pnl_net", 0.0)
                    days.append(f.stem.split("_")[-1])
                    pnls.append(pnl)
                except Exception:
                    continue

            if not days:
                self._ax_history.text(0.5, 0.5, '데이터 없음', 
                                     ha='center', va='center', 
                                     color=COLORS['text_secondary'],
                                     transform=self._ax_history.transAxes)
                self._canvas_history.draw_idle()
                return

            colors = [COLORS['success'] if p >= 0 else COLORS['danger'] for p in pnls]
            self._ax_history.bar(days, pnls, color=colors, alpha=0.8, edgecolor=COLORS['border'])
            self._ax_history.axhline(y=0, color=COLORS['text_secondary'], linestyle='--', linewidth=1, alpha=0.5)
            self._ax_history.set_title("최근 7일 손익 추이", color=COLORS['text_primary'], fontsize=12, pad=10)
            self._ax_history.set_ylabel("실현손익 (₩)", color=COLORS['text_secondary'], fontsize=10)
            self._ax_history.tick_params(colors=COLORS['text_secondary'], labelsize=9)
            self._ax_history.set_facecolor(COLORS['chart_bg'])
            self._ax_history.spines['top'].set_visible(False)
            self._ax_history.spines['right'].set_visible(False)
            self._ax_history.spines['left'].set_color(COLORS['border'])
            self._ax_history.spines['bottom'].set_color(COLORS['border'])
            self._ax_history.grid(True, alpha=0.15, axis='y', color=COLORS['text_secondary'])
            
            # draw_idle()만 호출 - tight_layout() 제거
            self._canvas_history.draw_idle()
        except Exception as e:
            logger.debug(f"History chart update error: {e}")

    # ========================= 알림 패널 =========================

    def _create_alert_panel(self, layout: QVBoxLayout) -> None:
        frame = QFrame()
        frame.setStyleSheet(f"""
            QFrame {{
                background-color: {COLORS['bg_light']};
                border-left: 4px solid {COLORS['warning']};
                border-radius: 6px;
                padding: 8px;  /* 12 → 8로 살짝 축소 */
            }}
        """)

        # ⬇️ 알림 영역 크기 제한
        frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        frame.setMaximumHeight(90)   # 원하는 높이(예: 70~120 사이)로 조절

        vbox = QVBoxLayout(frame)
        vbox.setSpacing(6)  # 8 → 6
        lbl = QLabel("⚠️ 알림")
        lbl.setStyleSheet(f"font-weight: bold; color: {COLORS['warning']}; font-size: 13px;")
        vbox.addWidget(lbl)
        
        self.lbl_alerts = QLabel("알림 없음")
        self.lbl_alerts.setWordWrap(True)
        self.lbl_alerts.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        vbox.addWidget(self.lbl_alerts)
        
        layout.addWidget(frame)

    # ========================= 타이머 =========================

    def _init_timer(self) -> None:
        self._timer = QTimer(self)   # parent=self → UI 스레드 소유
        self._timer.setInterval(self._poll_ms)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()



    @Slot()
    def _on_store_updated(self) -> None:
        if getattr(self, "_refresh_pending", False):
            return
        self._refresh_pending = True
        QTimer.singleShot(120, self._safe_refresh)

    
    @Slot()
    def _safe_refresh(self) -> None:
        try:
            self.refresh(force=True)
        finally:
            self._refresh_pending = False

    # ========================= 리프레시 =========================

    def refresh(self, force: bool = False) -> None:
        try:
            daily_path = self.store.daily_path
            cumulative_path = self.store.cumulative_path

            if not daily_path.exists():
                self._paint_empty_state()
                return

            fi_daily = QFileInfo(str(daily_path))
            mtime_daily = int(fi_daily.lastModified().toSecsSinceEpoch())

            if force or mtime_daily != self._last_daily_mtime:
                self._last_daily_mtime = mtime_daily
                daily_data = json.loads(daily_path.read_text(encoding="utf-8"))
                self._update_from_daily(daily_data)

                total_roi = sum(s.roi_pct for s in self._current_metrics) / len(self._current_metrics) if self._current_metrics else 0.0
                
                self._update_summary_cards()
                
                if _HAS_MPL:
                    self._update_pnl_snapshot(total_roi)

                self._paint_all_views()
                self.lbl_status.setText(f"● 갱신됨: {daily_path.name}")
                self.lbl_status.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px;")
        except Exception:
            logger.exception("Dashboard refresh error")
            self.lbl_status.setText("● 오류 발생")
            self.lbl_status.setStyleSheet(f"color: {COLORS['danger']}; font-size: 12px;")

    # ========================= 데이터 반영 =========================

    def _update_from_daily(self, data: Dict[str, Any]) -> None:
        metrics = []
        for name, s in data.get("strategies", {}).items():
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

    def _update_from_cumulative(self, data: Dict[str, Any]) -> None:
        positions = []
        for code, s in data.get("symbols", {}).items():
            positions.append(PositionInfo(
                code=code,
                qty=int(s.get("qty", 0)),
                avg_price=float(s.get("avg_price", 0.0)),
                last_buy_price=float(s.get("last_buy_price", 0.0)),
                last_buy_date=s.get("last_buy_date", ""),
                last_sell_price=float(s.get("last_sell_price", 0.0)),
                last_sell_date=s.get("last_sell_date", ""),
                cumulative_pnl=float(s.get("cumulative_realized_net", 0.0)),
                total_trades=int(s.get("total_trades", 0)),
                total_wins=int(s.get("total_wins", 0))
            ))
        self._current_positions = positions

    # ========================= 테이블 렌더링 =========================

    def _paint_all_views(self) -> None:
        tbl = self.tbl_performance
        tbl.setRowCount(len(self._current_metrics))
        
        for i, m in enumerate(self._current_metrics):
            # 데이터 설정
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
                
                # 색상 적용
                if value is not None:
                    if col == 1 or col == 2:  # 실현손익, ROI
                        color = QColor(COLORS['success']) if value >= 0 else QColor(COLORS['danger'])
                        item.setForeground(color)
                    elif col == 3:  # 승률
                        color = QColor(COLORS['success']) if value >= 50 else QColor(COLORS['warning'])
                        item.setForeground(color)
                    elif col == 7:  # PF
                        color = QColor(COLORS['success']) if value >= 1.5 else QColor(COLORS['warning'])
                        item.setForeground(color)
                
                tbl.setItem(i, col, item)

        # 포지션 테이블
        tbl2 = self.tbl_positions
        tbl2.setRowCount(len(self._current_positions))
        
        for i, p in enumerate(self._current_positions):
            items = [
                p.code,
                str(p.qty),
                f"{p.avg_price:,.0f}",
                f"{p.last_buy_price:,.0f}",
                f"{p.last_sell_price:,.0f}",
                (f"{p.cumulative_pnl:,.0f}", p.cumulative_pnl),
                p.last_buy_date,
                p.last_sell_date,
                str(p.total_trades),
                str(p.total_wins)
            ]
            
            for col, data in enumerate(items):
                if isinstance(data, tuple):
                    text, value = data
                    item = QTableWidgetItem(text)
                    color = QColor(COLORS['success']) if value >= 0 else QColor(COLORS['danger'])
                    item.setForeground(color)
                else:
                    item = QTableWidgetItem(data)
                
                item.setTextAlignment(Qt.AlignCenter)
                tbl2.setItem(i, col, item)

    def _paint_empty_state(self) -> None:
        self.tbl_performance.setRowCount(0)
        self.tbl_positions.setRowCount(0)
        self.lbl_status.setText("⚠️ 데이터 없음")
        self.lbl_status.setStyleSheet(f"color: {COLORS['warning']}; font-size: 12px;")

    # ========================= 기타 핸들러 =========================

    def _on_refresh_clicked(self) -> None:
        self.refresh(force=True)
        if _HAS_MPL:
            self._update_history_chart()
        self.lbl_status.setText("● 새로고침 완료")
        self.lbl_status.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px;")

    def _handle_alert(self, alert_type: str, message: str, data: Dict[str, Any]) -> None:
        """알림 핸들러 - 메인 스레드에서 안전하게 실행"""
        # 메인 스레드에서 UI 업데이트만 수행 (블로킹 다이얼로그 제거)
        QMetaObject.invokeMethod(
            self, 
            "_update_alert_ui",
            Qt.QueuedConnection,
            Q_ARG(str, alert_type),
            Q_ARG(str, message)
        )
    
    @Slot(str, str)
    def _update_alert_ui(self, alert_type: str, message: str) -> None:
        """UI 업데이트 (메인 스레드) - Non-blocking"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_msg = f"[{timestamp}] {message}"
        self._alert_messages.append(formatted_msg)
        
        # 최근 5개만 유지
        if len(self._alert_messages) > 5:
            self._alert_messages = self._alert_messages[-5:]
        
        recent_alerts = "\n".join(self._alert_messages)
        self.lbl_alerts.setText(recent_alerts)
        
        # 알림 타입에 따라 색상 변경
        if "critical" in alert_type.lower() or "error" in alert_type.lower():
            color = COLORS['danger']
        elif "warning" in alert_type.lower():
            color = COLORS['warning']
        else:
            color = COLORS['text_secondary']
            
        self.lbl_alerts.setStyleSheet(f"color: {color}; font-size: 12px;")

    def _clear_alerts(self) -> None:
        self._alert_messages.clear()
        self.lbl_alerts.setText("알림 없음")
        self.lbl_alerts.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")


    def shutdown(self):
        try:
            if hasattr(self, "_timer") and self._timer.isActive():
                self._timer.stop()
        except Exception:
            pass
        try:
            if hasattr(self, "watcher") and self.watcher:
                self.watcher.stop()  # 내부 python thread join
        except Exception:
            pass

    def closeEvent(self, e):
        self.shutdown()
        super().closeEvent(e)
