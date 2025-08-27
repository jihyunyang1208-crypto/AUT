# core/macd_calculator.py
from __future__ import annotations
from typing import List, Dict, Any, Tuple
import pandas as pd
from datetime import datetime, timezone, timedelta
from PySide6.QtCore import QObject, Signal

KST = timezone(timedelta(hours=9))

class MacdBus(QObject):
    """
    계산 결과를 UI/다이얼로그로 전달하는 공용 버스(브릿지 대체/보조).
    외부에서 bridge로 연결하고 싶다면, 이 신호를 bridge에 연결해서
    bridge가 다시 재발행하는 패턴도 가능.
    """
    macd_series_ready = Signal(dict)  # {"code": str, "tf": "5m"/"30m", "series": list[dict]}

macd_bus = MacdBus()  # 싱글톤처럼 사용


class MacdCalculator:
    """코드+타임프레임별 상태를 내부에 유지하여 증분 MACD를 빠르게 계산"""
    def __init__(self, fast=12, slow=26, signal=9):
        self.fast = fast
        self.slow = slow
        self.signal = signal
        # 상태 저장: {(code, tf): {"ema_fast": float, "ema_slow": float, "ema_signal": float, ...}}
        self._states: Dict[Tuple[str, str], Dict[str, float]] = {}

    # 외부에 노출되는 단 하나의 진입점 -----------------------------
    def apply_rows(self, code: str, tf: str, rows: List[Dict[str, Any]], need: int = 120) -> None:
        """
        rows(원시 분봉 배열)를 받아 내부에서 DataFrame 변환 → MACD 계산 → series 직렬화 → 신호 emit까지 수행.
        """
        df = self._rows_to_df_minute(rows)
        if df.empty or "close" not in df.columns:
            return

        # 과거로 초기화 + full 계산
        state, macd_df = self._init_state_from_history(df["close"])
        self._states[(code, tf)] = state

        # 최근 need개 직렬화
        payload = self._to_series_payload(macd_df.tail(need))
        macd_bus.macd_series_ready.emit({"code": code, "tf": tf, "series": payload})

    # 내부 유틸들 -----------------------------------------------
    def _rows_to_df_minute(self, rows: List[Dict[str, Any]]) -> pd.DataFrame:
        """
        rows 예시: [{"ts":"2025-08-27T14:30:00+09:00","open":..., "high":..., "low":..., "close":..., "vol":...}, ...]
        API 포맷에 맞게 여기서만 수정하면 외부 호출부는 바꿀 필요 없음.
        """
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        # 타임스탬프 표준화
        if "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"])
            df = df.sort_values("ts").reset_index(drop=True)
            df = df.set_index("ts")
        # 필요한 컬럼만 남기기(필요 시 조정)
        keep = [c for c in ["open", "high", "low", "close", "vol"] if c in df.columns]
        return df[keep]

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
        """
        UI/다이얼로그가 바로 쓰기 쉬운 형태로 직렬화
        [{"t":"2025-08-27T14:35:00+09:00","macd":..., "signal":..., "hist":...}, ...]
        """
        out = []
        for ts, row in df.iterrows():
            t = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            out.append({"t": t, "macd": float(row["macd"]), "signal": float(row["signal"]), "hist": float(row["hist"])})
        return out


# 전역(혹은 DI로 주입)
calculator = MacdCalculator()
