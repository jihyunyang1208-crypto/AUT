# smoke_test_autotrader.py
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

# === 프로젝트 모듈 ===
from trade_pro.auto_trader import AutoTrader, TradeSettings, LadderSettings

# (선택) 룰 컨텍스트용 pandas
try:
    import pandas as pd
except Exception:
    pd = None  # pandas 미설치여도 동작

# ─────────────────────────────────────────────────────────────
# 더미 브리지/로거
class DummyEmitter:
    def emit(self, msg):
        print(" [EMIT]", msg)

class DummyBridge:
    def __init__(self):
        self.log = DummyEmitter()
        self.order_event = DummyEmitter()

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

# ─────────────────────────────────────────────────────────────
# 시뮬 엔진 (지정가 전용)
class FakeSimEngine:
    """네트워크/실거래 없이 주문 경로만 검증하는 가짜 실행 엔진."""
    def __init__(self, log_fn=None):
        self._log = log_fn or (lambda m: print(m))
        self._n = 0

    def _oid(self, prefix):
        self._n += 1
        return f"{prefix}-{uuid.uuid4().hex[:8]}-{self._n}"

    # BUY (Limit)
    def submit_limit_buy(self, *, stk_cd, limit_price, qty, parent_uid=None, strategy=""):
        oid = self._oid("SIM-LMT-BUY")
        self._log(f"  Sim.buy LMT {stk_cd} x{qty} @ {limit_price} → {oid}")
        return oid

    # SELL (Limit)
    def submit_limit_sell(self, *, stk_cd, limit_price, qty, parent_uid=None, strategy=""):
        oid = self._oid("SIM-LMT-SELL")
        self._log(f"  Sim.sell LMT {stk_cd} x{qty} @ {limit_price} → {oid}")
        return oid

# AutoTrader 모듈 스코프의 SimEngine 치환 (시뮬 모드 경로에서 사용)
import trade_pro.auto_trader as at_mod
at_mod.SimEngine = FakeSimEngine

# ─────────────────────────────────────────────────────────────
# 라이브 HTTP 스텁 (네트워크 미사용)
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

# ─────────────────────────────────────────────────────────────
# 프로 룰 주입 (프로 경로 강제 진입용 스텁)
def inject_rules(trader: AutoTrader, *, enable_buy_pro: bool, enable_sell_pro: bool):
    """
    AutoTrader 내부의 _buy_rule_fn / _sell_rule_fn 슬롯에 스텁 룰을 주입.
    - 프로젝트 구현과 무관하게 '프로 경로'가 열려 있으면 반드시 True 반환하도록 보장.
    """
    def _buy_rule(ctx: dict) -> bool:
        # ctx(df5, avg_buy 등) 유무와 관계없이 True로 승인
        return True

    def _sell_rule(ctx: dict) -> bool:
        return True

    if enable_buy_pro and hasattr(trader, "_buy_rule_fn"):
        setattr(trader, "_buy_rule_fn", _buy_rule)
    if enable_sell_pro and hasattr(trader, "_sell_rule_fn"):
        setattr(trader, "_sell_rule_fn", _sell_rule)

# ─────────────────────────────────────────────────────────────
# 프로 호출 어댑터 (룰 + use_pro 힌트)
def _is_unhandled(res):
    return res is None or res is False or (isinstance(res, dict) and not res)

def _with_pro_ctx(data: dict, *, default_avg: float):
    ctx = dict(data)
    ctx.setdefault("avg_buy", float(data.get("ord_uv", 0)) or default_avg)
    if pd is not None:
        avg = ctx["avg_buy"]
        ctx.setdefault("df5", pd.DataFrame({
            "Open":  [avg - 50]*5,
            "High":  [avg + 50]*5,
            "Low":   [avg - 100]*5,
            "Close": [avg]*5,
            "Volume":[1000]*5,
        }))
    return ctx

async def call_buy(trader: AutoTrader, base_data: dict, *, use_pro: bool):
    if use_pro:
        data = dict(base_data)
        data["use_pro"] = True
        data = _with_pro_ctx(data, default_avg=70000.0)
        res = await trader.handle_signal({"signal": "BUY", "data": data})
        if not _is_unhandled(res):
            return res
    # 안전 탈출: 일반 BUY
    return await trader.handle_signal({"signal": "BUY", "data": dict(base_data)})

