# smoke_test_unified_autotrader.py
import asyncio
import sys
import types
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List

# ─────────────────────────────────────────────────────────
# 0) 가짜 SimEngine 주입 (simulation 모드 전용)
# ─────────────────────────────────────────────────────────
class _FakeSimEngine:
    def __init__(self, log_fn=None):
        self._log = log_fn or (lambda m: None)
        self._n = 0
    def _oid(self, prefix):
        self._n += 1
        return f"{prefix}-{uuid.uuid4().hex[:8]}-{self._n}"
    def submit_market_buy(self, *, stk_cd, qty, parent_uid=None, strategy=None):
        oid = self._oid("SIM-MKT-BUY"); self._log(f"  Sim.buy MKT {stk_cd} x{qty} → {oid}"); return oid
    def submit_limit_buy(self, *, stk_cd, limit_price, qty, parent_uid=None, strategy=None):
        oid = self._oid("SIM-LMT-BUY"); self._log(f"  Sim.buy LMT {stk_cd} x{qty} @ {limit_price} → {oid}"); return oid
    def submit_market_sell(self, *, stk_cd, qty, parent_uid=None, strategy=None):
        oid = self._oid("SIM-MKT-SELL"); self._log(f"  Sim.sell MKT {stk_cd} x{qty} → {oid}"); return oid
    def submit_limit_sell(self, *, stk_cd, limit_price, qty, parent_uid=None, strategy=None):
        oid = self._oid("SIM-LMT-SELL"); self._log(f"  Sim.sell LMT {stk_cd} x{qty} @ {limit_price} → {oid}"); return oid
    def on_market_update(self, event: dict): self._log(f"  [tick] {event}")

_sim_mod = types.ModuleType("simulator.sim_engine")
_sim_mod.SimEngine = _FakeSimEngine
sys.modules["simulator"] = types.ModuleType("simulator")
sys.modules["simulator.sim_engine"] = _sim_mod

# ─────────────────────────────────────────────────────────
# 1) AutoTrader 임포트 (여러 경로 호환)
# ─────────────────────────────────────────────────────────
AutoTrader = TradeSettings = LadderSettings = None
_errors = []
for path in ("trade_pro.auto_trader", "core.auto_trader", "auto_trader"):
    try:
        mod = __import__(path, fromlist=["*"])
        AutoTrader = getattr(mod, "AutoTrader")
        TradeSettings = getattr(mod, "TradeSettings")
        LadderSettings = getattr(mod, "LadderSettings")
        break
    except Exception as e:
        _errors.append((path, e))
if AutoTrader is None:
    raise ImportError("AutoTrader import failed. Tried:\n" + "\n".join(f"- {p}: {e}" for p, e in _errors))

# ─────────────────────────────────────────────────────────
# 2) 더미 브리지 & 포지션 매니저 (이벤트 수집용)
# ─────────────────────────────────────────────────────────
class _EventCollector:
    def __init__(self): self.events: List[Dict[str, Any]] = []
    def emit(self, evt): self.events.append(evt)

class _DummyBridge:
    def __init__(self, collector: _EventCollector):
        self.log = _EventCollector()             # 로그는 버려도 무방
        self.order_event = collector             # 신뢰할 이벤트 수집

class _DummyPM:
    def __init__(self, qty_map=None, pend=None):
        self.qty_map = qty_map or {}
        self.pending = pend or defaultdict(lambda: (0, 0))
    def get_qty(self, symbol: str) -> int:
        s = str(symbol)[-6:].zfill(6)
        return int(self.qty_map.get(s, 0))
    def get_pending(self, symbol: str):
        s = str(symbol)[-6:].zfill(6)
        return self.pending.get(s, (0, 0))
    def apply_fill_buy(self, symbol: str, qty: int, price: float):
        s = str(symbol)[-6:].zfill(6)
        self.qty_map[s] = self.get_qty(s) + int(qty)
    def apply_fill_sell(self, symbol: str, qty: int, price: float):
        s = str(symbol)[-6:].zfill(6)
        self.qty_map[s] = max(0, self.get_qty(s) - int(qty))

# ─────────────────────────────────────────────────────────
# 3) 유틸: 섹션별 이벤트 슬라이스
# ─────────────────────────────────────────────────────────
def slice_new_events(col: _EventCollector, before_len: int) -> List[Dict[str, Any]]:
    return col.events[before_len:]

def filter_events(evts, *, action=None, symbol=None, status=None):
    out = []
    for e in evts:
        if action and e.get("action") != action: continue
        if symbol and e.get("symbol") != symbol: continue
        if status and e.get("status") != status: continue
        out.append(e)
    return out

def assert_true(cond, msg):
    if not cond:
        raise AssertionError("FAIL: " + msg)
    print("PASS:", msg)

