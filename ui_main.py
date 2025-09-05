import os
import json
import pandas as pd
from datetime import datetime
from typing import Dict, Any
import asyncio
import time
from collections import deque

# QtCore
from PySide6.QtCore import (
    Qt, QTimer, Signal, Slot, QAbstractTableModel, 
    QModelIndex, QSettings, QSortFilterProxyModel, QUrl)

from collections import OrderedDict
# QtGui
from PySide6.QtGui import QAction, QIcon, QKeySequence

# QtWidgets
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QMessageBox,          
    QLabel, QPushButton, QComboBox, QVBoxLayout,
    QHBoxLayout, QStatusBar, QTableWidget, QTableWidgetItem,
    QLineEdit, QTableView, QToolBar, QHeaderView, QStatusBar,
    QCheckBox, QFrame, QSplitter, QListWidget, QTextEdit, QListWidgetItem, QTextBrowser   
)

# matplotlib (MACD 모달 차트)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib

from core.auto_trader import AutoTrader
from core.macd_dialog import MacdDialog
from utils.notifier import PrintNotifier
import logging

logger = logging.getLogger("ui_main")
matplotlib.set_loglevel("warning")
logging.getLogger("matplotlib.font_manager").setLevel(logging.WARNING)


# ---- 간단 토스트 다이얼로그 ----
class _Toast(QDialog):
    def __init__(self, parent, text: str, timeout_ms: int = 2500):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.ToolTip
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._label = QLabel(text, self)
        self._label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._label.setStyleSheet("""
            QLabel {
                background: rgba(30, 30, 35, 220);
                color: #ffffff;
                padding: 10px 14px;
                border-radius: 10px;
                border: 1px solid rgba(255,255,255,0.08);
                font-size: 13px;
            }
        """)
        self._label.adjustSize()
        self.resize(self._label.sizeHint())

        QTimer.singleShot(timeout_ms, self.close)

    def show_at_bottom_right(self, margin: int = 16):
        if not self.parent():
            self.show()
            return
        parent_geom = self.parent().geometry()
        x = parent_geom.x() + parent_geom.width() - self.width() - margin
        y = parent_geom.y() + parent_geom.height() - self.height() - margin - 40
        self.move(x, y)
        self.show()


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
        return ""  # 세로 헤더는 숨김 처리

    def set_value(self, row: int, col: int, value):
        if 0 <= row < len(self._df) and 0 <= col < len(self._df.columns):
            self._df.iat[row, col] = value
            idx = self.index(row, col)
            # 뷰에 “해당 셀만” 다시 그리라고 알림
            self.dataChanged.emit(idx, idx, [Qt.DisplayRole, Qt.ToolTipRole])


