# detail_getter_from_cache.py
from __future__ import annotations
import pandas as pd
from .candle_cache import CandleCache

class DetailGetterFromCache:
    """ExitEntryMonitor 가 기대하는 get_bars(...)를 캐시에서 제공"""
    def __init__(self, cache: CandleCache):
        self.cache = cache

    async def get_bars(self, code: str, interval: str, count: int) -> pd.DataFrame:
        # 비동기 시그니처를 유지하지만 실제 동작은 동기
        return self.cache.get_df(code, interval, count)
