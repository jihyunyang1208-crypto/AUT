# core/macd_calculator.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from collections import defaultdict, deque
from typing import Dict, Tuple, List, Any
import threading
import pandas as pd
from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# Bus (기존 그대로)
# ─────────────────────────────────────────────────────────
class MacdBus(QObject):
    # payload: {"code": str, "tf": "5m"/"30m"/"1d", "mode": "full"|"append", "series": [ {t,macd,signal,hist}, ... ]}
    macd_series_ready = Signal(dict)

macd_bus = MacdBus()


# ─────────────────────────────────────────────────────────
# MACD 캐시: Calculator 내부 동파일에 분리 클래스
# ─────────────────────────────────────────────────────────
class MacdCache:
    """
    (code, tf)별 최근 포인트를 보관. 견고한 시간정렬/중복제거.
    저장: save_series(code, tf, series)
    조회: get_points(code, tf, n)
    """
    def __init__(self, max_points: int = 400):
        self._buf: Dict[Tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=max_points))
        self._lock = threading.RLock()
        self.tz = "Asia/Seoul"

    @staticmethod
    def _norm_code(code: str) -> str:
        s = str(code).strip()
        if ":" in s:
            s = s.split(":", 1)[-1]
        if s.isdigit():
            s = s.zfill(6)
        return s

    @staticmethod
    def _norm_tf(tf: str) -> str:
        s = str(tf).strip().lower()
        if s in {"5", "5m", "5min", "m5"}:
            return "5m"
        if s in {"30", "30m", "30min", "m30"}:
            return "30m"
        if s in {"1d", "d", "day"}:
            return "1d"
        return s

    def _to_ts(self, t) -> pd.Timestamp:
        if isinstance(t, pd.Timestamp):
            ts = t
        else:
            # 문자열/숫자/iso 모두 pandas에 위임
            ts = pd.Timestamp(t)
        if ts.tzinfo is None:
            ts = ts.tz_localize(self.tz)
        return ts

    def save_series(self, code: str, tf: str, series: List[Dict[str, Any]]) -> None:
        """
        series: [{"t": <ts/str>, "macd": float, "signal": float, "hist": float}, ...]
        시간 역행 무시, 동일 ts는 덮어쓰기.
        """
        key = (self._norm_code(code), self._norm_tf(tf))
        with self._lock:
            buf = self._buf[key]
            for p in series or []:
                try:
                    ts = self._to_ts(p.get("t") or p.get("ts"))
                    macd = float(p.get("macd"))
                    sig  = float(p.get("signal"))
                    hist = float(p.get("hist", macd - sig))
                except Exception:
                    continue

                if buf:
                    last_ts = buf[-1]["ts"]
                    if ts < last_ts:
                        # 역행 데이터는 건너뜀
                        continue
                    if ts == last_ts:
                        buf[-1] = {"ts": ts, "macd": macd, "signal": sig, "hist": hist}
                        continue
                buf.append({"ts": ts, "macd": macd, "signal": sig, "hist": hist})

    def get_points(self, code: str, tf: str, n: int = 1) -> List[dict]:
        """
        최근 n개 포인트 반환. (n=1이면 latest 대체)
        반환: [{"ts": Timestamp(tz-aware), "macd":..., "signal":..., "hist":...}, ...] (시간 오름차순)
        """
        key = (self._norm_code(code), self._norm_tf(tf))
        with self._lock:
            buf = self._buf.get(key)
            if not buf:
                return []
            return list(buf)[-max(1, n):]


# 전역 캐시(싱글톤)
macd_cache = MacdCache(max_points=400)


# ─────────────────────────────────────────────────────────
# MACD 계산기 (기존 구조 유지하되 캐시 저장 + get_points 노출)
# ─────────────────────────────────────────────────────────
@dataclass
class _State:
    last_ts: pd.Timestamp
    ema_fast: float
    ema_slow: float
    ema_signal: float


