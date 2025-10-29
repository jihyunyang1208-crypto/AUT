# broker/factory.py
from __future__ import annotations
import os
import logging
from typing import Callable, Optional, Any, List, Dict

from .base import Broker
from .simulator import SimulatorBroker
from .kiwoom import KiwoomRestBroker
from .mirae import MiraeAssetBroker
from utils import token_manager

logger = logging.getLogger(__name__)

def _normalize_vendor(v: Optional[str]) -> str:
    if not v:
        return "sim"
    x = str(v).strip().lower().replace("_", "").replace("-", "")
    if x in ("sim", "simulation", "paper"):
        return "sim"
    if x in ("kiwoom", "kium", "kw"):
        return "kiwoom"
    if x in ("mirae", "miraeasset", "miraeassetdaewoo", "miraeassetsec"):
        return "mirae"
    if x in ("kis", "koreainvest", "koreainvestment", "koreainvestmentsec"):
        return "kis"
    return x

def _resolve_base_url(vendor: str, base_url_provider: Optional[Callable[[], str]]) -> Optional[str]:
    # 1) provider
    if callable(base_url_provider):
        try:
            u = (base_url_provider() or "").strip()
            if u:
                return u
        except Exception as e:
            logger.warning("[factory] base_url_provider error: %s", e)

    # 2) vendor별
    env_key_vendor = f"BROKER_BASE_URL_{vendor.upper()}"
    u = (os.getenv(env_key_vendor) or "").strip()
    if u:
        return u

    # 3) 공통
    u = (os.getenv("BROKER_BASE_URL") or "").strip()
    if u:
        return u

    return None

def create_broker(
    *,
    token_provider: Optional[Callable[[], Any]] = None,
    dealer: Optional[str] = None,
    base_url_provider: Optional[Callable[[], str]] = None,
) -> Broker:
    raw = dealer or os.getenv("BROKER_VENDOR") or os.getenv("BROKER_TYPE") or "sim"
    vendor = _normalize_vendor(raw)

    base_url = _resolve_base_url(vendor, base_url_provider)

    # ---------- 분기 ----------
    if vendor == "sim":
        logger.info("[factory] using SimulatorBroker (vendor=%s)", raw)
        return SimulatorBroker()

    if vendor == "kiwoom":
        logger.info("[factory] using KiwoomRestBroker (base_url=%s)", base_url or "-")

        # 기본 토큰 공급자: 메인 프로필 토큰
        tp = token_provider or token_manager.token_provider_for_main

        # account_provider: token_manager에서 활성 계좌를 가져와,
        # 계좌별 토큰이 포함된 AccountCtx 리스트로 변환
        def _account_provider() -> List[Dict[str, Any]]:
            acc_ids = token_manager.active_account_ids()
            ctxs: List[Dict[str, Any]] = []
            if acc_ids:
                for acc in acc_ids:
                    try:
                        tok = token_manager.token_provider_for_account_id(acc)
                    except Exception:
                        tok = ""  # 안전 폴백
                    ctxs.append({
                        "token": tok,
                        "acc_no": acc,
                        "enabled": True,
                        "alias": None,
                    })
                return ctxs

            # 활성 계좌가 없으면 단일(메인) 토큰 모드로 폴백
            try:
                tok = tp()
            except Exception:
                tok = ""
            main_acc = token_manager.main_account_id()
            return [{"token": tok, "acc_no": main_acc, "enabled": True, "alias": None}]

        return KiwoomRestBroker(
            token_provider=tp,
            base_url=(base_url or "https://api.kiwoom.com"),
            account_provider=_account_provider,  # ✅ 시그니처 일치
        )

    if vendor == "mirae":
        logger.info("[factory] using MiraeAssetBroker (base_url=%s)", base_url or "-")
        return MiraeAssetBroker(
            token_provider=token_manager.token_provider_for_main,
            base_url=base_url
        )

    if vendor == "kis":
        logger.warning("[factory] KIS broker not implemented yet → Simulator fallback")
        return SimulatorBroker()

    logger.warning("[factory] Unknown vendor '%s' (raw='%s') → Simulator fallback", vendor, raw)
    return SimulatorBroker()
