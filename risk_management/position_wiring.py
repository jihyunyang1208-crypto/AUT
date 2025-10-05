from __future__ import annotations
cond_id=data.get("cond_id") or data.get("condition") or data.get("strategy") or "default",
code=str(data.get("code")),
side=str(data.get("side")),
qty=float(data.get("qty", 0) or 0),
price=float(data.get("price", 0) or 0),
ts=data.get("ts"),
fee=float(data.get("fee", 0) or 0),
)


def _on_price_payload(self, payload: Any, *args, **kwargs):
data = self._normalize_price(payload, *args, **kwargs)
if not data:
return
self.pnl.on_price(
code=str(data.get("code")),
price=float(data.get("price", 0) or 0),
ts=data.get("ts"),
)


def _on_trade_like_payload(self, payload: dict):
"""브리지 구버전 호환: UI가 받던 trade_signal을 체결로 해석(옵션)."""
if not isinstance(payload, dict):
return
self._on_fill_payload(payload)


# -------------------------
# Normalizers
# -------------------------
def _normalize_fill(self, payload: Any, *args, **kwargs) -> Optional[dict]:
# dict 형태 우선 지원
if isinstance(payload, dict):
# 키 변형 매핑
return {
"ts": payload.get("ts") or payload.get("time") or payload.get("timestamp"),
"cond_id": payload.get("cond_id") or payload.get("condition_name") or payload.get("strategy"),
"code": payload.get("code") or payload.get("stock_code") or payload.get("ticker"),
"side": payload.get("side") or payload.get("ord_side") or payload.get("type"),
"qty": payload.get("qty") or payload.get("quantity") or payload.get("filled"),
"price": payload.get("price") or payload.get("fill_price") or payload.get("avg_px"),
"fee": payload.get("fee") or payload.get("commission") or 0.0,
}
# tuple/list 형태도 최소 지원: (ts, cond_id, code, side, qty, price)
if isinstance(payload, (tuple, list)) and len(payload) >= 6:
ts, cond_id, code, side, qty, price = payload[:6]
return {
"ts": ts, "cond_id": cond_id, "code": code,
"side": side, "qty": qty, "price": price,
}
# 기타는 미지원
return None


def _normalize_price(self, payload: Any, *args, **kwargs) -> Optional[dict]:
if isinstance(payload, dict):
return {
"ts": payload.get("ts") or payload.get("time") or payload.get("timestamp"),
"code": payload.get("code") or payload.get("stock_code") or payload.get("ticker"),
"price": payload.get("price") or payload.get("last") or payload.get("stck_prpr"),
}
if isinstance(payload, (tuple, list)) and len(payload) >= 3:
ts, code, price = payload[:3]
return {"ts": ts, "code": code, "price": price}
return None