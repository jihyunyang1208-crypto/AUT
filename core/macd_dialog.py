# core/macd_dialog.py
from __future__ import annotations

from typing import Optional, List
import pandas as pd
import pyqtgraph as pg

from PySide6.QtCore import Qt, Slot, QObject, Signal , QThread
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSplitter, QWidget, QHBoxLayout, QPushButton, QTextEdit
)
import logging

# 계산기/버스/조회 API
from core.macd_calculator import macd_bus, get_points
from utils.stock_info_manager import stock_info_manager
from utils.gemini_client import GeminiClient



logger = logging.getLogger(__name__)

# 🔹 Gemini API 호출을 처리하는 전용 작업자(Worker) 클래스
class BullishAnalysisWorker(QObject):
    analysis_ready = Signal(str)

    def __init__(self, gemini_client: GeminiClient, code: str, stock_name: str, parent=None):
        super().__init__(parent)
        self._client = gemini_client
        self._code = code
        self._name = stock_name

    @Slot()
    def run(self):
        # 🔹 GeminiClient의 run_briefing 메서드 호출
        try:
            extra_context = f"종목 코드: {self._code}\n종목명: {self._name}"
            response_text = self._client.run_briefing(extra_context=extra_context)
            self.analysis_ready.emit(response_text)
        except Exception as e:
            logger.error("Gemini API call failed (model=%s): %s", self._client.model_name, e)
            # 사용자에게 보일 UI 메시지도 개선
            self._show_error_toast(
                f"AI 브리핑 실패: 모델 접근 불가 또는 퇴역. 설정에서 모델을 '{self._client.model_name}' 로 전환했습니다."
            )


