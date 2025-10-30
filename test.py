# tools/run_kiwoom_broadcast_smoketest.py
from __future__ import annotations
import os
import json
from typing import Dict, Any, Optional, List

# 프로젝트 루트에서: PYTHONPATH=. python tools/run_kiwoom_broadcast_smoketest.py
from broker.kiwoom import KiwoomRestBroker
try:
    # 네 코드베이스에 있는 실제 OrderRequest 사용
    from broker.base import OrderRequest
except Exception:
    # 안전망: 동일 속성만 가진 경량 클래스
    class OrderRequest:  # type: ignore
        def __init__(self, side: str, dmst_stex_tp="P", stk_cd="000000", ord_qty=0, ord_uv=None, trde_tp="01", cond_uv="00"):
            self.side = side
            self.dmst_stex_tp = dmst_stex_tp
            self.stk_cd = stk_cd
            self.ord_qty = ord_qty
            self.ord_uv = ord_uv
            self.trde_tp = trde_tp
            self.cond_uv = cond_uv

def _print_banner(title: str):
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)

def _preview_env_accounts() -> List[Dict[str, Any]]:
    raw = os.getenv("KIWOOM_ACCOUNTS_JSON", "") or ""
    accs: List[Dict[str, Any]] = []
    try:
        data = json.loads(raw) if raw.strip() else []
        if isinstance(data, list):
            for x in data:
                if not isinstance(x, dict):
                    continue
                accs.append({
                    "alias": x.get("alias"),
                    "acc_no": x.get("acc_no"),
                    "enabled": bool(x.get("enabled", True)),
                    "token_tail": (x.get("token") or "")[-6:],  # 토큰 전체는 로그에 찍지 않음
                })
    except Exception as e:
        print(f"[WARN] Failed to parse KIWOOM_ACCOUNTS_JSON: {e}")
    return accs

def main():
    _print_banner("KIWOOM Multi-Account Broadcast Smoke Test (NON-TRADING)")
    accs = _preview_env_accounts()
    if not accs:
        print("⚠️  KIWOOM_ACCOUNTS_JSON 이 비어 있습니다. (env에 계정/토큰이 준비돼 있어야 브로드캐스트 확인 가능)")
    else:
        print(f"ENV accounts (enabled only will be used): total={len(accs)}")
        for a in accs:
            print(f" - alias={a['alias']!r} acc_no={a['acc_no']!r} enabled={a['enabled']} token_tail=...{a['token_tail']}")

    # ── 브로커 생성
    base_url = (os.getenv("HTTP_API_BASE") or "https://api.kiwoom.com").rstrip("/")
    order_path = os.getenv("KIWOOM_ORDER_PATH") or "/api/dostk/ordr"

    # token_provider는 싱글토큰 대비 안전망. 멀티는 ENV에서 읽힐 것.
    broker = KiwoomRestBroker(
        token_provider=lambda: os.getenv("ACCESS_TOKEN", ""),
        base_url=base_url,
        api_id_buy=os.getenv("KIWOOM_API_ID_BUY", "kt10000"),
        api_id_sell=os.getenv("KIWOOM_API_ID_SELL", "kt10001"),
        timeout=int(os.getenv("KIWOOM_HTTP_TIMEOUT", "8")),
        account_provider=None,   # 있으면 여기 넣으면 ENV보다 우선 사용됨
        max_workers=6,
        order_path=order_path,
    )

    # ── 주문 본문 (체결되지 않도록 설계: 수량 0 + 무효 종목 코드)
    #     ※ 서버는 보통 400/4xx를 주지만, 네트워크 요청은 계좌 수 만큼 실제 발송됨
    req = OrderRequest(
        side="BUY",          # BUY 또는 SELL
        dmst_stex_tp="P",    # 그대로 전달
        stk_cd="000000",     # 무효 코드 (거절 유도)
        ord_qty=0,           # 수량 0 (거절 유도)
        ord_uv=None,         # 지정가 비움
        trde_tp="01",        # "01": 지정가
        cond_uv="00",
    )

    print(f"\n[INFO] Base URL: {base_url}{order_path}")
    print("      This will send NON-executable requests (qty=0 / invalid stk_cd).")
    print("      목적: 계좌별 브로드캐스트 ‘시도’ 여부와 응답 코드 확인")

    # ── 실행
    resp = broker.place_order(req)

    # ── 결과 요약
    print("\n--- RESULT ---------------------------------------------")
    print(f"overall status_code: {resp.status_code}  (200=all ok, 207=부분성공, 그외=실패)")
    try:
        body = resp.body or {}
        if isinstance(body, dict) and "results" in body:
            summary = body.get("summary", {})
            print(f"summary: {summary}")
            print("per-account results:")
            for r in body.get("results", []):
                alias = r.get("account")
                acc_no = r.get("acc_no")
                sc = r.get("status_code")
                err = r.get("body", {}).get("error")
                print(f" - alias={alias!r} acc_no={acc_no!r} status={sc} error={err}")
        else:
            print("single-account response body:", body)
    except Exception as e:
        print(f"[WARN] failed to pretty print result: {e}")

    print("--------------------------------------------------------\n")
    print("✅ 확인 포인트: enabled=True 계정 수만큼 ‘status’ 라인이 찍히면 브로드캐스트 OK")

if __name__ == "__main__":
    main()
