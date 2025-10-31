# broker/factory.py
from __future__ import annotations
import os
import logging
from typing import Callable, Optional, Any

from .base import Broker
from .simulator import SimulatorBroker
from .kiwoom import KiwoomRestBroker
from .mirae import MiraeAssetBroker
from utils.token_manager import get_main_token  # ✅ 메인 토큰 공급자

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
    # 1) 외부 provider 우선
    if callable(base_url_provider):
        try:
            u = (base_url_provider() or "").strip()
            if u:
                return u
        except Exception as e:
            logger.warning("[factory] base_url_provider error: %s", e)

    # 2) 벤더별 환경변수
    env_key_vendor = f"BROKER_BASE_URL_{vendor.upper()}"
    u = (os.getenv(env_key_vendor) or "").strip()
    if u:
        return u

    # 3) 공통 환경변수
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

    if vendor == "sim":
        logger.info("[factory] using SimulatorBroker (vendor=%s)", raw)
        return SimulatorBroker()

    if vendor == "kiwoom":
        logger.info("[factory] using KiwoomRestBroker (base_url=%s)", base_url or "-")
        # ✅ 멀티계좌는 브로커가 ENV(KIWOOM_ACCOUNTS_JSON)만 신뢰해 팬아웃
        tp = token_provider or get_main_token  # 하위호환: 인자 우선, 없으면 메인 토큰
        return KiwoomRestBroker(
            token_provider=tp,                    # 시그니처 유지(내부에서 사용 안 함)
            base_url=(base_url or "https://api.kiwoom.com"),
            account_provider=None,                # ✅ 사용 안 함(ENV만 신뢰)
        )

    if vendor == "mirae":
        logger.info("[factory] using MiraeAssetBroker (base_url=%s)", base_url or "-")
        return MiraeAssetBroker(
            token_provider=(token_provider or get_main_token),
            base_url=base_url,
        )

    if vendor == "kis":
        logger.warning("[factory] KIS broker not implemented yet → Simulator fallback")
        return SimulatorBroker()

    logger.warning("[factory] Unknown vendor '%s' (raw='%s') → Simulator fallback", vendor, raw)
    return SimulatorBroker()
