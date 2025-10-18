# broker/mirae.py
import os, requests
from typing import Dict, Any, Optional
from .base import Broker, OrderRequest, OrderResponse

class MiraeAssetBroker(Broker):
    """미래에셋 오픈API 규격에 맞춰 URL/헤더/필드 매핑을 보강하세요."""
    def __init__(self, *, token_provider, base_url: Optional[str] = None, timeout=10):
        self._token_provider = token_provider
        self._base_url = (base_url or os.getenv("MIRAE_BASE", "https://openapi.miraeasset.com")).rstrip("/")
        self._timeout = timeout

    def name(self) -> str:
        return "mirae"

    def place_order(self, req: OrderRequest) -> OrderResponse:
        token = self._token_provider()
        url = self._base_url + "/uapi/domestic-stock/v1/trading/order"  # 예시
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "appkey": os.getenv("MIRAE_APP_KEY", ""),
            "appsecret": os.getenv("MIRAE_APP_SECRET", ""),
        }
        # 예시 매핑(실 운영 스펙으로 수정 필요)
        ord_dvsn = "00" if req.trde_tp == "0" else "03"   # 00: 지정, 03: 시장(예시)
        body = {
            "PDNO": req.stk_cd,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(req.ord_qty),
            "ORD_UNPR": "0" if req.ord_uv is None else str(req.ord_uv),
            "BUYSELL_DVSN": "01" if req.side.upper() == "BUY" else "02",
        }
        r = requests.post(url, headers=headers, json=body, timeout=self._timeout)
        header_subset = {k: r.headers.get(k) for k in ["X-RateLimit-Remaining", "Date"]}
        try:
            body_js = r.json()
        except Exception:
            body_js = {"raw": r.text}
        return OrderResponse(status_code=r.status_code, header=header_subset, body=body_js)
