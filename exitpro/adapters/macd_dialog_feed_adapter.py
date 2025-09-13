# exitpro/macd_dialog_feed_adapter.py
from __future__ import annotations
import logging
from typing import Optional, Dict, Tuple
import pandas as pd

logger = logging.getLogger(__name__)

class MacdDialogFeedAdapter:
    """
    macd_bus / bridge 로부터 들어오는 MACD 시리즈(payload)를 받아
    (symbol, timeframe)별 최신 {"ts","macd","signal","hist"} 를 캐시로 제공.

    사용:
      adapter = MacdDialogFeedAdapter()
      bridge.macd_series_ready.connect(adapter.on_bus_series_ready)  # 또는 macd_bus에 직접 연결
      ...
      info = adapter.get_latest("000660","30m") -> dict | None
    """

    def __init__(self, tz: str = "Asia/Seoul"):
        self._latest: Dict[Tuple[str, str], Dict[str, object]] = {}
        self.tz = tz

    # ── 정규화 ───────────────────────────────────
    @staticmethod
    def _norm_symbol(sym: str) -> str:
        s = str(sym).strip()
        if ":" in s:
            s = s.split(":", 1)[-1]
        if s.isdigit():
            s = s.zfill(6)
        return s

    @staticmethod
    def _norm_tf(tf: str) -> str:
        s = str(tf).strip().lower()
        if s in ("30", "30m", "30min", "m30"):
            return "30m"
        if s in ("5", "5m", "5min", "m5"):
            return "5m"
        if s in ("1d", "d", "day"):
            return "1d"
        return s

    def _to_ts(self, ts_obj) -> pd.Timestamp:
        if isinstance(ts_obj, pd.Timestamp):
            ts = ts_obj
        else:
            ts = pd.Timestamp(ts_obj)
        if ts.tzinfo is None:
            ts = ts.tz_localize(self.tz)
        return ts

    # ── 브리지/버스 핸들러 ────────────────────────
    def on_bus_series_ready(self, payload: dict) -> None:
        """
        payload 예:
          {"code": "005930", "tf": "30m", "series": [{"t": "...", "macd": 1.2, "signal": 0.9, "hist": 0.3}, ...]}
        """
        try:
            code = self._norm_symbol(payload.get("code", ""))
            tf = self._norm_tf(payload.get("tf") or payload.get("timeframe") or "")
            series = payload.get("series") or []
            if not code or not tf or not series:
                logger.debug(f"[MACD-ADAPTER] skip payload: code={code}, tf={tf}, series_len={len(series)}")
                return

            last = series[-1]
            macd = float(last.get("macd", 0.0))
            signal = float(last.get("signal", 0.0))
            hist = float(last.get("hist", macd - signal))
            ts = self._to_ts(last.get("t") or last.get("ts") or pd.Timestamp.now())

            key = (code, tf)
            self._latest[key] = {"ts": ts, "macd": macd, "signal": signal, "hist": hist}
            logger.debug(f"[MACD-ADAPTER] upsert {key} hist={hist:.4f} ts={ts}")

        except Exception as e:
            logger.exception(f"[MACD-ADAPTER] on_bus_series_ready error: {e}")

    # ── 모니터에서 조회 ──────────────────────────
    def get_latest(self, symbol: str, timeframe: str) -> Optional[dict]:
        key = (self._norm_symbol(symbol), self._norm_tf(timeframe))
        info = self._latest.get(key)
        logger.debug(f"[MACD-ADAPTER] get_latest {key} -> {info}")
        return info
