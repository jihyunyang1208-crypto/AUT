# core/macd_calculator.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

# =============== 공통 유틸 ===============

def _pick(d: Dict[str, Any], keys: List[str], default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", "-"):
            return v
    return default

def _to_float(x) -> Optional[float]:
    try:
        s = str(x).replace(",", "")
        return float(s)
    except Exception:
        return None

def _parse_dt_ymd(s: str) -> Optional[pd.Timestamp]:
    """YYYYMMDD → Timestamp"""
    try:
        s = str(s)
        return pd.to_datetime(s, format="%Y%m%d", errors="coerce")
    except Exception:
        return None

def _parse_dt_ymd_hms(d: str, t: str) -> Optional[pd.Timestamp]:
    """(YYYYMMDD, HHMMSS) → Timestamp"""
    try:
        d = str(d); t = str(t).zfill(6)
        return pd.to_datetime(d + t, format="%Y%m%d%H%M%S", errors="coerce")
    except Exception:
        return None

# =============== rows → DataFrame 변환 ===============

def rows_to_df_daily(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    일봉 응답 rows를 o/h/l/c/volume 로 정규화하여 DatetimeIndex 로 반환
    허용 키 예:
      - 날짜: dt
      - o/h/l/c: open_pric, high_pric, low_pric, close_pric / (fallback: cur_prc)
      - volume: trqu, acml_vol, now_trde_qty, trde_qty
    """
    recs: List[Tuple[pd.Timestamp, float, float, float, float, float]] = []
    for r in rows or []:
        dt = _parse_dt_ymd(_pick(r, ["dt"]))
        if dt is None:
            continue
        o = _to_float(_pick(r, ["open_pric"]))
        h = _to_float(_pick(r, ["high_pric"]))
        l = _to_float(_pick(r, ["low_pric"]))
        c = _to_float(_pick(r, ["close_pric", "cur_prc"]))
        v = _to_float(_pick(r, ["trqu", "acml_vol", "now_trde_qty", "trde_qty"]))
        if None in (o, h, l, c):
            # 최소 OHLC는 있어야 차트 가능
            continue
        recs.append((dt, o, h, l, c, v or 0.0))

    if not recs:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(recs, columns=["dt", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["dt"]).sort_values("dt").set_index("dt")
    return df


def rows_to_df_minute(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    분봉 응답 rows를 o/h/l/c/volume 로 정규화하여 DatetimeIndex 로 반환
    허용 키 예:
      - 날짜/시간: dt(YYYYMMDD), cntr_tm(HHMMSS) / (fallback: time)
      - o/h/l/c: open_pric, high_pric, low_pric, close_pric / (fallback: cur_prc)
      - volume: trqu, now_trde_qty, trde_qty
    """
    recs: List[Tuple[pd.Timestamp, float, float, float, float, float]] = []
    for r in rows or []:
        d = _pick(r, ["dt"])
        t = _pick(r, ["cntr_tm", "time"], "000000")
        ts = _parse_dt_ymd_hms(d, t)
        if ts is None:
            continue
        o = _to_float(_pick(r, ["open_pric"]))
        h = _to_float(_pick(r, ["high_pric"]))
        l = _to_float(_pick(r, ["low_pric"]))
        c = _to_float(_pick(r, ["close_pric", "cur_prc"]))
        v = _to_float(_pick(r, ["trqu", "now_trde_qty", "trde_qty"]))
        if None in (o, h, l, c):
            continue
        recs.append((ts, o, h, l, c, v or 0.0))

    if not recs:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(recs, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["ts"]).sort_values("ts").set_index("ts")
    return df

# =============== MACD 계산 (pandas EMA) ===============

def _ema(series: pd.Series, span: int) -> pd.Series:
    # adjust=False 로 지수평활(지연 덜 생김), min_periods=span 로 안정화
    return series.ewm(span=span, adjust=False, min_periods=span).mean()

def compute_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """
    close 시리즈로 MACD / signal / hist 계산
    반환: DataFrame[macd, signal, hist]
    """
    close = pd.to_numeric(close, errors="coerce").dropna()
    if close.empty:
        return pd.DataFrame(columns=["macd", "signal", "hist"])

    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    macd = ema_fast - ema_slow
    signal_line = _ema(macd, signal)
    hist = macd - signal_line

    out = pd.DataFrame({"macd": macd, "signal": signal_line, "hist": hist})
    return out

# =============== 증분형 EMA/MACD 갱신 ===============

@dataclass
class MacdState:
    fast: float
    slow: float
    signal: float

@dataclass
class MacdParams:
    fast: int = 12
    slow: int = 26
    signal: int = 9

    @property
    def k_fast(self) -> float:
        return 2.0 / (self.fast + 1.0)

    @property
    def k_slow(self) -> float:
        return 2.0 / (self.slow + 1.0)

    @property
    def k_signal(self) -> float:
        return 2.0 / (self.signal + 1.0)

def seed_macd_state(close: pd.Series, p: MacdParams = MacdParams()) -> Optional[MacdState]:
    """
    초기 구간으로 EMA들을 계산해 상태를 시드합니다.
    최소 slow+signal 개수 정도는 있어야 안정적.
    """
    if len(close) < max(p.slow, p.signal) + 1:
        return None
    ema_fast = _ema(close, p.fast).iloc[-1]
    ema_slow = _ema(close, p.slow).iloc[-1]
    macd_last = ema_fast - ema_slow
    signal_last = _ema(pd.Series(macd_last, index=[close.index[-1]]).reindex(close.index, method="ffill"), p.signal).iloc[-1]
    # 위 구현은 마지막 macd만으로 signal을 만드는 케이스가 있어 부정확할 수 있음.
    # 더 정확히는 전체 macd 시리즈로 signal을 계산:
    macd_series = _ema(close, p.fast) - _ema(close, p.slow)
    signal_last = _ema(macd_series, p.signal).iloc[-1]
    return MacdState(fast=float(ema_fast), slow=float(ema_slow), signal=float(signal_last))

def update_macd_incremental(state: MacdState, new_close: float, p: MacdParams = MacdParams()) -> Tuple[MacdState, Dict[str, float]]:
    """
    새 종가 1개가 들어올 때 EMA들을 갱신하고 macd/signal/hist를 반환
    EMA_t = EMA_{t-1} + k * (price_t - EMA_{t-1})
    """
    kf, ks, ksiga = p.k_fast, p.k_slow, p.k_signal
    fast_new = state.fast + kf * (new_close - state.fast)
    slow_new = state.slow + ks * (new_close - state.slow)
    macd_new = fast_new - slow_new
    signal_new = state.signal + ksiga * (macd_new - state.signal)
    hist_new = macd_new - signal_new
    return (
        MacdState(fast=fast_new, slow=slow_new, signal=signal_new),
        {"macd": macd_new, "signal": signal_new, "hist": hist_new},
    )

# =============== 직렬화/페이로드 헬퍼 ===============

def to_series_payload(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    MACD DataFrame을 [{t, macd, signal, hist}, ...] 로 직렬화 (UI/브릿지 전달용)
    """
    if df is None or df.empty:
        return []
    out: List[Dict[str, Any]] = []
    for ts, row in df.iterrows():
        out.append({
            "t": str(pd.Timestamp(ts)),
            "macd": float(row.get("macd", float("nan"))),
            "signal": float(row.get("signal", float("nan"))),
            "hist": float(row.get("hist", float("nan"))),
        })
    return out
