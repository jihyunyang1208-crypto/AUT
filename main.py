# main.py (발췌: 상단 import / main() / perform_filtering 그대로 유지)
import sys
import os
import logging
import asyncio
import threading
from datetime import datetime

from PyQt5.QtCore import QObject, pyqtSignal, QTimer  
from PyQt5.QtWidgets import QApplication

from utils.utils import load_api_keys
from utils.token_manager import get_access_token
from core.websocket_client import WebSocketClient

from strategy.filter_1_finance import run_finance_filter
from strategy.filter_2_technical import run_technical_filter
from core.detail_information_getter import SimpleMarketAPI, DetailInformationGetter
from typing import Dict, Tuple, List
import pandas as pd
from core.macd_calculator import (
    rows_to_df_minute,
    rows_to_df_daily,
    compute_macd,
    MacdParams,
    MacdState,
    seed_macd_state,
    update_macd_incremental,
    to_series_payload,
)

# ★ UI 모듈
from ui_main import MainWindow

# ─────────────────────────────────────────────────────────
# 로거 설정
# ─────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )

try:
    project_root  # noqa: F823
except NameError:
    project_root = os.getcwd()




# ─────────────────────────────────────────────────────────
# Bridge: 비UI 스레드 → UI 스레드로 신호 전달
# ─────────────────────────────────────────────────────────
class AsyncBridge(QObject):
    # 로그 문자열
    log = pyqtSignal(str)
    # 조건식 목록 수신
    condition_list_received = pyqtSignal(list)
    # 신규 종목 코드 수신(선공지)
    new_stock_received = pyqtSignal(str)
    # 종목 상세 딕셔너리 수신(후공지)
    new_stock_detail_received = pyqtSignal(dict)
    # MACD 데이터 수신
    macd_data_received = pyqtSignal(str, float, float, float)

    macd_series_ready = pyqtSignal(str, str, dict)   # code, tf("5m"/"1d"), series(dict)
    chart_rows_received = pyqtSignal(str, str, list) # code, tf, rows(list)

    def __init__(self):
        super().__init__()


