# exit_monitor.py
import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Protocol

import pandas as pd

# ──────────────────────────────
# Logger
# ──────────────────────────────
logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        encoding="utf-8",   
    )

# ========== 인터페이스(존재 클래스 재사용 전제) ==========

class DetailInformationGetter(Protocol):
    async def get_bars(self, code: str, interval: str, count: int) -> pd.DataFrame:
        """
        반환: index = tz-aware datetime(Asia/Seoul 권장)
              columns = ['Open','High','Low','Close','Volume']
        """
        ...

class IMacdFeed(Protocol):
    def get_latest(self, symbol: str, timeframe: str) -> Optional[dict]:
        """
        반환 예:
        {"ts": pd.Timestamp, "macd": float, "signal": float, "hist": float}
        timeframe: "30m" 등
        """
        ...

# ========== 설정 & 모델 ==========

@dataclass
class TradeSettings:
    master_enable: bool = True
    auto_buy: bool = False
    auto_sell: bool = True

@dataclass
class TradeSignal:
    side: str           # "BUY" | "SELL"
    symbol: str
    ts: pd.Timestamp    # 신호가 발생한 5분봉 종료시각
    price: float        # 기준가격(보통 종가)
    reason: str         # 신호 사유 텍스트

# ========== 룰 ==========

class BuyRules:
    @staticmethod
    def buy_if_5m_break_prev_bear_high(df5: pd.DataFrame) -> pd.Series:
        """
        예시 룰:
        - 1봉 전: 음봉
        - 현재봉: 양봉
        - 현재봉 고가가 직전(음봉) 고가를 돌파
        """
        prev = df5.shift(1)
        cond_bear = prev["Close"] < prev["Open"]
        cond_bull = df5["Close"] > df5["Open"]
        cond_break = df5["High"] > prev["High"]
        cond = cond_bear & cond_bull & cond_break
        if len(cond) > 0:
            cond.iloc[0] = False
        return cond

class SellRules:
    @staticmethod
    def sell_if_close_below_prev_open(df5: pd.DataFrame) -> pd.Series:
        """
        매도 조건:
        - 현재 5분봉 종가 < 직전 5분봉 시가
        """
        cond = df5["Close"] < df5["Open"].shift(1)
        if len(cond) > 0:
            cond.iloc[0] = False
        return cond

class TimeRules:
    @staticmethod
    def is_5m_bar_close_window(now_kst: pd.Timestamp) -> bool:
        """
        5분봉 마감 근사 판단:
        - 분 % 5 == 0 이고, 5~30초 사이(수신/체결 지연 버퍼)
        필요 시 운영 환경에 맞춰 조정
        """
        return (now_kst.minute % 5 == 0) and (5 <= now_kst.second <= 30)

# ========== 모니터러 본체 ==========

