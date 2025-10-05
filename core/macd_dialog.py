# core/macd_dialog.py
from __future__ import annotations

from typing import Optional, List, Dict, Any
import logging
import pandas as pd
import pyqtgraph as pg

from PySide6.QtCore import Qt, Slot, QObject, Signal, QThread
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSplitter, QWidget, QPushButton, QTextEdit, QHBoxLayout
)

# 계산기/버스/조회 API
from core.macd_calculator import macd_bus, get_points
from utils.stock_info_manager import stock_info_manager
from utils.gemini_client import GeminiClient

# 결과(JSONL) 유틸 – DailyResultsRecorder 포맷과 호환
from utils.results_store import (
    load_results_for_date, filter_by_symbol, to_dataframe, today_str
)

logger = logging.getLogger(__name__)


# ==============================
# Gemini 분석 워커
# ==============================
class BullishAnalysisWorker(QObject):
    analysis_ready = Signal(str)

    def __init__(self, gemini_client: GeminiClient, code: str, stock_name: str, parent=None):
        super().__init__(parent)
        self._client = gemini_client
        self._code = code
        self._name = stock_name

    @Slot()
    def run(self):
        try:
            extra_context = f"종목 코드: {self._code}\n종목명: {self._name}"
            response_text = self._client.run_briefing(extra_context=extra_context)
            self.analysis_ready.emit(response_text)
        except Exception as e:
            logger.error("Gemini API call failed (model=%s): %s", self._client.model_name, e)
            self.analysis_ready.emit("⚠️ AI 브리핑 실패: 모델 접근 불가/퇴역 또는 네트워크 문제로 보입니다.")