class MacdCalculator:
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast = fast
        self.slow = slow
        self.signal = signal

        # append용 α
        self._alpha_fast = 2.0 / (fast + 1)
        self._alpha_slow = 2.0 / (slow + 1)
        self._alpha_sig  = 2.0 / (signal + 1)

        self._states: Dict[Tuple[str, str], Dict[str, Any]] = {}

    # ------------- 내부 유틸 -------------
    @staticmethod
    def _rows_to_df(rows: List[Dict[str, Any]], tf: str) -> pd.DataFrame:
        """
        rows: list[dict] with time + close
        time key: "t"|"ts"|"trd_tm"|"cntr_tm"
        close key: "close"|"c"|"close_pric"|"cur_prc"
        """
        if not rows:
            return pd.DataFrame()
        # 시간/종가 추출
        def _to_ts(r):
            t = r.get("t") or r.get("ts") or r.get("trd_tm") or r.get("cntr_tm")
            if t is None:
                return pd.NaT
            if isinstance(t, pd.Timestamp):
                ts = t
            else:
                s = str(t)
                # "YYYYMMDDHHMMSS" 형태 고려
                if len(s) == 14 and s.isdigit():
                    ts = pd.to_datetime(s, format="%Y%m%d%H%M%S", errors="coerce")
                else:
                    ts = pd.to_datetime(s, errors="coerce")
            if ts is pd.NaT:
                return pd.NaT
            if ts.tzinfo is None:
                ts = ts.tz_localize("Asia/Seoul")
            return ts

        def _to_close(r):
            cand = r.get("close", None)
            cand = r.get("c", cand)
            cand = r.get("close_pric", cand)
            cand = r.get("cur_prc", cand)
            try:
                return float(str(cand).replace(",", "")) if cand is not None else float("nan")
            except Exception:
                return float("nan")

        df = pd.DataFrame({
            "ts": [ _to_ts(r) for r in rows ],
            "close": [ _to_close(r) for r in rows ],
        }).dropna(subset=["ts"]).sort_values("ts").set_index("ts")

        return df

    @staticmethod
    def _to_series_payload(macd_df: pd.DataFrame) -> List[dict]:
        """macd_df: index=ts, cols=[macd, signal, hist]"""
        out: List[dict] = []
        for ts, row in macd_df.iterrows():
            out.append({
                "t": ts.isoformat(),
                "macd": float(row["macd"]),
                "signal": float(row["signal"]),
                "hist": float(row["hist"]),
            })
        return out

    # ------------- public: FULL -------------
    def apply_rows_full(self, code: str, tf: str, rows: List[Dict[str, Any]], need: int = 120) -> None:
        tf = str(tf).lower()
        if tf not in ("5m", "30m", "1d"):
            return

        logger.debug("[MACD] FULL start: code=%s tf=%s rows_in=%d need=%d",
                     code, tf, len(rows or []), need)
        df = self._rows_to_df(rows, tf)

        if df.empty or "close" not in df.columns:
            logger.debug("[MACD] FULL abort: empty/invalid df")
            return

        # full calc via pandas EWM
        ema_fast = df["close"].ewm(span=self.fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=self.slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=self.signal, adjust=False).mean()
        hist = macd_line - signal_line

        macd_df = pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": hist})

        # save state (last point)
        last_ts = macd_df.index[-1]
        self._states[(code, tf)] = {
            "last_ts": last_ts,
            "ema_fast": float(ema_fast.iloc[-1]),
            "ema_slow": float(ema_slow.iloc[-1]),
            "ema_signal": float(signal_line.iloc[-1]),
        }

        payload = self._to_series_payload(macd_df.tail(need))
        # 🔹 캐시에 저장
        try:
            macd_cache.save_series(code, tf, payload)
        except Exception:
            logger.exception("[MACD] cache save failed (FULL) code=%s tf=%s", code, tf)

        logger.debug("[MACD] FULL emit: code=%s tf=%s points=%d last_t=%s",
                     code, tf, len(payload), (payload[-1]["t"] if payload else None))
        macd_bus.macd_series_ready.emit({"code": code, "tf": tf, "mode": "full", "series": payload})

    # ------------- public: APPEND -------------
    def apply_append(self, code: str, tf: str, rows: List[Dict[str, Any]]) -> None:
        """
        새 rows로 마지막 시각 이후 캔들만 증분 계산.
        - state 없으면 FULL로 회귀
        - 여러 캔들 들어오면 순차 갱신
        - 캐시에 연속 저장 후 bus로 emit
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
            logger.debug("[MACD] APPEND fallback to FULL (no state): code=%s tf=%s", code, tf)
            self.apply_rows_full(code, tf, rows, need=120)
            return

        last_ts = state["last_ts"]
        inc = df[df.index > last_ts]
        if inc.empty:
            return

        ema_fast = float(state["ema_fast"])
        ema_slow = float(state["ema_slow"])
        ema_sig  = float(state["ema_signal"])

        appended_payload: List[dict] = []

        for ts, row in inc.iterrows():
            c = float(row["close"])
            # EMA 증분
            ema_fast = ema_fast + self._alpha_fast * (c - ema_fast)
            ema_slow = ema_slow + self._alpha_slow * (c - ema_slow)
            macd = ema_fast - ema_slow
            ema_sig = ema_sig + self._alpha_sig * (macd - ema_sig)
            hist = macd - ema_sig

            appended_payload.append({
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

        if appended_payload:
            # 🔹 캐시에 저장(증분)
            try:
                macd_cache.save_series(code, tf, appended_payload)
            except Exception:
                logger.exception("[MACD] cache save failed (APPEND) code=%s tf=%s", code, tf)

            logger.debug("[MACD] APPEND emit: code=%s tf=%s points=%d last_t=%s",
                         code, tf, len(appended_payload), appended_payload[-1]["t"])
            macd_bus.macd_series_ready.emit({"code": code, "tf": tf, "mode": "append", "series": appended_payload})


# 전역 계산기
calculator = MacdCalculator()


# ─────────────────────────────────────────────────────────
# 외부 공개 조회 API (단일화)
# ─────────────────────────────────────────────────────────
def get_points(code: str, timeframe: str, n: int = 1) -> List[dict]:
    """
    최근 n개 MACD 포인트 반환. n=1로 latest 대체.
    MacdDialog, ExitEntryMonitor 모두 이걸 사용.
    """
    return macd_cache.get_points(code, timeframe, n)
