# -*- coding: utf-8 -*-
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

# SUT
from trade_pro.auto_trader import (
    AutoTrader, TradeSettings, LadderSettings, TradeLogger
)

# -----------------------------
# Test Doubles (간단 목 구현체)
# -----------------------------
class MockOrderResponse:
    def __init__(self, status_code=200, header=None, body=None):
        self.status_code = status_code
        self.header = header or {"api-id": "mock", "cont-yn": "N", "next-key": ""}
        self.body = body or {"return_code": 0, "return_msg": "OK"}

class MockBroker:
    def __init__(self):
        self.calls = []

    def name(self) -> str:
        return "mock"

    def place_order(self, req):
        # 최소 필드만 기록 (연결 여부 검증 목적)
        self.calls.append({
            "stk_cd": req.stk_cd,
            "side": req.side,
            "trde_tp": req.trde_tp,
            "ord_uv": req.ord_uv,
            "ord_qty": req.ord_qty,
        })
        return MockOrderResponse(200)

class MockPositionManager:
    def __init__(self, qty_map=None, pend_buy=0, pend_sell=0):
        self.qty_map = qty_map or {}
        self.pend_buy = pend_buy
        self.pend_sell = pend_sell
        self.reserved = {"buy": [], "sell": []}
        self.fills = []

    # reserves
    def reserve_buy(self, code, qty):  self.reserved["buy"].append((code, qty))
    def release_buy(self, code, qty):  pass
    def reserve_sell(self, code, qty): self.reserved["sell"].append((code, qty))
    def release_sell(self, code, qty): pass

    # holdings
    def get_qty(self, code): return int(self.qty_map.get(code, 0))
    def get_pending(self, code): return (self.pend_buy, self.pend_sell)

    # fills
    def apply_fill_buy(self, code, qty, price):
        self.fills.append(("BUY", code, qty, price))
        self.qty_map[code] = self.get_qty(code) + qty

    def apply_fill_sell(self, code, qty, price):
        self.fills.append(("SELL", code, qty, price))
        self.qty_map[code] = max(0, self.get_qty(code) - qty)

class CaptureSignal:
    """Qt 없이도 emit 모방을 위한 객체"""
    def __init__(self, sink):
        self._sink = sink
    def emit(self, evt):
        self._sink.append(evt)

class MockBridge:
    def __init__(self):
        self.events = []
        self.order_event = CaptureSignal(self.events)
        self.logs = []
        self.log = CaptureSignal(self.logs)

# -----------------------------
# 공용 픽스처
# -----------------------------
@pytest.fixture
def mock_broker(monkeypatch):
    """AutoTrader가 내부에서 사용하는 create_broker를 목으로 치환"""
    mb = MockBroker()
    def _create_broker(**kwargs):
        return mb
    # SUT 네임스페이스의 create_broker 심
    import trade_pro.auto_trader as at_mod
    monkeypatch.setattr(at_mod, "create_broker", lambda **kw: _create_broker(**kw))
    return mb

@pytest.fixture
def position_mgr():
    return MockPositionManager()

@pytest.fixture
def trader(mock_broker, position_mgr):
    ts = TradeSettings(
        master_enable=True,
        auto_buy=True,
        auto_sell=True,
        order_type="limit",
        simulation_mode=True,      # 실제 체결 아님
        on_signal_use_ladder=True,
    )
    ls = LadderSettings(
        unit_amount=100_000,
        num_slices=5,
        start_ticks_below=1,
        step_ticks=1,
        start_ticks_above=1,
    )
    br = MockBridge()
    t = AutoTrader(settings=ts, ladder=ls, bridge=br, position_mgr=position_mgr, use_mock=True)
    return t

# -----------------------------
# 1) Ladder 계산이 1틱씩 변하는지(동적 틱)
# -----------------------------
def _assert_one_tick_down(trader: AutoTrader, prices):
    assert len(prices) >= 2
    for prev, cur in zip(prices, prices[1:]):
        expected = trader._krx_tick(prev)
        assert prev - cur == expected, f"down step != tick: prev={prev}, cur={cur}, tick={expected}"

def _assert_one_tick_up(trader: AutoTrader, prices):
    assert len(prices) >= 2
    for prev, cur in zip(prices, prices[1:]):
        expected = trader._krx_tick(prev)
        assert cur - prev == expected, f"up step != tick: prev={prev}, cur={cur}, tick={expected}"

