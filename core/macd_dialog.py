from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, List, Optional

import pandas as pd
from PySide6.QtCore import Qt, Signal, Slot, QDateTime
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QCheckBox, QAbstractItemView, QFileDialog
)
from PySide6.QtGui import QColor, QFont

from core.macd_calculator import macd_bus, calculator


# ---- (옵션) raw rows를 받아 MACD 내부계산에 사용하고 싶을 때 ----
def _parse_dt(date_str: Optional[str], time_str: Optional[str]) -> Optional[pd.Timestamp]:
    if not date_str:
        return None
    ds = str(date_str)
    if len(ds) == 8 and ds.isdigit():
        if time_str and len(str(time_str)) == 6 and str(time_str).isdigit():
            return pd.to_datetime(ds + str(time_str), format="%Y%m%d%H%M%S", errors="coerce")
        return pd.to_datetime(ds, format="%Y%m%d", errors="coerce")
    return pd.to_datetime(ds, errors="coerce")

def _to_float(x) -> float:
    if x is None or x == "":
        return math.nan
    s = str(x).replace(",", "")
    neg = s.startswith("-")
    s = s.lstrip("+-")
    try:
        v = float(s)
    except Exception:
        return math.nan
    return -v if neg else v

def rows_to_df_minutes(rows: Iterable[dict]) -> pd.DataFrame:
    recs = []
    for r in rows or []:
        d = r.get("base_dt") or r.get("trd_dd") or r.get("dt") or r.get("date")
        t = r.get("trd_tm") or r.get("time") or r.get("tm") or r.get("cntr_tm")
        ts = _parse_dt(d, t)
        if ts is None or pd.isna(ts):
            continue
        recs.append({"dt": ts, "close": _to_float(r.get("close_pric") or r.get("close") or r.get("c"))})
    if not recs:
        return pd.DataFrame(columns=["close"])
    return pd.DataFrame(recs).set_index("dt").sort_index()

def rows_to_df_daily(rows: Iterable[dict]) -> pd.DataFrame:
    recs = []
    for r in rows or []:
        d = r.get("base_dt") or r.get("trd_dd") or r.get("dt") or r.get("date")
        ts = _parse_dt(d, None)
        if ts is None or pd.isna(ts):
            continue
        recs.append({"dt": ts, "close": _to_float(r.get("close_pric") or r.get("close"))})
    if not recs:
        return pd.DataFrame(columns=["close"])
    return pd.DataFrame(recs).set_index("dt").sort_index()


# ---------------- 수치 모니터링 ----------------

@dataclass
class MacdPoint:
    t: pd.Timestamp
    macd: float
    signal: float
    hist: float