# ----------------------------
# 고도화 UI MainWindow
# ----------------------------
class MainWindow(QMainWindow):
    """
    main.py에서 넘겨주는 것들:
      - bridge: AsyncBridge 인스턴스
      - engine: Engine 인스턴스 (start_loop/initialize 등 보유, 가능하면 .loop 제공)
      - perform_filtering_cb: callable -> 필터 실행 후 출력 경로(str) 반환 가능
      - project_root: str
    """

    # ✅ 비UI 스레드 → UI 스레드 안전 전환용 시그널 (dict payload)
    sig_new_stock_detail = Signal(dict)

    def __init__(self, bridge, engine, perform_filtering_cb, project_root: str):
        super().__init__()
        self.setWindowTitle("조건검색 & MACD 모니터 ")
        self.resize(1180, 760)

        self.bridge = bridge
        self.engine = engine
        self.perform_filtering_cb = perform_filtering_cb
        self.project_root = project_root
        self._macd_dialogs: dict[str, QDialog] = {}



        # 상단 툴바
        self._build_toolbar()

        # ─────────────────────────────────────────────
        # 중앙: 좌측(조건식) | 우측(상단-좌/우 + 하단 로그)
        # ─────────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        main_split = QSplitter(Qt.Horizontal)
        root_layout.addWidget(main_split)

        # ========== 좌측 패널 ==========
        left_panel = QWidget()
        left = QVBoxLayout(left_panel)

        self.search_conditions = QLineEdit(placeholderText="조건식 검색…")
        self.btn_init = QPushButton("초기화 (토큰+WS 연결)")
        self.btn_start = QPushButton("선택 조건 시작")
        self.btn_stop = QPushButton("선택 조건 중지")
        self.btn_filter = QPushButton("종목 필터링 실행 (재무+기술)")
        self.list_conditions = QListWidget()
        self.lbl_cond_info = QLabel("0개 / 선택: 0")


        left.addWidget(self.search_conditions)
        left.addWidget(QLabel("조건식 목록"))
        left.addWidget(self.list_conditions, 1)
        left.addWidget(self.btn_init)
        left.addWidget(self.btn_filter)
        left.addWidget(self.lbl_cond_info)
        row_btns = QHBoxLayout()
        row_btns.addWidget(self.btn_start)
        row_btns.addWidget(self.btn_stop)
        left.addLayout(row_btns)

        main_split.addWidget(left_panel)

        # ========== 우측 패널 ==========
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

        # 세로 분할: 상단(좌/우) | 하단(로그)
        vsplit = QSplitter(Qt.Vertical)
        right_layout.addWidget(vsplit, 1)

        # ── 상단: 좌우 스플리터 ──
        hsplit = QSplitter(Qt.Horizontal)
        self.hsplit = hsplit  # 상태 저장/복원
        vsplit.addWidget(hsplit)

        # (상단-좌) 후보 테이블
        pane_top_left = QWidget()
        top_left = QVBoxLayout(pane_top_left)
        top_left.addWidget(QLabel("25일이내 급등 종목"))
        self.search_candidates = QLineEdit(placeholderText="후보 종목 실시간 검색…")
        top_left.addWidget(self.search_candidates)

        self.cand_table = QTableView()
        self.cand_model = DataFrameModel(pd.DataFrame(columns=["회사명", "종목코드", "현재가"]))
        self.cand_proxy = QSortFilterProxyModel(self)
        self.cand_proxy.setSourceModel(self.cand_model)
        self.cand_proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.cand_proxy.setFilterKeyColumn(-1)
        self.cand_table.setModel(self.cand_proxy)
        self.cand_table.setSortingEnabled(False)
        self.cand_table.horizontalHeader().setStretchLastSection(True)
        self.cand_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.cand_table.setSelectionBehavior(QTableView.SelectRows)
        self.cand_table.setAlternatingRowColors(True)
        self.cand_table.verticalHeader().setVisible(False)
        self.cand_table.setCornerButtonEnabled(False)
        top_left.addWidget(self.cand_table, 1)

        # ✅ 컬럼 인덱스 상수 (현재 DataFrame 초기 컬럼 순서 기준)
        self.COL_NAME = 0
        self.COL_CODE = 1
        self.COL_PRICE = 2

        # ✅ 더블클릭 → MACD 다이얼로그 열기
        self.cand_table.doubleClicked.connect(self.on_candidate_double_clicked)

        hsplit.addWidget(pane_top_left)

        # (상단-우) 결과 로그/카드
        pane_top_right = QWidget()
        top_right = QVBoxLayout(pane_top_right)
        top_right.addWidget(QLabel("종목 검색 결과"))

        row_auto = QHBoxLayout()
        self.cb_auto_buy = QCheckBox("자동 매수")
        self.cb_auto_sell = QCheckBox("자동 매도")
        row_auto.addWidget(self.cb_auto_buy)
        row_auto.addWidget(self.cb_auto_sell)
        row_auto.addStretch(1)
        top_right.addLayout(row_auto)

        # 컨트롤러 생성 전 클릭해도 안전하게 동작하도록 getattr 사용
        self.cb_auto_buy.stateChanged.connect(
            lambda _:
                (getattr(self, "auto_trade_controller", None) and
                 setattr(self.auto_trade_controller.settings, "auto_buy", self.cb_auto_buy.isChecked()))
        )
        self.cb_auto_sell.stateChanged.connect(
            lambda _:
                (getattr(self, "auto_trade_controller", None) and
                 setattr(self.auto_trade_controller.settings, "auto_sell", self.cb_auto_sell.isChecked()))
        )

        self.text_result = QTextBrowser()
        self.text_result.setOpenExternalLinks(False)
        self.text_result.setOpenLinks(False)
        self.text_result.setReadOnly(True)
        self.text_result.anchorClicked.connect(self._on_result_anchor_clicked)
        top_right.addWidget(self.text_result, 1)

        self._cards = OrderedDict()               # code -> html (최신이 맨 앞)
        self._card_limit = 30

        # MACD 시리즈 캐시: { code6: { tf: data(dict) } }
        self._macd_cache: Dict[str, Dict[str, dict]] = {}

        hsplit.addWidget(pane_top_right)
        hsplit.setSizes([680, 440])

        # ── 하단: 로그 ──
        pane_bottom = QWidget()
        bottom = QVBoxLayout(pane_bottom)
        bottom.addWidget(QLabel("로그"))
        self.text_log = QTextEdit()
        self.text_log.setReadOnly(True)
        bottom.addWidget(self.text_log, 1)
        vsplit.addWidget(pane_bottom)
        vsplit.setSizes([540, 220])

        main_split.addWidget(right_panel)
        main_split.setSizes([380, 800])

        # 상태바 + 시계
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("준비됨")
        self._start_clock()
        self.label_new_stock = QLabel("신규 종목 없음")
        self.label_new_stock.setObjectName("label_new_stock")
        self.status.addPermanentWidget(self.label_new_stock)

        # 시그널 연결
        self.btn_init.clicked.connect(self.on_click_init)
        self.btn_start.clicked.connect(self.on_click_start_condition)
        self.btn_stop.clicked.connect(self.on_click_stop_condition)
        self.btn_filter.clicked.connect(self.on_click_filter)

        self.search_conditions.textChanged.connect(self._filter_conditions)
        self.search_candidates.textChanged.connect(self._filter_candidates)
        self.list_conditions.itemSelectionChanged.connect(self._update_cond_info)

        self.bridge.log.connect(self.append_log)
        self.bridge.condition_list_received.connect(self.populate_conditions)
        self.bridge.macd_data_received.connect(self.on_macd_data)
        self.bridge.macd_series_ready.connect(self.on_macd_series_ready)
        self.bridge.new_stock_received.connect(self.on_new_stock)
        self.bridge.token_ready.connect(self._on_token_ready)

        # 상세 정보 (Engine → Bridge → UI)
        if hasattr(self.bridge, "new_stock_detail_received"):
            self.bridge.new_stock_detail_received.connect(self.on_new_stock_detail)


        # ✅ 어떤 스레드/루프에서 오든 UI 스레드로 안전하게 전환되도록 내부 시그널도 연결
        self.sig_new_stock_detail.connect(self.on_new_stock_detail)

        # 스타일 (그레이 톤)
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #1f2124; color: #E6E6E6; }
            QLabel { color: #E6E6E6; }
            QLineEdit, QTextEdit, QListWidget, QTableView {
                background: #2a2d31; color: #E6E6E6; border: 1px solid #3a3d42;
                selection-background-color: #3d4650; selection-color: #ffffff;
                alternate-background-color: #26292d;
            }
            QLineEdit:focus, QTextEdit:focus { border: 1px solid #4d5661; }
            QPushButton {
                background: #2f3237; border: 1px solid #454a50; padding: 6px 10px;
                border-radius: 6px;
            }
            QPushButton:hover { background: #353a40; }
            QPushButton:pressed { background: #2a2e33; }
            QPushButton:disabled { color: #8b8f94; border-color: #3a3d42; }
            QTableView { gridline-color: #3a3d42; }
            QTableView::item:selected { background: #3d4650; }
            QHeaderView::section {
                background: #26292d; color: #E6E6E6; border: 0px; padding: 6px;
                border-bottom: 1px solid #3a3d42;
            }
            QStatusBar { background: #1b1d20; color: #cfd3d8; }
            QSplitter::handle { background: #2a2d31; }
        """)

        # 초기 로딩
        if hasattr(self.engine, "start_loop"):
            self.engine.start_loop()
        self.load_local_conditions_if_any()
        self.load_candidates()

        # 상태 저장/복원
        self._settings = QSettings("Trade", "AutoTraderUI")
        state = self._settings.value("hsplit_state")
        if state is not None:
            try:
                self.hsplit.restoreState(state)
            except Exception:
                pass
        self.cb_auto_buy.setChecked(self._settings.value("auto_buy", False, type=bool))
        self.cb_auto_sell.setChecked(self._settings.value("auto_sell", False, type=bool))

        # 종목별 MACD 모달 창 관리
        self.macd_windows: dict[str, MacdDialog] = {}
        self.auto_open_macd_modal = False  # ✅ 신규 종목 감지 시 자동 오픈 비활성화
        self.setup_signals()

    def _on_token_ready(self, token: str):
        try:
            # UI 내부에서 쓰는 getter/market_api가 있다면 여기서 갱신
            if not hasattr(self, "getter") or self.getter is None:
                self.getter = DetailInformationGetter(token=self.access_token)
            else:
                self.getter.token = self.access_token

            if not hasattr(self, "market_api") or self.market_api is None:
                self.market_api = SimpleMarketAPI(token=self.access_token)
            else:
                self.market_api.set_token(self.access_token)
        except Exception:
            pass


    def setup_signals(self):
        # Engine → UI
        if hasattr(self.engine, "initialization_complete"):
            self.engine.initialization_complete.connect(self.on_initialization_complete)

    # 종료 시 상태 저장 + 엔진 종료
    def closeEvent(self, event):
        try:
            if hasattr(self, "hsplit"):
                try:
                    self._settings.setValue("hsplit_state", self.hsplit.saveState())
                except Exception:
                    pass

            self._settings.setValue("auto_buy", self.cb_auto_buy.isChecked())
            self._settings.setValue("auto_sell", self.cb_auto_sell.isChecked())

            if hasattr(self.engine, "shutdown"):
                self.engine.shutdown()

            for code6 in list(self._macd_dialogs.keys()):
                if hasattr(self.engine, "stop_macd_stream"):
                    self.engine.stop_macd_stream(code6)

        finally:
            event.accept()

    # ========================================================
    # 🔒 외부(웹소켓/async/스레드)에서 안전하게 호출할 프록시
    # ========================================================
    def threadsafe_new_stock_detail(self, payload: dict):
        try:
            self.sig_new_stock_detail.emit(payload)
        except Exception as e:
            self.append_log(f"[UI] emit 실패: {e}")

    # -------- 툴바/시계 --------
    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, tb)

        act_init = QAction("초기화", self);            act_init.setShortcut("Ctrl+I")
        act_start = QAction("조건 시작", self);        act_start.setShortcut("Ctrl+S")
        act_stop = QAction("조건 중지", self);         act_stop.setShortcut("Ctrl+E")
        act_filter = QAction("필터 실행", self);       act_filter.setShortcut("Ctrl+F")
        act_refresh = QAction("후보 새로고침", self);   act_refresh.setShortcut("F5")

        act_init.triggered.connect(self.on_click_init)
        act_start.triggered.connect(self.on_click_start_condition)
        act_stop.triggered.connect(self.on_click_stop_condition)
        act_filter.triggered.connect(self.on_click_filter)
        act_refresh.triggered.connect(self.load_candidates)

        tb.addAction(act_init)
        tb.addSeparator()
        tb.addAction(act_start)
        tb.addAction(act_stop)
        tb.addSeparator()
        tb.addAction(act_filter)
        tb.addAction(act_refresh)

    def _start_clock(self):
        self._clock = QLabel()
        self.status.addPermanentWidget(self._clock)
        t = QTimer(self)
        t.timeout.connect(lambda: self._clock.setText(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        t.start(1000)
        self._clock_timer = t

    # -------- 버튼 핸들러 --------
    def on_click_init(self):
        try:
            if getattr(self.engine, "_initialized", False):
                QMessageBox.information(self, "안내", "이미 초기화되었습니다.")
                return
            self.engine.initialize()
            self.btn_init.setEnabled(False)  # ✅ 중복 눌림 방지
            self.status.showMessage("초기화 진행 중...", 0)
        except Exception as e:
            QMessageBox.critical(self, "초기화 실패", str(e))

    def on_initialization_complete(self):
        # Engine에서 초기화 완료 시그널을 받으면 이 메서드가 실행됩니다.
        self.status.showMessage("초기화 완료: WebSocket 수신 시작", 3000)
        QMessageBox.information(self, "초기화", "초기화 완료: WebSocket 수신 시작")

    def on_click_start_condition(self):
        item = self.list_conditions.currentItem()
        if not item:
            QMessageBox.warning(self, "안내", "시작할 조건식을 선택하세요.")
            return
        seq = item.data(Qt.UserRole) or ""
        self.engine.send_condition_search_request(seq)
        self.status.showMessage(f"조건검색 시작 요청: {seq}", 3000)

    def on_click_stop_condition(self):
        item = self.list_conditions.currentItem()
        if not item:
            QMessageBox.warning(self, "안내", "중지할 조건식을 선택하세요.")
            return
        seq = item.data(Qt.UserRole) or ""
        self.engine.remove_condition_realtime(seq)
        self.status.showMessage(f"조건검색 중지 요청: {seq}", 3000)

    def on_click_filter(self):
        try:
            out_path = self.perform_filtering_cb()
            self.append_log("✅ 필터링 완료 (finance + technical)")
            self.load_candidates(out_path if isinstance(out_path, str) else None)
            self.status.showMessage("필터링 완료", 3000)
        except Exception as e:
            QMessageBox.critical(self, "오류", str(e))

    # -------- 모달창 관리/주문 --------
    def _open_macd_modal(self, code: str):
        # (이전 호환용) 사용하지 않아도 무방합니다. _open_macd_dialog 사용 권장.
        if not code:
            return
        dlg = self.macd_windows.get(code)
        if dlg is None:
            dlg = MacdDialog(code, parent=self)
            dlg.buy_requested.connect(lambda c: self._on_order_request(c, "BUY"))
            dlg.sell_requested.connect(lambda c: self._on_order_request(c, "SELL"))
            dlg.finished.connect(lambda _: self.macd_windows.pop(code, None))
            self.macd_windows[code] = dlg
            dlg.show(); dlg.raise_(); dlg.activateWindow()
        else:
            dlg.show(); dlg.raise_(); dlg.activateWindow()

    def _on_order_request(self, code: str, side: str):
        """모달의 매수/매도 버튼 → 실제 주문 API 호출(비동기)."""
        ctrl = getattr(self, "auto_trade_controller", None)
        api = getattr(ctrl, "trade_api", None) if ctrl else None
        if not (ctrl and api and hasattr(api, "place_order")):
            QMessageBox.warning(self, "주문 실패", "주문 API가 준비되지 않았습니다.")
            return

        async def go():
            try:
                # 예: 시장가 모사 → order_type="market", qty=1
                res = await api.place_order(side=side, code=code, qty=1, order_type="market", limit_price=None, tag="manual")
                msg = f"[주문] {side} {code} → {getattr(res, 'message', 'ok')}"
                self.append_log(msg)
                QMessageBox.information(self, "주문 결과", msg)
            except Exception as e:
                self.append_log(f"❌ 주문 실패: {e}")
                QMessageBox.critical(self, "주문 실패", str(e))

        loop = getattr(self.engine, "loop", None)
        if loop and hasattr(loop, "create_task"):
            loop.create_task(go())
        else:
            try:
                asyncio.get_event_loop().create_task(go())
            except RuntimeError:
                self.append_log("⚠️ asyncio 루프 없음: 주문 제출 실패")

    # -------- 카드 렌더 --------
    def _render_card(self, code: str, html: str):
        key = str(code) if code else f"__nocode__:{hash(html)}"

        # 내용이 완전히 같으면 아무 것도 안 함
        if key in self._cards and self._cards[key] == html:
            return

        # 교체/추가 후 최신을 맨 앞으로 이동
        self._cards[key] = html
        self._cards.move_to_end(key, last=False)

        # 상한 초과 시 오래된 카드부터 제거
        while len(self._cards) > self._card_limit:
            self._cards.popitem(last=True)

        # 전체를 다시 그리기
        self.text_result.setHtml("\n".join(self._cards.values()))

    # -------- 브리지 → UI --------
    @Slot(str)
    def append_log(self, text: str):
        self.text_log.append(str(text))

    @Slot(list)
    def populate_conditions(self, conditions: list):
        self.list_conditions.clear()
        normalized = []
        for cond in (conditions or []):
            if isinstance(cond, dict):
                seq = str(cond.get("seq", "")).strip()
                name = str(cond.get("name", "(이름 없음)")).strip()
            elif isinstance(cond, (list, tuple)) and len(cond) >= 2:
                seq = str(cond[0]).strip()
                name = str(cond[1]).strip()
            else:
                continue
            if seq or name:
                normalized.append({"seq": seq, "name": name})

        for c in normalized:
            item = QListWidgetItem(f"[{c['seq']}] {c['name']}")
            item.setData(Qt.UserRole, c['seq'])
            self.list_conditions.addItem(item)

        self._update_cond_info()
        self.append_log(f"✅ 조건식 {len(normalized)}개 로드")

    @Slot(str)
    def on_new_stock(self, code: str):
        self.label_new_stock.setText(f"신규 종목: {code}")
        self.status.showMessage(f"신규 종목: {code}", 3000)


    def _open_macd_dialog(self, code: str):
        code6 = str(code)[-6:].zfill(6)
        dlg = self._macd_dialogs.get(code6)
        if dlg and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return
        
        # MacdDialog 객체 생성 및 연결
        from core.macd_dialog import MacdDialog
        dlg = MacdDialog(code=code6, bridge=self.bridge, parent=self)

        # 다이얼로그가 닫힐 때 딕셔너리에서 제거
        def on_dialog_closed():
            if code6 in self._macd_dialogs:
                del self._macd_dialogs[code6]
            # 🔻 다이얼로그가 닫히면 실시간 스트림 중지
            try:
                if hasattr(self.engine, "stop_macd_stream"):
                    self.engine.stop_macd_stream(code6)
            except Exception as e:
                logger.warning("stop_macd_stream failed for %s: %s", code6, e)
        dlg.finished.connect(on_dialog_closed)

        dlg.show()
        self._macd_dialogs[code6] = dlg

        # 🔺 다이얼로그가 보이는 동안만 실시간 스트림 시작
        try:
            if hasattr(self.engine, "start_macd_stream"):
                self.engine.start_macd_stream(code6)
        except Exception as e:
            logger.warning("start_macd_stream failed for %s: %s", code6, e)

    @Slot(dict)
    def on_macd_series_dict(self, payload: dict):
        # 필요한 경우 여기서 테이블/라벨 갱신
        pass

    @Slot(str, float, float, float)
    def on_macd_data(self, code: str, macd: float, signal: float, hist: float):
        """엔진/브릿지에서 올라오는 MACD 실시간 수신 슬롯"""
        code6 = str(code)[-6:].zfill(6)
        self.status.showMessage(f"[MACD] {code6} M:{macd:.2f} S:{signal:.2f} H:{hist:.2f}", 2500)

        dlg = self._macd_dialogs.get(code6)
        if dlg and hasattr(dlg, "push_point"):
            try:
                # `MacdDialog`에 `push_point` 메서드가 추가되었음.
                dlg.push_point(macd=macd, signal=signal, hist=hist)
            except Exception as e:
                logger.error(f"[MACD dlg] update fail for {code6}: {e}")
        
        logger.info(f"[MACD] {code6} | MACD:{macd:.2f} Signal:{signal:.2f} Hist:{hist:.2f}")


    @Slot(dict)
    def on_macd_series_ready(self, data: dict):
        code = data.get("code")
        tf = (data.get("tf") or "").lower()
        series = data.get("series") or data.get("values")
        if not code or not tf or not series:
            return

        code6 = str(code)[-6:].zfill(6)

        # ✅ 캐시에 저장 (원본 data 그대로)
        bucket = self._macd_cache.setdefault(code6, {})
        bucket[tf] = data

        # 다이얼로그가 열려 있으면 즉시 반영
        dlg = self._macd_dialogs.get(code6)
        if dlg:
            dlg.on_macd_series(data)


    @staticmethod
    def _to_float_loose(x):
        if x is None:
            return None
        s = str(x).strip()
        if s in ("", "-"):
            return None
        s = s.replace("%", "").replace(",", "")
        neg = s.startswith("-")
        s = s.lstrip("+-")
        try:
            v = float(s)
            return -v if neg else v
        except Exception:
            return None

    @staticmethod
    def _pick(payload, keys, default="-"):
        for k in keys:
            v = payload.get(k)
            if v not in (None, "", "-"):
                return str(v)
        return default
    
    def _trigger_auto_trade(self, trade_payload: dict):
        # 체크박스 게이트
        if not (self.cb_auto_buy.isChecked() or self.cb_auto_sell.isChecked()):
            return

        ctrl = getattr(self, "auto_trader", None)
        if not ctrl:
            return

        # 체크박스 → 설정 반영
        ctrl.settings.master_enable = True
        ctrl.settings.auto_buy = self.cb_auto_buy.isChecked()
        ctrl.settings.auto_sell = self.cb_auto_sell.isChecked()

        # ✅ 실제 주문 비동기 실행 (ladder/단일 모두 trade_payload에 따라 처리됨)
        if trade_payload:
            coro = ctrl.handle_signal(trade_payload)
            loop = getattr(self.engine, "loop", None)
            if loop and hasattr(loop, "create_task"):
                loop.create_task(coro)
            else:
                try:
                    asyncio.get_event_loop().create_task(coro)
                except RuntimeError:
                    self.append_log("⚠️ asyncio 루프 없음: auto-trade 스킵")


    @Slot(dict)
    def on_new_stock_detail(self, payload: dict):
        logger.info("[UI] on_new_stock_detail: code=%s, cond=%s",
                    payload.get("stock_code"), payload.get("condition_name"))
        #logger.debug("[UI] payload keys: %s", list(payload.keys()))

        # ── KA10015 전용: 리스트의 첫 행(row0)을 최상위로 올림 ──
        row0 = None
        if isinstance(payload.get("open_pric_pre_flu_rt"), list) and payload["open_pric_pre_flu_rt"]:
            row0 = payload["open_pric_pre_flu_rt"][0]
        elif isinstance(payload.get("rows"), list) and payload["rows"]:  # 혹시 서버/내부에서 rows로 넘길 때
            row0 = payload["rows"][0]

        flat = dict(payload)
        if isinstance(row0, dict):
            # 이미 같은 키가 있으면 최상위를 우선하기 위해 setdefault 사용
            for k, v in row0.items():
                flat.setdefault(k, v)

        # ── 여기부터는 flat 기준으로 그대로 사용 ──
        code = (flat.get("stock_code") or "").strip()
        name = flat.get("stock_name") or flat.get("stk_nm") or flat.get("isu_nm") or "종목명 없음"
        cond = flat.get("condition_name") or ""

        cur      = self._pick(flat, ["cur_prc", "stck_prpr", "price"])
        rt       = self._pick(flat, ["flu_rt", "prdy_ctrt"])
        opn      = self._pick(flat, ["open_pric", "stck_oprc"])
        high     = self._pick(flat, ["high_pric", "stck_hgpr"])
        low      = self._pick(flat, ["low_pric", "stck_lwpr"])
        vol      = self._pick(flat, ["now_trde_qty", "acml_vol", "trqu"])
        strength = self._pick(flat, ["cntr_str", "antc_tr_pbmn", "cttr"])
        opn_diff = self._pick(flat, ["open_pric_pre", "opn_diff", "prdy_vrss"])

        logger.info("[UI] extracted: name=%s cur=%s rt=%s opn=%s high=%s low=%s vol=%s strength=%s opn_diff=%s",
                    name, cur, rt, opn, high, low, vol, strength, opn_diff)

        # 등락률/색 (카드용)
        try:
            rt_val_card = float(str(rt).replace("%", "").replace(",", ""))
        except Exception:
            rt_val_card = 0.0
        color = "#e53935" if rt_val_card > 0 else ("#43a047" if rt_val_card < 0 else "#cfcfcf")
        rt_fmt = f"{rt_val_card:.2f}%"
        cond_chip = f'<span style="margin-left:8px; font-size:10px; padding:2px 6px; border:1px solid #2c2c2c; border-radius:10px; color:#cfd8dc;">[{cond}]</span>' if cond else ""

        # 카드 렌더(이름 위 분할선 + 종목명 주황색)
        code6 = str(code)[-6:].zfill(6)
        detail_btn = f'<a href="macd:{code6}" style="margin-left:8px; font-size:11px; padding:2px 8px; border:1px solid #4c566a; border-radius:10px; color:#e0e0e0; text-decoration:none; background:#2b2f36;">상세</a>'
        html = f"""
        <div style=\"margin:10px 0;\">
          <div style=\"border:1px solid #2c2c2c; border-left:6px solid {color}; background:#161616; padding:10px; border-radius:8px;\">
            <table style=\"width:100%; border-collapse:collapse;\">
              <tr>
                <td style=\"vertical-align:top; width:70%;\">
                  <div style=\"padding-top:6px; border-top:1px dashed #333; margin-top:2px;\">                
                    <b style=\"font-size:11px; color:#ff9800;\">{name}</b>
                    <span style=\"color:#9aa0a6; margin-left:5px;\">{code6}</span>
                    {cond_chip}
                    {detail_btn}
                  </div>
                </td>
                <td style=\"vertical-align:top; text-align:right;\">
                  <div style=\"font-size:12px; color:#bdbdbd;\">현재가</div>
                  <div style=\"font-size:18px; font-weight:700; font-family:Consolas,'Courier New',monospace;\">{cur}</div>
                  <div style=\"margin-top:2px; font-weight:700; color:{color};\">{rt_fmt}</div>
                </td>
              </tr>
            </table>

            <hr style=\"border:0; border-top:1px dashed #333; margin:8px 0;\">

            <div style=\"font-size:12px; color:#bdbdbd; line-height:1.6;\">
              시가 <b style=\"color:#e0e0e0;\">{opn}</b>
              <span style=\"color:#8e8e8e;\"> (시가대비 {opn_diff})</span><br>
              고가 <b style=\"color:#e0e0e0;\">{high}</b>&nbsp;&nbsp;저가 <b style=\"color:#e0e0e0;\">{low}</b><br>
              거래량 <b style=\"color:#e0e0e0;\">{vol}</b>&nbsp;&nbsp;체결강도 <b style=\"color:#e0e0e0;\">{strength}</b>
            </div>
          </div>
        </div>
        """
        # 종목명이 유효하지 않으면 카드 표시 생략
        if (not name) or (name.strip() in ("종목명 없음", "", "-")):
            logger.info("skip card render: name is empty for code=%s", code6)
        else:
            self._render_card(code6, html)

        if code:
            self.label_new_stock.setText(f"신규 종목: {code}")

        # ✅ 모달 배지(현재가/등락률) 업데이트 — 자동 오픈 금지, 열려있을 때만
        raw_code = (payload.get("stock_code") or "").strip()
        if not raw_code:
            logger.warning("No stock code found in payload; cannot update MACD dialog.")
            return
        code6 = str(raw_code)[-6:].zfill(6)

        dlg = self._macd_dialogs.get(code6)
        if not dlg:
            return  # 열려있지 않으면 아무 것도 안 함 (더블클릭으로만 띄움)

        # 다이얼로그에 현재가/등락률 업데이트
        raw_rt = self._pick(flat, ["flu_rt", "prdy_ctrt"])  # 문자열(%, 콤마 포함 가능)
        try:
            rt_val = float(str(raw_rt).replace("%", "").replace(",", ""))
        except (ValueError, TypeError):
            rt_val = None
        dlg.update_quote(cur, rt_val)

        # ✅ 자동매매 트리거 (체크박스 켜진 경우에만 내부에서 실행)
        self._trigger_auto_trade(payload)

        # 안전한 int 변환 헬퍼(클래스 메서드로 빼도 좋습니다)
        def _to_int(x, default=0):
            try:
                if x is None: return default
                s = str(x).replace(",", "").strip()
                return int(float(s))
            except Exception:
                return default

        # --- 시뮬 엔진에 마켓 이벤트 공급 (paper_mode일 때만 의미 있음) ---
        ctrl = getattr(self, "auto_trader", None)
        if ctrl and getattr(ctrl, "paper_mode", False):
            event = {
                "stk_cd": code6,  # 표준화된 6자리 코드
                "last": _to_int(cur),
                "bid": _to_int(self._pick(flat, ["bid","stck_bidp1","bid_prc"])),
                "ask": _to_int(self._pick(flat, ["ask","stck_askp1","ask_prc"])),
                "high": _to_int(high),
                "low": _to_int(low),
                "ts": flat.get("ts_iso") or flat.get("ts") or "",
            }
            ctrl.feed_market_event(event)

    # -------- 링크 클릭(상세) 핸들러 --------
    @Slot(QUrl)
    def _on_result_anchor_clicked(self, url: QUrl):
        try:
            if not url or url.scheme() != 'macd':
                return
            code = url.path().lstrip('/') or url.host() or url.toString()[5:]
            if code:
                self._open_macd_dialog(code)
        except Exception as e:
            logger.error(f"anchor click error: {e}")

    # -------- 후보 로딩/검색 --------
    def load_candidates(self, path: str = None):
        if path is None:
            path = os.path.join(self.project_root, "candidate_stocks.csv")

        if not os.path.exists(path):
            self.append_log(f"ℹ️ 후보 종목 파일 없음: {path}")
            self.cand_model.setDataFrame(pd.DataFrame(columns=["회사명", "종목코드", "현재가"]))
            return

        try:
            df = pd.read_csv(path, encoding="utf-8-sig")
            # 컬럼 정규화
            rename_map = {}
            for col in list(df.columns):
                low = col.lower()
                if low in {"stock_name", "name", "종목명", "kor_name"}:
                    rename_map[col] = "회사명"
                elif low in {"stock_code", "code", "종목코드", "ticker"}:
                    rename_map[col] = "종목코드"
                elif low in {"price", "현재가", "close", "prc"}:
                    rename_map[col] = "현재가"
            if rename_map:
                df = df.rename(columns=rename_map)
            for need in ["회사명", "종목코드", "현재가"]:
                if need not in df.columns:
                    df[need] = ""

            df = df[["회사명", "종목코드", "현재가"]]
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

    # -------- 로컬 conditions.json --------
    def load_local_conditions_if_any(self):
        static_dir = os.path.join(self.project_root, "static")
        path = os.path.join(static_dir, "conditions.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.populate_conditions(data)
                    self.append_log("📄 로컬 conditions.json 로드됨")
            except Exception as e:
                self.append_log(f"⚠️ 로컬 조건식 로드 실패: {e}")

    # ===== 신규: 후보 테이블 더블클릭 핸들러 =====
    @Slot(QModelIndex)
    def on_candidate_double_clicked(self, proxy_index: QModelIndex):
        if not proxy_index or not proxy_index.isValid():
            return
        try:
            src_index = self.cand_proxy.mapToSource(proxy_index)
            row = src_index.row()
            code = self.cand_model.data(self.cand_model.index(row, self.COL_CODE))
            if code:
                self._open_macd_dialog(code)
        except Exception as e:
            logger.error(f"on_candidate_double_clicked error: {e}")
