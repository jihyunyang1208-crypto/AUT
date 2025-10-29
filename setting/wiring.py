# setting/wiring.py 
from __future__ import annotations

import logging
import os
from typing import Callable
from .settings_manager import AppSettings

# 브로커 생성 및 기타 의존성
try:
    from broker.factory import create_broker 
except ImportError:
    logging.getLogger(__name__).critical("Failed to import broker.factory. Hot-swap disabled.")
    def create_broker(*args, **kwargs): return None # 안전한 폴백

logger = logging.getLogger(__name__)


class AppWiring:
    """
    트레이더/모니터/토큰/브리지 등 객체를 한 곳에서 결선하고,
    AppSettings를 받아 일괄 적용한다.
    """
    def __init__(self, *, trader, monitor):
        self.trader = trader
        self.monitor = monitor

    @staticmethod
    def _broker_identity(b):
        """브로커 객체의 식별자(vendor/name)를 반환합니다.""" 
        # 1) callable name()
        nm = getattr(b, "name", None)
        if callable(nm):
            try:
                v = nm()
                if v: return str(v).strip().lower()
            except Exception:
                pass
        # 2) name 속성
        v = getattr(b, "name", None)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
        # 3) vendor 속성
        v = getattr(b, "vendor", None)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
        # 4) 클래스명 fallback
        return b.__class__.__name__.lower()


    def apply_settings(self, cfg: AppSettings):
        # --- 공통 스위치 ---
        if hasattr(self.trader, "settings"):
            self.trader.settings.master_enable = cfg.master_enable
            self.trader.settings.auto_buy = cfg.auto_buy
            self.trader.settings.auto_sell = cfg.auto_sell

        if hasattr(self.monitor, "settings"):
            self.monitor.settings.master_enable = cfg.master_enable
            self.monitor.settings.auto_buy = cfg.auto_buy
            self.monitor.settings.auto_sell = cfg.auto_sell

        # --- MACD 30m 필터/윈도우/폴링 ---
        if hasattr(self.monitor, "use_macd30_filter"):
            self.monitor.use_macd30_filter = bool(cfg.use_macd30_filter)
        if hasattr(self.monitor, "macd30_timeframe"):
            self.monitor.macd30_timeframe = cfg.macd30_timeframe or "30m"
        if hasattr(self.monitor, "macd30_max_age_sec"):
            self.monitor.macd30_max_age_sec = int(cfg.macd30_max_age_sec)

        if hasattr(self.monitor, "poll_interval_sec"):
            self.monitor.poll_interval_sec = int(cfg.poll_interval_sec)
        if hasattr(self.monitor, "_win_start"):
            self.monitor._win_start = int(cfg.bar_close_window_start_sec)
        if hasattr(self.monitor, "_win_end"):
            self.monitor._win_end = int(cfg.bar_close_window_end_sec)
        if hasattr(self.monitor, "tz"):
            self.monitor.tz = cfg.timezone or "Asia/Seoul"

        # --- 라더(사다리) 매수 설정 적용 ---
        try:
            if hasattr(self.trader, "ladder") and self.trader.ladder is not None:
                # 안전 가드
                ua = max(10_000, int(cfg.ladder_unit_amount))
                ns = max(1, int(cfg.ladder_num_slices))
                self.trader.ladder.unit_amount = ua
                self.trader.ladder.num_slices = ns
                logger.info("[Wiring] Ladder config applied: unit_amount=%s, num_slices=%s", ua, ns)
        except Exception as e:
            logger.warning("[Wiring] apply ladder settings failed: %s", e)

        # --- 매수/매도 브로커(증권사/시뮬) 설정 적용 (HOT-SWAP) ---
        # a) 단일 브로커 핫스왑(레거시 유지)
        if hasattr(self.trader, "broker") and not (cfg.accounts and len(cfg.accounts) > 0):

            try:
                # 1. 설정값에서 벤더 추출
                vendor = None
                for key in ("broker_vendor", "broker", "dealer"):
                    if hasattr(cfg, key):
                        v = getattr(cfg, key)
                        if isinstance(v, str) and v.strip():
                            vendor = v.strip()
                            break

                if vendor:
                    # A. 현재 브로커 이름 확인
                    cur_name = None
                    try:
                        if self.trader.broker and hasattr(self.trader.broker, "name") and callable(self.trader.broker.name):
                            cur_name = self.trader.broker.name()
                    except Exception:
                        pass # cur_name remains None

                    # B. Provider 준비
                    # AutoTrader에서 사용하는 _token_provider를 가져옴
                    token_provider = getattr(self.trader, "_token_provider", None) 
                    base_url_provider: Callable[[], str] = lambda: getattr(cfg, "api_base_url", "") or os.getenv("HTTP_API_BASE", "")

                    # C. 새 브로커 생성
                    new_broker = create_broker(
                        token_provider=token_provider,
                        dealer=vendor,
                        base_url_provider=base_url_provider
                    )
                    
                    if new_broker is None:
                         raise RuntimeError("create_broker returned None.")

                    new_name = getattr(new_broker, "name", None)
                    if callable(new_name):
                        new_name = new_broker.name()

                    # D. 브로커 교체 (이름이 다를 경우에만)
                    cur_id = self._broker_identity(self.trader.broker) 
                    new_id = self._broker_identity(new_broker)

                    if cur_id != new_id:
                        self.trader.broker = new_broker
                        logger.info("[Wiring] broker set to '%s' (was: %s)", new_id, cur_id or "None")
                    else:
                        logger.info("[Wiring] broker unchanged: '%s'", cur_id)
                else:
                    logger.info("[Wiring] broker vendor not specified; keeping current")
            except Exception as e:
                # 이 에러는 핫스왑이 실패했음을 의미하며, 기존 브로커가 유지됩니다.
                logger.critical("[Wiring] BROKER HOT-SWAP FAILED. Using existing broker. Error: %s", e)

            # b) 멀티 계정 지원
            try:
                if cfg.accounts and hasattr(self.trader, "set_accounts") and callable(self.trader.set_accounts):
                    # 활성/권한체크는 트레이더에서 한 번 더 수행
                    self.trader.set_accounts(cfg.accounts)
                    logger.info("[Wiring] multi-accounts applied (%d)", len(cfg.accounts))
            except Exception as e:
                logger.warning("[Wiring] set_accounts failed: %s", e)


        # --- 시뮬/실거래 분기 ---
        try:
            sim_mode = getattr(cfg, "sim_mode", None)
            if sim_mode is None:
                sim_mode = (getattr(cfg, "mode", "").upper() == "SIMULATION")
            sim_mode = bool(sim_mode)

            # ✅ 올바른 토글: 내부 상태+엔진 일괄 세팅
            if hasattr(self.trader, "set_simulation_mode"):
                self.trader.set_simulation_mode(sim_mode)
            else:
                # 최후수단: 직접 필드 세팅 + 엔진 보장 (권장 X, 임시)
                self.trader.simulation = sim_mode
                if sim_mode and getattr(self.trader, "sim_engine", None) is None and hasattr(self.trader, "set_simulation_mode"):
                    self.trader.set_simulation_mode(sim_mode)

            # (paper_mode는 optional, 필요 시 유지)
            if hasattr(self.trader, "paper_mode"):
                self.trader.paper_mode = sim_mode

            logger.info("[Wiring] simulation=%s", sim_mode)
        except Exception as e:
            logger.warning("[Wiring] sim/apply failed: %s", e)


        # --- 로그 ---
        logger.info(
            "[Wiring] applied: master=%s buy=%s sell=%s sim=%s "
            "ladder(unit=%s, slices=%s) macd30=%s tf=%s age=%s "
            "poll=%ss close=[%s..%s] tz=%s",
            cfg.master_enable, cfg.auto_buy, cfg.auto_sell, cfg.sim_mode,
            cfg.ladder_unit_amount, cfg.ladder_num_slices,
            cfg.use_macd30_filter, cfg.macd30_timeframe, cfg.macd30_max_age_sec,
            cfg.poll_interval_sec, cfg.bar_close_window_start_sec, cfg.bar_close_window_end_sec,
            cfg.timezone
        )