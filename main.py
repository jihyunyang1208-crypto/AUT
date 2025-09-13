# main.py
import sys
import os
import logging
import asyncio
import threading
from datetime import datetime, date, timedelta
from typing import Dict

from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtWidgets import QApplication

from utils.utils import load_api_keys
from utils.token_manager import get_access_token
from core.websocket_client import WebSocketClient

from strategy.filter_1_finance import run_finance_filter
from strategy.filter_2_technical import run_technical_filter
from core.detail_information_getter import DetailInformationGetter, SimpleMarketAPI, normalize_ka10080_rows
from core.macd_calculator import calculator, macd_bus
from ui_main import MainWindow

from matplotlib import rcParams

from exitpro.adapters.candle_cache import CandleCache
from exitpro.adapters.detail_getter_from_cache import DetailGetterFromCache
from exitpro.adapters.macd_dialog_feed_adapter import MacdDialogFeedAdapter
from exitpro.exit_monitor import ExitEntryMonitor, TradeSettings, TradeSignal


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

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, datetime.now().strftime("app_%Y%m%d.log"))

# 기본 로거 설정
logging.basicConfig(
    filename=LOG_FILE,  # 로그 파일 경로
    level=logging.DEBUG, # 기록할 로그 레벨 (INFO, DEBUG, WARNING 등)
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    encoding='utf-8' ,   # 인코딩 설정 (한글 깨짐 방지)
    force=True
)

