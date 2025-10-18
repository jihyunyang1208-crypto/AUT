# broker/factory.py  (DROP-IN REPLACEMENT)

from __future__ import annotations
import os
import logging
from typing import Callable, Optional

from .base import Broker
from .simulator import SimulatorBroker
from .kiwoom import KiwoomRestBroker
from .mirae import MiraeAssetBroker  # ✅ 올바른 클래스명 사용

logger = logging.getLogger(__name__)

def _normalize_vendor(v: Optional[str]) -> str:
    """dealer / env 값을 일관된 벤더 키로 정규화."""
    if not v:
        return "sim"
    x = str(v).strip().lower().replace("_", "").replace("-", "")
    # 별칭 매핑
    if x in ("sim", "simulation", "paper"):
        return "sim"
    if x in ("kiwoom", "kium", "kw"):
        return "kiwoom"
    if x in ("mirae", "miraeasset", "miraeassetdaewoo", "miraeassetsec"):
        return "mirae"
    if x in ("kis", "koreainvest", "koreainvestment", "koreainvestmentsec"):
        return "kis"
    return x

def create_broker(
    *,
    token_provider: Optional[Callable[[], str]] = None,
    dealer: Optional[str] = None,
    base_url_provider: Optional[Callable[[], str]] = None,
) -> Broker:
    """
    dealer 우선순위:
    1) 인자로 받은 dealer
    2) 환경변수 BROKER_VENDOR / BROKER_TYPE
    3) 'sim'
    """
    raw = dealer or os.getenv("BROKER_VENDOR") or os.getenv("BROKER_TYPE") or "sim"
    vendor = _normalize_vendor(raw)

    # Base URL (옵션)
    base_url = None
    if callable(base_url_provider):
        try:
            base_url = (base_url_provider() or "").strip() or None
        except Exception as e:
            logger.warning("[factory] base_url_provider error: %s", e)
            base_url = None

    # ---------- 분기 ----------
    if vendor == "sim":
        logger.info("[factory] using SimulatorBroker (vendor=%s)", raw)
        return SimulatorBroker()

    if vendor == "kiwoom":
        logger.info("[factory] using KiwoomRestBroker (base_url=%s)", base_url or "-")
        return KiwoomRestBroker(token_provider=token_provider, base_url=base_url)

    if vendor == "mirae":
        logger.info("[factory] using MiraeAssetBroker (base_url=%s)", base_url or "-")
        return MiraeAssetBroker(token_provider=token_provider, base_url=base_url)  # ✅ 이름 일치

    if vendor == "kis":
        # 아직 미구현: 안전하게 시뮬레이터로 폴백
        logger.warning("[factory] KIS broker not implemented yet → Simulator fallback")
        return SimulatorBroker()

    # 알 수 없는 벤더 → 시뮬 폴백
    logger.warning("[factory] Unknown vendor '%s' (raw='%s') → Simulator fallback", vendor, raw)
    return SimulatorBroker()
