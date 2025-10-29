# broker/kiwoom.py
import requests
import logging
from typing import Dict, Any, Optional, List, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os

from .base import Broker, OrderRequest, OrderResponse

# --- 로깅 설정 ---
logger = logging.getLogger(__name__)
DEBUG_TAG = "[KIWOOM_REST_BROKER]"
# -----------------
AccountCtx = Dict[str, Any]  # {"token":str, "acc_no":str|None, "enabled":bool, "alias":str|None}
def _load_accounts_from_env() -> List[AccountCtx]:
    raw = os.getenv("KIWOOM_ACCOUNTS_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception:
        logger.warning(f"{DEBUG_TAG} Failed to parse KIWOOM_ACCOUNTS_JSON")
    return []

class KiwoomRestBroker(Broker):
    def __init__(
        self,
        *,
        token_provider: Callable[[], Any],
        base_url: Optional[str] = None,
        api_id_buy: str = "kt10000",
        api_id_sell: str = "kt10001",
        timeout: int = 10,
        account_provider: Optional[Callable[[], List[AccountCtx]]] = None,  # ✅ 추가
        max_workers: int = 6,  # ✅ 병렬 전송 스레드 수
    ):
        self._token_provider = token_provider
        self._base_url = (base_url or "https://api.kiwoom.com").rstrip("/")
        self._api_id_buy = api_id_buy
        self._api_id_sell = api_id_sell
        self._timeout = timeout
        self._account_provider = account_provider
        self._max_workers = max_workers

    def name(self) -> str:
        return "kiwoom"

    def _headers(self, token: str, api_id: str) -> Dict[str, str]:
        """요청 헤더 생성 + 토큰 마스킹 디버그 로그"""
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": api_id,
        }
        tok = (token or "").strip()
        if not tok:
            safe = "(empty)"
        else:
            safe = (tok[:6] + "..." + tok[-4:]) if len(tok) > 12 else "*" * len(tok)
        logger.debug(f"{DEBUG_TAG} HTTP.HEADERS | authorization=Bearer {safe} | api-id={api_id}")
        return headers



    def _resolve_accounts(self) -> List[AccountCtx]:
        """
        1) account_provider()가 있으면 그것을 사용
        2) token_provider()가 리스트를 리턴하면 계좌 리스트로 간주
        3) env(KIWOOM_ACCOUNTS_JSON) 파싱
        4) 최종적으로 단일 토큰이면 단일 계좌 모드
        """
        # 1) 외부 주입
        if callable(self._account_provider):
            try:
                accs = self._account_provider() or []
                if isinstance(accs, list) and any(isinstance(x, dict) for x in accs):
                    return accs
            except Exception:
                pass

        # 2) token_provider가 리스트 제공
        tp = self._token_provider()
        if isinstance(tp, list) and all(isinstance(x, dict) for x in tp):
            return tp

        # 3) 환경변수
        env_accs = _load_accounts_from_env()
        if env_accs:
            return env_accs

        # 4) 단일 토큰만
        if isinstance(tp, str) and tp.strip():
            return [{"token": tp.strip(), "acc_no": None, "enabled": True, "alias": None}]
        # 비정상 상황: 빈 토큰
        return [{"token": "", "acc_no": None, "enabled": True, "alias": None}]

    def _do_place(self, token: str, api_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self._base_url}/api/dostk/ordr"
        logger.debug(f"{DEBUG_TAG} HTTP.REQ | API_ID={api_id} | URL={url}")
        logger.debug(f"{DEBUG_TAG} HTTP.REQ | BODY={body}")
        try:
            r = requests.post(url, headers=self._headers(token, api_id), json=body, timeout=self._timeout)
            header_subset = {k: r.headers.get(k) for k in ["next-key", "cont-yn", "api-id"]}
            try:
                body_js = r.json()
            except Exception:
                body_js = {"raw": r.text}
            logger.debug(f"{DEBUG_TAG} HTTP.RESP | STATUS={r.status_code} | HEADERS={header_subset}")
            logger.debug(f"{DEBUG_TAG} HTTP.RESP | BODY={body_js}")
            return {"status_code": r.status_code, "header": header_subset, "body": body_js}
        except requests.RequestException as e:
            logger.warning(f"{DEBUG_TAG} HTTP.EXC | {type(e).__name__}: {e}")
            return {"status_code": 598, "header": {}, "body": {"error": str(e)}}

    def place_order(self, req: OrderRequest) -> OrderResponse:
        """
        - 싱글/멀티 계좌 자동 판별
        - 토큰 마스킹/모드 식별 로그
        - 빈 토큰 가드
        - 멀티 브로드캐스트 요약 로그
        """
        api_id = self._api_id_buy if req.side.upper() == "BUY" else self._api_id_sell

        # 공통 바디 (acc_no는 계좌 컨텍스트에 있을 때만 주입)
        base_body: Dict[str, Any] = {
            "dmst_stex_tp": req.dmst_stex_tp,
            "stk_cd": req.stk_cd,
            "ord_qty": str(req.ord_qty),
            "ord_uv": "" if req.ord_uv is None else str(req.ord_uv),
            "trde_tp": req.trde_tp,
            "cond_uv": req.cond_uv,
        }

        # 계좌 해석
        accounts = self._resolve_accounts()
        enabled_accounts = [a for a in accounts if a.get("enabled", True)]

        # 계좌/토큰 마스킹 로그
        def _mask(tok: Optional[str]) -> str:
            t = (tok or "").strip()
            if not t:
                return "(empty)"
            return (t[:6] + "..." + t[-4:]) if len(t) > 12 else "*" * len(t)

        safe_accounts = [
            {"alias": a.get("alias"), "acc_no": a.get("acc_no"), "token": _mask(a.get("token"))}
            for a in enabled_accounts
        ]
        logger.info(f"{DEBUG_TAG} RESOLVED_ACCOUNTS count={len(enabled_accounts)} details={safe_accounts}")

        # --- 단일 계좌 모드 ---
        if len(enabled_accounts) <= 1:
            logger.info(f"{DEBUG_TAG} MODE=single-account")
            ctx = enabled_accounts[0] if enabled_accounts else {"token": self._token_provider(), "acc_no": None}
            token = (ctx.get("token") or "").strip()
            if not token:
                # token_provider 최후 보루 시도
                tp = self._token_provider()
                token = (tp or "").strip() if isinstance(tp, str) else token

            if not token:
                raise RuntimeError(f"{DEBUG_TAG} Empty token resolved (single-account). "
                                f"Check token_provider() or KIWOOM_ACCOUNTS_JSON.")

            body = dict(base_body)
            if "acc_no" not in body and ctx.get("acc_no"):
                body["acc_no"] = ctx["acc_no"]

            single = self._do_place(token, api_id, body)
            # 단일 경로는 기존 응답 그대로 전달
            return OrderResponse(
                status_code=single.get("status_code", 500),
                header=single.get("header", {}),
                body=single.get("body", {}),
            )

        # --- 멀티 계좌 모드 ---
        logger.info(f"{DEBUG_TAG} MODE=multi-account | fanout={len(enabled_accounts)}")

        results: List[Dict[str, Any]] = []
        from concurrent.futures import ThreadPoolExecutor, as_completed  # (안전: 상단 import 이미 있음)

        def _submit_one(ctx: Dict[str, Any]):
            token = (ctx.get("token") or "").strip()
            if not token:
                # 빈 토큰은 네트워크 호출 없이 실패로 기록
                return {
                    "account": ctx.get("alias") or ctx.get("acc_no") or "unknown",
                    "acc_no": ctx.get("acc_no"),
                    "status_code": 599,
                    "header": {},
                    "body": {"error": "empty token for this account"},
                }
            body = dict(base_body)
            if "acc_no" not in body and ctx.get("acc_no"):
                body["acc_no"] = ctx["acc_no"]
            try:
                r = self._do_place(token, api_id, body)
                return {
                    "account": ctx.get("alias") or ctx.get("acc_no") or "unknown",
                    "acc_no": ctx.get("acc_no"),
                    "status_code": r.get("status_code"),
                    "header": r.get("header"),
                    "body": r.get("body"),
                }
            except Exception as e:
                return {
                    "account": ctx.get("alias") or ctx.get("acc_no") or "unknown",
                    "acc_no": ctx.get("acc_no"),
                    "status_code": 599,
                    "header": {},
                    "body": {"error": str(e)},
                }

        # 병렬 전송
        with ThreadPoolExecutor(max_workers=self._max_workers) as ex:
            futs = []
            for ctx in enabled_accounts:
                # 빈 토큰은 바로 동기 처리(네트워크 미호출)로 실패 기록
                tok = (ctx.get("token") or "").strip()
                if not tok:
                    results.append(_submit_one(ctx))
                    continue
                futs.append(ex.submit(_submit_one, ctx))

            for fut in as_completed(futs):
                results.append(fut.result())

        # 상태코드 합산/요약
        codes = [int(x.get("status_code") or 0) for x in results]
        all_ok = all(200 <= c < 300 for c in codes if c)
        any_ok = any(200 <= c < 300 for c in codes if c)
        if all_ok:
            overall = 200
        elif any_ok:
            overall = 207  # Multi-Status
        else:
            overall = codes[0] if codes else 500

        success_cnt = sum(1 for c in codes if 200 <= c < 300)
        failed_cnt = sum(1 for c in codes if not (200 <= c < 300))
        logger.info(f"{DEBUG_TAG} MULTI-ORDER SUMMARY | total={len(results)} | success={success_cnt} | failed={failed_cnt}")

        header = {"api-id": api_id, "multi-account": "true", "cont-yn": "", "next-key": ""}

        body = {
            "results": results,
            "summary": {
                "total": len(results),
                "success": success_cnt,
                "failed": failed_cnt,
            }
        }
        return OrderResponse(status_code=overall, header=header, body=body)