# 이제부터 모든 로거는 이 설정에 따라 동작합니다.
logger = logging.getLogger(__name__)


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
        # 다음 시간의 (next_min % 60)분
        target = (base + timedelta(hours=1)).replace(minute=(next_min % 60))
    else:
        target = base.replace(minute=next_min)
    return max(1.0, (target - now).total_seconds())


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

    # 토큰 브로드캐스트 (UI가 보유한 getter/market_api에 반영할 때 사용)
    token_ready = Signal(str)

    # MACD (새 포맷)
    # {"code": str, "tf": "5m"/"30m"/"1d", "series": [{"t","macd","signal","hist"}]}
    macd_series_ready = Signal(dict)

    # 레거시 4-튜플 (원하면 UI에서 그대로 받을 수 있게 유지)
    macd_data_received = Signal(str, float, float, float)

    # (옵션) 원시 캔들 rows
    chart_rows_received = Signal(str, str, list)  # code, tf, rows

    # (옵션) 후속 트리거
    macd_updated = Signal(dict)
    macd_buy_signal = Signal(dict)
    macd_sell_signal = Signal(dict)

    # (옵션) 5m 원시 rows
    minute_bars_received = Signal(str, list)
    symbol_name_updated = Signal(str, str)  # (code6, name)


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

        # 별도 asyncio 루프 스레드
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self._run_loop, daemon=True)

        # 자원
        self.access_token: str | None = None
        self.appkey: str | None = None
        self.secretkey: str | None = None
        self.market_api: SimpleMarketAPI | None = None
        self.websocket_client: WebSocketClient | None = None

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
            # 1) 토큰
            self.appkey, self.secretkey = load_api_keys()
            self.access_token = get_access_token(self.appkey, self.secretkey)
            self.bridge.log.emit("🔐 액세스 토큰 발급 완료")

            # 2) HTTP 클라이언트 (토큰 주입)
            if not hasattr(self, "market_api") or self.market_api is None:
                self.market_api = SimpleMarketAPI(token=self.access_token)
            else:
                self.market_api.set_token(self.access_token)
            if not hasattr(self, "getter") or self.getter is None:
                self.getter = DetailInformationGetter(token=self.access_token)
            else:
                self.getter.token = self.access_token

            # 3) WS 클라이언트 생성 및 시작
            if self.websocket_client is None:
                self.websocket_client = WebSocketClient(
                    uri="wss://api.kiwoom.com:10000/api/dostk/websocket",
                    token=self.access_token,
                    bridge=self.bridge,
                    market_api=self.market_api,
                    socketio=None,
                    on_condition_list=self._on_condition_list,
                    dedup_ttl_sec=3,
                    detail_timeout_sec=6.0,
                    refresh_token_cb=self._refresh_token_sync,
                )
            self.websocket_client.start(loop=self.loop)
            self.bridge.log.emit("🌐 WebSocket 클라이언트 시작")

            # 4) macd_bus → bridge 패스스루 (중복연결 방지)
            # Simply connect the signal. PySide6 handles duplicate connections gracefully.
            macd_bus.macd_series_ready.connect(self._on_bus_macd_series)

            # 5) UI 통보
            self.initialization_complete.emit()
            self.bridge.token_ready.emit(self.access_token)

        except Exception as e:
            self.bridge.log.emit(f"❌ 초기화 실패: {e}")
            raise

    def _refresh_token_sync(self) -> str | None:
        try:
            new_token = get_access_token(self.appkey, self.secretkey)
            if new_token:
                self.access_token = new_token
                if self.market_api:
                    self.market_api.set_token(new_token)
                if self.getter:
                    self.getter.token = new_token
                self.bridge.log.emit("🔁 액세스 토큰 재발급 완료")
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
        except Exception as e:
            self.bridge.log.emit(f"⚠️ MACD 패스스루 실패: {e}")

    # ── 조건검색 콜백/제어 ─────────────────────
    def _on_condition_list(self, conditions: list):
        self.bridge.log.emit("[Engine] 조건식 수신")
        # 저장된 list 를 프로그램 실행 초기에 load하는 것으로 대체
        self.bridge.condition_list_received.emit(conditions or [])
        pass

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
            self.bridge.log.emit(f"↩️ 이미 스트림 중: {code}")
            return

        def _safe_rows(rows_any) -> list[dict]:
            """
            rows_any 가
            - list[dict] 이면 그대로
            - list[str(JSON)] 이면 json.loads 로 파싱
            - str(JSON) 이면 json.loads 해서 rows 키를 찾거나 dict 한 개로 감쌈
            - 그 외는 빈 리스트
            """
            try:
                # ✅ case 1: 이미 list[dict]
                if isinstance(rows_any, list) and rows_any:
                    if isinstance(rows_any[0], dict):
                        return rows_any

                    # ✅ case 2: list[str(JSON)]
                    if isinstance(rows_any[0], str):
                        out = []
                        for s in rows_any:
                            try:
                                obj = json.loads(s)
                                if isinstance(obj, dict):
                                    out.append(obj)
                            except Exception:
                                # 비-JSON 문자열이면 스킵
                                continue
                        return out

                    # list인데 dict/str 아니면 빈 리스트
                    return []

                # ✅ case 3: rows 자체가 JSON 문자열
                if isinstance(rows_any, str):
                    try:
                        obj = json.loads(rows_any)
                    except Exception:
                        return []
                    if isinstance(obj, dict):
                        if "rows" in obj and isinstance(obj["rows"], list):
                            return _safe_rows(obj["rows"])
                        return [obj]
                    if isinstance(obj, list):
                        return _safe_rows(obj)

            except Exception:
                pass
            return []

        async def job_5m():
            try:
                # 초기 FULL (5m)
                res = await asyncio.to_thread(self.getter.fetch_minute_chart_ka10080, code, tic_scope=5, need=need_5m)
                rows5_raw = res.get("rows", []) or []
                rows5 = normalize_ka10080_rows(_safe_rows(rows5_raw))

                self.bridge.chart_rows_received.emit(code, "5m", rows5_raw)
                if rows5:
                    calculator.apply_rows_full(code=code, tf="5m", rows=rows5, need=need_5m)

                # 증분 루프
                while True:
                    await asyncio.sleep(_seconds_to_next_boundary(datetime.now(), poll_5m_step))
                    inc = await asyncio.to_thread(self.getter.fetch_minute_chart_ka10080, code, tic_scope=5, need=60)
                    rows_inc_raw = inc.get("rows", []) or []
                    if rows_inc_raw:
                        self.bridge.chart_rows_received.emit(code, "5m", rows_inc_raw)
                        rows_inc = normalize_ka10080_rows(_safe_rows(rows_inc_raw))  # ✅ 여기서도 반드시 safe
                        if rows_inc:
                            calculator.apply_append(code=code, tf="5m", rows=rows_inc)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.bridge.log.emit(f"⚠️ 5m 스트림 오류({code}): {e}")

        async def job_30m():
            try:
                # 초기 FULL (30m)
                res = await asyncio.to_thread(self.getter.fetch_minute_chart_ka10080, code, tic_scope=30, need=need_30m)
                rows30_raw = res.get("rows", []) or []
                rows30 = normalize_ka10080_rows(_safe_rows(rows30_raw))

                self.bridge.chart_rows_received.emit(code, "30m", rows30_raw)
                if rows30:
                    calculator.apply_rows_full(code=code, tf="30m", rows=rows30, need=need_30m)

                # 증분
                while True:
                    await asyncio.sleep(_seconds_to_next_boundary(datetime.now(), poll_30m_step))
                    inc = await asyncio.to_thread(self.getter.fetch_minute_chart_ka10080, code, tic_scope=30, need=60)
                    rows_inc_raw = inc.get("rows", []) or []
                    if rows_inc_raw:
                        self.bridge.chart_rows_received.emit(code, "30m", rows_inc_raw)
                        rows_inc = normalize_ka10080_rows(_safe_rows(rows_inc_raw))  # ✅ safe 추가
                        if rows_inc:
                            calculator.apply_append(code=code, tf="30m", rows=rows_inc)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.bridge.log.emit(f"⚠️ 30m 스트림 오류({code}): {e}")

        async def job_1d():
            try:
                today = date.today().strftime("%Y%m%d")
                res = await asyncio.to_thread(self.getter.fetch_daily_chart_ka10081, code, base_dt=today, need=need_1d)
                rows1d = res.get("rows", []) or []
                self.bridge.chart_rows_received.emit(code, "1d", rows1d)
                calculator.apply_rows_full(code=code, tf="1d", rows=rows1d, need=need_1d)
                # 일봉은 장 종료 이후 1회 갱신이면 충분. 필요 시 다음날 재호출.
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.bridge.log.emit(f"⚠️ 1d 초기화 오류({code}): {e}")

        def _submit(coro):
            return asyncio.run_coroutine_threadsafe(coro, self.loop)

        tasks = {
            "5m": _submit(job_5m()),
            "30m": _submit(job_30m()),
            "1d": _submit(job_1d()),
        }
        self._minute_stream_tasks[code] = tasks
        self.bridge.log.emit(f"▶️ MACD 스트림 시작: {code} (5m/30m/1d)")


    def stop_macd_stream(self, code: str):
        tasks = self._minute_stream_tasks.get(code)
        if not tasks:
            return
        for tf, fut in list(tasks.items()):
            try:
                fut.cancel()
            except Exception:
                pass
        self._minute_stream_tasks.pop(code, None)
        self.bridge.log.emit(f"🛑 MACD 스트림 중지 요청: {code}")

    # (옵션) 단발 계산
    def update_macd_once(self, code: str, tic_scope: int = 5):
        try:
            self.getter.emit_macd_for_ka10080(self.bridge, code, tic_scope=tic_scope, need=200, exchange_prefix="KRX")
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
# ExitPro: 모니터/캐시/어댑터 배선 함수
# ─────────────────────────────────────────────────────────
def wire_exit_monitor(engine: Engine, bridge: AsyncBridge):
    """
    - 5/30/1d rows → CandleCache 에 적재
    - macd_bus → MacdDialogFeedAdapter 로 30m 최신 MACD 제공
    - ExitEntryMonitor 시작(5분봉 마감 근사시에 룰 평가)
    - 신규 종목 디테일 수신 시: 스트림 시작 + 모니터 심볼 추가
    """
    logger.info("[WIRING] ExitEntryMonitor wiring...")

    # 1) 캔들 캐시
    candle_cache = CandleCache(maxlen=4000, tz="Asia/Seoul")

    def _on_chart_rows(code: str, tf: str, rows: list):
        # Engine.start_macd_stream 에서 초기/증분 rows가 들어옴
        logger.debug(f"[MAIN] chart_rows_received code={code} tf={tf} rows={len(rows)}")
        candle_cache.upsert_rows(code, tf, rows)

    bridge.chart_rows_received.connect(_on_chart_rows)

    # 2) 모니터가 읽을 getter (캐시 기반)
    detail_getter = DetailGetterFromCache(candle_cache)

    # 3) 30분 MACD 최신값 피드(버스 구독)
    macd_feed = MacdDialogFeedAdapter(tz="Asia/Seoul")
    macd_bus.macd_series_ready.connect(macd_feed.on_bus_series_ready)
    # (원하면) bridge.macd_series_ready도 연결 가능:
    # bridge.macd_series_ready.connect(macd_feed.on_bus_series_ready)

    # 4) 시그널 콜백(여기에 주문/알림 연결 가능)
    def on_signal(sig: TradeSignal):
        logger.info("📣 %s | %s | %s | %.2f | %s",
                    sig.side, sig.symbol, sig.ts, sig.price, sig.reason)

    # 5) 모니터 생성 및 루프 시작
    settings = TradeSettings(master_enable=True, auto_buy=False, auto_sell=True)
    monitor = ExitEntryMonitor(
        detail_getter=detail_getter,
        macd_feed=macd_feed,
        symbols=[],                        # 신규 종목 수신 시 동적으로 추가
        settings=settings,
        use_macd30_filter=True,            # 30분 MACD hist ≥ 0 필터
        macd30_timeframe="30m",
        macd30_max_age_sec=1800,
        tz="Asia/Seoul",
        poll_interval_sec=10,
        on_signal=on_signal,
    )
    asyncio.run_coroutine_threadsafe(monitor.start(), engine.loop)

    # 6) 신규 종목 디테일 수신 시 스트림 확보 + 모니터 등록
    _active_streams: set[str] = set()

    def _ensure_macd_stream(code6: str):
        if code6 in _active_streams:
            logger.debug("start_macd_stream: already active for %s", code6)
            return
        try:
            engine.start_macd_stream(code6)
            _active_streams.add(code6)
            logger.info("✅ started MACD stream for %s (trigger=new_stock_detail)", code6)
        except Exception as e:
            logger.warning("start_macd_stream failed for %s: %s", code6, e)

    def _on_new_stock_detail(payload: dict):
        raw_code = (payload.get("stock_code") or "").strip()
        if not raw_code:
            return
        code6 = raw_code[-6:].zfill(6)
        _ensure_macd_stream(code6)
        if code6 not in monitor.symbols:
            monitor.symbols.append(code6)
            logger.info("[monitor] add symbol %s", code6)

    bridge.new_stock_detail_received.connect(_on_new_stock_detail)

    logger.info("[WIRING] ExitEntryMonitor ready.")
    return monitor


