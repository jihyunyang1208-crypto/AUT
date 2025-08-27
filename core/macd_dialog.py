from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional

import pandas as pd

# ---------------- PySide6 ----------------
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox

# ---------------- Matplotlib ----------------
import matplotlib
# ✅ 한글/마이너스 표시를 위한 기본 설정
matplotlib.rcParams["font.family"] = "Malgun Gothic"   # Windows: Malgun Gothic / macOS: AppleGothic / Linux: NanumGothic
matplotlib.rcParams["axes.unicode_minus"] = False

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.dates import AutoDateLocator, DateFormatter

# ---------------- 프로젝트 내부 ----------------
from core.macd_calculator import macd_bus


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


# ---------------- MACD 모달 다이얼로그 (Matplotlib 일원화) ----------------
class MacdDialog(QDialog):
    """
    - code: 감시 종목코드(6자리)
    - bridge: (선택) 브릿지 객체. 아래 이름의 시그널이 있으면 자동 연결합니다.
        * minute_bars_received(code: str, rows: list[dict])
        * daily_bars_received(code: str, rows: list[dict])
        * macd_updated(code: str, macd: float, signal: float, hist: float)  # 선택
        * macd_data_received(code: str, macd: float, signal: float, hist: float)  # 선택
    - macd_bus.macd_series_ready(dict): {"code","tf","series":[{"t", "macd","signal","hist"}]}
    """
    def __init__(self, code: str, bridge=None, parent=None, title: Optional[str] = None):
        super().__init__(parent)

        # ------------ 상태 ------------
        self.code = str(code)[-6:].zfill(6)
        self.bridge = bridge
        self._dfs: Dict[str, pd.DataFrame] = {     # OHLCV/가공 데이터 캐시
            "5m": pd.DataFrame(),
            "1d": pd.DataFrame(),
            "macd_5m": pd.DataFrame(),
            "macd_1d": pd.DataFrame(),
        }
        self._current_tf = "5m"
        self._sig_connected = False
        self._quotes: Dict[str, str] = {"price": "", "rate": ""}

        # ------------ 윈도우/상단 UI ------------
        self.setWindowTitle(title or f"MACD 모니터 - {self.code}")
        self.setModal(True)
        self.setMinimumSize(960, 680)

        top = QHBoxLayout()
        self.lbl_code = QLabel(f"종목: <b>{self.code}</b>")
        self.lbl_quote = QLabel("")
        self.lbl_quote.setStyleSheet("font-weight: bold; font-size: 14px;")

        self.cmb_tf = QComboBox()
        self.cmb_tf.addItems(["5분봉", "일봉"])  # 내부적으로 "5m" / "1d" 맵핑
        self.btn_refresh = QPushButton("새로고침")

        top.addWidget(self.lbl_code)
        top.addStretch(1)
        top.addWidget(self.lbl_quote)
        top.addSpacing(12)
        top.addWidget(self.cmb_tf)
        top.addWidget(self.btn_refresh)

        # ------------ Matplotlib 차트 ------------
        self.fig = Figure(figsize=(7.5, 5.5), tight_layout=True)
        self.canvas = FigureCanvas(self.fig)
        self.ax_price = self.fig.add_subplot(2, 1, 1)
        self.ax_macd = self.fig.add_subplot(2, 1, 2, sharex=self.ax_price)

        # 초기 안내 텍스트
        self.ax_price.text(0.02, 0.90, "가격 데이터 대기 중...", transform=self.ax_price.transAxes, fontsize=10, alpha=0.7)
        self.ax_macd.text(0.02, 0.90, "MACD 데이터 대기 중...", transform=self.ax_macd.transAxes, fontsize=10, alpha=0.7)
        self._style_axes()
        self._apply_time_formatter(self.ax_price)
        self._apply_time_formatter(self.ax_macd)
        self.canvas.draw()

        # ------------ 레이아웃 ------------
        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addWidget(self.canvas)

        # ------------ 이벤트 연결 ------------
        self.cmb_tf.currentIndexChanged.connect(self._on_tf_changed)
        self.btn_refresh.clicked.connect(self._on_refresh_clicked)

        # ------------ 외부 시그널 연결 ------------
        if self.bridge and not self._sig_connected:
            try:
                if hasattr(self.bridge, "minute_bars_received"):
                    self.bridge.minute_bars_received.connect(self.on_minute_bars, Qt.UniqueConnection)
                if hasattr(self.bridge, "daily_bars_received"):
                    self.bridge.daily_bars_received.connect(self.on_daily_bars, Qt.UniqueConnection)
                if hasattr(self.bridge, "macd_updated"):
                    self.bridge.macd_updated.connect(self.on_macd_point, Qt.UniqueConnection)
                elif hasattr(self.bridge, "macd_data_received"):
                    self.bridge.macd_data_received.connect(self.on_macd_point, Qt.UniqueConnection)
            except TypeError:
                pass

        try:
            macd_bus.macd_series_ready.connect(self.on_macd_series, Qt.UniqueConnection)
        except TypeError:
            pass

        self._sig_connected = True

    # ---------- 스타일/포맷 ----------
    def _style_axes(self):
        self.ax_price.grid(True, linestyle="--", alpha=0.3)
        self.ax_macd.grid(True, linestyle="--", alpha=0.3)
        self.ax_price.set_ylabel("Price")
        self.ax_macd.set_ylabel("MACD / Signal / Hist")
        self.ax_macd.set_xlabel("Time")

    def _apply_time_formatter(self, ax):
        locator = AutoDateLocator(minticks=5, maxticks=10)
        if self._current_tf == "5m":
            formatter = DateFormatter("%m-%d %H:%M")
        else:
            formatter = DateFormatter("%Y-%m-%d")
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(formatter)

    # ---------- 데이터 수신 슬롯 ----------
    @Slot(str, list)
    def on_minute_bars(self, code: str, rows: List[dict]):
        if code[-6:] != self.code:
            return
        df = rows_to_df_minutes(rows)
        if df.empty:
            return
        self._dfs["5m"] = df
        if self._current_tf == "5m":
            self._render_price_and_macd()

    @Slot(str, list)
    def on_daily_bars(self, code: str, rows: List[dict]):
        if code[-6:] != self.code:
            return
        df = rows_to_df_daily(rows)
        if df.empty:
            return
        self._dfs["1d"] = df
        if self._current_tf == "1d":
            self._render_price_and_macd()

    @Slot(dict)
    def on_macd_series(self, data: dict):
        # data: {"code": str, "tf": "5m"|"1d", "series": [{"t": iso, "macd": f, "signal": f, "hist": f}, ...]}
        code = data.get("code")
        tf = (data.get("tf") or "").lower()
        series = data.get("series", [])
        if code and code[-6:] != self.code:
            return
        if not series:
            return

        df = pd.DataFrame(series)
        if "t" in df.columns:
            df["t"] = pd.to_datetime(df["t"])
            df = df.sort_values("t").set_index("t")

        key = "5m" if tf == "5m" else "1d"
        self._dfs[f"macd_{key}"] = df

        # 현재 TF가 일치하면 하단 MACD만/혹은 전체 재렌더
        if self._current_tf == key:
            self._render_price_and_macd()

    @Slot(str, float, float, float)
    def on_macd_point(self, code: str, macd: float, signal: float, hist: float):
        if code[-6:] != self.code:
            return
        self.setWindowTitle(f"MACD 모니터 - {self.code}  |  MACD:{macd:.2f}  SIG:{signal:.2f}  HIST:{hist:.2f}")

    # ---------- 상단 시세 ----------
    def update_quote(self, price: str, rate):
        """UI 상단 호가/등락률 레이블 갱신 (float/str 모두 안전 처리)"""
        self._quotes["price"] = "" if price is None else str(price)
        if rate is None:
            rate_str = ""
        elif isinstance(rate, (int, float)):
            rate_str = f"{rate:+.2f}%"
        else:
            rate_str = str(rate)
        self._quotes["rate"] = rate_str

        color = "#bdbdbd"
        if rate_str.startswith("+"):
            color = "#d32f2f"  # 상승(빨강)
        elif rate_str.startswith("-"):
            color = "#1976d2"  # 하락(파랑)

        self.lbl_quote.setText(f"{self._quotes['price']}  ({rate_str})".strip())
        self.lbl_quote.setStyleSheet(f"font-weight:bold; font-size:14px; color:{color};")

    # ---------- 차트 렌더 ----------
    def _render_price_and_macd(self):
        """현재 타임프레임(self._current_tf)의 가격 + MACD를 한 번에 렌더링"""
        df_price = self._dfs.get(self._current_tf, pd.DataFrame())
        if df_price is None or df_price.empty or "close" not in df_price.columns:
            return

        # MACD 원본(실시간) 시리즈가 들어온 경우 우선 사용, 없으면 계산
        df_macd = self._dfs.get(f"macd_{self._current_tf}", pd.DataFrame())
        if df_macd is None or df_macd.empty or not set(["macd", "signal", "hist"]).issubset(df_macd.columns):
            df_macd = calc_macd(df_price["close"])

        # 가격/지표 축 초기화
        self.ax_price.clear()
        self.ax_macd.clear()
        self._style_axes()

        # 가격
        self.ax_price.plot(df_price.index, df_price["close"], label="Close")
        self.ax_price.set_title(f"{self.code} - {('5분봉' if self._current_tf=='5m' else '일봉')}")
        self.ax_price.legend(loc="upper left")

        # MACD
        if not df_macd.empty:
            self.ax_macd.plot(df_macd.index, df_macd["macd"], label="MACD")
            self.ax_macd.plot(df_macd.index, df_macd["signal"], label="Signal")
            self.ax_macd.bar(df_macd.index, df_macd["hist"], alpha=0.4)
            self.ax_macd.axhline(0, linestyle="--", linewidth=1)
            self.ax_macd.legend(loc="upper left")

        # 시간 포맷(매 렌더마다 재부착)
        self._apply_time_formatter(self.ax_price)
        self._apply_time_formatter(self.ax_macd)
        self.fig.autofmt_xdate()

        self.canvas.draw_idle()

    # ---------- UI 이벤트 ----------
    def _on_tf_changed(self, idx: int):
        self._current_tf = "5m" if idx == 0 else "1d"
        self._render_price_and_macd()

    def _on_refresh_clicked(self):
        if not self.bridge:
            return
        try:
            if self._current_tf == "5m" and hasattr(self.bridge, "request_minutes_bars"):
                self.bridge.request_minutes_bars(self.code)
            elif self._current_tf == "1d" and hasattr(self.bridge, "request_daily_bars"):
                self.bridge.request_daily_bars(self.code)
        except Exception:
            pass

    # ---------- 안전한 해제 ----------
    def closeEvent(self, e):
        # macd_bus 해제
        try:
            macd_bus.macd_series_ready.disconnect(self.on_macd_series)
        except Exception:
            pass
        # bridge 시그널 해제
        if self.bridge:
            for name, slot in [
                ("minute_bars_received", self.on_minute_bars),
                ("daily_bars_received", self.on_daily_bars),
                ("macd_updated", self.on_macd_point),
                ("macd_data_received", self.on_macd_point),
            ]:
                try:
                    if hasattr(self.bridge, name):
                        getattr(self.bridge, name).disconnect(slot)
                except Exception:
                    pass
        super().closeEvent(e)