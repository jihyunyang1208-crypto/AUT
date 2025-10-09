# simulator/sim_engine.py
from __future__ import annotations
import time, uuid
from typing import Callable, Dict, Any, Optional, List

class SimEngine:
    """
    논리 레벨 시뮬레이터:
      - limit BUY/SELL 주문을 큐에 보관
      - on_market_update(event)로 체결 시뮬
      - AutoTrader의 position_mgr 및 이벤트 emition은 AutoTrader 쪽에서 처리
    """
    def __init__(self, log_fn: Callable[[str], None]):
        self._log = log_fn
        self._orders: Dict[str, dict] = {}

    # ---- 주문 제출 ----
    def submit_limit_buy(self, *, stk_cd: str, limit_price: int, qty: int,
                         parent_uid: str, strategy: str) -> str:
        oid = uuid.uuid4().hex[:10]
        self._orders[oid] = {
            "side": "BUY",
            "stk_cd": stk_cd,
            "limit": int(limit_price),
            "qty": int(qty),
            "pid": parent_uid,
            "strategy": strategy,
            "ts": time.time(),
        }
        self._log(f"[sim] limit BUY {stk_cd} x{qty} @ {limit_price} (oid={oid})")
        return oid

    def submit_limit_sell(self, *, stk_cd: str, limit_price: Optional[int], qty: int,
                          parent_uid: str, strategy: str) -> str:
        oid = uuid.uuid4().hex[:10]
        self._orders[oid] = {
            "side": "SELL",
            "stk_cd": stk_cd,
            "limit": 0 if limit_price is None else int(limit_price),
            "qty": int(qty),
            "pid": parent_uid,
            "strategy": strategy,
            "ts": time.time(),
        }
        self._log(f"[sim] limit SELL {stk_cd} x{qty} @ {limit_price if limit_price is not None else 'MKT'} (oid={oid})")
        return oid

    # ---- 마켓 이벤트로 체결 시뮬 ----
    def on_market_update(self, event: Dict[str, Any]):
        """
        event 예시: {"symbol":"005930","last": 72800, "ts": "..."}
        BUY  : last <= limit → 체결
        SELL : last >= limit → 체결
        (시장가 SELL은 limit=0으로 저장되므로, last>0이면 바로 체결되도록 처리)
        """
        try:
            last = int(float(event.get("last") or 0))
        except Exception:
            return

        done: List[str] = []
        for oid, od in list(self._orders.items()):
            side = od.get("side")
            limit = int(od.get("limit") or 0)
            if side == "BUY" and last and (limit > 0) and (last <= limit):
                self._log(f"[sim] filled BUY {od['stk_cd']} x{od['qty']} @ {limit} (oid={oid})")
                done.append(oid)
            elif side == "SELL":
                # 시장가로 저장된 경우(limit==0) → 틱이 오면 즉시 체결
                if limit == 0 and last > 0:
                    self._log(f"[sim] filled SELL(MKT) {od['stk_cd']} x{od['qty']} @ {last} (oid={oid})")
                    done.append(oid)
                elif last and last >= limit and limit > 0:
                    self._log(f"[sim] filled SELL {od['stk_cd']} x{od['qty']} @ {limit} (oid={oid})")
                    done.append(oid)

        for oid in done:
            self._orders.pop(oid, None)
