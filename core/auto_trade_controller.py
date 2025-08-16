# core/auto_trade_controller.py
import time, asyncio
from typing import Dict, Optional
from core.ports import TradeAPIPort, NotifierPort, OrderResult

class AutoTradeSettings:
    master_enable: bool = True
    auto_buy: bool = True
    auto_sell: bool = False
    default_qty: int = 1
    cooldown_sec: int = 60
    dry_run: bool = True
    order_type: str = "market"  # "market" or "limit"
    slippage_ticks: int = 1

class AutoTradeController:
    def __init__(self, trade_api: TradeAPIPort, notifier: NotifierPort, settings: Optional[AutoTradeSettings] = None):
        self.trade_api = trade_api
        self.notifier = notifier
        self.settings = settings or AutoTradeSettings()
        self._cooldown_buy: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock(self, code: str) -> asyncio.Lock:
        self._locks.setdefault(code, asyncio.Lock())
        return self._locks[code]

    def _in_cooldown(self, code: str) -> bool:
        ts = self._cooldown_buy.get(code, 0.0)
        return (time.time() - ts) < self.settings.cooldown_sec

    async def handle_signal(self, payload: dict) -> None:
        """신규 종목 상세 콜백에서 호출"""
        if not self.settings.master_enable or not self.settings.auto_buy:
            return
        code = str(payload.get("stock_code") or payload.get("code") or "")
        if not code or self._in_cooldown(code):
            return

        async with self._lock(code):
            if self._in_cooldown(code):
                return

            # 아주 단순한 판정: 현재가 존재하면 1주 매수 (추후 룰 확장)
            price = float(payload.get("price") or payload.get("curprc") or payload.get("prc") or 0)
            qty = max(1, int(self.settings.default_qty))

            msg = f"🛒 AUTO BUY try: {code} x{qty} @{self.settings.order_type}"
            self.notifier.info(msg)

            if self.settings.dry_run:
                self._cooldown_buy[code] = time.time()
                self.notifier.info(f"DRY-RUN OK: {code} x{qty}")
                return

            res: OrderResult = await self.trade_api.place_order(
                side="BUY", code=code, qty=qty, order_type=self.settings.order_type,
                limit_price=None, tag="AUTO"
            )
            if res.accepted:
                self._cooldown_buy[code] = time.time()
                self.notifier.info(f"✅ BUY OK: {code} id={res.order_id}")
            else:
                self.notifier.warn(f"❌ BUY FAIL: {code} {res.message}")
