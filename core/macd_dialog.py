# ui/macd_dialog.py 
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, List, Optional

import pandas as pd
from PySide6.QtCore import Qt, Signal, Slot, QTimer, QDateTime
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QCheckBox, QAbstractItemView, QFileDialog
)

from core.macd_calculator import macd_bus, calculator

@dataclass
class MacdPoint:
    t: pd.Timestamp
    macd: float
    signal: float
    hist: float

class MacdDialog(QDialog):
    HISTORY_N = 10

    def __init__(self, code: str, bridge=None, parent=None, title: Optional[str] = None):
        super().__init__(parent)
        self.code = str(code)[-6:].zfill(6)
        self.bridge = bridge

        self.buffers: Dict[str, Deque[MacdPoint]] = {
            "5m": deque(maxlen=500),
            "30m": deque(maxlen=500),
            "1d": deque(maxlen=500),
        }

        self.setWindowTitle(title or f"MACD 모니터(수치) - {self.code}")
        self.setModal(True)
        self.setMinimumSize(1180, 520)

        # ========== 상단 바 ==========
        top = QHBoxLayout()
        self.lbl_code = QLabel(f"종목: <b>{self.code}</b>")
        self.lbl_quote = QLabel("")
        self.lbl_updated = QLabel("—")
        self.chk_autorefresh = QCheckBox("자동갱신"); self.chk_autorefresh.setChecked(True)
        self.btn_export = QPushButton("CSV 내보내기")
        self.btn_clear = QPushButton("초기화")

        top.addWidget(self.lbl_code)
        top.addStretch(1)
        top.addWidget(self.lbl_quote)
        top.addSpacing(10)
        top.addWidget(QLabel("업데이트:"))
        top.addWidget(self.lbl_updated)
        top.addSpacing(12)
        top.addWidget(self.chk_autorefresh)
        top.addWidget(self.btn_export)
        top.addWidget(self.btn_clear)

        # ========== 테이블 ==========
        # 컬럼: TF | Time | MACD | Signal | Hist | ΔMACD | ΔHist | Cross | Trend(10) | Hist(10)
        self.COLS = ["TF", "Time", "MACD", "Signal", "Hist", "ΔMACD", "ΔHist", "Cross", "Trend(10)", "Hist(10)"]
        self.table = QTableWidget(3, len(self.COLS), self)
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setShowGrid(True)
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet("""
            QTableWidget {
                background-color: #1f1f1f;
                color: #e0e0e0;
                gridline-color: #333;
                alternate-background-color: #232323;
                selection-background-color: #303030;
            }
            QHeaderView::section {
                background-color: #2a2a2a;
                color: #cfcfcf;
                padding: 6px;
                border: 0px;
                border-right: 1px solid #3a3a3a;
            }
        """)

        # 숫자는 고정폭 폰트
        num_font = QFont("Consolas"); num_font.setStyleHint(QFont.Monospace)
        self.table.setFont(num_font)
        self.table.horizontalHeader().setMinimumSectionSize(80)
        widths = [50, 150, 90, 90, 90, 85, 85, 80, 170, 170]
        for i, w in enumerate(widths):
            self.table.setColumnWidth(i, w)

        for row, tf in enumerate(["5m", "30m", "1d"]):
            self._set_item(row, 0, tf, align=Qt.AlignCenter)

        # ========== 레이아웃 ==========
        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addWidget(self.table)

        # ========== 이벤트 ==========
        self.btn_clear.clicked.connect(self._on_clear_clicked)
        self.btn_export.clicked.connect(self._on_export_clicked)

        try:
            macd_bus.macd_series_ready.connect(self.on_macd_series, Qt.UniqueConnection)
        except Exception:
            pass

        if self.bridge:
            try:
                if hasattr(self.bridge, "minute_bars_received"):
                    self.bridge.minute_bars_received.connect(self.on_minute_bars, Qt.UniqueConnection)
                if hasattr(self.bridge, "daily_bars_received"):
                    self.bridge.daily_bars_received.connect(self.on_daily_bars, Qt.UniqueConnection)
            except Exception:
                pass

        self._refresh_all_rows()

    # ---------- 유틸 ----------
    def _set_item(self, row: int, col: int, text: str,
                  *, color: Optional[str] = None,
                  align: Qt.AlignmentFlag = Qt.AlignRight | Qt.AlignVCenter,
                  tooltip: Optional[str] = None,
                  bold: bool = False,
                  bg: Optional[str] = None):
        item = QTableWidgetItem(text)
        item.setTextAlignment(align)
        if tooltip:
            item.setToolTip(tooltip)
        if bold:
            f = item.font(); f.setBold(True); item.setFont(f)
        # 전경색
        if color == "red":
            item.setForeground(Qt.red)
        elif color == "blue":
            item.setForeground(Qt.blue)
        elif color == "green":
            item.setForeground(Qt.darkGreen)
        # 배경색
        if bg:
            from PySide6.QtGui import QColor
            item.setBackground(QColor(bg))
        self.table.setItem(row, col, item)

    @staticmethod
    def _sparkline(vals: List[float]) -> str:
        # -1~+1 범위 대충 정규화 (값이 큰 경우도 모양만 보자)
        if not vals: return "-"
        blocks = "▁▂▃▄▅▆▇█"
        vmin, vmax = min(vals), max(vals)
        span = (vmax - vmin) or 1.0
        out = []
        for v in vals:
            z = (v - vmin) / span
            idx = min(len(blocks)-1, max(0, int(round(z * (len(blocks)-1)))))
            out.append(blocks[idx])
        return "".join(out)

    def _fmt_signed(self, x: float) -> str:
        if pd.isna(x): return "-"
        return f"{x:+.5f}"

    # ---------- 데이터 수신 ----------
    @Slot(dict)
    def on_macd_series(self, data: dict):
        code = data.get("code")
        if code and code[-6:] != self.code:
            return
        tf = str(data.get("tf", "")).lower()
        if tf not in ("5m", "30m", "1d"):
            return
        series = data.get("series", []) or []
        if not series:
            return

        buf = self.buffers[tf]
        for p in series:
            try:
                t = pd.to_datetime(p.get("t"))
                macd = float(p.get("macd"))
                sig = float(p.get("signal"))
                hist = float(p.get("hist"))
            except Exception:
                continue
            buf.append(MacdPoint(t=t, macd=macd, signal=sig, hist=hist))

        if self.chk_autorefresh.isChecked():
            self._refresh_row(tf)
            self._touch_updated()

    # (옵션) raw rows 경로
    @Slot(str, list)
    def on_minute_bars(self, code: str, rows: List[dict]):
        if code[-6:] != self.code:
            return
        calculator.apply_rows(code=self.code, tf="5m", rows=rows, need=120)

    @Slot(str, list)
    def on_daily_bars(self, code: str, rows: List[dict]):
        if code[-6:] != self.code:
            return
        calculator.apply_rows(code=self.code, tf="1d", rows=rows, need=120)

    # ---------- 표 갱신 ----------
    def _refresh_all_rows(self):
        for tf in ("5m", "30m", "1d"):
            self._refresh_row(tf)

    def _trend_cols(self, row: int, start_col: int, vals: List[float], label: str):
        # 스파크라인 + 색
        spark = self._sparkline(vals)
        tooltip = f"{label} 최근 {len(vals)}개: " + ", ".join(f"{v:.4f}" for v in vals)
        color = "red" if (vals and vals[-1] > 0) else "blue" if (vals and vals[-1] < 0) else None
        self._set_item(row, start_col, spark, color=color, align=Qt.AlignCenter, tooltip=tooltip)

    def _refresh_row(self, tf: str):
        row = 0 if tf == "5m" else 1 if tf == "30m" else 2
        buf = self.buffers[tf]
        self._set_item(row, 0, tf, align=Qt.AlignCenter)

        if not buf:
            for c in range(1, len(self.COLS)):
                self._set_item(row, c, "-", align=Qt.AlignCenter)
            return

        last = buf[-1]
        prev = buf[-2] if len(buf) >= 2 else None

        # Time
        self._set_item(row, 1, last.t.strftime("%Y-%m-%d %H:%M"), align=Qt.AlignCenter)

        # 최신 값
        macd_color = "red" if last.macd > 0 else "blue" if last.macd < 0 else None
        sig_color  = "red" if last.signal > 0 else "blue" if last.signal < 0 else None
        hist_color = "red" if last.hist > 0 else "blue" if last.hist < 0 else None

        self._set_item(row, 2, f"{last.macd:.5f}", color=macd_color, tooltip="MACD")
        self._set_item(row, 3, f"{last.signal:.5f}", color=sig_color, tooltip="Signal")
        self._set_item(row, 4, f"{last.hist:.5f}", color=hist_color, tooltip="Histogram")

        # Δ값
        d_macd = (last.macd - prev.macd) if prev else math.nan
        d_hist = (last.hist - prev.hist) if prev else math.nan
        dmacd_color = "red" if d_macd > 0 else "blue" if d_macd < 0 else None
        dhist_color = "red" if d_hist > 0 else "blue" if d_hist < 0 else None
        self._set_item(row, 5, self._fmt_signed(d_macd), color=dmacd_color, tooltip="현재 - 직전 MACD")
        self._set_item(row, 6, self._fmt_signed(d_hist), color=dhist_color, tooltip="현재 - 직전 Hist")

        # Cross 표기 (MACD vs Signal)
        cross = "-"
        cross_bg = None
        if prev:
            prev_side = (prev.macd - prev.signal)
            now_side  = (last.macd - last.signal)
            if prev_side <= 0 < now_side:
                cross = "↑golden"; cross_bg = "#2c3d15"   # 골든크로스
            elif prev_side >= 0 > now_side:
                cross = "↓dead";   cross_bg = "#3d2c2c"   # 데드크로스
        self._set_item(row, 7, cross, align=Qt.AlignCenter, bold=True, bg=cross_bg)

        # Trend(10), Hist(10)
        macd_vals = [p.macd for p in list(buf)[-self.HISTORY_N:]]
        hist_vals = [p.hist for p in list(buf)[-self.HISTORY_N:]]
        self._trend_cols(row, 8, macd_vals, "MACD")
        self._trend_cols(row, 9, hist_vals, "Hist")

    # ---------- 시세(옵션) ----------
    def update_quote(self, price: str, rate):
        price_str = "" if price is None else str(price)
        if rate is None:
            rate_str = ""
        elif isinstance(rate, (int, float)):
            rate_str = f"{rate:+.2f}%"
        else:
            rate_str = str(rate)

        color = "#bdbdbd"
        if rate_str.startswith("+"):
            color = "#d32f2f"
        elif rate_str.startswith("-"):
            color = "#1976d2"

        self.lbl_quote.setText(f"{price_str} ({rate_str})".strip())
        self.lbl_quote.setStyleSheet(f"font-weight:bold; color:{color};")

    # ---------- 기타 ----------
    def _touch_updated(self):
        self.lbl_updated.setText(QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss"))

    def _on_clear_clicked(self):
        for tf in self.buffers:
            self.buffers[tf].clear()
        self._refresh_all_rows()
        self._touch_updated()

    def _on_export_clicked(self):
        # 현재 3개 TF의 버퍼를 합쳐 CSV 저장
        path, _ = QFileDialog.getSaveFileName(self, "CSV 저장", f"{self.code}_macd.csv", "CSV Files (*.csv)")
        if not path:
            return
        rows = []
        for tf in ("5m", "30m", "1d"):
            for p in self.buffers[tf]:
                rows.append({
                    "code": self.code, "tf": tf,
                    "time": p.t.strftime("%Y-%m-%d %H:%M:%S"),
                    "macd": p.macd, "signal": p.signal, "hist": p.hist
                })
        if not rows:
            return
        pd.DataFrame(rows).to_csv(path, index=False)

    def closeEvent(self, e):
        try:
            macd_bus.macd_series_ready.disconnect(self.on_macd_series)
        except Exception:
            pass
        if self.bridge:
            try:
                if hasattr(self.bridge, "minute_bars_received"):
                    self.bridge.minute_bars_received.disconnect(self.on_minute_bars)
            except Exception:
                pass
            try:
                if hasattr(self.bridge, "daily_bars_received"):
                    self.bridge.daily_bars_received.disconnect(self.on_daily_bars)
            except Exception:
                pass
        super().closeEvent(e)
