#trade_pro/auto_trader.py
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
from typing import Any, Callable, Dict, List, Optional, Literal, Tuple
import logging

logger = logging.getLogger(__name__)  # 모듈별 로거


# --- Optional import: PositionManager (graceful if missing) ---
try:
    from trade_pro.position_manager import PositionManager  # type: ignore
except Exception:  # pragma: no cover
    PositionManager = None  # type: ignore


from broker.base import Broker, OrderRequest, OrderResponse
from broker.factory import create_broker

# =========================
# Settings / Data Classes
# =========================
@dataclass
class TradeSettings:
    master_enable: bool = False
    auto_buy: bool = True
    auto_sell: bool = False
    order_type: Literal["limit", "market"] = "limit"
    simulation_mode: Optional[bool] = None  # None이면 env/인자 기반으로 결정
    ladder_sell_enable: bool = False        # 라더 매도 전체 스위치
    on_signal_use_ladder: bool = True       # 신호 수신 시 기본 라더 분기 사용


@dataclass
class LadderSettings:
    unit_amount: int = 100_000         # per-slice notional (KRW)
    num_slices: int = 10               # number of slices
    start_ticks_below: int = 1         # first step: N ticks below current
    step_ticks: int = 1                # gap in ticks between slices
    trde_tp: str = "0"                 # '0' limit, '3' market (broker-specific)
    min_qty: int = 1                   # minimum shares per order
    interval_sec: float = 0.08         # delay between ladder legs
    start_ticks_above: int = 1         # SELL 라더 시작 틱(현재가 위)


