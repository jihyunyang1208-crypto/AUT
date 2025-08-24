# macd_dialog.py
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional

import pandas as pd
from PyQt5.QtCore import Qt, pyqtSlot
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox
)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure


# ---------------- MACD (pandas만 사용) ----------------
def calc_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    if close is None or close.empty:
        return pd.DataFrame(columns=["macd", "signal", "hist"])
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    macd = ema_f - ema_s
    sig = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - sig
    return pd.DataFrame({"macd": macd, "signal": sig, "hist": hist})


# ---------------- rows → DataFrame 유틸 ----------------
def _parse_dt(date_str: Optional[str], time_str: Optional[str]) -> Optional[pd.Timestamp]:
    if not date_str:
        return None
    ds = str(date_str)
    if len(ds) == 8 and ds.isdigit():  # YYYYMMDD
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
        t = r.get("trd_tm") or r.get("time") or r.get("tm")
        ts = _parse_dt(d, t)
        if ts is None or pd.isna(ts):
            continue
        recs.append({
            "dt": ts,
            "open": _to_float(r.get("open_pric") or r.get("open") or r.get("o")),
            "high": _to_float(r.get("high_pric") or r.get("high") or r.get("h")),
            "low":  _to_float(r.get("low_pric") or r.get("low") or r.get("l")),
            "close": _to_float(r.get("close_pric") or r.get("close") or r.get("c")),
            "volume": _to_float(r.get("trde_qty") or r.get("volume") or r.get("v")),
        })
    if not recs:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(recs).set_index("dt").sort_index()
    return df


def rows_to_df_daily(rows: Iterable[dict]) -> pd.DataFrame:
    recs = []
    for r in rows or []:
        d = r.get("base_dt") or r.get("trd_dd") or r.get("dt") or r.get("date")
        ts = _parse_dt(d, None)
        if ts is None or pd.isna(ts):
            continue
        recs.append({
            "dt": ts,
            "open": _to_float(r.get("open_pric") or r.get("open")),
            "high": _to_float(r.get("high_pric") or r.get("high")),
            "low":  _to_float(r.get("low_pric") or r.get("low")),
            "close": _to_float(r.get("close_pric") or r.get("close")),
            "volume": _to_float(r.get("trde_qty") or r.get("volume")),
        })
    if not recs:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(recs).set_index("dt").sort_index()
    return df


