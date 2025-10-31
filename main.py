# main.py
from __future__ import annotations

import sys
import os
import json
import logging
import asyncio
import threading
from datetime import datetime, date, timedelta
from typing import Dict, Optional, List

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtWidgets import QApplication
from matplotlib import rcParams

# ---- 앱 유틸/코어 ----
from utils.utils import load_api_keys  # (다른 곳에서 사용할 수 있어 보존)
from utils.token_manager import (
    # ✅ 메인 토큰은 반드시 이 함수로 획득
    get_main_token,
    # 필요 시(백업 경로) 전역 supplier 구성
    build_token_supplier,
    set_global_token_supplier,
    load_keys,  # .env 백업 경로용
)

from core.websocket_client import WebSocketClient
from strategy.filter_1_finance import run_finance_filter
from strategy.filter_2_technical import run_technical_filter
from core.detail_information_getter import (
    DetailInformationGetter,
    SimpleMarketAPI,
    normalize_ka10080_rows,
    _rows_to_df_ohlcv,
)
from core.macd_calculator import calculator, macd_bus

# ---- UI ----
from ui_main import MainWindow

# ---- trade_pro 모듈 ----
from trade_pro.entry_exit_monitor import ExitEntryMonitor

# ---- 설정: settings_manager에 일원화 ----
from setting.settings_manager import (
    SettingsStore, AppSettings, SettingsDialog,
    to_trade_settings, to_ladder_settings, apply_to_autotrader, apply_all_settings
)

# ─────────────────────────────────────────────────────────
# 한글 폰트 설정
# ─────────────────────────────────────────────────────────
def _setup_korean_font():
    import platform
    sysname = platform.system()
    if sysname == "Windows":
        rcParams["font.family"] = "Malgun Gothic"
    elif sysname == "Darwin":
        rcParams["font.family"] = "AppleGothic"
    else:
        rcParams["font.family"] = "NanumGothic"
    rcParams["axes.unicode_minus"] = False

_setup_korean_font()

# ─────────────────────────────────────────────────────────
# 로거 설정
# ─────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

def setup_logger(to_console: bool = True, to_file: bool = True, log_dir: str = "logs"):
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 기존 핸들러 제거(중복 방지)
    for h in root.handlers[:]:
        root.removeHandler(h)

    handlers: List[logging.Handler] = []
    if to_console:
        handlers.append(logging.StreamHandler())
    if to_file:
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, datetime.now().strftime("app_%Y%m%d.log"))
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=handlers,
    )
    return root

# 이제부터 모든 로거는 이 설정에 따라 동작합니다.
logger = setup_logger(to_console=False, to_file=True)  # 파일만

try:
    project_root  # noqa: F823
except NameError:
    project_root = os.getcwd()

