# candle_cache.py
from __future__ import annotations
import logging
from collections import defaultdict, deque
from typing import Dict, Tuple, List
import pandas as pd

logger = logging.getLogger(__name__)

class CandleCache:
    def __init__(self, maxlen: int = 3000, tz: str = "Asia/Seoul"):
        self._buf: Dict[Tuple[str,str], deque] = defaultdict(lambda: deque(maxlen=maxlen))
        self.tz = tz

    # ---- 키 정규화 ----
    @staticmethod
    def _norm_symbol(s: str) -> str:
        s = str(s).strip()
        if ":" in s: s = s.split(":",1)[-1]
        if s.isdigit(): s = s.zfill(6)
        return s
    @staticmethod
    def _norm_tf(tf: str) -> str:
        s = str(tf).strip().lower()
        if s in ("5","5m","5min","m5"): return "5m"
        if s in ("30","30m","30min","m30"): return "30m"
        if s in ("1d","d","day"): return "1d"
        return s
    def _key(self, code: str, tf: str): return (self._norm_symbol(code), self._norm_tf(tf))

    # ---- upsert (rows: list[dict]) ----
    def upsert_rows(self, code: str, tf: str, rows: List[dict]) -> None:
        key = self._key(code, tf)
        buf = self._buf[key]
        before = len(buf)

        for r in rows or []:
            # 시간 파싱(필수)
            t = r.get("t") or r.get("ts") or r.get("trd_tm") or r.get("cntr_tm")
            if not t:
                continue
            if isinstance(t, pd.Timestamp):
                ts = t
            else:
                s = str(t)
                if len(s) == 14 and s.isdigit():
                    ts = pd.to_datetime(s, format="%Y%m%d%H%M%S", errors="coerce")
                else:
                    ts = pd.to_datetime(s, errors="coerce")
            if ts is pd.NaT:
                continue
            if ts.tzinfo is None:
                ts = ts.tz_localize(self.tz)

            # 값 매핑
            rec = {
                "ts": ts,
                "Open":  _to_num(r.get("open")  or r.get("open_pric")  or r.get("o")),
                "High":  _to_num(r.get("high")  or r.get("high_pric")  or r.get("h")),
                "Low":   _to_num(r.get("low")   or r.get("low_pric")   or r.get("l")),
                "Close": _to_num(r.get("close") or r.get("close_pric") or r.get("cur_prc") or r.get("c")),
                "Volume":_to_num(r.get("vol")   or r.get("trde_qty")   or r.get("volume") or r.get("v")),
            }
            buf.append(rec)

        # 시간순/중복 제거
        if buf:
            df = pd.DataFrame(list(buf)).dropna(subset=["ts"]).sort_values("ts")
            df = df.drop_duplicates("ts", keep="last")
            self._buf[key] = deque(df.to_dict("records"), maxlen=buf.maxlen)

        after = len(self._buf[key])
        logger.debug(f"[CandleCache] upsert key={key} size {before}→{after}")

    # ---- DataFrame 읽기 ----
    def get_df(self, code: str, tf: str, count: int = 200) -> pd.DataFrame:
        key = self._key(code, tf)
        buf = self._buf.get(key)
        n = len(buf) if buf else 0
        if not buf or n == 0:
            logger.debug(f"[CandleCache] get_df key={key} empty")
            return pd.DataFrame(columns=["Open","High","Low","Close","Volume"])
        tail = list(buf)[-count:]
        df = pd.DataFrame(tail).set_index("ts")
        return df[["Open","High","Low","Close","Volume"]]

def _to_num(x):
    if x is None or x == "": return float("nan")
    try:
        s = str(x).replace(",","").strip()
        neg = s.startswith("-")
        s = s.lstrip("+-")
        v = float(s)
        return -v if neg else v
    except Exception:
        return float("nan")