class MacdDialog(QDialog):
    """
    - 5m/30m/1d 행 3줄, 각 행에:
      최신 시각, 최신 MACD/Signal/Hist, 최근 10개 MACD/HIST(+방향)
    - macd_bus에서 mode에 따라 full/append 처리
    """
    HISTORY_N = 10
    COLOR_PINK = "#F8BBD0"
    COLOR_BLUE = "#81D4FA"
    COLOR_WHITE = "#E0E0E0"

    def __init__(self, code: str, bridge=None, parent=None, title: Optional[str] = None):
        super().__init__(parent)
        self.code = str(code)[-6:].zfill(6)
        self.bridge = bridge

        # TF별 버퍼
        self.buffers: Dict[str, Deque[MacdPoint]] = {
            "5m": deque(maxlen=500),
            "30m": deque(maxlen=500),
            "1d": deque(maxlen=500),
        }

        self.setWindowTitle(title or f"MACD 모니터(수치) - {self.code}")
        self.setModal(True)
        self.setMinimumSize(1200, 460)

        # 상단 바
        top = QHBoxLayout()
        self.lbl_code = QLabel(f"종목: <b>{self.code}</b>")
        self.lbl_quote = QLabel("")
        self.lbl_updated = QLabel("—")
        self.btn_export = QPushButton("CSV 내보내기")

        self.btn_clear = QPushButton("초기화")
        self.chk_autorefresh = QCheckBox("자동갱신")
        self.chk_autorefresh.setChecked(True)
        top.addWidget(self.lbl_code)
        top.addStretch(1)
        top.addWidget(self.lbl_quote)
        top.addSpacing(8)
        top.addWidget(self.chk_autorefresh)
        top.addWidget(self.btn_clear)

        # 테이블
        # 컬럼: TF | Time | MACD | Signal | Hist | ΔMACD | ΔHist | Cross | Trend(10) | Hist(10)
        self.COLS = ["TF", "Time", "MACD", "Signal", "Hist", "ΔMACD", "ΔHist", "Cross", "Trend(10)", "Hist(10)"]

        self.table = QTableWidget(3, len(self.COLS), self)
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
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

        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addWidget(self.table)

        # 이벤트
        self.btn_clear.clicked.connect(self._on_clear_clicked)
        self.btn_export.clicked.connect(self._on_export_clicked)

        # MACD 버스 연결
        try:
            macd_bus.macd_series_ready.connect(self.on_macd_series, Qt.UniqueConnection)
        except Exception:
            pass

        # (옵션) 브릿지가 raw rows를 주면 → 계산기로 전달해 버스 emit
        if self.bridge:
            try:
                if hasattr(self.bridge, "minute_bars_received"):
                    self.bridge.minute_bars_received.connect(self.on_minute_bars, Qt.UniqueConnection)
                if hasattr(self.bridge, "daily_bars_received"):
                    self.bridge.daily_bars_received.connect(self.on_daily_bars, Qt.UniqueConnection)
            except Exception:
                pass

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
            item.setForeground(QColor(self.COLOR_PINK))
        elif color == "blue":
            item.setForeground(QColor(self.COLOR_BLUE))
        elif color == "green":
            item.setForeground(QColor("#C8E6C9")) # 연한 녹색
        # 배경색
        if bg:
            item.setBackground(QColor(bg))
        self.table.setItem(row, col, item)

    @staticmethod
    def _sparkline(vals: List[float]) -> str:
        # -1~+1 범위 대충 정규화 (값이 큰 경우도 모양만 보자)
        if not vals: 
            return "-"
        blocks = "▁▂▃▄▅▆▇█"
        vmin, vmax = float(min(vals)), float(max(vals))
        span = (vmax - vmin) or 1.0
        out = []
        for v in vals:
            z = (v - vmin) / span
            idx = min(len(blocks)-1, max(0, int(round(z * (len(blocks)-1)))))
            out.append(blocks[idx])
        return "".join(out)

    def _fmt_val_with_dir(self, prev: Optional[float], cur: float) -> (str, Optional[str]):
        arrow = "→"
        if prev is None or pd.isna(prev) or pd.isna(cur):
            arrow = " "
        else:
            if cur > prev:
                arrow = "↑"
            elif cur < prev:
                arrow = "↓"
            else:
                arrow = "→"
        color = "red" if cur > 0 else "blue" if cur < 0 else None
        return f"{cur:.5f}{arrow}", color

    def _row_index(self, tf: str) -> int:
        return 0 if tf == "5m" else 1 if tf == "30m" else 2

    # ------------ 버스 수신 ------------
    @Slot(dict)
    def on_macd_series(self, data: dict):
        code = data.get("code")
        if code and code[-6:] != self.code:
            return
        tf = str(data.get("tf", "")).lower()
        if tf not in ("5m", "30m", "1d"):
            return

        mode = str(data.get("mode") or "full").lower()
        series = data.get("series") or []
        if not series:
            return

        buf = self.buffers[tf]

        if mode == "full":
            buf.clear()
            for p in series:
                t = pd.to_datetime(p.get("t"), errors="coerce")
                if pd.isna(t):
                    continue
                buf.append(MacdPoint(
                    t=t,
                    macd=float(p.get("macd")),
                    signal=float(p.get("signal")),
                    hist=float(p.get("hist")),
                ))
        else:  # append
            # 마지막 1개만 온다는 가정. 안전하게 여러 개도 처리
            last_ts = buf[-1].t if buf else None
            for p in series:
                t = pd.to_datetime(p.get("t"), errors="coerce")
                if pd.isna(t):
                    continue
                if last_ts is not None and not (t > last_ts):
                    # 과거/중복은 무시
                    continue
                buf.append(MacdPoint(
                    t=t,
                    macd=float(p.get("macd")),
                    signal=float(p.get("signal")),
                    hist=float(p.get("hist")),
                ))

        if self.chk_autorefresh.isChecked():
            self._refresh_row(tf)

    # ------------ raw rows 경로(옵션) ------------
    @Slot(str, list)
    def on_minute_bars(self, code: str, rows: List[dict]):
        if code[-6:] != self.code:
            return
        # 최초엔 full, 이후엔 append를 호출하는 것은 호출부(스케줄러/브릿지)에서 결정하는 게 깔끔
        # 여기서는 예시로 full만 수행(초기 로딩용)
        calculator.apply_rows_full(code=self.code, tf="5m", rows=rows, need=120)

    @Slot(str, list)
    def on_daily_bars(self, code: str, rows: List[dict]):
        if code[-6:] != self.code:
            return
        calculator.apply_rows_full(code=self.code, tf="1d", rows=rows, need=120)

    # ------------ 표 갱신 ------------

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
        row = self._row_index(tf)
        buf = self.buffers[tf]
        self._set_item(row, 0, tf, align=Qt.AlignCenter)

        if not buf:
            for c in range(1, len(self.COLS)):
                self._set_item(row, c, "-", align=Qt.AlignCenter)
            return

        last = buf[-1]
        # Time (1열)
        self._set_item(row, 1, last.t.strftime("%Y-%m-%d %H:%M"), align=Qt.AlignCenter)
        # MACD (2열), Signal (3열), Hist (4열)
        macd_color = "red" if last.macd > 0 else "blue" if last.macd < 0 else None
        sig_color = "red" if last.signal > 0 else "blue" if last.signal < 0 else None
        hist_color = "red" if last.hist > 0 else "blue" if last.hist < 0 else None
        self._set_item(row, 2, f"{last.macd:.5f}", color=macd_color)
        self._set_item(row, 3, f"{last.signal:.5f}", color=sig_color)
        self._set_item(row, 4, f"{last.hist:.5f}", color=hist_color)

        # ΔMACD 와 ΔHist
        prev_macd = buf[-2].macd if len(buf) > 1 else None
        prev_hist = buf[-2].hist if len(buf) > 1 else None
        macd_delta_text, macd_delta_color = self._fmt_val_with_dir(prev_macd, last.macd)
        hist_delta_text, hist_delta_color = self._fmt_val_with_dir(prev_hist, last.hist)

        self._set_item(row, 5, macd_delta_text, color=macd_delta_color)
        self._set_item(row, 6, hist_delta_text, color=hist_delta_color)

        # Cross (7열)
        # MACD와 Signal의 교차점 (Golden/Dead Cross)
        # 현재 코드에는 Cross 계산 로직이 없으므로 필요하다면 추가
        cross_text = "—"
        cross_color = None
        if prev_macd is not None and prev_macd < last.signal and last.macd > last.signal:
            cross_text = "G-Cross" # 골든 크로스
            cross_color = "red"
        elif prev_macd is not None and prev_macd > last.signal and last.macd < last.signal:
            cross_text = "D-Cross" # 데드 크로스
            cross_color = "blue"
        self._set_item(row, 7, cross_text, color=cross_color, align=Qt.AlignCenter, bold=True)
        
        # Trend(10) (8열) & Hist(10) (9열)
        macd_vals = [p.macd for p in list(buf)[-self.HISTORY_N:]]
        hist_vals = [p.hist for p in list(buf)[-self.HISTORY_N:]]

        self._trend_cols(row, 8, macd_vals, "MACD")
        self._trend_cols(row, 9, hist_vals, "Hist")


        # 최근 10개 MACD, HIST 값 추출
        macd_vals = [p.macd for p in list(buf)[-self.HISTORY_N:]]
        hist_vals = [p.hist for p in list(buf)[-self.HISTORY_N:]]
        
        # Trend(10) (MACD의 스파크라인)
        self._trend_cols(row, 8, macd_vals, "MACD")
        
        # Hist(10) (Hist의 스파크라인)
        self._trend_cols(row, 9, hist_vals, "Hist")

    # ------------ 시세(옵션) ------------
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

    def update_series(self, tf: str, series: dict): 
        if tf == "5m": 
            df = rows_to_df_minutes(series.get("rows")) 
            if not df.empty: 
                self._dfs["5m"] = df 
                if self._current_tf == "5m": 
                    self._render(df) 
        elif tf == "1d": 
            df = rows_to_df_daily(series.get("rows")) 
            if not df.empty: 
                self._dfs["1d"] = df 
                if self._current_tf == "1d": 
                    self._render(df)


    # ---------- 기타 ----------
    def _touch_updated(self):
        self.lbl_updated.setText(QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss"))


    def _on_clear_clicked(self):
        for tf in self.buffers:
            self.buffers[tf].clear()
        for row in range(3):
            for c in range(1, len(self.COLS)):
                self._set_item(row, c, "-", align=Qt.AlignCenter)

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

    # ------------ 종료 ------------
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
