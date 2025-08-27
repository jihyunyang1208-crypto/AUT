# macd_dialog.py
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional

import pandas as pd
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox
)
from PySide6.QtCharts import (
    QChart, QChartView, QLineSeries, QBarSeries, QBarSet,
    QDateTimeAxis, QValueAxis
)

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.dates import AutoDateLocator, DateFormatter

from PySide6.QtCore import Qt, QTimer, Signal, Slot
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

        # ------------ 기본 상태 ------------
        self.code = str(code)[-6:].zfill(6)
        self.bridge = bridge
        self._dfs: Dict[str, pd.DataFrame] = {     # OHLCV/가공 데이터 캐싱 용도
            "5m": pd.DataFrame(),
            "1d": pd.DataFrame(),
            "macd_5m": pd.DataFrame(),
            "macd_1d": pd.DataFrame(),
        }
        self._current_tf = "5m"                    # 기본 타임프레임
        self._sig_connected = False                # 중복 시그널 연결 방지 플래그

        # ------------ 윈도우 설정 ------------
        self.setWindowTitle(title or f"MACD 모니터 - {self.code}")
        self.setModal(True)
        self.setMinimumSize(960, 680)

        # ------------ 상단 UI ------------
        top = QHBoxLayout()
        self.lbl_code = QLabel(f"종목: <b>{self.code}</b>")
        self._quotes: Dict[str, str] = {"price": "", "rate": ""}  # ✅ 누락됐던 초기화
        self.lbl_quote = QLabel("")                                # 이미 있으시면 중복 생성 X
        self.lbl_quote.setStyleSheet("font-weight: bold; font-size: 14px;")

        self.cmb_tf = QComboBox()
        self.cmb_tf.addItems(["5분봉", "일봉"])  # 내부적으로 "5m" / "1d"로 매핑

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

        # 상단: 가격(or 원하는 라인), 하단: MACD
        self.ax_price = self.fig.add_subplot(2, 1, 1)
        self.ax_macd = self.fig.add_subplot(2, 1, 2, sharex=self.ax_price)

        # 축 스타일
        self.ax_price.grid(True, linestyle="--", alpha=0.3)
        self.ax_macd.grid(True, linestyle="--", alpha=0.3)
        self.ax_price.set_ylabel("Price")
        self.ax_macd.set_ylabel("MACD / Signal / Hist")
        self.ax_macd.set_xlabel("Time")

        # 시간축 포맷터
        locator = AutoDateLocator(minticks=5, maxticks=10)
        formatter = DateFormatter("%m-%d %H:%M")
        self.ax_macd.xaxis.set_major_locator(locator)
        self.ax_macd.xaxis.set_major_formatter(formatter)
        self.fig.autofmt_xdate()

        # ------------ 루트 레이아웃 ------------
        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addWidget(self.canvas)

        # ------------ 이벤트 연결 ------------
        self.cmb_tf.currentIndexChanged.connect(self._on_tf_changed)
        self.btn_refresh.clicked.connect(self._on_refresh_clicked)

        # ------------ 시그널 연결 (중복 방지) ------------
        # 외부 bridge가 있으면 존재하는 시그널에만 안전 연결
        if self.bridge and not self._sig_connected:
            try:
                if hasattr(self.bridge, "minute_bars_received"):
                    self.bridge.minute_bars_received.connect(self.on_minute_bars, Qt.UniqueConnection)
                if hasattr(self.bridge, "daily_bars_received"):
                    self.bridge.daily_bars_received.connect(self.on_daily_bars, Qt.UniqueConnection)
                # 다양한 이름 대응: macd_updated(dict) or macd_data_received(QString,double,double,double)
                if hasattr(self.bridge, "macd_updated"):
                    self.bridge.macd_updated.connect(self.on_macd_point, Qt.UniqueConnection)
                elif hasattr(self.bridge, "macd_data_received"):
                    self.bridge.macd_data_received.connect(self.on_macd_point, Qt.UniqueConnection)
            except TypeError:
                # 이미 연결되어 있으면 UniqueConnection에서 TypeError가 날 수 있음 → 무시
                pass

        # 전역 MACD 버스 연결(반드시 한 번만)
        try:
            macd_bus.macd_series_ready.connect(self.on_macd_series, Qt.UniqueConnection)
        except TypeError:
            # 이미 연결된 경우 무시
            pass

        self._sig_connected = True

        # ------------ 초기 렌더 안내 텍스트 ------------
        self.ax_price.text(0.02, 0.90, "가격 데이터 대기 중...", transform=self.ax_price.transAxes, fontsize=10, alpha=0.7)
        self.ax_macd.text(0.02, 0.90, "MACD 데이터 대기 중...", transform=self.ax_macd.transAxes, fontsize=10, alpha=0.7)
        self.canvas.draw()

    # (선택) 안전한 해제
    def closeEvent(self, e):
        # 시그널 해제 (필요 시)
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
            try:
                if hasattr(self.bridge, "macd_updated"):
                    self.bridge.macd_updated.disconnect(self.on_macd_point)
                elif hasattr(self.bridge, "macd_data_received"):
                    self.bridge.macd_data_received.disconnect(self.on_macd_point)
            except Exception:
                pass

        self._sig_connected = False
        super().closeEvent(e)

    def _init_chart_macd(self):
        # 시리즈 준비
        self._macd_line = QLineSeries(name="MACD")
        self._signal_line = QLineSeries(name="Signal")
        self._hist_bars = QBarSeries()  # 막대 묶음
        self._hist_set = QBarSet("Hist")
        self._hist_bars.append(self._hist_set)

        # 차트
        self._macd_chart = QChart()
        self._macd_chart.addSeries(self._macd_line)
        self._macd_chart.addSeries(self._signal_line)
        self._macd_chart.addSeries(self._hist_bars)
        self._macd_chart.legend().setVisible(True)
        self._macd_chart.setTitle("MACD (라인) / Histogram (막대)")

        # 축 (시간축 / 값축)
        self._axis_x = QDateTimeAxis()
        self._axis_x.setFormat("MM-dd HH:mm")
        self._axis_x.setTitleText("Time")

        self._axis_y = QValueAxis()
        self._axis_y.setTitleText("Value")

        self._macd_chart.addAxis(self._axis_x, Qt.AlignBottom)
        self._macd_chart.addAxis(self._axis_y, Qt.AlignLeft)

        # 시리즈를 축에 붙이기
        for s in (self._macd_line, self._signal_line, self._hist_bars):
            s.attachAxis(self._axis_x)
            s.attachAxis(self._axis_y)

        # ChartView
        self.macd_chart_view = QChartView(self._macd_chart)
        self.macd_chart_view.setRenderHint(self.macd_chart_view.renderHints())  # 기본 힌트 유지

        # 원하는 레이아웃에 추가 (예: 우측 패널 레이아웃)
        # self.right_layout.addWidget(self.macd_chart_view)

    # ---------- 슬롯 ----------

    @Slot(dict)
    def on_macd_series(self, data: dict):
        # data: {"code": str, "tf": "5m"|"1d", "series": [{"t": iso, "macd": f, "signal": f, "hist": f}, ...]}
        code = data.get("code")
        tf   = data.get("tf")
        series = data.get("series", [])

        # 코드/타임프레임 필터 (필요 시)
        # 기존 on_minute_bars의 패턴과 맞추려면:
        if code and hasattr(self, "code") and code[-6:] != self.code:
            return

        if not series:
            return

        # MACD용 DF 생성
        df = pd.DataFrame(series)
        # t를 인덱스로
        if "t" in df.columns:
            df["t"] = pd.to_datetime(df["t"])
            df = df.sort_values("t").set_index("t")

        # 캐시
        key = "5m" if tf == "5m" else "1d"
        if not hasattr(self, "_dfs"):
            self._dfs = {}
        if "_current_tf" not in self.__dict__:
            self._current_tf = "5m"

        self._dfs[f"macd_{key}"] = df  # ex) macd_5m / macd_1d

        # 현재 선택된 TF가 들어온 TF와 일치하면 렌더
        if self._current_tf == key:
            self._render_macd(df)

    def _render_macd(self, df_macd):
        """
        df_macd: index = datetime, columns = ["macd","signal","hist"]
        """
        if not hasattr(self, "_macd_chart"):
            self._init_chart_macd()

        # 기존 포인트/막대 초기화
        self._macd_line.clear()
        self._signal_line.clear()
        self._hist_set.remove(0, self._hist_set.count())  # 모두 제거

        # 값 범위 계산
        try:
            y_min = float(min(df_macd[["macd", "signal", "hist"]].min()))
            y_max = float(max(df_macd[["macd", "signal", "hist"]].max()))
        except Exception:
            y_min, y_max = -1.0, 1.0

        # 포인트 추가
        # QtCharts의 X는 ms단위 epoch. QDateTime.fromSecsSinceEpoch 사용 → //1000
        for idx, row in df_macd.iterrows():
            ts_ms = int(idx.timestamp() * 1000)
            self._macd_line.append(ts_ms, float(row["macd"]))
            self._signal_line.append(ts_ms, float(row["signal"]))
            # Hist 막대는 QBarSet이 index 기반이라 X축을 category로 쓰는 게 일반적.
            # 하지만 시간축(QDateTimeAxis)을 쓰고 싶다면, 막대 대신 라인으로 표현하거나,
            # 아래처럼 간단 표기로 막대 대체:
            self._hist_set.append(float(row["hist"]))

        # X축 범위
        if not df_macd.empty:
            start_ms = int(df_macd.index[0].timestamp() * 1000)
            end_ms = int(df_macd.index[-1].timestamp() * 1000)
            self._axis_x.setRange(QDateTime.fromMSecsSinceEpoch(start_ms),
                                QDateTime.fromMSecsSinceEpoch(end_ms))

        # Y축 범위 약간 여유
        pad = (y_max - y_min) * 0.1 if y_max > y_min else 1.0
        self._axis_y.setRange(y_min - pad, y_max + pad)

        # (선택) 텍스트 로그
        if hasattr(self, "text_result") and not df_macd.empty:
            last = df_macd.iloc[-1]
            self.text_result.append(
                f"[MACD] {df_macd.index[-1]:%m-%d %H:%M}  "
                f"macd={last['macd']:.5f}  signal={last['signal']:.5f}  hist={last['hist']:.5f}"
            )

    def closeEvent(self, e):
        try:
            macd_bus.macd_series_ready.disconnect(self.on_macd_series)
        except (TypeError, RuntimeError):
            pass
        super().closeEvent(e)

    @Slot(str, list)
    def on_minute_bars(self, code: str, rows: List[dict]):
        if code[-6:] != self.code:
            return
        df = rows_to_df_minutes(rows)
        if not df.empty:
            self._dfs["5m"] = df
            if self._current_tf == "5m":
                self._render(df)

    @Slot(str, list)
    def on_daily_bars(self, code: str, rows: List[dict]):
        if code[-6:] != self.code:
            return
        df = rows_to_df_daily(rows)
        if not df.empty:
            self._dfs["1d"] = df
            if self._current_tf == "1d":
                self._render(df)

    # 실시간 MACD 데이터 수신 슬롯
    @Slot(str, float, float, float)
    def push_point(self, code: str, macd: float, signal: float, hist: float):
        if code[-6:] != self.code:
            return
        
        # MACD 데이터를 그래프에 추가하는 로직을 여기에 구현
        # 현재는 타이틀에만 표시
        self.setWindowTitle(f"MACD 모니터 - {self.code} | MACD:{macd:.2f} SIG:{signal:.2f} HIST:{hist:.2f}")
        
    # 주식 현재가/등락률 업데이트
    def update_quote(self, price: str, rate: float):
        """UI 상단 호가/등락률 레이블 갱신"""
        # ✅ 내부 상태 갱신
        self._quotes["price"] = price if price is not None else ""
        self._quotes["rate"] = rate if rate is not None else ""

        # 색상(등락률 부호 기준)
        color = "#bdbdbd"
        try:
            # rate 예: "+5.17" 혹은 "+5.17%"
            r = rate.strip()
            if r.startswith("+"):
                color = "#d32f2f"  # 빨강 (상승)
            elif r.startswith("-"):
                color = "#1976d2"  # 파랑 (하락)
        except Exception:
            pass

        # 표시 문자열: "가격 6920 ( +5.17 )"
        text = f"{self._quotes['price']}  ({self._quotes['rate']})".strip()
        self.lbl_quote.setText(text)
        self.lbl_quote.setStyleSheet(f"font-weight:bold; font-size:14px; color:{color};")

    # 시계열 데이터 업데이트
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

    @Slot(str, float, float, float)
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
