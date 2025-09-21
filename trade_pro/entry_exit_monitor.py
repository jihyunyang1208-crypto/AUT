# core/exit_monitor.py
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Protocol

import pandas as pd
import json
from pathlib import Path

# ──────────────────────────────
# Logger
# ──────────────────────────────
logger = logging.getLogger(__name__)

# ===== 결과 집계 & 저장 유틸 =====
class DailyResultsRecorder:
    """
    - on_signal 콜백에 연결해서 BUY/SELL 신호를 수집
    - 날짜별 파일(data/system_results_YYYY-MM-DD.json)로 저장
    - 프로그램 종료/일자 변경 시에도 안전하게 flush 가능
    - JSON 스키마:
      {
        "date": "YYYY-MM-DD",
        "app": "ExitPro",
        "generated_at": "YYYY-MM-DD HH:MM:SS",
        "summary": {"buys":int, "sells":int, "pnl_estimate": null},
        "signals": [
          {"side":"BUY|SELL","symbol":"005930","ts":"ISO8601","price":float,"reason":"..."}
        ],
        "meta": {"timezone":"Asia/Seoul"}
      }
    """
    def __init__(self, out_dir: str = "data", tz: str = "Asia/Seoul", app_name: str = "ExitPro"):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.tz = tz
        self.app_name = app_name
        self._today = self._today_str()
        self._data = self._new_day_blob()

    def _today_str(self) -> str:
        return pd.Timestamp.now(tz=self.tz).strftime("%Y-%m-%d")

    def _new_day_blob(self) -> dict:
        return {
            "date": self._today,
            "app": self.app_name,
            "generated_at": pd.Timestamp.now(tz=self.tz).strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "buys": 0,
                "sells": 0,
                "pnl_estimate": None,
            },
            "signals": [],
            "meta": {"timezone": self.tz}
        }

    def _rollover_if_new_day(self):
        now = self._today_str()
        if now != self._today:
            self.flush()
            self._today = now
            self._data = self._new_day_blob()

    def record_signal(self, sig) -> None:
        """
        sig: TradeSignal dataclass
        """
        self._rollover_if_new_day()
        # tz-aware ISO8601로 정규화
        ts = sig.ts
        if ts.tzinfo is None:
            ts = ts.tz_localize(self.tz)
        else:
            ts = ts.tz_convert(self.tz)

        item = {
            "side": str(sig.side).upper(),
            "symbol": str(sig.symbol),
            "ts": ts.isoformat(),
            "price": float(sig.price),
            "reason": str(sig.reason),
        }
        self._data["signals"].append(item)

        if item["side"] == "BUY":
            self._data["summary"]["buys"] += 1
        elif item["side"] == "SELL":
            self._data["summary"]["sells"] += 1

        # 안전하게 즉시 저장 (원하면 배치 저장으로 변경 가능)
        self.flush()

    def flush(self):
        out = self.out_dir / f"system_results_{self._today}.json"
        self._data["generated_at"] = pd.Timestamp.now(tz=self.tz).strftime("%Y-%m-%d %H:%M:%S")
        out.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"[DailyResultsRecorder] 💾 Saved: {out}")


# ===== 외부 프로토콜 =====
class DetailInformationGetter(Protocol):
    async def get_bars(self, code: str, interval: str, count: int) -> pd.DataFrame:
        """
        반환: index = tz-aware datetime(Asia/Seoul 권장)
              columns = ['Open','High','Low','Close','Volume']
        """
        ...


class IMacdPointsFeed(Protocol):
    def get_points(self, symbol: str, timeframe: str, n: int = 1) -> List[dict]:
        """
        최근 n개 MACD 포인트 반환 (오름차순 보장 권장)
        각 포인트 dict 예:
          {"ts": pd.Timestamp, "macd": float, "signal": float, "hist": float}
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
        """
        return (now_kst.minute % 5 == 0) and (5 <= now_kst.second <= 30)


