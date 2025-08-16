# ui_main.py
import os
import json
import pandas as pd
from datetime import datetime
from typing import Dict, Any

from PyQt5.QtCore import (
    Qt, QSortFilterProxyModel, QTimer, QAbstractTableModel, QModelIndex, pyqtSlot, pyqtSignal, QSettings

)
from PyQt5.QtWidgets import (
    QDialog, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QSplitter,
    QPushButton, QListWidget, QListWidgetItem, QLabel, QTextEdit, QMessageBox,
    QLineEdit, QTableView, QToolBar, QAction, QHeaderView, QStatusBar
)

class _Toast(QDialog):
    def __init__(self, parent, text: str, timeout_ms: int = 2500):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.ToolTip
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        # 간단한 라벨 UI
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

        # 자동 종료
        QTimer.singleShot(timeout_ms, self.close)

    def show_at_bottom_right(self, margin: int = 16):
        if not self.parent():
            self.show()
            return
        parent_geom = self.parent().geometry()
        x = parent_geom.x() + parent_geom.width() - self.width() - margin
        y = parent_geom.y() + parent_geom.height() - self.height() - margin - 40  # 상태바 고려
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
        return str(section + 1)

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsSelectable | Qt.ItemIsEnabled


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

    # ✅ 비UI 스레드 → UI 스레드 안전 전환용 시그널 (dict payload)
    sig_new_stock_detail = pyqtSignal(dict)

    def __init__(self, bridge, engine, perform_filtering_cb, project_root: str):
        super().__init__()
        self.setWindowTitle("조건검색 & MACD 모니터 ")
        self.resize(1180, 760)

        self.bridge = bridge
        self.engine = engine
        self.perform_filtering_cb = perform_filtering_cb
        self.project_root = project_root

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

        # ========== 좌측 패널: 조건식/검색/버튼 ==========
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

        # ========== 우측 패널: 상단(좌/우) + 하단(로그) ==========
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)


        # 세로 분할: 상단(좌/우) | 하단(로그)
        vsplit = QSplitter(Qt.Vertical)
        right_layout.addWidget(vsplit, 1)

        # ── 상단: 좌우 스플리터 ──
        hsplit = QSplitter(Qt.Horizontal)
        vsplit.addWidget(hsplit)

        # (상단-좌) 25일이내 급등 종목 (검색+테이블)
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
        self.cand_proxy.setFilterKeyColumn(-1)  # 모든 컬럼 검색
        self.cand_table.setModel(self.cand_proxy)
        self.cand_table.setSortingEnabled(False)
        self.cand_table.horizontalHeader().setStretchLastSection(True)
        self.cand_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.cand_table.setSelectionBehavior(QTableView.SelectRows)
        self.cand_table.setAlternatingRowColors(False)  # 줄무늬 제거
        self.cand_table.verticalHeader().setVisible(False)   # 행 번호 숨김
        self.cand_table.setCornerButtonEnabled(False)        # 좌상단 코너 버튼 숨김(선택)
        self.cand_table.setAlternatingRowColors(True)        # 줄무늬

        top_left.addWidget(self.cand_table, 1)

        hsplit.addWidget(pane_top_left)

        # (상단-우) 종목 검색 결과 (신규 종목 / MACD + 상세 stkinfo 피드)
        pane_top_right = QWidget()
        top_right = QVBoxLayout(pane_top_right)
        top_right.addWidget(QLabel("종목 검색 결과"))
        self.text_result = QTextEdit(); self.text_result.setReadOnly(True)
        top_right.addWidget(self.text_result, 1)

        # 카드 중복 방지 상태
        self._last_cards: dict[str, str] = {}   # code -> last html
        self._card_limit = 200                  # (선택) 메모리 안전용


        hsplit.addWidget(pane_top_right)
        hsplit.setSizes([680, 440])  # 초기 상단 좌/우 비율

        # ── 하단: 로그 ──
        pane_bottom = QWidget()
        bottom = QVBoxLayout(pane_bottom)
        bottom.addWidget(QLabel("로그"))
        self.text_log = QTextEdit(); self.text_log.setReadOnly(True)
        bottom.addWidget(self.text_log, 1)
        vsplit.addWidget(pane_bottom)
        vsplit.setSizes([540, 220])  # 상/하 초기 비율

        main_split.addWidget(right_panel)
        main_split.setSizes([380, 800])  # 좌/우 초기 비율

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
        self.bridge.new_stock_received.connect(self.on_new_stock)
        self.bridge.macd_data_received.connect(self.on_macd_data)

        # ✔ bridge가 Qt 시그널을 이미 제공한다면 그대로 연결
        if hasattr(self.bridge, "new_stock_detail_received"):
            self.bridge.new_stock_detail_received.connect(self.on_new_stock_detail)

        # ✅ 어떤 스레드/루프에서 오든 UI 스레드로 안전하게 전환되도록 내부 시그널도 연결
        self.sig_new_stock_detail.connect(self.on_new_stock_detail)

        # 스타일(원치 않으면 주석)
        self.setStyleSheet("""
            /* 기본 배경 & 글자색 */
            QMainWindow, QWidget { background: #1f2124; color: #E6E6E6; }
            QLabel { color: #E6E6E6; }

            /* 입력/텍스트/리스트/테이블 */
            QLineEdit, QTextEdit, QListWidget, QTableView {
                background: #2a2d31; color: #E6E6E6; border: 1px solid #3a3d42;
                selection-background-color: #3d4650; selection-color: #ffffff;
                alternate-background-color: #26292d;
            }
            QLineEdit:focus, QTextEdit:focus { border: 1px solid #4d5661; }

            /* 버튼 */
            QPushButton {
                background: #2f3237; border: 1px solid #454a50; padding: 6px 10px;
                border-radius: 6px;
            }
            QPushButton:hover { background: #353a40; }
            QPushButton:pressed { background: #2a2e33; }
            QPushButton:disabled { color: #8b8f94; border-color: #3a3d42; }

            /* 테이블 */
            QTableView { gridline-color: #3a3d42; }
            QTableView::item:selected { background: #3d4650; }
            QHeaderView::section {
                background: #26292d; color: #E6E6E6; border: 0px; padding: 6px;
                border-bottom: 1px solid #3a3d42;
            }

            /* 상태바 & 스플리터 */
            QStatusBar { background: #1b1d20; color: #cfd3d8; }
            QSplitter::handle { background: #2a2d31; }
        """)
        # 초기 로딩
        if hasattr(self.engine, "start_loop"):
            self.engine.start_loop()
        self.load_local_conditions_if_any()
        self.load_candidates()

        self._settings = QSettings("Trade", "AutoTraderUI")
        state = self._settings.value("hsplit_state")
        if state is not None:
            self.hsplit.restoreState(state)

    def closeEvent(self, event):
        try:
            self._settings.setValue("hsplit_state", self.hsplit.saveState())
            self.engine.shutdown()
        finally:
            event.accept()

    # ========================================================
    # 🔒 외부(웹소켓/async/스레드)에서 안전하게 호출할 프록시
    # ========================================================
    def threadsafe_new_stock_detail(self, payload: dict):
        """
        비UI 스레드/async 컨텍스트에서 UI 업데이트를 요청할 때 호출.
        내부에서 self.sig_new_stock_detail.emit(payload)로
        UI 스레드(메인 스레드)에서 on_new_stock_detail이 실행되도록 한다.
        """
        try:
            self.sig_new_stock_detail.emit(payload)
        except Exception as e:
            # 로그 텍스트에 안전하게 찍어두기
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
            self.engine.initialize()
            self.status.showMessage("초기화 완료: WebSocket 수신 시작", 3000)
            QMessageBox.information(self, "초기화", "초기화 완료: WebSocket 수신 시작")
        except Exception as e:
            QMessageBox.critical(self, "초기화 실패", str(e))

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
            out_path = self.perform_filtering_cb()  # main.py에서 전달한 콜백
            self.append_log("✅ 필터링 완료 (finance + technical)")
            self.load_candidates(out_path if isinstance(out_path, str) else None)
            self.status.showMessage("필터링 완료", 3000)
        except Exception as e:
            QMessageBox.critical(self, "오류", str(e))

    def _render_card(self, code: str, html: str):
        key = code or f"__nocode__:{hash(html)}"
        prev = self._last_cards.get(key)
        if prev == html:
            return  # 동일 내용이면 재출력 안 함

        # (선택) 오래된 키 정리
        if len(self._last_cards) >= self._card_limit and key not in self._last_cards:
            # 임의로 하나 제거(간단 버전)
            self._last_cards.pop(next(iter(self._last_cards)))

        self._last_cards[key] = html
        self.text_result.append(html) 


    # -------- 브리지 → UI --------
    @pyqtSlot(str)
    def append_log(self, text: str):
        self.text_log.append(str(text))

    @pyqtSlot(list)
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

    @pyqtSlot(str)
    def on_new_stock(self, code: str):
        self.label_new_stock.setText(f"신규 종목: {code}")
        #self.text_result.append(f"🆕 신규 종목: {code}")
        self.append_log(f"🆕 신규 종목: {code}")
        self.status.showMessage(f"신규 종목: {code}", 3000)
        QMessageBox.information(self, "알림", f"🆕 신규 종목 감지: {code}")


    @pyqtSlot(str, float, float, float)
    def on_macd_data(self, code: str, macd: float, signal: float, hist: float):
        self.text_result.append(f"{code} | MACD:{macd:.2f}  Signal:{signal:.2f}  Hist:{hist:.2f}")

    @staticmethod
    def _pick(payload, keys, default="-"):
        for k in keys:
            v = payload.get(k)
            if v not in (None, "", "-"):
                return str(v)
        return default

    @pyqtSlot(dict)
    def on_new_stock_detail(self, payload: Dict[str, Any]):
        code = (payload.get("stock_code") or "").strip()
        name = payload.get("stock_name") or payload.get("isu_nm") or "종목명 없음"
        cond = payload.get("condition_name") or ""

        # 다양한 케이스 대응
        cur  = self._pick(payload, ["cur_prc", "stck_prpr", "price"])
        rt   = self._pick(payload, ["flu_rt", "prdy_ctrt"])
        opn  = self._pick(payload, ["open_pric", "stck_oprc"])
        high = self._pick(payload, ["high_pric", "stck_hgpr"])
        low  = self._pick(payload, ["low_pric", "stck_lwpr"])
        vol  = self._pick(payload, ["now_trde_qty", "acml_vol", "trqu"])
        strength = self._pick(payload, ["cntr_str", "antc_tr_pbmn", "cttr"])
        opn_diff = self._pick(payload, ["open_pric_pre", "opn_diff", "prdy_vrss"])

        # 등락률 색
        try:
            rt_val = float(str(rt).replace("%","").replace(",",""))
        except Exception:
            rt_val = 0.0
        color = "#e53935" if rt_val > 0 else ("#43a047" if rt_val < 0 else "#cfcfcf")
        rt_fmt = f"{rt_val:.2f}%"
        cond_chip = f'<span style="margin-left:8px; font-size:11px; padding:2px 6px; border:1px solid #2c2c2c; border-radius:10px; color:#cfd8dc;">[{cond}]</span>' if cond else ""

        html = f"""
        <div style="margin:10px 0;">
        <div style="border:1px solid #2c2c2c; border-left:6px solid {color}; background:#161616; padding:10px; border-radius:8px;">
            <table style="width:100%; border-collapse:collapse;">
            
            <tr>
                <td style="vertical-align:top; width:70%;">
                <div style="padding-top:6px; border-top:1px dashed #333; margin-top:2px;">
                    <hr style="border:0; border-top:1px dashed #333; margin:8px 0;">

                    <b style="font-size:15px; color:#ff9800;">{name}</b>
                    <span style="color:#9aa0a6; margin-left:6px;">{code}</span>
                    {cond_chip}
                </div>
                </td>
            </tr>
            </table>


            <div style="font-size:12px; color:#bdbdbd; font-weight:700; line-height:1.6;">
            현재가 <b style="color:#e0e0e0; font-weight:700">{cur}</b>
            <span style="color:#8e8e8e; font-weight:700"> ({rt_fmt})</span><br>
            시가 <b style="color:#e0e0e0;">{opn}</b>
            <span style="color:#8e8e8e;"> (시가대비 {opn_diff})</span><br>
            고가 <b style="color:#e0e0e0;">{high}</b>&nbsp;&nbsp;저가 <b style="color:#e0e0e0;">{low}</b><br>
            거래량 <b style="color:#e0e0e0;">{vol}</b>&nbsp;&nbsp;체결강도 <b style="color:#e0e0e0;">{strength}</b>
            </div>
        </div>
        </div>
        """
        #self.text_result.append(html)
        self._render_card(code, html)
        if code:
            self.label_new_stock.setText(f"신규 종목: {code}")

            try:
                rt_val = float(str(self._pick(payload, ["flu_rt", "prdy_ctrt"], "0")).replace("%","").replace(",",""))
            except Exception:
                rt_val = 0.0
            sign = "▲" if rt_val > 0 else ("▼" if rt_val < 0 else "■")
            QMessageBox.information(self, "신규 종목", f"🆕 {name} ({code}) {sign} {rt_val:.2f}%")


    # -------- 후보 로딩/검색 --------
    def load_candidates(self, path: str = None):
        """
        candidate_stocks.csv를 읽어 테이블에 표기.
        컬럼명이 제각각이어도 회사명/종목코드/현재가로 정규화.
        """
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

    # -------- 종료 --------
    def closeEvent(self, event):
        try:
            self.engine.shutdown()
        finally:
            event.accept()