# ─────────────────────────────────────────────────────────
# 필터 파이프라인
# ─────────────────────────────────────────────────────────
def perform_filtering():
    logger.info("--- 필터링 프로세스 시작 ---")
    today = datetime.now()

    # 분기 재무 업데이트 기준일 (예시)
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
# 진입점
# ─────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)

    # Bridge/Engine
    bridge = AsyncBridge()
    logger.info("[MAIN] bridge id=%s", id(bridge))

    getter = DetailInformationGetter()
    engine = Engine(bridge, getter)

    # UI
    ui = MainWindow(
        bridge=bridge,
        engine=engine,
        perform_filtering_cb=perform_filtering,
        project_root=os.getcwd(),
    )

    # 이벤트 배선
    bridge.new_stock_received.connect(ui.on_new_stock)
    bridge.new_stock_detail_received.connect(ui.on_new_stock_detail)

    ui.show()

    # 1) 루프 시작 + 초기화
    engine.start_loop()
    QTimer.singleShot(0, ui.on_click_init)

    # 2) 어댑터 
    # ── Engine 초기화 완료 후 ExitPro 배선 ──
    def _after_init():
        try:
            wire_exit_monitor(engine, bridge)
        except Exception as e:
            logger.exception("[WIRING] ExitEntryMonitor wiring failed: %s", e)

    engine.initialization_complete.connect(_after_init)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
