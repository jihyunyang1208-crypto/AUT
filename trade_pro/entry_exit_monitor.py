# trade_pro/entry_exit_monitor.py
from __future__ import annotations

import asyncio
from asyncio import run_coroutine_threadsafe
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol, Tuple, Literal

import pandas as pd
import json
from pathlib import Path
import threading
from datetime import datetime, timezone

# MACD 버스/조회기 (필요 시 의존성 주입으로 대체 가능)
from core.macd_calculator import get_points as _get_points
from core.macd_calculator import macd_bus

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
    extra: dict = None        # 추가정보 (optional)
    return_msg: str | None = None

@dataclass
class MonitorCustom:
    """고급 커스텀 설정 (모니터가 해석)"""
    enabled: bool = False                # 고급 커스텀 전체 스위치
    auto_buy: bool = True                # '매수' 체크
    auto_sell: bool = False              # '매도' 체크
    allow_intrabar_condition_triggers: bool = True  # 봉마감 전 즉시 트리거 허용

    # 🔵 추가: Pro 토글
    # 기본값을 buy_pro=False, sell_pro=True 로 두어 기존 동작과 100% 호환
    buy_pro: bool = False               # Buy-Pro ON/OFF (조건 즉시 트리거에서 룰 체크)
    sell_pro: bool = True               # Sell-Pro ON/OFF (주기 평가/조건 즉시 트리거에서 룰 체크)


# ============================================================================
# 룰
# ============================================================================
class BuyRules:
    @staticmethod
    def buy_if_5m_break_prev_bear_high(df5: pd.DataFrame) -> pd.Series:
        """
        [DEPRECATED] 이 메서드는 추세 전환/Pro 로직으로 완전히 대체되었습니다.
        더 이상 신호 평가에 사용되지 않습니다.
        """
        # 💡 이 메서드를 호출하는 코드가 남아있다면, 즉시 False를 반환하여 안전하게 처리합니다.
        if df5 is None or df5.empty:
            return pd.Series(dtype=bool)
        
        # 항상 False 신호를 반환하여 기존 기능을 비활성화
        return pd.Series([False] * len(df5), index=df5.index, dtype=bool)
        
class SellRules:
    @staticmethod
    def profit3_and_prev_candle_pattern(df5: pd.DataFrame, avg_buy: float) -> bool:
        """
        조건(모두 만족 시 True):
          1) 현재가(현재 5분봉 종가) ≥ 평균매수가 * 1.03  (매수가 대비 +3% 이상)
          2) 이전봉 패턴에 따라:
             - 이전봉이 '음봉'(prev.Close < prev.Open) 이면:  현재 종가 < 이전봉 종가
             - 이전봉이 '양봉'(prev.Close > prev.Open) 이면:  현재 종가 < 이전봉 시가
             - (도지 등 중립이면 매도 X)
        """
        if df5 is None or len(df5) < 2 or pd.isna(avg_buy) or avg_buy <= 0:
            return False

        last_close = float(df5["Close"].iloc[-1])
        prev_open  = float(df5["Open"].iloc[-2])
        prev_close = float(df5["Close"].iloc[-2])

        # 1) +3% 이상
        if last_close < avg_buy * 1.03:
            return False

        # 2) 이전봉 패턴별 분기
        if prev_close < prev_open:  # 이전봉 음봉
            return last_close < prev_close
        elif prev_close > prev_open:  # 이전봉 양봉
            return last_close < prev_open
        else:
            # 도지/무변동 등은 보수적으로 패스
            return False

class TimeRules:
    @staticmethod
    def is_5m_bar_close_window(now_kst: pd.Timestamp, start_sec: int = 5, end_sec: int = 30) -> bool:
        """
        5분봉 마감 근사 구간:
        - now.minute % 5 == 0
        - start_sec ~ end_sec 사이(둘 다 포함)
        """
        return (now_kst.minute % 5 == 0) and (start_sec <= now_kst.second <= end_sec)


# ============================================================================
# DetailGetter 인터페이스 (Duck typing)
# ============================================================================
class DetailGetter(Protocol):
    async def get_bars(self, code: str, interval: str, count: int) -> pd.DataFrame: ...


# ============================================================================
# 모니터러 본체
# ============================================================================
RuleFn = Callable[[Dict[str, object]], bool]

