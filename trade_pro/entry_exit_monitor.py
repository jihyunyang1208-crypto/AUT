# trade_pro/entry_exit_monitor.py
from __future__ import annotations

import asyncio
from asyncio import run_coroutine_threadsafe
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol, Tuple

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
# 결과 집계 & 저장 유틸 (일별 JSONL)
# ============================================================================
class DailyResultsRecorder:
    def __init__(self, out_dir: str = "data/results", tz: str = "Asia/Seoul"):
        self.out_dir = Path(out_dir)
        self.tz = tz
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path_for_today(self) -> Path:
        now = pd.Timestamp.now(tz=self.tz)
        return self.out_dir / f"system_results_{now.strftime('%Y-%m-%d')}.jsonl"

    def record_signal(self, sig: "TradeSignal"):
        payload = {
            "ts": sig.ts.isoformat(),
            "side": sig.side,
            "symbol": sig.symbol,
            "price": sig.price,
            "reason": sig.reason,
            "source": getattr(sig, "source", "bar"),
            "condition_name": getattr(sig, "condition_name", ""),
            "return_msg": getattr(sig, "return_msg", None), 
        }
        with self._lock:
            p = self._path_for_today()
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")


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


# ============================================================================
# 룰
# ============================================================================
class BuyRules:
    @staticmethod
    def buy_if_5m_break_prev_bear_high(df5: pd.DataFrame) -> pd.Series:
        """
        조건:
        - 1봉 전: 음봉 (Close < Open)
        - 현재봉: 양봉 (Close > Open)
        - 현재봉 고가 > 직전(음봉) 고가
        """
        if df5 is None or df5.empty:
            return pd.Series(dtype=bool)
        prev = df5.shift(1)
        cond_bear  = prev["Close"] < prev["Open"]
        cond_bull  = df5["Close"] > df5["Open"]
        cond_break = df5["High"]  > prev["High"]
        cond = cond_bear & cond_bull & cond_break
        if len(cond) > 0:
            cond.iloc[0] = False
        return cond


