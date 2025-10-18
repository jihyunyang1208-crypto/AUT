# broker/kiwoom.py
import requests
from typing import Dict, Any, Optional
from .base import Broker, OrderRequest, OrderResponse

class KiwoomRestBroker(Broker):
    def __init__(self, *, token_provider, base_url: Optional[str] = None, api_id_buy="kt10000", api_id_sell="kt10001", timeout=10):
        self._token_provider = token_provider
        self._base_url = (base_url or "https://api.kiwoom.com").rstrip("/")
        self._api_id_buy = api_id_buy
        self._api_id_sell = api_id_sell
        self._timeout = timeout

    def name(self) -> str:
        return "kiwoom"

    def _headers(self, token: str, api_id: str) -> Dict[str, str]:
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": api_id,
        }

    def place_order(self, req: OrderRequest) -> OrderResponse:
        token = self._token_provider()
        url = f"{self._base_url}/api/dostk/ordr"
        api_id = self._api_id_buy if req.side.upper() == "BUY" else self._api_id_sell
        body = {
            "dmst_stex_tp": req.dmst_stex_tp,
            "stk_cd": req.stk_cd,
            "ord_qty": str(req.ord_qty),
            "ord_uv": "" if req.ord_uv is None else str(req.ord_uv),
            "trde_tp": req.trde_tp,
            "cond_uv": req.cond_uv,
        }
        r = requests.post(url, headers=self._headers(token, api_id), json=body, timeout=self._timeout)
        header_subset = {k: r.headers.get(k) for k in ["next-key", "cont-yn", "api-id"]}
        try:
            body_js = r.json()
        except Exception:
            body_js = {"raw": r.text}
        return OrderResponse(status_code=r.status_code, header=header_subset, body=body_js)
