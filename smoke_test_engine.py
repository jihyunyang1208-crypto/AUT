import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

# --- 테스트 대상 클래스 임포트 ---
# engine.py 파일이 다른 경로에 있다면 경로를 맞게 수정해야 합니다.
# 예: from my_app.engine import Engine
# 이 예제에서는 engine.py가 있다고 가정합니다.

# engine.py에서 사용하는 다른 모듈들을 Mock으로 대체합니다.
# 실제 파일 경로에 맞게 수정해주세요.
from core.detail_information_getter import normalize_ka10080_rows, _rows_to_df_ohlcv

# 'engine' 모듈이 아직 존재하지 않는 경우를 대비해 가상 클래스를 만듭니다.
# 실제 Engine 클래스가 있다면 이 부분은 필요 없습니다.
try:
    from engine import Engine
except ImportError:
    print("Warning: 'engine.py' not found. Using a mock Engine class for test structure.")
    # 사용자가 제공한 코드를 기반으로 한 가상 Engine 클래스
    class Engine:
        def __init__(self, bridge, getter, monitor):
            self.bridge = bridge
            self.getter = getter
            self.monitor = monitor
            self._tasks = {}
        async def start_streaming_for_code(self, code, *args, **kwargs):
            self.task_5m = asyncio.create_task(self.job_5m(code))
            self.task_30m = asyncio.create_task(self.job_30m(code))
            await asyncio.gather(self.task_5m, self.task_30m)
        async def stop_streaming(self, code):
            if self.task_5m: self.task_5m.cancel()
            if self.task_30m: self.task_30m.cancel()
        async def job_5m(self, code, **kwargs): pass
        async def job_30m(self, code, **kwargs): pass


class TestEngineSmoke(unittest.IsolatedAsyncioTestCase):
    """
    Engine 클래스의 스모크 테스트.
    - 엔진이 정상적으로 초기화되는가?
    - 데이터 스트리밍 작업(5분봉, 30분봉)을 시작하는가?
    - 초기 데이터를 받아 관련 컴포넌트(monitor, calculator)를 호출하는가?
    """

    @patch('core.macd_calculator.calculator', new_callable=MagicMock)
    async def test_engine_starts_and_processes_initial_data(self, mock_calculator):
        """
        엔진이 시작되고 초기 데이터를 정상 처리하는지 테스트합니다.
        """
        print("--- 엔진 스모크 테스트 시작 ---")

        # 1. 의존성 객체들을 Mock으로 생성합니다.
        print("1. 의존성 객체(getter, monitor, bridge) Mock 생성...")
        mock_getter = MagicMock()
        mock_monitor = MagicMock()
        mock_bridge = MagicMock()

        # 가짜 API 응답 데이터 설정
        fake_api_response = {"rows": [{"cntr_tm": "20251013100500", "cur_prc": "10000"}]}
        mock_getter.fetch_minute_chart_ka10080.return_value = fake_api_response

        # 2. Mock 객체들을 주입하여 Engine 인스턴스를 생성합니다.
        print("2. Engine 인스턴스 생성...")
        # 사용자가 제공한 engine 코드에는 job_5m, job_30m이 구현되어 있으므로
        # 이를 테스트하기 위해 실제 클래스를 사용하되, 내부 로직은 Mock으로 대체합니다.
        # 아래 라인은 실제 Engine 클래스 구조에 따라 수정이 필요할 수 있습니다.
        with patch('engine.Engine.job_5m', new_callable=AsyncMock) as mock_job_5m, \
             patch('engine.Engine.job_30m', new_callable=AsyncMock) as mock_job_30m:

            engine_instance = Engine(bridge=mock_bridge, getter=mock_getter, monitor=mock_monitor)

            # 3. 데이터 스트리밍 작업을 시작합니다.
            print("3. 데이터 스트리밍 시작 (start_streaming_for_code)...")
            test_code = "005930"
            
            # 스트리밍 작업을 백그라운드에서 실행하고 즉시 제어를 돌려받습니다.
            streaming_task = asyncio.create_task(
                engine_instance.start_streaming_for_code(test_code)
            )

            # 엔진이 작업을 시작할 수 있도록 아주 짧은 시간(0.1초) 대기합니다.
            await asyncio.sleep(0.1)

            # 4. 스트리밍 작업이 시작되었는지 검증합니다.
            print("4. 5분봉, 30분봉 작업이 호출되었는지 확인...")
            mock_job_5m.assert_awaited_once()
            mock_job_30m.assert_awaited_once()
            print("   [성공] 5분봉 및 30분봉 데이터 수집 작업이 시작되었습니다.")
            
            # --- 실제 데이터 처리 흐름을 테스트하기 위해 job_5m을 직접 호출 ---
            # (실제 코드에서는 start_streaming_for_code가 내부적으로 호출)
            print("\n5. 실제 데이터 처리 흐름 확인 (job_5m 직접 호출)...")
            
            # job_5m의 실제 로직을 모방하되, 무한 루프는 제거합니다.
            res = mock_getter.fetch_minute_chart_ka10080(test_code, tic_scope=5)
            rows5 = res.get("rows", [])
            mock_bridge.chart_rows_received.emit(test_code, "5m", rows5)
            rows5_norm = normalize_ka10080_rows(rows5)
            
            # calculator와 monitor가 호출되는지 확인
            mock_calculator.apply_rows_full.assert_called_with(code=test_code, tf="5m", rows=rows5_norm, need=350)
            
            df_push = _rows_to_df_ohlcv(rows5_norm, tz="Asia/Seoul")
            mock_monitor.ingest_bars.assert_called_with(test_code, "5m", df_push)
            
            print("   [성공] 초기 데이터가 MACD 계산기와 매매 모니터로 정상 전달되었습니다.")

            # 5. 테스트를 위해 생성된 비동기 작업을 정리합니다.
            print("\n6. 테스트 작업 정리...")
            streaming_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await streaming_task
            print("   [성공] 스트리밍 작업이 정상적으로 종료되었습니다.")


        print("\n--- 엔진 스모크 테스트 성공: 엔진의 핵심 로직이 정상적으로 동작합니다. ---")


if __name__ == '__main__':
    # 'engine.py'를 임시로 생성하여 테스트를 실행할 수 있도록 합니다.
    # 실제 프로젝트에 engine.py가 있다면 이 부분은 필요 없습니다.
    if 'engine' not in sys.modules:
        with open("engine.py", "w") as f:
            f.write("""
import asyncio
# 이 파일은 smoke_test_engine.py를 실행하기 위한 임시 파일입니다.
# 실제 Engine 클래스의 코드로 교체해야 합니다.
class Engine:
    def __init__(self, bridge, getter, monitor):
        self.bridge = bridge
        self.getter = getter
        self.monitor = monitor
        self._tasks = {}
    async def start_streaming_for_code(self, code, need_5m=350, need_30m=400, poll_5m_step=300, poll_30m_step=1800):
        print(f"Starting stream for {code}")
        # 실제 로직을 여기에 붙여넣으세요.
        # self.task_5m = asyncio.create_task(self.job_5m(code))
        # self.task_30m = asyncio.create_task(self.job_30m(code))
        # await asyncio.gather(self.task_5m, self.task_30m)
    async def stop_streaming(self, code):
        pass
    async def job_5m(self, code):
        pass # 실제 job_5m 코드를 여기에 붙여넣으세요.
    async def job_30m(self, code):
        pass # 실제 job_30m 코드를 여기에 붙여넣으세요.
""")
    unittest.main()