# =========================
# Logger (CSV + JSONL)
# =========================
class TradeLogger:
    def __init__(self, log_dir: str = "logs/trades", file_prefix: str = "orders",
                 slim: bool = False):
        self.log_dir = Path(log_dir)
        self.file_prefix = file_prefix
        self._lock = threading.Lock()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._slim = bool(slim)

    def _paths(self) -> Tuple[Path, Path]:
        day = datetime.now().strftime("%Y-%m-%d")
        return (
            self.log_dir / f"{self.file_prefix}_{day}.csv",
            self.log_dir / f"{self.file_prefix}_{day}.jsonl",
        )

    @staticmethod
    def _flatten_response(resp: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(resp, dict):
            return {
                "resp_status_code": None, "resp_api_id": "", "resp_cont_yn": "",
                "resp_next_key": "", "resp_return_code": None, "resp_return_msg": "",
            }
        header = resp.get("header") or {}
        body = resp.get("body") or {}
        return {
            "resp_status_code": resp.get("status_code"),
            "resp_api_id": header.get("api-id", ""),
            "resp_cont_yn": header.get("cont-yn", ""),
            "resp_next_key": header.get("next-key", ""),
            "resp_return_code": body.get("return_code"),
            "resp_return_msg": body.get("return_msg", ""),
        }

    def _ensure_csv_header(self, csv_path: Path):
        if not csv_path.exists() or csv_path.stat().st_size == 0:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if self._slim:
                    w.writerow([
                        "ts", "strategy", "action", "stk_cd", "order_type", "price", "qty",
                        "status", "resp_code", "resp_msg"
                    ])
                else:
                    w.writerow([
                        "ts", "session_id", "uid", "strategy", "action", "stk_cd", "dmst_stex_tp",
                        "cur_price", "limit_price", "qty", "trde_tp",
                        "tick_mode", "tick_used",
                        "slice_idx", "slice_total", "unit_amount", "notional",
                        "duration_ms", "status_code",
                        "status_label", "success", "order_id", "order_id_hint", "error_msg", "note",
                        "resp_status_code", "resp_api_id", "resp_cont_yn", "resp_next_key",
                        "resp_return_code", "resp_return_msg",
                    ])

    def write_order_record(self, record: Dict[str, Any]):
        if not self._slim:
            logger.info("Warning: Full log mode is not fully supported in this version.")
            return

        csv_path, jsonl_path = self._paths()
        with self._lock:
            self._ensure_csv_header(csv_path)

            ts = record.get("ts") or datetime.now(timezone.utc).isoformat()
            status = record.get("status") or record.get("status_label", "UNKNOWN")

            # 1) order_type
            order_type = record.get("order_type")
            if not order_type:
                trde_tp = str(record.get("trde_tp", ""))
                if trde_tp == "3":
                    order_type = "market"
                elif trde_tp in ("0", "00"):
                    order_type = "limit"
                else:
                    order_type = trde_tp

            # 2) price
            price = record.get("price")
            if price is None:
                price = record.get("limit_price") if order_type == "limit" else ""

            # 3) resp
            resp_code = record.get("resp_code")
            resp_msg = record.get("resp_msg")
            if status == "SIM_SUBMIT":
                resp_code = 0 if resp_code is None else resp_code
                resp_msg = "SIM" if resp_msg is None else resp_msg
            else:
                resp_body = (record.get("response") or {}).get("body") or {}
                if resp_code is None:
                    resp_code = resp_body.get("return_code")
                if resp_msg is None:
                    resp_msg = resp_body.get("return_msg", "")

            log_entry = {
                "ts": ts,
                "strategy": record.get("strategy", ""),
                "action": record.get("action", ""),
                "stk_cd": record.get("stk_cd", ""),
                "order_type": order_type,
                "price": price,
                "qty": record.get("qty", 0),
                "status": status,
                "resp_code": resp_code,
                "resp_msg": resp_msg,
            }

            try:
                logger.debug("[AT] log.write.start slim=True")
                logger.debug(f"[AT] log.write.entry {log_entry}")
                with open(csv_path, "a", newline="", encoding="utf-8") as f_csv:
                    writer = csv.DictWriter(f_csv, fieldnames=log_entry.keys())
                    writer.writerow(log_entry)
                with open(jsonl_path, "a", encoding="utf-8") as f_jsonl:
                    f_jsonl.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
            except IOError as e:
                logger.info(f"Error writing to log file: {e}")

    # Legacy-compatible alias
    def log(self, record: Dict[str, Any], response: Optional[Dict[str, Any]] = None, note: str = ""):
        rec = dict(record)
        rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
        if response is not None and isinstance(response, dict):
            rec.setdefault("response", response)
            rec.setdefault("status_code", response.get("status_code"))
        rec.setdefault("note", rec.get("note") or note or "")
        self.write_order_record(rec)


# =========================
# AutoTrader
# =========================
DEBUG_TAG = "[AT]"

class AutoTrader:
    # ---------- Debug helpers ----------
    def _dbg(self, msg: str, **kw) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            if kw:
                kv = " ".join([f"{k}={v!r}" for k, v in kw.items()])
                logger.debug(f"{DEBUG_TAG} {msg} | {kv}")
            else:
                logger.debug(f"{DEBUG_TAG} {msg}")

    def _dbg_ret(self, label: str, ret: Any = None, **kw) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            base = f"{DEBUG_TAG} return:{label}"
            if kw:
                base += " | " + " ".join([f"{k}={v!r}" for k, v in kw.items()])
            if ret is not None:
                base += f" | ret={ret!r}"
            logger.debug(base)

    def __init__(
        self,
        *,
        settings: Optional[TradeSettings] = None,
        ladder: Optional[LadderSettings] = None,
        token_provider: Optional[Callable[[], str]] = None,
        base_url_provider: Optional[Callable[[], str]] = None,
        paper_mode: Optional[bool] = None,
        log: Optional[Callable[[str], None]] = None,
        bridge: Optional[object] = None,
        position_mgr: Optional[PositionManager] = None,
        use_mock: Optional[bool] = None,
    ):
        self.settings = settings or TradeSettings()
        self.ladder = ladder or LadderSettings()
        self._token_provider = token_provider or (lambda: os.getenv("ACCESS_TOKEN", ""))
        self._base_url_provider = base_url_provider

        env_mode = (os.getenv("TRADE_MODE") or "").strip().lower()
        env_pm = _parse_bool(os.getenv("PAPER_MODE"), default=False)
        env_sim = _parse_bool(os.getenv("SIMULATION_MODE"), default=False)

        if self.settings.simulation_mode is not None:
            self.simulation = bool(self.settings.simulation_mode)
        elif paper_mode is not None:
            self.simulation = bool(paper_mode)
        elif env_mode in ("paper", "sim", "simulation"):
            self.simulation = True
        else:
            self.simulation = bool(env_pm or env_sim)

        self.paper_mode = self.simulation

        if use_mock is not None:
            self._use_mock = bool(use_mock)
        else:
            self._use_mock = _parse_bool(os.getenv("USE_MOCK_API"), default=False)

        self.session_id = uuid.uuid4().hex[:12]
        self.trade_logger = TradeLogger(slim=True)
        self.bridge = bridge
        self.position_mgr = position_mgr

        self.broker: Broker = create_broker(
            token_provider=self._token_provider,
            base_url_provider=(self._base_url_provider or (lambda: os.getenv("HTTP_API_BASE", ""))),
        )


        self._seen_exec_keys: set[tuple[str, Optional[str]]] = set()
        self._exec_lock = threading.Lock()

        logger.info(f"[AutoTrader] mode={'SIMULATION' if self.simulation else 'LIVE'} use_mock={self._use_mock} broker={self.broker.name()}")
        self._dbg("__init__",
                  simulation=self.simulation, use_mock=self._use_mock,
                  order_type=self.settings.order_type,
                  auto_buy=self.settings.auto_buy, auto_sell=self.settings.auto_sell,
                  ladder_sell_enable=self.settings.ladder_sell_enable,
                  on_signal_use_ladder=self.settings.on_signal_use_ladder)

    # ---------- 런타임 토글 ----------
    def set_simulation_mode(self, on: bool) -> None:
        # AutoTrader 내부 시뮬 토글은 유지하되, 실제 체결은 브로커가 담당
        self.simulation = bool(on)
        self.paper_mode = self.simulation
        logger.info(f"[AutoTrader] simulation_mode flag set to {self.simulation}")
        self._dbg("set_simulation_mode", simulation=self.simulation)


    # ---------- Public utils ----------
    @staticmethod
    def _to_int(x, default=0):
        try:
            return int(float(x))
        except Exception:
            return int(default)

    def submit_buy_order(self, code: str, qty: int, price: float, **kwargs) -> None:
        if self.position_mgr:
            self.position_mgr.reserve_buy(code, qty)
        response = self._submit_order(code=code, side="BUY", qty=qty, price=price, **kwargs)

        if self.position_mgr and not getattr(response, "ok", True):
            self.position_mgr.release_buy(code, qty)

    def submit_sell_order(self, code: str, qty: int, price: float, **kwargs) -> None:
        if self.position_mgr:
            self.position_mgr.reserve_sell(code, qty)
        response = self._submit_order(code=code, side="SELL", qty=qty, price=price, **kwargs)
        if self.position_mgr and not getattr(response, "ok", True):
            self.position_mgr.release_sell(code, qty)

    def on_order_fill(self, code: str, side: str, qty: int, price: float) -> None:
        if not self.position_mgr:
            return
        if side.upper() == "BUY":
            self.position_mgr.apply_fill_buy(code, qty, price)
        elif side.upper() == "SELL":
            self.position_mgr.apply_fill_sell(code, qty, price)

    async def handle_signal(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        data   = payload.get("data") or payload
        signal = str(payload.get("signal") or data.get("signal") or "").upper()
        mode   = str(payload.get("mode")   or data.get("mode")   or "").lower()

        dmst_stex_tp = str(data.get("dmst_stex_tp") or "KRX").upper()
        stk_cd       = str(data.get("stk_cd") or "").strip()
        if not stk_cd:
            logger.info("🚫 handle_signal: stk_cd 누락")
            self._dbg_ret("handle_signal.block", reason="stk_cd_missing")
            return None

        self._dbg("handle_signal.enter",
                  raw_signal=payload.get("signal") or (payload.get("data") or {}).get("signal"),
                  raw_mode=payload.get("mode") or (payload.get("data") or {}).get("mode"),
                  stk_cd=stk_cd)

        # 자동 라우팅(모드 보강)
        if not mode and getattr(self.settings, "on_signal_use_ladder", True):
            try:
                last_price = int(float(data.get("cur_price") or data.get("ord_uv") or 0))
            except Exception:
                last_price = 0
            trde_tp = "3" if self.settings.order_type == "market" else "0"
            tick = self._krx_tick(last_price) if last_price > 0 else 0
            self._dbg("handle_signal.autofill.enabled", last_price=last_price, trde_tp=trde_tp, tick=tick)

            if signal == "BUY":
                mode = "ladder_buy"
                data.setdefault("cur_price", last_price)
                data.setdefault("num_slices", int(self.ladder.num_slices))
                data.setdefault("start_ticks_below", int(self.ladder.start_ticks_below))
                data.setdefault("step_ticks", int(self.ladder.step_ticks))
                data.setdefault("unit_amount", int(self.ladder.unit_amount))
                data.setdefault("trde_tp", trde_tp)
                if tick: data.setdefault("tick", int(tick))
                self._dbg("handle_signal.autofill.BUY",
                          num_slices=data.get("num_slices"),
                          start_ticks_below=data.get("start_ticks_below"),
                          step_ticks=data.get("step_ticks"),
                          unit_amount=data.get("unit_amount"))

            elif signal == "SELL":
                mode = "ladder_sell"
                qty = 1
                if self.position_mgr:
                    try:
                        qty = max(1, int(self.position_mgr.get_qty(stk_cd)))
                    except Exception:
                        qty = 1
                data.setdefault("cur_price", last_price)
                data.setdefault("num_slices", int(self.ladder.num_slices))
                data.setdefault("start_ticks_above", int(self.ladder.start_ticks_above))
                data.setdefault("step_ticks", int(self.ladder.step_ticks))
                data.setdefault("total_qty", int(qty))
                data.setdefault("trde_tp", trde_tp)
                if tick: data.setdefault("tick", int(tick))
                self._dbg("handle_signal.autofill.SELL",
                          total_qty=data.get("total_qty"),
                          num_slices=data.get("num_slices"),
                          start_ticks_above=data.get("start_ticks_above"),
                          step_ticks=data.get("step_ticks"))

                # 🔽 옵션: 보유 0이면 ladder_sell 대신 simple_sell로 강등
                if self.position_mgr:
                    try:
                        cur_qty = int(self.position_mgr.get_qty(stk_cd))
                    except Exception:
                        cur_qty = 0
                    if cur_qty <= 0:
                        mode = "simple_sell"


        if payload.get("ladder_buy") or data.get("ladder_buy"):
            mode = "ladder_buy"
            signal = "BUY"

        # Ladder BUY
        if mode == "ladder_buy" and signal == "BUY":
            self._dbg("handle_signal.branch", branch="ladder_buy")
            lb = {
                "stk_cd": stk_cd,
                "dmst_stex_tp": dmst_stex_tp,
                "cur_price": self._to_int(data.get("cur_price") or data.get("ord_uv") or 0),
                "num_slices": int(data.get("num_slices") or self.ladder.num_slices),
                "start_ticks_below": int(data.get("start_ticks_below") or self.ladder.start_ticks_below),
                "step_ticks": int(data.get("step_ticks") or self.ladder.step_ticks),
                "unit_amount": int(data.get("unit_amount") or self.ladder.unit_amount),
                "trde_tp": str(data.get("trde_tp") or self._resolve_trde_tp()),
                "tick": int(data.get("tick") or 0),
                "target_total_qty": data.get("target_total_qty"),
            }
            ret = await self._handle_ladder_buy(lb)
            self._dbg_ret("handle_signal.ladder_buy", ret=ret)
            return ret

        # Ladder SELL
        if mode == "ladder_sell" and signal == "SELL":
            self._dbg("handle_signal.branch", branch="ladder_sell")
            ls = {
                "stk_cd": stk_cd,
                "dmst_stex_tp": dmst_stex_tp,
                "cur_price": self._to_int(data.get("cur_price") or data.get("ord_uv") or 0),
                "num_slices": int(data.get("num_slices") or 10),
                "start_ticks_above": int(data.get("start_ticks_above") or 1),
                "step_ticks": int(data.get("step_ticks") or 1),
                "total_qty": data.get("total_qty"),
                "slice_qty": data.get("slice_qty"),
                "trde_tp": str(data.get("trde_tp") or "0"),
                "tick": int(data.get("tick") or 0),
            }
            ret = await self._handle_ladder_sell(ls)
            self._dbg_ret("handle_signal.ladder_sell", ret=ret)
            return ret

        # Simple SELL
        if mode == "simple_sell" and signal == "SELL":
            self._dbg("handle_signal.branch", branch="simple_sell")
            ss = {
                "dmst_stex_tp": dmst_stex_tp,
                "stk_cd": stk_cd,
                "ord_qty": str(data.get("ord_qty") or "0"),
                "ord_uv": str(data.get("ord_uv") or "0"),
                "trde_tp": str(data.get("trde_tp") or "0"),
                "cond_uv": str(data.get("cond_uv") or ""),
            }
            ret = await self._handle_simple_sell(ss)
            self._dbg_ret("handle_signal.simple_sell", ret=ret)
            return ret

        # Fallbacks
        if signal == "BUY" and not mode:
            self._dbg("handle_signal.fallback", to="ladder_buy_oneshot")
            lb = {
                "stk_cd": stk_cd,
                "dmst_stex_tp": dmst_stex_tp,
                "cur_price": self._to_int(data.get("cur_price") or data.get("ord_uv") or 0),
                "num_slices": 1,
                "start_ticks_below": 0,
                "step_ticks": 1,
                "unit_amount": int(data.get("unit_amount") or self.ladder.unit_amount),
                "trde_tp": str(data.get("trde_tp") or self._resolve_trde_tp()),
            }
            ret = await self._handle_ladder_buy(lb)
            self._dbg_ret("handle_signal.fallback_ladder_buy", ret=ret)
            return ret

        if signal == "SELL" and not mode:
            self._dbg("handle_signal.fallback", to="simple_sell")
            ss = {
                "dmst_stex_tp": dmst_stex_tp,
                "stk_cd": stk_cd,
                "ord_qty": str(data.get("ord_qty") or "1"),
                "ord_uv": str(data.get("ord_uv") or "0"),
                "trde_tp": str(data.get("trde_tp") or "0"),
                "cond_uv": str(data.get("cond_uv") or ""),
            }
            ret = await self._handle_simple_sell(ss)
            self._dbg_ret("handle_signal.fallback_simple_sell", ret=ret)
            return ret

        self._emit_order_event({
            "type": "ORDER_EVENT",
            "action": signal or "UNKNOWN",
            "symbol": stk_cd,
            "price": 0,
            "qty": 0,
            "status": f"UNHANDLED_MODE:{mode or 'NONE'}",
            "ts": datetime.now(timezone.utc).isoformat(),
            "extra": payload,
        })
        self._dbg_ret("UNHANDLED_MODE", mode=mode, signal=signal, stk_cd=stk_cd)
        return None

    def make_on_signal(self, bridge: Optional[object] = None) -> Callable[[object], None]:
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

            self._dbg("on_signal.recv", side=side, symbol=symbol, last_price=last_price)

            if not symbol or last_price <= 0:
                logger.info("🚫 on_signal: 유효하지 않은 심볼/가격")
                return

            try:
                if self.bridge and hasattr(self.bridge, "log"):
                    self.bridge.log.emit(f"📶 on_signal: {side} {symbol} @ {last_price}")
            except Exception:
                pass

            payload = {
                "signal": side,
                "data": {
                    "stk_cd": symbol,
                    "dmst_stex_tp": "KRX",
                    "ord_uv": str(last_price),
                },
            }
            self._dbg("on_signal.dispatch", payload_minimal=True, signal=side, stk_cd=symbol, ord_uv=last_price)
            try:
                asyncio.create_task(self.handle_signal(payload))
            except RuntimeError:
                threading.Thread(
                    target=lambda: asyncio.run(self.handle_signal(payload)),
                    daemon=True
                ).start()

        return _handler


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
            return "3"
        return "0"

    def _ticks_above_from_target(self, cur_price: int, target_price: int) -> int:
        if cur_price <= 0 or target_price <= 0:
            return 1
        tick = self._krx_tick(cur_price)
        snapped = self._snap_to_tick(int(target_price), tick)
        diff = max(1, math.ceil((snapped - cur_price) / tick))
        return diff

    # =========================
    # Ladder BUY
    # =========================
    async def _handle_ladder_buy(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        self._dbg("_handle_ladder_buy.enter", auto_buy=self.settings.auto_buy,
                  stk_cd=str(payload.get("stk_cd")), cur_price=payload.get("cur_price"),
                  trde_tp=str(payload.get("trde_tp")))

        if not self.settings.auto_buy:
            logger.info("⛔ auto_buy=False: 사다리 매수 차단")
            self._dbg_ret("ladder_buy.block", reason="auto_buy=False")
            return None

        stk_cd = str(payload.get("stk_cd") or "").strip()
        cur_price = int(payload.get("cur_price") or 0)
        dmst_stex_tp = (payload.get("dmst_stex_tp") or "KRX").upper()
        if not stk_cd or cur_price <= 0:
            logger.info("🚫 (ladder) 종목코드 또는 현재가가 유효하지 않습니다.")
            self._dbg_ret("ladder_buy.block", reason="invalid_symbol_or_price", stk_cd=stk_cd, cur_price=cur_price)
            return None

        if "tick" in payload and int(payload["tick"]) > 0:
            tick = int(payload["tick"]); tick_mode = "fixed"
        else:
            tick = self._krx_tick(cur_price); tick_mode = "fixed"

        unit_amount = int(payload.get("unit_amount") or self.ladder.unit_amount)
        num_slices = int(payload.get("num_slices") or self.ladder.num_slices)
        start_ticks_below = int(payload.get("start_ticks_below") or self.ladder.start_ticks_below)
        step_ticks = int(payload.get("step_ticks") or self.ladder.step_ticks)
        trde_tp = str(payload.get("trde_tp") or self._resolve_trde_tp())
        min_qty = self.ladder.min_qty

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

        self._dbg("ladder_buy.params",
                  tick=tick, tick_mode=tick_mode, unit_amount=unit_amount,
                  num_slices=num_slices, start_ticks_below=start_ticks_below,
                  step_ticks=step_ticks, target_total_qty=target_total_qty,
                  remaining_cap=remaining_cap)

        prices = self._compute_ladder_prices_fixed(
            cur_price=cur_price, tick=tick, count=num_slices,
            start_ticks_below=start_ticks_below, step_ticks=step_ticks
        )
        self._dbg("ladder_buy.prices", count=len(prices), first=prices[:3], last=prices[-3:])
        mode_label = "(SIM)" if self.simulation else "(LIVE)"


        results: List[Dict[str, Any]] = []
        total = len(prices)
        for i, limit_price in enumerate(prices, start=1):
            qty = max(min_qty, math.floor(unit_amount / limit_price))
            if remaining_cap is not None:
                if remaining_cap <= 0:
                    logger.info("ℹ️ (ladder) target_total_qty 도달 → 남은 주문 스킵")
                    self._dbg("ladder_buy.slice.skip", reason="remaining_cap<=0", i=i, total=total)
                    break
                qty = min(qty, remaining_cap)
                remaining_cap -= qty
            if qty <= 0:
                self._emit_order_event({
                    "type": "ORDER_SKIP","action": "BUY","symbol": stk_cd,
                    "price": limit_price,"qty": 0,"status": "SKIPPED",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "extra": {"reason": "qty==0", "slice": i, "total": total},
                })
                self._dbg("ladder_buy.slice.skip", reason="qty==0", i=i, total=total)
                continue

            req = OrderRequest(
                dmst_stex_tp=dmst_stex_tp,
                stk_cd=stk_cd,
                ord_qty=int(qty),
                ord_uv=None if trde_tp == "3" else int(limit_price),
                trde_tp=trde_tp,
                side="BUY",
                cond_uv=""
            )

            uid = uuid.uuid4().hex
            tick_used = tick
            try:
                self._dbg("ladder_buy.http.submit", i=i, limit_price=limit_price, qty=qty)
                start = time.perf_counter()
                resp_obj: OrderResponse = await asyncio.to_thread(self.broker.place_order, req)
                resp = {"status_code": resp_obj.status_code, "header": resp_obj.header, "body": resp_obj.body}
                duration_ms = int((time.perf_counter() - start) * 1000)
                code = resp.get("status_code")
                results.append(resp)
                
                if self.bridge and hasattr(self.bridge, "log"):
                    self.bridge.log.emit(f"✅ {mode_label}(ladder) [{i}/{total}] {stk_cd} {qty}주 @ {limit_price} → Code={code}") 

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
                self.trade_logger.write_order_record(record)

                self._dbg("ladder_buy.http.resp", i=i, status_code=code, duration_ms=duration_ms)

                self._emit_order_event({
                    "type": "ORDER_NEW","action": "BUY","symbol": stk_cd,
                    "price": limit_price,"qty": qty,"status": f"HTTP_{code}",
                    "ts": record["ts"],"extra": {"slice": i, "total": total, "resp": resp},
                })
            except Exception as e:
                if self.bridge and hasattr(self.bridge, "log"):
                    self.bridge.log.emit(f"💥 {mode_label}(ladder) [{i}/{total}] 주문 실패: {e}")
                self._dbg("ladder_buy.http.error", i=i, err=str(e))
                self._emit_order_event({
                    "type": "ORDER_NEW","action": "BUY","symbol": stk_cd,
                    "price": limit_price,"qty": qty,"status": "ERROR",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "extra": {"slice": i, "total": total, "error": str(e)},
                })


            await asyncio.sleep(self.ladder.interval_sec)

        ok = sum(1 for r in results if (r.get("status_code") or 0) // 100 == 2)
        logger.info(f"🧾 (ladder) 완료: 성공 {ok}/{len(results)} (계단수={len(prices)})")
        self._dbg_ret("ladder_buy", ladder_count=len(prices))
        return {"ladder_results": results}

    # =========================
    # Simple SELL
    # =========================
    async def _handle_simple_sell(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        stk_cd = str(payload.get("stk_cd") or "").strip()
        dmst_stex_tp = (payload.get("dmst_stex_tp") or "KRX").upper()
        trde_tp = str(payload.get("trde_tp") or "0")  # '0': limit, '3': market
        qty = int(payload.get("ord_qty") or 0)
        limit_price = int(payload.get("ord_uv") or 0) if trde_tp == "0" else None

        self._dbg("_handle_simple_sell.enter", auto_sell=self.settings.auto_sell,
                  stk_cd=stk_cd, trde_tp=trde_tp, qty=qty, limit_price=limit_price)

        if not self.settings.auto_sell:
            logger.info("⛔ auto_sell=False: 매도 차단")
            self._dbg_ret("simple_sell.block", reason="auto_sell=False")
            return None
        if not stk_cd:
            logger.info("🚫 (sell) 종목코드 없음")
            self._dbg_ret("simple_sell.block", reason="no_symbol")
            return None
        if qty <= 0:
            logger.info("🚫 (sell) 수량 0 이하")
            self._dbg_ret("simple_sell.block", reason="qty<=0")
            return None
        if trde_tp == "0" and (limit_price is None or limit_price <= 0):
            logger.info("🚫 (sell) 지정가인데 가격 없음")
            self._dbg_ret("simple_sell.block", reason="limit_without_price")
            return None


        # (시뮬 제거) 항상 브로커 호출
        req = OrderRequest(
            dmst_stex_tp=dmst_stex_tp,
            stk_cd=stk_cd,
            ord_qty=int(qty),
            ord_uv=None if trde_tp == "3" else int(limit_price or 0),
            trde_tp=trde_tp,
            side="SELL",
            cond_uv=""
        )

        uid = uuid.uuid4().hex
        try:
            self._dbg("simple_sell.http.submit", trde_tp=trde_tp, qty=qty, limit_price=limit_price)
            start = time.perf_counter()
            resp_obj: OrderResponse = await asyncio.to_thread(self.broker.place_order, req)
            resp = {"status_code": resp_obj.status_code, "header": resp_obj.header, "body": resp_obj.body}
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
            self.trade_logger.write_order_record(record)

            logger.info(f"✅ (sell) {stk_cd} {qty}주 @{limit_price or 'MKT'} → Code={code}")
            self._dbg("simple_sell.http.resp", status_code=code, duration_ms=duration_ms)

            self._emit_order_event({
                "type": "ORDER_NEW","action": "SELL","symbol": stk_cd,
                "price": limit_price or 0,"qty": qty,"status": f"HTTP_{code}",
                "ts": record["ts"],"extra": {"resp": resp, "trde_tp": trde_tp},
            })
            self._dbg_ret("simple_sell.http", status_code=code)
            return {"sell_result": resp}
        except Exception as e:
            logger.info(f"❌ (sell) {stk_cd} 실패 → {e}")
            self._dbg("simple_sell.http.error", err=str(e))
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
        stk_cd = str(payload.get("stk_cd") or "").strip()
        cur_price = int(payload.get("cur_price") or 0)
        dmst_stex_tp = (payload.get("dmst_stex_tp") or "KRX").upper()
        trde_tp = str(payload.get("trde_tp") or "0")

        self._dbg("_handle_ladder_sell.enter", auto_sell=self.settings.auto_sell,
                  stk_cd=stk_cd, cur_price=cur_price, trde_tp=trde_tp)

        if not self.settings.auto_sell:
            logger.info("⛔ auto_sell=False: 라더 매도 차단")
            self._dbg_ret("ladder_sell.block", reason="auto_sell=False")
            return None
        if not stk_cd or cur_price <= 0:
            logger.info("🚫 (ladder-sell) 종목코드 또는 현재가가 유효하지 않습니다.")
            self._dbg_ret("ladder_sell.block", reason="invalid_symbol_or_price", stk_cd=stk_cd, cur_price=cur_price)
            return None

        if "tick" in payload and int(payload["tick"]) > 0:
            tick = int(payload["tick"]); tick_mode = "fixed"
        else:
            tick = self._krx_tick(cur_price); tick_mode = "fixed"

        num_slices = int(payload.get("num_slices") or self.ladder.num_slices)
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
                logger.info("ℹ️ (ladder-sell) 매도 가능 수량 없음")
                self._dbg_ret("ladder_sell.block", reason="no_sellable_qty", cur_qty=cur_qty, pend_sell=pend_sell)
                return {"ladder_sell_results": []} if not self.simulation else {"ladder_sell_submitted": 0}
            base = sellable // num_slices ; rem = sellable % num_slices
            qty_plan = [(base + 1 if i < rem else base) for i in range(num_slices)]
        else:
            if slice_qty is not None:
                sq = int(slice_qty)
                if sq <= 0:
                    logger.info("🚫 (ladder-sell) slice_qty ≤ 0")
                    self._dbg_ret("ladder_sell.block", reason="slice_qty<=0")
                    return None
                qty_plan = [sq] * num_slices
            else:
                if total_qty is None:
                    logger.info("🚫 (ladder-sell) slice_qty 또는 total_qty 필요")
                    self._dbg_ret("ladder_sell.block", reason="qty_param_missing")
                    return None
                tq = int(total_qty)
                if tq <= 0:
                    logger.info("🚫 (ladder-sell) total_qty ≤ 0")
                    self._dbg_ret("ladder_sell.block", reason="total_qty<=0")
                    return None
                base = tq // num_slices ; rem = tq % num_slices
                qty_plan = [(base + 1 if i < rem else base) for i in range(num_slices)]

        self._dbg("ladder_sell.params", tick=tick, num_slices=num_slices,
                  start_ticks_above=start_ticks_above, step_ticks=step_ticks,
                  slice_qty=slice_qty, total_qty=total_qty)

        self._dbg("ladder_sell.qty_plan", total=sum(qty_plan),
                  plan=qty_plan[:10] + (["..."] if len(qty_plan) > 10 else []))

        prices = self._compute_ladder_prices_fixed_up(
            cur_price=cur_price, tick=tick, count=num_slices,
            start_ticks_above=start_ticks_above, step_ticks=step_ticks
        )
        self._dbg("ladder_sell.prices", count=len(prices), first=prices[:3], last=prices[-3:])


        results: List[Dict[str, Any]] = []
        total = min(len(prices), len(qty_plan))
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
                self._dbg("ladder_sell.slice.skip", reason="qty==0", i=i+1)
                continue

            req = OrderRequest(
                dmst_stex_tp=dmst_stex_tp,
                stk_cd=stk_cd,
                ord_qty=int(qty),
                ord_uv=None if trde_tp == "3" else int(limit_price),
                trde_tp=trde_tp,
                side="SELL",
                cond_uv=""
            )

            uid = uuid.uuid4().hex
            tick_used = tick
            try:
                self._dbg("ladder_sell.http.submit", i=i+1, limit_price=limit_price, qty=qty)
                start = time.perf_counter()
                resp_obj: OrderResponse = await asyncio.to_thread(self.broker.place_order, req)
                resp = {"status_code": resp_obj.status_code, "header": resp_obj.header, "body": resp_obj.body}
                duration_ms = int((time.perf_counter() - start) * 1000)
                code = resp.get("status_code")
                results.append(resp)

                logger.info(f"✅ (ladder-sell) [{i+1}/{total}] {stk_cd} {qty}주 @ {limit_price} → Code={code}")

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
                self.trade_logger.write_order_record(record)

                self._dbg("ladder_sell.http.resp", i=i+1, status_code=code, duration_ms=duration_ms)

                self._emit_order_event({
                    "type": "ORDER_NEW","action": "SELL","symbol": stk_cd,
                    "price": limit_price,"qty": qty,"status": f"HTTP_{code}",
                    "ts": record["ts"],"extra": {"slice": i+1, "total": total, "resp": resp},
                })
            except Exception as e:
                logger.info(f"❌ (ladder-sell) [{i+1}/{total}] {stk_cd} 실패 → {e}")
                self._dbg("ladder_sell.http.error", i=i+1, err=str(e))
                self._emit_order_event({
                    "type": "ORDER_NEW","action": "SELL","symbol": stk_cd,
                    "price": limit_price,"qty": qty,"status": "ERROR",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "extra": {"slice": i+1, "total": total, "error": str(e)},
                })

        self._dbg_ret("ladder_sell", slices=len(prices))
        return {"ladder_sell_results": results}

    # =========================
    # Immediate BUY (1-shot)
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
        self._dbg("buy_immediate.enter", auto_buy=self.settings.auto_buy,
                  stk_cd=stk_cd, last_price=last_price, order_type=order_type)

        if not self.settings.auto_buy:
            logger.info("⛔ [immediate] auto_buy 비활성 → 매수 차단")
            self._dbg_ret("buy_immediate.block", reason="auto_buy=False")
            return None

        try:
            p = int(float(last_price))
        except Exception:
            logger.info("🚫 [immediate] last_price 변환 실패")
            self._dbg_ret("buy_immediate.block", reason="price_cast_fail")
            return None
        if not stk_cd or p <= 0:
            logger.info("🚫 [immediate] 종목코드/가격 유효하지 않음")
            self._dbg_ret("buy_immediate.block", reason="invalid_params", stk_cd=stk_cd, price=p)
            return None

        payload = {
            "stk_cd": stk_cd,
            "dmst_stex_tp": dmst_stex_tp,
            "cur_price": p,
            "num_slices": 1,
            "start_ticks_below": 0,
            "step_ticks": 1,
            "unit_amount": int(unit_amount) if unit_amount else self.ladder.unit_amount,
        }

        self._dbg("buy_immediate.dispatch", payload=payload, temp_order_type=order_type or self.settings.order_type)

        if order_type in ("limit", "market"):
            old = self.settings.order_type
            try:
                self.settings.order_type = order_type
                ret = await self._handle_ladder_buy(payload)
                self._dbg_ret("buy_immediate")
                return ret
            finally:
                self.settings.order_type = old
        else:
            ret = await self._handle_ladder_buy(payload)
            self._dbg_ret("buy_immediate")
            return ret

    def _submit_order(self, *, code: str, side: str, qty: int, price: float, trde_tp: Optional[str] = None):
        """레거시 내부 경로도 브로커로 우회."""
        trde_tp = trde_tp or self._resolve_trde_tp()
        req = OrderRequest(
            dmst_stex_tp="KRX",
            stk_cd=str(code),
            ord_qty=int(qty),
            ord_uv=None if trde_tp == "3" else int(price),
            trde_tp=trde_tp,
            side=side.upper(),
            cond_uv=""
        )
        r = self.broker.place_order(req)
        return {"status_code": r.status_code, "header": r.header, "body": r.body}

    # =========================
    # WebSocket events mapping
    # =========================
    def on_ws_message(self, raw: Dict[str, Any]) -> None:
        try:
            msg_type = str(raw.get("type") or raw.get("event") or "").upper()
        except Exception:
            return

        self._dbg("ws.recv", msg_type=msg_type, raw_type=raw.get("type") or raw.get("event"))

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
        info = {
            "side": side, "symbol": sym, "fill_qty": qty, "fill_price": price,
            "exec_id": exec_id, "part_seq": None if part_seq in (None, "") else str(part_seq),
            "order_id": raw.get("order_id"), "ts": ts,
        }
        self._dbg("ws.fill.mapped", info=info)
        return info

    def _map_cancel(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            sym = str(raw.get("symbol") or raw.get("stk_cd"))
            qty = int(raw.get("canceled_qty") or raw.get("qty") or 0)
            order_id = str(raw.get("order_id") or "")
            ts = raw.get("ts") or raw.get("time") or ""
        except Exception:
            return None
        info = {"symbol": sym, "qty": qty, "order_id": order_id, "ts": ts, "raw": raw}
        self._dbg("ws.cancel.mapped", info=info)
        return info

    def _map_reject(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            sym = str(raw.get("symbol") or raw.get("stk_cd"))
            qty = int(raw.get("qty") or 0)
            reason = raw.get("reason") or raw.get("err_msg") or ""
            ts = raw.get("ts") or raw.get("time") or ""
        except Exception:
            return None
        info = {"symbol": sym, "qty": qty, "reason": reason, "ts": ts, "raw": raw}
        self._dbg("ws.reject.mapped", info=info)
        return info

    def _dedupe_fill(self, info: Dict[str, Any]) -> bool:
        key = (info["exec_id"], info.get("part_seq"))
        with self._exec_lock:
            accept = key not in self._seen_exec_keys
            self._dbg("ws.fill.dedupe", exec_id=info["exec_id"], part_seq=info.get("part_seq"), accept=accept)
            if not accept:
                return False
            self._seen_exec_keys.add(key)
            return True

    def _apply_fill(self, info: Dict[str, Any]) -> None:
        if not self.position_mgr:
            return
        side = info["side"]; sym = info["symbol"]
        qty = int(info["fill_qty"]); price = float(info["fill_price"])
        self._dbg("pm.apply_fill", side=side, sym=sym, qty=qty, price=price)
        if side == "BUY":
            try: self.position_mgr.apply_fill_buy(sym, qty, price)
            except Exception: pass
        elif side == "SELL":
            try: self.position_mgr.apply_fill_sell(sym, qty, price)
            except Exception: pass

    # ---------- UI event emit ----------
    def _emit_order_event(self, evt: Dict[str, Any]) -> None:
        self._dbg("emit.order_event", status=evt.get("status"), type=evt.get("type"),
                  action=evt.get("action"), symbol=evt.get("symbol"), price=evt.get("price"), qty=evt.get("qty"))
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
