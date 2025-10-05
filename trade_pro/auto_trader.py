# trade_pro/auto_trader.py
from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Literal

import requests

# --- Optional import: PositionManager (graceful if missing) ---
try:
    from trade_pro.position_manager import PositionManager  # type: ignore
except Exception:  # pragma: no cover
    PositionManager = None  # type: ignore


# =========================
# Settings / Data Classes
# =========================
@dataclass
class TradeSettings:
    # Match legacy defaults to avoid unintended live orders
    master_enable: bool = False
    auto_buy: bool = True
    auto_sell: bool = False
    order_type: Literal["limit", "market"] = "limit"


@dataclass
class LadderSettings:
    unit_amount: int = 100_000         # per-slice notional (KRW)
    num_slices: int = 10               # number of slices
    start_ticks_below: int = 1         # first step: N ticks below current
    step_ticks: int = 1                # gap in ticks between slices
    trde_tp: str = "0"                 # '0' limit, '3' market (broker-specific)
    min_qty: int = 1                   # minimum shares per order
    interval_sec: float = 0.08         # delay between ladder legs


# =========================
# Logger (CSV + JSONL)
# =========================
class TradeLogger:
    def __init__(
        self,
        log_dir: str = "logs/trades",
        file_prefix: str = "orders",
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        self.log_dir = Path(log_dir)
        self.file_prefix = file_prefix
        self._log = log_fn or (lambda m: None)
        self._lock = threading.Lock()
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _paths(self):
        day = datetime.now().strftime("%Y-%m-%d")
        return (
            self.log_dir / f"{self.file_prefix}_{day}.csv",
            self.log_dir / f"{self.file_prefix}_{day}.jsonl",
        )

    def _ensure_csv_header(self, csv_path: Path):
        if not csv_path.exists() or csv_path.stat().st_size == 0:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    "ts","session_id","uid","strategy","action","stk_cd","dmst_stex_tp",
                    "cur_price","limit_price","qty","trde_tp",
                    "tick_mode","tick_used",
                    "slice_idx","slice_total","unit_amount","notional",
                    "duration_ms","status_code",
                ])

    def write_order_record(self, record: Dict[str, Any]):
        csv_path, jsonl_path = self._paths()
        with self._lock:
            self._ensure_csv_header(csv_path)
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    record.get("ts"),
                    record.get("session_id"),
                    record.get("uid"),
                    record.get("strategy"),
                    record.get("action"),
                    record.get("stk_cd"),
                    record.get("dmst_stex_tp"),
                    record.get("cur_price"),
                    record.get("limit_price"),
                    record.get("qty"),
                    record.get("trde_tp"),
                    record.get("tick_mode"),
                    record.get("tick_used"),
                    record.get("slice_idx"),
                    record.get("slice_total"),
                    record.get("unit_amount"),
                    record.get("notional"),
                    record.get("duration_ms"),
                    record.get("status_code"),
                ])
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # Legacy-compatible alias (minimal): mirrors old .log(record, response, note)
    def log(self, record: Dict[str, Any], response: Optional[Dict[str, Any]] = None, note: str = ""):
        rec = dict(record)
        rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
        if response is not None and isinstance(response, dict):
            rec.setdefault("status_code", response.get("status_code"))
        self.write_order_record(rec)


