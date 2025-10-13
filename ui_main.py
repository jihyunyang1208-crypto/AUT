# ui_main.py 

import os
import sys
import logging
from typing import Optional, Dict, Any
from datetime import datetime

import pandas as pd

# Qt
from PySide6.QtCore import (
    Qt, QTimer, Signal, Slot, QObject, QModelIndex, QSettings, QUrl, QSortFilterProxyModel, QAbstractTableModel
)
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QMessageBox,
    QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QStatusBar,
    QTableView, QHeaderView, QLineEdit, QToolBar, QListWidget,
    QTextEdit, QListWidgetItem, QTextBrowser, QSplitter, QCheckBox,
    QComboBox, QGroupBox, QScrollArea, QFrame, QProgressBar, QTabWidget
)


try:
    from matplotlib.dates import DateFormatter
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    import matplotlib
    from matplotlib.ticker import FuncFormatter

    matplotlib.rc('font', family='Malgun Gothic')
    matplotlib.rc('axes', unicode_minus=False)  # 마이너스 기호 깨짐 방지

    _HAS_MPL = True
except Exception:
    _HAS_MPL = False

# ---- 외부 다이얼로그 ----
try:
    from core.macd_dialog import MacdDialog
except Exception:
    MacdDialog = None
import subprocess
from pathlib import Path
from PySide6.QtWidgets import QFileDialog
from PySide6.QtGui import QDesktopServices


from trading_report.report_dialog import ReportDialog
# 설정 / 와이어링 (구버전 호환)
try:
    from setting.settings_manager import SettingsStore, SettingsDialog
    from setting.wiring import AppWiring
except Exception:
    class _DummyStore:
        def load(self): return type("Cfg", (), {})()
        def save(self, _): pass
    SettingsStore = _DummyStore
    SettingsDialog = None
    AppWiring = None


logger = logging.getLogger("ui_main")
logging.getLogger("matplotlib.font_manager").setLevel(logging.WARNING)


# ----------------------------
# DataFrame → Qt 모델
# ----------------------------
class DataFrameModel(QAbstractTableModel):
    def __init__(self, df: pd.DataFrame = pd.DataFrame(), parent=None):
        super().__init__(parent)
        self._df = df.copy()

    def setDataFrame(self, df: pd.DataFrame):
        self.beginResetModel()
        self._df = df.copy()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()):
        return 0 if parent.isValid() else len(self._df)

    def columnCount(self, parent: QModelIndex = QModelIndex()):
        return 0 if parent.isValid() else len(self._df.columns)

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or role not in (Qt.DisplayRole, Qt.EditRole, Qt.ToolTipRole):
            return None
        value = self._df.iat[index.row(), index.column()]
        return "" if pd.isna(value) else str(value)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            try:
                return str(self._df.columns[section])
            except Exception:
                return ""
        return ""