def test_ladder_buy_dynamic_tick_1step_each(trader: AutoTrader):
    # 10,050원에서 시작 → 10,000 경계(틱 10) 아래로 내려가면 9,995(틱 5)로 바뀌어야 함
    cur_price = 10050
    prices = trader._compute_ladder_prices_dynamic(
        cur_price=cur_price, count=6, start_ticks_below=1, step_ticks=1, tick_fn=trader._krx_tick
    )
    # 예: [10040, 10030, 10020, 10010, 10000,  9995] 처럼 경계 통과 시 틱 전환
    _assert_one_tick_down(trader, prices)

def test_ladder_sell_dynamic_tick_1step_each(trader: AutoTrader):
    # SELL은 위로 → 99,950(틱 5)에서 100,000 경계 넘으면 틱 10으로
    cur_price = 99950
    # 동적 업 버전이 없다면 본 테스트는 skip(사용자 코드에 따라 구현 여부 다름)
    if not hasattr(trader, "_compute_ladder_prices_dynamic_up"):
        pytest.skip("dynamic_up helper가 아직 구현되지 않았습니다.")
    prices = trader._compute_ladder_prices_dynamic_up(
        cur_price=cur_price, count=6, start_ticks_above=1, step_ticks=1, tick_fn=trader._krx_tick
    )
    _assert_one_tick_up(trader, prices)

# -----------------------------
# 2) handle_signal 라우팅 연기 없이 동작
# -----------------------------
def test_handle_signal_routes_to_ladder_buy(trader: AutoTrader, mock_broker: MockBroker):
    payload = {
        "signal": "BUY",
        "data": {
            "stk_cd": "005930",
            "cur_price": 10050
        },
        "strategy": "test-cond"
    }
    ret = asyncio.run(trader.handle_signal(payload))
    assert any(call["side"] == "BUY" for call in mock_broker.calls), "BUY 주문이 발생해야 합니다."
    assert isinstance(ret, dict)

def test_handle_signal_routes_to_simple_sell_when_no_qty(
    trader: AutoTrader, mock_broker: MockBroker, position_mgr: MockPositionManager, monkeypatch
):
    # 보유 수량 없음 → ladder_sell이 아닌 simple_sell로 강등되는지 관찰
    position_mgr.qty_map["005930"] = 0

    called = {"simple_sell": 0}
    async def fake_simple_sell(payload):
        called["simple_sell"] += 1
        return {"ok": True}

    monkeypatch.setattr(trader, "_handle_simple_sell", fake_simple_sell)

    payload = {
        "signal": "SELL",
        "data": {
            "stk_cd": "005930",
            "cur_price": 10050
        },
        "strategy": "test-cond"
    }
    asyncio.run(trader.handle_signal(payload))
    assert called["simple_sell"] == 1, "보유 0이면 simple_sell 브랜치가 호출되어야 합니다."

# -----------------------------
# 3) WebSocket 이벤트 중복 체결 dedupe
# -----------------------------
def test_ws_fill_dedupe(trader: AutoTrader):
    # 동일 exec_id/part_seq 2회 → 1회만 ACCEPT
    msg = {
        "type": "FILL",
        "side": "BUY",
        "symbol": "005930",
        "filled_qty": 10,
        "fill_price": 10000,
        "exec_id": "X123",
        "part_seq": "1",
        "order_id": "O-1",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    trader.on_ws_message(msg)
    trader.on_ws_message(msg)  # duplicate
    # 이벤트는 1건만 ORDER_FILL이어야 함
    fills = [e for e in trader.bridge.events if e.get("type") == "ORDER_FILL"]
    assert len(fills) == 1, f"중복 체결이 제거되어야 합니다. got={len(fills)}"

# -----------------------------
# 4) TradeLogger 슬림 모드 파일 기록 스모크
# -----------------------------
def test_trade_logger_slim_writes(tmp_path: Path):
    tl = TradeLogger(log_dir=str(tmp_path), slim=True)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "strategy": "smoke",
        "action": "BUY",
        "stk_cd": "123456",
        "order_type": "limit",
        "price": 12345,
        "qty": 1,
        "status": "HTTP_200",
        "resp_code": 0,
        "resp_msg": "OK",
    }
    tl.write_order_record(rec)
    # 파일 존재 확인
    csv_file = next(tmp_path.glob("orders_*.csv"))
    jsonl_file = next(tmp_path.glob("orders_*.jsonl"))
    assert csv_file.exists()
    assert jsonl_file.exists()
    # 간단 무결성: 최소 길이 확인
    assert csv_file.stat().st_size > 0
    assert jsonl_file.stat().st_size > 0
