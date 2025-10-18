"""
메인 UI(window) 클래스 모듈.

이 파일은 기존 ui_main.py의 구조를 기반으로 하며, 포지션 매니저의 신호를 통해 보유 수량/평단 업데이트를 받고
리스크 스냅샷은 기존 리스크 관리 모듈을 통해 전달받는 구조를 유지합니다.

수정 사항:
1. MainWindow 생성자에서 `position_mgr` 매개변수를 받아 보유 수량/평단 관리를 담당하는 PositionManager
   인스턴스를 주입하도록 수정했습니다. 주입된 매니저가 없으면 새로 생성합니다.
2. `PositionManager.position_changed` 신호를 UI 슬롯(on_position_changed)과 연결하여 포지션 변화가
   발생할 때 테이블을 갱신합니다.
3. SharedWalletPnL/PositionWiring 인스턴스 생성 및 `bridge.pnl_snapshot_ready` → on_pnl_snapshot_ready
   연결 코드를 제거하여 기존 리스크 모듈(on_pnl_snapshot)만 사용하도록 했습니다.
4. 주석은 한글로 추가되어 코드의 역할을 설명합니다.

이외의 UI 구성, 시그널 연결, 결과 테이블 렌더링 등 기존 기능은 그대로 유지합니다.
"""

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

    # 한글 폰트 설정 및 마이너스 기호 깨짐 방지
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

# 설정 / 와이어링 
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

