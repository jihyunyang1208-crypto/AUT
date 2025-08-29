# core/macd_calculator.py
from __future__ import annotations
from typing import List, Dict, Any, Tuple
from datetime import timezone, timedelta
import pandas as pd
from PySide6.QtCore import QObject, Signal

import logging
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

class MacdBus(QObject):
    """
    계산 결과를 UI/다이얼로그로 전달하는 공용 버스.
    페이로드:
      {"code": str, "tf": "5m"/"30m"/"1d", "series": [{"t": iso, "macd": float, "signal": float, "hist": float}, ...]}
    """
    macd_series_ready = Signal(dict)

macd_bus = MacdBus()  # 싱글톤처럼 사용


class MacdCalculator:
    """코드+타임프레임별 상태를 내부에 유지하여 MACD 계산을 수행"""
    def __init__(self, fast=12, slow=26, signal=9):
        self.fast = fast
        self.slow = slow
        self.signal = signal
        # 상태 저장 (필요 시 증분계산 확장 가능)
        self._states: Dict[Tuple[str, str], Dict[str, float]] = {}

    # ---------------- 외부 엔트리 포인트 ----------------
    def apply_rows(self, code: str, tf: str, rows: List[Dict[str, Any]], need: int = 120) -> None:
        """
        rows(원시 분/일봉 배열)를 받아:
          1) 시계열 DataFrame 변환
          2) MACD(full) 계산
          3) 최근 need개 직렬화하여 버스로 emit
        rows 예:
          - {"ts":"2025-08-27T14:30:00+09:00", "open":..,"high":..,"low":..,"close":..,"vol":..}
          - 또는 {"base_dt":"20250827","trd_tm":"143000", "close":..} / {"dt":"20250827","cntr_tm":"143000", ...}
          - 일봉은 time이 없으면 09:00:00을 기본으로 부여함
        """
        tf = str(tf).lower()
        if tf not in ("5m", "30m", "1d"):
            return

        df = self._rows_to_df(rows, tf)
        if df.empty or "close" not in df.columns:
            return

        state, macd_df = self._init_state_from_history(df["close"])
        self._states[(code, tf)] = state

        payload = self._to_series_payload(macd_df.tail(need))
        macd_bus.macd_series_ready.emit({"code": code, "tf": tf, "series": payload})

    # ---------------- 내부 유틸 ----------------
    def _rows_to_df(self, rows: List[Dict[str, Any]], tf: str) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()

        recs = []
        for r in rows:
            ts = r.get("ts")
            if not ts:
                d = str(r.get("base_dt") or r.get("trd_dd") or r.get("dt") or r.get("date") or "")
                t = str(r.get("trd_tm") or r.get("cntr_tm") or r.get("time") or r.get("tm") or "")
                if len(d) == 8 and d.isdigit():
                    if len(t) == 6 and t.isdigit():
                        ts = pd.to_datetime(d + t, format="%Y%m%d%H%M%S", errors="coerce")
                    else:
                        ts = pd.to_datetime(d + "090000", format="%Y%m%d%H%M%S", errors="coerce")
                else:
                    # ✅ 여기 추가: d(날짜)가 없더라도 t(=cntr_tm)가 14자리면 그 자체가 YYYYMMDDHHMMSS
                    if len(t) == 14 and t.isdigit():
                        ts = pd.to_datetime(t, format="%Y%m%d%H%M%S", errors="coerce")
                    else:
                        ts = pd.to_datetime(r.get("ts"), errors="coerce")
            else:
                ts = pd.to_datetime(ts, errors="coerce")

            if ts is None or pd.isna(ts):
                continue

            # ✅ close: close/close_pric 가 없으면 cur_prc 사용 (KA10080 실 응답 커버)
            close_val = (r.get("close") or r.get("close_pric") or r.get("cur_prc"))

            recs.append({
                "ts": ts,
                "open": _to_float(r.get("open") or r.get("open_pric")),
                "high": _to_float(r.get("high") or r.get("high_pric")),
                "low":  _to_float(r.get("low")  or r.get("low_pric")),
                "close": _to_float(close_val),
                "vol": _to_float(r.get("vol") or r.get("trde_qty") or r.get("volume") or r.get("v")),
            })

        if not recs:
            return pd.DataFrame()

        df = pd.DataFrame(recs).set_index("ts").sort_index()
        return df

    def _init_state_from_history(self, close_series: pd.Series) -> Tuple[Dict[str, float], pd.DataFrame]:
        ema_fast = close_series.ewm(span=self.fast, adjust=False).mean()
        ema_slow = close_series.ewm(span=self.slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=self.signal, adjust=False).mean()
        hist = macd_line - signal_line

        macd_df = pd.DataFrame({
            "close": close_series,
            "macd": macd_line,
            "signal": signal_line,
            "hist": hist
        })

        state = {
            "ema_fast": float(ema_fast.iloc[-1]),
            "ema_slow": float(ema_slow.iloc[-1]),
            "ema_signal": float(signal_line.iloc[-1]),
            "last_macd": float(macd_line.iloc[-1]),
            "last_hist": float(hist.iloc[-1]),
        }
        return state, macd_df

    def _to_series_payload(self, df: pd.DataFrame) -> list[dict]:
        out: list[dict] = []
        for ts, row in df.iterrows():
            t = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            out.append({
                "t": t,
                "macd": _to_float(row.get("macd")),
                "signal": _to_float(row.get("signal")),
                "hist": _to_float(row.get("hist")),
            })
        return out


def _to_float(x) -> float:
    if x is None or x == "":
        return float("nan")
    try:
        s = str(x).replace(",", "")
        neg = s.startswith("-")
        s = s.lstrip("+-")
        v = float(s)
        return -v if neg else v
    except Exception:
        return float("nan")

def log_macd_result(payload: dict):
    code = payload["code"]
    tf = payload["tf"]
    series = payload["series"]
    logger.info(f"--- MACD Calculation Result for {code} ({tf}) ---")
    for data_point in series[-5:]: # 최근 5개만 출력하여 간결하게
        logger.info(f"Time: {data_point['t']}, MACD: {data_point['macd']:.2f}, Signal: {data_point['signal']:.2f}, Hist: {data_point['hist']:.2f}")
    logger.info("---------------------------------------------")


# 전역(혹은 DI)
calculator = MacdCalculator()
macd_bus.macd_series_ready.connect(log_macd_result)