# ==============================
# MACD 상세 다이얼로그
# ==============================
class MacdDialog(QDialog):
    COLS = ["TF", "Time", "MACD", "Hist", "ΔMACD", "ΔHist", "Cross"]
    _ROW_OF = {"5m": 0, "30m": 1}

    # 다이얼로그를 닫아도 유지하고 싶은 데이터 캐시
    _UI_CACHE_BY_CODE: Dict[str, pd.DataFrame] = {}

    def __init__(self, code: str, *, parent=None, bus=macd_bus):
        super().__init__(parent)
        self.code = str(code)[-6:].zfill(6)
        self.bus = bus
        stock_name = stock_info_manager.get_name(self.code)

        self.setWindowFlags(self.windowFlags() | Qt.Window | Qt.WindowMinMaxButtonsHint)
        self.setWindowTitle(f"MACD 상세 - {self.code}")
        self.setModal(False)
        self.resize(1280, 900)          # ▶ 기본 크기 확대
        self.setMinimumSize(1100, 760)

        # ====== 상단 타이틀/버튼 ======
        self.lbl = QLabel(f"종목: <b>{stock_name} ({self.code})</b>")
        self.lbl.setTextFormat(Qt.RichText)

        self.gemini_client = GeminiClient(prompt_file="resources/bullish_analysis_prompt.md")
        self.bullish_analysis_btn = QPushButton("종목 분석")
        self.bullish_analysis_btn.clicked.connect(self._on_bullish_analysis_clicked)

        self.analysis_btn = QPushButton("종목 분석 결과 ▶")
        self.analysis_btn.clicked.connect(self._on_analysis_toggle)

        # ▶ 차트 토글
        self.chart_visible = True
        self.chart_toggle_btn = QPushButton("차트 숨기기 ▲")
        self.chart_toggle_btn.clicked.connect(self._on_chart_toggle)

        self._is_analysis_loaded = False
        self._is_analysis_visible = False

        # ====== 메인 레이아웃 ======
        main_layout = QVBoxLayout(self)

        header_row = QHBoxLayout()
        header_row.addWidget(self.lbl)
        header_row.addStretch(1)
        header_row.addWidget(self.chart_toggle_btn)
        header_row.addWidget(self.bullish_analysis_btn)
        header_row.addWidget(self.analysis_btn)
        main_layout.addLayout(header_row)

        # ====== 상부: MACD 테이블 + 그래프 ======
        self.top_split = QSplitter(Qt.Vertical)

        # MACD 테이블 (5m/30m/1d)
        self.table = QTableWidget(3, len(self.COLS), self)
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setAlternatingRowColors(True)
        self._set_item(0, 0, "5m", center=True)
        self._set_item(1, 0, "30m", center=True)
        self._set_item(2, 0, "1d", center=True)

        # 그래프
        self.graph_widget = pg.PlotWidget(title="MACD & Histogram (10 Recent)")
        self.graph_widget.setBackground('k')
        self.graph_widget.getAxis('bottom').setPen('w')
        self.graph_widget.getAxis('left').setPen('w')
        self.graph_widget.getAxis('left').setTicks([])
        self.graph_widget.plotItem.getAxis('bottom').setTicks([])

        self.top_split.addWidget(self.table)
        self.top_split.addWidget(self.graph_widget)
        self.top_split.setStretchFactor(0, 1)
        self.top_split.setStretchFactor(1, 1)

        # ====== 하부: 오늘자 결과(JSONL) 표 ======
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)

        # 버튼/라벨 행
        btn_row = QHBoxLayout()
        self.refresh_results_btn = QPushButton("당일 결과 새로고침")
        self.refresh_results_btn.clicked.connect(self._on_refresh_results_clicked)
        btn_row.addWidget(QLabel("<b>당일 트레이딩 기록</b>"))   # ▶ 텍스트 변경
        btn_row.addStretch(1)
        btn_row.addWidget(self.refresh_results_btn)
        bottom_layout.addLayout(btn_row)

        # 결과 표
        self.results_table = QTableWidget(0, 7, self)
        self.results_table.setHorizontalHeaderLabels(
            ["Time", "Side", "Price", "Reason", "Source", "Condition", "ReturnMsg"]
        )
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.results_table.setAlternatingRowColors(True)
        bottom_layout.addWidget(self.results_table)

        # ====== 전체 스플리터 구성 ======
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.top_split)
        splitter.addWidget(bottom_widget)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        main_layout.addWidget(splitter)

        # ====== 분석 결과 영역 (초기 숨김) ======
        self.analysis_widget = QWidget()
        self.analysis_widget.setMinimumHeight(200)
        self.analysis_widget.hide()
        analysis_layout = QVBoxLayout(self.analysis_widget)
        analysis_layout.addWidget(QLabel("<b>종목 분석 결과</b>"))
        self.analysis_output = QTextEdit()
        self.analysis_output.setReadOnly(True)
        analysis_layout.addWidget(self.analysis_output)
        main_layout.addWidget(self.analysis_widget)

        # 초기 표시
        self._refresh_all()
        self._load_and_show_results(use_cache_first=True)

        # 버스 구독 (중복 연결 방지)
        try:
            self.bus.macd_series_ready.connect(self._on_bus, Qt.UniqueConnection)
        except TypeError:
            pass

    # -----------------------------
    # 내부 유틸
    # -----------------------------
    def _set_item(self, row: int, col: int, text: str, *, center: bool = False):
        it = QTableWidgetItem(text)
        it.setTextAlignment(Qt.AlignCenter if center else (Qt.AlignRight | Qt.AlignVCenter))
        self.table.setItem(row, col, it)

    @staticmethod
    def _fmt(v: Optional[float], digits: int = 5) -> str:
        try:
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return "—"
            return f"{float(v):.{digits}f}"
        except Exception:
            return "—"

    def _row_of(self, tf: str) -> Optional[int]:
        s = str(tf).strip().lower()
        if s in {"5", "5m", "5min", "m5"}:
            return 0
        if s in {"30", "30m", "30min", "m30"}:
            return 1
        if s in {"1d", "d", "day"}:
            return 2
        return self._ROW_OF.get(s)

    # -----------------------------
    # MACD 테이블 & 그래프
    # -----------------------------
    def _refresh_all(self):
        for tf in ("5m", "30m", "1d"):
            self._refresh_row(tf)

    def _refresh_row(self, tf: str):
        row = self._row_of(tf)
        if row is None:
            return

        pts = get_points(self.code, tf, n=10) or []
        if not pts:
            for c in range(1, len(self.COLS)):
                self._set_item(row, c, "—", center=True)
            return

        last = pts[-1]
        prev = pts[-2] if len(pts) > 1 else None

        ts = last["ts"] if isinstance(last["ts"], pd.Timestamp) else pd.Timestamp(last["ts"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("Asia/Seoul")
        self._set_item(row, 1, ts.strftime("%Y-%m-%d %H:%M"), center=True)

        macd = float(last.get("macd", float("nan")))
        hist = float(last.get("hist", float("nan")))

        self._set_item(row, 2, self._fmt(macd))
        self._set_item(row, 3, self._fmt(hist))

        def _delta(cur, prv):
            if cur is None or prv is None:
                return "—"
            if any(pd.isna(x) for x in (cur, prv)):
                return "—"
            return f"{(float(cur) - float(prv)):+.5f}"

        prev_macd = float(prev["macd"]) if prev else None
        prev_hist = float(prev["hist"]) if prev else None

        self._set_item(row, 4, _delta(macd, prev_macd))
        self._set_item(row, 5, _delta(hist, prev_hist))

        cross = "—"
        if prev is not None and not any(pd.isna(x) for x in (hist, prev["hist"])):
            was_above = float(prev["hist"]) > 0
            now_above = hist > 0
            if (not was_above) and now_above:
                cross = "G-Cross"
            elif was_above and (not now_above):
                cross = "D-Cross"
        self._set_item(row, 6, cross, center=True)

        if tf == "5m" and self.chart_visible:
            self._update_graph(pts)

    def _update_graph(self, pts: List[Dict[str, Any]]):
        """MACD 히스토그램과 MACD 라인을 스케일 조정 후 함께 표시."""
        if len(pts) < 5:
            return

        self.graph_widget.clear()
        zero_line = pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen('w', width=1, style=Qt.DashLine))
        self.graph_widget.addItem(zero_line)

        recent = pts[-10:]
        x_vals = list(range(len(recent)))

        hist_values = [float(p.get("hist", float("nan"))) / 10.0 for p in recent]
        macd_values = [float(p.get("macd", float("nan"))) / 100.0 for p in recent]

        colors = [pg.mkColor('#3cb371') if (h is not None and not pd.isna(h) and h >= 0) else pg.mkColor('#dc143c')
                  for h in hist_values]

        bar_graph = pg.BarGraphItem(x=x_vals, height=hist_values, width=0.6, brushes=colors)
        self.graph_widget.addItem(bar_graph)
        self.graph_widget.plot(x_vals, macd_values, pen=pg.mkPen('c', width=2))
        self.graph_widget.autoRange()

    # -----------------------------
    # 차트 토글
    # -----------------------------
    @Slot()
    def _on_chart_toggle(self):
        self.chart_visible = not self.chart_visible
        self.graph_widget.setVisible(self.chart_visible)
        self.chart_toggle_btn.setText("차트 숨기기 ▲" if self.chart_visible else "차트 보이기 ▼")
        # 차트를 숨길 때 테이블 영역을 넓혀주기 위해 스플리터 비율 손봄
        if self.chart_visible:
            self.top_split.setStretchFactor(0, 1)
            self.top_split.setStretchFactor(1, 1)
        else:
            self.top_split.setStretchFactor(0, 1)
            self.top_split.setStretchFactor(1, 0)
        self.resize(self.size())

    # -----------------------------
    # 분석 패널 토글/로드
    # -----------------------------
    @Slot()
    def _on_analysis_toggle(self):
        self._is_analysis_visible = not self._is_analysis_visible
        self.analysis_widget.setVisible(self._is_analysis_visible)
        self.analysis_btn.setText("종목 분석 결과 ▼" if self._is_analysis_visible else "종목 분석 결과 ▶")
        self.resize(self.minimumSizeHint())

    @Slot()
    def _on_bullish_analysis_clicked(self):
        self.analysis_output.clear()
        self.analysis_output.setText("종목 분석 중...")
        if not self.analysis_widget.isVisible():
            self.analysis_widget.show()
            self.resize(self.minimumSizeHint())

        self.worker_thread = QThread()
        self.worker = BullishAnalysisWorker(self.gemini_client, self.code, stock_info_manager.get_name(self.code))
        self.worker.moveToThread(self.worker_thread)
        self.worker.analysis_ready.connect(self._on_analysis_ready)
        self.worker_thread.started.connect(self.worker.run)
        self.worker_thread.start()

    @Slot(str)
    def _on_analysis_ready(self, result: str):
        self.analysis_output.setText(result)
        self._is_analysis_loaded = True
        self._is_analysis_visible = True
        self.analysis_widget.show()
        self.analysis_btn.setText("종목 분석 결과 ▼")
        try:
            self.worker_thread.quit()
            self.worker_thread.wait()
        except Exception:
            pass

    # -----------------------------
    # 오늘자 결과(JSONL) 표 로딩/표시
    # -----------------------------
    def _load_and_show_results(self, *, use_cache_first: bool = True):
        code = self.code
        df: Optional[pd.DataFrame] = None
        if use_cache_first:
            df = self._UI_CACHE_BY_CODE.get(code)

        if df is None:
            rows = load_results_for_date(today_str())
            rows = filter_by_symbol(rows, code)
            df = to_dataframe(rows)
            self._UI_CACHE_BY_CODE[code] = df

        self._populate_results_table(df)

    def _populate_results_table(self, df: pd.DataFrame):
        self.results_table.setRowCount(0)
        if df is None or df.empty:
            return

        cols = ["ts", "side", "price", "reason", "source", "condition_name", "return_msg"]
        for col in cols:
            if col not in df.columns:
                df[col] = None

        for _, row in df.sort_values("ts").iterrows():
            r = self.results_table.rowCount()
            self.results_table.insertRow(r)

            def _s(val) -> str:
                if pd.isna(val) or val is None:
                    return ""
                return str(val)

            ts_val = row["ts"]
            if isinstance(ts_val, pd.Timestamp):
                ts_disp = ts_val.tz_localize("Asia/Seoul") if ts_val.tzinfo is None else ts_val
                ts_text = ts_disp.strftime("%Y-%m-%d %H:%M:%S")
            else:
                ts_text = _s(ts_val)

            cells = [
                ts_text,
                _s(row["side"]),
                _s(row["price"]),
                _s(row["reason"]),
                _s(row["source"]),
                _s(row["condition_name"]),
                _s(row["return_msg"]),
            ]
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                align = Qt.AlignCenter if c in (0, 1, 4, 5) else (Qt.AlignRight | Qt.AlignVCenter)
                it.setTextAlignment(align)
                self.results_table.setItem(r, c, it)

        self.results_table.scrollToBottom()

    @Slot()
    def _on_refresh_results_clicked(self):
        rows = load_results_for_date(today_str())
        rows = filter_by_symbol(rows, self.code)
        df = to_dataframe(rows)
        self._UI_CACHE_BY_CODE[self.code] = df
        self._populate_results_table(df)

    # -----------------------------
    # MACD 버스 이벤트 핸들러
    # -----------------------------
    @Slot(dict)
    def _on_bus(self, payload: dict):
        try:
            if str(payload.get("code", ""))[-6:].zfill(6) != self.code:
                return
            tf = str(payload.get("tf", "")).lower()
            if tf not in {"5m", "30m", "1d"}:
                return
            self._refresh_row(tf)
        except Exception:
            pass

    # -----------------------------
    # 종료 처리
    # -----------------------------
    def closeEvent(self, e):
        try:
            self.bus.macd_series_ready.disconnect(self._on_bus)
        except Exception:
            pass
        super().closeEvent(e)