# 포지션 관리 및 리스크 집계 모듈
from trade_pro.position_manager import PositionManager
from trade_pro.auto_trader import AutoTrader
from risk_management.shared_wallet_pnl import SharedWalletPnL  # 사용하지 않지만 호환성 위해 남겨둠
from risk_management.position_wiring import PositionWiring  # 사용하지 않지만 호환성 위해 남겨둠
# RiskDashboard 모듈 가져오기: 리스크 대시보드를 별도 모듈에서 관리
from risk_dashboard import RiskDashboard
from utils.stock_info_manager import StockInfoManager 

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
        # DataFrame 변경 시 모델 리셋
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
    """
    메인 윈도우 클래스.

    포지션 변화는 PositionManager의 신호를 통해 직접 받고,
    손익 스냅샷은 브리지(QtBridge)에서 제공하는 기존 risk_management 구조(on_pnl_snapshot)를 통해 갱신합니다.

    생성자에서는 PositionManager 인스턴스를 주입받아 포지션 변화 신호를 연결합니다. 전달되지 않은 경우
    새 인스턴스를 생성합니다.
    """

    # 외부 스레드 → UI 프록시 시그널 정의
    sig_new_stock_detail = Signal(dict)
    sig_trade_signal = Signal(dict)

    def __init__(
        self,
        bridge=None,
        engine=None,
        perform_filtering_cb=None,
        project_root: str = ".",
        wiring: Optional[AppWiring] = None,
        position_mgr: Optional[PositionManager] = None  # 포지션 매니저 인스턴스 주입
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
        self.wiring = AppWiring(trader=self.trader, monitor=self.monitor)

        self.stock_info = StockInfoManager() if StockInfoManager else None 

        # 포지션 매니저: 외부에서 주입하거나 새 인스턴스를 생성
        self.position_mgr: PositionManager = position_mgr if position_mgr is not None else PositionManager()

        # 포지션 변화 신호를 UI 슬롯과 연결
        try:
            self.position_mgr.position_changed.connect(self.on_position_changed)
        except Exception:
            pass

        # UI 상태 변수 초기화
        self._last_report_path: Optional[str] = None
        self._result_rows: list[dict] = []
        self._result_index: dict[str, int] = {}
        self._macd_dialogs: dict[str, QDialog] = {}
        self._active_macd_streams: set[str] = set()
        self._last_stream_req_ts: dict[str, Any] = {}
        self._stream_debounce_sec = 15
        self._cond_seq_to_name: dict[str, str] = {}
        self._code_to_conds: dict[str, set[str]] = {}

        # UI 빌드
        self._build_toolbar()
        self._build_layout()
        self._build_risk_panel()
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
        self.app_cfg = self.store.load() if self.store else type("Cfg", (), {})()

        if getattr(self.app_cfg, "broker_vendor", ""):
            os.environ["BROKER_VENDOR"] = self.app_cfg.broker_vendor  
        if self.wiring and hasattr(self.wiring, "apply_settings"):
            try:
                self.wiring.apply_settings(self.app_cfg)
            except Exception:
                pass

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

        # 리스크 패널 토글
        tb.addSeparator()
        self.act_toggle_risk = tb.addAction("리스크패널")
        self.act_toggle_risk.setCheckable(True)
        self.act_toggle_risk.setChecked(True)
        self.act_toggle_risk.toggled.connect(self._toggle_risk_panel)

    def _build_layout(self):
        # 메인 레이아웃 구성
        central = QWidget(); self.setCentralWidget(central)
        root = QHBoxLayout(central); root.setContentsMargins(8,8,8,8); root.setSpacing(8)
        
        main_split = QSplitter(Qt.Horizontal); root.addWidget(main_split)

        # 좌측 패널 (검색/조건/후보)
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

        # 우측 패널 (종목 결과/로그)
        right_panel = QWidget(); right = QVBoxLayout(right_panel)
        vsplit = QSplitter(Qt.Vertical); right.addWidget(vsplit, 1)
        
        # 상단 좌/우 분할
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

        # 후보 테이블 모델/프록시 연결
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

        # 다크테마 스타일 적용
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
        border-bottom: 2px solid #60a5fa;
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

        # 우측 리스크 패널 (홀더)
        self.risk_panel_holder = QWidget()
        holder_lay = QVBoxLayout(self.risk_panel_holder)
        holder_lay.setContentsMargins(0,0,0,0)
        main_split.addWidget(self.risk_panel_holder)
        main_split.setSizes([380, 800, 360])

    def _build_risk_panel(self):
        """
        리스크 패널을 초기화합니다.

        RiskDashboard 클래스를 이용하여 대시보드를 생성하고, risk_panel_holder에 장착합니다.
        """
        # RiskDashboard는 리스크 대시보드를 자체적으로 관리하는 위젯입니다.
        # 버튼 클릭 시 마지막 리포트를 열도록 콜백을 전달합니다.
        self.risk_dashboard = RiskDashboard(on_daily_report_callback=self.on_click_open_last_report)
        # risk_dashboard를 패널로 지정 (리스크 패널 토글에서 사용)
        self.risk_panel = self.risk_dashboard
        # risk_panel_holder는 _build_layout에서 생성된 QWidget입니다.
        # 해당 홀더의 레이아웃에 risk_dashboard를 추가하여 화면에 표시합니다.
        if hasattr(self, "risk_panel_holder") and self.risk_panel_holder.layout() is not None:
            self.risk_panel_holder.layout().addWidget(self.risk_dashboard)
        else:
            # 홀더가 없는 경우 새로 생성하여 추가
            try:
                self.risk_panel_holder = QWidget()
                holder_layout = QVBoxLayout(self.risk_panel_holder)
                holder_layout.setContentsMargins(0,0,0,0)
                holder_layout.addWidget(self.risk_dashboard)
            except Exception:
                pass

    # ---------------- 스타일 ----------------
    def _apply_stylesheet(self):
        # 다크 테마 스타일 정의
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
        # 상태바 시계 설정
        self._clock = QLabel(); self.status.addPermanentWidget(self._clock)
        t = QTimer(self); t.timeout.connect(lambda: self._clock.setText(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        t.start(1000); self._clock_timer = t

    def closeEvent(self, event):
        # 창 종료 시 설정 저장 및 엔진/스트림 정리
        try:
            if hasattr(self, "hsplit"):
                try:
                    self._settings_qs.setValue("hsplit_state", self.hsplit.saveState())
                except Exception:
                    pass
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

            try:
                store = SettingsStore()
                # self.cfg 가 최신인지 보장: 세션 중 변경사항이 self.cfg 에 반영되어 있어야 함
                store.save(self.cfg)
            except Exception as e:
                logging.getLogger(__name__).exception("Failed to save settings on close: %s", e)


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
        # 버튼/액션 연결
        self.btn_init.clicked.connect(self.on_click_init)
        self.btn_start.clicked.connect(self.on_click_start_condition)
        self.btn_stop.clicked.connect(self.on_click_stop_condition)
        self.btn_filter.clicked.connect(self.on_click_filter)
        if self.btn_settings:
            self.btn_settings.triggered.connect(self.on_open_settings_dialog)

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
            # 기존 리스크 스냅샷 (risk_management 모듈) 처리
            if hasattr(self.bridge, "pnl_snapshot_ready"):
                try:
                    self.bridge.pnl_snapshot_ready.connect(self.on_pnl_snapshot, Qt.UniqueConnection)
                except Exception:
                    self.bridge.pnl_snapshot_ready.connect(self.on_pnl_snapshot)

        # 비UI → UI 스레드 프록시 시그널
        self.sig_new_stock_detail.connect(self.on_new_stock_detail)
        self.sig_trade_signal.connect(self.on_trade_signal)

        # 엔진 초기화 완료 시그널
        if self.engine is not None and hasattr(self.engine, "initialization_complete"):
            try:
                self.engine.initialization_complete.connect(self.on_initialization_complete)
            except Exception:
                pass

        # 정렬 핸들러
        self.cmb_sort_key.currentIndexChanged.connect(lambda _: self._render_results_html())
        self.btn_sort_dir.toggled.connect(lambda checked: (self.btn_sort_dir.setText("내림차순" if checked else "오름차순"), self._render_results_html()))

    # ---------------- 포지션 변화 처리 ----------------
    def on_position_changed(self, code: str, qty: int, avg_price: float) -> None:
        """
        PositionManager에서 포지션 변경 신호를 받으면 호출됩니다.
        종목코드에 해당하는 테이블 행을 찾아 수량과 평균 매수가를 갱신합니다.
        """
        try:
            # position_table이 정의되어 있으면 갱신 (예: 보유 종목 테이블)
            table = getattr(self, 'position_table', None)
            if table is not None and hasattr(table, 'findRowByCode'):
                row = table.findRowByCode(code)
                if row is not None:
                    table.setItem(row, 1, QTableWidgetItem(str(qty)))
                    table.setItem(row, 2, QTableWidgetItem(f"{avg_price:.2f}" if avg_price else "-"))
        except Exception as e:
            # 예외가 발생해도 로그만 남김
            self.append_log(f"[UI] on_position_changed 오류: {e}")

    # ---------------- 손익 스냅샷 수신 ----------------
    @Slot(dict)
    def on_pnl_snapshot(self, snap: dict):
        """
        risk_management 모듈에서 전달하는 손익 스냅샷 딕셔너리를 받아
        리스크 대시보드 UI를 갱신합니다.
        snap 구조 예시:
        {
            "portfolio": {
                "daily_pnl_pct": -0.5,
                "cum_return_pct": 1.2,
                "mdd_pct": -4.3,
                "gross_exposure_pct": 85,
                "equity_curve": [...],
                "daily_hist": [...],
            },
            "by_condition": {...},
            "by_symbol": {...}
        }
        """
        try:
            # RiskDashboard가 존재하면 스냅샷을 사용하여 리스크 대시보드를 갱신합니다.
            if hasattr(self, "risk_dashboard"):
                try:
                    self.risk_dashboard.update_snapshot(snap)
                except Exception as ex:
                    logger.error("RiskDashboard 업데이트 실패: %s", ex)

            # 중앙 종목 리스트 평균 매수가/매도가 갱신
            positions_by_symbol = snap.get("by_symbol") or {}
            updated = False
            for code6, pos_data in positions_by_symbol.items():
                if not pos_data or not isinstance(pos_data, dict):
                    continue
                idx = self._result_index.get(code6)
                if idx is not None:
                    row = self._result_rows[idx]
                    avg_buy = pos_data.get("avg_buy_price")
                    avg_sell = pos_data.get("avg_sell_price")
                    if row.get("buy_price") != avg_buy:
                        row["buy_price"] = avg_buy
                        updated = True
                    if avg_sell is not None and row.get("sell_price") != avg_sell:
                        row["sell_price"] = avg_sell
                        updated = True
            if updated:
                self._render_results_html()

        except Exception as e:
            self.append_log(f"[UI] on_pnl_snapshot 오류: {e}")

    # ---------------- 기존 메서드들 ----------------
    # 이하 메서드들은 원본 코드의 기능을 그대로 유지합니다.

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
            code = str(payload)
            cond_name = ""
        code6 = str(code)[-6:].zfill(6) if code else ""
        if not code6:
            return
        self.label_new_stock.setText(f"신규 종목: {code6}")
        self.status.showMessage(f"신규 종목: {code6} ({cond_name})", 3000)

    @Slot(dict)
    def on_new_stock_detail(self, payload: dict):
        # 신규 종목 상세 정보 처리 및 중앙 결과 표 갱신
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
        pass  # 확장 포인트: 미니차트 등

    @Slot(dict)
    def on_trade_signal(self, payload: dict):
        try:
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

    # =========================
    # 추가: 클릭 핸들러 구현
    # =========================
    def on_click_init(self) -> None:
        # 초기화 버튼 클릭 시 엔진 초기화 수행
        try:
            if getattr(self.engine, "_initialized", False):
                QMessageBox.information(self, "안내", "이미 초기화되었습니다.")
                return
            if hasattr(self.engine, "initialize"):
                self.engine.initialize()
            # 초기화 후 초기화 버튼 비활성화
            try:
                self.btn_init.setEnabled(False)
            except Exception:
                pass
        except Exception as e:
            QMessageBox.critical(self, "초기화 실패", str(e))

    def on_click_start_condition(self) -> None:
        # 선택된 조건식 시작
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
        # 선택된 조건식 실시간 검색 중지
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
        # 종목 필터링 실행
        try:
            out_path = self.perform_filtering_cb()
            self.append_log("✅ 필터링 완료 (finance + technical)")
            self.load_candidates(out_path if isinstance(out_path, str) else None)
            self.status.showMessage("필터링 완료", 3000)
        except Exception as e:
            QMessageBox.critical(self, "오류", str(e))

    def on_open_settings_dialog(self) -> None:
        # 환경설정 대화상자를 엽니다.
        if not SettingsDialog:
            QMessageBox.information(self, "안내", "SettingsDialog 모듈이 없습니다.")
            return
        store = SettingsStore() 
        dlg = SettingsDialog(self, store.load())

        if dlg.exec() == QDialog.Accepted:
            new_cfg = dlg.get_settings()
            store.save(new_cfg)

            # ✅ wiring이 없으면 지금 만든다
            if not getattr(self, "wiring", None):
                logging.getLogger(__name__).warning("wiring was None; initializing now")
                self.wiring = AppWiring(trader=self.trader, monitor=getattr(self, "monitor", None))

            self.wiring.apply_settings(new_cfg)  # 🔄 브로커 핫스왑/시뮬토글 등
            self.append_log("⚙️ 설정이 적용되었습니다.")

    def on_click_daily_report(self) -> None:
        # 오늘자 데일리 매매리포트를 표시
        try:
            now_kst = pd.Timestamp.now(tz="Asia/Seoul") if pd is not None else datetime.now()
            date_str = now_kst.strftime("%Y-%m-%d")
            dialog = ReportDialog(date_str, self)
            dialog.exec()
        except Exception as e:
            self.append_log(f"[UI] on_click_daily_report 오류: {e}")
            QMessageBox.critical(self, "리포트 오류", f"리포트를 표시하는 중 오류가 발생했습니다: {e}")

    def on_click_open_last_report(self) -> None:
        # 마지막 매매 리포트를 엽니다.
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
                        self,
                        "리포트 생성",
                        "당일 매매 리포트가 없습니다. 지금 생성하시겠습니까?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No
                    )
                    if reply == QMessageBox.Yes:
                        self.on_click_daily_report()
                    return
            if path and Path(path).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            elif not getattr(self, "_last_report_path", None):
                # on_click_daily_report에서 리포트가 생성된 후 자동으로 열리므로 아무 처리도 하지 않음
                pass
            else:
                QMessageBox.information(self, "안내", "리포트 파일을 찾을 수 없습니다.")
        except Exception as e:
            self.append_log(f"[UI] on_click_open_last_report 오류: {e}")

    # -------------------------------
    # 추가 메서드: 후보 종목 로드/필터링 및 스레드 안전 메서드
    # -------------------------------
    def load_candidates(self, path: str = None):
        """
        CSV 파일에서 후보 종목을 읽어 DataFrame 모델에 설정합니다.
        경로가 주어지지 않으면 project_root/candidate_stocks.csv를 사용합니다.
        파일이 없을 경우 모델을 비워두고 로그를 남깁니다.
        """
        if path is None:
            path = os.path.join(self.project_root, "candidate_stocks.csv")
        # 파일이 존재하지 않는 경우, 빈 모델로 초기화하고 반환
        if not os.path.exists(path):
            self.append_log(f"ℹ️ 후보 종목 파일 없음: {path}")
            # 빈 DataFrame을 설정하여 테이블을 초기화
            self.cand_model.setDataFrame(pd.DataFrame(columns=["회사명","종목코드","현재가"]))
            return
        try:
            # CSV를 읽어 컬럼명을 표준화합니다.
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
            # 필요한 컬럼이 없으면 빈 컬럼을 추가합니다.
            for need in ["회사명","종목코드","현재가"]:
                if need not in df.columns:
                    df[need] = ""
            df = df[["회사명","종목코드","현재가"]]
            # 모델에 DataFrame을 설정
            self.cand_model.setDataFrame(df)
            # 현재 검색어에 따라 필터를 적용
            self._filter_candidates(self.search_candidates.text())
            # 상태바에 로드 결과 표시
            self.status.showMessage(f"후보 종목 {len(df)}건 로드", 3000)
        except Exception as e:
            # 로딩 중 오류 발생 시 로그 출력
            self.append_log(f"❌ 후보 종목 파일 로드 오류: {e}")

    def _filter_conditions(self, text: str):
        """
        조건식 목록을 검색어에 따라 필터링합니다.
        검색어를 포함하지 않는 항목은 숨깁니다.
        """
        text = (text or "").strip().lower()
        for i in range(self.list_conditions.count()):
            item = self.list_conditions.item(i)
            visible = (text in item.text().lower()) if text else True
            item.setHidden(not visible)
        # 조건 리스트 정보 갱신
        self._update_cond_info()

    def _filter_candidates(self, text: str):
        """
        후보 종목 테이블에서 텍스트를 포함한 행만 보여주도록 필터를 설정합니다.
        """
        self.cand_proxy.setFilterFixedString(text or "")

    def _update_cond_info(self):
        """
        조건 목록의 총 개수와 현재 선택 개수를 갱신합니다.
        """
        total = self.list_conditions.count()
        selected = len(self.list_conditions.selectedItems())
        self.lbl_cond_info.setText(f"{total}개 / 선택: {selected}")

    def threadsafe_new_stock_detail(self, payload: dict):
        """
        다른 스레드에서 종목 상세 정보를 UI 스레드로 전달하기 위한 메서드.
        Qt의 시그널을 통해 UI 스레드에서 on_new_stock_detail을 호출합니다.
        """
        try:
            self.sig_new_stock_detail.emit(payload)
        except Exception as e:
            self.append_log(f"[UI] emit 실패: {e}")

    def threadsafe_trade_signal(self, payload: dict):
        """
        다른 스레드에서 매매 신호를 UI 스레드로 전달하기 위한 메서드.
        Qt의 시그널을 통해 UI 스레드에서 on_trade_signal을 호출합니다.
        """
        try:
            self.sig_trade_signal.emit(payload)
        except Exception as e:
            self.append_log(f"[UI] trade emit 실패: {e}")

    def _on_result_anchor_clicked(self, url: QUrl) -> None:
        """
        결과 테이블에서 'macd:' 링크를 클릭했을 때 호출되는 슬롯.
        URL에서 종목 코드를 추출하여 MACD 상세 다이얼로그를 엽니다.
        """
        try:
            if not url or url.scheme() != 'macd':
                return
            # URL의 경로 또는 호스트에서 종목 코드를 추출 (macd:005930 형식)
            code = url.path().lstrip('/') or url.host() or url.toString()[5:]
            if code:
                self._open_macd_dialog(code)
        except Exception as e:
            logger.error(f"anchor click error: {e}")

    def _on_token_ready(self, token: str) -> None:
        """
        토큰 준비 완료 시 DetailInformationGetter와 SimpleMarketAPI의 토큰을 설정합니다.
        브리지에서 'token_ready' 시그널을 받을 때 호출됩니다.
        """
        try:
            from core.detail_information_getter import DetailInformationGetter, SimpleMarketAPI  # type: ignore
        except Exception:
            DetailInformationGetter = None
            SimpleMarketAPI = None
        try:
            # DetailInformationGetter의 토큰 설정
            if DetailInformationGetter:
                if not hasattr(self, "getter") or self.getter is None:
                    self.getter = DetailInformationGetter(token=token)
                else:
                    self.getter.token = token  # type: ignore
            # SimpleMarketAPI의 토큰 설정
            if SimpleMarketAPI:
                if not hasattr(self, "market_api") or self.market_api is None:
                    self.market_api = SimpleMarketAPI(token=token)
                else:
                    self.market_api.set_token(token)
        except Exception:
            pass

    def on_initialization_complete(self) -> None:
        """
        엔진 초기화 완료 시 호출되는 슬롯.
        상태바에 메시지를 표시하고 사용자에게 알림을 띄웁니다.
        """
        try:
            self.status.showMessage("초기화 완료: WebSocket 수신 시작", 3000)
            QMessageBox.information(self, "초기화", "초기화 완료: WebSocket 수신 시작")
        except Exception:
            pass

    def _toggle_risk_panel(self, visible: bool):
        """
        리스크 패널 표시/숨김 토글.
        risk_panel_holder가 존재하면 해당 위젯을 기준으로 보이기 설정을 하고,
        없으면 risk_panel 자체의 보이기 설정을 처리합니다.
        사용자 설정도 QSettings에 저장합니다.
        """
        try:
            # risk_panel_holder를 우선 처리
            if hasattr(self, "risk_panel_holder") and self.risk_panel_holder is not None:
                self.risk_panel_holder.setVisible(bool(visible))
            # fallback: risk_panel 단독 사용 시 처리
            elif hasattr(self, "risk_panel") and self.risk_panel is not None:
                self.risk_panel.setVisible(bool(visible))
            # 사용자가 설정한 risk_panel_visible 값을 저장
            if hasattr(self, "_settings_qs"):
                try:
                    self._settings_qs.setValue("risk_panel_visible", bool(visible))
                except Exception:
                    pass
        except Exception as e:
            # 예외 발생 시 로그 출력
            self.append_log(f"[UI] _toggle_risk_panel 오류: {e}")

    # ---------------- 숫자 포맷 유틸리티 ----------------
    def _fmt_num(self, v, digits: int = 0) -> str:
        """
        숫자 또는 문자열을 사람 읽기 좋은 문자열로 포맷합니다.
        v가 None 또는 빈 문자열이면 '-'를 반환하고, digits가 0이면 정수형으로,
        그 외에는 소수점 자릿수를 유지합니다.
        """
        try:
            if v is None or v == "":
                return "-"
            f = float(str(v).replace(",", "").replace("%", ""))
            return f"{f:,.{digits}f}" if digits else f"{int(round(f)):,.0f}"
        except Exception:
            return str(v)

    # ---------------- 결과 테이블 렌더링 ----------------
    def _render_results_html(self) -> None:
        """
        검색 결과를 HTML 테이블 형태로 렌더링하여 QTextBrowser에 표시합니다.
        self._result_rows 리스트를 정렬하고, 각 행을 포맷하여 HTML 문자열을 만듭니다.
        """
        if not self._result_rows:
            self.text_result.setHtml("<div style='color:#9aa0a6;'>표시할 결과가 없습니다.</div>")
            return

        # 정렬 키 매핑
        key_map = {
            "등락률(%)":"rt", "현재가":"price", "거래량":"vol",
            "매수가":"buy_price", "매도가":"sell_price",
            "코드":"code", "이름":"name", "최근 갱신시간":"updated_at", "조건식":"conds"
        }
        sort_label = self.cmb_sort_key.currentText()
        key = key_map.get(sort_label, "updated_at")
        desc = self.btn_sort_dir.isChecked()

        # 정렬 함수 정의
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

        # 정렬 수행
        rows = sorted(self._result_rows, key=sort_key, reverse=desc)

        # HTML 문자열 생성
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
            """
        ]
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
        # 생성된 HTML 문자열을 QTextBrowser에 설정
        self.text_result.setHtml("".join(html))

    # ---------------- MACD 스트림 보조 메서드 ----------------
    def _ensure_macd_stream(self, code6: str):
        """
        MACD 스트림을 시작해야 하는지 확인하고, 필요 시 엔진에 요청합니다.
        중복 실행을 방지하기 위해 _last_stream_req_ts와 _active_macd_streams를 사용합니다.
        """
        try:
            # 현재 시간
            now = pd.Timestamp.now(tz="Asia/Seoul")
            # 마지막 요청 시각과 비교하여 디바운스
            last = self._last_stream_req_ts.get(code6)
            if last is not None and (now - last).total_seconds() < self._stream_debounce_sec:
                return
            self._last_stream_req_ts[code6] = now
            # 이미 활성화된 스트림이면 종료
            if code6 in self._active_macd_streams:
                return
            # 엔진이 start_macd_stream을 지원하면 호출
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
        """
        종목 코드에 대한 MACD 상세 다이얼로그를 연다. 이미 열려 있으면 포커스를 이동시킨다.
        """
        code6 = str(code)[-6:].zfill(6)
        dlg = self._macd_dialogs.get(code6)
        # 이미 열려 있고 표시 중이면 포커스를 맞춘다.
        if dlg and dlg.isVisible():
            try:
                dlg.raise_()
                dlg.activateWindow()
            except Exception:
                pass
            return
        # MACD 스트림이 시작되어 있는지 확인
        self._ensure_macd_stream(code6)
        # macd_dialog 모듈이 없는 경우 경고
        if MacdDialog is None:
            QMessageBox.warning(self, "안내", "macd_dialog 모듈이 없습니다.")
            return
        try:
            dlg = MacdDialog(code=code6, parent=self)
            # 다이얼로그가 닫힐 때 사전에서 제거
            dlg.finished.connect(lambda _: self._macd_dialogs.pop(code6, None))
            dlg.show()
            self._macd_dialogs[code6] = dlg
        except Exception:
            pass

    # 나머지 유틸 및 클릭 핸들러는 원본 코드와 동일하므로 생략합니다.