# ========== 모니터러 본체 ==========
class ExitEntryMonitor:
    """
    - 5분봉 종가 기준으로 매수/매도 신호 판단
    - (옵션) 30분 MACD 히스토그램 >= 0 필터 (get_points 단일 API 사용)
    - 동일 봉 중복 트리거 방지
    - 봉 마감 구간에서만 평가
    - 'report_daily_md.py' 실행 트리거 없이 JSON만 기록합니다.
    """
    def __init__(
        self,
        detail_getter: DetailInformationGetter,
        macd_feed: IMacdPointsFeed,             # ✅ 단일 API(get_points)
        symbols: List[str],
        settings: TradeSettings,
        *,
        use_macd30_filter: bool = False,
        macd30_timeframe: str = "30m",
        macd30_max_age_sec: int = 1800,  # 30분봉 신선도 권장값
        tz: str = "Asia/Seoul",
        poll_interval_sec: int = 20,
        on_signal: Optional[Callable[[TradeSignal], None]] = None,
        results_recorder: Optional[DailyResultsRecorder] = None,
        bridge: Optional[object] = None,
    ):
        self.detail_getter = detail_getter
        self.macd_feed = macd_feed
        self.symbols = symbols
        self.settings = settings
        self.bridge = bridge
        self.use_macd30_filter = use_macd30_filter
        self.macd30_timeframe = macd30_timeframe
        self.macd30_max_age_sec = macd30_max_age_sec

        self.tz = tz
        self.poll_interval_sec = poll_interval_sec
        self.on_signal = on_signal or (lambda sig: logger.info(f"[SIGNAL] {sig}"))
        self.results_recorder = results_recorder

        # (symbol, side) → 마지막 트리거된 봉 ts
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

        try:
            pts = self.macd_feed.get_points(symbol, self.macd30_timeframe, n=1) or []
        except Exception as e:
            logger.error(f"[ExitEntryMonitor] get_points 에러: {symbol} {self.macd30_timeframe}: {e}")
            return False

        if not pts:
            logger.debug(f"[ExitEntryMonitor] {symbol} NO MACD30 → failed filtering")
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

    def _emit(self, side: str, symbol: str, ts: pd.Timestamp, price: float, reason: str):
        key = (symbol, side)
        if self._last_trig.get(key) == ts:
            logger.debug(f"[ExitEntryMonitor] {symbol} {side} 신호 중복(ts={ts}) → 무시")
            return
        self._last_trig[key] = ts

        # bridge 로그 안전 호출
        try:
            if self.bridge and hasattr(self.bridge, "log"):
                self.bridge.log.emit(f"[ExitEntryMonitor] 📣 신호 발생 {side} {symbol} {price:.2f} @ {ts} | {reason}")
        except Exception:
            pass

        sig_obj = TradeSignal(side, symbol, ts, price, reason)

        # 1) 외부 콜백 호출
        self.on_signal(sig_obj)

        # 2) JSON 기록 (리포트 트리거 없음)
        if self.results_recorder:
            try:
                self.results_recorder.record_signal(sig_obj)
            except Exception as e:
                logger.exception(f"[ExitEntryMonitor] 기록 실패: {e}")

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

        # 매도: 현재 5분봉 종가 < 직전 5분봉 시가
        if self.settings.master_enable and self.settings.auto_sell:
            if last_close < prev_open:
                reason = f"SELL: Close<{prev_open:.2f} (prev open)" + (" + MACD30(hist>=0)" if self.use_macd30_filter else "")
                self._emit("SELL", symbol, ref_ts, last_close, reason)

        # (선택) 예시 매수 룰
        if self.settings.master_enable and self.settings.auto_buy:
            buy = BuyRules.buy_if_5m_break_prev_bear_high(df5).iloc[-1]
            if bool(buy) and (not self.use_macd30_filter or self._macd30_pass(symbol, ref_ts)):
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
