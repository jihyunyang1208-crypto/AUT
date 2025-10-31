# ui_main.py
import os
import sys
from typing import Optional, Dict, Any
from datetime import datetime

import pandas as pd

# Qt
from PySide6.QtCore import (
    Qt, QTimer, Signal, Slot, QObject, QModelIndex, QSettings, QUrl, QSortFilterProxyModel, QAbstractTableModel
)
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QMessageBox,
    QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QStatusBar,
    QTableView, QHeaderView, QLineEdit, QToolBar, QListWidget,
    QTextEdit, QListWidgetItem, QTextBrowser, QSplitter, QCheckBox,
    QComboBox, QGroupBox, QScrollArea, QFrame, QProgressBar, QTabWidget, QTableWidgetItem
)

try:
    from matplotlib.dates import DateFormatter
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    import matplotlib
    from matplotlib.ticker import FuncFormatter
    matplotlib.rc('font', family='Malgun Gothic')
    matplotlib.rc('axes', unicode_minus=False)
    _HAS_MPL = True
except Exception:
    _HAS_MPL = False

# 외부 모듈 로드
try:
    from core.macd_dialog import MacdDialog
except Exception:
    MacdDialog = None

from pathlib import Path
from PySide6.QtWidgets import QFileDialog

from trading_report.report_dialog import ReportDialog
import utils.result_paths as result_paths
import logging
logger = logging.getLogger(__name__)

# 설정 / 와이어링
try:
    from setting.settings_manager import (
        SettingsStore, SettingsDialog, apply_to_autotrader, AppSettings, apply_all_settings
    )
except Exception as e:
    logger.exception("Failed to import setting.settings_manager: %s", e)
    class _DummyStore:
        def load(self): return type("Cfg", (), {})()
        def save(self, _): pass
    SettingsStore = _DummyStore
    SettingsDialog = None
    apply_to_autotrader = lambda *a, **k: None
    AppSettings = type("Cfg", (), {})

try:
    from setting.wiring import AppWiring
except Exception as e:
    logger.exception("Failed to import setting.wiring: %s", e)
    AppWiring = None

# 포지션 관리 및 리스크 집계 모듈
from trade_pro.auto_trader import AutoTrader
from utils.stock_info_manager import StockInfoManager
from utils.result_paths import path_today, path_today

from risk_management.trading_results import TradingResultStore  # [CHANGED] AlertConfig 제거 (존재 X)
from risk_management.risk_dashboard import RiskDashboard

logger = logging.getLogger("ui_main")
logging.getLogger("matplotlib.font_manager").setLevel(logging.WARNING)

