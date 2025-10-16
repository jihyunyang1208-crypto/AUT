# tests/smoke_test_risk_dashboard_ui.py
from __future__ import annotations

import sys
import traceback
import pandas as pd

from PySide6.QtCore import QObject, Signal, Slot, QTimer
from PySide6.QtWidgets import QApplication

# ---- 프로젝트 클래스 임포트 (경로는 프로젝트 구조에 맞게) ----
# 1) 브리지/메인윈도우는 main.py에 있다고 가정
try:
    from main import AsyncBridge, MainWindow
except Exception as e:
    print("[SMOKE-UI] main.py에서 AsyncBridge/MainWindow 임포트 실패:", e)
    traceback.print_exc()
    # 간이 브리지/더미 UI로 대체
    class AsyncBridge(QObject):
        pnl_snapshot_ready = Signal(dict)
        new_stock_received = Signal(dict)
        new_stock_detail_received = Signal(dict)
        log_message = Signal(str)
        price_update = Signal(dict)
        fill_or_trade = Signal(dict)

    class MainWindow(QObject):
        def __init__(self, *args, **kwargs):
            super().__init__()
        @Slot(dict)
        def on_pnl_snapshot(self, snap: dict):
            port = (snap or {}).get("portfolio", {})
            equity = float(port.get("equity", 0))
            daily_pct = float(port.get("daily_pnl_pct", 0))
            print(f"[SMOKE-UI] (DummyMain) pnl_snapshot: equity={equity:,.0f} daily%={daily_pct:+.2f}%")

        # 테스트 편의: show() 시그니처만 흉내
        def show(self):
            print("[SMOKE-UI] Dummy MainWindow shown.")

# 2) 포지션/와이어링
try:
    from trade_pro.position_manager import PositionManager
except Exception as e:
    print("[SMOKE-UI] PositionManager 임포트 실패:", e)
    traceback.print_exc()
    sys.exit(1)

try:
    from risk_management.position_wiring import PositionWiring

except Exception as e:
    print("[SMOKE-UI] PositionWiring 임포트 실패:", e)
    traceback.print_exc()
    sys.exit(1)

# 3) MainWindow 생성에 필요한 인자들(엔진/콜백)은 더미로 대체 가능
class DummyEngine(QObject):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
    def start_loop(self):
        # UI 테스트에선 불필요. 로그만 찍음
        print("[SMOKE-UI] DummyEngine.start_loop()")

def dummy_perform_filtering(*args, **kwargs):
    print("[SMOKE-UI] dummy_perform_filtering called.")


def main():
    app = QApplication(sys.argv)

    # ---- Bridge / Position / Wiring ----
    bridge = AsyncBridge()
    pm = PositionManager(base_cash=100_000_000, tz="Asia/Seoul")
    wiring = PositionWiring(pm, bridge)
    wiring.setup_pnl_snapshot_flow(interval_sec=1)  # 빠른 피드백

    # ---- Wiring 슬롯에 브리지 신호 연결 ----
    try:
        bridge.price_update.connect(wiring.on_price_update)
        bridge.fill_or_trade.connect(wiring.on_fill_or_trade)
    except Exception as e:
        print("[SMOKE-UI] AsyncBridge에 price_update/fill_or_trade 시그널이 필요합니다:", e)
        sys.exit(2)

    # ---- UI(MainWindow) 띄우기 ----
    # main.py의 MainWindow 시그니처에 맞춰 인자 전달
    try:
        ui = MainWindow(
            bridge=bridge,
            engine=DummyEngine(),
            perform_filtering_cb=dummy_perform_filtering,
            project_root=".",
        )
    except TypeError:
        # 시그니처가 다를 경우 최소 인자만으로 생성 시도
        ui = MainWindow(bridge)  # 필요시 수정
    except Exception as e:
        print("[SMOKE-UI] MainWindow 생성 실패:", e)
        traceback.print_exc()
        sys.exit(3)

    # pnl 스냅샷 수신 연결
    try:
        bridge.pnl_snapshot_ready.connect(ui.on_pnl_snapshot)
    except Exception as e:
        print("[SMOKE-UI] ui.on_pnl_snapshot 연결 실패:", e)
        traceback.print_exc()
        sys.exit(4)

    ui.show()

    # ---- 시나리오: 체결/가격 이벤트를 UI에 반영시키기 ----
    def step_1_buy():
        bridge.fill_or_trade.emit({
            "ts": pd.Timestamp.now(tz="Asia/Seoul"),
            "condition_name": "SMOKE-UI",
            "stock_code": "005930",
            "side": "BUY",
            "qty": 2,
            "price": 70000,
            "fee": 30,
        })
        print("[SMOKE-UI] step1: BUY 005930 x2 @70000")

    def step_2_price_701():
        bridge.price_update.emit({
            "ts": pd.Timestamp.now(tz="Asia/Seoul"),
            "stock_code": "005930",
            "price": 70100,
        })
        print("[SMOKE-UI] step2: price → 70100")

    def step_3_price_705():
        bridge.price_update.emit({
            "ts": pd.Timestamp.now(tz="Asia/Seoul"),
            "stock_code": "005930",
            "price": 70500,
        })
        print("[SMOKE-UI] step3: price → 70500")

    def step_4_sell_half():
        bridge.fill_or_trade.emit({
            "ts": pd.Timestamp.now(tz="Asia/Seoul"),
            "condition_name": "SMOKE-UI",
            "stock_code": "005930",
            "side": "SELL",
            "qty": 1,
            "price": 70400,
            "fee": 30,
        })
        print("[SMOKE-UI] step4: SELL 005930 x1 @70400")

    def step_5_price_699():
        bridge.price_update.emit({
            "ts": pd.Timestamp.now(tz="Asia/Seoul"),
            "stock_code": "005930",
            "price": 69900,
        })
        print("[SMOKE-UI] step5: price → 69900")

    def finish():
        print("[SMOKE-UI] ✅ UI 스모크 종료")

    # ---- 타임라인 (메인 스레드에서 순차 실행) ----
    QTimer.singleShot(150, step_1_buy)
    QTimer.singleShot(450, step_2_price_701)
    QTimer.singleShot(850, step_3_price_705)
    QTimer.singleShot(1250, step_4_sell_half)
    QTimer.singleShot(1650, step_5_price_699)
    # 스냅샷 타이머가 1초이므로, 3초 후 종료하면 2~3회 on_pnl_snapshot 수신됨
    QTimer.singleShot(3200, finish)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