# ─────────────────────────────────────────────────────────
# Engine: 백그라운드 asyncio 루프 + WS 클라이언트 관리
# ─────────────────────────────────────────────────────────
class Engine(QObject):
    """
    - 비동기 루프를 백그라운드 스레드에서 관리
    - WebSocketClient 연결/수신
    - 신규 종목 감지 시 MACD 모니터링 연결
    """
    initialization_complete = pyqtSignal()

    def __init__(self, bridge: AsyncBridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge

        # asyncio 이벤트 루프 (별도 스레드)
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self._run_loop, daemon=True)

        self.access_token = None
        self.websocket_client: WebSocketClient | None = None
        self.monitored_stocks: set[str] = set()  # 중복 모니터링 방지

        self.appkey = None
        self.secretkey = None
        self.market_api = None

        # 증분 상태 저장: (code, tf) -> MacdState
        self._macd_states: Dict[Tuple[str,str], MacdState] = {}
        # 코얼레싱 큐
        self._macd_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._latest = {}
        self._emit_task = None

        # 스트림 태스크: code -> task
        self._minute_stream_tasks: Dict[str, asyncio.Task] = {}



    # ---------- 루프 ----------
    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start_loop(self):
        """백그라운드에서 asyncio 루프 시작"""
        if not self.loop_thread.is_alive():
            self.loop_thread.start()
            self.bridge.log.emit("🌀 asyncio 루프 시작")

        if not self._emit_task:
            async def emitter():
                import time
                last = 0.0
                while True:
                    item = await self._macd_queue.get()
                    self._latest[(item["code"], item["tf"])] = item
                    now = time.time()
                    if now - last >= 0.2:
                        for _, payload in list(self._latest.items()):
                            self.bridge.macd_series_ready.emit(payload["code"], payload["tf"], payload["series"])
                        self._latest.clear()
                        last = now
                    self._macd_queue.task_done()
            self._emit_task = self.loop.create_task(emitter())



    # ---------- 초기화 ----------
    def initialize(self):
        if getattr(self, "_initialized", False):
            self.bridge.log.emit("[Engine] initialize: already initialized, skip")
            return
        self._initialized = True

        """
        API 키 로드 → 토큰 발급 → SimpleMarketAPI 생성 → WebSocketClient(DI) 연결
        """
        try:
            # 1) 토큰 발급
            self.appkey, self.secretkey = load_api_keys()
            self.access_token = get_access_token(self.appkey, self.secretkey)
            self.bridge.log.emit("🔐 액세스 토큰 발급 완료")

            # 2) SimpleMarketAPI 생성 (여기서 지연 임포트로 상단 import 안 건드립니다)
            from core.detail_information_getter import SimpleMarketAPI, DetailInformationGetter
            self.market_api = SimpleMarketAPI(token=self.access_token)
            self.detail = DetailInformationGetter(token=self.access_token)


            # 3) WebSocketClient 생성 (의존성 주입)
            if self.websocket_client is None:
                self.websocket_client = WebSocketClient(
                    uri='wss://api.kiwoom.com:10000/api/dostk/websocket',
                    token=self.access_token,
                    bridge=self.bridge,
                    market_api=self.market_api,                 
                    socketio=None,
                    on_condition_list=self._on_condition_list,
                    on_new_stock=self._on_new_stock,
                    on_new_stock_detail=self._on_new_stock_detail,
                    dedup_ttl_sec=3,
                    detail_timeout_sec=6.0,
                    refresh_token_cb=self._refresh_token_sync,  # (옵션) WS 재로그인용
                )
            else :
                self.bridge.log.emit("[Engine] Reusing existing WebSocketClient")
                

            self.websocket_client.start(loop=self.loop)
            
            self.bridge.log.emit("🌐 WebSocket 클라이언트 시작")
            self.initialization_complete.emit()




        except Exception as e:
            self.bridge.log.emit(f"❌ 초기화 실패: {e}")
            raise

    def _refresh_token_sync(self) -> str | None:
        """WebSocketClient에서 호출하는 동기 콜백. 새 토큰 반환 (실패 시 None)."""
        try:
            new_token = get_access_token(self.appkey, self.secretkey)
            if new_token:
                self.access_token = new_token
                # HTTP 클라(SimpleMarketAPI)에도 반영
                if self.market_api:
                    self.market_api.set_token(new_token)
                self.bridge.log.emit("🔁 액세스 토큰 재발급 완료")
                return new_token
        except Exception as e:
            self.bridge.log.emit(f"❌ 토큰 재발급 실패: {e}")
        return None

    # ---------- 콜백 처리 ----------
    def _on_condition_list(self, conditions: list):
        self.bridge.log.emit("[Engine] 조건식 수신")
        self.bridge.condition_list_received.emit(conditions or [])

    def _on_new_stock_detail(self, payload: dict):
        # 1) UI로 바로 송신
        self.bridge.new_stock_detail_received.emit(payload)

        # 2) MACD 모듈에 rows 전달
        try:
            rows = payload.get("rows") or []
            code = payload.get("stock_code")
            if rows and code:
                threading.Thread(
                    target=self._run_macd_from_rows, args=(code, rows), daemon=True
                ).start()
        except Exception as e:
            self.bridge.log.emit(f"❌ MACD rows 전달 실패: {e}")



    def _on_new_stock(self, stock_code: str):
        # 신규 종목 선공지 수신
        self.bridge.log.emit(f"📈 신규 종목 감지: {stock_code}, MACD 모니터링 시작")


        try:
            # 단일 종목 실시간 MACD 모니터링 시작
            self.start_macd_stream(stock_code, poll_sec=30, need5m=350, need1d=400)
            # 선공지: 코드만 UI로
            self.bridge.new_stock_received.emit(stock_code)
            asyncio.run_coroutine_threadsafe(fetch_and_emit_macd_snapshot(), self.loop)

        except Exception as e:
            self.bridge.log.emit(f"❌ MACD 모니터링 시작 실패({stock_code}): {e}")

        async def fetch_and_emit_macd_snapshot():
            try:
                if not self.detail:
                    self.bridge.log.emit("[MACD] detail getter not ready")
                    return
                # 5분봉 200개 정도 → MACD 안정화
                js = await self.detail.fetch_minute_chart_ka10080_async(
                    stock_code, tic_scope="5", upd_stkpc_tp="1", max_bars=200
                )
                rows = js.get("rows") or []
                from core.macd_calculator import rows_to_df_minute, compute_macd_last_from_close
                df = rows_to_df_minute(rows)
                m, s, h = compute_macd_last_from_close(df["close"]) if not df.empty else (None, None, None)
                if m is None:
                    self.bridge.log.emit(f"[MACD] no minute rows for {stock_code}")
                    return
                # UI로 전달
                self.bridge.macd_data_received.emit(stock_code, m, s, h)
            except Exception as e:
                self.bridge.log.emit(f"[MACD] snapshot error {stock_code}: {e}")



    # ---------- 조건검색 제어 ----------
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

    # 조건검색에서 편입(I) 신호를 받으면 engine.start_macd_stream(code)만 호출하면 됩니다.
    # 초기 한 번은 풀 계산으로 시딩하고, 이후는 새 캔들만 증분 반영합니다.
    def start_macd_stream(self, code: str, *, poll_sec: int = 30, need5m: int = 350, need1d: int = 400):
        """분봉은 주기 폴링+증분 갱신, 일봉은 초기화만 계산"""
        if code in self._minute_stream_tasks:
            self.bridge.log.emit(f"↩️ 이미 스트림 중: {code}")
            return

        async def job():
            try:
                # 1) 초기: 5분봉 큰 히스토리 → 상태 시딩
                res5 = await asyncio.to_thread(self.detail.fetch_minute_chart_ka10080, code, tic_scope=5, need=need5m)
                rows5 = res5.get("rows", [])
                self.bridge.chart_rows_received.emit(code, "5m", rows5)

                df5 = rows_to_df_minute(rows5)
                if not df5.empty:
                    # 상태 + full MACD 생성
                    state5, macd_full5 = init_state_from_history(df5["close"])
                    self._macd_states[(code, "5m")] = state5
                    payload5 = to_series_payload(macd_full5.tail(need5m))
                    try: self._macd_queue.put_nowait({"code": code, "tf": "5m", "series": payload5})
                    except asyncio.QueueFull: pass

                # 2) 초기: 일봉도 계산(증분은 생략해도 무방)
                end = date.today()
                today = date.today().strftime("%Y%m%d")

                res1d = await asyncio.to_thread(
                    self.detail.fetch_daily_chart_ka10081,
                    code,
                    base_dt=today,       # 기준일(오늘) 기준으로 과거가 내려오도록
                    upd_stkpc_tp="1",
                    need=need1d
                )
                rows1d = res1d.get("rows", [])
                self.bridge.chart_rows_received.emit(code, "1d", rows1d)

                df1d = rows_to_df_daily(rows1d)
                if not df1d.empty:
                    macd1d = compute_macd(df1d["close"]).dropna().tail(need1d)
                    payload1d = to_series_payload(macd1d)
                    try: self._macd_queue.put_nowait({"code": code, "tf": "1d", "series": payload1d})
                    except asyncio.QueueFull: pass

                # 3) 루프: 분봉 증분 업데이트
                while True:
                    await asyncio.sleep(poll_sec)
                    # 최근 n개만 다시 받아서 마지막 ts 이후만 반영
                    res = await asyncio.to_thread(self.detail.fetch_minute_chart_ka10080, code, tic_scope=5, need=60)
                    rows = res.get("rows", [])
                    df = rows_to_df_minute(rows)

                    state = self._macd_states.get((code, "5m"))
                    if state is None:
                        # 드물지만 상태가 사라졌다면 재시딩
                        if not df.empty:
                            state, macd_full = init_state_from_history(df["close"])
                            self._macd_states[(code, "5m")] = state
                            payload = to_series_payload(macd_full.tail(need5m))
                            try: self._macd_queue.put_nowait({"code": code, "tf": "5m", "series": payload})
                            except asyncio.QueueFull: pass
                        continue

                    if df.empty:
                        continue

                    # 새 포인트만 추출하여 증분 업데이트
                    new_points: List[Tuple[pd.Timestamp, float]] = list(df["close"].items())
                    inc = update_state_with_points(state, new_points)
                    if not inc.empty:
                        # 기존 마지막 구간과 이어 붙이는 건 UI단에서 time index를 기준으로 병합 렌더
                        payload = to_series_payload(inc)
                        try: self._macd_queue.put_nowait({"code": code, "tf": "5m", "series": payload})
                        except asyncio.QueueFull: pass

            except Exception as e:
                self.bridge.log.emit(f"❌ MACD 증분 스트림 실패({code}): {e}")

        task = asyncio.run_coroutine_threadsafe(job(), self.loop)
        self._minute_stream_tasks[code] = task

    def stop_macd_stream(self, code: str):
        t = self._minute_stream_tasks.pop(code, None)
        if t:
            # run_coroutine_threadsafe의 Future는 cancel() 가능
            t.cancel()
            self.bridge.log.emit(f"⏹ MACD 스트림 중지: {code}")

    # ---------- 종료 ----------
    def shutdown(self):
        self.bridge.log.emit("🛑 종료 처리 중...")
        try:
            if self.loop.is_running():
                self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception as e:
            self.bridge.log.emit(f"❌ 루프 종료 오류: {e}")


