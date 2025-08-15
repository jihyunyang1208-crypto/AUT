# main.py (발췌: 상단 import / main() / perform_filtering 그대로 유지)
import sys
import os
import logging
import asyncio
import threading
from datetime import datetime

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication

from utils.utils import load_api_keys
from utils.token_manager import get_access_token
from monitor_macd import start_monitoring
from websocket_client import WebSocketClient

from strategy.filter_1_finance import run_finance_filter
from strategy.filter_2_technical import run_technical_filter

# ★ UI 모듈
from ui_main import MainWindow

# ─────────────────────────────────────────────────────────
# 로거 설정
# ─────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

try:
    project_root  # noqa: F823
except NameError:
    project_root = os.getcwd()


import sys
import os
import logging
import asyncio
import threading
from datetime import datetime

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication

from utils.utils import load_api_keys
from utils.token_manager import get_access_token
from monitor_macd import start_monitoring
from websocket_client import WebSocketClient

from strategy.filter_1_finance import run_finance_filter
from strategy.filter_2_technical import run_technical_filter

# ★ UI 모듈
from ui_main import MainWindow

# ─────────────────────────────────────────────────────────
# 로거 설정
# ─────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
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



# ─────────────────────────────────────────────────────────
# Engine: 백그라운드 asyncio 루프 + WS 클라이언트 관리
# ─────────────────────────────────────────────────────────
class Engine(QObject):
    """
    - 비동기 루프를 백그라운드 스레드에서 관리
    - WebSocketClient 연결/수신
    - 신규 종목 감지 시 MACD 모니터링 연결
    """
    def __init__(self, bridge: AsyncBridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge

        # asyncio 이벤트 루프 (별도 스레드)
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self._run_loop, daemon=True)

        self.access_token = None
        self.websocket_client: WebSocketClient | None = None
        self.monitored_stocks: set[str] = set()  # 중복 모니터링 방지

    # ---------- 루프 ----------
    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start_loop(self):
        """백그라운드에서 asyncio 루프 시작"""
        if not self.loop_thread.is_alive():
            self.loop_thread.start()
            self.bridge.log.emit("🌀 asyncio 루프 시작")

    # ---------- 초기화 ----------
    def initialize(self):
        """
        API 키 로드 → 토큰 발급 → WebSocketClient 연결 및 수신 시작
        """
        try:
            appkey, secretkey = load_api_keys()
            self.access_token = get_access_token(appkey, secretkey)
            self.bridge.log.emit("🔐 액세스 토큰 발급 완료")

            # WebSocketClient 생성 (콜백 연결)
            # on_new_stock_detail 콜백을 명시적으로 연결하여 상세 딕셔너리 전달
            self.websocket_client = WebSocketClient(
                uri='wss://api.kiwoom.com:10000/api/dostk/websocket',
                token=self.access_token,
                socketio=None,                              # 웹 Socket.IO 사용 안 함
                on_condition_list=self._on_condition_list,  # 조건식 수신 → UI
                on_new_stock=self._on_new_stock,            # 신규 종목 선공지 → MACD 시작
                on_new_stock_detail=self._on_new_stock_detail  # 상세 딕셔너리 → UI
            )

            async def handle_websocket():
                await self.websocket_client.connect()
                await self.websocket_client.receive_messages()

            # 비동기 태스크 실행 (백그라운드 루프에 등록)
            asyncio.run_coroutine_threadsafe(handle_websocket(), self.loop)
            self.bridge.log.emit("🌐 WebSocket 연결 및 수신 시작")

        except Exception as e:
            self.bridge.log.emit(f"❌ 초기화 실패: {e}")
            raise

    # ---------- 콜백 처리 ----------
    def _on_condition_list(self, conditions: list):
        self.bridge.log.emit("[Engine] 조건식 수신")
        self.bridge.condition_list_received.emit(conditions or [])

    def _on_new_stock_detail(self, payload: dict):
        # 상세 딕셔너리를 UI로 전달
        self.bridge.new_stock_detail_received.emit(payload)

    def _on_new_stock(self, stock_code: str):
        # 신규 종목 선공지 수신
        self.bridge.log.emit(f"📈 신규 종목 감지: {stock_code}, MACD 모니터링 시작")

        # 이미 모니터링 중이면 스킵
        if stock_code in self.monitored_stocks:
            self.bridge.log.emit(f"↩️ 이미 모니터링 중: {stock_code}")
            return
        self.monitored_stocks.add(stock_code)

        # MACD 콜백 → UI 시그널
        def macd_to_ui_callback(code, macd_line, signal_line, macd_histogram):
            try:
                self.bridge.log.emit(
                    f"[MACD] {code} | MACD:{macd_line:.2f} "
                    f"Signal:{signal_line:.2f} Hist:{macd_histogram:.2f}"
                )
                self.bridge.macd_data_received.emit(code, macd_line, signal_line, macd_histogram)
            except Exception as e:
                self.bridge.log.emit(f"❌ MACD UI emit 오류: {e}")

        try:
            # 단일 종목 실시간 MACD 모니터링 시작
            start_monitoring(self.access_token, [stock_code], macd_callback=macd_to_ui_callback)
            # 선공지: 코드만 UI로
            self.bridge.new_stock_received.emit(stock_code)
        except Exception as e:
            self.bridge.log.emit(f"❌ MACD 모니터링 시작 실패({stock_code}): {e}")

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
    # 정적 폴더 준비(필요 시)
    if not os.path.exists('static'):
        os.makedirs('static')

    app = QApplication(sys.argv)

    # Bridge & Engine 준비 (UI에 주입)
    bridge = AsyncBridge()
    engine = Engine(bridge)

    # MainWindow 생성(엔진/콜백/루트 전달)
    w = MainWindow(
        bridge=bridge,
        engine=engine,
        perform_filtering_cb=perform_filtering,
        project_root=project_root
    )
    w.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