# -------------------------------
# 📊 테이블 컬럼 인덱스 상수
# -------------------------------
COL_RT          = 0
COL_PRICE       = 1
COL_VOL         = 2
COL_BUY_PRICE   = 3
COL_SELL_PRICE  = 4
COL_CODE        = 5
COL_NAME        = 6
COL_UPDATED_AT  = 7
COL_CONDS       = 8

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
    sig_alert = Signal(str)   
    sig_new_stock_detail = Signal(dict)
    sig_trade_signal = Signal(dict)

    def __init__(
        self,
        bridge=None,
        engine=None,
        perform_filtering_cb=None,
        project_root: str = ".",
        wiring: Optional[AppWiring] = None,
    ):
        super().__init__()
        self.setWindowTitle("오트 · 조건검색 & 리스크 대시보드")
        self.resize(1280, 860)

        # 멤버 주입
        self.trader = AutoTrader()
        self.monitor = None
        self.bridge = bridge
        self.engine = engine
        self.perform_filtering_cb = perform_filtering_cb or (lambda: None)
        self.project_root = self._resolve_project_root(project_root)
        self.wiring = (AppWiring(trader=self.trader, monitor=self.monitor) if callable(AppWiring) else None)

        self.stock_info = StockInfoManager() if StockInfoManager else None 

        # UI 상태 변수
        self._last_report_path: Optional[str] = None
        self._result_rows: list[dict] = []
        self._result_index: dict[str, int] = {}
        self._macd_dialogs: dict[str, QDialog] = {}
        self._active_macd_streams: set[str] = set()
        self._last_stream_req_ts: dict[str, Any] = {}
        self._stream_debounce_sec = 15
        self._cond_seq_to_name: dict[str, str] = {}
        self._code_to_conds: dict[str, set[str]] = {}

        # [ADDED] 대시보드 핸들 (탭 2개)
        self.risk_tabs: Optional[QTabWidget] = None
        self.risk_dashboard_daily: Optional[RiskDashboard] = None
        self.risk_dashboard_cum: Optional[RiskDashboard] = None

        # UI 빌드
        self._build_toolbar()
        self._build_layout()
        self._apply_stylesheet()

        # 상태바/시계
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("준비됨")
        self.label_new_stock = QLabel("신규 종목 없음")
        self.status.addPermanentWidget(self.label_new_stock)
        self._start_clock()

        # 시그널 연결
        self._connect_signals()

        # 엔진 루프 초기화
        if hasattr(self.engine, "start_loop"):
            try:
                self.engine.start_loop()
            except Exception:
                pass

        # 후보 종목 로드
        self.load_candidates()

        # 창 상태 저장/복원
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

        # 앱 설정 로드 및 적용
        self.store = SettingsStore() if SettingsStore else None
        loaded = self.store.load() if self.store else type("Cfg", (), {})()
        self.cfg = loaded                
        self.app_cfg = self.cfg         

        if getattr(self.app_cfg, "broker_vendor", ""):
            os.environ["BROKER_VENDOR"] = self.app_cfg.broker_vendor  
        if self.wiring and hasattr(self.wiring, "apply_settings"):
            try:
                self.wiring.apply_settings(self.app_cfg)
                if getattr(self.wiring, "monitor", None) is not None:
                    self.monitor = self.wiring.monitor
                if self.monitor is not None:
                    try:
                        apply_to_monitor(self.monitor, self.app_cfg)  # type: ignore[name-defined]
                        logger.info(
                            "Monitor custom applied: buy_pro=%s sell_pro=%s auto_buy=%s auto_sell=%s",
                            getattr(self.app_cfg, "buy_pro", False),
                            getattr(self.app_cfg, "sell_pro", False),
                            getattr(self.app_cfg, "auto_buy", True),
                            getattr(self.app_cfg, "auto_sell", True),
                        )
                    except Exception:
                        logger.exception("apply_to_monitor failed")
            except Exception:
                pass

        # 리스크 패널 구성 (탭 2개)  ---------------------------------------
        self._build_risk_panel()  # [CHANGED]
        # ---------------------------------------------------------------

        # 리스크 패널 토글 복원
        vis = self._settings_qs.value("risk_panel_visible", True)
        vis = (str(vis).lower() in ("true", "1", "yes")) if not isinstance(vis, bool) else vis
        self._toggle_risk_panel(bool(vis))
        if hasattr(self, 'act_toggle_risk'):
            self.act_toggle_risk.setChecked(bool(vis))

    # ---------------- UI 구성 함수들 ----------------
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
        tb.addSeparator()
        self.act_toggle_risk = tb.addAction("리스크패널")
        self.act_toggle_risk.setCheckable(True)
        self.act_toggle_risk.setChecked(True)
        self.act_toggle_risk.toggled.connect(self._toggle_risk_panel)

    def _build_layout(self):
        central = QWidget(); self.setCentralWidget(central)
        root = QHBoxLayout(central); root.setContentsMargins(8,8,8,8); root.setSpacing(8)
        main_split = QSplitter(Qt.Horizontal); root.addWidget(main_split)

        # 좌측
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

        # 우측
        right_panel = QWidget(); right = QVBoxLayout(right_panel)
        vsplit = QSplitter(Qt.Vertical); right.addWidget(vsplit, 1)
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

        # 상단-우: 종목 검색 결과
        pane_top_right = QWidget(); top_right = QVBoxLayout(pane_top_right)
        header_row = QHBoxLayout(); header_row.addStretch(1); top_right.addLayout(header_row)
        
        sort_row = QHBoxLayout()
        self.cmb_sort_key = QComboBox()
        self.cmb_sort_key.addItems(["등락률(%)", "현재가", "거래량", "매수가", "매도가", "코드", "이름", "최근 갱신시간", "조건식"])
        self.cmb_sort_key.setCurrentText("최근 갱신시간")
        self.SORT_COL_MAP = {
            "등락률(%)": COL_RT,
            "현재가": COL_PRICE,
            "거래량": COL_VOL,
            "매수가": COL_BUY_PRICE,
            "매도가": COL_SELL_PRICE,
            "코드": COL_CODE,
            "이름": COL_NAME,
            "최근 갱신시간": COL_UPDATED_AT,
            "조건식": COL_CONDS,
        }
        self.btn_sort_dir = QPushButton("내림차순")
        self.btn_sort_dir.setCheckable(True); self.btn_sort_dir.setChecked(True)
        sort_row.addWidget(QLabel("정렬:")); sort_row.addWidget(self.cmb_sort_key); sort_row.addWidget(self.btn_sort_dir); sort_row.addStretch(1)
        top_right.addLayout(sort_row)

        self.text_result = QTextBrowser(); self.text_result.setOpenExternalLinks(False); self.text_result.setOpenLinks(False); self.text_result.setReadOnly(True)
        self.text_result.anchorClicked.connect(self._on_result_anchor_clicked)
        top_right.addWidget(self.text_result, 1)

        tab_top = QTabWidget()
        tab_top.setDocumentMode(True)
        tab_top.setMovable(True)
        tab_top.setTabPosition(QTabWidget.North)
        tab_top.setStyleSheet("""
        QTabWidget::pane { border: 1px solid #3a414b; border-radius: 10px; top: -1px; background: #23272e; }
        QTabBar::tab { background: #2a2f36; color: #cfd6df; padding: 10px 18px; margin-right: 6px;
                       border: 1px solid #3a414b; border-bottom: 2px solid #3a414b;
                       border-top-left-radius: 10px; border-top-right-radius: 10px; font-weight: 600; }
        QTabBar::tab:hover { background: #303641; }
        QTabBar::tab:selected { background: #343b47; color: #ffffff; border-bottom: 2px solid #60a5fa; }
        QTabBar::tab:!selected { color: #aab2bd; }
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

        # 좌/우/리스크 패널
        root_parent = main_split
        main_split.addWidget(left_panel)
        main_split.addWidget(right_panel)

        # 우측 리스크 패널 (홀더)
        self.risk_panel_holder = QWidget()
        holder_lay = QVBoxLayout(self.risk_panel_holder)
        holder_lay.setContentsMargins(0,0,0,0)
        main_split.addWidget(self.risk_panel_holder)
        main_split.setSizes([380, 800, 360])

    def _build_risk_panel(self):
        """
        [CHANGED]
        리스크 패널을 Daily/Cumulative 2개 탭으로 구성.
        """
        from pathlib import Path

        results_dir = Path.cwd() / "logs" / "results"
        results_dir.mkdir(parents=True, exist_ok=True)

        # 누적 JSON (여러 날 합산)
        json_path_cum = results_dir / "trading_results.json"
        # 당일 JSON (YYYY-MM-DD)
        today = pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d")
        json_path_daily = results_dir / f"trading_results_{today}.json"

        # ✅ TradingResultStore는 누적 파일 경로로 유지 (내부에서 overwrite 사용)
        try:
            self.trading_store = TradingResultStore(json_path=str(json_path_cum))
            logger.info("[UI] TradingResultStore initialized (json overwrite @ cumulative)")
        except Exception as e:
            logger.exception(f"TradingResultStore 초기화 실패: {e}")

        # 탭 위젯 생성
        tabs = QTabWidget(self.risk_panel_holder)
        tabs.setDocumentMode(True)
        tabs.setStyleSheet("""
            QTabWidget::pane { border: 1px solid #3a414b; border-radius: 10px; top: -1px; background: #23272e; }
            QTabBar::tab { background: #2a2f36; color: #cfd6df; padding: 8px 16px; margin-right: 6px;
                           border: 1px solid #3a414b; border-bottom: 2px solid #3a414b;
                           border-top-left-radius: 10px; border-top-right-radius: 10px; font-weight: 600; }
            QTabBar::tab:selected { background: #343b47; color: #ffffff; border-bottom: 2px solid #60a5fa; }
        """)

        # ✅ Daily 대시보드
        self.risk_dashboard_daily = RiskDashboard(
            json_path=str(json_path_daily),
            price_provider=self._price_provider_safe,
            on_daily_report=self.on_click_daily_report,
            poll_ms=60_000,
            parent=self,
        )
        tabs.addTab(self.risk_dashboard_daily, "당일 (Daily)")

        # ✅ Cumulative 대시보드
        self.risk_dashboard_cum = RiskDashboard(
            json_path=str(json_path_cum),
            price_provider=self._price_provider_safe,
            on_daily_report=self.on_click_daily_report,
            poll_ms=60_000,
            parent=self,
        )
        tabs.addTab(self.risk_dashboard_cum, "누적 (Cumulative)")

        self.risk_tabs = tabs

        # 홀더 레이아웃에 장착
        lay = self.risk_panel_holder.layout() or QVBoxLayout(self.risk_panel_holder)
        self.risk_panel_holder.setLayout(lay)
        lay.addWidget(tabs)

        # 최초 1회 리프레시
        try:
            self.risk_dashboard_daily.refresh_json()
        except Exception:
            pass
        try:
            self.risk_dashboard_cum.refresh_json()
        except Exception:
            pass

    # ---------------- 스타일 ----------------
    def _apply_stylesheet(self):
        self.setStyleSheet("""
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
        """)

    # ---------------- 시계/종료 ----------------
    def _start_clock(self):
        self._clock = QLabel(); self.status.addPermanentWidget(self._clock)
        t = QTimer(self); t.timeout.connect(lambda: self._clock.setText(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        t.start(1000); self._clock_timer = t

    def closeEvent(self, event):
        """종료 시 리소스 정리"""
        try:
            # ✅ RiskDashboard 타이머 정지 (두 탭 모두)  [CHANGED]
            for dash in (getattr(self, "risk_dashboard_daily", None), getattr(self, "risk_dashboard_cum", None)):
                if dash:
                    try:
                        dash.stop_auto_refresh()
                        logger.info("RiskDashboard auto-refresh stopped")
                    except Exception:
                        logger.exception("Failed to stop risk_dashboard refresh")

            if hasattr(self, "orders_watcher") and self.orders_watcher:
                try:
                    self.orders_watcher.stop()
                    logger.info("OrdersCSVWatcher stopped")
                except Exception:
                    logger.exception("Failed to stop orders_watcher")

            if self.engine and hasattr(self.engine, "shutdown"):
                try:
                    self.engine.shutdown()
                except Exception:
                    pass
        finally:
            event.accept()

    # ---------------- 시그널 연결 ----------------
    def _connect_signals(self):
        self.btn_init.clicked.connect(self.on_click_init)
        self.btn_start.clicked.connect(self.on_click_start_condition)
        self.btn_stop.clicked.connect(self.on_click_stop_condition)
        self.btn_filter.clicked.connect(self.on_click_filter)
        if self.btn_settings:
            self.btn_settings.triggered.connect(self.on_open_settings_dialog)

        self.search_candidates.textChanged.connect(self._filter_candidates)
        self.search_conditions.textChanged.connect(self._filter_conditions)
        self.list_conditions.itemSelectionChanged.connect(self._update_cond_info)

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

            if hasattr(self.bridge, "pnl_snapshot_ready"):
                try:
                    self.bridge.pnl_snapshot_ready.connect(self.on_pnl_snapshot, Qt.UniqueConnection)
                except Exception:
                    self.bridge.pnl_snapshot_ready.connect(self.on_pnl_snapshot)

        self.sig_new_stock_detail.connect(self.on_new_stock_detail)
        self.sig_trade_signal.connect(self.on_trade_signal)

        if self.engine is not None and hasattr(self.engine, "initialization_complete"):
            try:
                self.engine.initialization_complete.connect(self.on_initialization_complete)
            except Exception:
                pass

        self.cmb_sort_key.currentIndexChanged.connect(lambda _: self._render_results_html())
        self.btn_sort_dir.toggled.connect(lambda checked: (self.btn_sort_dir.setText("내림차순" if checked else "오름차순"), self._render_results_html()))

    # ---------------- 손익 스냅샷 수신 ----------------
    @Slot(dict)
    def on_pnl_snapshot(self, snap: dict):
        """
        [CHANGED]
        1) RiskDashboard(Daily/Cumulative) 갱신 트리거 (필요 시)
        2) 중앙 종목 리스트의 매수가/매도가를 '당일 누적 평단/매도가'로 업데이트
        """
        try:
            # (선택) 즉시 리프레시 — 내부 타이머가 있지만 수신 시 동기화 가속
            for dash in (self.risk_dashboard_daily, self.risk_dashboard_cum):
                try:
                    if dash:
                        dash.update_snapshot(snap)  # RiskDashboard가 스냅샷 직접 반영 지원 시
                        dash.refresh_json()
                except Exception:
                    pass

            # 2) 중앙 종목 리스트 갱신
            positions_by_symbol = (snap or {}).get("by_symbol") or {}
            updated = False
            for code6, pos in positions_by_symbol.items():
                if not isinstance(pos, dict):
                    continue
                idx = self._result_index.get(code6)
                if idx is None:
                    continue

                # [ADDED] 스냅샷 키 다양성 흡수
                avg_buy = self._pick_first_num(pos,
                    "avg_buy_price", "avg_price", "avg_entry", "avg_cost")
                last_sell = self._pick_first_num(pos,
                    "avg_sell_price", "sell_avg", "last_sell_price", "avg_exit")

                row = self._result_rows[idx]
                if avg_buy is not None and row.get("buy_price") != avg_buy:
                    row["buy_price"] = avg_buy
                    updated = True
                if last_sell is not None and row.get("sell_price") != last_sell:
                    row["sell_price"] = last_sell
                    updated = True

            if updated:
                self._render_results_html()

            # 3) 상태바 요약
            portfolio = snap.get("portfolio") or {}
            daily_pnl = portfolio.get("daily_pnl_pct", 0)
            if daily_pnl != 0:
                sign = "+" if daily_pnl > 0 else ""
                self.status.showMessage(
                    f"일일 손익: {sign}{daily_pnl:.2f}% | "
                    f"노출도: {portfolio.get('gross_exposure_pct', 0):.1f}%",
                    3000
                )

        except Exception as e:
            self.append_log(f"[UI] on_pnl_snapshot 오류: {e}")

    # ---------------- 기존 메서드들 ----------------
    def append_log(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.text_log.append(f"[{ts}] {str(text)}")
        logging.getLogger("ui_logger").info(str(text))

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
            code = str(payload); cond_name = ""
        code6 = str(code)[-6:].zfill(6) if code else ""
        if not code6:
            return
        self.label_new_stock.setText(f"신규 종목: {code6}")
        self.status.showMessage(f"신규 종목: {code6} ({cond_name})", 3000)

    @Slot(dict)
    def on_new_stock_detail(self, payload: dict):
        flat = dict(payload)
        row0 = None
        if isinstance(flat.get("open_pric_pre_flu_rt"), list) and flat["open_pric_pre_flu_rt"]:
            row0 = flat["open_pric_pre_flu_rt"][0]
        elif isinstance(flat.get("rows"), list) and flat["rows"]:
            row0 = flat["rows"][0]
        if isinstance(row0, dict):
            for k, v in row0.items():
                flat.setdefault(k, v)
        code = (flat.get("stock_code") or flat.get("code") or "").strip()
        name = flat.get("stock_name") or flat.get("stk_nm") or flat.get("isu_nm")
        if not name and self.stock_info:
            code_from_payload = flat.get("stock_code") or flat.get("code") or ""
            if code_from_payload:
                name = self.stock_info.get_name(code_from_payload.strip())
        name = name or "종목명 없음"

        cond_name = (flat.get("condition_name") or flat.get("cond_name") or "").strip()
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
            "code": code6, "name": name, "price": price, "rt": rt, "vol": vol,
            "buy_price": None, "sell_price": None,
            "updated_at": updated_at, "conds": cond_name or "-",
        }
        idx = self._result_index.get(code6)
        if idx is None:
            self._result_index[code6] = len(self._result_rows); self._result_rows.append(row)
        else:
            keep_buy = self._result_rows[idx].get("buy_price")
            keep_sell = self._result_rows[idx].get("sell_price")
            if keep_buy is not None: row["buy_price"] = keep_buy
            if keep_sell is not None: row["sell_price"] = keep_sell
            self._result_rows[idx] = row
        self._render_results_html()
        self._ensure_macd_stream(code6)

    @Slot(str, float, float, float)
    def on_macd_data(self, code: str, macd: float, signal: float, hist: float):
        code6 = str(code)[-6:].zfill(6)
        self.status.showMessage(f"[MACD] {code6} M:{macd:.2f} S:{signal:.2f} H:{hist:.2f}", 2500)

    @Slot(dict)
    def on_macd_series_ready(self, data: dict):
        pass

    @Slot(dict)
    def on_trade_signal(self, payload: dict):
        try:
            side = str(payload.get("side") or payload.get("action") or "").upper()
            code = (payload.get("code") or payload.get("stock_code") or payload.get("stk_cd") or "").strip()
            code6 = str(code)[-6:].zfill(6) if code else ""
            raw_price = (
                payload.get("price") or payload.get("limit_price") or payload.get("cur_price")
                or payload.get("prc") or payload.get("avg_price")
            )
            if not code6 or raw_price in (None, ""):
                return
            price = float(str(raw_price).replace(",", ""))
            qty = int(payload.get("qty") or 1)

            idx = self._result_index.get(code6)
            if idx is None:
                self._result_index[code6] = len(self._result_rows)
                self._result_rows.append({"code": code6, "buy_price": None, "sell_price": None})
                idx = self._result_index[code6]

            row = self._result_rows[idx]
            if side == "BUY":
                row["buy_price"] = price
            elif side == "SELL":
                row["sell_price"] = price
            self._render_results_html()

            if hasattr(self, "trading_store") and self.trading_store:
                self.trading_store.apply_trade(
                    symbol=code6,
                    side="buy" if side == "BUY" else "sell",
                    qty=int(payload.get("qty") or payload.get("quantity") or 1),
                    price=price,
                )
            self.append_log(f"[Trade] {side} {code6} @ {price:,.0f} 반영 완료 ✅")
        except Exception as e:
            logger.exception(f"[UI] on_trade_signal 오류: {e}")

    # =========================
    # 매매리포트: 경로/루트 유틸
    # =========================
    def _resolve_project_root(self, root_like: str) -> str:
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

    # =========================
    # 추가: 클릭 핸들러
    # =========================
    def on_click_init(self) -> None:
        try:
            if getattr(self.engine, "_initialized", False):
                QMessageBox.information(self, "안내", "이미 초기화되었습니다.")
                return
            if hasattr(self.engine, "initialize"):
                self.engine.initialize()
            try:
                self.btn_init.setEnabled(False)
            except Exception:
                pass
        except Exception as e:
            QMessageBox.critical(self, "초기화 실패", str(e))

    def on_click_start_condition(self) -> None:
        item = self.list_conditions.currentItem()
        if not item:
            QMessageBox.warning(self, "안내", "시작할 조건식을 선택하세요.")
            return
        seq = item.data(Qt.UserRole) or ""
        if hasattr(self.engine, "send_condition_search_request"):
            try:
                self.engine.send_condition_search_request(seq)
                self.status.showMessage(f"조건검색 시작 요청: {seq}", 3000)
            except Exception:
                pass

    def on_click_stop_condition(self) -> None:
        item = self.list_conditions.currentItem()
        if not item:
            QMessageBox.warning(self, "안내", "중지할 조건식을 선택하세요.")
            return
        seq = item.data(Qt.UserRole) or ""
        if hasattr(self.engine, "remove_condition_realtime"):
            try:
                self.engine.remove_condition_realtime(seq)
                self.status.showMessage(f"조건검색 중지 요청: {seq}", 3000)
            except Exception:
                pass

    def on_click_filter(self) -> None:
        try:
            out_path = self.perform_filtering_cb()
            self.append_log("✅ 필터링 완료 (finance + technical)")
            self.load_candidates(out_path if isinstance(out_path, str) else None)
            self.status.showMessage("필터링 완료", 3000)
        except Exception as e:
            QMessageBox.critical(self, "오류", str(e))

    def on_open_settings_dialog(self) -> None:
        if not SettingsDialog:
            logger.warning("SettingsDialog is None (settings_manager import failed earlier)")
            QMessageBox.information(self, "안내", "SettingsDialog 모듈이 없습니다.")
            return

        if not getattr(self, "store", None):
            self.store = SettingsStore()
        current_cfg = getattr(self, "cfg", None) or self.store.load()
        dlg = SettingsDialog(self, current_cfg)

        if dlg.exec() == QDialog.Accepted:
            new_cfg = dlg.get_settings()
            self.store.save(new_cfg)
            try:
                if 'AppWiring' in globals() and isinstance(AppWiring, type):
                    return AppWiring(trader=self.trader, monitor=getattr(self, "monitor", None))
            except Exception as e:
                logger.exception("AppWiring init failed: %s", e)
            return None

    def on_click_daily_report(self) -> None:
        try:
            now_kst = pd.Timestamp.now(tz="Asia/Seoul") if pd is not None else datetime.now()
            date_str = now_kst.strftime("%Y-%m-%d")
            dialog = ReportDialog(date_str, self)
            dialog.exec()
        except Exception as e:
            self.append_log(f"[UI] on_click_daily_report 오류: {e}")
            QMessageBox.critical(self, "리포트 오류", f"리포트를 표시하는 중 오류가 발생했습니다: {e}")

    def on_click_open_last_report(self) -> None:
        try:
            path = getattr(self, "_last_report_path", None)
            if not path:
                now_kst = pd.Timestamp.now(tz="Asia/Seoul") if pd is not None else datetime.now()
                date_str = now_kst.strftime("%Y-%m-%d")
                p = Path(self.project_root) / "reports" / f"daily_{date_str}.md"
                if p.exists():
                    path = str(p)
                else:
                    reply = QMessageBox.question(
                        self, "리포트 생성",
                        "당일 매매 리포트가 없습니다. 지금 생성하시겠습니까?",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No
                    )
                    if reply == QMessageBox.Yes:
                        self.on_click_daily_report()
                    return
            if path and Path(path).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            elif not getattr(self, "_last_report_path", None):
                pass
            else:
                QMessageBox.information(self, "안내", "리포트 파일을 찾을 수 없습니다.")
        except Exception as e:
            self.append_log(f"[UI] on_click_open_last_report 오류: {e}")

    # -------------------------------
    # 후보 로드/필터/유틸
    # -------------------------------
    def load_candidates(self, path: str = None):
        if path is None:
            path = os.path.join(self.project_root, "candidate_stocks.csv")
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
        total = self.list_conditions.count()
        selected = len(self.list_conditions.selectedItems())
        self.lbl_cond_info.setText(f"{total}개 / 선택: {selected}")

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

    def _on_result_anchor_clicked(self, url: QUrl) -> None:
        try:
            if not url or url.scheme() != 'macd':
                return
            code = url.path().lstrip('/') or url.host() or url.toString()[5:]
            if code:
                self._open_macd_dialog(code)
        except Exception as e:
            logger.error(f"anchor click error: {e}")

    def _on_token_ready(self, token: str) -> None:
        try:
            from core.detail_information_getter import DetailInformationGetter, SimpleMarketAPI  # type: ignore
        except Exception:
            DetailInformationGetter = None
            SimpleMarketAPI = None
        try:
            if DetailInformationGetter:
                if not hasattr(self, "getter") or self.getter is None:
                    self.getter = DetailInformationGetter(token=token)
                else:
                    self.getter.token = token  # type: ignore
            if SimpleMarketAPI:
                if not hasattr(self, "market_api") or self.market_api is None:
                    self.market_api = SimpleMarketAPI(token=token)
                else:
                    self.market_api.set_token(token)
        except Exception:
            pass

    def on_initialization_complete(self) -> None:
        try:
            self.status.showMessage("초기화 완료: WebSocket 수신 시작", 3000)
            logger.info("초기화 완료: WebSocket 수신 시작")
        except Exception:
            pass

    def _toggle_risk_panel(self, visible: bool):
        try:
            if hasattr(self, "risk_panel_holder") and self.risk_panel_holder is not None:
                self.risk_panel_holder.setVisible(bool(visible))
            elif hasattr(self, "risk_panel") and self.risk_panel is not None:
                self.risk_panel.setVisible(bool(visible))
            if hasattr(self, "_settings_qs"):
                try:
                    self._settings_qs.setValue("risk_panel_visible", bool(visible))
                except Exception:
                    pass
        except Exception as e:
            self.append_log(f"[UI] _toggle_risk_panel 오류: {e}")

    # ---------------- 숫자 포맷 ----------------
    def _fmt_num(self, v, digits: int = 0) -> str:
        try:
            if v is None or v == "":
                return "-"
            f = float(str(v).replace(",", "").replace("%", ""))
            return f"{f:,.{digits}f}" if digits else f"{int(round(f)):,.0f}"
        except Exception:
            return str(v)

    # ---------------- 결과 테이블 렌더링 ----------------
    def _render_results_html(self) -> None:
        if not self._result_rows:
            self.text_result.setHtml("<div style='color:#9aa0a6;'>표시할 결과가 없습니다.</div>")
            return

        key_map = {
            "등락률(%)":"rt", "현재가":"price", "거래량":"vol",
            "매수가":"buy_price", "매도가":"sell_price",
            "코드":"code", "이름":"name", "최근 갱신시간":"updated_at", "조건식":"conds"
        }
        sort_label = self.cmb_sort_key.currentText()
        key = key_map.get(sort_label, "updated_at")
        desc = self.btn_sort_dir.isChecked()

        def sort_key(row):
            v = row.get(key)
            if key == "updated_at":
                try:
                    return datetime.fromisoformat(str(v)).timestamp()
                except Exception:
                    return 0
            try:
                return float(str(v).replace("%", "").replace(",", ""))
            except Exception:
                return str(v)

        rows = sorted(self._result_rows, key=sort_key, reverse=desc)

        html = ["""
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
            name = r.get("name", "-")
            code6 = r.get("code", "-")
            price = self._fmt_num(r.get("price"))
            try:
                f_rt = float(str(r.get("rt", 0)).replace("%", "").replace(",", ""))
            except Exception:
                f_rt = 0.0
            cls = "pos" if f_rt > 0 else ("neg" if f_rt < 0 else "muted")
            rtf = f"{f_rt:.2f}%"
            buy = self._fmt_num(r.get("buy_price"))
            sell = self._fmt_num(r.get("sell_price"))
            upd = str(r.get("updated_at", "-"))
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

    # ---------------- MACD 스트림 보조 ----------------
    def _ensure_macd_stream(self, code6: str):
        try:
            now = pd.Timestamp.now(tz="Asia/Seoul")
            last = self._last_stream_req_ts.get(code6)
            if last is not None and (now - last).total_seconds() < self._stream_debounce_sec:
                return
            self._last_stream_req_ts[code6] = now
            if code6 in self._active_macd_streams:
                return
            if hasattr(self.engine, "start_macd_stream"):
                try:
                    self.engine.start_macd_stream(code6)
                    self._active_macd_streams.add(code6)
                except Exception:
                    pass
        except Exception:
            pass

    # ---------------- MACD 상세 다이얼로그 ----------------
    def _open_macd_dialog(self, code: str) -> None:
        code6 = str(code)[-6:].zfill(6)
        dlg = self._macd_dialogs.get(code6)
        if dlg and dlg.isVisible():
            try:
                dlg.raise_(); dlg.activateWindow()
            except Exception:
                pass
            return
        self._ensure_macd_stream(code6)
        if MacdDialog is None:
            QMessageBox.warning(self, "안내", "macd_dialog 모듈이 없습니다.")
            return
        try:
            dlg = MacdDialog(code=code6, parent=self)
            dlg.finished.connect(lambda _: self._macd_dialogs.pop(code6, None))
            dlg.show()
            self._macd_dialogs[code6] = dlg
        except Exception:
            pass

    def on_trade_applied(self, symbol: str, side: str, qty: int, price: float, avg_after: float):
        try:
            row = self._find_row_by_symbol(symbol)
            if row is None:
                return
            if side == "buy":
                self.positions_table.setItem(row, COL_BUY_PRICE, self._mk_item(f"{price:,.0f}"))
                self.positions_table.setItem(row, COL_AVG_PRICE, self._mk_item(f"{avg_after:,.2f}"))
            else:
                self.positions_table.setItem(row, COL_SELL_PRICE, self._mk_item(f"{price:,.0f}"))
            from datetime import datetime
            now_txt = datetime.now().strftime("%H:%M:%S")
            self.positions_table.setItem(row, COL_UPDATED_AT, self._mk_item(now_txt))
            self.positions_table.viewport().update()
        except Exception as e:
            logger.warning("on_trade_applied update failed for %s: %s", symbol, e)

    def _mk_item(self, text: str, sort_value: Optional[float | int | str] = None):
        it = QTableWidgetItem(text)
        it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if sort_value is not None:
            it.setData(Qt.UserRole, sort_value)
        return it

    # ---------------- 내부 유틸 (추가) ----------------
    def _price_provider_safe(self, sym: str) -> Optional[float]:
        """[ADDED] RiskDashboard price_provider 래퍼"""
        try:
            if hasattr(self, "market_api") and self.market_api:
                return self.market_api.get_price(sym)
        except Exception:
            pass
        return None

    @staticmethod
    def _pick_first_num(d: Dict[str, Any], *keys: str) -> Optional[float]:
        """[ADDED] 다양한 스냅샷 키에서 첫 번째 숫자 추출"""
        for k in keys:
            v = d.get(k)
            if v in (None, "", "-"):
                continue
            try:
                return float(str(v).replace(",", ""))
            except Exception:
                continue
        return None
