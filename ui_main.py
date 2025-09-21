# ui_main.py
from __future__ import annotations

import os
import json
import logging
import asyncio
from datetime import datetime
from collections import OrderedDict
from typing import Dict, Any, Optional

import pandas as pd

# QtCore
from PySide6.QtCore import (
    Qt, QTimer, Signal, Slot, QAbstractTableModel,
    QModelIndex, QSettings, QSortFilterProxyModel, QUrl
)

# QtWidgets
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QMessageBox,
    QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QStatusBar,
    QTableView, QHeaderView, QLineEdit, QToolBar, QListWidget,
    QTextEdit, QListWidgetItem, QTextBrowser, QSplitter, QCheckBox
)

from core.detail_information_getter import DetailInformationGetter, SimpleMarketAPI
from core.macd_calculator import macd_bus
from core.macd_dialog import MacdDialog


logger = logging.getLogger("ui_main")
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
            self.dataChanged.emit(idx, idx, [Qt.DisplayRole, Qt.ToolTipRole])


# ----------------------------
# 고도화 UI MainWindow
# ----------------------------
class MainWindow(QMainWindow):
    """
    main.py에서 넘겨주는 것들:
      - bridge: AsyncBridge 인스턴스
      - engine: Engine 인스턴스 (start_loop/initialize 등 보유)
      - perform_filtering_cb: callable -> 필터 실행 후 출력 경로(str) 반환 가능
      - project_root: str
    """

    # 비UI → UI 스레드 안전 전환용 시그널
    sig_new_stock_detail = Signal(dict)

    def __init__(self, bridge, engine, perform_filtering_cb, project_root: str):
        super().__init__()
        self.setWindowTitle("조건검색 & MACD 모니터")
        self.resize(1180, 760)

        self.bridge = bridge
        self.engine = engine
        self.perform_filtering_cb = perform_filtering_cb
        self.project_root = project_root


        # 다이얼로그/스트림 상태
        self._macd_dialogs: dict[str, QDialog] = {}
        self._active_macd_streams: set[str] = set()
        self._last_stream_req_ts: dict[str, pd.Timestamp] = {}
        self._stream_debounce_sec: int = 15

        # 상단 툴바 + 레이아웃
        self._build_toolbar()
        self._build_layout()

        # 상태바 + 시계
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("준비됨")
        self._start_clock()
        self.label_new_stock = QLabel("신규 종목 없음")
        self.label_new_stock.setObjectName("label_new_stock")
        self.status.addPermanentWidget(self.label_new_stock)

        # 시그널 연결
        self._connect_signals()

        # 스타일
        self._apply_stylesheet()

        # 초기 로딩
        if hasattr(self.engine, "start_loop"):
            self.engine.start_loop()
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

        # 옵션: 자동 오픈 off
        self.auto_open_macd_modal = False

    # ---- 토큰 갱신 수신 ----
    def _on_token_ready(self, token: str):
        try:
            if not hasattr(self, "getter") or self.getter is None:
                self.getter = DetailInformationGetter(token=token)
            else:
                self.getter.token = token

            if not hasattr(self, "market_api") or self.market_api is None:
                self.market_api = SimpleMarketAPI(token=token)
            else:
                self.market_api.set_token(token)
        except Exception:
            pass

    # ---- 신호 연결 ----
    def _connect_signals(self):
        # 버튼
        self.btn_init.clicked.connect(self.on_click_init)
        self.btn_start.clicked.connect(self.on_click_start_condition)
        self.btn_stop.clicked.connect(self.on_click_stop_condition)
        self.btn_filter.clicked.connect(self.on_click_filter)

        # 입력/목록
        self.search_conditions.textChanged.connect(self._filter_conditions)
        self.search_candidates.textChanged.connect(self._filter_candidates)
        self.list_conditions.itemSelectionChanged.connect(self._update_cond_info)

        # 브리지
        self.bridge.log.connect(self.append_log)
        self.bridge.condition_list_received.connect(self.populate_conditions)
        self.bridge.macd_series_ready.connect(self.on_macd_series_ready, Qt.UniqueConnection)
        self.bridge.macd_data_received.connect(self.on_macd_data, Qt.UniqueConnection)
        self.bridge.new_stock_received.connect(self.on_new_stock)
        self.bridge.token_ready.connect(self._on_token_ready)
        if hasattr(self.bridge, "new_stock_detail_received"):
            self.bridge.new_stock_detail_received.connect(self.on_new_stock_detail)

        # 비UI→UI 프록시
        self.sig_new_stock_detail.connect(self.on_new_stock_detail)

        # 엔진 초기화 완료
        if hasattr(self.engine, "initialization_complete"):
            self.engine.initialization_complete.connect(self.on_initialization_complete)

    # ---- 레이아웃 빌드 ----
    def _build_layout(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        main_split = QSplitter(Qt.Horizontal)
        root_layout.addWidget(main_split)

        # 좌측 패널
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

        # 우측 패널
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

        # 상단(좌/우) + 하단(로그)
        vsplit = QSplitter(Qt.Vertical)
        right_layout.addWidget(vsplit, 1)

        # 상단 좌/우
        hsplit = QSplitter(Qt.Horizontal)
        self.hsplit = hsplit
        vsplit.addWidget(hsplit)

        # 상단-좌: 후보 테이블
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

        # 컬럼 인덱스
        self.COL_NAME = 0
        self.COL_CODE = 1
        self.COL_PRICE = 2

        hsplit.addWidget(pane_top_left)

        # 상단-우: 결과 카드 영역
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

        self.cb_auto_buy.stateChanged.connect(lambda _:
            (getattr(self, "auto_trade_controller", None) and
             setattr(self.auto_trade_controller.settings, "auto_buy", self.cb_auto_buy.isChecked()))
        )
        self.cb_auto_sell.stateChanged.connect(lambda _:
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

        hsplit.addWidget(pane_top_right)
        hsplit.setSizes([680, 440])

        # 하단: 로그
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

    # ---- 스타일 ----
    def _apply_stylesheet(self):
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

    # ---- 툴바 ----
    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, tb)

        act_init = tb.addAction("초기화")
        act_init.setShortcut("Ctrl+I")
        act_init.triggered.connect(self.on_click_init)

        tb.addSeparator()
        act_start = tb.addAction("조건 시작")
        act_start.setShortcut("Ctrl+S")
        act_start.triggered.connect(self.on_click_start_condition)

        act_stop = tb.addAction("조건 중지")
        act_stop.setShortcut("Ctrl+E")
        act_stop.triggered.connect(self.on_click_stop_condition)

        tb.addSeparator()
        act_filter = tb.addAction("필터 실행")
        act_filter.setShortcut("Ctrl+F")
        act_filter.triggered.connect(self.on_click_filter)

        act_refresh = tb.addAction("후보 새로고침")
        act_refresh.setShortcut("F5")
        act_refresh.triggered.connect(self.load_candidates)

    def _start_clock(self):
        self._clock = QLabel()
        self.status.addPermanentWidget(self._clock)
        t = QTimer(self)
        t.timeout.connect(lambda: self._clock.setText(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        t.start(1000)
        self._clock_timer = t

    # ---- 종료시 상태 저장 + 엔진 종료 ----
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

            # 스트림 중지(선택): 유지하고 싶으면 이 블록 제거
            for code6 in list(self._active_macd_streams):
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

    # -------- 버튼 핸들러 --------
    def on_click_init(self):
        try:
            if getattr(self.engine, "_initialized", False):
                QMessageBox.information(self, "안내", "이미 초기화되었습니다.")
                return
            self.engine.initialize()
            self.btn_init.setEnabled(False)
            # self.status.showMessage("초기화 진행 중...", 0)
        except Exception as e:
            QMessageBox.critical(self, "초기화 실패", str(e))

    def on_initialization_complete(self):
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

    # -------- 다이얼로그 열기 --------
    def _open_macd_dialog(self, code: str):
        code6 = str(code)[-6:].zfill(6)
        dlg = self._macd_dialogs.get(code6)
        if dlg and dlg.isVisible():
            dlg.raise_(); dlg.activateWindow(); return

        self._ensure_macd_stream(code6)

        dlg = MacdDialog(code=code6, parent=self)
        dlg.finished.connect(lambda _: self._macd_dialogs.pop(code6, None))
        dlg.show()
        self._macd_dialogs[code6] = dlg

    # -------- 카드 렌더 --------
    def _render_card(self, code: str, html: str):
        key = str(code) if code else f"__nocode__:{hash(html)}"

        if not hasattr(self, "_cards"):
            self._cards = OrderedDict()
        if key in self._cards and self._cards[key] == html:
            return

        self._cards[key] = html
        self._cards.move_to_end(key, last=False)

        while len(self._cards) > getattr(self, "_card_limit", 30):
            self._cards.popitem(last=True)

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

    @Slot(str, float, float, float)
    def on_macd_data(self, code: str, macd: float, signal: float, hist: float):
        """레거시 4-튜플 신호 (표시만)"""
        code6 = str(code)[-6:].zfill(6)
        self.status.showMessage(f"[MACD] {code6} M:{macd:.2f} S:{signal:.2f} H:{hist:.2f}", 2500)
        # 새 다이얼로그는 macd_bus를 직접 구독하므로, 별도 push 필요 없음
        logger.info(f"[MACD] {code6} | MACD:{macd:.2f} Signal:{signal:.2f} Hist:{hist:.2f}")

    @Slot(dict)
    def on_macd_series_ready(self, data: dict):
        """브리지 경유 새 포맷 수신. 다이얼로그는 macd_bus를 직접 구독하므로 여기서는 캐시/표시만."""
        code = data.get("code")
        tf = (data.get("tf") or "").lower()
        series = data.get("series") or data.get("values")
        if not code or not tf or not series:
            return

        code6 = str(code)[-6:].zfill(6)

        logger.info("[ui_main] on_macd_series_ready: %s", code6)
        # 카드 등 다른 표시를 원하면 여기서 가공
        # (현재는 다이얼로그가 store(feed)에서 바로 로드하므로 별도 처리 불필요)
        dlg = self._macd_dialogs.get(code6)
        if dlg:
            # 다이얼로그는 feed↔bus로 최신 반영되므로 no-op 가능
            pass

    # ---- 변환/도우미 ----
    @staticmethod
    def _pick(payload, keys, default="-"):
        for k in keys:
            v = payload.get(k)
            if v not in (None, "", "-"):
                return str(v)
        return default

    def _ensure_macd_stream(self, code6: str):
        """신규 종목 감지될 때 MACD 스트림을 안전하게 시작."""
        try:
            now = pd.Timestamp.now(tz="Asia/Seoul")
            last = self._last_stream_req_ts.get(code6)
            if last is not None and (now - last).total_seconds() < getattr(self, "_stream_debounce_sec", 15):
                logger.debug("debounce: skip start_macd_stream for %s", code6)
                return
            self._last_stream_req_ts[code6] = now

            if code6 in self._active_macd_streams:
                logger.debug("start_macd_stream: already active for %s", code6)
                return

            if hasattr(self.engine, "start_macd_stream"):
                self.engine.start_macd_stream(code6)
                self._active_macd_streams.add(code6)
                logger.info("✅ started MACD stream for %s (UI ensure)", code6)
            else:
                logger.warning("engine has no start_macd_stream")
        except Exception as e:
            logger.warning("start_macd_stream failed for %s: %s", code6, e)

    # ---- 신규 종목 상세 수신 ----
    @Slot(dict)
    def on_new_stock_detail(self, payload: dict):
        logger.info("[UI] on_new_stock_detail: code=%s, cond=%s",
                    payload.get("stock_code"), payload.get("condition_name"))

        row0 = None
        if isinstance(payload.get("open_pric_pre_flu_rt"), list) and payload["open_pric_pre_flu_rt"]:
            row0 = payload["open_pric_pre_flu_rt"][0]
        elif isinstance(payload.get("rows"), list) and payload["rows"]:
            row0 = payload["rows"][0]

        flat = dict(payload)
        if isinstance(row0, dict):
            for k, v in row0.items():
                flat.setdefault(k, v)

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

        # 등락률/색
        try:
            rt_val_card = float(str(rt).replace("%", "").replace(",", ""))
        except Exception:
            rt_val_card = 0.0
        color = "#e53935" if rt_val_card > 0 else ("#43a047" if rt_val_card < 0 else "#cfcfcf")
        rt_fmt = f"{rt_val_card:.2f}%"
        cond_chip = f'<span style="margin-left:8px; font-size:10px; padding:2px 6px; border:1px solid #2c2c2c; border-radius:10px; color:#cfd8dc;">[{cond}]</span>' if cond else ""

        code6 = str(code)[-6:].zfill(6)
        detail_btn = f'<a href="macd:{code6}" style="margin-left:8px; font-size:11px; padding:2px 8px; border:1px solid #4c566a; border-radius:10px; color:#e0e0e0; text-decoration:none; background:#2b2f36;">상세</a>'

        html = f"""
        <div style="margin:10px 0;">
          <div style="border:1px solid #2c2c2c; border-left:6px solid {color}; background:#161616; padding:10px; border-radius:8px;">
            <table style="width:100%; border-collapse:collapse;">
              <tr>
                <td style="vertical-align:top; width:70%;">
                  <div style="padding-top:6px; border-top:1px dashed #333; margin-top:2px;">
                    <b style="font-size:11px; color:#ff9800;">{name}</b>
                    <span style="color:#9aa0a6; margin-left:5px;">{code6}</span>
                    {cond_chip}
                    {detail_btn}
                  </div>
                </td>
                <td style="vertical-align:top; text-align:right;">
                  <div style="font-size:12px; color:#bdbdbd;">현재가</div>
                  <div style="font-size:18px; font-weight:700; font-family:Consolas,'Courier New',monospace;">{cur}</div>
                  <div style="margin-top:2px; font-weight:700; color:{color};">{rt_fmt}</div>
                </td>
              </tr>
            </table>

            <hr style="border:0; border-top:1px dashed #333; margin:8px 0;">

            <div style="font-size:12px; color:#bdbdbd; line-height:1.6;">
              시가 <b style="color:#e0e0e0;">{opn}</b>
              <span style="color:#8e8e8e;"> (시가대비 {opn_diff})</span><br>
              고가 <b style="color:#e0e0e0;">{high}</b>&nbsp;&nbsp;저가 <b style="color:#e0e0e0;">{low}</b><br>
              거래량 <b style="color:#e0e0e0;">{vol}</b>&nbsp;&nbsp;체결강도 <b style="color:#e0e0e0;">{strength}</b>
            </div>
          </div>
        </div>
        """
        if (not name) or (name.strip() in ("종목명 없음", "", "-")):
            logger.info("skip card render: name is empty for code=%s", code6)
        else:
            self._render_card(code6, html)

        if code:
            self.label_new_stock.setText(f"신규 종목: {code}")

        # 스트림 확보
        if code6:
            self._ensure_macd_stream(code6)

        # 열려있을 때만 다이얼로그 배지 업데이트 (현재가/등락률)
        dlg = self._macd_dialogs.get(code6)
        if not dlg:
            return
        try:
            raw_rt = self._pick(flat, ["flu_rt", "prdy_ctrt"])
            rt_val = float(str(raw_rt).replace("%", "").replace(",", ""))
        except Exception:
            rt_val = None
        dlg.update_quote(cur, rt_val)

        # (선택) 자동매매 트리거 → 필요시 구현
        # self._trigger_auto_trade(payload)

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