class SellRules:
    @staticmethod
    def sell_if_close_below_prev_open(df5: pd.DataFrame) -> pd.Series:
        """
        조건:
        - 직전 봉: 음봉 (prev.Close < prev.Open)
        - 현재 봉: 종가 <= 직전 봉 종가
        """
        if df5 is None or df5.empty:
            return pd.Series(dtype=bool)

        prev = df5.shift(1)

        cond_prev_bear = prev["Close"] < prev["Open"]
        cond_close_lte_prev_close = df5["Close"] <= prev["Close"]

        cond = cond_prev_bear & cond_close_lte_prev_close

        if len(cond) > 0:
            cond.iloc[0] = False  # 첫 행은 직전 봉이 없으므로 False

        return cond

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
class ExitEntryMonitor:
    """
    - 5분봉 종가 기준으로 매수/매도 신호 판단
    - (옵션) 30분 MACD 히스토그램 >= 0 필터
      ↳ get_points_fn(symbol, "30m", 1) 로 조회
    - 동일 봉 중복 트리거 방지
    - 봉 마감 구간에서만 평가
    - JSON 기록
    - 🔧 캐시 우선 설계: ingest_bars()로 들어온 DF를 먼저 활용, 없을 때만 pull
    - 🔔 조건검색(편입) 즉시 트리거 → TradeSignal로 통합 발행
    """
    def __init__(
        self,
        detail_getter: DetailGetter,
        *,
        use_macd30_filter: bool = False,
        macd30_timeframe: str = "30m",
        macd30_max_age_sec: int = 1800,  # 30분
        tz: str = "Asia/Seoul",
        poll_interval_sec: int = 20,
        on_signal: Optional[Callable[[TradeSignal], None]] = None,
        results_recorder: Optional[DailyResultsRecorder] = None,
        bridge: Optional[object] = None,
        get_points_fn: Callable[[str, str, int], List[dict]] = _get_points,
        bar_close_window_start_sec: int = 5,
        bar_close_window_end_sec: int = 30,
        disable_server_pull: bool = False,   # 💡 캐시만 사용하고 싶을 때 True
        custom: Optional[MonitorCustom] = None,  # 💡 고급 커스텀
    ):
        self.detail_getter = detail_getter
        self.bridge = bridge
        self.use_macd30_filter = use_macd30_filter
        self.macd30_timeframe = macd30_timeframe
        self.macd30_max_age_sec = macd30_max_age_sec
        self.get_points_fn = get_points_fn

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.tz = tz
        self.poll_interval_sec = poll_interval_sec
        self.on_signal = on_signal or (lambda sig: logger.info(f"[SIGNAL] {sig}"))
        self.results_recorder = results_recorder
        self.disable_server_pull = bool(disable_server_pull)
        self.custom = custom or MonitorCustom()

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

        # MACD 버스 구독 (30m 시리즈 준비되면 추적에 추가)
        try:
            macd_bus.macd_series_ready.connect(self._on_macd_series_ready)
            logger.info("[ExitEntryMonitor] tracking symbols from MACD bus: tf=%s", self.macd30_timeframe)
        except Exception as e:
            logger.warning("[ExitEntryMonitor] macd_bus connect failed: %s", e)

    # ----------------------------------------------------------------------
    # 고급 커스텀 설정 업데이트
    # ----------------------------------------------------------------------
    def set_custom(
        self,
        *,
        enabled: bool | None = None,
        auto_buy: bool | None = None,
        auto_sell: bool | None = None,
        allow_intrabar_condition_triggers: bool | None = None,
    ):
        if enabled is not None:
            self.custom.enabled = bool(enabled)
        if auto_buy is not None:
            self.custom.auto_buy = bool(auto_buy)
        if auto_sell is not None:
            self.custom.auto_sell = bool(auto_sell)
        if allow_intrabar_condition_triggers is not None:
            self.custom.allow_intrabar_condition_triggers = bool(allow_intrabar_condition_triggers)

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
    # MACD 30m 필터
    # ----------------------------------------------------------------------
    def _macd30_pass(self, symbol: str, ref_ts: pd.Timestamp) -> bool:
        if not self.use_macd30_filter:
            return True

        try:
            pts = self.get_points_fn(symbol, self.macd30_timeframe, n=1) or []
        except Exception as e:
            logger.error(f"[ExitEntryMonitor] get_points 에러: {symbol} {self.macd30_timeframe}: {e}")
            return False

        if not pts:
            logger.debug(f"[ExitEntryMonitor] {symbol} MACD30 not ready yet → skip this bar")
            return False

        info = pts[-1]
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

        # 2) JSON 기록
        if self.results_recorder:
            try:
                self.results_recorder.record_signal(sig_obj)
            except Exception as e:
                logger.exception(f"[ExitEntryMonitor] 기록 실패: {e}")

    # ----------------------------------------------------------------------
    # 조건검색 '편입(I)' 즉시 트리거 → TradeSignal 통합 발행
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
        - custom.enabled & allow_intrabar_condition_triggers 일 때만 즉시 평가/발행
        - auto_buy/auto_sell 토글에 따라 BUY/SELL 선택
        - 가격은 5분봉 캐시 또는 pull 결과의 마지막 종가 사용
        """
        try:
            # 추적 목록에는 추가해 둔다(이후 정규루프에서도 평가 가능)
            sym = _code6(symbol)
            with self._sym_lock:
                self._symbols.add(sym)

            if not (self.custom.enabled and self.custom.allow_intrabar_condition_triggers):
                logger.debug(f"[Monitor] custom disabled or intrabar not allowed → skip immediate ({sym})")
                return

            df5 = await self._get_5m(sym, count=200)
            if df5 is None or df5.empty:
                logger.debug(f"[Monitor] {sym} 즉시트리거: 5m 없음 → skip")
                return

            ref_ts = df5.index[-1]
            last_close = float(df5["Close"].iloc[-1])

            # MACD30 필터
            if self.use_macd30_filter and not self._macd30_pass(sym, ref_ts):
                logger.debug(f"[Monitor] {sym} 즉시트리거: MACD30 fail → skip")
                return

            # 사이드 결정
            side = None
            if self.custom.auto_buy:
                side = "BUY"
            elif self.custom.auto_sell:
                side = "SELL"

            if side is None:
                logger.debug(f"[Monitor] {sym} 즉시트리거: side 토글 없음 → skip")
                return

            sig = TradeSignal(
                side=side,
                symbol=sym,
                ts=ref_ts,
                price=last_close,
                reason=reason,
                source=source,
                condition_name=condition_name,
                extra={"immediate": True},
            )

            # 외부 콜백(오토트레이더 on_signal 등)
            try:
                self.on_signal(sig)
            except Exception:
                logger.exception("[ExitEntryMonitor] on_signal handler error (immediate)")

            # 로그/저장
            try:
                if self.bridge and hasattr(self.bridge, "log"):
                    self.bridge.log.emit(f"📊 즉시신호 [{side}] {sym} @ {last_close} ({condition_name})")
            except Exception:
                pass

            if self.results_recorder:
                try:
                    self.results_recorder.record_signal(sig)
                except Exception as e:
                    logger.exception(f"[ExitEntryMonitor] 기록 실패(immediate): {e}")

        except Exception:
            logger.exception(f"[Monitor] on_condition_detected error: {symbol}")

    # ----------------------------------------------------------------------
    # 심볼 평가
    # ----------------------------------------------------------------------
    async def _check_symbol(self, symbol: str):
        try:
            sym = _code6(symbol)

            df5 = await self._get_5m(sym)
            if df5 is None or df5.empty:
                logger.debug(f"[ExitEntryMonitor] {sym} no 5m data")
                return

            # 1) 최소 행수/필수 컬럼 체크
            need_cols = {"Open", "High", "Low", "Close", "Volume"}
            if not need_cols.issubset(df5.columns):
                logger.debug(f"[ExitEntryMonitor] {sym} missing columns for 5m: {set(df5.columns)}")
                return
            if len(df5) < 2:
                logger.debug(f"[ExitEntryMonitor] {sym} not enough 5m bars (need>=2, got={len(df5)})")
                return

            ref_ts = df5.index[-1]

            # 2) (보수적) 5분봉 마감창에서만 평가
            now_kst = pd.Timestamp.now(tz=self.tz)
            if not TimeRules.is_5m_bar_close_window(now_kst, self._win_start, self._win_end):
                logger.debug(f"[ExitEntryMonitor] {sym} skip (not in 5m close window)")
                return

            last_close = float(df5["Close"].iloc[-1])
            prev_open  = float(df5["Open"].iloc[-2])

            # 3) NaN 가드
            if pd.isna(last_close) or pd.isna(prev_open):
                logger.debug(f"[ExitEntryMonitor] {sym} NaN in last_close/prev_open -> skip")
                return

            # 4) MACD30 필터
            macd_ok = (not self.use_macd30_filter) or self._macd30_pass(sym, ref_ts)
            if not macd_ok:
                logger.debug(f"[ExitEntryMonitor] {sym} skip: MACD30 filter")
                return

            # ----- SELL -----
            if last_close < prev_open:
                reason = "SELL: Close < prev Open" + (" + MACD30(hist>=0)" if self.use_macd30_filter else "")
                self._emit("SELL", sym, ref_ts, last_close, reason)
            else:
                logger.debug(f"[ExitEntryMonitor] {sym} no SELL (last={last_close:.2f} prevOpen={prev_open:.2f})")

            # ----- BUY -----
            # buy_series = BuyRules.buy_if_5m_break_prev_bear_high(df5)
            # will_buy = bool(buy_series.iloc[-1]) if len(buy_series) else False
            # if will_buy:
            reason = "BUY: Bull breaks prev bear High" + (" + MACD30(hist>=0)" if self.use_macd30_filter else "")
            self._emit("BUY", sym, ref_ts, last_close, reason)
            # else:
            #    logger.debug(f"[ExitEntryMonitor] {sym} no BUY (rule/bool={will_buy})")

        except Exception:
            logger.exception(f"[ExitEntryMonitor] _check_symbol error: {symbol}")

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

                if TimeRules.is_5m_bar_close_window(now_kst, self._win_start, self._win_end):
                    symbols_snapshot = self._get_symbols_snapshot()
                    if not symbols_snapshot:
                        logger.debug("[ExitEntryMonitor] no symbols to check (snapshot empty)")
                    else:
                        logger.debug(
                            f"[ExitEntryMonitor] 5분봉 마감 구간 @ {now_kst} | symbols={len(symbols_snapshot)}"
                        )
                        # 심볼별 병렬 평가
                        await asyncio.gather(
                            *(self._check_symbol(s) for s in symbols_snapshot),
                            return_exceptions=True,
                        )
            except Exception as e:
                logger.exception(f"[ExitEntryMonitor] 루프 오류: {e}")

            await asyncio.sleep(self.poll_interval_sec)