# =========================
# AutoTrader (drop-in comp.)
# =========================
class AutoTrader:
    def __init__(
        self,
        *,
        settings: Optional[TradeSettings] = None,
        ladder: Optional[LadderSettings] = None,
        token_provider: Optional[Callable[[], str]] = None,
        base_url_provider: Optional[Callable[[], str]] = None,
        endpoint: str = "/api/dostk/ordr",   # keep legacy endpoint
        paper_mode: Optional[bool] = None,
        log: Optional[Callable[[str], None]] = None,
        bridge: Optional[object] = None,
        position_mgr: Optional[PositionManager] = None,
        use_mock: Optional[bool] = None,
    ):
        self.settings = settings or TradeSettings()
        self.ladder = ladder or LadderSettings()
        self._token_provider = token_provider or (lambda: os.getenv("ACCESS_TOKEN", ""))
        self._base_url_provider = base_url_provider or (lambda: os.getenv("HTTP_API_BASE", "https://api.kiwoom.com").rstrip("/"))
        self._endpoint = endpoint

        # --- Mode & env overrides ---
        env_mode = (os.getenv("TRADE_MODE") or "").strip().lower()
        env_pm = _parse_bool(os.getenv("PAPER_MODE"), default=False)
        if paper_mode is not None:
            self.paper_mode = bool(paper_mode)
        elif env_mode in ("paper", "sim", "simulation"):
            self.paper_mode = True
        elif env_mode in ("live", "real", "prod"):
            self.paper_mode = False
        else:
            self.paper_mode = env_pm

        if use_mock is not None:
            self._use_mock = bool(use_mock)
        else:
            self._use_mock = _parse_bool(os.getenv("USE_MOCK_API"), default=False)

        self.session_id = uuid.uuid4().hex[:12]
        self._log = log or (lambda m: print(str(m)))
        self.logger = TradeLogger(log_fn=self._log)
        self.bridge = bridge
        self.position_mgr = position_mgr

        # Paper trading: ensure we have a dummy sim if none provided
        if self.paper_mode and not getattr(self, "sim_engine", None):
            self.sim_engine = _DummySim(self._log)

        # dedupe for websocket fills
        self._seen_exec_keys: set[tuple[str, Optional[str]]] = set()
        self._exec_lock = threading.Lock()

        self._api_id_buy = "kt10000"
        self._api_id_sell = "kt10001"

        self._log(f"[AutoTrader] mode={'PAPER' if self.paper_mode else 'LIVE'} use_mock={self._use_mock}")

    # ---------- Public: legacy-compatible dispatcher ----------
    async def handle_signal(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Legacy entrypoint: routes ladder_buy / single BUY/SELL payloads."""
        if not getattr(self.settings, "master_enable", True):
            self._log("⏹ master_enable=False: 주문 중단")
            return None

        if payload.get("ladder_buy"):
            return await self._handle_ladder_buy(payload)

        signal = str(payload.get("signal") or "").upper()
        data = payload.get("data") or payload
        if signal == "SELL":
            return await self._handle_simple_sell(data)
        elif signal == "BUY":
            # emulate single buy as one-leg ladder near current
            try:
                cur_price = int(float(data.get("ord_uv") or 0))
            except Exception:
                cur_price = 0
            ladder_like = {
                "stk_cd": data.get("stk_cd"),
                "dmst_stex_tp": data.get("dmst_stex_tp") or "KRX",
                "cur_price": cur_price,
                "num_slices": 1,
                "start_ticks_below": 0,
                "step_ticks": 1,
            }
            return await self._handle_ladder_buy(ladder_like)
        else:
            self._emit_order_event({
                "type": "ORDER_EVENT", "action": None, "symbol": data.get("stk_cd"),
                "price": 0, "qty": 0, "status": f"UNHANDLED_SIGNAL:{signal}",
                "ts": datetime.now(timezone.utc).isoformat(), "extra": payload,
            })
            return None

    # ---------- Public: legacy on_signal(sig_obj) compatible ----------
    def make_on_signal_legacy(self, bridge: Optional[object] = None) -> Callable[[Any], None]:
        """Accepts TradeSignal object (has .side/.symbol/.price)."""
        if bridge is not None:
            self.bridge = bridge

        def _handler(sig_obj):
            try:
                side = str(getattr(sig_obj, "side", "")).upper()
                symbol = str(getattr(sig_obj, "symbol", "")).strip()
                price_attr = getattr(sig_obj, "price", 0)
                last_price = int(float(price_attr)) if price_attr is not None else 0
            except Exception:
                return

            if not symbol or last_price <= 0:
                self._log("🚫 on_signal: 유효하지 않은 심볼/가격")
                return

            self.bridge.log.emit(f"📶 on_signal: {side} {symbol} @ {last_price}")
            if side == "BUY":
                payload = {"stk_cd": symbol, "dmst_stex_tp": "KRX", "cur_price": last_price}
                asyncio.create_task(self._handle_ladder_buy(payload))
            elif side == "SELL":
                data = {"dmst_stex_tp": "KRX", "stk_cd": symbol, "ord_qty": "1", "ord_uv": str(last_price), "trde_tp": "0", "cond_uv": ""}
                asyncio.create_task(self._handle_simple_sell(data))
            else:
                self._emit_order_event({
                    "type": "ORDER_EVENT", "action": None, "symbol": symbol,
                    "price": 0, "qty": 0, "status": f"UNHANDLED_SIDE:{side}",
                    "ts": datetime.now(timezone.utc).isoformat(), "extra": {},
                })
        return _handler

    # Keep old name for drop-in replacement
    def make_on_signal(self, bridge: Optional[object] = None) -> Callable[[Any], None]:
        return self.make_on_signal_legacy(bridge)

    # ---------- Market feed (paper sim) ----------
    def feed_market_event(self, event: Dict[str, Any]):
        if self.paper_mode and getattr(self, "sim_engine", None):
            self.sim_engine.on_market_update(event)

    # ---------- Tick utils ----------
    @staticmethod
    def _krx_tick(price: int) -> int:
        if price < 1_000: return 1
        if price < 5_000: return 5
        if price < 10_000: return 10
        if price < 50_000: return 50
        if price < 100_000: return 100
        if price < 500_000: return 500
        return 1_000

    @staticmethod
    def _snap_to_tick(price: int, tick: int) -> int:
        if tick <= 0:
            return int(price)
        return int((price // tick) * tick)

    def _compute_ladder_prices_fixed(
        self, *, cur_price: int, tick: int, count: int, start_ticks_below: int, step_ticks: int
    ) -> List[int]:
        prices: List[int] = []
        ticks = start_ticks_below
        for _ in range(count):
            p = cur_price - (ticks * tick)
            p = self._snap_to_tick(p, tick)
            if p <= 0:
                break
            prices.append(p)
            ticks += step_ticks
        return prices

    def _compute_ladder_prices_fixed_up(
        self, *, cur_price: int, tick: int, count: int, start_ticks_above: int, step_ticks: int
    ) -> List[int]:
        prices: List[int] = []
        ticks = start_ticks_above
        for _ in range(count):
            p = cur_price + (ticks * tick)
            p = self._snap_to_tick(p, tick)
            prices.append(p)
            ticks += step_ticks
        return prices

    def _compute_ladder_prices_dynamic(
        self, *, cur_price: int, count: int, start_ticks_below: int, step_ticks: int, tick_fn: Callable[[int], int]
    ) -> List[int]:
        prices: List[int] = []
        ticks_to_go = start_ticks_below
        base = cur_price
        for _ in range(count):
            t = max(1, tick_fn(base))
            p = base - (ticks_to_go * t)
            p = self._snap_to_tick(p, t)
            if p <= 0:
                break
            prices.append(p)
            base = p
            ticks_to_go += step_ticks
        return prices

    def _resolve_trde_tp(self) -> str:
        if self.settings.order_type == "market":
            return "3"   # 시장가 (broker-specific mapping)
        return "0"       # 지정가

    # =========================
    # Ladder BUY (below current)
    # =========================
    async def _handle_ladder_buy(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.settings.auto_buy:
            self._log("⛔ auto_buy=False: 사다리 매수 차단")
            return None

        stk_cd = str(payload.get("stk_cd") or "").strip()
        cur_price = int(payload.get("cur_price") or 0)
        dmst_stex_tp = (payload.get("dmst_stex_tp") or "KRX").upper()
        if not stk_cd or cur_price <= 0:
            self._log("🚫 (ladder) 종목코드 또는 현재가가 유효하지 않습니다.")
            return None

        # Fixed tick mode (match legacy default behavior)
        if "tick" in payload and int(payload["tick"]) > 0:
            tick = int(payload["tick"]) ; tick_mode = "fixed"
        else:
            tick = self._krx_tick(cur_price) ; tick_mode = "fixed"

        unit_amount = int(payload.get("unit_amount") or self.ladder.unit_amount)
        num_slices = int(payload.get("num_slices") or self.ladder.num_slices)
        start_ticks_below = int(payload.get("start_ticks_below") or self.ladder.start_ticks_below)
        step_ticks = int(payload.get("step_ticks") or self.ladder.step_ticks)
        trde_tp = str(payload.get("trde_tp") or self._resolve_trde_tp())
        min_qty = self.ladder.min_qty

        # optional: cap by target_total_qty using position manager
        target_total_qty = payload.get("target_total_qty")
        remaining_cap = None
        if self.position_mgr and target_total_qty is not None:
            try:
                target = int(target_total_qty)
                cur_qty = int(self.position_mgr.get_qty(stk_cd))
                pend_buy, _ = self.position_mgr.get_pending(stk_cd)
                remaining_cap = max(0, target - (cur_qty + int(pend_buy)))
            except Exception:
                remaining_cap = None

        prices = self._compute_ladder_prices_fixed(
            cur_price=cur_price, tick=tick, count=num_slices,
            start_ticks_below=start_ticks_below, step_ticks=step_ticks
        )
        self.bridge.log.emit(f"🪜 (ladder/{tick_mode}) tick={tick} prices={prices}")

        # Paper mode: simulate only
        if self.paper_mode and getattr(self, "sim_engine", None):
            total = len(prices)
            for i, limit_price in enumerate(prices, start=1):
                qty = max(min_qty, math.floor(unit_amount / limit_price))
                if remaining_cap is not None:
                    if remaining_cap <= 0:
                        self._log("ℹ️ (ladder) target_total_qty 도달 → 남은 주문 스킵")
                        break
                    qty = min(qty, remaining_cap)
                    remaining_cap -= qty
                if qty <= 0:
                    self._log(f"↪️ (paper/ladder) [{i}/{total}] {limit_price}원: 수량=0 → 스킵")
                    self._emit_order_event({
                        "type": "ORDER_SKIP","action": "BUY","symbol": stk_cd,
                        "price": limit_price,"qty": 0,"status": "SKIPPED",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "extra": {"reason": "qty==0", "slice": i, "total": total},
                    })
                    continue
                try:
                    sim_oid = self.sim_engine.submit_limit_buy(
                        stk_cd=stk_cd, limit_price=limit_price, qty=qty,
                        parent_uid=uuid.uuid4().hex, strategy="ladder",
                    )
                    self._log(f"🧪 (paper) [{i}/{total}] NEW {stk_cd} {qty}주 @ {limit_price}원 → sim_oid={sim_oid}")
                    self._emit_order_event({
                        "type": "ORDER_NEW","action": "BUY","symbol": stk_cd,
                        "price": limit_price,"qty": qty,"status": "PAPER_SUBMIT",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "extra": {"slice": i, "total": total, "sim_oid": sim_oid},
                    })
                except Exception as e:
                    self._log(f"❌ (ladder) paper submit 실패 → {e}")
                await asyncio.sleep(self.ladder.interval_sec)
            return {"ladder_submitted": total}

        # Live mode
        try:
            token = self._token_provider()
            if not token:
                raise RuntimeError("액세스 토큰이 비어있습니다.")
        except Exception as e:
            self._log(f"🚫 토큰 조회 실패: {e}")
            return None

        results: List[Dict[str, Any]] = []
        total = len(prices)
        for i, limit_price in enumerate(prices, start=1):
            qty = max(min_qty, math.floor(unit_amount / limit_price))
            if remaining_cap is not None:
                if remaining_cap <= 0:
                    self._log("ℹ️ (ladder) target_total_qty 도달 → 남은 주문 스킵")
                    break
                qty = min(qty, remaining_cap)
                remaining_cap -= qty
            if qty <= 0:
                self._log(f"↪️ (LIVE)(ladder) [{i}/{total}] {limit_price}원: 수량=0 → 스킵")
                self._emit_order_event({
                    "type": "ORDER_SKIP","action": "BUY","symbol": stk_cd,
                    "price": limit_price,"qty": 0,"status": "SKIPPED",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "extra": {"reason": "qty==0", "slice": i, "total": total},
                })
                continue

            data = {
                "dmst_stex_tp": dmst_stex_tp,
                "stk_cd": stk_cd,
                "ord_qty": str(qty),
                "ord_uv": str(limit_price),
                "trde_tp": trde_tp,
                "cond_uv": "",
            }

            uid = uuid.uuid4().hex
            tick_used = tick

            try:
                start = time.perf_counter()
                resp = await asyncio.to_thread(self._fn_kt10000, token=token, data=data, cont_yn="N", next_key="")
                duration_ms = int((time.perf_counter() - start) * 1000)
                code = resp.get("status_code")
                results.append(resp)

                self.bridge.log.emit(f"✅ (LIVE)(ladder) [{i}/{total}] {stk_cd} {qty}주 @ {limit_price}원 → Code={code}")

                record = {
                    "session_id": self.session_id,"uid": uid,"strategy": "ladder","action": "BUY",
                    "stk_cd": stk_cd,"dmst_stex_tp": dmst_stex_tp,"cur_price": cur_price,
                    "limit_price": limit_price,"qty": qty,"trde_tp": trde_tp,
                    "tick_mode": tick_mode,"tick_used": tick_used,
                    "slice_idx": i,"slice_total": total,
                    "unit_amount": unit_amount,"notional": unit_amount,
                    "duration_ms": duration_ms,"status_code": code,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
                self.logger.write_order_record(record)

                self._emit_order_event({
                    "type": "ORDER_NEW","action": "BUY","symbol": stk_cd,
                    "price": limit_price,"qty": qty,"status": f"HTTP_{code}",
                    "ts": record["ts"],"extra": {"slice": i, "total": total, "resp": resp},
                })
            except Exception as e:
                self.bridge.log.emit(f"💥 (LIVE)(ladder) [{i}/{total}] 주문 실패: {e}")
                self._emit_order_event({
                    "type": "ORDER_NEW","action": "BUY","symbol": stk_cd,
                    "price": limit_price,"qty": qty,"status": "ERROR",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "extra": {"slice": i, "total": total, "error": str(e)},
                })

            await asyncio.sleep(self.ladder.interval_sec)

        ok = sum(1 for r in results if (r.get("status_code") or 0) // 100 == 2)
        self._log(f"🧾 (ladder) 완료: 성공 {ok}/{len(results)} (계단수={len(prices)})")
        return {"ladder_results": results}

    # =========================
    # Simple SELL (limit/market)
    # =========================
    async def _handle_simple_sell(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.settings.auto_sell or not self.settings.master_enable:
            self._log("⛔ auto_sell=False 또는 master_enable=False: 매도 차단")
            return None

        stk_cd = str(payload.get("stk_cd") or "").strip()
        dmst_stex_tp = (payload.get("dmst_stex_tp") or "KRX").upper()
        trde_tp = str(payload.get("trde_tp") or "0")  # '0': limit, '3': market (broker-specific)
        qty = int(payload.get("ord_qty") or 0)
        limit_price = int(payload.get("ord_uv") or 0) if trde_tp == "0" else None

        if not stk_cd:
            self._log("🚫 (sell) 종목코드 없음"); return None
        if qty <= 0:
            self._log("🚫 (sell) 수량 0 이하"); return None
        if trde_tp == "0" and (limit_price is None or limit_price <= 0):
            self._log("🚫 (sell) 지정가인데 가격 없음"); return None

        try:
            token = self._token_provider()
            if not token:
                raise RuntimeError("액세스 토큰이 비어있습니다.")
        except Exception as e:
            self._log(f"🚫 토큰 조회 실패: {e}")
            return None

        data = {
            "dmst_stex_tp": dmst_stex_tp,"stk_cd": stk_cd,
            "ord_qty": str(qty),"ord_uv": str(limit_price or 0),
            "trde_tp": trde_tp,"cond_uv": "",
        }

        uid = uuid.uuid4().hex
        try:
            start = time.perf_counter()
            resp = await asyncio.to_thread(self._fn_kt10001, token=token, data=data, cont_yn="N", next_key="")
            duration_ms = int((time.perf_counter() - start) * 1000)
            code = resp.get("status_code")

            record = {
                "session_id": self.session_id,"uid": uid,"strategy": "manual","action": "SELL",
                "stk_cd": stk_cd,"dmst_stex_tp": dmst_stex_tp,"cur_price": None,
                "limit_price": limit_price,"qty": qty,"trde_tp": trde_tp,
                "tick_mode": "n/a","tick_used": "n/a","slice_idx": 1,"slice_total": 1,
                "unit_amount": None,"notional": None,"duration_ms": duration_ms,
                "status_code": code,"ts": datetime.now(timezone.utc).isoformat(),
            }
            self.logger.write_order_record(record)

            self._log(f"✅ (sell) {stk_cd} {qty}주 @{limit_price or 'MKT'} → Code={code}")
            self._emit_order_event({
                "type": "ORDER_NEW","action": "SELL","symbol": stk_cd,
                "price": limit_price or 0,"qty": qty,"status": f"HTTP_{code}",
                "ts": record["ts"],"extra": {"resp": resp, "trde_tp": trde_tp},
            })
            return {"sell_result": resp}

        except Exception as e:
            self._log(f"❌ (sell) {stk_cd} 실패 → {e}")
            self._emit_order_event({
                "type": "ORDER_NEW","action": "SELL","symbol": stk_cd,
                "price": limit_price or 0,"qty": qty,"status": "ERROR",
                "ts": datetime.now(timezone.utc).isoformat(),
                "extra": {"error": str(e), "trde_tp": trde_tp},
            })
            return None

    # =========================
    # Ladder SELL (above current)
    # =========================
    async def _handle_ladder_sell(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.settings.auto_sell or not self.settings.master_enable:
            self._log("⛔ auto_sell=False 또는 master_enable=False: 라더 매도 차단"); return None

        stk_cd = str(payload.get("stk_cd") or "").strip()
        cur_price = int(payload.get("cur_price") or 0)
        dmst_stex_tp = (payload.get("dmst_stex_tp") or "KRX").upper()
        trde_tp = str(payload.get("trde_tp") or "0")
        if not stk_cd or cur_price <= 0:
            self._log("🚫 (ladder-sell) 종목코드 또는 현재가가 유효하지 않습니다."); return None

        if "tick" in payload and int(payload["tick"]) > 0:
            tick = int(payload["tick"]) ; tick_mode = "fixed"
        else:
            tick = self._krx_tick(cur_price) ; tick_mode = "fixed"

        num_slices = int(payload.get("num_slices") or 10)
        start_ticks_above = int(payload.get("start_ticks_above") or 1)
        step_ticks = int(payload.get("step_ticks") or 1)

        slice_qty = payload.get("slice_qty")
        total_qty = payload.get("total_qty")
        qty_plan: List[int] = []

        if slice_qty is None and total_qty is None and self.position_mgr:
            cur_qty = int(self.position_mgr.get_qty(stk_cd))
            _, pend_sell = self.position_mgr.get_pending(stk_cd)
            sellable = max(0, cur_qty - int(pend_sell))
            if sellable <= 0:
                self._log("ℹ️ (ladder-sell) 매도 가능 수량 없음")
                return {"ladder_sell_results": []} if not self.paper_mode else {"ladder_sell_submitted": 0}
            base = sellable // num_slices ; rem = sellable % num_slices
            qty_plan = [(base + 1 if i < rem else base) for i in range(num_slices)]
        else:
            if slice_qty is not None:
                sq = int(slice_qty)
                if sq <= 0: self._log("🚫 (ladder-sell) slice_qty ≤ 0"); return None
                qty_plan = [sq] * num_slices
            else:
                if total_qty is None: self._log("🚫 (ladder-sell) slice_qty 또는 total_qty 필요"); return None
                tq = int(total_qty)
                if tq <= 0: self._log("🚫 (ladder-sell) total_qty ≤ 0"); return None
                base = tq // num_slices ; rem = tq % num_slices
                qty_plan = [(base + 1 if i < rem else base) for i in range(num_slices)]

        prices = self._compute_ladder_prices_fixed_up(
            cur_price=cur_price, tick=tick, count=num_slices,
            start_ticks_above=start_ticks_above, step_ticks=step_ticks
        )
        self._log(f"🪜 (ladder-sell/{tick_mode}) tick={tick} prices={prices} qty_plan={qty_plan}")

        try:
            token = self._token_provider()
            if not token: raise RuntimeError("액세스 토큰이 비어있습니다.")
        except Exception as e:
            self._log(f"🚫 토큰 조회 실패: {e}"); return None

        results: List[Dict[str, Any]] = []
        total = min(len(prices), len(qty_plan))

        # Paper mode
        if self.paper_mode and getattr(self, "sim_engine", None):
            for i in range(total):
                limit_price = prices[i] ; qty = int(qty_plan[i] or 0)
                if qty <= 0:
                    self._emit_order_event({
                        "type": "ORDER_SKIP","action": "SELL","symbol": stk_cd,
                        "price": limit_price,"qty": 0,"status": "SKIPPED",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "extra": {"reason": "qty==0", "slice": i+1, "total": total},
                    }) ; continue
                try:
                    sim_oid = self.sim_engine.submit_limit_sell(
                        stk_cd=stk_cd, limit_price=limit_price, qty=qty,
                        parent_uid=uuid.uuid4().hex, strategy="ladder-sell",
                    )
                    self._log(f"🧪 (paper) [SELL {i+1}/{total}] {stk_cd} {qty}주 @ {limit_price} → sim_oid={sim_oid}")
                    self._emit_order_event({
                        "type": "ORDER_NEW","action": "SELL","symbol": stk_cd,
                        "price": limit_price,"qty": qty,"status": "PAPER_SUBMIT",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "extra": {"slice": i+1, "total": total, "sim_oid": sim_oid},
                    })
                except Exception as e:
                    self._log(f"❌ (ladder-sell) paper submit 실패 → {e}")
                await asyncio.sleep(self.ladder.interval_sec)
            return {"ladder_sell_submitted": total}

        # Live mode
        for i in range(total):
            limit_price = prices[i]
            qty = int(qty_plan[i] or 0)
            if qty <= 0:
                self._emit_order_event({
                    "type": "ORDER_SKIP","action": "SELL","symbol": stk_cd,
                    "price": limit_price,"qty": 0,"status": "SKIPPED",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "extra": {"reason": "qty==0", "slice": i+1, "total": total},
                })
                continue

            data = {
                "dmst_stex_tp": dmst_stex_tp,"stk_cd": stk_cd,
                "ord_qty": str(qty),"ord_uv": str(limit_price),
                "trde_tp": trde_tp,"cond_uv": "",
            }

            uid = uuid.uuid4().hex
            tick_used = tick

            try:
                start = time.perf_counter()
                resp = await asyncio.to_thread(self._fn_kt10001, token=token, data=data, cont_yn="N", next_key="")
                duration_ms = int((time.perf_counter() - start) * 1000)
                code = resp.get("status_code")
                results.append(resp)

                self._log(f"✅ (ladder-sell) [{i+1}/{total}] {stk_cd} {qty}주 @ {limit_price} → Code={code}")

                record = {
                    "session_id": self.session_id,"uid": uid,"strategy": "ladder-sell","action": "SELL",
                    "stk_cd": stk_cd,"dmst_stex_tp": dmst_stex_tp,"cur_price": cur_price,
                    "limit_price": limit_price,"qty": qty,"trde_tp": trde_tp,
                    "tick_mode": tick_mode,"tick_used": tick_used,
                    "slice_idx": i+1,"slice_total": total,
                    "unit_amount": None,"notional": None,
                    "duration_ms": duration_ms,"status_code": code,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
                self.logger.write_order_record(record)

                self._emit_order_event({
                    "type": "ORDER_NEW","action": "SELL","symbol": stk_cd,
                    "price": limit_price,"qty": qty,"status": f"HTTP_{code}",
                    "ts": record["ts"],"extra": {"slice": i+1, "total": total, "resp": resp},
                })
            except Exception as e:
                self._log(f"❌ (ladder-sell) [{i+1}/{total}] {stk_cd} 실패 → {e}")
                self._emit_order_event({
                    "type": "ORDER_NEW","action": "SELL","symbol": stk_cd,
                    "price": limit_price,"qty": qty,"status": "ERROR",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "extra": {"slice": i+1, "total": total, "error": str(e)},
                })

        return {"ladder_sell_results": results}


    # =========================
    # Public: buy immediately on condition detection
    # =========================
    async def buy_immediate_on_detection(
        self,
        *,
        stk_cd: str,
        last_price: int | float | str,
        dmst_stex_tp: str = "KRX",
        unit_amount: int | None = None,
        order_type: Literal["limit", "market"] | None = None,
    ) -> Optional[Dict[str, Any]]:
        """
        조건검색식 '편입(I)' 즉시 한 번만 매수하는 간편 API.
        - 기본은 지정가 한 번(현재가에 틱 스냅)으로 보냄
        - order_type="market"이면 시장가 1회
        - unit_amount 입력 없으면 self.ladder.unit_amount 사용
        """
        if not self.settings.master_enable or not self.settings.auto_buy:
            self._log("⛔ [immediate] master_enable/auto_buy 비활성 → 매수 차단")
            return None

        try:
            p = int(float(last_price))
        except Exception:
            self._log("🚫 [immediate] last_price 변환 실패")
            return None
        if not stk_cd or p <= 0:
            self._log("🚫 [immediate] 종목코드/가격 유효하지 않음")
            return None

        # 일회성 사다리로 위임 (1틱 아래 대신 현재가 그대로)
        payload = {
            "stk_cd": stk_cd,
            "dmst_stex_tp": dmst_stex_tp,
            "cur_price": p,
            "num_slices": 1,
            "start_ticks_below": 0,
            "step_ticks": 1,
            "unit_amount": int(unit_amount) if unit_amount else self.ladder.unit_amount,
        }

        # 주문 타입 강제 지정(옵션)
        if order_type in ("limit", "market"):
            old = self.settings.order_type
            try:
                self.settings.order_type = order_type  # 일시 오버라이드
                return await self._handle_ladder_buy(payload)
            finally:
                self.settings.order_type = old
        else:
            return await self._handle_ladder_buy(payload)

    # =========================
    # WebSocket events mapping
    # =========================
    def on_ws_message(self, raw: Dict[str, Any]) -> None:
        try:
            msg_type = str(raw.get("type") or raw.get("event") or "").upper()
        except Exception:
            return

        if msg_type in ("FILL", "PARTIAL_FILL", "EXECUTION_REPORT", "TRADE"):
            info = self._map_fill(raw)
            if not info:
                return
            if not self._dedupe_fill(info):
                return
            self._apply_fill(info)
            self._emit_order_event({
                "type": "ORDER_FILL","action": info["side"],"symbol": info["symbol"],
                "price": info["fill_price"],"qty": info["fill_qty"],
                "status": "FILLED","ts": info.get("ts"),
                "extra": {"exec_id": info.get("exec_id"),"part_seq": info.get("part_seq"),"order_id": info.get("order_id")},
            })
        elif msg_type in ("CANCEL", "CANCELED", "ORDER_CANCELED"):
            info = self._map_cancel(raw)
            if not info: return
            self._emit_order_event({
                "type": "ORDER_CANCEL","action": None,"symbol": info.get("symbol"),
                "price": 0,"qty": info.get("qty", 0),"status": "ORDER_CANCEL",
                "ts": info.get("ts"),"extra": info,
            })
        elif msg_type in ("REJECT", "REJECTED", "ORDER_REJECTED"):
            info = self._map_reject(raw)
            if not info: return
            self._emit_order_event({
                "type": "ORDER_REJECT","action": None,"symbol": info.get("symbol"),
                "price": 0,"qty": info.get("qty", 0),"status": "ORDER_REJECT",
                "ts": info.get("ts"),"extra": info,
            })
        else:
            self._emit_order_event({
                "type": "ORDER_EVENT","action": None,
                "symbol": raw.get("symbol") or raw.get("stk_cd"),
                "price": 0,"qty": 0,"status": "ORDER_EVENT",
                "ts": raw.get("ts") or raw.get("time"),"extra": raw,
            })

    # ---- broker → internal schema mappers ----
    def _map_fill(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            side = str(raw["side"]).upper()
            sym = str(raw.get("symbol") or raw.get("stk_cd"))
            qty = int(raw.get("filled_qty") or raw.get("fill_qty") or raw.get("qty"))
            price = float(raw.get("fill_price") or raw.get("price"))
            exec_id = str(raw.get("exec_id") or raw.get("trade_id") or raw.get("last_exec_id"))
            part_seq = raw.get("part_seq")
            ts = raw.get("ts") or raw.get("time") or ""
        except Exception:
            return None
        return {
            "side": side, "symbol": sym, "fill_qty": qty, "fill_price": price,
            "exec_id": exec_id, "part_seq": None if part_seq in (None, "") else str(part_seq),
            "order_id": raw.get("order_id"), "ts": ts,
        }

    def _map_cancel(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            sym = str(raw.get("symbol") or raw.get("stk_cd"))
            qty = int(raw.get("canceled_qty") or raw.get("qty") or 0)
            order_id = str(raw.get("order_id") or "")
            ts = raw.get("ts") or raw.get("time") or ""
        except Exception:
            return None
        return {"symbol": sym, "qty": qty, "order_id": order_id, "ts": ts, "raw": raw}

    def _map_reject(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            sym = str(raw.get("symbol") or raw.get("stk_cd"))
            qty = int(raw.get("qty") or 0)
            reason = raw.get("reason") or raw.get("err_msg") or ""
            ts = raw.get("ts") or raw.get("time") or ""
        except Exception:
            return None
        return {"symbol": sym, "qty": qty, "reason": reason, "ts": ts, "raw": raw}

    # ---- dedupe fills ----
    def _dedupe_fill(self, info: Dict[str, Any]) -> bool:
        key = (info["exec_id"], info.get("part_seq"))
        with self._exec_lock:
            if key in self._seen_exec_keys:
                return False
            self._seen_exec_keys.add(key)
            return True

    # ---- apply fills to position manager ----
    def _apply_fill(self, info: Dict[str, Any]) -> None:
        if not self.position_mgr:
            return
        side = info["side"] ; sym = info["symbol"]
        qty = int(info["fill_qty"]) ; price = float(info["fill_price"])
        if side == "BUY":
            try: self.position_mgr.apply_fill_buy(sym, qty, price)
            except Exception: pass
        elif side == "SELL":
            try: self.position_mgr.apply_fill_sell(sym, qty, price)
            except Exception: pass

    # =========================
    # HTTP helpers
    # =========================
    def _base_url(self) -> str:
        if self._use_mock:
            return "https://mockapi.kiwoom.com"
        return self._base_url_provider()

    def _headers(self, access_token: str, cont_yn: str, next_key: str, api_id: str) -> Dict[str, str]:
        h: Dict[str, str] = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {access_token}",
            "api-id": api_id,
        }
        if cont_yn:
            h["cont-yn"] = cont_yn
        if next_key:
            h["next-key"] = next_key
        return h

    def _payload_to_kt10000_data(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "dmst_stex_tp": str(payload.get("dmst_stex_tp") or "KRX").upper(),
            "stk_cd": str(payload.get("stk_cd") or "").strip(),
            "ord_qty": str(payload.get("ord_qty") or "0"),
            "ord_uv": str(payload.get("ord_uv") or "0"),
            "trde_tp": str(payload.get("trde_tp") or self._resolve_trde_tp()),
            "cond_uv": str(payload.get("cond_uv") or ""),
        }

    def _fn_kt10000(self, token: str, data: Dict[str, Any], cont_yn: str = "N", next_key: str = "") -> Dict[str, Any]:
        host = self._base_url()
        url = host + self._endpoint
        headers = self._headers(token, cont_yn, next_key, api_id=self._api_id_buy)
        # request
        response = requests.post(url, headers=headers, json=data, timeout=10)
        header_subset = {k: response.headers.get(k) for k in ["next-key", "cont-yn", "api-id"]}
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}
        return {"status_code": response.status_code, "header": header_subset, "body": body}

    def _fn_kt10001(self, token: str, data: Dict[str, Any], cont_yn: str = "N", next_key: str = "") -> Dict[str, Any]:
        host = self._base_url()
        url = host + self._endpoint  # same endpoint, different api-id
        headers = self._headers(token, cont_yn, next_key, api_id=self._api_id_sell)
        response = requests.post(url, headers=headers, json=data, timeout=10)
        header_subset = {k: response.headers.get(k) for k in ["next-key", "cont-yn", "api-id"]}
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}
        return {"status_code": response.status_code, "header": header_subset, "body": body}

    # ---------- UI event emit ----------
    def _emit_order_event(self, evt: Dict[str, Any]) -> None:
        try:
            if self.bridge and hasattr(self.bridge, "order_event"):
                self.bridge.order_event.emit(evt)
        except Exception:
            pass


# =========================
# Utilities
# =========================

def _parse_bool(v: Optional[str], default: bool = False) -> bool:
    if v is None:
        return default
    t = str(v).strip().lower()
    return t in ("1", "true", "t", "yes", "y", "on")


class _DummySim:
    def __init__(self, log_fn: Callable[[str], None]):
        self._log = log_fn
        self._orders: Dict[str, dict] = {}

    def submit_limit_buy(self, *, stk_cd: str, limit_price: int, qty: int, parent_uid: str, strategy: str) -> str:
        oid = uuid.uuid4().hex[:10]
        self._orders[oid] = {"side": "BUY", "stk_cd": stk_cd, "limit": limit_price, "qty": qty,
                              "pid": parent_uid, "strategy": strategy, "ts": time.time()}
        self._log(f"[sim] limit BUY {stk_cd} x{qty} @ {limit_price} (oid={oid})")
        return oid

    def submit_limit_sell(self, *, stk_cd: str, limit_price: Optional[int], qty: int, parent_uid: str, strategy: str) -> str:
        oid = uuid.uuid4().hex[:10]
        self._orders[oid] = {"side": "SELL", "stk_cd": stk_cd, "limit": limit_price or 0, "qty": qty,
                              "pid": parent_uid, "strategy": strategy, "ts": time.time()}
        self._log(f"[sim] limit SELL {stk_cd} x{qty} @ {limit_price if limit_price is not None else 'MKT'} (oid={oid})")
        return oid

    def on_market_update(self, event: Dict[str, Any]):
        last = int(event.get("last") or 0)
        done: List[str] = []
        for oid, od in self._orders.items():
            if od["side"] == "BUY" and last and last <= int(od["limit"]):
                self._log(f"[sim] filled BUY {od['stk_cd']} x{od['qty']} @ {od['limit']} (oid={oid})")
                done.append(oid)
            if od["side"] == "SELL" and last and last >= int(od["limit"]):
                self._log(f"[sim] filled SELL {od['stk_cd']} x{od['qty']} @ {od['limit']} (oid={oid})")
                done.append(oid)
        for oid in done:
            self._orders.pop(oid, None)
