# main.py (발췌: 상단 import / main() / perform_filtering 그대로 유지)
import sys
import os
import logging
import asyncio
import threading
from datetime import datetime, date, timedelta, timezone
from typing import List, Tuple


from PySide6.QtCore import QObject, Signal, Slot, QTimer
from PySide6.QtWidgets import QApplication


from utils.utils import load_api_keys
from utils.token_manager import get_access_token
from core.websocket_client import WebSocketClient

from strategy.filter_1_finance import run_finance_filter
from strategy.filter_2_technical import run_technical_filter
from core.detail_information_getter import SimpleMarketAPI, DetailInformationGetter
from typing import Dict, Tuple, List
import pandas as pd
from core.macd_calculator import calculator, macd_bus  
import matplotlib
from matplotlib import rcParams

from ui_main import MainWindow

def _setup_korean_font():
    import platform
    sysname = platform.system()
    if sysname == "Windows":
        rcParams["font.family"] = "Malgun Gothic"      # ✅ 한글 지원
    elif sysname == "Darwin":  # macOS
        rcParams["font.family"] = "AppleGothic"
    else:  # Linux 등
        # 설치되어 있다면 아래 중 하나를 선택
        # sudo apt install fonts-nanum -y  (NanumGothic)
        rcParams["font.family"] = "NanumGothic"

    rcParams["axes.unicode_minus"] = False   # 마이너스 깨짐 방지

_setup_korean_font()


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
    log = Signal(str)

    # 조건식 목록 수신
    condition_list_received = Signal(list)
    # 신규 종목 코드 수신(선공지)
    new_stock_received = Signal(str)
    # 종목 상세 딕셔너리 수신(후공지)
    new_stock_detail_received = Signal(dict)
    # MACD 데이터 수신
    macd_data_received = Signal(str, float, float, float)

    chart_rows_received = Signal(str, str, list) # code, tf, rows(list)
    macd_series_ready = Signal(dict)  # {"code","tf","series":[...]}

    macd_updated = Signal(dict)
    macd_buy_signal = Signal(dict)
    macd_sell_signal = Signal(dict)
    minute_bars_received = Signal(str, list)  # code, rows


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
    initialization_complete = Signal()

    def __init__(self, bridge, getter: DetailInformationGetter, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.getter = getter


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
                    #on_new_stock=self._on_new_stock,
                    #on_new_stock_detail=self._on_new_stock_detail,
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
    def start_macd_stream(self, code: str, poll_sec: int = 30, need5m: int = 350, need1d: int = 400):
        """
        분봉은 주기 폴링 + 증분 갱신, 일봉은 초기화만 계산(필요 시 유지)
        리팩터링: rows_to_df_* / init_state_from_history / to_series_payload / 내부 큐 제거.
        """
        logger.debug("111 start_macd_stream")

        # 루프 존재/실행 확인 (필요 시)
        if not hasattr(self, "loop") or self.loop is None:
            self.bridge.log.emit("❌ 이벤트 루프가 초기화되지 않았습니다.")
            return
        if self.loop.is_closed():
            self.bridge.log.emit("❌ 이벤트 루프가 이미 종료되었습니다.")
            return

        # 재진입 방지
        if not hasattr(self, "_minute_stream_tasks"):
            self._minute_stream_tasks = {}
        if code in self._minute_stream_tasks:
            self.bridge.log.emit(f"↩️ 이미 스트림 중: {code}")
            logger.debug("222 start_macd_stream (already running)")
            return

        async def job():
            try:
                logger.debug("333 macd stream job 실행")

                # 1) 초기: 5분봉 대량 → 계산기 내부에서 변환/계산/emit
                res5 = await asyncio.to_thread(self.detail.fetch_minute_chart_ka10080, code, tic_scope=5, need=need5m)
                rows5 = res5.get("rows", []) or []
                self.bridge.chart_rows_received.emit(code, "5m", rows5)
                calculator.apply_rows(code=code, tf="5m", rows=rows5, need=need5m)

                # 2) 초기: 일봉 (증분 생략)
                today = date.today().strftime("%Y%m%d")
                res1d = await asyncio.to_thread(
                    self.detail.fetch_daily_chart_ka10081,
                    code,
                    base_dt=today,
                    upd_stkpc_tp="1",
                    need=need1d
                )
                rows1d = res1d.get("rows", []) or []
                self.bridge.chart_rows_received.emit(code, "1d", rows1d)
                calculator.apply_rows(code=code, tf="1d", rows=rows1d, need=need1d)

                # 3) 루프: 분봉 증분 (최근 60개만 재조회)
                while True:
                    await asyncio.sleep(poll_sec)
                    try:
                        res_inc = await asyncio.to_thread(self.detail.fetch_minute_chart_ka10080, code, tic_scope=5, need=60)
                        rows_inc = res_inc.get("rows", []) or []
                        if not rows_inc:
                            continue
                        self.bridge.chart_rows_received.emit(code, "5m", rows_inc)
                        calculator.apply_rows(code=code, tf="5m", rows=rows_inc, need=need5m)
                    except asyncio.CancelledError:
                        raise
                    except Exception as inner_e:
                        self.bridge.log.emit(f"⚠️ MACD 증분 갱신 오류({code}): {inner_e}")
                        logger.exception("incremental update failed")

            except asyncio.CancelledError:
                self.bridge.log.emit(f"⏹️ MACD 스트림 종료: {code}")
                logger.info("MACD stream cancelled: %s", code)
                raise
            except Exception as e:
                self.bridge.log.emit(f"❌ MACD 증분 스트림 실패({code}): {e}")
                logger.exception("MACD stream failed")
            finally:
                # 태스크 테이블 정리
                try:
                    if code in self._minute_stream_tasks:
                        del self._minute_stream_tasks[code]
                except Exception:
                    pass

        # ✅ 이 두 줄은 반드시 함수 내부에 있어야 함 (들여쓰기 주의)
        task = asyncio.run_coroutine_threadsafe(job(), self.loop)
        self._minute_stream_tasks[code] = task


    def update_macd_once(self, code: str, tic_scope: int = 5):
        try:
            self.getter.emit_macd_for_ka10080(
                self.bridge, code, tic_scope=tic_scope, need=350, exchange_prefix="KRX"
            )
        except Exception:
            logger.exception("update_macd_once failed for %s", code)


    def stop_macd_stream(self, code: str):
        task = self._minute_stream_tasks.get(code)
        if task:
            task.cancel()
            self.bridge.log.emit(f"🛑 MACD 스트림 중지 요청: {code}")

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

    getter = DetailInformationGetter()
    engine = Engine(bridge, getter)    

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
    
    bridge.new_stock_received.connect(engine.start_macd_stream)
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

