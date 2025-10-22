#trade_pro/entry_exit_monitor.py
from __future__ import annotations

import asyncio
from asyncio import run_coroutine_threadsafe
import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Protocol, Tuple, Literal

import pandas as pd
import threading

# MACD 버스/조회기 (필요 시 의존성 주입으로 대체 가능)
from core.macd_calculator import get_points as _get_points
from core.macd_calculator import macd_bus
from risk_management.result_reader import TradingResultReader

logger = logging.getLogger(__name__)

# ============================================================================
# 유틸
# ============================================================================

def _code6(s: str) -> str:
    """심볼을 6자리 숫자 문자열로 정규화."""
    d = "".join(c for c in str(s) if c.isdigit())
    return d[-6:].zfill(6)


# ============================================================================
# 설정 & 모델
# ============================================================================

@dataclass
class TradeSignal:
    side: str           # "BUY" | "SELL"
    symbol: str
    ts: pd.Timestamp    # 신호 발생 시각
    price: float        # 기준가격(보통 종가)
    reason: str         # 신호 사유 텍스트
    source: str = "bar" # "bar" | "condition" | "manual" | "macd" 등
    condition_name: str = ""  # 조건검색식 이름
    extra: dict | None = None  # 추가정보 (optional)
    return_msg: str | None = None


@dataclass
class MonitorCustom:
    """고급 커스텀 설정 (모니터가 해석)"""
    enabled: bool = False                # 고급 커스텀 전체 스위치
    auto_buy: bool = True                # '매수' 체크
    auto_sell: bool = False              # '매도' 체크
    allow_intrabar_condition_triggers: bool = True  # 봉마감 전 즉시 트리거 허용

    # 🔵 추가: Pro 토글 (룰 주입 제거, 추세 전환만 사용)
    buy_pro: bool = False               # Buy-Pro: DOWN/HOLD → UP 전환 시
    sell_pro: bool = True               # Sell-Pro: UP/HOLD → DOWN 전환 시


# ============================================================================
# 룰 (내장 패턴; 외부 주입 룰 제거됨)
# ============================================================================

class BuyRules:
    @staticmethod
    def buy_if_5m_break_prev_bear_high(df5: pd.DataFrame) -> pd.Series:
        """
        [DEPRECATED] 추세 전환 로직으로 대체. 안전하게 False 시그널만 반환.
        """
        if df5 is None or df5.empty:
            return pd.Series(dtype=bool)
        return pd.Series([False] * len(df5), index=df5.index, dtype=bool)


class SellRules:
    @staticmethod
    def profit3_and_prev_candle_pattern(df5: pd.DataFrame, avg_buy: float) -> bool:
        """
        참고용 내부 패턴. 현재 버전에서는 사용하지 않음.
        """
        if df5 is None or len(df5) < 2 or pd.isna(avg_buy) or avg_buy <= 0:
            return False
        last_close = float(df5["Close"].iloc[-1])
        prev_open  = float(df5["Open"].iloc[-2])
        prev_close = float(df5["Close"].iloc[-2])
        if last_close < avg_buy * 1.03:
            return False
        if prev_close < prev_open:
            return last_close < prev_close
        elif prev_close > prev_open:
            return last_close < prev_open
        else:
            return False


class TimeRules:
    @staticmethod
    def is_5m_bar_close_window(now_kst: pd.Timestamp, start_sec: int = 5, end_sec: int = 60) -> bool:
        return (now_kst.minute % 5 == 0) and (start_sec <= now_kst.second <= end_sec)


# ============================================================================
# DetailGetter 인터페이스 (Duck typing)
# ============================================================================

class DetailGetter(Protocol):
    async def get_bars(self, code: str, interval: str, count: int) -> pd.DataFrame: ...


# ============================================================================
# 모니터러 본체
# ============================================================================