# ─────────────────────────────────────────────────────────
# 4) 테스트 러너
# ─────────────────────────────────────────────────────────
async def run_tests():
    print("===== SMOKE (unified handle_signal / thin make_on_signal) =====")

    # 공통 라더 파라미터 (테스트 빨리 돌도록 interval 짧게)
    ladder = LadderSettings(
        unit_amount=100_000, num_slices=3,
        start_ticks_below=1, step_ticks=1,
        start_ticks_above=1, min_qty=1,
        interval_sec=0.01
    )

    # === A) on_signal_use_ladder=True: on_signal BUY=ladder_buy, SELL=ladder_sell ===
    colA = _EventCollector()
    bridgeA = _DummyBridge(colA)
    pmA = _DummyPM(qty_map={"005930": 11})  # 보유 11주 → SELL ladder 분할 기대
    atA = AutoTrader(
        settings=TradeSettings(
            auto_buy=True, auto_sell=True,
            order_type="limit",
            simulation_mode=True,
            ladder_sell_enable=True,
            on_signal_use_ladder=True,  # 핵심
        ),
        ladder=ladder,
        bridge=bridgeA,
        position_mgr=pmA,
        use_mock=True,
    )
    handlerA = atA.make_on_signal(bridgeA)

    # A1: BUY (on_signal) → ladder_buy (3슬라이스)
    before = len(colA.events)
    class Sig: 
        def __init__(self, side, symbol, price): self.side, self.symbol, self.price = side, symbol, price
    handlerA(Sig("BUY", "005930", 71900))
    await asyncio.sleep(0.1)
    evtsA1 = slice_new_events(colA, before)
    buy_news = filter_events(evtsA1, action="BUY", symbol="005930", status="SIM_SUBMIT")
    assert_true(len(buy_news) == ladder.num_slices, f"A1 BUY ladder events == {ladder.num_slices}")

    # A2: SELL (on_signal) → ladder_sell (3슬라이스), 총 11주 분할
    before = len(colA.events)
    handlerA(Sig("SELL", "005930", 72500))
    await asyncio.sleep(0.1)
    evtsA2 = slice_new_events(colA, before)
    sell_news = filter_events(evtsA2, action="SELL", symbol="005930", status="SIM_SUBMIT")
    assert_true(len(sell_news) == ladder.num_slices, f"A2 SELL ladder events == {ladder.num_slices}")
    total_qty = sum(int(e.get("qty", 0)) for e in sell_news)
    assert_true(total_qty == 11, "A2 SELL total_qty == PositionManager qty (11)")

    # === B) handle_signal 직접 호출 (mode 미지정) → 자동 보강으로 ladder 동작 ===
    colB = _EventCollector()
    bridgeB = _DummyBridge(colB)
    atB = AutoTrader(
        settings=TradeSettings(
            auto_buy=True, auto_sell=True,
            order_type="limit",
            simulation_mode=True,
            on_signal_use_ladder=True,  # 자동 보강 켬
        ),
        ladder=ladder,
        bridge=bridgeB,
        position_mgr=_DummyPM(qty_map={"000660": 5}),
        use_mock=True,
    )
    # B1: BUY minimal payload → ladder_buy로 3슬라이스 기대
    before = len(colB.events)
    payloadB1 = {"signal": "BUY", "data": {"stk_cd": "000660", "dmst_stex_tp": "KRX", "ord_uv": "121000"}}
    await atB.handle_signal(payloadB1)
    await asyncio.sleep(0.1)
    evtsB1 = slice_new_events(colB, before)
    buysB = filter_events(evtsB1, action="BUY", symbol="000660", status="SIM_SUBMIT")
    assert_true(len(buysB) == ladder.num_slices, "B1 BUY auto-ladder events == num_slices")

    # B2: SELL minimal payload → ladder_sell로 3슬라이스 기대 (총 5주)
    before = len(colB.events)
    payloadB2 = {"signal": "SELL", "data": {"stk_cd": "000660", "dmst_stex_tp": "KRX", "ord_uv": "121000"}}
    await atB.handle_signal(payloadB2)
    await asyncio.sleep(0.1)
    evtsB2 = slice_new_events(colB, before)
    sellsB = filter_events(evtsB2, action="SELL", symbol="000660", status="SIM_SUBMIT")
    assert_true(len(sellsB) == ladder.num_slices, "B2 SELL auto-ladder events == num_slices")
    assert_true(sum(int(e.get("qty", 0)) for e in sellsB) == 5, "B2 SELL total_qty == PM qty (5)")

    # === C) on_signal_use_ladder=False: fallback 확인 (BUY=1슬라이스, SELL=원샷) ===
    colC = _EventCollector()
    bridgeC = _DummyBridge(colC)
    pmC = _DummyPM(qty_map={"035720": 8})
    atC = AutoTrader(
        settings=TradeSettings(
            auto_buy=True, auto_sell=True,
            order_type="limit",
            simulation_mode=True,
            on_signal_use_ladder=False,  # 자동 보강 끔 → fallback
        ),
        ladder=ladder,
        bridge=bridgeC,
        position_mgr=pmC,
        use_mock=True,
    )
    handlerC = atC.make_on_signal(bridgeC)

    # C1: BUY (fallback → 1슬라이스 라더)
    before = len(colC.events)
    handlerC(Sig("BUY", "035720", 131000))
    await asyncio.sleep(0.1)
    evtsC1 = slice_new_events(colC, before)
    buysC = filter_events(evtsC1, action="BUY", symbol="035720", status="SIM_SUBMIT")
    assert_true(len(buysC) == 1, "C1 BUY fallback → 1 slice")

    # C2: SELL (fallback → simple sell 1건, qty=8)
    before = len(colC.events)
    handlerC(Sig("SELL", "035720", 132000))
    await asyncio.sleep(0.1)
    evtsC2 = slice_new_events(colC, before)
    sellsC = filter_events(evtsC2, action="SELL", symbol="035720", status="SIM_SUBMIT")
    # simple_sell 도 SIM_SUBMIT 한 건으로 기록됨
    assert_true(len(sellsC) == 1, "C2 SELL fallback → single simple sell")
    assert_true(int(sellsC[0].get("qty", 0)) == 8, "C2 SELL qty == PM qty (8)")

    # === D) 레거시 제거 확인 ===
    assert_true(not hasattr(AutoTrader, "make_on_signal_legacy"), "D legacy method removed (no make_on_signal_legacy)")

    print("\n===== ALL TESTS PASSED =====")

def main():
    try:
        asyncio.run(run_tests())
    except AssertionError as e:
        print(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        print("Interrupted")
        sys.exit(130)

if __name__ == "__main__":
    main()