class ExitEntryMonitor:
    """
    - 5분봉 종가 기준으로 매수/매도 신호 판단
    - 동일 봉 중복 트리거 방지
    - 봉 마감 구간에서만 평가
    - 🔧 캐시 우선 설계: ingest_bars()로 들어온 DF를 먼저 활용, 없을 때만 pull
    - 🔔 조건검색(편입) 즉시 트리거 → TradeSignal로 통합 발행
    - 🔵 Pro 분기:
        * Buy-Pro ON  → 조건 즉시 트리거 시 buy_rule_fn 통과 시 발행 (없으면 True)
        * Buy-Pro OFF → 조건 즉시 트리거 시 즉시 발행(이전과 동일)
        * Sell-Pro ON → 내부 매도전략/혹은 sell_rule_fn 통과 시 발행(없으면 기존 전략)
        * Sell-Pro OFF→ 내부 매도전략 발행 중지(주기 평가), 조건 즉시 트리거 시 즉시 발행
    """
    def __init__(
        self,
        detail_getter: DetailGetter,
        *,
        macd30_timeframe: str = "30m",
        macd30_max_age_sec: int = 1800,  # 30분
        tz: str = "Asia/Seoul",
        poll_interval_sec: int = 20,
        on_signal: Optional[Callable[[TradeSignal], None]] = None,
        bridge: Optional[object] = None,
        bar_close_window_start_sec: int = 5,
        bar_close_window_end_sec: int = 30,
        disable_server_pull: bool = False,   # 💡 캐시만 사용하고 싶을 때 True
        custom: Optional[MonitorCustom] = None,  # 💡 고급 커스텀
        position_mgr: Optional[object] = None,   # 💡 PM 주입(평단 조회 전담)

        # 🔵 Pro 룰 주입(선택). 미제공 시 기본 동작:
        #  - BUY: True 반환(= Pro ON이어도 기존 즉시 발행과 동일)
        #  - SELL: 기존 내부 전략(SellRules...)을 사용
        buy_rule_fn: Optional[RuleFn] = None,
        sell_rule_fn: Optional[RuleFn] = None,
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
        self.position_mgr = position_mgr  # ✅ PositionManager 주입

        # 🔵 Pro 룰(없으면 기본 동작)
        self._buy_rule_fn: RuleFn = buy_rule_fn or (lambda ctx: True)
        # SELL 기본 룰은 내부 전략을 디폴트로 묶어둔다.
        self._sell_rule_fn: RuleFn = sell_rule_fn or (lambda ctx: bool(
            SellRules.profit3_and_prev_candle_pattern(ctx["df5"], float(ctx["avg_buy"]))  # type: ignore[index]
            if (ctx.get("df5") is not None and ctx.get("avg_buy") is not None)
            else False
        ))
        # 직전 추세 상태 저장 (Pro 전략용)
        self._last_trend: Dict[Tuple[str, str], Literal['UP', 'DOWN', 'NEUTRAL']] = {}

        # 파라미터 검증
        if not (0 <= bar_close_window_start_sec <= bar_close_window_end_sec <= 59):
            raise ValueError("bar_close_window must satisfy 0 <= start <= end <= 59")
        self._win_start = int(bar_close_window_start_sec)
        self._win_end   = int(bar_close_window_end_sec)

        # 내부 상태
        self._last_trig: Dict[Tuple[str, str], pd.Timestamp] = {}  # (symbol, side) → ts
        self._bars_cache: Dict[Tuple[str, str], pd.DataFrame] = {}
        self._symbols: set[str] = set()
        self._sym_lock = threading.RLock()  # 캐시/심볼 보호

        # (선택) 고정 리스트 self.symbols 지원 (외부가 채우는 경우)
        self.symbols: List[str] = []


    # ----------------------------------------------------------------------
    # Pro 설정/룰 업데이트 (옵션)
    # ----------------------------------------------------------------------
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

    def set_rules(self, *, buy_rule_fn: Optional[RuleFn] = None, sell_rule_fn: Optional[RuleFn] = None):
        if buy_rule_fn is not None:
            self._buy_rule_fn = buy_rule_fn
        if sell_rule_fn is not None:
            self._sell_rule_fn = sell_rule_fn

    # ----------------------------------------------------------------------
    # 내부 헬퍼
    # ----------------------------------------------------------------------
    def _schedule_check(self, symbol: str):
        """이벤트 루프 환경 여부와 무관하게 안전하게 _check_symbol 스케줄링."""
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
        """
        - 동적 추적(_symbols) 있으면 그것을 사용
        - 아니면 고정 리스트(self.symbols)를 사용
        """
        with self._sym_lock:
            if self._symbols:
                return list(self._symbols)
            return list(self.symbols)

    # ----------------------------------------------------------------------
    # 데이터 주입(Feed → Cache)
    # ----------------------------------------------------------------------
    def ingest_bars(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """
        외부에서 받은 OHLCV df(예: 5m, 30m)를 내부 캐시에 '병합' 저장하고
        심볼을 트래킹 목록에 추가. 5분봉 마감창이면 즉시 1회 평가.
        - 인덱스: tz-aware(Asia/Seoul) 권장
        - 컬럼  : Open,High,Low,Close,Volume
        """
        tf = str(timeframe).lower()
        sym = _code6(symbol)

        # 0) 입력 가드
        if df is None or df.empty:
            return
        df = df.copy()  # 외부 DF 오염 방지

        # 1) 컬럼 정규화
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

        # 2) 인덱스 정규화(시간/타임존)
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

        # 3) 타입 보정(숫자형 강제)
        for c in need_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["Close"])  # 핵심열 결측 제거
        if df.empty:
            return

        # 4) 병합(기존 캐시와 concat→중복 제거→정렬→슬라이딩 윈도우)
        key = (sym, tf)
        with self._sym_lock:
            cur = self._bars_cache.get(key)
            if cur is not None and not cur.empty:
                merged = pd.concat([cur, df])
            else:
                merged = df

            # 중복 타임스탬프 제거(마지막 값 우선), 시간 정렬
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()

            # 미래 시각(클럭 이슈) 필터(±3일 이상 튀면 제거)
            now = pd.Timestamp.now(tz=self.tz)
            cutoff_future = now + pd.Timedelta(days=3)
            merged = merged[merged.index <= cutoff_future]

            # 메모리 보호: 최근 N개만 유지(필요시 조정)
            MAX_KEEP = 5000
            if len(merged) > MAX_KEEP:
                merged = merged.iloc[-MAX_KEEP:]

            self._bars_cache[key] = merged
            self._symbols.add(sym)

            last_ts = merged.index[-1]
            last_close = float(merged["Close"].iloc[-1])

        logger.debug(f"[ExitEntryMonitor] cache[{sym},{tf}] size={len(merged)} last={last_ts} close={last_close}")

        # 5) 5분봉 마감창이면 즉시 1회 평가 (루프 전/후 모두 안전하게)
        if tf == "5m":
            now_kst = pd.Timestamp.now(tz=self.tz)
            if TimeRules.is_5m_bar_close_window(now_kst, self._win_start, self._win_end):
                try:
                    self._schedule_immediate_check(sym)
                except Exception:
                    self._schedule_check(sym)  # 루프 미기동 시 폴백

    # ----------------------------------------------------------------------
    # 캐시-우선 5분봉 조회
    # ----------------------------------------------------------------------
    async def _get_5m(self, symbol: str, count: int = 200) -> Optional[pd.DataFrame]:
        sym = _code6(symbol)
        key = (sym, "5m")

        # 1) 캐시 우선
        with self._sym_lock:
            df_cache = self._bars_cache.get(key)

        if df_cache is not None and not df_cache.empty:
            tail = df_cache.iloc[-count:] if len(df_cache) > count else df_cache
            logger.debug(f"[ExitEntryMonitor] 5m 캐시 HIT: {sym} len={len(tail)} last={tail.index[-1]}")
            return tail

        logger.debug(f"[ExitEntryMonitor] 5m 캐시 MISS: {sym}")

        # 2) pull 금지면 종료
        if self.disable_server_pull:
            logger.debug(f"[ExitEntryMonitor] server pull disabled → None ({sym})")
            return None

        # 3) 캐시에 없으면 pull 시도
        logger.debug(f"[ExitEntryMonitor] 5m 캐시에 없음 → pull 시도: {sym}")
        try:
            df_pull = await self.detail_getter.get_bars(code=sym, interval="5m", count=count)
        except Exception as e:
            logger.debug(f"[ExitEntryMonitor] pull 실패: {sym} {e}")
            return None

        if df_pull is not None and not df_pull.empty:
            # 형식 보정
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

    # ----------------------------------------------------------------------
    # PM에서 평균매수가 조회
    # ----------------------------------------------------------------------
    def _get_avg_buy(self, symbol: str) -> Optional[float]:
        """
        PositionManager의 공식 API(get_avg_buy)를 통해 평균매수가를 조회한다.
        - PM이 없거나 메서드가 없으면 None
        """
        pm = getattr(self, "position_mgr", None)
        if not pm:
            return None
        fn = getattr(pm, "get_avg_buy", None)
        if not callable(fn):
            return None
        try:
            sym = _code6(symbol)
            v = fn(sym)
            return float(v) if (v is not None and float(v) > 0) else None
        except Exception:
            return None


    # ----------------------------------------------------------------------
    # 신호 발행
    # ----------------------------------------------------------------------
    def _emit(self, side: str, symbol: str, ts: pd.Timestamp, price: float, reason: str):
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

        sig_obj = TradeSignal(side, symbol, ts, price, reason)  # source='bar' 기본값 유지

        # 1) 외부 콜백
        try:
            self.on_signal(sig_obj)
        except Exception:
            logger.exception("[ExitEntryMonitor] on_signal handler error")


    # ----------------------------------------------------------------------
    # 조건검색 '편입(I)' 즉시 트리거 → TradeSignal 통합 발행 (+ Pro 분기)
    # ----------------------------------------------------------------------

    async def on_condition_detected(
        self,
        symbol: str,
        *,
        condition_name: str = "",
        source: str = "condition",
        reason: str = "조건검색 편입(I)",
    ):
        """
        조건검색식에서 종목이 편입될 때 호출됨.
        - SELL 평가는 5분봉 마감 시점(_check_symbol)으로 분리됨.
        - BUY 평가는 buy_pro OFF 시 즉시 실행되며, Pro ON 시에만 엄격한 intrabar 룰을 따릅니다.
        """
        sym = _code6(symbol)
        try:
            # 1. 추적 목록에 추가
            with self._sym_lock:
                self._symbols.add(sym)

            # ts 변수가 인수로 전달되지 않았으므로 현재 시간으로 초기화
            now_ts = pd.Timestamp.now(tz=self.tz)

            # ----------------------------------------------------------------------
            # 2. [핵심] 즉시 트리거 차단 로직 (Strict Pro 경로만 차단)
            # buy_pro가 ON이고, 동시에 즉시 트리거가 허용되지 않은 경우에만 차단합니다.
            should_block_pro_only = (
                self.custom.buy_pro # Pro 경로 ON
                and not (self.custom.enabled and self.custom.allow_intrabar_condition_triggers)
            )
            
            if should_block_pro_only:
                logger.debug(f"[Monitor] buy_pro ON, but intrabar not allowed → skip immediate ({sym})")
                return
            # ----------------------------------------------------------------------

            df5: Optional[pd.DataFrame] = None
            last_close: float = 0.0
            ref_ts: pd.Timestamp = now_ts
            
            # === BUY 평가 ===
            if self.custom.auto_buy:
                
                # 🔵 Pro 전략 OFF: 즉시 신호 발행 (5분봉 조회 폴백)
                if not self.custom.buy_pro:
                    
                    logger.warning(f"[Monitor] {sym} 즉시신호(BUY): price 정보 없음, 5분봉 조회로 대체")
                    df5_fallback = await self._get_5m(sym, count=2)
                    if df5_fallback is not None and not df5_fallback.empty:
                        fallback_price = float(df5_fallback["Close"].iloc[-1])
                        fallback_ts = df5_fallback.index[-1]
                        self._emit("BUY", sym, fallback_ts, fallback_price, reason or f"즉시신호(BUY) {condition_name}")
                        
                        # 이후 Pro 로직에서 재사용을 위해 값 저장 (SELL 평가가 없으므로 필수 아님)
                        last_close = fallback_price
                        ref_ts = fallback_ts
                        df5 = df5_fallback
                        
                    # 매수 처리 후에도 함수를 종료하지 않고 아래 UI 로그로 이어집니다.
                
                # ✨ Pro 전략 ON: 5분봉 데이터 조회 및 Rule 체크
                elif self.custom.buy_pro:
                    # 데이터가 없으면 조회 (이미 위에서 폴백으로 조회했을 수 있음)
                    if df5 is None:
                        df5 = await self._get_5m(sym, count=200)

                    if df5 is None or df5.empty or len(df5) < 2:
                        logger.debug(f"[Monitor] {sym} 즉시트리거(Pro): 5m 없음/부족 → skip")
                        return

                    last_close = float(df5["Close"].iloc[-1])
                    ref_ts = df5.index[-1]

                    ctx_buy = {
                        "side": "BUY", "symbol": sym, "price": last_close, "df5": df5,
                        "ts": ref_ts, "source": source, "condition_name": condition_name,
                    }
                    try:
                        ok_buy = bool(self._buy_rule_fn(ctx_buy))
                    except Exception as e:
                        logger.warning(f"[Monitor] BUY rule error: {e} → pass-through(True)")
                        ok_buy = True
                    
                    if ok_buy:
                        self._emit("BUY", sym, ref_ts, last_close, reason or f"즉시신호(BUY-Pro) {condition_name}")

            # ----------------------------------------------------------------------
            # === SELL 평가 블록 삭제됨 ===
            # SELL 평가는 5분봉 마감 시점인 _check_symbol에서만 실행됩니다.
            # ----------------------------------------------------------------------

            # 3. UI 로그 (선택)
            try:
                if self.bridge and hasattr(self.bridge, "log") and self.custom.auto_buy:
                    # last_close가 0이면 BUY 신호가 발행되지 않았을 가능성 높음
                    display_price = last_close if last_close > 0 else 0
                    self.bridge.log.emit(f"📊 즉시신호 [BUY] {sym} @ {display_price} ({condition_name})")
            except Exception:
                pass

        except Exception:
            logger.exception(f"[Monitor] on_condition_detected error: {symbol}")

    # ----------------------------------------------------------------------
    # 심볼 평가 (SELL 전략 적용)  + Pro 분기
    # ----------------------------------------------------------------------

    # ----------------------------------------------------------------------
    # 심볼 평가 (SELL 전략 적용) + Pro 분기 (기존 호출부 호환용 래퍼)
    # ----------------------------------------------------------------------

    async def _check_symbol(self, symbol: str):
        """
        기존 호출부와의 호환성을 위해 5분봉 평가 로직을 _evaluate_tf("5m")으로 대체.
        실제 모든 신호 및 추세 평가는 _evaluate_tf에서 수행됩니다.
        """
        # self._evaluate_tf가 5m/30m 데이터 조회, 추세 분석, 신호 발행까지 모두 처리합니다.
        await self._evaluate_tf(symbol, "5m")

        # 기존 로직 (데이터 조회, 시간 체크, SELL/BUY 평가)은 모두 _evaluate_tf 내부로 이동

    # ----------------------------------------------------------------------
    # MACD 버스 이벤트 핸들러
    # ----------------------------------------------------------------------
    def _on_macd_series_ready(self, payload: dict):
        """
        macd_calculator.apply_rows_full/append 완료 이벤트.
        해당 TF(보통 30m)의 시리즈가 감지되면 그 종목을 추적 대상에 등록.
        """
        try:
            code = _code6(payload.get("code") or "")
            tf   = str(payload.get("tf") or "").lower()
            if not code or tf != self.macd30_timeframe.lower():  # "30m"만 추적
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

    # ----------------------------------------------------------------------
    # 루프 시작
    # ----------------------------------------------------------------------
    async def start(self):
        self._loop = asyncio.get_running_loop()
        logger.info("[ExitEntryMonitor] 모니터링 시작")
        while True:
            try:
                now_kst = pd.Timestamp.now(tz=self.tz)

                # 5분봉 마감 구간에서만 평가 실행
                if TimeRules.is_5m_bar_close_window(now_kst, self._win_start, self._win_end):
                    symbols_snapshot = self._get_symbols_snapshot()
                    if not symbols_snapshot:
                        logger.debug("[ExitEntryMonitor] no symbols to check (snapshot empty)")
                    else:
                        # 5분봉 마감 주기 로그 (debug 레벨로 유지)
                        logger.debug(
                            f"[ExitEntryMonitor] 5분봉 마감 구간 @ {now_kst} | symbols={len(symbols_snapshot)}"
                        )
                        tasks = []
                        for s in symbols_snapshot:
                            # 5분봉 평가 (신호 + 추세)
                            tasks.append(self._evaluate_tf(s, "5m")) 
                            # 30분봉 평가 (추세만 갱신)
                            tasks.append(self._evaluate_tf(s, "30m")) 

                        # 심볼별/TF별 병렬 평가 실행
                        await asyncio.gather(*tasks, return_exceptions=True)
                
            except Exception as e:
                # 루프 실행 중 발생한 예외 처리
                logger.exception(f"[ExitEntryMonitor] 루프 오류: {e}")

            await asyncio.sleep(self.poll_interval_sec)
    # ----------------------------------------------------------------------
    # 🔵 추세 분석 헬퍼 (강력 돌파 기준으로 수정)
    # ----------------------------------------------------------------------
    def _get_trend_message(self, symbol: str, timeframe: str, df: pd.DataFrame) -> str:
        """
        봉 마감가를 기준으로 추세 메시지 반환. (최소 2봉 필요)
        
        새로운 정의:
        - 추세 상승: 현재 종가 > max(직전 시가, 직전 종가)
        - 추세 하락: 현재 종가 < min(직전 시가, 직전 종가)
        - 추세 유지: 현재 종가 (직전 시가, 직전 종가) 사이에 위치
        """
        if df is None or len(df) < 2:
            return ""

        sym = _code6(symbol)
        tf = timeframe
        last = df.iloc[-1]
        prev = df.iloc[-2]

        cur_close = float(last["Close"])
        prev_open = float(prev["Open"])
        prev_close = float(prev["Close"])
        
        # 1. 이전 봉의 영역 설정
        prev_min = min(prev_open, prev_close) # 직전 봉의 몸통 최저가
        prev_max = max(prev_open, prev_close) # 직전 봉의 몸통 최고가
        prev_is_bear = prev_close < prev_open # 음봉 여부

        # 2. 추세 판별
        trend_msg = "추세 중립/불확실" # 기본값

        # -------------------------------------------------------------
        # 2-1. 🚀 '추세 상승' 조건 (강력 돌파)
        #   : 현재 종가가 직전 봉의 몸통 최고가보다 높을 때
        # -------------------------------------------------------------
        if cur_close > prev_max:
            trend_msg = f"추세 상승: 직전 봉 몸통 ({prev_max:.2f}) 상방 강력 돌파 마감"
        
        # -------------------------------------------------------------
        # 2-2. 📉 '추세 하락' 조건 (강력 돌파)
        #   : 현재 종가가 직전 봉의 몸통 최저가보다 낮을 때
        # -------------------------------------------------------------
        elif cur_close < prev_min:
            trend_msg = f"추세 하락: 직전 봉 몸통 ({prev_min:.2f}) 하방 강력 이탈 마감"

        # -------------------------------------------------------------
        # 2-3. ↔️ '추세 유지' 조건 (추가된 로직)
        #   : 현재 종가가 직전 봉의 몸통 내부에 존재할 때
        # -------------------------------------------------------------
        elif prev_min <= cur_close <= prev_max:
            if prev_is_bear:
                trend_msg = "추세 유지: 직전 음봉 몸통 내 마감 (약한 반등 또는 횡보)"
            else:
                trend_msg = "추세 유지: 직전 양봉 몸통 내 마감 (약한 조정 또는 횡보)"
            
        # 3. 메시지 포맷
        return f"[{tf}] {sym} @ {last.name.strftime('%H:%M')} | {trend_msg} (종가: {cur_close:.2f})"


    # ----------------------------------------------------------------------
    # UI 로그 전송 헬퍼 (bridge가 있는 경우)
    # ----------------------------------------------------------------------
    def _log_trend(self, msg: str):
        try:
            if self.bridge and hasattr(self.bridge, "log"):
                self.bridge.log.emit(f"📈 {msg}")
                logger.info(f"📈 {msg}")
        except Exception:
            pass



    # ----------------------------------------------------------------------
    # 심볼 평가 (SELL 전략 적용) + Pro 분기 (5m, 30m 모두에서 호출)
    # ----------------------------------------------------------------------

    # 💡 참고: 기존 _get_5m 함수를 사용하되, timeframe 인수를 받아 처리하도록 확장해야 합니다.
    # 아래 코드에서는 편의상 별도의 통합 조회 함수를 호출하는 것으로 가정합니다.
    async def _get_bars_for_evaluation(self, symbol: str, timeframe: str, count: int = 200) -> Optional[pd.DataFrame]:
        """5m와 30m 데이터를 캐시 우선으로 조회하는 통합 헬퍼 (구현은 생략)."""
        if timeframe == "5m":
            return await self._get_5m(symbol, count=count)
        else:
            # 30m 데이터 조회 로직 (기존 _get_5m 복사 및 interval='30m' 수정 필요)
            sym = _code6(symbol)
            key = (sym, timeframe)
            with self._sym_lock:
                df_cache = self._bars_cache.get(key)
            if df_cache is not None and not df_cache.empty:
                return df_cache.iloc[-count:] if len(df_cache) > count else df_cache
            # pull 로직은 detail_getter를 사용하여 구현되어야 함.
            try:
                 df_pull = await self.detail_getter.get_bars(code=sym, interval=timeframe, count=count)
                 if df_pull is not None and not df_pull.empty:
                    # 형식 보정 및 캐시 저장 로직 (ingest_bars 참고)
                    return df_pull
            except Exception:
                 pass
            return None


    async def _evaluate_tf(self, symbol: str, timeframe: str):
            try:
                sym = _code6(symbol)
                tf  = timeframe.lower()
                trend_key = (sym, tf) # (sym, 5m) 또는 (sym, 30m)
                
                # 1. 데이터 조회 (생략)
                df_bars = await self._get_bars_for_evaluation(sym, tf) 
                if df_bars is None or df_bars.empty or len(df_bars) < 2:
                    return
                
                now_kst = pd.Timestamp.now(tz=self.tz)
                
                # 2. 5m 봉 마감 구간 체크 (5m 평가만 해당)
                if tf == "5m":
                    if not TimeRules.is_5m_bar_close_window(now_kst, self._win_start, self._win_end):
                        return

                ref_ts = df_bars.index[-1]
                last_close = float(df_bars["Close"].iloc[-1])

                # ==============================================================
                # 4. 추세 상태 결정, 갱신 및 로깅
                # ==============================================================
                
                trend_msg = self._get_trend_message(sym, tf, df_bars)
                self._log_trend(trend_msg) # UI 로그 전송

                # 4-1. 단순 추세 상태 결정 ('UP', 'DOWN', 'HOLD', 'NEUTRAL')
                current_trend: Literal['UP', 'DOWN', 'HOLD', 'NEUTRAL']
                if "추세 상승" in trend_msg:
                    current_trend = 'UP'
                elif "추세 하락" in trend_msg:
                    current_trend = 'DOWN'
                elif "추세 유지" in trend_msg:
                    current_trend = 'HOLD'
                else:
                    current_trend = 'NEUTRAL' 
                
                # 4-2. 직전 추세 상태 로드 및 현재 상태 저장
                previous_trend = self._last_trend.get(trend_key, 'NEUTRAL')
                self._last_trend[trend_key] = current_trend # 현재 상태 저장
                
                logger.debug(f"[Monitor] {sym} {tf} 추세: Prev={previous_trend}, Curr={current_trend}")


                # 5. 5분봉: BUY/SELL 신호 평가 (5m 평가에서만 진행)
                if tf == "5m":
                    # ===============================================
                    # 🔵 SELL 평가 진입 (auto_sell 체크)
                    # ===============================================
                    if self.custom.auto_sell:
                        
                        if self.custom.sell_pro:
                            # 🔴 [분기 로그] SELL PRO ON
                            logger.debug(f"[Monitor] {sym} SELL: Pro ON. Checking Trend Reversal/Custom Rule.")
                            should_sell = False
                            reason = ""
                            
                            # 🔴 [Pro 전략] 추세 상승/유지 (UP/HOLD) -> 추세 하락 (DOWN) 전환 시 매도
                            if previous_trend in ('UP', 'HOLD') and current_trend == 'DOWN':
                                should_sell = True
                                reason = "SELL(Pro Trend Reversal: ->DOWN)"
                                logger.info(f"📣 [Monitor] {sym} SELL SIGNAL: Pro Trend Reversal ({previous_trend}->{current_trend})")
                            
                            # [기존 로직] 전환이 아닐 경우, 주입된 일반 SELL 룰 체크
                            elif not should_sell: 
                                avg_buy = self._get_avg_buy(sym)
                                ctx = {
                                    "side": "SELL", "symbol": sym, "price": last_close, "df5": df_bars, 
                                    "avg_buy": avg_buy, "ts": ref_ts, "source": "bar",
                                }
                                try:
                                    should_sell = bool(self._sell_rule_fn(ctx))
                                except Exception as e:
                                    logger.warning(f"[Monitor] {sym} sell_rule error: {e} → treat as False")
                                    should_sell = False

                                if should_sell:
                                    reason = "SELL(Pro Rule)" + (f": +3% vs avg({avg_buy:.2f}) & pattern" if avg_buy else "")
                                    logger.info(f"📣 [Monitor] {sym} SELL SIGNAL: Pro Custom Rule Triggered.")

                            if should_sell:
                                self._emit("SELL", sym, ref_ts, last_close, reason)
                        
                        else:
                            # 🔴 [분기 로그] SELL PRO OFF
                            logger.debug(f"[Monitor] {sym} SELL: Pro OFF. Periodic SELL suppressed.")
                            pass # sell_pro=False → periodic SELL suppressed

                    # ===============================================
                    # 🔵 BUY 평가 진입 (auto_buy 체크)
                    # ===============================================
                    if self.custom.auto_buy:
                        
                        if self.custom.buy_pro:
                            # 🔴 [분기 로그] BUY PRO ON (추세 전환 / Custom Rule 체크)
                            logger.debug(f"[Monitor] {sym} BUY: Pro ON. Checking Trend Reversal/Custom Rule.")
                            should_buy = False
                            reason = ""
                            
                            # 🔴 [Pro 전략] 추세 하락/유지 (DOWN/HOLD) -> 추세 상승 (UP) 전환 시 매수
                            if previous_trend in ('DOWN', 'HOLD') and current_trend == 'UP':
                                should_buy = True
                                reason = "BUY(Pro Trend Reversal: ->UP)"
                                logger.info(f"📣 [Monitor] {sym} BUY SIGNAL: Pro Trend Reversal ({previous_trend}->{current_trend})")
                            
                            # [기존 로직] 전환이 아닐 경우, 주입된 일반 BUY 룰 체크
                            elif not should_buy:
                                ctx = {
                                    "side": "BUY", "symbol": sym, "price": last_close, "df5": df_bars, 
                                    "ts": ref_ts, "source": "bar",
                                }
                                try:
                                    should_buy = bool(self._buy_rule_fn(ctx))
                                except Exception as e:
                                    logger.warning(f"[Monitor] {sym} buy_rule error: {e} → pass-through(True)")
                                    should_buy = True 

                                if should_buy and not reason:
                                    reason = "BUY(Pro Rule)"
                                    logger.info(f"📣 [Monitor] {sym} BUY SIGNAL: Pro Custom Rule Triggered.")


                            if should_buy:
                                self._emit("BUY", sym, ref_ts, last_close, reason)

                        
                        else:
                            # 🔴 [분기 로그] BUY PRO OFF (요청 사항: 즉시 신호 발행)
                            logger.debug(f"[Monitor] {sym} BUY: Pro OFF. Emitting immediate signal (No condition check).")
                            
                            # 📌 BUY PRO OFF: 조건 체크 없이 즉시 신호 발행
                            reason = "BUY(Legacy Bar Close Immediate)"
                            logger.info(f"📣 [Monitor] {sym} BUY SIGNAL: Legacy Immediate Rule Triggered (buy_pro=False).")
                            self._emit("BUY", sym, ref_ts, last_close, reason)                                
            except Exception:
                logger.exception(f"[ExitEntryMonitor] _evaluate_tf error: {symbol}")