class ExitEntryMonitor:
    """
    - 5분봉 종가 기준으로 매수/매도 신호 판단
    - 동일 봉 중복 트리거 방지
    - 봉 마감 구간에서만 평가
    - 🔧 캐시 우선 설계: ingest_bars()로 들어온 DF를 먼저 활용, 없을 때만 pull
    - 🔔 조건검색(편입) 즉시 트리거 → TradeSignal로 통합 발행
    - 🔵 Pro 분기: **추세 전환(Trend Reversal)** 기준만 사용 (외부 룰 주입 제거)
      * Buy-Pro ON  → DOWN/HOLD → UP 전환 시 발행
      * Buy-Pro OFF → 즉시 발행(레거시)
      * Sell-Pro ON → UP/HOLD → DOWN 전환 시 발행
      * Sell-Pro OFF→ 주기적 SELL 억제
    """

    def __init__(
        self,
        detail_getter: DetailGetter,
        *,
        macd30_timeframe: str = "30m",
        macd30_max_age_sec: int = 1800,
        tz: str = "Asia/Seoul",
        poll_interval_sec: int = 20,
        on_signal: Optional[Callable[[TradeSignal], None]] = None,
        bridge: Optional[object] = None,
        bar_close_window_start_sec: int = 5,
        bar_close_window_end_sec: int = 30,
        disable_server_pull: bool = False,
        custom: Optional[MonitorCustom] = None,
        trading_result_path: str = "data/trading_result.json", # ← 추가
        result_reader: TradingResultReader | None = None,      # ← 추가
        sell_profit_threshold: float = 0.03,                   # ← 추가: +3%
    ):
        self.detail_getter = detail_getter
        self.bridge = bridge
        self.macd30_timeframe = macd30_timeframe
        self.macd30_max_age_sec = macd30_max_age_sec

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.tz = tz
        self.poll_interval_sec = poll_interval_sec
        self.on_signal = on_signal or (lambda sig: logger.info(f"[SIGNAL] {sig}"))
        self.disable_server_pull = bool(disable_server_pull)
        self.custom = custom or MonitorCustom()

        # 직전 추세 상태 저장 (Pro 전략용)
        self._last_trend: Dict[Tuple[str, str], Literal['UP', 'DOWN', 'NEUTRAL', 'HOLD']] = {}

        # 파라미터 검증
        if not (0 <= bar_close_window_start_sec <= bar_close_window_end_sec <= 59):
            raise ValueError("bar_close_window must satisfy 0 <= start <= end <= 59")
        self._win_start = int(bar_close_window_start_sec)
        self._win_end   = int(bar_close_window_end_sec)

        # 내부 상태
        self._last_trig: Dict[Tuple[str, str], pd.Timestamp] = {}
        self._bars_cache: Dict[Tuple[str, str], pd.DataFrame] = {}
        self._symbols: set[str] = set()
        self._sym_lock = threading.RLock()
        self.symbols: List[str] = []

        self.use_macd30_filter: bool = False
        self.sell_profit_threshold: float = float(sell_profit_threshold)

        # ✅ 결과 리더 세팅 (없으면 경로로 생성)
        self.result_reader: TradingResultReader = (
            result_reader or TradingResultReader(trading_result_path)
        )

        # MACD 시리즈 준비 이벤트 구독 (가능할 때만)
        try:
            if hasattr(macd_bus, "on"):
                macd_bus.on("series_ready", self._on_macd_series_ready)
        except Exception:
            logger.debug("macd_bus subscription failed; continue without it")


    # ------------------------------------------------------------------
    # SettingsManager 연동: 통합 적용 API
    # ------------------------------------------------------------------

    def apply_settings(self, cfg) -> None:
        """SettingsManager.AppSettings 값을 모니터에 반영.
        duck-typing으로 접근하여 외부 의존 최소화.
        """
        try:
            # 핵심 스위치
            if hasattr(self, "set_custom") and callable(self.set_custom):
                self.set_custom(
                    enabled=True,
                    auto_buy=bool(getattr(cfg, "auto_buy", True)),
                    auto_sell=bool(getattr(cfg, "auto_sell", False)),
                    allow_intrabar_condition_triggers=True,
                    buy_pro=bool(getattr(cfg, "buy_pro", False)),
                    sell_pro=bool(getattr(cfg, "sell_pro", True)),
                )
            # 루프/시간대/창
            self.poll_interval_sec = int(getattr(cfg, "poll_interval_sec", self.poll_interval_sec))
            self._win_start = int(getattr(cfg, "bar_close_window_start_sec", self._win_start))
            self._win_end   = int(getattr(cfg, "bar_close_window_end_sec", self._win_end))
            self.tz = getattr(cfg, "timezone", self.tz) or "Asia/Seoul"
            # MACD 필터/파라미터
            self.use_macd30_filter = bool(getattr(cfg, "use_macd30_filter", self.use_macd30_filter))
            self.macd30_timeframe = str(getattr(cfg, "macd30_timeframe", self.macd30_timeframe) or self.macd30_timeframe)
            self.macd30_max_age_sec = int(getattr(cfg, "macd30_max_age_sec", self.macd30_max_age_sec))
            self.sell_profit_threshold = float(getattr(cfg, "sell_profit_threshold", self.sell_profit_threshold))
        
        except Exception:
            logger.exception("[ExitEntryMonitor] apply_settings failed")

    # ------------------------------------------------------------------
    # Pro 설정 업데이트 (개별 토글용 기존 API 유지)
    # ------------------------------------------------------------------

    def set_custom(
        self,
        *,
        enabled: bool | None = None,
        auto_buy: bool | None = None,
        auto_sell: bool | None = None,
        allow_intrabar_condition_triggers: bool | None = None,
        buy_pro: bool | None = None,
        sell_pro: bool | None = None,
    ):
        if enabled is not None:
            self.custom.enabled = bool(enabled)
        if auto_buy is not None:
            self.custom.auto_buy = bool(auto_buy)
        if auto_sell is not None:
            self.custom.auto_sell = bool(auto_sell)
        if allow_intrabar_condition_triggers is not None:
            self.custom.allow_intrabar_condition_triggers = bool(allow_intrabar_condition_triggers)
        if buy_pro is not None:
            self.custom.buy_pro = bool(buy_pro)
        if sell_pro is not None:
            self.custom.sell_pro = bool(sell_pro)

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _schedule_check(self, symbol: str):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._check_symbol(symbol))
        except RuntimeError:
            threading.Thread(target=lambda: asyncio.run(self._check_symbol(symbol)), daemon=True).start()

    def _schedule_immediate_check(self, symbol: str):
        loop = self._loop
        if loop and loop.is_running():
            run_coroutine_threadsafe(self._check_symbol(symbol), loop)
        else:
            logger.debug("loop not running; skip")

    def _get_symbols_snapshot(self) -> List[str]:
        with self._sym_lock:
            if self._symbols:
                return list(self._symbols)
            return list(self.symbols)

    # ------------------------------------------------------------------
    # 데이터 주입(Feed → Cache)
    # ------------------------------------------------------------------

    def ingest_bars(self, symbol: str, timeframe: str, df: pd.DataFrame):
        tf = str(timeframe).lower()
        sym = _code6(symbol)

        if df is None or df.empty:
            return
        df = df.copy()

        need_cols = ["Open", "High", "Low", "Close", "Volume"]
        if list(df.columns) != need_cols:
            mapper = {
                "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume",
                "Open": "Open", "High": "High", "Low": "Low", "Close": "Close", "Volume": "Volume",
            }
            try:
                df = df.rename(columns=mapper)[need_cols]
            except Exception:
                logger.warning("[ExitEntryMonitor] ingest: invalid columns=%s", list(df.columns))
                return

        if not isinstance(df.index, pd.DatetimeIndex):
            try:
                df.index = pd.to_datetime(df.index)
            except Exception:
                logger.warning("[ExitEntryMonitor] ingest: non-datetime index -> skip")
                return
        if df.index.tz is None:
            df.index = df.index.tz_localize(self.tz)
        else:
            df.index = df.index.tz_convert(self.tz)

        for c in need_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["Close"])  # 핵심열 결측 제거
        if df.empty:
            return

        key = (sym, tf)
        with self._sym_lock:
            cur = self._bars_cache.get(key)
            merged = (pd.concat([cur, df]) if cur is not None and not cur.empty else df)
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()

            now = pd.Timestamp.now(tz=self.tz)
            cutoff_future = now + pd.Timedelta(days=3)
            merged = merged[merged.index <= cutoff_future]

            MAX_KEEP = 5000
            if len(merged) > MAX_KEEP:
                merged = merged.iloc[-MAX_KEEP:]

            self._bars_cache[key] = merged
            self._symbols.add(sym)

            last_ts = merged.index[-1]
            last_close = float(merged["Close"].iloc[-1])

        logger.debug(f"[ExitEntryMonitor] cache[{sym},{tf}] size={len(merged)} last={last_ts} close={last_close}")

        if tf == "5m":
            now_kst = pd.Timestamp.now(tz=self.tz)
            if TimeRules.is_5m_bar_close_window(now_kst, self._win_start, self._win_end):
                try:
                    self._schedule_immediate_check(sym)
                except Exception:
                    self._schedule_check(sym)

    # ------------------------------------------------------------------
    # 캐시-우선 5분봉 조회
    # ------------------------------------------------------------------

    async def _get_5m(self, symbol: str, count: int = 200) -> Optional[pd.DataFrame]:
        sym = _code6(symbol)
        key = (sym, "5m")

        with self._sym_lock:
            df_cache = self._bars_cache.get(key)

        if df_cache is not None and not df_cache.empty:
            tail = df_cache.iloc[-count:] if len(df_cache) > count else df_cache
            logger.debug(f"[ExitEntryMonitor] 5m 캐시 HIT: {sym} len={len(tail)} last={tail.index[-1]}")
            return tail

        logger.debug(f"[ExitEntryMonitor] 5m 캐시 MISS: {sym}")

        if self.disable_server_pull:
            logger.debug(f"[ExitEntryMonitor] server pull disabled → None ({sym})")
            return None

        logger.debug(f"[ExitEntryMonitor] 5m 캐시에 없음 → pull 시도: {sym}")
        try:
            df_pull = await self.detail_getter.get_bars(code=sym, interval="5m", count=count)
        except Exception as e:
            logger.debug(f"[ExitEntryMonitor] pull 실패: {sym} {e}")
            return None

        if df_pull is not None and not df_pull.empty:
            need_cols = ["Open", "High", "Low", "Close", "Volume"]
            if list(df_pull.columns) != need_cols:
                mapper = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
                try:
                    df_pull = df_pull.rename(columns=mapper)[need_cols]
                except Exception:
                    logger.debug(f"[ExitEntryMonitor] pull DF invalid columns: {list(df_pull.columns)}")
                    return None
            if df_pull.index.tz is None:
                df_pull.index = df_pull.index.tz_localize(self.tz)
            with self._sym_lock:
                self._bars_cache[key] = df_pull
            logger.debug(f"[ExitEntryMonitor] 5m pull 저장: {sym} len={len(df_pull)}")
            return df_pull

        logger.debug(f"[ExitEntryMonitor] 5m 데이터 부족/없음: {sym}")
        return None

    # ------------------------------------------------------------------
    # ---- 평균/수량 조회 (리더 사용) ----
    # ------------------------------------------------------------------

    def _get_avg_buy(self, symbol: str) -> Optional[float]:
        try:
            return self.result_reader.get_avg_buy(symbol)
        except Exception:
            return None


    def _is_profit_threshold_met(self, symbol: str, last_price: float, threshold: Optional[float] = None) -> bool:
        """평균매수가 대비 threshold 이상 이익이면 True. 평균/가격 불명확 시 False."""
        thr = float(self.sell_profit_threshold if threshold is None else threshold)
        if last_price is None or float(last_price) <= 0:
            return False
        avg = self._get_avg_buy(symbol)
        if avg is None or avg <= 0:
            return False
        return float(last_price) >= float(avg) * (1.0 + thr)

    def _get_qty_and_avg(self, symbol: str) -> Optional[tuple[int, float]]:
        """(qty, avg_price) 튜플. 평균이 없거나 0 이하면 None."""
        try:
            return self.result_reader.get_qty_and_avg_buy(symbol)
        except Exception:
            return None

    def _has_position(self, symbol: str) -> bool:
        """result_reader 기준 보유수량 > 0이면 True."""
        qa = self._get_qty_and_avg(symbol)
        return bool(qa and int(qa[0]) > 0)

    # ------------------------------------------------------------------
    # MACD 30m 필터 (옵션)
    # ------------------------------------------------------------------

    def _macd30_allows_long(self, symbol: str) -> bool:
        """use_macd30_filter가 켜져 있을 때 BUY 허용 여부를 판단.
        - hist >= 0 이고, 시그널 시각이 macd30_max_age_sec 이내면 True
        - 실패/예외 시에는 보수적으로 **허용**(False로 막지 않음)
        """
        if not self.use_macd30_filter:
            return True
        try:
            sym = _code6(symbol)
            pts = _get_points(sym, tf=self.macd30_timeframe, limit=1)  # 구현체에 따라 dict/list 반환 가정
            if not pts:
                return True
            p = pts[0] if isinstance(pts, (list, tuple)) else pts
            hist = float(p.get("hist")) if isinstance(p, dict) and p.get("hist") is not None else None
            ts = p.get("ts") if isinstance(p, dict) else None
            if ts is None:
                return True
            ts = pd.Timestamp(ts)
            if ts.tz is None:
                ts = ts.tz_localize(self.tz)
            age = (pd.Timestamp.now(tz=self.tz) - ts).total_seconds()
            if age > float(self.macd30_max_age_sec):
                return True  # 오래됐으면 필터 비활성 취급(차단하지 않음)
            if hist is None:
                return True
            return hist >= 0
        except Exception:
            return True

    # ------------------------------------------------------------------
    # 신호 발행
    # ------------------------------------------------------------------

    def _emit(self, side: str, symbol: str, ts: pd.Timestamp, price: float, reason: str,
            *, condition_name: str = "", source: str = "bar", extra: dict | None = None):
        key = (symbol, side)
        if self._last_trig.get(key) == ts:
            logger.debug(f"[ExitEntryMonitor] {symbol} {side} 신호 중복(ts={ts}) → 무시")
            return
        self._last_trig[key] = ts

        try:
            if self.bridge and hasattr(self.bridge, "log"):
                self.bridge.log.emit(f"[ExitEntryMonitor] 📣 신호 발생 {side} {symbol} {price:.2f} @ {ts} | {reason}")
        except Exception:
            pass

        sig_obj = TradeSignal(
            side=side, symbol=symbol, ts=ts, price=price, reason=reason,
            source=source, condition_name=condition_name, extra=extra
        )
        try:
            self.on_signal(sig_obj)
        except Exception:
            logger.exception("[ExitEntryMonitor] on_signal handler error")

    # ------------------------------------------------------------------
    # 조건검색 '편입(I)' 즉시 트리거 → TradeSignal 통합 발행 (+ Pro 분기)
    # ------------------------------------------------------------------

    async def on_condition_detected(
        self,
        symbol: str,
        *,
        condition_name: str = "",
        source: str = "condition",
        reason: str = "조건검색 편입(I)",
    ):
        sym = _code6(symbol)
        try:
            with self._sym_lock:
                self._symbols.add(sym)

            now_ts = pd.Timestamp.now(tz=self.tz)

            df5: Optional[pd.DataFrame] = None
            last_close: float = 0.0
            ref_ts: pd.Timestamp = now_ts

            if self.custom.auto_buy:
                if not self.custom.buy_pro:
                    # 레거시: 즉시 발행 (가격은 5분봉 종가 폴백)
                    df5_fallback = await self._get_5m(sym, count=2)
                    if df5_fallback is not None and not df5_fallback.empty:
                        fallback_price = float(df5_fallback["Close"].iloc[-1])
                        fallback_ts = df5_fallback.index[-1]
                        # MACD 필터 체크
                        if self._macd30_allows_long(sym):
                            self._emit("BUY", sym, fallback_ts, fallback_price,
                                reason or f"즉시신호(BUY) {condition_name}",
                                condition_name=condition_name, source="condition")

                        last_close = fallback_price
                        ref_ts = fallback_ts
                        df5 = df5_fallback
                else:
                    # Pro: 추세 전환 기준으로 즉시 평가 (intrabar 허용 조건 반영)
                    should_block = (
                        self.custom.buy_pro and not (self.custom.enabled and self.custom.allow_intrabar_condition_triggers)
                    )
                    if should_block:
                        logger.debug(f"[Monitor] buy_pro ON, intrabar not allowed → skip immediate ({sym})")
                        return

                    if df5 is None:
                        df5 = await self._get_5m(sym, count=200)
                    if df5 is None or df5.empty or len(df5) < 2:
                        logger.debug(f"[Monitor] {sym} 즉시(Pro) 5m 부족 → skip")
                        return

                    trend_msg = self._get_trend_message(sym, "5m", df5)
                    cur_close = float(df5["Close"].iloc[-1])
                    ref_ts = df5.index[-1]

                    current_trend = self._trend_label_from_message(trend_msg)
                    previous_trend = self._last_trend.get((sym, "5m"), 'NEUTRAL')
                    self._last_trend[(sym, "5m")] = current_trend

                    if previous_trend in ('DOWN', 'HOLD') and current_trend == 'UP':
                        if self._macd30_allows_long(sym):
                            self._emit("BUY", sym, ref_ts, cur_close,
                                reason or f"BUY(Pro Trend Reversal) {condition_name}",
                                condition_name=condition_name, source="condition")


            try:
                if self.bridge and hasattr(self.bridge, "log") and self.custom.auto_buy:
                    display_price = last_close if last_close > 0 else 0
                    self.bridge.log.emit(f"📊 즉시신호 [BUY] {sym} @ {display_price} ({condition_name})")
            except Exception:
                pass

        except Exception:
            logger.exception(f"[Monitor] on_condition_detected error: {symbol}")

    # ------------------------------------------------------------------
    # 심볼 평가 (5m, 30m)
    # ------------------------------------------------------------------

    async def _check_symbol(self, symbol: str):
        await self._evaluate_tf(symbol, "5m")

    # ------------------------------------------------------------------
    # MACD 버스 이벤트 핸들러
    # ------------------------------------------------------------------

    def _on_macd_series_ready(self, payload: dict):
        try:
            code = _code6(payload.get("code") or "")
            tf   = str(payload.get("tf") or "").lower()
            if not code or tf != self.macd30_timeframe.lower():
                return

            with self._sym_lock:
                if code not in self._symbols:
                    self._symbols.add(code)
                    logger.info("[ExitEntryMonitor] ▶ track add: %s (tf=%s, total=%d)",
                                code, tf, len(self._symbols))

            try:
                now_kst = pd.Timestamp.now(tz=self.tz)
                if TimeRules.is_5m_bar_close_window(now_kst, self._win_start, self._win_end):
                    self._schedule_immediate_check(code)
            except Exception as e:
                logger.debug("[ExitEntryMonitor] immediate check skip: %s", e)

        except Exception:
            logger.exception("[ExitEntryMonitor] MACD bus handler error")

    # ------------------------------------------------------------------
    # 루프 시작
    # ------------------------------------------------------------------

    async def start(self):
        self._loop = asyncio.get_running_loop()
        logger.info("[ExitEntryMonitor] 모니터링 시작")
        while True:
            try:
                now_kst = pd.Timestamp.now(tz=self.tz)

                if TimeRules.is_5m_bar_close_window(now_kst, self._win_start, self._win_end):
                    symbols_snapshot = self._get_symbols_snapshot()
                    if not symbols_snapshot:
                        logger.debug("[ExitEntryMonitor] no symbols to check (snapshot empty)")
                    else:
                        logger.debug(
                            f"[ExitEntryMonitor] 5분봉 마감 구간 @ {now_kst} | symbols={len(symbols_snapshot)}"
                        )
                        tasks = []
                        for s in symbols_snapshot:
                            tasks.append(self._evaluate_tf(s, "5m"))
                            tasks.append(self._evaluate_tf(s, "30m"))
                        await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as e:
                logger.exception(f"[ExitEntryMonitor] 루프 오류: {e}")

            await asyncio.sleep(self.poll_interval_sec)

    # ------------------------------------------------------------------
    # 🔵 추세 분석 헬퍼 (강력 돌파 기준)
    # ------------------------------------------------------------------

    def _get_trend_message(self, symbol: str, timeframe: str, df: pd.DataFrame) -> str:
        if df is None or len(df) < 2:
            return ""

        sym = _code6(symbol)
        tf = timeframe
        last = df.iloc[-1]
        prev = df.iloc[-2]

        cur_close = float(last["Close"])
        prev_open = float(prev["Open"])
        prev_close = float(prev["Close"])

        prev_min = min(prev_open, prev_close)
        prev_max = max(prev_open, prev_close)
        prev_is_bear = prev_close < prev_open

        trend_msg = "추세 중립/불확실"
        if cur_close > prev_max:
            trend_msg = f"추세 상승: 직전 봉 몸통 ({prev_max:.2f}) 상방 강력 돌파 마감"
        elif cur_close < prev_min:
            trend_msg = f"추세 하락: 직전 봉 몸통 ({prev_min:.2f}) 하방 강력 이탈 마감"
        elif prev_min <= cur_close <= prev_max:
            if prev_is_bear:
                trend_msg = "추세 유지: 직전 음봉 몸통 내 마감 (약한 반등 또는 횡보)"
            else:
                trend_msg = "추세 유지: 직전 양봉 몸통 내 마감 (약한 조정 또는 횡보)"
        return f"[{tf}] {sym} @ {last.name.strftime('%H:%M')} | {trend_msg} (종가: {cur_close:.2f})"

    def _trend_label_from_message(self, trend_msg: str) -> Literal['UP', 'DOWN', 'HOLD', 'NEUTRAL']:
        if "추세 상승" in trend_msg:
            return 'UP'
        if "추세 하락" in trend_msg:
            return 'DOWN'
        if "추세 유지" in trend_msg:
            return 'HOLD'
        return 'NEUTRAL'

    # ------------------------------------------------------------------
    # UI 로그 전송 헬퍼 (bridge가 있는 경우)
    # ------------------------------------------------------------------

    def _log_trend(self, msg: str):
        try:
            if self.bridge and hasattr(self.bridge, "log"):
                self.bridge.log.emit(f"📈 {msg}")
                logger.info(f"📈 {msg}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # TF별 평가 (5m에서 신호, 30m는 추세만)
    # ------------------------------------------------------------------

    async def _get_bars_for_evaluation(self, symbol: str, timeframe: str, count: int = 200) -> Optional[pd.DataFrame]:
        if timeframe == "5m":
            return await self._get_5m(symbol, count=count)
        else:
            sym = _code6(symbol)
            key = (sym, timeframe)
            with self._sym_lock:
                df_cache = self._bars_cache.get(key)
            if df_cache is not None and not df_cache.empty:
                return df_cache.iloc[-count:] if len(df_cache) > count else df_cache
            try:
                df_pull = await self.detail_getter.get_bars(code=sym, interval=timeframe, count=count)
                if df_pull is not None and not df_pull.empty:
                    return df_pull
            except Exception:
                pass
            return None

    async def _evaluate_tf(self, symbol: str, timeframe: str):
        try:
            sym = _code6(symbol)
            tf  = timeframe.lower()
            trend_key = (sym, tf)

            df_bars = await self._get_bars_for_evaluation(sym, tf)
            if df_bars is None or df_bars.empty or len(df_bars) < 2:
                return

            ref_ts = df_bars.index[-1]
            last_close = float(df_bars["Close"].iloc[-1])

            # 추세 메시지 & 라벨
            trend_msg = self._get_trend_message(sym, tf, df_bars)
            self._log_trend(trend_msg)

            current_trend = self._trend_label_from_message(trend_msg)
            previous_trend = self._last_trend.get(trend_key, 'NEUTRAL')
            self._last_trend[trend_key] = current_trend

            logger.debug(f"[Monitor] {sym} {tf} 추세: Prev={previous_trend}, Curr={current_trend}")

            if tf == "5m":
                # =============== SELL (Pro: 전환 기준 + 이익 임계치) ===============
                if self.custom.auto_sell:
                    if self.custom.sell_pro:
                        # ✅ ① 보유 여부 체크 (result_reader 기준)
                        if not self._has_position(sym):
                            logger.debug(f"[Monitor] {sym} SELL-Pro: 보유수량 0 → 모니터링 스킵")
                        else:
                            # ✅ ② 이익 임계치(+3% 등) 충족 여부
                            profit_ok = self._is_profit_threshold_met(sym, last_close)
                            if not profit_ok:
                                logger.debug(f"[Monitor] {sym} SELL-Pro: +{self.sell_profit_threshold*100:.1f}% 미만 → 스킵")
                            else:
                                # ✅ ③ 추세 전환 조건
                                if previous_trend in ('UP', 'HOLD') and current_trend == 'DOWN':
                                    sell_qty: Optional[int] = None
                                    avg_px: Optional[float] = None
                                    qa = self._get_qty_and_avg(sym)
                                    if qa:
                                        sell_qty, avg_px = qa  # (qty, avg)

                                    suggested_qty = int(sell_qty or 0)
                                    if suggested_qty <= 0:
                                        logger.debug(f"[Monitor] {sym} SELL-Pro: 보유수량 0 → 신호만 발행")

                                    self._emit(
                                        "SELL", sym, ref_ts, last_close,
                                        f"SELL(Pro Trend Reversal: ->DOWN, +{self.sell_profit_threshold*100:.1f}% OK)",
                                        condition_name="",
                                        source="bar",
                                        extra={
                                            "suggested_qty": suggested_qty,
                                            "avg_buy": avg_px,
                                            "profit_threshold": self.sell_profit_threshold,
                                        },
                                    )
                    else:
                        logger.debug(f"[Monitor] {sym} SELL: Pro OFF. Periodic SELL suppressed.")

                # =============== BUY  (Pro: 전환 기준만) ===============
                if self.custom.auto_buy:
                    if self.custom.buy_pro:
                        if previous_trend in ('DOWN', 'HOLD') and current_trend == 'UP':
                            if self._macd30_allows_long(sym):
                                self._emit("BUY", sym, ref_ts, last_close, "BUY(Pro Trend Reversal: ->UP)")
                    else:
                        if self._macd30_allows_long(sym):
                            self._emit("BUY", sym, ref_ts, last_close, "BUY(Legacy Bar Close Immediate)")

        except Exception:
            logger.exception(f"[ExitEntryMonitor] _evaluate_tf error: {symbol}")