class ExitEntryMonitor:
    """
    - 5분봉 종가 기준으로 매수/매도 신호 판단
    - 선택적으로 30분 MACD 히스토그램 >= 0 필터 사용(재계산 없음, MacdDialog/Calculator 값을 그대로 사용)
    - 동일 봉 중복 트리거 방지
    - 봉 마감 구간에서만 평가
    """
    def __init__(
        self,
        detail_getter: DetailInformationGetter,
        macd_feed: IMacdFeed,
        symbols: List[str],
        settings: TradeSettings,
        *,
        use_macd30_filter: bool = False,
        macd30_timeframe: str = "30m",
        macd30_max_age_sec: int = 1800,  # 30분봉 신선도 권장값
        tz: str = "Asia/Seoul",
        poll_interval_sec: int = 20,
        on_signal: Optional[Callable[[TradeSignal], None]] = None,
    ):
        self.detail_getter = detail_getter
        self.macd_feed = macd_feed
        self.symbols = symbols
        self.settings = settings

        self.use_macd30_filter = use_macd30_filter
        self.macd30_timeframe = macd30_timeframe
        self.macd30_max_age_sec = macd30_max_age_sec

        self.tz = tz
        self.poll_interval_sec = poll_interval_sec
        self.on_signal = on_signal or (lambda sig: logger.info(f"[SIGNAL] {sig}"))

        # 중복 트리거 방지: (symbol, side) → 마지막 트리거된 봉 ts
        self._last_trig: Dict[tuple[str, str], pd.Timestamp] = {}

        logger.info(
            f"[ExitEntryMonitor] 초기화: symbols={symbols}, "
            f"auto_buy={settings.auto_buy}, auto_sell={settings.auto_sell}, "
            f"use_macd30_filter={use_macd30_filter}, macd30_max_age_sec={macd30_max_age_sec}"
        )

    # -------- 내부 유틸 --------

    async def _get_5m(self, symbol: str, count: int = 200) -> Optional[pd.DataFrame]:
        logger.debug(f"[ExitEntryMonitor] 5m 데이터 요청: {symbol} (count={count})")
        df = await self.detail_getter.get_bars(code=symbol, interval="5m", count=count)
        if df is None or df.empty or len(df) < 2:
            logger.warning(f"[ExitEntryMonitor] 5m 데이터 부족/없음: {symbol}")
            return None
        return df

    def _macd30_pass(self, symbol: str, ref_ts: pd.Timestamp) -> bool:
        """
        30m MACD 최신값으로 필터링:
        - hist >= 0 이어야 통과
        - 신선도(age_sec) <= macd30_max_age_sec
        """
        if not self.use_macd30_filter:
            return True

        info = self.macd_feed.get_latest(symbol, self.macd30_timeframe)
        if not info:
            logger.debug(f"[ExitEntryMonitor] {symbol} NO MACD30 → failed filtering")
            return False

        hist = info.get("hist")
        ts: pd.Timestamp = info.get("ts")
        if hist is None or ts is None:
            logger.debug(f"[ExitEntryMonitor] {symbol} MACD30 불완전(hist/ts None) → failed")
            return False

        try:
            rts = ref_ts if ref_ts.tzinfo else ref_ts.tz_localize(self.tz)
            tts = ts if ts.tzinfo else ts.tz_localize(self.tz)
            age_sec = (rts - tts).total_seconds()
        except Exception as e:
            logger.error(f"[ExitEntryMonitor] {symbol} MACD30 age 계산 오류: {e}")
            return False

        logger.debug(f"[ExitEntryMonitor] {symbol} MACD30 hist={float(hist):.2f} age={age_sec:.0f}s")
        if age_sec > self.macd30_max_age_sec:
            logger.debug(f"[ExitEntryMonitor] {symbol} MACD30 too old ({age_sec:.0f}s > {self.macd30_max_age_sec}s) → failed")
            return False

        return float(hist) >= 0.0

    def _emit(self, side: str, symbol: str, ts: pd.Timestamp, price: float, reason: str):
        key = (symbol, side)
        if self._last_trig.get(key) == ts:
            logger.debug(f"[ExitEntryMonitor] {symbol} {side} 신호 중복(ts={ts}) → 무시")
            return
        self._last_trig[key] = ts
        logger.info(f"[ExitEntryMonitor] 📣 신호 발생 {side} {symbol} {price:.2f} @ {ts} | {reason}")
        self.on_signal(TradeSignal(side, symbol, ts, price, reason))

    # -------- 심볼별 평가 --------

    async def _check_symbol(self, symbol: str):
        df5 = await self._get_5m(symbol)
        if df5 is None:
            return

        ref_ts = df5.index[-1]
        last_close = float(df5["Close"].iloc[-1])
        prev_open  = float(df5["Open"].iloc[-2])

        # (옵션) 30분 MACD 필터
        if self.use_macd30_filter and not self._macd30_pass(symbol, ref_ts):
            return

        # ✅ 매도: 현재 5분봉 종가 < 직전 5분봉 시가
        if self.settings.master_enable and self.settings.auto_sell:
            if last_close < prev_open:
                reason = f"SELL: Close<{prev_open:.2f} (prev open)" + (" + MACD30(hist>=0)" if self.use_macd30_filter else "")
                self._emit("SELL", symbol, ref_ts, last_close, reason)

        # (선택) 예시 매수 룰
        if self.settings.master_enable and self.settings.auto_buy:
            buy = BuyRules.buy_if_5m_break_prev_bear_high(df5).iloc[-1]
            if bool(buy) and self._macd30_pass(symbol, ref_ts):
                reason = "BUY: Bull breaks prev bear high" + (" + MACD30(hist>=0)" if self.use_macd30_filter else "")
                self._emit("BUY", symbol, ref_ts, last_close, reason)

    # -------- 루프 시작 --------

    async def start(self):
        logger.info("[ExitEntryMonitor] 모니터링 시작")
        while True:
            try:
                now_kst = pd.Timestamp.now(tz=self.tz)
                if TimeRules.is_5m_bar_close_window(now_kst):
                    logger.debug(f"[ExitEntryMonitor] 5분봉 마감 구간 @ {now_kst}")
                    await asyncio.gather(*[self._check_symbol(s) for s in self.symbols])
            except Exception as e:
                logger.exception(f"[ExitEntryMonitor] 루프 오류: {e}")
            await asyncio.sleep(self.poll_interval_sec)
