# core/app_wiring.py
from __future__ import annotations

import logging
from .settings_manager import AppSettings

logger = logging.getLogger(__name__)


class AppWiring:
    """
    트레이더/모니터/토큰/브리지 등 객체를 한 곳에서 결선하고,
    AppSettings를 받아 일괄 적용한다.
    """
    def __init__(self, *, trader, monitor):
        self.trader = trader
        self.monitor = monitor

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

        # --- 라더(사다리) 매수 설정 적용 ✅ ---
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

        # --- 시뮬/실거래 분기 ---
        try:
            if hasattr(self.trader, "paper_mode"):
                self.trader.paper_mode = bool(cfg.sim_mode)
            if cfg.api_base_url and hasattr(self.trader, "_base_url"):
                logger.info("[Wiring] api_base_url override requested -> %s (실제 사용은 AutoTrader 구현에 따름)",
                            cfg.api_base_url)
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