# ---------------- MACD 모달 다이얼로그 ----------------
class MacdDialog(QDialog):
    """
    - code: 감시 종목코드(6자리)
    - bridge: (선택) 브릿지 객체. 아래 이름의 시그널이 있으면 자동 연결합니다.
        * minute_bars_received(code: str, rows: list[dict])
        * daily_bars_received(code: str, rows: list[dict])
        * macd_data_received(code: str, macd: float, signal: float, hist: float)  # 선택
    """
    def __init__(self, code: str, bridge=None, parent=None, title: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle(title or f"MACD 모니터 - {code}")
        self.setModal(True)          # 진짜 모달
        self.setMinimumSize(860, 640)

        self.code = str(code)[-6:].zfill(6)
        self.bridge = bridge
        self._dfs: Dict[str, pd.DataFrame] = {"5m": pd.DataFrame(), "1d": pd.DataFrame()}
        self._current_tf = "5m"

        # UI
        top = QHBoxLayout()
        self.lbl = QLabel(f"종목: <b>{self.code}</b>")
        self.cmb_tf = QComboBox()
        self.cmb_tf.addItems(["5분봉", "일봉"])
        self.btn_refresh = QPushButton("새로고침")
        top.addWidget(self.lbl)
        top.addStretch(1)
        top.addWidget(self.cmb_tf)
        top.addWidget(self.btn_refresh)

        self.fig = Figure(figsize=(6, 4), tight_layout=True)
        self.canvas = FigureCanvas(self.fig)
        self.ax_price = self.fig.add_subplot(2, 1, 1)
        self.ax_macd = self.fig.add_subplot(2, 1, 2, sharex=self.ax_price)

        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addWidget(self.canvas)

        # 이벤트
        self.cmb_tf.currentIndexChanged.connect(self._on_tf_changed)
        self.btn_refresh.clicked.connect(self._on_refresh_clicked)

        # 브릿지 시그널 연결(있을 때만)
        if self.bridge:
            if hasattr(self.bridge, "minute_bars_received"):
                self.bridge.minute_bars_received.connect(self.on_minute_bars)
            if hasattr(self.bridge, "daily_bars_received"):
                self.bridge.daily_bars_received.connect(self.on_daily_bars)
            if hasattr(self.bridge, "macd_data_received"):
                self.bridge.macd_data_received.connect(self.on_macd_point)

    # ---------- 슬롯 ----------
    @pyqtSlot(str, list)
    def on_minute_bars(self, code: str, rows: List[dict]):
        if code[-6:] != self.code:
            return
        df = rows_to_df_minutes(rows)
        if not df.empty:
            self._dfs["5m"] = df
            if self._current_tf == "5m":
                self._render(df)

    @pyqtSlot(str, list)
    def on_daily_bars(self, code: str, rows: List[dict]):
        if code[-6:] != self.code:
            return
        df = rows_to_df_daily(rows)
        if not df.empty:
            self._dfs["1d"] = df
            if self._current_tf == "1d":
                self._render(df)

    @pyqtSlot(str, float, float, float)
    def on_macd_point(self, code: str, macd: float, signal: float, hist: float):
        # 선택 기능: 실시간 포인트만 주어질 때 상태바에 간단 표기
        if code[-6:] != self.code:
            return
        self.setWindowTitle(f"MACD 모니터 - {self.code}  |  MACD:{macd:.2f}  SIG:{signal:.2f}  HIST:{hist:.2f}")

    # ---------- 내부 ----------
    def _on_tf_changed(self, idx: int):
        self._current_tf = "5m" if idx == 0 else "1d"
        df = self._dfs.get(self._current_tf, pd.DataFrame())
        if not df.empty:
            self._render(df)

    def _on_refresh_clicked(self):
        # 브릿지 쪽에 재요청 메서드가 있으면 호출(있을 때만)
        if not self.bridge:
            return
        req = None
        if self._current_tf == "5m" and hasattr(self.bridge, "request_minutes_bars"):
            req = ("5m", getattr(self.bridge, "request_minutes_bars"))
        elif self._current_tf == "1d" and hasattr(self.bridge, "request_daily_bars"):
            req = ("1d", getattr(self.bridge, "request_daily_bars"))
        if req:
            _, fn = req
            try:
                fn(self.code)  # bridge에서 해당 종목 bars를 다시 emit 하도록
            except Exception:
                pass

    def _render(self, df: pd.DataFrame):
        if df is None or df.empty or "close" not in df.columns:
            return

        macd_df = calc_macd(df["close"])
        self.ax_price.clear()
        self.ax_macd.clear()

        self.ax_price.plot(df.index, df["close"])
        self.ax_price.set_title(f"{self.code} - {('5분봉' if self._current_tf=='5m' else '일봉')}")

        if not macd_df.empty:
            self.ax_macd.plot(macd_df.index, macd_df["macd"], label="MACD")
            self.ax_macd.plot(macd_df.index, macd_df["signal"], label="Signal")
            self.ax_macd.bar(macd_df.index, macd_df["hist"], alpha=0.4)
            self.ax_macd.axhline(0, linestyle="--", linewidth=1)
            self.ax_macd.legend(loc="upper left")

        self.canvas.draw_idle()

    # ---------- 정리 ----------
    def closeEvent(self, e):
        # 시그널 해제
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
            try:
                if hasattr(self.bridge, "macd_data_received"):
                    self.bridge.macd_data_received.disconnect(self.on_macd_point)
            except Exception:
                pass
        super().closeEvent(e)
