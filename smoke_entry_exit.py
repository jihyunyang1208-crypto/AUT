# smoke_test_autotrader.py
import asyncio
import uuid
from datetime import datetime, timezone

# === 1) 프로젝트 모듈 임포트 ===
from trade_pro.auto_trader import AutoTrader, TradeSettings, LadderSettings

# === 2) 브리지/포지션 더미 ===
class DummyEmitter:
    def emit(self, msg):
        print(" [EMIT]", msg)

class DummyBridge:
    def __init__(self):
        self.log = DummyEmitter()
        self.order_event = DummyEmitter()

# === 3) 시뮬 엔진 더미 ===
class FakeSimEngine:
    """프로덕션 없이 동작 확인용으로만 사용. order_id는 임의 UUID 조각."""
    def __init__(self, log_fn=None):
        self._log = log_fn or (lambda m: print(m))
        self._n = 0

    def _oid(self, prefix):
        self._n += 1
        return f"{prefix}-{uuid.uuid4().hex[:8]}-{self._n}"

    # --- BUY ---
    def submit_market_buy(self, *, stk_cd, qty, parent_uid=None, strategy=""):
        oid = self._oid("SIM-MKT-BUY")
        self._log(f"  FakeSimEngine.submit_market_buy: {stk_cd} x{qty} → {oid}")
        return oid

    def submit_limit_buy(self, *, stk_cd, limit_price, qty, parent_uid=None, strategy=""):
        oid = self._oid("SIM-LMT-BUY")
        self._log(f"  FakeSimEngine.submit_limit_buy: {stk_cd} x{qty} @ {limit_price} → {oid}")
        return oid

    # --- SELL ---
    def submit_market_sell(self, *, stk_cd, qty, parent_uid=None, strategy=""):
        oid = self._oid("SIM-MKT-SELL")
        self._log(f"  FakeSimEngine.submit_market_sell: {stk_cd} x{qty} → {oid}")
        return oid

    def submit_limit_sell(self, *, stk_cd, limit_price, qty, parent_uid=None, strategy=""):
        oid = self._oid("SIM-LMT-SELL")
        self._log(f"  FakeSimEngine.submit_limit_sell: {stk_cd} x{qty} @ {limit_price} → {oid}")
        return oid

    # --- 시세 이벤트 (옵션) ---
    def on_market_update(self, event: dict):
        self._log(f"  FakeSimEngine.on_market_update: {event}")

# === 4) 모듈 스코프의 SimEngine 심기 ===
# AutoTrader.__init__에서 trade_pro.auto_trader 모듈 스코프의 SimEngine 심볼을 참조하므로,
# 여기서 테스트용 FakeSimEngine으로 치환한다.
import trade_pro.auto_trader as at_mod
at_mod.SimEngine = FakeSimEngine

# === 5) 공용 로그 함수 ===
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

# === 6) 라이브 모드 더미 HTTP 패치 ===
def make_live_stub_methods(trader: AutoTrader):
    def _fn_kt10000_stub(token: str, data: dict, cont_yn: str = "N", next_key: str = "") -> dict:
        log(f"  LIVE_STUB kt10000(BUY) called: token={bool(token)} data={data}")
        return {
            "status_code": 200,
            "header": {"api-id": "kt10000", "cont-yn": cont_yn, "next-key": next_key},
            "body": {"return_code": 0, "return_msg": "OK(STUB)"},
        }

    def _fn_kt10001_stub(token: str, data: dict, cont_yn: str = "N", next_key: str = "") -> dict:
        log(f"  LIVE_STUB kt10001(SELL) called: token={bool(token)} data={data}")
        return {
            "status_code": 200,
            "header": {"api-id": "kt10001", "cont-yn": cont_yn, "next-key": next_key},
            "body": {"return_code": 0, "return_msg": "OK(STUB)"},
        }

    trader._fn_kt10000 = _fn_kt10000_stub  # type: ignore
    trader._fn_kt10001 = _fn_kt10001_stub  # type: ignore

# === 7) 스모크 시나리오 ===
async def smoke_simulation_mode():
    print("\n===== A) SIMULATION MODE =====")
    settings = TradeSettings(
        master_enable=True,
        auto_buy=True, auto_sell=True,
        order_type="market",
        simulation_mode=True,      # 시뮬 강제 On
    )
    trader = AutoTrader(
        settings=settings,
        ladder=LadderSettings(unit_amount=100_000, num_slices=1),  # 단일 BUY를 ladder(1슬라이스)로
        token_provider=lambda: "FAKE_TOKEN",   # 시뮬에서는 사용되지 않음
        log=log,
        bridge=DummyBridge(),
    )

    # 1) 단일 BUY (시장가) → handle_signal로 진입하면 ladder(1슬라이스) 경로를 탑니다.
    buy_result = await trader.handle_signal({
        "signal": "BUY",
        "data": {
            "dmst_stex_tp": "KRX",
            "stk_cd": "005930",
            "ord_qty": "10",     # ladder 경로에서는 qty 대신 unit_amount/price로 계산
            "ord_uv": "72000",   # 현재가로 사용됨
            "trde_tp": "3",      # 시장가
        }
    })
    print("→ BUY(sim) result:", buy_result)

    # 2) 단일 SELL (지정가) → _handle_simple_sell 경로
    sell_result = await trader.handle_signal({
        "signal": "SELL",
        "data": {
            "dmst_stex_tp": "KRX",
            "stk_cd": "005930",
            "ord_qty": "3",
            "ord_uv": "72500",
            "trde_tp": "0",      # 지정가
        }
    })
    print("→ SELL(sim) result:", sell_result)

async def smoke_live_mode():
    print("\n===== B) LIVE MODE (HTTP STUB) =====")
    settings = TradeSettings(
        master_enable=True,
        auto_buy=True, auto_sell=True,
        order_type="limit",
        simulation_mode=False,    # 라이브 강제
    )
    trader = AutoTrader(
        settings=settings,
        ladder=LadderSettings(unit_amount=200_000, num_slices=1),
        token_provider=lambda: "FAKE_TOKEN",   # 더미 토큰
        base_url_provider=lambda: "https://example.invalid",  # 실제 호출 안 함(스텁으로 대체)
        log=log,
        bridge=DummyBridge(),
        use_mock=True,            # 로깅 메시지만 다름
    )
    # HTTP 호출 스텁 주입
    make_live_stub_methods(trader)

    # 1) 단일 BUY (지정가) → ladder(1슬라이스) + kt10000 스텁
    buy_result = await trader.handle_signal({
        "signal": "BUY",
        "data": {
            "dmst_stex_tp": "KRX",
            "stk_cd": "000660",
            "ord_qty": "7",
            "ord_uv": "145000",
            "trde_tp": "0",
        }
    })
    print("→ BUY(live-stub) result:", buy_result)

    # 2) 단일 SELL (시장가) → _handle_simple_sell + kt10001 스텁
    sell_result = await trader.handle_signal({
        "signal": "SELL",
        "data": {
            "dmst_stex_tp": "KRX",
            "stk_cd": "000660",
            "ord_qty": "4",
            "ord_uv": "0",
            "trde_tp": "3",   # 시장가
        }
    })
    print("→ SELL(live-stub) result:", sell_result)

async def main():
    await smoke_simulation_mode()
    await smoke_live_mode()
    print("\n✅ Smoke tests finished (no real network I/O). Check logs/trades/*.csv for records.")

if __name__ == "__main__":
    asyncio.run(main())
