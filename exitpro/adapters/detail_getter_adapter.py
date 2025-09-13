# exitpro/detail_getter_adapter.py
from __future__ import annotations
import asyncio
from typing import Optional, Callable, Awaitable, Dict
import pandas as pd

class DetailInformationGetterAdapter:
    """
    ExitEntryMonitor 가 기대하는 시그니처로 표준화:
      async def get_bars(code: str, interval: str, count: int) -> pd.DataFrame

    지원 call_style:
      - "async_get_bars" : real_getter.get_bars(...) 가 async 이고 (code, interval, count) 그대로 받는 경우
      - "sync_get_bars"  : real_getter.get_bars(...) 가 동기인 경우
      - "sync_ka10081"   : real_getter.get_ka10081(code, unit, count) 스타일(분/일봉 등)
      - "sync_ka10080"   : real_getter.fetch_minute_chart_ka10080(code, tic_scope, need) 스타일(분봉 rows)
      - "custom"         : 직접 비동기 fetcher 제공

    반환 DataFrame 은 index=tz-aware(Asia/Seoul 권장),
    columns = ['Open','High','Low','Close','Volume'] 로 표준화합니다.
    """

    def __init__(
        self,
        real_getter: object,
        *,
        call_style: str = "async_get_bars",
        interval_mapper: Optional[Callable[[str], str | int]] = None,
        # custom 스타일일 때: async (code, interval, count) -> DataFrame
        custom_fetcher: Optional[Callable[[str, str, int], Awaitable[pd.DataFrame]]] = None,
        # rows→DF 변환 시 옵션
        rows_time_keys: tuple[str, ...] = ("t", "time", "datetime"),
        rows_colmap: Optional[Dict[str, str]] = None,  # {'o':'Open','h':'High','l':'Low','c':'Close','v':'Volume'}
        tz: str = "Asia/Seoul",
    ):
        self.real = real_getter
        self.call_style = call_style
        self.interval_mapper = interval_mapper or self._default_interval_mapper
        self.custom_fetcher = custom_fetcher
        self.rows_time_keys = rows_time_keys
        self.rows_colmap = rows_colmap or {"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"}
        self.tz = tz

        if self.call_style == "custom" and not self.custom_fetcher:
            raise ValueError("call_style='custom' 인 경우 custom_fetcher 를 제공하세요.")

    # 기본 매핑: '5m' → '5', '30m' → '30', '1d' → 'D'
    @staticmethod
    def _default_interval_mapper(interval: str) -> str:
        if not isinstance(interval, str):
            return str(interval)
        s = interval.strip().lower()
        if s.endswith("m"):
            return s[:-1]           # '5m' -> '5'
        if s.endswith("min"):
            return s[:-3]           # '30min' -> '30'
        if s in ("1d", "d", "day"):
            return "D"
        return interval

    async def get_bars(self, code: str, interval: str, count: int) -> pd.DataFrame:
        mapped = self.interval_mapper(interval)

        # 1) 동일 시그니처 (async)
        if self.call_style == "async_get_bars":
            df = await self.real.get_bars(code=code, interval=interval, count=count)
            return self._ensure_df(df)

        # 2) 동일 시그니처 (sync)
        if self.call_style == "sync_get_bars":
            def _call():
                return self.real.get_bars(code=code, interval=interval, count=count)
            df = await asyncio.to_thread(_call)
            return self._ensure_df(df)

        # 3) KA10081 류
        if self.call_style == "sync_ka10081":
            def _call():
                return self.real.get_ka10081(code=code, unit=mapped, count=count)
            df = await asyncio.to_thread(_call)
            return self._ensure_df(df)

        # 4) KA10080 류 (rows 구조 반환 → 표준 DF로 변환)
        if self.call_style == "sync_ka10080":
            def _call_rows():
                scope = int(mapped) if str(mapped).isdigit() else 5  # '5m' -> 5
                res = self.real.fetch_minute_chart_ka10080(code, tic_scope=scope, need=count)
                return (res or {}).get("rows", []) or []
            rows = await asyncio.to_thread(_call_rows)
            df = self._rows_to_df(rows)
            return self._ensure_df(df)

        # 5) custom
        if self.call_style == "custom":
            df = await self.custom_fetcher(code, interval, count)
            return self._ensure_df(df)

        raise ValueError(f"Unknown call_style: {self.call_style}")

    # rows(list[dict]) → 표준 DF
    def _rows_to_df(self, rows: list[dict]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        df = pd.DataFrame(rows).copy()

        # 1) 시간 컬럼 자동 탐지 (키움 KA10080 대응)
        time_candidates = list(self.rows_time_keys) + ["trd_tm", "cntr_tm"]
        tcol = next((k for k in time_candidates if k in df.columns), None)
        if not tcol:
            raise ValueError(
                f"시간 컬럼을 찾을 수 없습니다. 후보={time_candidates}, 실제={list(df.columns)}"
            )

        # 2) 컬럼 매핑: 사용자 매핑(rows_colmap) + 키움 필드 보강
        colmap = {
            **{"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"},
            **self.rows_colmap,
            "open_pric": "Open",
            "high_pric": "High",
            "low_pric": "Low",
            "cur_prc": "Close",
            "trde_qty": "Volume",
        }

        for src, dst in colmap.items():
            if src in df.columns:
                df[dst] = df[src]

        # 필수 컬럼 체크(최소한 Close는 있어야 함)
        keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        if "Close" not in keep:
            raise ValueError(f"가격 컬럼 매핑 실패. 입력 컬럼={list(df.columns)}")

        # 3) 시간 파싱
        raw_t = df[tcol].astype(str)
        ts = None
        try:
            # 14자리(YYYYMMDDHHMMSS)
            mask_num14 = raw_t.str.len() == 14
            ts_num14 = pd.to_datetime(raw_t[mask_num14], format="%Y%m%d%H%M%S", errors="coerce") if mask_num14.any() else None
            if ts_num14 is not None and not ts_num14.isna().all():
                ts = ts_num14
            if ts is None or (hasattr(ts, "isna") and ts.isna().all()):
                ts = pd.to_datetime(raw_t, errors="coerce")
        except Exception:
            ts = pd.to_datetime(raw_t, errors="coerce")

        if ts.isna().all():
            raise ValueError(f"시간 파싱 실패: 예시={raw_t.iloc[0]!r}")

        if ts.dt.tz is None:
            ts = ts.dt.tz_localize(self.tz)

        df.index = ts
        df = df[keep]

        # 4) 정렬/중복 제거/수치형 보정
        df = df[~df.index.duplicated(keep="last")].sort_index()
        for c in ["Open", "High", "Low", "Close", "Volume"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")

        return df

    def _ensure_df(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        expect_cols = {"Open", "High", "Low", "Close", "Volume"}
        missing = expect_cols - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame columns missing: {missing}")
        idx = df.index
        if not isinstance(idx, pd.DatetimeIndex):
            raise ValueError("DataFrame index must be DatetimeIndex")
        if idx.tz is None:
            df.index = idx.tz_localize(self.tz)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df
