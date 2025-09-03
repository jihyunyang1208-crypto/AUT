# core/macd_calculator.py
from __future__ import annotations
from typing import List, Dict, Any, Tuple
from datetime import timezone, timedelta
import pandas as pd
from PySide6.QtCore import QObject, Signal
import logging
file_handler = logging.FileHandler('log.txt', encoding='utf-8')

KST = timezone(timedelta(hours=9))
logger = logging.getLogger(__name__)

class MacdBus(QObject):
    """
    계산 결과를 UI/다이얼로그로 전달하는 공용 버스.
    payload 예:
      {"code": "014940", "tf": "5m"/"30m"/"1d",
       "series": [{"t": iso8601, "macd": float, "signal": float, "hist": float}, ...]}
    """
    macd_series_ready = Signal(dict)


macd_bus = MacdBus()  # 싱글톤처럼 사용




class MacdCalculator:
    """
    - 최초엔 apply_rows_full(...)로 전체 계산
    - 이후엔 apply_append(...)로 새 봉만 증분 갱신
    상태 저장: {(code, tf): {"last_ts": pd.Timestamp, "ema_fast": float, "ema_slow": float, "ema_signal": float}}
    """
    """코드+타임프레임별 상태를 내부에 유지하여 MACD(full 또는 증분) 계산"""
    def __init__(self, fast=12, slow=26, signal=9):
        self.fast = fast
        self.slow = slow
        self.signal = signal
        # (선택) 상태 저장: {(code, tf): {"ema_fast": float, ...}} — 현재는 full 계산 위주로 사용
        self._states: Dict[Tuple[str, str], Dict[str, float]] = {}

        self._alpha_fast = 2.0 / (self.fast + 1)
        self._alpha_slow = 2.0 / (self.slow + 1)
        self._alpha_sig  = 2.0 / (self.signal + 1)

    # ---------------- 외부 엔트리 포인트 ----------------
    # ---------- public: FULL ----------
    def apply_rows_full(self, code: str, tf: str, rows: List[Dict[str, Any]], need: int = 120) -> None:
        tf = str(tf).lower()
        if tf not in ("5m", "30m", "1d"):
            return

        logger.debug("[MACD] FULL start: code=%s tf=%s rows_in=%d need=%d", code, tf, len(rows or []), need)
        df = self._rows_to_df(rows, tf)
        if df.empty or "close" not in df.columns:
            logger.debug("[MACD] FULL abort: empty/invalid df")
            return

        # --- full calc via pandas EWM ---
        ema_fast = df["close"].ewm(span=self.fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=self.slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=self.signal, adjust=False).mean()
        hist = macd_line - signal_line

        macd_df = pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": hist})

        # --- save state (last point) ---
        last_ts = macd_df.index[-1]
        state = {
            "last_ts": last_ts,
            "ema_fast": float(ema_fast.iloc[-1]),
            "ema_slow": float(ema_slow.iloc[-1]),
            "ema_signal": float(signal_line.iloc[-1]),
        }
        self._states[(code, tf)] = state

        payload = self._to_series_payload(macd_df.tail(need))
        logger.debug("[MACD] FULL emit: code=%s tf=%s points=%d last_t=%s",
                     code, tf, len(payload), (payload[-1]["t"] if payload else None))
        macd_bus.macd_series_ready.emit({"code": code, "tf": tf, "mode": "full", "series": payload})

    # ---------- public: APPEND ----------
    def apply_append(self, code: str, tf: str, rows: List[Dict[str, Any]]) -> None:
        """
        새로 들어온 분봉/일봉 rows를 받아, 마지막 시각 이후의 캔들만 증분 계산.
        - state 없으면 FULL로 전환(보수적)
        - 여러 캔들이 한 번에 들어오면 시간 오름차순으로 순차 갱신
        - 모든 업데이트된 데이터를 emit하여 UI의 sparkline 갱신을 보장
        """
        tf = str(tf).lower()
        if tf not in ("5m", "30m", "1d"):
            return

        key = (code, tf)
        state = self._states.get(key)
        df = self._rows_to_df(rows, tf)
        if df.empty or "close" not in df.columns:
            return

        if not state:
            # 시드가 없으면 full 계산로 변경
            logger.debug("[MACD] APPEND fallback to FULL (no state): code=%s tf=%s", code, tf)
            self.apply_rows_full(code, tf, rows, need=120)
            return

        last_ts = state.get("last_ts")
        # last_ts 이후의 새 캔들만
        inc = df[df.index > last_ts]
        if inc.empty:
            return

        ema_fast = state["ema_fast"]
        ema_slow = state["ema_slow"]
        ema_sig  = state["ema_signal"]

        current_payload = self._to_series_payload(self._rows_to_df(code, tf))

        for ts, row in inc.iterrows():
            c = float(row["close"])
            # EMA 증분
            ema_fast = ema_fast + self._alpha_fast * (c - ema_fast)
            ema_slow = ema_slow + self._alpha_slow * (c - ema_slow)
            macd = ema_fast - ema_slow
            ema_sig = ema_sig + self._alpha_sig * (macd - ema_sig)
            hist = macd - ema_sig

            current_payload.append({
                "t": ts.isoformat(),
                "macd": float(macd),
                "signal": float(ema_sig),
                "hist": float(hist),
            })
            last_ts = ts

        # 상태 갱신
        self._states[key] = {
            "last_ts": last_ts,
            "ema_fast": float(ema_fast),
            "ema_slow": float(ema_slow),
            "ema_signal": float(ema_sig),
        }

        # 마지막 1개만 append emit
        logger.debug("[MACD] APPEND (full-series) emit: code=%s tf=%s points=%d last_t=%s",
                    code, tf, len(current_payload), current_payload[-1]["t"])
        macd_bus.macd_series_ready.emit({"code": code, "tf": tf, "mode": "full", "series": current_payload})

    
    # 구버전 코드(혹시 남아있다면) 호환용 별칭
    def apply_rows(self, code: str, tf: str, rows: List[Dict[str, Any]], need: int = 120) -> None:
        # 기존 호출이 있다면 FULL로 동작
        self.apply_rows_full(code, tf, rows, need)

    # ---------------- 내부 유틸 ----------------
    def _rows_to_df(self, rows: List[Dict[str, Any]], tf: str) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()

        recs = []
        for r in rows:
            ts = r.get("ts")
            if not ts:
                # dt/date + tm(time) 조합을 ISO로 변환
                d = str(r.get("base_dt") or r.get("trd_dd") or r.get("dt") or r.get("date") or "")
                t = str(r.get("trd_tm") or r.get("cntr_tm") or r.get("time") or r.get("tm") or "")
                if len(d) == 8 and d.isdigit():
                    if len(t) == 6 and t.isdigit():
                        ts = pd.to_datetime(d + t, format="%Y%m%d%H%M%S", errors="coerce")
                    else:
                        # 일봉 등 시간 미제공 → 09:00 부여
                        ts = pd.to_datetime(d + "090000", format="%Y%m%d%H%M%S", errors="coerce")
                else:
                    if len(t) == 14 and t.isdigit():
                        ts = pd.to_datetime(t, format="%Y%m%d%H%M%S", errors="coerce")
                    else:
                        ts = pd.to_datetime(r.get("ts"), errors="coerce")
            else:
                ts = pd.to_datetime(ts, errors="coerce")

            if ts is None or pd.isna(ts):
                continue

            recs.append({
                "ts": ts,
                "open":  _to_float(r.get("open")  or r.get("open_pric")),
                "high":  _to_float(r.get("high")  or r.get("high_pric")),
                "low":   _to_float(r.get("low")   or r.get("low_pric")),
                "close": _to_float(r.get("close") or r.get("close_pric") or r.get("cur_prc")),
                "vol":   _to_float(r.get("vol")   or r.get("trde_qty")   or r.get("volume") or r.get("v")),
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