async def call_sell(trader: AutoTrader, base_data: dict, *, use_pro: bool):
    if use_pro:
        data = dict(base_data)
        data["use_pro"] = True
        data = _with_pro_ctx(data, default_avg=140000.0)
        res = await trader.handle_signal({"signal": "SELL", "data": data})
        if not _is_unhandled(res):
            return res
    # 안전 탈출: 일반 SELL
    return await trader.handle_signal({"signal": "SELL", "data": dict(base_data)})

# ─────────────────────────────────────────────────────────────
# 한 케이스 실행
async def run_one_case(*, simulation_mode: bool, auto_buy: bool, auto_sell: bool, buy_pro: bool, sell_pro: bool):
    mode = "SIM" if simulation_mode else "LIVE"
    print(f"\n===== {mode} | auto_buy:{auto_buy} auto_sell:{auto_sell} | buy_pro:{buy_pro} sell_pro:{sell_pro} | order_type:limit =====")

    # 주문 타입은 항상 limit (시장가 테스트 제외)
    settings = TradeSettings(
        master_enable=True,
        auto_buy=auto_buy,
        auto_sell=auto_sell,
        order_type="limit",
        simulation_mode=simulation_mode,
    )

    trader = AutoTrader(
        settings=settings,
        ladder=LadderSettings(unit_amount=150_000, num_slices=1),
        token_provider=lambda: "FAKE_TOKEN",
        base_url_provider=lambda: "https://example.invalid",
        log=log,
        bridge=DummyBridge(),
        use_mock=not simulation_mode,   # 라이브도 네트워크 미사용
    )
    if not simulation_mode:
        make_live_stub_methods(trader)

    # TradeSettings가 해당 필드를 지원한다면 주입(없으면 무시)
    if hasattr(trader, "settings"):
        if hasattr(trader.settings, "buy_pro"):
            trader.settings.buy_pro = bool(buy_pro)
        if hasattr(trader.settings, "sell_pro"):
            trader.settings.sell_pro = bool(sell_pro)

    # 프로 룰 훅 주입 (존재 시만)
    inject_rules(trader, enable_buy_pro=buy_pro, enable_sell_pro=sell_pro)

    # 공통 페이로드 (지정가) — cond_uv 키는 라이브 스텁에서 기대할 수 있어 기본 제공
    base_buy  = {"dmst_stex_tp": "KRX", "ord_qty": "7", "ord_uv": "72000",  "trde_tp": "0", "cond_uv": ""}
    base_sell = {"dmst_stex_tp": "KRX", "ord_qty": "4", "ord_uv": "145000", "trde_tp": "0", "cond_uv": ""}

    # 종목별 테스트: 005930, 000660, 437730
    cases = [
        ("005930", "BUY",  base_buy  | {"stk_cd": "005930"}),
        ("000660", "SELL", base_sell | {"stk_cd": "000660"}),
        ("437730", "BUY",  base_buy  | {"stk_cd": "437730"}),
        ("437730", "SELL", base_sell | {"stk_cd": "437730"}),
    ]

    for sym, side, payload in cases:
        if side == "BUY":
            res = await call_buy(trader, payload, use_pro=buy_pro)
            print(f"→ [{sym}] BUY  result:", res)
        else:
            res = await call_sell(trader, payload, use_pro=sell_pro)
            print(f"→ [{sym}] SELL result:", res)

# ─────────────────────────────────────────────────────────────
# 전체 러너
async def main():
    # 모든 조합:
    # 시뮬/라이브(2) × auto_buy(2) × auto_sell(2) × buy_pro(2) × sell_pro(2) = 32 케이스
    for simulation_mode in (True, False):
        for auto_buy in (True, False):
            for auto_sell in (True, False):
                for buy_pro in (True, False):
                    for sell_pro in (True, False):
                        await run_one_case(
                            simulation_mode=simulation_mode,
                            auto_buy=auto_buy,
                            auto_sell=auto_sell,
                            buy_pro=buy_pro,
                            sell_pro=sell_pro,
                        )

    print("\n✅ All smoke cases finished (limit orders only, no real network I/O).")
    print("   로그 파일은 AutoTrader 내부 로거가 활성화된 경우 기존과 동일한 경로(예: logs/trades/)에 저장됩니다.")

if __name__ == "__main__":
    asyncio.run(main())