# ----------------------------
# 메인 윈도우
# ----------------------------
class MainWindow(QMainWindow):
    # 외부 스레드 → UI 프록시
    sig_new_stock_detail = Signal(dict)
    sig_trade_signal = Signal(dict)

    def __init__(
        self,
        bridge=None,
        engine=None,
        perform_filtering_cb=None,
        project_root: str = ".",
        wiring: Optional[AppWiring] = None
    ):
        super().__init__()
        self.setWindowTitle("오트 · 조건검색 & 리스크 대시보드")
        self.resize(1280, 860)

        # 주입
        self.bridge = bridge
        self.engine = engine
        self.perform_filtering_cb = perform_filtering_cb or (lambda: None)
        self.project_root = project_root
        self.project_root = self._resolve_project_root(self.project_root)
        self._last_report_path: Optional[str] = None

        self.wiring = wiring

        # 상태
        self._result_rows: list[dict] = []
        self._result_index: dict[str, int] = {}
        self._macd_dialogs: dict[str, QDialog] = {}
        self._active_macd_streams: set[str] = set()
        self._last_stream_req_ts: dict[str, Any] = {}
        self._stream_debounce_sec = 15
        self._cond_seq_to_name: dict[str, str] = {}     # "12" -> "20일 신고가"
        self._code_to_conds: dict[str, set[str]] = {}   # "005930" -> {"12:20일 신고가", "34:거래량 급증"}

        # UI 구성
        self._build_toolbar()
        self._build_layout()
        self._build_risk_panel()
        self._apply_stylesheet()

        # 상태바/시계
        self.status = QStatusBar(); self.setStatusBar(self.status)
        self.status.showMessage("준비됨")
        self.label_new_stock = QLabel("신규 종목 없음")
        self.status.addPermanentWidget(self.label_new_stock)
        self._start_clock()

        # 시그널 연결
        self._connect_signals()

        # 초기화
        if hasattr(self.engine, "start_loop"):
            try:
                self.engine.start_loop()
            except Exception:
                pass
        self.load_candidates()

        # 설정 저장/복원
        self._settings_qs = QSettings("Trade", "AutoTraderUI")
        try:
            state = self._settings_qs.value("hsplit_state")
            if state is not None:
                self.hsplit.restoreState(state)
            w = self._settings_qs.value("window_width"); h = self._settings_qs.value("window_height")
            maximized = self._settings_qs.value("window_maximized")
            if str(maximized).lower() in ("true", "1", "yes"):
                self.showMaximized()
            elif w and h:
                self.resize(int(w), int(h))
        except Exception:
            pass

        # 앱 설정 로드 및 UI 토글 동기화
        self.store = SettingsStore() if SettingsStore else None
        self.app_cfg = self.store.load() if self.store else type("Cfg", (), {})()
        if self.wiring and hasattr(self.wiring, "apply_settings"):
            try:
                self.wiring.apply_settings(self.app_cfg)
            except Exception:
                pass

        # 리스크 패널 토글 복원
        vis = self._settings_qs.value("risk_panel_visible", True)
        vis = (str(vis).lower() in ("true","1","yes")) if not isinstance(vis, bool) else vis
        self._toggle_risk_panel(bool(vis))
        self.act_toggle_risk.setChecked(bool(vis))

    # ---------------- UI 빌드 ----------------
    def _build_toolbar(self):
        tb = QToolBar("Main"); tb.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, tb)

        act_init = tb.addAction("초기화"); act_init.setShortcut("Ctrl+I"); act_init.triggered.connect(self.on_click_init)
        tb.addSeparator()
        act_start = tb.addAction("조건 시작"); act_start.setShortcut("Ctrl+S"); act_start.triggered.connect(self.on_click_start_condition)
        act_stop  = tb.addAction("조건 중지"); act_stop.setShortcut("Ctrl+E"); act_stop.triggered.connect(self.on_click_stop_condition)
        tb.addSeparator()
        act_filter = tb.addAction("필터 실행"); act_filter.setShortcut("Ctrl+F"); act_filter.triggered.connect(self.on_click_filter)
        act_refresh = tb.addAction("후보 새로고침"); act_refresh.setShortcut("F5"); act_refresh.triggered.connect(self.load_candidates)
        tb.addSeparator()
        self.btn_settings = tb.addAction("환경설정…")

        # 리스크 패널 토글
        tb.addSeparator()
        self.act_toggle_risk = tb.addAction("리스크패널")
        self.act_toggle_risk.setCheckable(True)
        self.act_toggle_risk.setChecked(True)
        self.act_toggle_risk.toggled.connect(self._toggle_risk_panel)

    def _build_layout(self):
        central = QWidget(); self.setCentralWidget(central)
        root = QHBoxLayout(central); root.setContentsMargins(8,8,8,8); root.setSpacing(8)

        main_split = QSplitter(Qt.Horizontal); root.addWidget(main_split)

        # 좌측: 컨트롤/리스트
        left_panel = QWidget(); left = QVBoxLayout(left_panel)
        self.search_conditions = QLineEdit(placeholderText="조건식 검색…")
        self.btn_init = QPushButton("초기화 (토큰+WS 연결)")
        self.btn_start = QPushButton("선택 조건 시작")
        self.btn_stop  = QPushButton("선택 조건 중지")
        self.btn_filter = QPushButton("종목 필터링 실행 (재무+기술)")
        self.list_conditions = QListWidget()
        self.lbl_cond_info = QLabel("0개 / 선택: 0")

        left.addWidget(self.search_conditions)
        left.addWidget(QLabel("조건식 목록"))
        left.addWidget(self.list_conditions, 1)
        left.addWidget(self.btn_init)
        left.addWidget(self.btn_filter)
        left.addWidget(self.lbl_cond_info)
        row_btns = QHBoxLayout(); row_btns.addWidget(self.btn_start); row_btns.addWidget(self.btn_stop)
        left.addLayout(row_btns)

        # 우측: 결과/로그 영역
        right_panel = QWidget(); right = QVBoxLayout(right_panel)
        vsplit = QSplitter(Qt.Vertical); right.addWidget(vsplit, 1)

        # 상단 좌/우
        hsplit = QSplitter(Qt.Horizontal); vsplit.addWidget(hsplit); self.hsplit = hsplit

        # 상단-좌: 후보 테이블
        pane_top_left = QWidget(); top_left = QVBoxLayout(pane_top_left)
        self.search_candidates = QLineEdit(placeholderText="후보 종목 실시간 검색…")
        top_left.addWidget(self.search_candidates)

        self.cand_table = QTableView()
        self.cand_table.horizontalHeader().setStretchLastSection(True)
        self.cand_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.cand_table.setAlternatingRowColors(True)
        self.cand_table.verticalHeader().setVisible(False)

        # ✅ 후보 테이블 모델/프록시 연결 (구버전 호환)
        self.cand_model = DataFrameModel(pd.DataFrame(columns=["회사명", "종목코드", "현재가"]))
        self.cand_proxy = QSortFilterProxyModel(self)
        self.cand_proxy.setSourceModel(self.cand_model)
        self.cand_proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.cand_proxy.setFilterKeyColumn(-1)
        self.cand_table.setModel(self.cand_proxy)
        self.cand_table.setSortingEnabled(False)
        self.cand_table.setSelectionBehavior(QTableView.SelectRows)
        self.cand_table.setCornerButtonEnabled(False)
        top_left.addWidget(self.cand_table, 1)

        # 상단-우: 종목 검색 결과 (HTML 테이블 렌더)
        pane_top_right = QWidget(); top_right = QVBoxLayout(pane_top_right)
        header_row = QHBoxLayout()
        header_row.addStretch(1)
        top_right.addLayout(header_row)

        sort_row = QHBoxLayout()
        self.cmb_sort_key = QComboBox()
        self.cmb_sort_key.addItems(["등락률(%)", "현재가", "거래량", "매수가", "매도가", "코드", "이름", "최근 갱신시간", "조건식"])
        self.cmb_sort_key.setCurrentText("최근 갱신시간")
        self.btn_sort_dir = QPushButton("내림차순")
        self.btn_sort_dir.setCheckable(True)
        self.btn_sort_dir.setChecked(True)
        sort_row.addWidget(QLabel("정렬:")); sort_row.addWidget(self.cmb_sort_key); sort_row.addWidget(self.btn_sort_dir); sort_row.addStretch(1)
        top_right.addLayout(sort_row)

        self.text_result = QTextBrowser(); self.text_result.setOpenExternalLinks(False); self.text_result.setOpenLinks(False); self.text_result.setReadOnly(True)
        self.text_result.anchorClicked.connect(self._on_result_anchor_clicked)
        top_right.addWidget(self.text_result, 1)

        tab_top = QTabWidget()
        tab_top.setDocumentMode(True)
        tab_top.setMovable(True)
        tab_top.setTabPosition(QTabWidget.North)

        # 다크테마 가독성 향상 스타일
        tab_top.setStyleSheet("""
        QTabWidget::pane {
        border: 1px solid #3a414b; border-radius: 10px; top: -1px; background: #23272e;
        }
        QTabBar::tab {
        background: #2a2f36; color: #cfd6df;
        padding: 10px 18px; margin-right: 6px;
        border: 1px solid #3a414b; border-bottom: 2px solid #3a414b;
        border-top-left-radius: 10px; border-top-right-radius: 10px;
        font-weight: 600;
        }
        QTabBar::tab:hover {
        background: #303641;
        }
        QTabBar::tab:selected {
        background: #343b47; color: #ffffff;
        border-bottom: 2px solid #60a5fa;   /* 선택 탭 하이라이트 */
        }
        QTabBar::tab:!selected {
        color: #aab2bd;
        }
        """)

        tab_top.addTab(pane_top_left, "25일이내 급등 종목")
        tab_top.addTab(pane_top_right, "종목 검색 결과")
        tab_top.setCurrentIndex(1)
        hsplit.addWidget(tab_top)

        # 하단 로그
        pane_bottom = QWidget(); bottom = QVBoxLayout(pane_bottom)
        bottom.addWidget(QLabel("로그"))
        self.text_log = QTextEdit(); self.text_log.setReadOnly(True)
        bottom.addWidget(self.text_log, 1)
        vsplit.addWidget(pane_bottom)
        vsplit.setSizes([540, 220])

        main_split.addWidget(left_panel)
        main_split.addWidget(right_panel)

        # 우측에 리스크 패널 고정 추가 (홀더에 장착)
        self.risk_panel_holder = QWidget()
        holder_lay = QVBoxLayout(self.risk_panel_holder)
        holder_lay.setContentsMargins(0,0,0,0)
        main_split.addWidget(self.risk_panel_holder)
        main_split.setSizes([380, 800, 360])

    def _build_risk_panel(self):
        risk = QGroupBox("리스크 대시보드"); risk.setObjectName("riskBox")
        lay = QVBoxLayout(risk); lay.setContentsMargins(10, 10, 10, 10); lay.setSpacing(10)

        # KPI 칩 + 상태 배지
        row = QHBoxLayout()
        self.kpi_equity = QLabel("Equity +0.00%")
        self.kpi_daily  = QLabel("Today +0.00%")
        self.kpi_mdd    = QLabel("MDD 0.00%")
        for w in (self.kpi_equity, self.kpi_daily, self.kpi_mdd):
            w.setStyleSheet("QLabel { background:#23272e; border:1px solid #3a414b; border-radius:999px; padding:6px 10px; color:#dbe3ec; font-weight:600; }")
        row.addWidget(self.kpi_equity); row.addWidget(self.kpi_daily); row.addWidget(self.kpi_mdd); row.addStretch(1)

        self.lbl_risk_status = QLabel("SAFE")
        self._apply_risk_badge("safe")
        row.addWidget(self.lbl_risk_status)
        lay.addLayout(row)

                # 📄 데일리 매매리포트 버튼 줄
        btn_row = QHBoxLayout()
        self.btn_daily_report = QPushButton("📄 데일리 매매리포트")
        self.btn_daily_report.clicked.connect(self.on_click_open_last_report)
        #self.btn_daily_report_open = QPushButton("열기")
        #self.btn_daily_report_open.clicked.connect(self.on_click_open_last_report)
        btn_row.addWidget(self.btn_daily_report)
        #btn_row.addWidget(self.btn_daily_report_open)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)


        # 익스포저 게이지
        lay.addWidget(QLabel("총 포지션 비중(순자산 대비, %)"))
        self.pb_exposure = QProgressBar(); self.pb_exposure.setRange(0, 200); self.pb_exposure.setValue(0)

        lay.addWidget(self.pb_exposure)
        self._update_exposure_gauge(0)

        # 차트
        if _HAS_MPL:
            money_fmt = FuncFormatter(lambda x, pos: f"{int(x):,}")

            # ── [차트1] 계좌 손익곡선 (Equity Curve) ─────────────────
            fig1 = Figure(figsize=(4, 2.2), tight_layout=True, facecolor="#000000")
            self.canvas_equity = FigureCanvas(fig1)
            self.ax_equity = fig1.add_subplot(111)
            self.ax_equity.set_facecolor("#000000")
            self.ax_equity.tick_params(colors="#e9edf1")
            self.ax_equity.title.set_color("#e9edf1")
            self.ax_equity.set_title(
                "20일 손익",
                color="#e9edf1",
                fontsize=8,
                fontweight="bold",
            )
            self.ax_equity.set_xlabel("일자");  self.ax_equity.xaxis.label.set_color("#cfd6df")
            self.ax_equity.set_ylabel("순자산"); self.ax_equity.yaxis.label.set_color("#cfd6df")
            self.ax_equity.yaxis.set_major_formatter(money_fmt)
            for s in self.ax_equity.spines.values(): s.set_color("#555")
            self.ax_equity.grid(True, which="major", alpha=0.25, color="#555")
            lay.addWidget(self.canvas_equity)

            # ── [차트2] 일일 손익 (Daily P/L) ───────────────────────
            fig2 = Figure(figsize=(4, 1.8), tight_layout=True, facecolor="#000000")
            self.canvas_hist = FigureCanvas(fig2)
            self.ax_hist = fig2.add_subplot(111)
            self.ax_hist.set_facecolor("#000000")
            self.ax_hist.tick_params(colors="#e9edf1")
            self.ax_hist.title.set_color("#e9edf1")
            self.ax_hist.set_title(
                "일일 손익",
                color="#e9edf1",
                fontsize=8,
                fontweight="bold",
            )

            self.ax_hist.set_xlabel("일자"); self.ax_hist.xaxis.label.set_color("#cfd6df")
            self.ax_hist.set_ylabel("손익"); self.ax_hist.yaxis.label.set_color("#cfd6df")
            self.ax_hist.yaxis.set_major_formatter(money_fmt)
            for s in self.ax_hist.spines.values(): s.set_color("#555")
            self.ax_hist.grid(True, which="major", alpha=0.25, color="#555")
            lay.addWidget(self.canvas_hist)
        else:
            lay.addWidget(QLabel("(차트 라이브러리 없음 – 손익곡선/히스토그램 생략)"))

        # 전략 카드뷰
        card_box = QGroupBox("전략별 손익"); card_box.setObjectName("cardBox")
        card_lay = QVBoxLayout(card_box); card_lay.setContentsMargins(8, 8, 8, 8); card_lay.setSpacing(6)

        self._strategy_cards_container = QWidget()
        self._strategy_cards_layout = QVBoxLayout(self._strategy_cards_container)
        self._strategy_cards_layout.setContentsMargins(0, 0, 0, 0)
        self._strategy_cards_layout.setSpacing(8)
        self._strategy_cards_layout.addStretch(1)

        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(self._strategy_cards_container)
        card_lay.addWidget(scroll)
        lay.addWidget(card_box)

        # 패널 장착
        self._strategy_card_widgets: Dict[str, QFrame] = {}
        self.risk_panel = risk
        self.risk_panel_holder.layout().addWidget(self.risk_panel)

    # ---------------- 스타일 ----------------
    def _apply_stylesheet(self):
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #1e2126; color: #e9edf1; }
            QLineEdit, QTextEdit, QListWidget, QTableView, QTextBrowser {
                background: #23272e; color: #e9edf1; border: 1px solid #3a414b;
                selection-background-color: #2f3742; selection-color: #ffffff;
                border-radius: 8px; padding: 6px 8px;
            }
            QHeaderView::section {
                background: #262b33; color: #e0e6ee; border: 0px; padding: 8px 10px;
                border-bottom: 1px solid #3a414b;
            }
            QPushButton {
                background: #2a2f36; border: 1px solid #3a414b; padding: 7px 12px;
                border-radius: 10px;
            }
            QPushButton:hover { background: #2f3540; }
            QPushButton:pressed { background: #272c33; }
            QPushButton:disabled { color: #8b93a0; border-color: #373d46; }
            QGroupBox#riskBox, QGroupBox#cardBox {
                background: #23272e; border: 1px solid #3a414b; border-radius: 12px;
                margin-top: 12px;
            }
            QGroupBox#riskBox::title, QGroupBox#cardBox::title {
                subcontrol-origin: margin; left: 12px; padding: 0 6px;
                color: #aab2bd; font-weight: 600;
            }
            QSplitter::handle { background: #2a2f36; }
            QStatusBar { background: #1a1d22; color: #cfd6df; }
            """
        )

    # ---------------- 시계/종료 ----------------
    def _start_clock(self):
        self._clock = QLabel(); self.status.addPermanentWidget(self._clock)
        t = QTimer(self); t.timeout.connect(lambda: self._clock.setText(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        t.start(1000); self._clock_timer = t

    def closeEvent(self, event):
        try:
            if hasattr(self, "hsplit"):
                try:
                    self._settings_qs.setValue("hsplit_state", self.hsplit.saveState())
                except Exception:
                    pass
            # 저장: 창 크기/리스크 패널 상태
            try:
                self._settings_qs.setValue("window_maximized", self.isMaximized())
                if not self.isMaximized():
                    self._settings_qs.setValue("window_width", self.width())
                    self._settings_qs.setValue("window_height", self.height())
                self._settings_qs.setValue("risk_panel_visible", bool(self.risk_panel.isVisible()))
            except Exception:
                pass
            # cfg 동기화
            try:
                setattr(self.app_cfg, "window_maximized", bool(self.isMaximized()))
                if not self.isMaximized():
                    setattr(self.app_cfg, "window_width", int(self.width()))
                    setattr(self.app_cfg, "window_height", int(self.height()))
                if self.store:
                    self.store.save(self.app_cfg)
            except Exception:
                pass
            # 엔진 종료/스트림 정리
            if self.engine is not None and hasattr(self.engine, "shutdown"):
                try:
                    self.engine.shutdown()
                except Exception:
                    pass
            for code6 in list(self._active_macd_streams):
                if self.engine is not None and hasattr(self.engine, "stop_macd_stream"):
                    try:
                        self.engine.stop_macd_stream(code6)
                    except Exception:
                        pass
        finally:
            event.accept()

    # ---------------- 시그널 연결 ----------------
    def _connect_signals(self):
        # 버튼/액션
        self.btn_init.clicked.connect(self.on_click_init)
        self.btn_start.clicked.connect(self.on_click_start_condition)
        self.btn_stop .clicked.connect(self.on_click_stop_condition)
        self.btn_filter.clicked.connect(self.on_click_filter)
        if self.btn_settings: self.btn_settings.triggered.connect(self.on_open_settings_dialog)

        # 입력/목록
        self.search_candidates.textChanged.connect(self._filter_candidates)
        self.search_conditions.textChanged.connect(self._filter_conditions)
        self.list_conditions.itemSelectionChanged.connect(self._update_cond_info)

        # 브리지(가드)
        if self.bridge is not None:
            for name, slot in [
                ("log", self.append_log),
                ("condition_list_received", self.populate_conditions),
                ("macd_series_ready", self.on_macd_series_ready),
                ("macd_data_received", self.on_macd_data),
                ("new_stock_received", self.on_new_stock),
                ("token_ready", self._on_token_ready),
                ("new_stock_detail_received", self.on_new_stock_detail),
            ]:
                if hasattr(self.bridge, name):
                    try: getattr(self.bridge, name).connect(slot)
                    except Exception: pass
            # 리스크 스냅샷 (신버전 브리지)
            if hasattr(self.bridge, "pnl_snapshot_ready"):
                try:
                    self.bridge.pnl_snapshot_ready.connect(self.on_pnl_snapshot, Qt.UniqueConnection)
                except Exception:
                    self.bridge.pnl_snapshot_ready.connect(self.on_pnl_snapshot)

        # 비UI→UI
        self.sig_new_stock_detail.connect(self.on_new_stock_detail)
        self.sig_trade_signal.connect(self.on_trade_signal)

        # 엔진 초기화 완료
        if self.engine is not None and hasattr(self.engine, "initialization_complete"):
            try:
                self.engine.initialization_complete.connect(self.on_initialization_complete)
            except Exception:
                pass

        # 정렬 핸들러
        self.cmb_sort_key.currentIndexChanged.connect(lambda _ : self._render_results_html())
        self.btn_sort_dir.toggled.connect(lambda checked: (self.btn_sort_dir.setText("내림차순" if checked else "오름차순"), self._render_results_html()))

    # ---------------- 토큰 준비 ----------------
    def _on_token_ready(self, token: str):
        try:
            from core.detail_information_getter import DetailInformationGetter, SimpleMarketAPI
        except Exception:
            DetailInformationGetter = None
            SimpleMarketAPI = None
        try:
            if DetailInformationGetter:
                if not hasattr(self, "getter") or self.getter is None:
                    self.getter = DetailInformationGetter(token=token)
                else:
                    self.getter.token = token
            if SimpleMarketAPI:
                if not hasattr(self, "market_api") or self.market_api is None:
                    self.market_api = SimpleMarketAPI(token=token)
                else:
                    self.market_api.set_token(token)
        except Exception:
            pass

    # ---------------- 리스크 스냅샷 수신 ----------------
    @Slot(dict)
    def on_pnl_snapshot(self, snap: dict):
        try:
            port = snap.get("portfolio") or {}
            daily_pct = float(port.get("daily_pnl_pct", 0.0))
            cum_pct   = float(port.get("cum_return_pct", 0.0))
            mdd_pct   = float(port.get("mdd_pct", 0.0))
            gross_pct = float(port.get("gross_exposure_pct", 0.0))

            # KPI
            self.kpi_equity.setText(f"누적 수익률 {cum_pct:+.2f}%")
            self.kpi_daily.setText(f"Today {daily_pct:+.2f}%")
            self.kpi_mdd.setText(f"MDD {mdd_pct:.2f}%")

            # 상태 배지
            level = self._risk_level(daily_pct, mdd_pct, gross_pct)
            self._apply_risk_badge(level)

            # 익스포저 게이지
            self._update_exposure_gauge(gross_pct)

            # 차트
            if _HAS_MPL:
                # 손익곡선
                self.ax_equity.clear()
                eq = port.get("equity_curve") or []
                if eq:
                    xs = [p.get("t") for p in eq][-20:]  # 최근 20개
                    ys = [float(p.get("equity", 0)) for p in eq][-20:]
                    self.ax_equity.plot(xs, ys, linewidth=1.8)
                    self.ax_equity.xaxis.set_major_formatter(DateFormatter("%Y-%m-%d"))

                    if len(xs) > 5:
                        step = max(1, len(xs)//5)
                        self.ax_equity.set_xticks(xs[::step])
                        for t in self.ax_equity.get_xticklabels():
                            t.set_rotation(30)  # 글씨 겹치지 않게 기울임

                self.ax_equity.set_title(
                    "20일 손익",
                    color="#e9edf1",
                    fontsize=8,
                    fontweight="bold",
                    fontname="Malgun Gothic"
                )
                self.ax_equity.grid(True, which="major", alpha=0.25)
                self.ax_equity.tick_params(axis="x", labelsize=8); self.ax_equity.tick_params(axis="y", labelsize=8)
                self.canvas_equity.draw_idle()
                # 일일손익
                self.ax_hist.clear()
                hist = port.get("daily_hist") or []
                if hist:
                    xs = [p.get("d") for p in hist]
                    ys = [float(p.get("pnl", 0)) for p in hist]
                    self.ax_hist.bar(xs, ys)
                    if len(xs) > 8:
                        self.ax_hist.set_xticks(xs[:: max(1, len(xs)//8)])
                        for t in self.ax_hist.get_xticklabels(): t.set_rotation(28)

                self.ax_hist.set_title(
                    "일일 손익",
                    color="#e9edf1",
                    fontsize=8,
                    fontweight="bold",
                    fontname="Malgun Gothic"
                )
                self.ax_hist.grid(True, which="major", alpha=0.25)
                self.ax_hist.tick_params(axis="x", labelsize=8); self.ax_hist.tick_params(axis="y", labelsize=8)
                self.canvas_hist.draw_idle()

            # 전략 카드뷰
            self._update_strategy_cards(snap.get("by_condition") or {})
        except Exception as e:
            self.append_log(f"[UI] on_pnl_snapshot 오류: {e}")

    # ---------------- 전략 카드뷰 ----------------
    def _create_strategy_card(self, cond_id: str) -> QFrame:
        card = QFrame(); card.setFrameShape(QFrame.StyledPanel)
        card.setCursor(Qt.PointingHandCursor)
        card.setStyleSheet("QFrame { background:#2a2f36; border:1px solid #3a414b; border-radius:12px; } QFrame:hover { border-color:#4b5563; }")
        lay = QHBoxLayout(card); lay.setContentsMargins(12,8,12,8); lay.setSpacing(8)
        name = QLabel(cond_id); name.setStyleSheet("font-weight:700;")
        pct  = QLabel("오늘 +0.00% | 누적 +0.00%"); pct.setStyleSheet("color:#c7d0db;")
        meta = QLabel("종목수 0"); meta.setStyleSheet("color:#8b93a0;")
        lay.addWidget(name, 1); lay.addWidget(pct); lay.addWidget(meta)
        card._lbl_name = name; card._lbl_pcts = pct; card._lbl_meta = meta
        return card

    def _paint_strategy_card(self, card: QFrame, daily_pct: float):
        if daily_pct <= -3:
            css = "QFrame { background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #2a1f21, stop:1 #2d1f22); border:1px solid #ef4444; border-radius:12px; }"
        elif daily_pct <= -1:
            css = "QFrame { background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #2a281f, stop:1 #2d271f); border:1px solid #f59e0b; border-radius:12px; }"
        else:
            css = "QFrame { background:#2a2f36; border:1px solid #3a414b; border-radius:12px; }"
        card.setStyleSheet(css + " QFrame:hover { border-color:#4b5563; }")

    def _update_strategy_cards(self, by_cond: dict):
        # 생성/업데이트
        for cond_id, data in by_cond.items():
            card = getattr(self, "_strategy_card_widgets", {}).get(cond_id)
            daily = float(data.get("daily_pnl_pct", 0.0))
            cum   = float(data.get("cum_return_pct", 0.0))
            symbols = int(data.get("symbol_count", len(data.get("positions", []))))
            if card is None:
                card = self._create_strategy_card(cond_id)
                self._strategy_cards_layout.insertWidget(self._strategy_cards_layout.count()-1, card)
                if not hasattr(self, "_strategy_card_widgets"):
                    self._strategy_card_widgets = {}
                self._strategy_card_widgets[cond_id] = card
            card._lbl_pcts.setText(f"오늘 {daily:+.2f}% | 누적 {cum:+.2f}%")
            card._lbl_meta.setText(f"종목수 {symbols}")
            self._paint_strategy_card(card, daily)
        # 제거된 카드 정리
        if hasattr(self, "_strategy_card_widgets"):
            for cond_id in list(self._strategy_card_widgets.keys()):
                if cond_id not in by_cond:
                    w = self._strategy_card_widgets.pop(cond_id)
                    w.setParent(None); w.deleteLater()

    # ---------------- 보조: 배지/게이지/리스크 ----------------
    def _apply_risk_badge(self, level: str):
        mapping = {
            "safe":   ("SAFE",   "rgba(34,197,94,0.12)",  "#22c55e", "rgba(34,197,94,0.4)"),
            "warn":   ("WARN",   "rgba(245,158,11,0.12)", "#f59e0b", "rgba(245,158,11,0.4)"),
            "danger": ("DANGER", "rgba(239,68,68,0.12)",  "#ef4444", "rgba(239,68,68,0.4)"),
        }
        text, bg, fg, bd = mapping.get(level, ("N/A", "rgba(255,255,255,0.06)", "#e9edf1", "rgba(255,255,255,0.2)"))
        self.lbl_risk_status.setText(text)
        self.lbl_risk_status.setStyleSheet(f"QLabel {{ background:{bg}; color:{fg}; border:1px solid {bd}; border-radius:999px; padding:4px 10px; font-weight:700; }}")

    def _update_exposure_gauge(self, pct: float):
        v = max(0, min(200, int(round(pct))))
        self.pb_exposure.setValue(v)
        if v <= 60:
            chunk = "background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #60a5fa, stop:1 #60a5fa);"
        elif v <= 120:
            chunk = "background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #60a5fa, stop:1 #22c55e);"
        else:
            chunk = "background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #f59e0b, stop:1 #ef4444);"
        css = f"QProgressBar{{background:#23272e; border:1px solid #3a414b; border-radius:8px; text-align:center; color:#dbe3ec; height:14px;}} QProgressBar::chunk{{border-radius:8px; margin:0px; {chunk}}}"
        self.pb_exposure.setStyleSheet(css)
        self.pb_exposure.setToolTip("총 익스포저(%) – 120% 이상은 위험 구간")

    def _risk_level(self, daily_pct: float, mdd_pct: float, gross_pct: float) -> str:
        if daily_pct <= -3 or mdd_pct <= -10 or gross_pct >= 120:
            return "danger"
        if daily_pct <= -1 or mdd_pct <= -5 or gross_pct >= 90:
            return "warn"
        return "safe"

    def _toggle_risk_panel(self, visible: bool):
        """리스크 패널 표시/숨김 토글 (툴바 체크박스와 연동)"""
        try:
            # 홀더 단위로 숨김 처리 (레이아웃 리플로우 깔끔)
            if hasattr(self, "risk_panel_holder") and self.risk_panel_holder is not None:
                self.risk_panel_holder.setVisible(bool(visible))
            elif hasattr(self, "risk_panel") and self.risk_panel is not None:
                self.risk_panel.setVisible(bool(visible))
            # QSettings 저장도 여기서 한 번 더 보장(안전)
            self._settings_qs.setValue("risk_panel_visible", bool(visible))
        except Exception as e:
            self.append_log(f"[UI] _toggle_risk_panel 오류: {e}")

    # ---------------- 기존 기능: 로그/조건/신규/렌더 ----------------
    @Slot(str)
    def append_log(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.text_log.append(f"[{ts}] {str(text)}")

    @Slot(list)
    def populate_conditions(self, conditions: list):
        self.list_conditions.clear()
        self._cond_seq_to_name.clear()
        normalized = []
        for cond in (conditions or []):
            if isinstance(cond, dict):
                seq = str(cond.get("seq", "")).strip(); name = str(cond.get("name", "(이름 없음)")).strip()
            elif isinstance(cond, (list, tuple)) and len(cond) >= 2:
                seq = str(cond[0]).strip(); name = str(cond[1]).strip()
            else:
                continue
            if seq or name:
                normalized.append({"seq": seq, "name": name})
                if seq:
                    self._cond_seq_to_name[seq] = name
        for c in normalized:
            item = QListWidgetItem(f"[{c['seq']}] {c['name']}"); item.setData(Qt.UserRole, c['seq'])
            self.list_conditions.addItem(item)
        self._update_cond_info(); self.append_log(f"✅ 조건식 {len(normalized)}개 로드")

    @Slot(object)
    def on_new_stock(self, payload):
        if isinstance(payload, dict):
            code = payload.get("stock_code") or payload.get("code")
            cond_name = payload.get("condition_name") or payload.get("cond_name") or ""
        else:
            code = str(payload)
            cond_name = ""
        code6 = str(code)[-6:].zfill(6)
        if not code6:
            return

        self.label_new_stock.setText(f"신규 종목: {code6}")
        self.status.showMessage(f"신규 종목: {code6} ({cond_name})", 3000)


    @Slot(dict)
    def on_new_stock_detail(self, payload: dict):
        flat = dict(payload)
        # 행 내부 dict가 오는 케이스도 안전 합침
        row0 = None
        if isinstance(flat.get("open_pric_pre_flu_rt"), list) and flat["open_pric_pre_flu_rt"]:
            row0 = flat["open_pric_pre_flu_rt"][0]
        elif isinstance(flat.get("rows"), list) and flat["rows"]:
            row0 = flat["rows"][0]
        if isinstance(row0, dict):
            for k, v in row0.items():
                flat.setdefault(k, v)

        code = (flat.get("stock_code") or flat.get("code") or "").strip()
        name = flat.get("stock_name") or flat.get("stk_nm") or flat.get("isu_nm") or "종목명 없음"

        cond_name = (
            flat.get("condition_name")
            or flat.get("cond_name")
            or ""
        ).strip()

        def _num(*keys):
            for k in keys:
                v = flat.get(k)
                if v not in (None, "", "-"):
                    try:
                        return float(str(v).replace(",", "").replace("%", ""))
                    except Exception:
                        pass
            return None

        price = _num("cur_prc", "stck_prpr", "price")
        rt    = _num("flu_rt", "prdy_ctrt") or 0.0
        vol   = _num("now_trde_qty", "acml_vol", "trqu")
        code6 = str(code)[-6:].zfill(6) if code else ""
        if not code6:
            return

        updated_at = datetime.now().isoformat(timespec="seconds")

        row = {
            "code": code6,
            "name": name,
            "price": price,
            "rt": rt,
            "vol": vol,
            "buy_price": None,
            "sell_price": None,
            "updated_at": updated_at,
            "conds": cond_name or "-",
        }


        idx = self._result_index.get(code6)
        if idx is None:
            self._result_index[code6] = len(self._result_rows); self._result_rows.append(row)
            idx = self._result_index[code6]
        else:
            keep_buy = self._result_rows[idx].get("buy_price")
            keep_sell = self._result_rows[idx].get("sell_price")
            if keep_buy is not None: 
                row["buy_price"] = keep_buy
            if keep_sell is not None: 
                row["sell_price"] = keep_sell
            self._result_rows[idx] = row


        self._render_results_html()
        self._ensure_macd_stream(code6)


    @Slot(str, float, float, float)
    def on_macd_data(self, code: str, macd: float, signal: float, hist: float):
        code6 = str(code)[-6:].zfill(6)
        self.status.showMessage(f"[MACD] {code6} M:{macd:.2f} S:{signal:.2f} H:{hist:.2f}", 2500)

    @Slot(dict)
    def on_macd_series_ready(self, data: dict):
        # 확장 포인트: 미니차트 등
        pass

    @Slot(dict)
    def on_trade_signal(self, payload: dict):
        try:
            # 사이드/코드/가격 안전 파싱
            side = str(payload.get("side") or payload.get("action") or "").upper()
            code = (payload.get("code") or payload.get("stock_code") or payload.get("stk_cd") or "").strip()
            code6 = str(code)[-6:].zfill(6) if code else ""
            raw_price = (payload.get("price") if "price" in payload else
                         payload.get("limit_price") if "limit_price" in payload else
                         payload.get("cur_price") if "cur_price" in payload else
                         payload.get("prc") if "prc" in payload else
                         payload.get("avg_price"))
            if not code6 or raw_price in (None, ""):
                return
            try:
                price = float(str(raw_price).replace(",", ""))
            except Exception:
                return

            idx = self._result_index.get(code6)
            if idx is None:
                row = {"code":code6, "name":code6, "price":None, "rt":0.0, "vol":None,
                       "buy_price":None, "sell_price":None,
                       "conds": " | ".join(sorted(self._code_to_conds.get(code6, set()))) if code6 in self._code_to_conds else "-",
                       "updated_at": datetime.now().isoformat(timespec="seconds")}
                self._result_index[code6] = len(self._result_rows); self._result_rows.append(row); idx = self._result_index[code6]

            row = self._result_rows[idx]

            # conds 최신화(다른 경로로 들어온 조건 누적 반영)
            if code6 in self._code_to_conds:
                row["conds"] = " | ".join(sorted(self._code_to_conds.get(code6, set()))) or "-"

            if side == "BUY":
                row["buy_price"] = price
            elif side == "SELL":
                row["sell_price"] = price
            else:
                return

            row["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._render_results_html()
        except Exception as e:
            self.append_log(f"[UI] on_trade_signal 오류: {e}")


    # =========================
    # 매매리포트: 경로/루트 유틸
    # =========================
    def _resolve_project_root(self, root_like: str) -> str:
        """
        실행 위치가 어긋나도 실제 프로젝트 루트를 찾아서 사용.
        기준: candidate_stocks.csv 또는 trading_report/ 폴더 존재.
        """
        cand = Path(root_like or ".").resolve()

        def _ok(p: Path) -> bool:
            return (p / "candidate_stocks.csv").exists() or (p / "trading_report").exists()

        if _ok(cand):
            return str(cand)

        here = Path(__file__).resolve().parent
        if _ok(here):
            return str(here)
        if _ok(here.parent):
            return str(here.parent)
        return str(cand)

    def _guess_report_paths(self, date_str: str) -> dict:
        """
        질문에서 제공된 디렉토리 구조 기준 기본 경로를 유추.
        - trades:   ./logs/trades/orders_YYYY-MM-DD.jsonl
        - template: ./trading_report/daily_report_template.md
        - script:   ./trading_report/daily_report_generator.py (subprocess fallback)
        - output:   ./reports/daily_YYYY-MM-DD.md
        """
        root = Path(self.project_root or ".").resolve()
        reports_dir = (root / "reports"); reports_dir.mkdir(parents=True, exist_ok=True)

        ymd_dash = date_str

        # 'system' 경로를 제거하고 'trades' 경로를 기본 데이터 소스로 사용합니다.
        trades_path   = root / "logs" / "trades" / f"orders_{ymd_dash}.jsonl"
        template_path = root / "trading_report" / "daily_report_template.md"
        script_path   = root / "trading_report" / "daily_report_generator.py"
        output_path   = reports_dir / f"daily_{ymd_dash}.md"

        return dict(
            # system 키 제거
            trades=trades_path,
            template=template_path,
            script=script_path,
            output=output_path,
            reports_dir=reports_dir,
        )

    def _pick_file(self, title: str, start_dir: Path, pattern: str) -> Optional[Path]:
        sel, _ = QFileDialog.getOpenFileName(self, title, str(start_dir), pattern)
        return Path(sel) if sel else None

    # =========================
    # 매매리포트: 실행/열기 핸들러
    # =========================
    @Slot()
    def on_click_daily_report(self):
        """
        사용자가 제공한 ReportDialog를 사용하여 리포트를 표시합니다.
        """
        try:
            # ReportDialog에 전달할 오늘 날짜 문자열 생성
            now_kst = pd.Timestamp.now(tz="Asia/Seoul") if pd is not None else datetime.now()
            date_str = now_kst.strftime("%Y-%m-%d")

            # ReportDialog 인스턴스를 생성하고 실행합니다.
            # 이 다이얼로그는 닫힐 때까지 부모창의 작업을 막는 모달(modal) 형태로 실행됩니다.
            dialog = ReportDialog(date_str, self)
            dialog.exec()

        except Exception as e:
            self.append_log(f"[UI] on_click_daily_report 오류: {e}")
            QMessageBox.critical(self, "리포트 오류", f"리포트를 표시하는 중 오류가 발생했습니다: {e}")

    @Slot()
    def on_click_open_last_report(self):
        """마지막으로 생성된 리포트를 열거나, 오늘자 기본 경로를 시도."""
        try:
            path = getattr(self, "_last_report_path", None)
            if not path:
                now_kst = pd.Timestamp.now(tz="Asia/Seoul") if pd is not None else datetime.now()
                date_str = now_kst.strftime("%Y-%m-%d")
                p = Path(self.project_root) / "reports" / f"daily_{date_str}.md"
                if p.exists():
                    path = str(p)
                else:
                    # 리포트가 없을 때, 생성할지 물어보는 대화 상자 추가
                    reply = QMessageBox.question(self, "리포트 생성",
                                                 "당일 매매 리포트가 없습니다. 지금 생성하시겠습니까?",
                                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                    if reply == QMessageBox.Yes:
                        self.on_click_daily_report()  # "예"를 누르면 리포트 생성 함수 호출
                    return

            if path and Path(path).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            elif not getattr(self, "_last_report_path", None):
                # on_click_daily_report 에서 리포트가 생성된 후 자동으로 열리기 때문에
                # এখানে 특별히 처리할 필요가 없습니다.
                pass
            else:
                 QMessageBox.information(self, "안내", "리포트 파일을 찾을 수 없습니다.")

        except Exception as e:
            self.append_log(f"[UI] on_click_open_last_report 오류: {e}")

    def _render_results_html(self):
        if not self._result_rows:
            self.text_result.setHtml("<div style='color:#9aa0a6;'>표시할 결과가 없습니다.</div>")
            return

        # ✅ 정렬 키 맵 (들여쓰기 버그 수정: if 블록 밖에 있어야 함)
        key_map = {
            "등락률(%)":"rt","현재가":"price","거래량":"vol","매수가":"buy_price","매도가":"sell_price",
            "코드":"code","이름":"name","최근 갱신시간":"updated_at","조건식":"conds"
        }
        sort_label = self.cmb_sort_key.currentText(); key = key_map.get(sort_label, "updated_at"); desc = self.btn_sort_dir.isChecked()

        def sort_key(row):
            v = row.get(key)
            if key == "updated_at":
                try: return datetime.fromisoformat(str(v)).timestamp()
                except Exception: return 0
            try: return float(str(v).replace("%", "").replace(",", ""))
            except Exception: return str(v)

        rows = sorted(self._result_rows, key=sort_key, reverse=desc)
        html = [
            """
            <style>
              table.res { width:100%; border-collapse:collapse; font-size:12px; }
              table.res th, table.res td { border-bottom:1px solid #2f3338; padding:8px 10px; }
              table.res th { text-align:center; color:#cfd3d8; background:#25282d; position:sticky; top:0; }
              table.res td.right { text-align:right; font-family:Consolas,'Courier New',monospace; }
              table.res tr:hover { background:#2a2e33; }
              .pos { color:#e53935; font-weight:700; }
              .neg { color:#43a047; font-weight:700; }
              .muted { color:#9aa0a6; font-weight:700; }
              .code { color:#9aa0a6; font-family:Consolas,'Courier New',monospace; }
              .btn { padding:2px 8px; border:1px solid #4c566a; border-radius:10px; color:#e0e0e0; text-decoration:none; background:#2b2f36; }
            </style>
            <table class="res">
              <thead><tr>
                <th style='width:24%;'>이름</th>
                <th style='width:12%;'>코드</th>
                <th style='width:12%;'>현재가</th>
                <th style='width:11%;'>등락률</th>
                <th style='width:11%;'>매수가</th>
                <th style='width:11%;'>매도가</th>
                <th style='width:11%;'>최근 갱신</th>
                <th style='width:18%;'>조건식</th>
                <th style='width:8%;'></th>
              </tr></thead><tbody>
            """]
        for r in rows:
            name=r.get("name","-"); code6=r.get("code","-"); price=self._fmt_num(r.get("price"))
            try: f_rt=float(str(r.get("rt",0)).replace("%","").replace(",",""))
            except Exception: f_rt=0.0
            cls = "pos" if f_rt>0 else ("neg" if f_rt<0 else "muted"); rtf=f"{f_rt:.2f}%"
            buy=self._fmt_num(r.get("buy_price")); sell=self._fmt_num(r.get("sell_price")); upd=str(r.get("updated_at","-"))
            conds = r.get("conds") or "-"

            html.append(f"""
                <tr>
                  <td>{name}</td>
                  <td class='code'>{code6}</td>
                  <td class='right'>{price}</td>
                  <td class='right {cls}'>{rtf}</td>
                  <td class='right'>{buy}</td>
                  <td class='right'>{sell}</td>
                  <td class='right'>{upd}</td>
                  <td>{conds}</td>
                  <td><a href='macd:{code6}' class='btn'>상세</a></td>
                </tr>
            """)
        html.append("</tbody></table>")
        self.text_result.setHtml("".join(html))

    # ---------------- 유틸 ----------------
    def _fmt_num(self, v, digits=0):
        try:
            if v is None or v == "": return "-"
            f = float(str(v).replace(",", "").replace("%", ""))
            return f"{f:,.{digits}f}" if digits else f"{int(round(f)):,.0f}"
        except Exception:
            return str(v)

    def _ensure_macd_stream(self, code6: str):
        try:
            now = pd.Timestamp.now(tz="Asia/Seoul")
            last = self._last_stream_req_ts.get(code6)
            if last is not None and (now - last).total_seconds() < self._stream_debounce_sec:
                return
            self._last_stream_req_ts[code6] = now
            if code6 in self._active_macd_streams: return
            if hasattr(self.engine, "start_macd_stream"):
                self.engine.start_macd_stream(code6); self._active_macd_streams.add(code6)
        except Exception:
            pass

    # -------- MACD 상세 다이얼로그 (구버전 호환) --------
    def _open_macd_dialog(self, code: str):
        code6 = str(code)[-6:].zfill(6)
        dlg = self._macd_dialogs.get(code6)
        if dlg and dlg.isVisible():
            dlg.raise_(); dlg.activateWindow(); return

        self._ensure_macd_stream(code6)

        if MacdDialog is None:
            QMessageBox.warning(self, "안내", "macd_dialog 모듈이 없습니다.")
            return
        dlg = MacdDialog(code=code6, parent=self)
        dlg.finished.connect(lambda _: self._macd_dialogs.pop(code6, None))
        dlg.show()
        self._macd_dialogs[code6] = dlg

    # ---------------- 버튼 핸들러 ----------------
    def on_click_init(self):
        try:
            if getattr(self.engine, "_initialized", False):
                QMessageBox.information(self, "안내", "이미 초기화되었습니다."); return
            if hasattr(self.engine, "initialize"): self.engine.initialize()
            self.btn_init.setEnabled(False)
        except Exception as e:
            QMessageBox.critical(self, "초기화 실패", str(e))

    def on_initialization_complete(self):
        self.status.showMessage("초기화 완료: WebSocket 수신 시작", 3000)
        QMessageBox.information(self, "초기화", "초기화 완료: WebSocket 수신 시작")

    def on_click_start_condition(self):
        item = self.list_conditions.currentItem()
        if not item: QMessageBox.warning(self, "안내", "시작할 조건식을 선택하세요."); return
        seq = item.data(Qt.UserRole) or ""
        if hasattr(self.engine, "send_condition_search_request"): self.engine.send_condition_search_request(seq)
        self.status.showMessage(f"조건검색 시작 요청: {seq}", 3000)

    def on_click_stop_condition(self):
        item = self.list_conditions.currentItem()
        if not item: QMessageBox.warning(self, "안내", "중지할 조건식을 선택하세요."); return
        seq = item.data(Qt.UserRole) or ""
        if hasattr(self.engine, "remove_condition_realtime"): self.engine.remove_condition_realtime(seq)
        self.status.showMessage(f"조건검색 중지 요청: {seq}", 3000)

    def on_click_filter(self):
        try:
            out_path = self.perform_filtering_cb()
            self.append_log("✅ 필터링 완료 (finance + technical)")
            self.load_candidates(out_path if isinstance(out_path, str) else None)
            self.status.showMessage("필터링 완료", 3000)
        except Exception as e:
            QMessageBox.critical(self, "오류", str(e))

    def on_open_settings_dialog(self):
        if not SettingsDialog:
            QMessageBox.information(self, "안내", "SettingsDialog 모듈이 없습니다.")
            return
        dlg = SettingsDialog(self, self.app_cfg)
        if dlg.exec() == QDialog.Accepted:
            new_cfg = dlg.get_settings()
            if self.store: self.store.save(new_cfg)
            self.app_cfg = new_cfg
            if self.wiring and hasattr(self.wiring, "apply_settings"):
                self.wiring.apply_settings(new_cfg)
            self.append_log("⚙️ 설정이 적용되었습니다.")

    # ---------------- 링크/필터 ----------------
    @Slot(QUrl)
    def _on_result_anchor_clicked(self, url: QUrl):
        try:
            if not url or url.scheme() != 'macd': return
            code = url.path().lstrip('/') or url.host() or url.toString()[5:]
            if code:
                self._open_macd_dialog(code)
        except Exception as e:
            logger.error(f"anchor click error: {e}")

    def load_candidates(self, path: str = None):
        if path is None: path = os.path.join(self.project_root, "candidate_stocks.csv")
        if not os.path.exists(path):
            self.append_log(f"ℹ️ 후보 종목 파일 없음: {path}")
            self.cand_model.setDataFrame(pd.DataFrame(columns=["회사명","종목코드","현재가"]))
            return
        try:
            df = pd.read_csv(path, encoding="utf-8-sig")
            rename_map = {}
            for col in list(df.columns):
                low = str(col).lower()
                if low in {"stock_name", "name", "종목명", "kor_name"}:
                    rename_map[col] = "회사명"
                elif low in {"stock_code", "code", "종목코드", "ticker"}:
                    rename_map[col] = "종목코드"
                elif low in {"price", "현재가", "close", "prc"}:
                    rename_map[col] = "현재가"
            if rename_map:
                df = df.rename(columns=rename_map)
            for need in ["회사명","종목코드","현재가"]:
                if need not in df.columns:
                    df[need] = ""
            df = df[["회사명","종목코드","현재가"]]
            self.cand_model.setDataFrame(df)
            self._filter_candidates(self.search_candidates.text())
            self.status.showMessage(f"후보 종목 {len(df)}건 로드", 3000)
        except Exception as e:
            self.append_log(f"❌ 후보 종목 파일 로드 오류: {e}")

    def _filter_conditions(self, text: str):
        text = (text or "").strip().lower()
        for i in range(self.list_conditions.count()):
            item = self.list_conditions.item(i)
            visible = (text in item.text().lower()) if text else True
            item.setHidden(not visible)
        self._update_cond_info()

    def _filter_candidates(self, text: str):
        self.cand_proxy.setFilterFixedString(text or "")

    def _update_cond_info(self):
        total = self.list_conditions.count(); selected = len(self.list_conditions.selectedItems())
        self.lbl_cond_info.setText(f"{total}개 / 선택: {selected}")

    # ---------------- 외부 스레드 프록시 ----------------
    def threadsafe_new_stock_detail(self, payload: dict):
        try:
            self.sig_new_stock_detail.emit(payload)
        except Exception as e:
            self.append_log(f"[UI] emit 실패: {e}")

    def threadsafe_trade_signal(self, payload: dict):
        try:
            self.sig_trade_signal.emit(payload)
        except Exception as e:
            self.append_log(f"[UI] trade emit 실패: {e}")


# ---------------- 단독 실행 스텁 ----------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()

    # 데모 스냅샷 흘려보기 (차트/게이지/카드뷰)
    from PySide6.QtCore import QTimer
    import random, datetime as dt

    def demo():
        # 포트폴리오 곡선
        eq_base = 100_000_000
        eq_curve = [{"t": (dt.datetime.now()-dt.timedelta(minutes=5*i)).isoformat(), "equity": eq_base + random.uniform(-2e6, 2e6)} for i in range(20)][::-1]
        hist = [{"d": (dt.date.today()).isoformat(), "pnl": random.uniform(-500000, 500000)}]
        snap = {
            "portfolio": {
                "equity": eq_curve[-1]["equity"],
                "daily_pnl": hist[-1]["pnl"],
                "daily_pnl_pct": hist[-1]["pnl"]/eq_base*100,
                "cum_return_pct": (eq_curve[-1]["equity"]/eq_base - 1)*100,
                "mdd_pct": -5.2,
                "equity_curve": eq_curve,
                "daily_hist": hist,
                "gross_exposure_pct": random.uniform(40, 130),
            },
            "by_condition": {
                "단타_급등": {"daily_pnl_pct": random.uniform(-4, 3), "cum_return_pct": random.uniform(-2, 8), "positions": [{"code":"005930","qty":10,"avg":70000,"last":71000,"unreal":10000}], "symbol_count": 1},
                "관심주_돌파": {"daily_pnl_pct": random.uniform(-4, 3), "cum_return_pct": random.uniform(-2, 8), "positions": [], "symbol_count": 0},
            }
        }
        win.on_pnl_snapshot(snap)

        # 결과표 데모
        win.on_new_stock_detail({"stock_code":"005930", "stock_name":"삼성전자", "cur_prc":"70400", "flu_rt":"+1.20"})
        win.on_new_stock_detail({"stock_code":"000660", "stock_name":"SK하이닉스", "cur_prc":"151000", "flu_rt":"-0.45"})
        win.on_trade_signal({"side":"BUY","code":"005930","price":70300})

    QTimer.singleShot(400, demo)
    sys.exit(app.exec())