class MacdDialog(QDialog):
    # Signal 컬럼 제거
    COLS = ["TF", "Time", "MACD", "Hist", "ΔMACD", "ΔHist", "Cross"]
    _ROW_OF = {"5m": 0, "30m": 1}

    def __init__(self, code: str, *, parent=None, bus=macd_bus):
        super().__init__(parent)
        self.code = str(code)[-6:].zfill(6)
        self.bus = bus
        stock_name = stock_info_manager.get_name(self.code)

        self.setWindowFlags(
            self.windowFlags() | Qt.Window | Qt.WindowMinMaxButtonsHint
        )

        self.setWindowTitle(f"MACD 상세 - {self.code}")
        self.setModal(False)
        self.setMinimumSize(980, 700)

        # UI 요소 초기화
        self.lbl = QLabel(f"종목: <b>{stock_name} ({self.code})</b>")
        self.lbl.setTextFormat(Qt.RichText)

        self._is_analysis_loaded = False
        self._is_analysis_visible = False

        self.gemini_client = GeminiClient(prompt_file="resources/bullish_analysis_prompt.md")
        self.bullish_analysis_btn = QPushButton("종목 분석")
        self.bullish_analysis_btn.clicked.connect(self._on_bullish_analysis_clicked)

        self.analysis_btn = QPushButton("종목 분석 결과 ▶")
        self.analysis_btn.clicked.connect(self._on_analysis_toggle)


        # 🔹 단 하나의 메인 레이아웃만 생성
        main_layout = QVBoxLayout(self)

        # 테이블 위젯
        self.table = QTableWidget(3, len(self.COLS), self)
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setAlternatingRowColors(True)

        # TF 라벨 고정
        self._set_item(0, 0, "5m", center=True)
        self._set_item(1, 0, "30m", center=True)
        
        # 막대그래프 위젯 (PyQtGraph)
        self.graph_widget = pg.PlotWidget(title="MACD & Histogram (10 Recent)")
        self.graph_widget.setBackground('w')
        self.graph_widget.getAxis('bottom').setPen('k')
        self.graph_widget.getAxis('left').setTicks([]) 
        self.graph_widget.getAxis('left').setPen(None)
        self.graph_widget.plotItem.getAxis('bottom').setTicks([]) 
        self.graph_widget.setBackground('k') 

        # 하단 위젯: Gemini 분석 결과 위젯  (초기에는 숨겨둡니다)
        self.analysis_widget = QWidget()
        self.analysis_widget.setMinimumHeight(200)
        self.analysis_widget.hide()
        analysis_layout = QVBoxLayout(self.analysis_widget)
        analysis_layout.addWidget(QLabel("<b>종목 분석 결과</b>"))
        self.analysis_output = QTextEdit()
        self.analysis_output.setReadOnly(True)
        analysis_layout.addWidget(self.analysis_output)

        # 🔹 모든 위젯을 메인 레이아웃에 추가
        main_layout.addWidget(self.lbl)
        main_layout.addWidget(self.bullish_analysis_btn)
        
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.graph_widget)
        splitter.addWidget(self.bullish_analysis_btn)  
        splitter.addWidget(self.analysis_btn)  

        splitter.addWidget(self.analysis_widget)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        main_layout.addWidget(splitter)
        
        # 초기 표시 (캐시에 이미 있으면 나옴)
        self._refresh_all()

        # 버스 구독 (중복 연결 방지)
        try:
            self.bus.macd_series_ready.connect(self._on_bus, Qt.UniqueConnection)
        except TypeError:
            pass

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
        if s in {"5", "5m", "5min", "m5"}: return 0
        if s in {"30", "30m", "30min", "m30"}: return 1
        # if s in {"1d", "d", "day"}: return 2
        return self._ROW_OF.get(s)

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

        # 보장된 시간 오름차순
        last = pts[-1]
        prev = pts[-2] if len(pts) > 1 else None

        ts = last["ts"] if isinstance(last["ts"], pd.Timestamp) else pd.Timestamp(last["ts"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("Asia/Seoul")
        self._set_item(row, 1, ts.strftime("%Y-%m-%d %H:%M"), center=True)

        macd = float(last.get("macd", float("nan")))
        # sig  = float(last.get("signal", float("nan"))) # Signal 값 제거
        hist = float(last.get("hist", float("nan")))

        # MACD, Hist 컬럼 위치 조정
        self._set_item(row, 2, self._fmt(macd))
        self._set_item(row, 3, self._fmt(hist))

        def _delta(cur, prv):
            if cur is None or prv is None: return "—"
            if any(pd.isna(x) for x in (cur, prv)): return "—"
            return f"{(float(cur) - float(prv)):+.5f}"

        prev_macd = float(prev["macd"]) if prev else None
        prev_hist = float(prev["hist"]) if prev else None

        # ΔMACD, ΔHist 컬럼 위치 조정
        self._set_item(row, 4, _delta(macd, prev_macd))
        self._set_item(row, 5, _delta(hist, prev_hist))

        cross = "—"
        # Signal 값 제거로 인해 크로스 로직 수정
        # 현재는 MACD와 Signal 비교가 불가능하므로, 히스토그램 값의 0 교차로 변경
        if prev is not None and not any(pd.isna(x) for x in (hist, prev["hist"])):
             was_above = float(prev["hist"]) > 0
             now_above = hist > 0
             if (not was_above) and now_above: cross = "G-Cross"
             elif was_above and (not now_above): cross = "D-Cross"
        self._set_item(row, 6, cross, center=True)
        
        # 막대그래프 업데이트 로직 추가
        if tf == "5m":
            self._update_graph(pts)

    def _update_graph(self, pts: List[Dict[str, Any]]):
        """MACD 히스토그램과 MACD 라인을 100으로 나눈 값으로 함께 표시합니다."""
        if len(pts) < 5:
            return

        self.graph_widget.clear()
        # 0선 추가
        zero_line = pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen('k', width=1, style=Qt.DashLine))
        self.graph_widget.addItem(zero_line)

        # 최신 5개 데이터 추출
        recent_data = pts[-10:]
        x_vals = list(range(len(recent_data)))
        
        # MACD와 히스토그램 값을 각각 100으로 나눔
        hist_values = [p.get("hist", float("nan")) / 10 for p in recent_data]
        macd_values = [p.get("macd", float("nan")) / 100 for p in recent_data]
        
        # 1. 히스토그램 바 그래프
        # 막대 색상 설정 (양수: 초록, 음수: 빨강)
        colors = ['#FFB6C1' if h >= 0 else '#87CEEB' for h in hist_values]
        
        # 바 아이템 생성 및 색상 설정
        bar_graph = pg.BarGraphItem(x=x_vals, height=hist_values, width=0.6, brushes=colors)
        self.graph_widget.addItem(bar_graph)

        # 2. MACD 라인 그래프
        # MACD 라인 색상 (파란색)
        line_graph = self.graph_widget.plot(x_vals, macd_values, pen=pg.mkPen('b', width=2))
        
        # 3. Y축 범위 자동 조절
        # 값이 100으로 나누어졌으므로, Y축 범위가 더 작게 자동 조정됩니다.
        self.graph_widget.autoRange()


    @Slot()
    def _on_analysis_toggle(self):
        # 보이면 숨기고, 숨겨져 있으면 보이게 함
        self._is_analysis_visible = not self._is_analysis_visible
        self.analysis_widget.setVisible(self._is_analysis_visible)
        
        # 버튼 텍스트 변경
        if self._is_analysis_visible:
            self.analysis_btn.setText("종목 분석 결과 ▼")
        else:
            self.analysis_btn.setText("종목 분석 결과 ▶")
            
        # 다이얼로그 크기 조정
        self.resize(self.minimumSizeHint())
        
        
    @Slot(str)
    def _on_analysis_ready(self, result: str):
        self.analysis_output.setText(result)
        self.analysis_btn.setEnabled(True) # 버튼 다시 활성화
        self._is_analysis_loaded = True
        self._is_analysis_visible = True
        self.analysis_widget.show()
        self.analysis_btn.setText("분석 숨기기 ▼")
        self.resize(self.minimumSizeHint())
        
        self.worker_thread.quit()
        self.worker_thread.wait()


    @Slot()
    def _on_bullish_analysis_clicked(self):
        self.analysis_output.clear()
        self.analysis_output.setText("종목 분석 중...")
        
        # 🔹 분석 결과 창을 보이도록 설정
        if not self.analysis_widget.isVisible():
            self.analysis_widget.show()
            self.resize(self.minimumSizeHint()) # 🔹 위젯 크기에 맞게 다이얼로그 확장
        
        self.worker_thread = QThread()
        self.worker = BullishAnalysisWorker(self.gemini_client, self.code, stock_info_manager.get_name(self.code))
        self.worker.moveToThread(self.worker_thread)
        self.worker.analysis_ready.connect(self._on_analysis_ready)
        self.worker_thread.started.connect(self.worker.run)
        self.worker_thread.start()
        
    @Slot(str)
    def _on_analysis_ready(self, result: str):
        self.analysis_output.setText(result)
        self.worker_thread.quit()
        self.worker_thread.wait()

        
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

    def closeEvent(self, e):
        try:
            self.bus.macd_series_ready.disconnect(self._on_bus)
        except Exception:
            pass
        super().closeEvent(e)