# auto_trader.py
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
from typing import Any, Callable, Dict, List, Optional

import requests

# 외부(코어 외부) 패키지: execsim (별도 모듈)
# pip install -e ./execsim (editable 설치 권장)
from execsim import SimConfig, SimExecLogger, VirtualExecutionEngine


# =========================
# 설정 데이터클래스
# =========================
@dataclass
class AutoTradeSettings:
    master_enable: bool = False
    auto_buy: bool = False
    auto_sell: bool = False


@dataclass
class LadderConfig:
    """
    라더(사다리) 매수 기본 설정.
    - unit_amount: 1회 주문 금액(원) — 기본 10만원
    - num_slices: 분할 횟수 — 기본 10회
    - start_ticks_below: 현재가 대비 시작 틱 — 기본 1틱 아래
    - step_ticks: 각 호가 사이 간격 — 기본 1틱 간격
    - trde_tp: '0' 보통(지정가), '3' 시장가 등
    - interval_sec: 연속 주문 간 간격(초)
    """
    unit_amount: int = 100_000
    num_slices: int = 10
    start_ticks_below: int = 1
    step_ticks: int = 1
    min_qty: int = 1
    trde_tp: str = "0"
    interval_sec: float = 0.08


# =========================
# 실주문 로그 파일 로거
# =========================
class TradeLogger:
    """
    주문 로그를 CSV + JSONL로 일자별 저장.
    - CSV: 요약 필드 (분석/피벗 용이)
    - JSONL: 응답 원문 포함 (재현/디버깅)
    개인정보/토큰 등 민감정보는 기록하지 않음.
    """
    def __init__(self, log_dir: str = "logs/trades", file_prefix: str = "orders", log_fn=None):
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
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    "ts","session_id","uid","strategy","action",
                    "stk_cd","dmst_stex_tp","cur_price","limit_price","qty","trde_tp",
                    "tick_mode","tick_used","slice_idx","slice_total","unit_amount",
                    "status_code","status_label","duration_ms","order_id",
                    "resp_header","resp_error","note"
                ])

    @staticmethod
    def _extract_order_id(body: Any) -> Optional[str]:
        if isinstance(body, dict):
            for k in ("odr_no","order_no","ORD_NO","ODR_NO","orgn_odno","odno","KRX_ORD_NO","orderId","ord_id"):
                v = body.get(k)
                if v not in (None, ""):
                    return str(v)
        return None

    def log(self, record: dict, response: Optional[dict], note: str = ""):
        with self._lock:
            csv_path, jsonl_path = self._paths()
            self._ensure_csv_header(csv_path)

            ts = datetime.now(timezone.utc).astimezone().isoformat()

            status_code = response.get("status_code") if response else None
            status_label = "SUCCESS" if (status_code and 200 <= int(status_code) < 300) else ("FAIL" if status_code else "N/A")
            resp_header = response.get("header") if response else None
            body = response.get("body") if response else None
            order_id = self._extract_order_id(body)

            # CSV용 에러 메시지 요약
            body_err = ""
            if isinstance(body, dict):
                body_err = str(body.get("error") or body.get("msg") or body.get("message") or "")[:300]
            elif body is not None:
                body_err = str(body)[:300]

            row = [
                ts,
                record.get("session_id",""),
                record.get("uid",""),
                record.get("strategy",""),
                record.get("action",""),
                record.get("stk_cd",""),
                record.get("dmst_stex_tp",""),
                record.get("cur_price",""),
                record.get("limit_price",""),
                record.get("qty",""),
                record.get("trde_tp",""),
                record.get("tick_mode",""),
                record.get("tick_used",""),
                record.get("slice_idx",""),
                record.get("slice_total",""),
                record.get("unit_amount",""),
                status_code,
                status_label,
                record.get("duration_ms",""),
                order_id or "",
                json.dumps(resp_header, ensure_ascii=False) if resp_header else "",
                body_err,
                note,
            ]

            with csv_path.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)

            full = {
                "ts": ts,
                **record,
                "status_code": status_code,
                "status_label": status_label,
                "order_id": order_id,
                "response": response,
                "note": note,
            }
            with jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(full, ensure_ascii=False) + "\n")