# ─────────────────────────────────────────────────────────
# 유틸: 다음 분/30분 경계까지 남은 초
# ─────────────────────────────────────────────────────────
def _seconds_to_next_boundary(now: datetime, minutes_step: int) -> float:
    """
    now 기준 다음 minutes_step(5, 30 등) 경계까지 남은 초.
    최소 1초 보장.
    """
    base = now.replace(second=0, microsecond=0)
    bucket = (now.minute // minutes_step) * minutes_step
    next_min = bucket + minutes_step
    if next_min >= 60:
        target = (base + timedelta(hours=1)).replace(minute=(next_min % 60))
    else:
        target = base.replace(minute=next_min)
    return max(1.0, (target - now).total_seconds())

# 모니터 스레드 기동 헬퍼
def start_monitor_on_thread(monitor: ExitEntryMonitor):
    def _runner():
        asyncio.run(monitor.start())
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return t

# ─────────────────────────────────────────────────────────
# Bridge: 비UI 스레드 → UI 신호
# ─────────────────────────────────────────────────────────
class AsyncBridge(QObject):
    # 일반 로그
    log = Signal(str)

    # 조건식/신규 종목
    condition_list_received = Signal(list)
    new_stock_received = Signal(str)
    new_stock_detail_received = Signal(dict)

    # 토큰 브로드캐스트
    token_ready = Signal(str)

    # MACD (새 포맷)
    # {"code": str, "tf": "5m"/"30m"/"1d", "series": [{"t","macd","signal","hist"}]}
    macd_series_ready = Signal(dict)

    # 레거시 4-튜플
    macd_data_received = Signal(str, float, float, float)

    # 원시 캔들 rows
    chart_rows_received = Signal(str, str, list)  # code, tf, rows

    # 옵션 신호
    macd_updated = Signal(dict)
    macd_buy_signal = Signal(dict)
    macd_sell_signal = Signal(dict)

    # 5m 원시 rows, 심볼명 등
    minute_bars_received = Signal(str, list)
    symbol_name_updated = Signal(str, str)  # (code6, name)

    # 주문/체결 이벤트 브릿지
    order_event = Signal(dict)
    pnl_snapshot_ready = Signal(dict)
    price_update = Signal(dict)      # 예: {"ts":..., "stock_code":"005930","price":70500}
    fill_or_trade = Signal(dict)     # 예: {"ts":..., "side":"BUY","qty":1,"price":70000,...}

    def __init__(self):
        super().__init__()

# ─────────────────────────────────────────────────────────
# Engine: 토큰/WS/HTTP 초기화 + 5m/30m/1d 병렬 스트림
# ─────────────────────────────────────────────────────────
class Engine(QObject):
    initialization_complete = Signal()

    def __init__(self, bridge: AsyncBridge, getter: DetailInformationGetter, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.getter = getter

        self.monitor: Optional[object] = None  # or: Optional["ExitEntryMonitor"]

        # 별도 asyncio 루프 스레드
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self._run_loop, daemon=True)

        # 자원
        self.access_token: Optional[str] = None
        self.market_api: Optional[SimpleMarketAPI] = None
        self.websocket_client: Optional[WebSocketClient] = None

        # 종목별 병렬 태스크 (5m/30m/1d)
        self._minute_stream_tasks: Dict[str, Dict[str, asyncio.Future]] = {}

    # ── 루프 제어 ──────────────────────────────
    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start_loop(self):
        if not self.loop_thread.is_alive():
            self.loop_thread.start()
            self.bridge.log.emit("🌀 asyncio 루프 시작")

    # ── 초기화 ────────────────────────────────
    def initialize(self):
        if getattr(self, "_initialized", False):
            self.bridge.log.emit("[Engine] initialize: already initialized, skip")
            return
        self._initialized = True

        try:
            # 1) ✅ 메인 토큰: get_main_token() 강제 사용
            try:
                self.access_token = get_main_token()
            except Exception:
                # 전역 supplier가 아직 없다면 .env 키로 백업 공급자 구성
                ak, sk = load_keys()
                if not (ak and sk):
                    raise RuntimeError("전역 토큰 공급자/프로필이 없고 .env APP_KEY/APP_SECRET도 없습니다.")
                supplier = build_token_supplier(app_key=ak, app_secret=sk)
                set_global_token_supplier(supplier)
                # 재시도
                self.access_token = get_main_token()

            self.bridge.log.emit("🔐 액세스 토큰 발급 완료 (main)")

            # 2) HTTP 클라이언트 (토큰 주입)
            if not self.market_api:
                self.market_api = SimpleMarketAPI(token=self.access_token)
            else:
                self.market_api.set_token(self.access_token)
            if not self.getter:
                self.getter = DetailInformationGetter(token=self.access_token)
            else:
                self.getter.token = self.access_token

            # 3) WS 클라이언트 생성 및 시작
            if self.websocket_client is None:
                self.websocket_client = WebSocketClient(
                    uri=os.getenv("WS_URI", "wss://api.kiwoom.com:10000/api/dostk/websocket"),
                    token=self.access_token,
                    bridge=self.bridge,
                    market_api=self.market_api,
                    socketio=None,
                    on_condition_list=self._on_condition_list,
                    dedup_ttl_sec=3,
                    detail_timeout_sec=6.0,
                    # ✅ 재발급도 get_main_token() 경유
                    refresh_token_cb=self._refresh_token_sync,
                )
            self.websocket_client.start(loop=self.loop)
            self.bridge.log.emit("🌐 WebSocket 클라이언트 시작")

            # 4) MACD 버스 → 브릿지 패스스루
            macd_bus.macd_series_ready.connect(self._on_bus_macd_series, Qt.UniqueConnection)

            # 5) UI 통보
            self.initialization_complete.emit()
            self.bridge.token_ready.emit(self.access_token)

        except Exception as e:
            self.bridge.log.emit(f"❌ 초기화 실패: {type(e).__name__}: {e}")
            raise

    def _refresh_token_sync(self) -> Optional[str]:
        """WS 레이어에서 요청하는 동기적 토큰 재발급 콜백: get_main_token() 경유"""
        try:
            new_token = get_main_token()
            if new_token:
                self.access_token = new_token
                if self.market_api:
                    self.market_api.set_token(new_token)
                if self.getter:
                    self.getter.token = new_token
                self.bridge.log.emit("🔁 액세스 토큰 재발급 완료 (main)")
                return new_token
        except Exception as e:
            self.bridge.log.emit(f"❌ 토큰 재발급 실패: {e}")
        return None

    # ── MACD 버스 패스스루 ─────────────────────
    def _on_bus_macd_series(self, payload: dict):
        try:
            # 새 신호 그대로 UI로
            self.bridge.macd_series_ready.emit(payload)

            # 레거시 신호(마지막 포인트만)
            code = str(payload.get("code", ""))
            series = payload.get("series") or []
            if code and series:
                last = series[-1]
                self.bridge.macd_data_received.emit(
                    code,
                    float(last.get("macd")),
                    float(last.get("signal")),
                    float(last.get("hist")),
                )
                logger.info(f"[Engine] macd_series_ready: {code}")
        except Exception as e:
            self.bridge.log.emit(f"⚠️ MACD 패스스루 실패: {e}")

    # ── 조건검색 콜백/제어 ─────────────────────
    def _on_condition_list(self, conditions: list):
        self.bridge.log.emit("[Engine] 조건식 수신")
        self.bridge.condition_list_received.emit(conditions or [])

    def send_condition_search_request(self, seq: str):
        if not self.websocket_client:
            self.bridge.log.emit("⚠️ WebSocket 미초기화")
            return

        async def run():
            await self.websocket_client.send_condition_search_request(seq=seq)

        asyncio.run_coroutine_threadsafe(run(), self.loop)
        self.bridge.log.emit(f"▶️ 조건검색 시작 요청: seq={seq}")

    def remove_condition_realtime(self, seq: str):
        if not self.websocket_client:
            self.bridge.log.emit("⚠️ WebSocket 미초기화")
            return

        async def run():
            await self.websocket_client.remove_condition_realtime(seq=seq)

        asyncio.run_coroutine_threadsafe(run(), self.loop)
        self.bridge.log.emit(f"⏹ 조건검색 중지 요청: seq={seq}")

    # ── 5m/30m/1d 병렬 스트림 ──────────────────
    def start_macd_stream(
        self,
        code: str,
        *,
        poll_5m_step: int = 5,
        poll_30m_step: int = 30,
        need_5m: int = 200,
        need_30m: int = 200,
        need_1d: int = 400,
    ):
        if code in self._minute_stream_tasks:
            logger.info(f"↩️ 이미 스트림 중: {code}")
            return

        def _safe_rows(rows_any) -> list[dict]:
            try:
                if isinstance(rows_any, dict):
                    cand = rows_any.get("rows") or rows_any.get("data") or rows_any.get("bars")
                    return _safe_rows(cand)

                if isinstance(rows_any, list):
                    if not rows_any:
                        return []
                    if isinstance(rows_any[0], dict):
                        return rows_any
                    if isinstance(rows_any[0], str):
                        out = []
                        for s in rows_any:
                            try:
                                obj = json.loads(s)
                                if isinstance(obj, dict):
                                    out.append(obj)
                            except Exception:
                                continue
                        return out
                    return []

                if isinstance(rows_any, str):
                    try:
                        obj = json.loads(rows_any)
                    except Exception:
                        return []
                    return _safe_rows(obj)
            except Exception:
                pass
            return []

        def _extract_rows(any_res) -> list[dict]:
            if isinstance(any_res, dict):
                return _safe_rows(any_res.get("rows") or any_res.get("data") or any_res.get("bars") or [])
            return _safe_rows(any_res)

        async def job_5m():
            try:
                # ① 초기 5분봉 로딩
                res = await asyncio.to_thread(self.getter.fetch_minute_chart_ka10080, code, tic_scope=5, need=need_5m)
                rows5 = _extract_rows(res)
                self.bridge.chart_rows_received.emit(code, "5m", rows5)
                if rows5:
                    rows5_norm = normalize_ka10080_rows(rows5)
                    if rows5_norm:
                        calculator.apply_rows_full(code=code, tf="5m", rows=rows5_norm, need=need_5m)
                        # 초기 데이터도 캐시에 주입
                        try:
                            df_push = _rows_to_df_ohlcv(rows5_norm, tz="Asia/Seoul")
                            mon = getattr(self, "monitor", None) or getattr(self.bridge, "monitor", None)
                            if mon is not None and not df_push.empty:
                                mon.ingest_bars(code, "5m", df_push)
                        except Exception as e:
                            logger.error(f"모니터 데이터 주입 실패({code}): {e}")

                # ② 증분 루프
                while True:
                    await asyncio.sleep(_seconds_to_next_boundary(datetime.now(), poll_5m_step))
                    inc = await asyncio.to_thread(self.getter.fetch_minute_chart_ka10080, code, tic_scope=5, need=60)
                    rows_inc = _extract_rows(inc)
                    if rows_inc:
                        self.bridge.chart_rows_received.emit(code, "5m", rows_inc)
                        rows_inc_norm = normalize_ka10080_rows(rows_inc)
                        if rows_inc_norm:
                            calculator.apply_append(code=code, tf="5m", rows=rows_inc_norm)
                            try:
                                df_push = _rows_to_df_ohlcv(rows_inc_norm, tz="Asia/Seoul")
                                mon = getattr(self, "monitor", None) or getattr(self.bridge, "monitor", None)
                                if mon is not None and not df_push.empty:
                                    mon.ingest_bars(code, "5m", df_push)
                            except Exception as e:
                                logger.error(f"모니터 데이터 주입 실패({code}): {e}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.info(f"⚠️ 5m 스트림 오류({code}): {e}  (type={type(e).__name__})")

        async def job_30m():
            try:
                # 초기 30분봉 로딩
                res = await asyncio.to_thread(self.getter.fetch_minute_chart_ka10080, code, tic_scope=30, need=need_30m)
                rows30 = _extract_rows(res)
                self.bridge.chart_rows_received.emit(code, "30m", rows30)
                if rows30:
                    rows30_norm = normalize_ka10080_rows(rows30)
                    if rows30_norm:
                        calculator.apply_rows_full(code=code, tf="30m", rows=rows30_norm, need=need_30m)
                        # 초기 데이터도 모니터 캐시에 주입
                        try:
                            df_push = _rows_to_df_ohlcv(rows30_norm, tz="Asia/Seoul")
                            mon = getattr(self, "monitor", None) or getattr(self.bridge, "monitor", None)
                            if mon is not None and not df_push.empty:
                                mon.ingest_bars(code, "30m", df_push)
                        except Exception as e:
                            logger.error(f"모니터 데이터 주입 실패({code}): {e}")

                # 증분 루프
                while True:
                    await asyncio.sleep(_seconds_to_next_boundary(datetime.now(), poll_30m_step))
                    inc = await asyncio.to_thread(self.getter.fetch_minute_chart_ka10080, code, tic_scope=30, need=60)
                    rows_inc = _extract_rows(inc)
                    if rows_inc:
                        self.bridge.chart_rows_received.emit(code, "30m", rows_inc)
                        rows_inc_norm = normalize_ka10080_rows(rows_inc)
                        if rows_inc_norm:
                            calculator.apply_append(code=code, tf="30m", rows=rows_inc_norm)
                            try:
                                df_push = _rows_to_df_ohlcv(rows_inc_norm, tz="Asia/Seoul")
                                mon = getattr(self, "monitor", None) or getattr(self.bridge, "monitor", None)
                                if mon is not None and not df_push.empty:
                                    mon.ingest_bars(code, "30m", df_push)
                            except Exception as e:
                                logger.error(f"모니터 데이터 주입 실패({code}): {e}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.info(f"⚠️ 30m 스트림 오류({code}): {e}  (type={type(e).__name__})")

        async def job_1d():
            try:
                today = date.today().strftime("%Y%m%d")
                res = await asyncio.to_thread(self.getter.fetch_daily_chart_ka10081, code, base_dt=today, need=need_1d)
                rows1d = _extract_rows(res)
                self.bridge.chart_rows_received.emit(code, "1d", rows1d)

                if rows1d:
                    rows1d_norm = normalize_ka10080_rows(rows1d) or []
                    if rows1d_norm:
                        calculator.apply_rows_full(code=code, tf="1d", rows=rows1d_norm, need=need_1d)
                        try:
                            df_push = _rows_to_df_ohlcv(rows1d_norm, tz="Asia/Seoul")
                            mon = getattr(self, "monitor", None) or getattr(self.bridge, "monitor", None)
                            if mon is not None and not df_push.empty:
                                mon.ingest_bars(code, "1d", df_push)
                        except Exception as e:
                            logger.error(f"모니터 데이터 주입 실패({code}): {e}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.info(f"⚠️ 1d 초기화 오류({code}): {e}  (type={type(e).__name__})")

        def _submit(coro):
            return asyncio.run_coroutine_threadsafe(coro, self.loop)

        tasks = {"5m": _submit(job_5m()), "30m": _submit(job_30m()), "1d": _submit(job_1d())}
        self._minute_stream_tasks[code] = tasks
        self.bridge.log.emit(f"▶️ MACD 스트림 시작: {code} (5m/30m/1d)")

    def stop_macd_stream(self, code: str):
        tasks = self._minute_stream_tasks.get(code)
        if not tasks:
            return
        for _, fut in list(tasks.items()):
            try:
                fut.cancel()
            except Exception:
                pass
        self._minute_stream_tasks.pop(code, None)
        self.bridge.log.emit(f"🛑 MACD 스트림 중지 요청: {code}")

    # (옵션) 단발 계산
    def update_macd_once(self, code: str, tic_scope: int = 5):
        try:
            res = self.getter.fetch_minute_chart_ka10080(code, tic_scope=tic_scope, need=200)
            rows = res.get("rows") or res.get("data") or res.get("bars") or []
            rows = rows if isinstance(rows, list) else []
            calculator.apply_rows_full(code=code, tf="5m", rows=rows, need=200)
        except Exception:
            logger.exception("update_macd_once failed for %s", code)

    # 종료
    def shutdown(self):
        self.bridge.log.emit("🛑 종료 처리 중...")
        try:
            if self.loop.is_running():
                self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception as e:
            self.bridge.log.emit(f"❌ 루프 종료 오류: {e}")

# ─────────────────────────────────────────────────────────
# 필터 파이프라인
# ─────────────────────────────────────────────────────────
def perform_filtering():
    logger.info("--- 필터링 프로세스 시작 ---")
    today = datetime.now()

    finance_filter_dates = [(4, 1), (5, 16), (8, 15), (11, 15)]
    run_finance_filter_today = any(today.month == m and today.day == d for (m, d) in finance_filter_dates)

    if run_finance_filter_today:
        logger.info(f"오늘은 {today.month}월 {today.day}일. 1단계 금융 필터링을 실행합니다.")
        try:
            run_finance_filter()
            logger.info("금융 필터링 완료. 결과는 %s 에 저장되었습니다.", os.path.join(project_root, "stock_codes.csv"))
        except Exception as e:
            logger.exception("금융 필터링 중 오류 발생")
            raise RuntimeError(f"금융 필터링 실패: {e}")
    else:
        logger.info("오늘은 %d월 %d일. 1단계 금융 필터링 실행일이 아니므로 건너뜁니다.", today.month, today.day)
        logger.info("기존의 %s 파일을 사용합니다.", os.path.join(project_root, "stock_codes.csv"))

    logger.info("기술적 필터 (filter_2_technical.py)를 실행합니다.")
    try:
        stock_codes_path = os.path.join(project_root, "stock_codes.csv")
        candidate_stocks_path = os.path.join(project_root, "candidate_stocks.csv")
        run_technical_filter(input_csv=stock_codes_path, output_csv=candidate_stocks_path)
        logger.info("기술적 필터링 완료. 결과는 %s 에 저장되었습니다.", candidate_stocks_path)
        return candidate_stocks_path
    except Exception as e:
        logger.exception("기술적 필터링 중 오류 발생")
        raise RuntimeError(f"기술적 필터링 실패: {e}")

# ─────────────────────────────────────────────────────────
# 트레이더 팩토리 (설정 → AutoTrader)
# ─────────────────────────────────────────────────────────
def _build_trader_from_cfg(cfg: AppSettings):
    """
    AppSettings -> AutoTrader + KiwoomRestBroker 결선
    - APP_KEY/APP_SECRET: cfg > .env(load_keys) 우선순위
    - ✅ 토큰은 전역 싱글톤 공급자(get_main_token) 경유로만 접근
    - ✅ 브로커는 list_order_accounts_strict() (ENV 최신 리스트) 기반 브로드캐스트
    """
    import os
    from trade_pro.auto_trader import AutoTrader
    from broker.kiwoom import KiwoomRestBroker  # (브로커 내부에서 strict 계정 목록 사용)

    # 1) Settings 변환
    trade_settings = to_trade_settings(cfg)
    ladder_settings = to_ladder_settings(cfg)

    # 2) API 엔드포인트/헤더 ID
    base_url     = (getattr(cfg, "api_base_url", None)   or os.getenv("HTTP_API_BASE", "https://api.kiwoom.com")).rstrip("/")
    order_path   =  getattr(cfg, "api_order_path", None) or "/api/dostk/ordr"
    api_id_buy   =  getattr(cfg, "api_id_buy", None)     or "kt10000"
    api_id_sell  =  getattr(cfg, "api_id_sell", None)    or "kt10001"
    http_timeout = int(getattr(cfg, "http_timeout", 10))

    # 3) (필요 시) 전역 supplier 구성 — 이미 설정돼 있다면 생략
    #    메인 UI에서는 세팅 다이얼로그가 프로필/ENV를 관리하므로,
    #    여기서는 supplier가 없을 때만 .env를 사용해 구성
    try:
        _ = get_main_token()
    except Exception:
        ak, sk = load_keys()
        if ak and sk:
            supplier = build_token_supplier(app_key=ak, app_secret=sk)
            set_global_token_supplier(supplier)
            # 프리워밍으로 조기 오류 감지
            _ = get_main_token()
        else:
            # supplier가 없어도 브로커는 ENV 계정 리스트만으로 동작 가능하나,
            # WS/시세 등 메인 토큰이 필요한 구성에서는 오류가 될 수 있음
            logger.warning("전역 토큰 supplier가 없고 .env 키도 없어 get_main_token 준비를 건너뜁니다.")

    # 4) 브로커 생성 (토큰 공급자는 내부적으로 사용하지 않음: strict 계정 리스트 사용)
    broker = KiwoomRestBroker(
        base_url=base_url,
        api_id_buy=api_id_buy,
        api_id_sell=api_id_sell,
        order_path=order_path,
        timeout=http_timeout,
    )

    # 5) AutoTrader 생성 + 브로커 주입
    trader = AutoTrader(
        settings=trade_settings,
        ladder=ladder_settings,
    )
    if hasattr(trader, "attach_broker") and callable(trader.attach_broker):
        trader.attach_broker(broker)
    elif hasattr(trader, "set_broker") and callable(trader.set_broker):
        trader.set_broker(broker)
    else:
        setattr(trader, "broker", broker)  # 안전망: 속성 주입

    logger.info(
        "AutoTrader wired: base_url=%s, order_path=%s, api_id(BUY/SELL)=%s/%s",
        base_url, order_path, api_id_buy, api_id_sell
    )
    return trader

# ─────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)

    # Bridge/Engine
    bridge = AsyncBridge()

    getter = DetailInformationGetter()
    engine = Engine(bridge, getter)

    # UI
    ui = MainWindow(
        bridge=bridge,
        engine=engine,
        perform_filtering_cb=perform_filtering,
        project_root=os.getcwd(),
    )
    bridge.pnl_snapshot_ready.connect(ui.on_pnl_snapshot)

    # 이벤트 배선
    bridge.new_stock_received.connect(ui.on_new_stock)
    bridge.new_stock_detail_received.connect(ui.on_new_stock_detail)

    # 설정 로드
    store = SettingsStore()
    app_cfg = store.load()

    # 트레이더 생성(단일 인스턴스 원칙)
    trader = _build_trader_from_cfg(app_cfg)

    # 모니터: AppSettings의 모든 관련 옵션을 주입
    monitor = ExitEntryMonitor(
        detail_getter=getter,
        macd30_timeframe=str(app_cfg.macd30_timeframe or "30m"),
        macd30_max_age_sec=int(app_cfg.macd30_max_age_sec),
        tz=str(app_cfg.timezone or "Asia/Seoul"),
        poll_interval_sec=int(app_cfg.poll_interval_sec),
        on_signal=trader.make_on_signal(bridge),  # AutoTrader 핸들러 연결
        bar_close_window_start_sec=int(app_cfg.bar_close_window_start_sec),
        bar_close_window_end_sec=int(app_cfg.bar_close_window_end_sec),
    )
    engine.monitor = monitor
    bridge.monitor = monitor

    apply_all_settings(app_cfg, trader=trader, monitor=monitor)

    ui.show()

    # 1) 루프 시작 + 초기화
    engine.start_loop()
    QTimer.singleShot(0, ui.on_click_init)

    # 2) 모니터 시작 (별도 스레드)
    QTimer.singleShot(0, lambda: start_monitor_on_thread(monitor))

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
