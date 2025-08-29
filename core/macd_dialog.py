# ui/macd_dialog.py
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, List, Optional

import pandas as pd
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QCheckBox, QAbstractItemView
)

# 전역 MACD 버스
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
    차트 없이 수치만 모니터링.
    - TF: 5m / 30m / 1d
    - 각 TF 행: 최신 시각, 최신값(MACD/Signal/Hist), MACD 최근10개(+방향), HIST 최근10개(+방향)
    - 입력: macd_bus.macd_series_ready({"code","tf","series":[{"t","macd","signal","hist"}]})
    """
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
        self.setMinimumSize(1100, 420)

        # 상단
        top = QHBoxLayout()
        self.lbl_code = QLabel(f"종목: <b>{self.code}</b>")
        self.lbl_quote = QLabel("")
        self.btn_clear = QPushButton("초기화")
        self.chk_autorefresh = QCheckBox("자동갱신")
        self.chk_autorefresh.setChecked(True)
        top.addWidget(self.lbl_code)
        top.addStretch(1)
        top.addWidget(self.lbl_quote)
        top.addSpacing(8)
        top.addWidget(self.chk_autorefresh)
        top.addWidget(self.btn_clear)

        # 표 컬럼
        macd_cols = [f"MACD[{i}]" for i in range(self.HISTORY_N, 0, -1)]  # [10] ... [1] (오른쪽이 최신)
        hist_cols = [f"HIST[{i}]" for i in range(self.HISTORY_N, 0, -1)]
        self.COLS = ["TF", "Time", "MACD", "Signal", "Hist"] + macd_cols + hist_cols

        self.table = QTableWidget(3, len(self.COLS), self)
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)

        for row, tf in enumerate(["5m", "30m", "1d"]):
            self._set_item(row, 0, tf, align=Qt.AlignCenter)

        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addWidget(self.table)

        # 이벤트
        self.btn_clear.clicked.connect(self._on_clear_clicked)

        # 시그널 연결
        try:
            macd_bus.macd_series_ready.connect(self.on_macd_series, Qt.UniqueConnection)
        except Exception:
            pass

        if self.bridge:
            # (옵션) raw rows 수신 시 내부 계산 경로
            try:
                if hasattr(self.bridge, "minute_bars_received"):
                    self.bridge.minute_bars_received.connect(self.on_minute_bars, Qt.UniqueConnection)
                if hasattr(self.bridge, "daily_bars_received"):
                    self.bridge.daily_bars_received.connect(self.on_daily_bars, Qt.UniqueConnection)
            except Exception:
                pass

        self._refresh_all_rows()

    # ---------- 셀 헬퍼 ----------
    def _set_item(self, row: int, col: int, text: str, *, color: Optional[str] = None,
                  align: Qt.AlignmentFlag = Qt.AlignRight | Qt.AlignVCenter):
        item = QTableWidgetItem(text)
        item.setTextAlignment(align)
        if color == "red":
            item.setForeground(Qt.red)
        elif color == "blue":
            item.setForeground(Qt.blue)
        elif color == "green":
            item.setForeground(Qt.darkGreen)
        self.table.setItem(row, col, item)

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

    # (옵션) raw rows 경로: 내부 계산하여 버퍼에 쌓기
    @Slot(str, list)
    def on_minute_bars(self, code: str, rows: List[dict]):
        if code[-6:] != self.code:
            return
        # raw rows를 calculator로 보내 바로 emit되도록 사용 가능
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

    def _refresh_row(self, tf: str):
        row = 0 if tf == "5m" else 1 if tf == "30m" else 2
        buf = self.buffers[tf]
        self._set_item(row, 0, tf, align=Qt.AlignCenter)

        if not buf:
            for c in range(1, len(self.COLS)):
                self._set_item(row, c, "-", align=Qt.AlignCenter)
            return

        last = buf[-1]

        # 최신 시각
        self._set_item(row, 1, last.t.strftime("%Y-%m-%d %H:%M"), align=Qt.AlignCenter)

        # 최신 값 3종
        macd_color = "red" if last.macd > 0 else "blue" if last.macd < 0 else None
        sig_color = "red" if last.signal > 0 else "blue" if last.signal < 0 else None
        hist_color = "red" if last.hist > 0 else "blue" if last.hist < 0 else None

        self._set_item(row, 2, f"{last.macd:.5f}", color=macd_color)
        self._set_item(row, 3, f"{last.signal:.5f}", color=sig_color)
        self._set_item(row, 4, f"{last.hist:.5f}", color=hist_color)

        # 최근 10개 MACD/HIST + 방향(직전 대비)
        macd_vals = [p.macd for p in list(buf)[-self.HISTORY_N:]]
        hist_vals = [p.hist for p in list(buf)[-self.HISTORY_N:]]

        macd_start_col = 5
        hist_start_col = macd_start_col + self.HISTORY_N

        for i, v in enumerate(macd_vals):
            prev_v = macd_vals[i-1] if i > 0 else None
            text, c = self._fmt_val_with_dir(prev_v, v)
            self._set_item(row, macd_start_col + i, text, color=c, align=Qt.AlignCenter)

        for i, v in enumerate(hist_vals):
            prev_v = hist_vals[i-1] if i > 0 else None
            text, c = self._fmt_val_with_dir(prev_v, v)
            self._set_item(row, hist_start_col + i, text, color=c, align=Qt.AlignCenter)

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

    # ---------- 초기화 ----------
    def _on_clear_clicked(self):
        for tf in self.buffers:
            self.buffers[tf].clear()
        self._refresh_all_rows()

    # ---------- 종료 ----------
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