# =========================
# AutoTrader 본체
# =========================
class AutoTrader:
    """
    Kiwoom REST 주문(kt10000) + 라더 매수 + 실주문/시뮬 로그 + 틱 규칙 자동 적용.
    - token_provider: () -> str  (액세스 토큰 반환 함수)
    - log_fn: Optional[Callable[[str], None]]  (UI 로그 출력)
    - paper_mode=True 시 execsim을 이용한 페이퍼 트레이딩 활성화
    """

    def __init__(
        self,
        token_provider: Callable[[], str],
        use_mock: bool = False,
        log_fn: Optional[Callable[[str], None]] = None,
        ladder_config: Optional[LadderConfig] = None,
        log_dir: Optional[str] = None,
        paper_mode: bool = False,
        sim_config: Optional[SimConfig] = None,
        sim_log_dir: Optional[str] = None,
    ) -> None:
        self.settings = AutoTradeSettings()
        self._token_provider = token_provider
        self._use_mock = use_mock
        self._log = log_fn or (lambda m: None)

        self._api_id = "kt10000"
        self._endpoint = "/api/dostk/ordr"

        self.ladder = ladder_config or LadderConfig()

        # 세션/로거
        self.session_id = uuid.uuid4().hex[:8]
        self.trade_logger = TradeLogger(
            log_dir=log_dir or "logs/trades",
            file_prefix="orders",
            log_fn=self._log,
        )

        # 시뮬 엔진 (core 외부 패키지 execsim)
        self.paper_mode = paper_mode
        self.sim_logger = SimExecLogger(log_dir=sim_log_dir or "logs/sim_exec", log_fn=self._log)
        self.sim_engine = VirtualExecutionEngine(
            session_id=self.session_id,
            logger=self.sim_logger,
            config=sim_config or SimConfig(),
            tick_fn=self._krx_tick,
            log_fn=self._log,
        )

    # ============ 공개 API ============
    async def handle_signal(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        payload 예시:
        - 단일 주문:
          {
            "signal": "BUY"|"SELL",
            "data": {...kt10000 body...},
            "cont_yn": "N", "next_key": ""
          }

        - 라더 매수:
          {
            "ladder_buy": True,
            "stk_cd": "005930",
            "dmst_stex_tp": "KRX",
            "cur_price": 67500,
            # "tick": 100,           # 선택: 고정 틱. 없으면 KRX 규칙으로 자동 계산(동적)
            # (옵션) 라더 상수 덮어쓰기:
            # "unit_amount": 100_000, "num_slices": 10,
            # "start_ticks_below": 1, "step_ticks": 1, "trde_tp": "0"
          }
        """
        if not self.settings.master_enable:
            self._log("⏹ master_enable=False: 주문 중단")
            return None

        if payload.get("ladder_buy"):
            return await self._handle_ladder_buy(payload)

        # ---- 단일 주문 경로 ----
        signal = (payload.get("signal") or "").upper()
        if signal == "BUY" and not self.settings.auto_buy:
            self._log("⛔ auto_buy=False: 매수 차단")
            return None
        if signal == "SELL" and not self.settings.auto_sell:
            self._log("⛔ auto_sell=False: 매도 차단")
            return None

        data = self._payload_to_kt10000_data(payload)
        cont_yn = payload.get("cont_yn", "N")
        next_key = payload.get("next_key", "")

        # 토큰
        try:
            token = self._token_provider()
            if not token:
                raise RuntimeError("액세스 토큰이 비어있습니다.")
        except Exception as e:
            self._log(f"🚫 토큰 조회 실패: {e}")
            return None

        # 실주문 수행
        try:
            start = time.perf_counter()
            resp = await asyncio.to_thread(
                self._fn_kt10000, token=token, data=data, cont_yn=cont_yn, next_key=next_key
            )
            duration_ms = int((time.perf_counter() - start) * 1000)

            self._log(f"🛰 kt10000 Code={resp.get('status_code')}")
            self._log(f"🛰 Header={json.dumps(resp.get('header', {}), ensure_ascii=False)}")
            # self._log(f"🛰 Body={json.dumps(resp.get('body'), ensure_ascii=False)}")

            record = {
                "session_id": self.session_id,
                "uid": uuid.uuid4().hex,
                "strategy": "single",
                "action": signal or "N/A",
                "stk_cd": str(data.get("stk_cd","")),
                "dmst_stex_tp": str(data.get("dmst_stex_tp","KRX")).upper(),
                "cur_price": "",
                "limit_price": str(data.get("ord_uv","")),
                "qty": str(data.get("ord_qty","")),
                "trde_tp": str(data.get("trde_tp","")),
                "tick_mode": "", "tick_used": "",
                "slice_idx": "", "slice_total": "",
                "unit_amount": "",
                "duration_ms": duration_ms,
            }
            self.trade_logger.log(record, resp)
            return resp

        except Exception as e:
            record = {
                "session_id": self.session_id,
                "uid": uuid.uuid4().hex,
                "strategy": "single",
                "action": signal or "N/A",
                "stk_cd": str(data.get("stk_cd","")),
                "dmst_stex_tp": str(data.get("dmst_stex_tp","KRX")).upper(),
                "cur_price": "",
                "limit_price": str(data.get("ord_uv","")),
                "qty": str(data.get("ord_qty","")),
                "trde_tp": str(data.get("trde_tp","")),
                "tick_mode": "", "tick_used": "",
                "slice_idx": "", "slice_total": "",
                "unit_amount": "",
                "duration_ms": "",
            }
            self.trade_logger.log(record, response={"status_code": None, "header": None, "body": {"error": str(e)}}, note="exception")
            self._log(f"💥 주문 실패: {e}")
            return None

    def feed_market_event(self, event: Dict[str, Any]):
        """
        실시간 체결/호가 이벤트를 시뮬 엔진에 전달 (paper_mode=True일 때만 유효)
        event = {
          "stk_cd": "005930",
          "last": 67400,
          "bid": 67300, "ask": 67400,
          "high": 68000, "low": 67000,
          "ts": "2025-09-03T15:10:05+09:00"
        }
        """
        if self.paper_mode:
            self.sim_engine.on_market_update(event)

    # ============ 라더(사다리) 매수 ============
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

        # 틱 결정: payload에 tick 있으면 고정, 없으면 동적(KRX 규칙)
        if "tick" in payload and int(payload["tick"]) > 0:
            tick = int(payload["tick"])
            tick_mode = "fixed"
        else:
            tick = self._krx_tick(cur_price)
            tick_mode = "dynamic"

        # 상수(기본) + payload 덮어쓰기
        unit_amount = int(payload.get("unit_amount") or self.ladder.unit_amount)
        num_slices = int(payload.get("num_slices") or self.ladder.num_slices)
        start_ticks_below = int(payload.get("start_ticks_below") or self.ladder.start_ticks_below)
        step_ticks = int(payload.get("step_ticks") or self.ladder.step_ticks)
        trde_tp = str(payload.get("trde_tp") or self.ladder.trde_tp)
        min_qty = self.ladder.min_qty

        # 라더 가격 생성
        if tick_mode == "fixed":
            prices = self._compute_ladder_prices_fixed(
                cur_price=cur_price, tick=tick, count=num_slices,
                start_ticks_below=start_ticks_below, step_ticks=step_ticks
            )
        else:
            prices = self._compute_ladder_prices_dynamic(
                cur_price=cur_price, count=num_slices,
                start_ticks_below=start_ticks_below, step_ticks=step_ticks,
                tick_fn=self._krx_tick
            )

        self._log(f"🪜 (ladder/{tick_mode}) prices={prices}")

        # 토큰
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
            if qty <= 0:
                self._log(f"↪️ (ladder) [{i}/{total}] {limit_price}원: 계산된 수량=0 → 스킵")
                continue

            data = {
                "dmst_stex_tp": dmst_stex_tp,
                "stk_cd": stk_cd,
                "ord_qty": str(qty),
                "ord_uv": str(limit_price),  # 지정가
                "trde_tp": trde_tp,          # 보통(지정가): '0'
                "cond_uv": "",
            }

            uid = uuid.uuid4().hex
            tick_used = tick if tick_mode == "fixed" else self._krx_tick(limit_price)

            if self.paper_mode:
                # === 페이퍼 트레이딩: 가상 주문 등록, 체결은 feed_market_event()로 처리 ===
                sim_oid = self.sim_engine.submit_limit_buy(
                    stk_cd=stk_cd,
                    limit_price=limit_price,
                    qty=qty,
                    parent_uid=uid,
                    strategy="ladder",
                )
                self._log(f"🧪 (paper) [{i}/{total}] NEW {stk_cd} {qty}주 @ {limit_price}원 → sim_oid={sim_oid}")

                # 실주문 로그에는 기록하지 않음(시뮬 로그는 execsim에서 기록)
            else:
                # === 실주문 REST ===
                try:
                    start = time.perf_counter()
                    resp = await asyncio.to_thread(
                        self._fn_kt10000, token=token, data=data, cont_yn="N", next_key=""
                    )
                    duration_ms = int((time.perf_counter() - start) * 1000)

                    code = resp.get("status_code")
                    results.append(resp)
                    self._log(f"✅ (ladder) [{i}/{total}] {stk_cd} {qty}주 @ {limit_price}원 → Code={code}")

                    record = {
                        "session_id": self.session_id,
                        "uid": uid,
                        "strategy": "ladder",
                        "action": "BUY",
                        "stk_cd": stk_cd,
                        "dmst_stex_tp": dmst_stex_tp,
                        "cur_price": cur_price,
                        "limit_price": limit_price,
                        "qty": qty,
                        "trde_tp": trde_tp,
                        "tick_mode": tick_mode,
                        "tick_used": tick_used,
                        "slice_idx": i,
                        "slice_total": total,
                        "unit_amount": unit_amount,
                        "duration_ms": duration_ms,
                    }
                    self.trade_logger.log(record, resp)
                except Exception as e:
                    self._log(f"💥 (ladder) [{i}/{total}] 주문 실패: {e}")
                    record = {
                        "session_id": self.session_id,
                        "uid": uid,
                        "strategy": "ladder",
                        "action": "BUY",
                        "stk_cd": stk_cd,
                        "dmst_stex_tp": dmst_stex_tp,
                        "cur_price": cur_price,
                        "limit_price": limit_price,
                        "qty": qty,
                        "trde_tp": trde_tp,
                        "tick_mode": tick_mode,
                        "tick_used": tick_used,
                        "slice_idx": i,
                        "slice_total": total,
                        "unit_amount": unit_amount,
                        "duration_ms": "",
                    }
                    self.trade_logger.log(record, response={"status_code": None, "header": None, "body": {"error": str(e)}}, note="exception")

                await asyncio.sleep(self.ladder.interval_sec)

        if not self.paper_mode:
            ok = sum(1 for r in results if (r.get("status_code") or 0) // 100 == 2)
            self._log(f"🧾 (ladder) 완료: 성공 {ok}/{len(prices)}")

        return {"ladder_results": results} if not self.paper_mode else {"ladder_submitted": total}

    # ============ 내부 유틸 (KRX 틱 규칙 / 라더 가격) ============
    @staticmethod
    def _krx_tick(price: int) -> int:
        """
        KRX 일반 주식 호가단위 자동계산.
        - < 1,000: 1
        - 1,000 ~ < 5,000: 5
        - 5,000 ~ < 10,000: 10
        - 10,000 ~ < 50,000: 50
        - 50,000 ~ < 100,000: 100
        - 100,000 ~ < 500,000: 500
        - >= 500,000: 1,000
        """
        if price < 1_000:
            return 1
        if price < 5_000:
            return 5
        if price < 10_000:
            return 10
        if price < 50_000:
            return 50
        if price < 100_000:
            return 100
        if price < 500_000:
            return 500
        return 1_000

    @staticmethod
    def _snap_to_tick(price: int, tick: int) -> int:
        if tick <= 0:
            return price
        return (price // tick) * tick

    @classmethod
    def _compute_ladder_prices_fixed(
        cls, cur_price: int, tick: int, count: int, start_ticks_below: int, step_ticks: int
    ) -> List[int]:
        prices: List[int] = []
        ticks = start_ticks_below
        for _ in range(count):
            p = cur_price - (ticks * tick)
            p = cls._snap_to_tick(p, tick)
            if p <= 0:
                break
            prices.append(p)
            ticks += step_ticks
        return prices

    @classmethod
    def _compute_ladder_prices_dynamic(
        cls, cur_price: int, count: int, start_ticks_below: int, step_ticks: int, tick_fn: Callable[[int], int]
    ) -> List[int]:
        prices: List[int] = []
        ticks_to_go = start_ticks_below
        base = cur_price
        for _ in range(count):
            t = max(1, tick_fn(base))
            p = base - (ticks_to_go * t)
            p = cls._snap_to_tick(p, t)
            if p <= 0:
                break
            prices.append(p)
            base = p
            ticks_to_go += step_ticks
        return prices

    # ============ REST 호출 ============
    def _base_url(self) -> str:
        return "https://mockapi.kiwoom.com" if self._use_mock else "https://api.kiwoom.com"

    def _headers(self, token: str, cont_yn: str, next_key: str) -> Dict[str, str]:
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "cont-yn": cont_yn,
            "next-key": next_key,
            "api-id": self._api_id,
        }

    def _payload_to_kt10000_data(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if "data" in payload and isinstance(payload["data"], dict):
            return payload["data"]
        keys = ("dmst_stex_tp", "stk_cd", "ord_qty", "ord_uv", "trde_tp", "cond_uv")
        return {k: str(payload.get(k, "")) for k in keys}

    def _fn_kt10000(
        self, token: str, data: Dict[str, Any], cont_yn: str = "N", next_key: str = ""
    ) -> Dict[str, Any]:
        host = self._base_url()
        url = host + self._endpoint
        headers = self._headers(token, cont_yn, next_key)
        response = requests.post(url, headers=headers, json=data, timeout=10)

        header_subset = {k: response.headers.get(k) for k in ["next-key", "cont-yn", "api-id"]}
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}

        return {"status_code": response.status_code, "header": header_subset, "body": body}