# ─────────────────────────────────────────────────────────
# 필터링 파이프라인
# (UI 쪽에서 QThread/Executor로 백그라운드 실행을 권장)
# ─────────────────────────────────────────────────────────
def perform_filtering():
    logger.info("--- 필터링 프로세스 시작 ---")
    today = datetime.now()
    # 분기 재무 업데이트 기준일 (예시): 4/1, 5/16, 8/15, 11/15
    finance_filter_dates = [(4, 1), (5, 16), (8, 15), (11, 15)]
    run_finance_filter_today = any(today.month == m and today.day == d
                                   for (m, d) in finance_filter_dates)

    if run_finance_filter_today:
        logger.info(f"오늘은 {today.month}월 {today.day}일. 1단계 금융 필터링을 실행합니다.")
        try:
            run_finance_filter()
            logger.info(
                "금융 필터링 완료. 결과는 %s 에 저장되었습니다.",
                os.path.join(project_root, 'stock_codes.csv')
            )
        except Exception as e:
            logger.exception("금융 필터링 중 오류 발생")
            raise RuntimeError(f"금융 필터링 실패: {e}")
    else:
        logger.info(
            "오늘은 %d월 %d일. 1단계 금융 필터링 실행일이 아니므로 건너뜁니다.",
            today.month, today.day
        )
        logger.info("기존의 %s 파일을 사용합니다.",
                    os.path.join(project_root, 'stock_codes.csv'))

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

    # Bridge & Engine 준비 (UI에 주입)
    bridge = AsyncBridge()
    logger.info("[MAIN] bridge id=%s", id(bridge))

    engine = Engine(bridge)

    # 🌟 수정: main() 함수에서 WebSocketClient를 직접 생성하지 않습니다.
    # 🌟 대신 Engine의 initialize() 메서드에 모든 책임을 위임합니다.
    
    # UI 생성
    project_root = os.getcwd()
    ui = MainWindow(
        bridge=bridge,
        engine=engine,
        perform_filtering_cb=perform_filtering,
        project_root=project_root,
    )
    
    # 브릿지 → UI 슬롯 연결 (MainWindow 내에서 이미 연결했다면 중복 연결은 생략 가능)
    bridge.new_stock_received.connect(ui.on_new_stock)
    bridge.new_stock_detail_received.connect(ui.on_new_stock_detail)

    ui.show()

    # 🌟 수정: Engine의 루프만 시작하고, WS 클라이언트 초기화 및 시작은 Engine.initialize()에 맡깁니다.
    engine.start_loop()
    
    # 프로그램 시작 시 자동 초기화 (토큰/WS 등 엔진 초기화)
    QTimer.singleShot(0, ui.on_click_init)

    sys.exit(app.exec_())

if __name__ == "__main__":
    main()

