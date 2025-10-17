# core/macd_dialog.py 

from __future__ import annotations

import logging
from typing import Dict, Any

import pandas as pd
# Matplotlib / PySide6 연동을 위한 import
from PySide6.QtCore import Qt, Slot, QObject, Signal, QThread
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSplitter, QWidget, QPushButton, QTextEdit, QHBoxLayout
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.dates import DateFormatter
from matplotlib.figure import Figure

# 기존 유틸리티 import
from core.macd_calculator import macd_bus, get_points
from utils.stock_info_manager import stock_info_manager
from utils.gemini_client import GeminiClient
from utils.results_store import load_orders_jsonl, results_path_for, today_str

logger = logging.getLogger(__name__)


# ==============================
# Gemini 분석 워커 (기존과 동일)
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
            logger.error("Gemini API call failed: %s", e)
            self.analysis_ready.emit("⚠️ AI 브리핑 실패: 모델 접근 불가 또는 네트워크 문제로 보입니다.")


# ==============================
# MACD 상세 다이얼로그
# ==============================
class MacdDialog(QDialog):
    COLS = ["TF", "Time", "MACD", "Hist", "ΔMACD", "ΔHist", "Cross"]
    _ANALYSIS_TEXT_CACHE: Dict[str, str] = {}
    _UI_CACHE_BY_CODE: Dict[str, pd.DataFrame] = {}

    def __init__(self, code: str, *, parent=None, bus=macd_bus):
        super().__init__(parent)
        self.code = str(code)[-6:].zfill(6)
        self.bus = bus
        stock_name = stock_info_manager.get_name(self.code)

        self.setWindowFlags(self.windowFlags() | Qt.Window | Qt.WindowMinMaxButtonsHint)
        self.setWindowTitle(f"MACD 상세 - {stock_name} ({self.code})")
        self.setModal(False)
        self.resize(1280, 900)
        self.setMinimumSize(1100, 760)

        # --- Gemini 분석기 준비 ---
        self.gemini_client = GeminiClient(prompt_file="resources/bullish_analysis_prompt.md")

        # --- UI 위젯 생성 ---
        self.lbl = QLabel(f"종목: <b>{stock_name} ({self.code})</b>")
        self.lbl.setTextFormat(Qt.RichText)
        self.bullish_analysis_btn = QPushButton("✨ AI 종목 분석")
        self.analysis_btn = QPushButton("분석 결과 보기 ▶")
        self.chart_toggle_btn = QPushButton("차트 숨기기 ▲")
        self.refresh_results_btn = QPushButton("🔄 당일 결과 새로고침")

        # --- 차트 위젯 (Matplotlib) ---
        self.fig_5m, self.canvas_5m, self.ax_5m = self._create_chart_canvas()
        self.fig_30m, self.canvas_30m, self.ax_30m = self._create_chart_canvas()

        # --- 테이블 위젯 ---
        self.table = self._create_macd_table()
        self.results_table = self._create_results_table()

        # --- 분석 결과 위젯 (초기 숨김) ---
        self.analysis_widget, self.analysis_output = self._create_analysis_panel()
        cached_text = MacdDialog._ANALYSIS_TEXT_CACHE.get(self.code)
        if cached_text:
            self.analysis_output.setText(cached_text)

        # --- 레이아웃 구성 ---
        self._setup_layout()

        # --- 시그널 연결 ---
        self.bullish_analysis_btn.clicked.connect(self._on_bullish_analysis_clicked)
        self.analysis_btn.clicked.connect(self._on_analysis_toggle)
        self.chart_toggle_btn.clicked.connect(self._on_chart_toggle)
        self.refresh_results_btn.clicked.connect(self._on_refresh_results_clicked)

        try:
            # ✅ 데이터 버스를 on_series_data 슬롯에 직접 연결
            self.bus.macd_series_ready.connect(self.on_series_data, Qt.UniqueConnection)
        except (TypeError, RuntimeError):
            pass # 이미 연결되었거나 다른 문제 발생 시 무시

        # --- 초기 데이터 로드 ---
        self._refresh_all_data() # ✅ 테이블과 차트를 모두 초기화하는 함수 호출
        self._load_and_show_results(use_cache_first=True)


    # -----------------------------
    # UI 구성 헬퍼 함수
    # -----------------------------
    def _create_chart_canvas(self):
        """Matplotlib 차트와 캔버스를 생성하고 스타일을 지정합니다."""
        fig = Figure(tight_layout=True, facecolor="#1e2126")
        canvas = FigureCanvas(fig)
        ax = fig.add_subplot(111, facecolor="#23272e")
        ax.tick_params(colors="#e9edf1")
        ax.title.set_color("#e9edf1")
        ax.xaxis.label.set_color("#cfd6df")
        ax.yaxis.label.set_color("#cfd6df")
        for s in ax.spines.values(): s.set_color("#555")
        return fig, canvas, ax

    def _create_macd_table(self):
        """MACD 데이터 표시용 테이블을 생성합니다."""
        table = QTableWidget(3, len(self.COLS))
        table.setHorizontalHeaderLabels(self.COLS)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setAlternatingRowColors(True)
        self._set_item(table, 0, 0, "5m", center=True)
        self._set_item(table, 1, 0, "30m", center=True)
        self._set_item(table, 2, 0, "1d", center=True)
        return table
        
    def _create_results_table(self):
        """당일 트레이딩 결과 표시용 테이블을 생성합니다."""
        table = QTableWidget(0, 7)
        table.setHorizontalHeaderLabels(["Time", "Action", "Code", "Name", "Qty", "ReturnMsg", "Strategy"])
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setAlternatingRowColors(True)
        return table

    def _create_analysis_panel(self):
        """AI 분석 결과 표시용 패널을 생성합니다."""
        widget = QWidget()
        widget.setMinimumHeight(200)
        widget.hide()
        layout = QVBoxLayout(widget)
        layout.addWidget(QLabel("<b>✨ AI 종목 분석 결과</b>"))
        output = QTextEdit()
        output.setReadOnly(True)
        layout.addWidget(output)
        return widget, output

    def _setup_layout(self):
        """메인 UI 레이아웃을 구성합니다."""
        main_layout = QVBoxLayout(self)

        header_row = QHBoxLayout()
        header_row.addWidget(self.lbl)
        header_row.addStretch(1)
        header_row.addWidget(self.chart_toggle_btn)
        header_row.addWidget(self.bullish_analysis_btn)
        header_row.addWidget(self.analysis_btn)
        main_layout.addLayout(header_row)

        main_splitter = QSplitter(Qt.Vertical)

        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(0,0,0,0)
        top_layout.addWidget(self.table)

        self.chart_splitter = QSplitter(Qt.Horizontal)
        self.chart_splitter.addWidget(self.canvas_5m)
        self.chart_splitter.addWidget(self.canvas_30m)
        top_layout.addWidget(self.chart_splitter, 1)

        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        btn_row = QHBoxLayout()
        btn_row.addWidget(QLabel("<b>당일 트레이딩 기록</b>"))
        btn_row.addStretch(1)
        btn_row.addWidget(self.refresh_results_btn)
        bottom_layout.addLayout(btn_row)
        bottom_layout.addWidget(self.results_table)

        main_splitter.addWidget(top_widget)
        main_splitter.addWidget(bottom_widget)
        main_splitter.setSizes([500, 250])

        main_layout.addWidget(main_splitter, 1)
        main_layout.addWidget(self.analysis_widget)

    # -----------------------------
    # 데이터 처리 및 차트 업데이트
    # -----------------------------
    @Slot(dict)
    def on_series_data(self, data: dict):
        """[핵심 슬롯] 실시간 데이터를 받아 해당 차트와 테이블을 업데이트합니다."""
        # 디버깅을 위해 들어온 데이터 출력
        print(f"[DEBUG] on_series_data received: {data.get('code')} / {data.get('interval')}")

        if data.get("code") != self.code:
            return
        
        interval = data.get("interval")
        series = data.get("series", [])
        if not series or interval not in ("5m", "30m"):
            return

        # 1. 테이블 업데이트 (최신 데이터 기반)
        self._update_table_from_series(interval, series)

        # 2. 차트 업데이트
        df = pd.DataFrame(series)
        if df.empty or 'ts' not in df.columns:
            return
            
        df['ts'] = pd.to_datetime(df['ts'])
        df = df.tail(20)

        if interval == "5m":
            self.update_plot(self.ax_5m, self.canvas_5m, df['ts'], df['macd'], df['signal'], df['hist'], "MACD (5분봉)")
        elif interval == "30m":
            self.update_plot(self.ax_30m, self.canvas_30m, df['ts'], df['macd'], df['signal'], df['hist'], "MACD (30분봉)")

    def update_plot(self, ax, canvas, dates, macd, signal, hist, title):
            """지정된 축(ax)에 MACD 차트를 그리는 공통 함수 (데이터 인덱스 기반으로 변경)"""
            ax.clear()
            if not dates.empty:
                # ▼▼▼ 1. X축을 위한 숫자 인덱스 생성 ▼▼▼
                x_indices = range(len(dates))

                # ▼▼▼ 2. 시간(dates) 대신 인덱스(x_indices)를 사용해 플로팅 ▼▼▼
                ax.plot(x_indices, macd, label='MACD', color='#60a5fa', linewidth=1.5)
                ax.plot(x_indices, signal, label='Signal', color='#f59e0b', linewidth=1.2, linestyle='--')
                
                bar_colors = ['#22c55e' if h >= 0 else '#ef4444' for h in hist]
                # ▼▼▼ 3. 막대 너비를 고정 값으로 변경하여 일관성 유지 ▼▼▼
                ax.bar(x_indices, hist, label='Hist', color=bar_colors, width=0.8, alpha=0.7)
                
                # ▼▼▼ 4. X축 눈금(ticks) 위치와 라벨을 직접 설정 ▼▼▼
                max_ticks = 6
                if len(dates) > max_ticks:
                    # 눈금을 표시할 인덱스 위치 계산
                    step = len(dates) // max_ticks
                    tick_positions = x_indices[::step]
                    # 해당 위치의 시간 텍스트 라벨 생성
                    tick_labels = [d.strftime('%H:%M') for d in dates.iloc[tick_positions]]
                else:
                    # 데이터가 적으면 모든 데이터의 라벨 표시
                    tick_positions = x_indices
                    tick_labels = [d.strftime('%H:%M') for d in dates]

                ax.set_xticks(tick_positions)
                ax.set_xticklabels(tick_labels, rotation=30, ha='right', fontsize=8) # ha='right'로 정렬 개선

                ax.legend(labelcolor="#e9edf1", facecolor="#2a2f36", edgecolor="#3a414b")
            
            ax.set_title(title, color='#e9edf1')
            ax.grid(True, linestyle='--', alpha=0.25, color="#555")
            canvas.draw()
        
        
    # -----------------------------
    # 테이블 업데이트
    # -----------------------------
    def _refresh_all_data(self):
        """(최초 실행용) 모든 시간대의 테이블과 차트를 캐시 데이터로 초기화합니다."""
        for tf in ("5m", "30m", "1d"):
            self._update_ui_from_cache(tf)
            
    def _update_ui_from_cache(self, tf:str):
        """캐시(get_points)에서 데이터를 가져와 테이블과 차트를 업데이트합니다."""
        pts = get_points(self.code, tf, n=20) or []
        self._update_table_from_series(tf, pts)
        
        if not pts: return
        df = pd.DataFrame(pts)
        df['ts'] = pd.to_datetime(df['ts'])
        
        if tf == "5m":
            self.update_plot(self.ax_5m, self.canvas_5m, df['ts'], df['macd'], df['signal'], df['hist'], "MACD (5분봉)")
        elif tf == "30m":
            self.update_plot(self.ax_30m, self.canvas_30m, df['ts'], df['macd'], df['signal'], df['hist'], "MACD (30분봉)")

    def _update_table_from_series(self, tf: str, series: list):
        """주어진 데이터 시리즈로 MACD 테이블의 한 행을 업데이트합니다."""
        row_map = {"5m": 0, "30m": 1, "1d": 2}
        row = row_map.get(tf)
        if row is None or not series:
            return

        last = series[-1]
        prev = series[-2] if len(series) > 1 else None
        
        ts = pd.to_datetime(last["ts"]).tz_convert("Asia/Seoul")
        self._set_item(self.table, row, 1, ts.strftime("%H:%M:%S" if tf != "1d" else "%m-%d"), center=True)
        
        macd = float(last.get("macd", float("nan")))
        hist = float(last.get("hist", float("nan")))
        self._set_item(self.table, row, 2, self._fmt(macd))
        self._set_item(self.table, row, 3, self._fmt(hist))
        
        if prev:
            prev_macd = float(prev.get("macd", float("nan")))
            prev_hist = float(prev.get("hist", float("nan")))
            self._set_item(self.table, row, 4, self._fmt_delta(macd, prev_macd))
            self._set_item(self.table, row, 5, self._fmt_delta(hist, prev_hist))
            
            cross = "—"
            if pd.notna(hist) and pd.notna(prev_hist):
                if prev_hist <= 0 and hist > 0: cross = "G-Cross"
                elif prev_hist >= 0 and hist < 0: cross = "D-Cross"
            self._set_item(self.table, row, 6, cross, center=True)

    # -----------------------------
    # 이벤트 핸들러 (버튼 등)
    # -----------------------------
    @Slot()
    def _on_chart_toggle(self):
        is_visible = self.chart_splitter.isVisible()
        self.chart_splitter.setVisible(not is_visible)
        self.chart_toggle_btn.setText("차트 보이기 ▼" if is_visible else "차트 숨기기 ▲")

    @Slot()
    def _on_analysis_toggle(self):
        is_visible = self.analysis_widget.isVisible()
        self.analysis_widget.setVisible(not is_visible)
        self.analysis_btn.setText("분석 결과 숨기기 ▲" if not is_visible else "분석 결과 보기 ▶")

    @Slot()
    def _on_bullish_analysis_clicked(self):
        self.analysis_output.setText("✨ AI가 종목을 분석하고 있습니다. 잠시만 기다려주세요...")
        self.analysis_widget.show()
        self.analysis_btn.setText("분석 결과 숨기기 ▲")
        
        self.worker_thread = QThread()
        self.worker = BullishAnalysisWorker(self.gemini_client, self.code, stock_info_manager.get_name(self.code))
        self.worker.moveToThread(self.worker_thread)
        self.worker.analysis_ready.connect(self._on_analysis_ready)
        self.worker_thread.started.connect(self.worker.run)
        self.worker_thread.start()

    @Slot(str)
    def _on_analysis_ready(self, result: str):
        self.analysis_output.setText(result)
        MacdDialog._ANALYSIS_TEXT_CACHE[self.code] = result
        try:
            self.worker_thread.quit()
            self.worker_thread.wait()
        except RuntimeError: pass

    @Slot()
    def _on_refresh_results_clicked(self):
        self._load_and_show_results(use_cache_first=False)

    # -----------------------------
    # 결과 테이블 로딩 및 표시
    # -----------------------------
    def _load_and_show_results(self, *, use_cache_first: bool = True):
        df = self._UI_CACHE_BY_CODE.get(f"orders:{self.code}") if use_cache_first else None
        if df is None:
            orders_path = results_path_for(today_str())
            df_all = load_orders_jsonl(orders_path)
            df = df_all[df_all["code"] == self.code].copy() if not df_all.empty else pd.DataFrame()
            self._UI_CACHE_BY_CODE[f"orders:{self.code}"] = df
        self._populate_results_table(df)

    def _populate_results_table(self, df: pd.DataFrame):
        self.results_table.setRowCount(0)
        if df is None or df.empty: return

        df = df.sort_values("ts_kst", kind="mergesort") if "ts_kst" in df.columns else df
        
        for _, row in df.iterrows():
            r = self.results_table.rowCount()
            self.results_table.insertRow(r)
            ts_text = row["ts_kst"].strftime("%H:%M:%S") if pd.notna(row.get("ts_kst")) else ""
            code_val = row.get("code", "")
            cells = [
                ts_text,
                row.get("action", ""),
                code_val,
                stock_info_manager.get_name(code_val),
                str(row.get("qty", "")),
                row.get("resp_msg", ""),
                row.get("strategy", "")
            ]
            for c, text in enumerate(cells):
                self._set_item(self.results_table, r, c, str(text), center=True)
        self.results_table.scrollToBottom()

    # -----------------------------
    # 포맷팅 및 종료 처리
    # -----------------------------
    def _set_item(self, table, row, col, text, *, center=False):
        it = QTableWidgetItem(text)
        it.setTextAlignment(Qt.AlignCenter if center else (Qt.AlignRight | Qt.AlignVCenter))
        table.setItem(row, col, it)

    def _fmt(self, v):
        return f"{float(v):.2f}" if pd.notna(v) else "—"
    
    def _fmt_delta(self, cur, prv):
        return f"{(float(cur) - float(prv)):+.2f}" if pd.notna(cur) and pd.notna(prv) else "—"

    def closeEvent(self, e):
        MacdDialog._ANALYSIS_TEXT_CACHE[self.code] = self.analysis_output.toPlainText()
        try:
            self.bus.macd_series_ready.disconnect(self.on_series_data)
        except (TypeError, RuntimeError): pass
        super().closeEvent(e)