# websocket_client.py
import asyncio
import websockets
import json
import time
import os
import requests
from typing import Optional, Any, Dict, List

# Flask-SocketIO를 쓰지 않는 환경도 고려한 선택적 임포트
try:
    from flask_socketio import SocketIO  # type: ignore
except Exception:
    SocketIO = Any  # 타입 힌팅 대체


def _pick_first(d: Dict[str, Any], keys: List[str], default: str = "") -> str:
    """여러 후보 키 중 첫 값 반환"""
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip() != "":
            return str(v)
    return default


def _normalize_code(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    # 가장 흔한 패턴들 제거
    if s.startswith("A"):
        s = s[1:]
    s = s.replace("_AL", "")
    # 6자리로
    s = s[:6]
    return s.zfill(6)


class WebSocketClient:
    def __init__(
        self,
        uri: str,
        token: str,
        socketio: Optional[SocketIO] = None,
        on_condition_list=None,
        on_new_stock=None,             # 문자열 code 콜백
        rest_domain: str = "https://api.kiwoom.com",
        on_new_stock_detail=None,      # 상세 dict 콜백(옵션)
        refresh_token_cb=None,
    ):
        self.uri = uri
        self.token = token
        self.websocket = None
        self.connected = False
        self.keep_running = True
        self.stock_callback = None
        self.socketio = socketio

        self.on_condition_list = on_condition_list
        self.on_new_stock = on_new_stock
        self.on_new_stock_detail = on_new_stock_detail

        self.condition_idx_to_name_dict: Dict[str, str] = {}
        self.condition_name_to_idx_dict: Dict[str, str] = {}
        self.stock_code_to_name: Dict[str, str] = {}

        # REST 도메인 (모의투자일 경우 "https://mockapi.kiwoom.com")
        self.rest_domain = rest_domain
        self.refresh_token_cb = refresh_token_cb        


    # --------------------------
    # WebSocket 연결/송수신
    # --------------------------
    async def connect(self):
        try:
            self.websocket = await websockets.connect(self.uri)
            self.connected = True
            print("🟢 WebSocket 연결 성공")

            # 로그인
            await self.send_message({"trnm": "LOGIN", "token": self.token})

        except Exception as e:
            print(f"❌ WebSocket 연결 실패: {e}")
            self.connected = False

    async def send_message(self, message):
        if not self.connected:
            await self.connect()
        if self.connected:
            if not isinstance(message, str):
                message = json.dumps(message, ensure_ascii=False)
            await self.websocket.send(message)
            print(f"Message sent: {message}")

    async def wait_for_condition_list(self, timeout=10):
        start = time.time()
        while time.time() - start < timeout:
            try:
                response = json.loads(await self.websocket.recv())
                if response.get("trnm") == "CNSRLST":
                    return response
            except Exception as e:
                print(f"수신 오류: {e}")
        return {}

    async def receive_messages(self):
        while self.keep_running:
            if not self.connected:
                print("🔄 WebSocket 재연결 시도 중...")
                await self.connect()
                if not self.connected:
                    await asyncio.sleep(5)
                    continue

            try:
                raw = await self.websocket.recv()
                response = json.loads(raw)
                trnm = response.get("trnm")

                # 1) 로그인
                if trnm == "LOGIN":
                    if response.get("return_code") != 0:
                        print(f"❌ 로그인 실패: {response.get('return_msg')}")
                        await self.disconnect()
                    else:
                        print("🔐 로그인 성공")
                        await self.request_condition_list()

                # 2) 핑퐁
                elif trnm == "PING" or response.get("trnm") == "PING":
                    await self.send_message(response)

                # 3) 조건식 목록
                elif trnm == "CNSRLST":
                    print(f"📦 raw response: {response}")

                    data = response.get("data")
                    if not isinstance(data, list):
                        print("⚠️ 'data' 키 없음 또는 형식 오류")
                        continue

                    print(f"✅ 수신한 조건식 목록: {data}")

                    # 매핑 초기화
                    self.condition_idx_to_name_dict.clear()
                    self.condition_name_to_idx_dict.clear()

                    for cond in data:
                        # 보통 [seq, name]
                        if isinstance(cond, (list, tuple)) and len(cond) >= 2:
                            seq, name = cond[0], cond[1]
                            self.condition_idx_to_name_dict[str(seq)] = str(name)
                            self.condition_name_to_idx_dict[str(name)] = str(seq)

                    # JSON 저장 & 콜백
                    try:
                        os.makedirs("static", exist_ok=True)
                        with open("static/conditions.json", "w", encoding="utf-8") as f:
                            json.dump(data, f, ensure_ascii=False, indent=2)
                        print("conditions.json 저장 완료")
                        if self.on_condition_list:
                            self.on_condition_list(data)
                    except Exception as e:
                        print(f"❌ 조건식 저장 또는 전송 실패: {e}")

                # 4) 초기 조건검색 결과
                elif trnm == "CNSRREQ":
                    print("✅ 초기 조건검색 응답:")

                    data_list = response.get("data") or []
                    # 코드→이름 캐시 초기화
                    self.stock_code_to_name = {}

                    for item in data_list:
                        # 구조: {"values": {...}} 또는 평평한 dict
                        values = item.get("values")
                        if not isinstance(values, dict) or not values:
                            values = item

                        raw_code = _pick_first(values, ["9001", "jmcode", "code", "isu_cd", "stock_code", "stk_cd"])
                        code = _normalize_code(raw_code)
                        name = _pick_first(values, ["302", "name", "stock_name", "isu_nm", "stk_nm"], default="종목명 없음")
                        price = _pick_first(values, ["10", "cur_prc", "price", "stck_prpr"], default="0")

                        if not code:
                            print("⚠️ 종목코드 없음. 응답 구조 확인 필요:", item)
                            continue

                        self.stock_code_to_name[code] = name
                        print(f"   - 종목코드: {code}, 종목명: {name}, 현재가: {price}")

                        cond_name = self.condition_idx_to_name_dict.get(response.get("seq", ""), "")
                        base_payload = {
                            "stock_code": code,
                            "stock_name": name,
                            "price": price,
                            "condition_name": cond_name,
                        }
                        asyncio.create_task(self._emit_code_and_detail(base_payload))

                # 5) 실시간 편입/편출
                elif trnm == "REAL":
                    for item in response.get("data", []):
                        if item.get("name") == "조건검색":
                            info_map = item.get("values", {}) or {}
                            cond_idx = (info_map.get("841") or "").split(" ")[0]
                            cond_name = self.condition_idx_to_name_dict.get(cond_idx, "")
                            code = _normalize_code(info_map.get("9001", ""))
                            inout = info_map.get("843")
                            name = self.stock_code_to_name.get(code, "알 수 없음")

                            print(f"- 종목명: {name},\n- 종목코드: {code},\n- 편입편출: {inout}")

                            if inout == "I" and code:
                                base_payload = {
                                    "stock_code": code,
                                    "condition_index": cond_idx,
                                    "condition_name": cond_name,
                                    "stock_name": name,
                                }
                                asyncio.create_task(self._emit_code_and_detail(base_payload))

            except websockets.ConnectionClosed:
                print("⚠️ WebSocket 연결 종료됨. 재연결 대기...")
                self.connected = False
                await asyncio.sleep(5)
            except Exception as e:
                print(f"❌ 메시지 수신 중 예외 발생: {e}")
                self.connected = False
                await asyncio.sleep(5)

    # --------------------------
    # 조건식 관련 송신
    # --------------------------
    async def request_condition_list(self):
        await self.send_message({"trnm": "CNSRLST"})

    async def send_condition_clear_request(self, seq: str):
        clear_payload = {"trnm": "CNSRCLR", "seq": seq}
        await self.send_message(clear_payload)
        print(f"🧹 CNSRCLR 조건 해제 요청 보냄: {clear_payload}")

    async def send_condition_search_request(self, seq: str = "034"):
        # 기존 조건 해제 → 재등록
        await self.send_condition_clear_request(seq)
        search_payload = {
            "trnm": "CNSRREQ",
            "seq": seq,
            "search_type": "1",  # 1: 실시간
            "stex_tp": "K",      # 거래소구분 (필요 시 조정)
        }
        await self.send_message(search_payload)
        print(f"📨 CNSRREQ 조건검색 요청 보냄: {search_payload}")

    async def register_condition_realtime_result(self, condition_name: str):
        cond_idx = self.condition_name_to_idx_dict.get(condition_name)
        if not cond_idx:
            print(f"⚠️ 조건식명 매핑 실패: {condition_name}")
            return
        print(f"{condition_name} 실시간 등록")
        await self.send_message({
            "trnm": "CNSRREQ",
            "seq": f"{cond_idx}",
            "search_type": "1",
            "stex_tp": "K",
        })

    async def remove_condition_realtime(self, seq: str):
        print(f"{seq} 실시간 등록 해제")
        await self.send_message({"trnm": "CNSRCLR", "seq": seq})

    # --------------------------
    # 내부 콜백/유틸
    # --------------------------
    def set_stock_callback(self, callback):
        self.stock_callback = callback

    async def disconnect(self):
        self.keep_running = False
        if self.connected and self.websocket:
            await self.websocket.close()
            self.connected = False
            print("Disconnected from WebSocket server")

    # -------- 신규 감지 시: 코드/상세를 분리 콜백 --------
    async def _emit_code_and_detail(self, base_payload: dict):
        """문자열 코드 콜백(on_new_stock) → 상세 조회 → 상세 콜백(on_new_stock_detail)"""
        code = base_payload.get("stock_code", "")

        # 1) 먼저 문자열 코드만 전달 (즉시 UI표시/큐잉/중복제어)
        try:
            if self.on_new_stock and code:
                self.on_new_stock(code)
        except Exception as e:
            print(f"⚠️ on_new_stock 콜백 처리 오류: {e}")

        # 2) 상세 조회 후 dict 전달(옵션)
        try:
            extra = await self._fetch_stkinfo_for_code(code)
            if extra:
                print(f"🔍 상세 조회 결과: {extra}")
                base_payload.update(extra)
            if self.on_new_stock_detail:
                self.on_new_stock_detail(base_payload)
        except Exception as e:
            print(f"⚠️ on_new_stock_detail 콜백 처리 오류: {e}")

    # -------- REST: /api/dostk/stkinfo --------
    async def _fetch_stkinfo_for_code(self, stock_code: str) -> Optional[dict]:
        """
        /api/dostk/stkinfo를 연속조회 처리하며 stock_code가 포함된 레코드를 찾아 반환.
        반환 필드: cur_prc, flu_rt, open_pric, high_pric, low_pric, open_pric_pre, now_trde_qty, cntr_str
        """
        code = _normalize_code(stock_code)
        if not code:
            return None

        # rest_domain 은 __init__ 등에서 self.rest_domain = "https://api.kiwoom.com" 로 설정해둬야 함
        url = f"{self.rest_domain}/api/dostk/stkinfo"

        def _headers(cont_yn: str | None, next_key: str | None, bearer: str) -> dict:
            h = {
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {bearer}",
                "api-id": "STKINFO",  # 문서상 TR명
            }
            if cont_yn == "Y" and next_key:
                h["cont-yn"] = "Y"
                h["next-key"] = next_key
            return h

        # 조회 범위를 너무 넓히지 않도록(선택)
        # KRX만 본다면 stex_tp=1, 필요한 경우 3(통합) 사용
        body = {
            "sort_tp": "4",             # 4: 기준가 (문서 기준)
            "trde_qty_cnd": "0000",     # 거래량 전체
            "mrkt_tp": "000",           # 전체 (001: 코스피, 101: 코스닥로 좁히면 응답량↓)
            "updown_incls": "1",        # 상/하 포함
            "stk_cnd": "0",             # 전체
            "crd_cnd": "0",             # 전체
            "trde_prica_cnd": "0",      # 거래대금 전체
            "flu_cnd": "1",             # 상위
            "stex_tp": "1",             # 1: KRX (필요시 3: 통합)
        }

        cont_yn = None
        next_key = None
        retries = 0
        max_retries = 3
        backoff = 0.8

        while True:
            # 동기 requests → 스레드 오프로딩
            try:
                resp = await asyncio.to_thread(
                    requests.post,
                    url,
                    headers=_headers(cont_yn, next_key, self.token),
                    json=body,
                    timeout=8,
                )
            except requests.RequestException as e:
                if retries < max_retries:
                    retries += 1
                    await asyncio.sleep(backoff * retries)
                    continue
                print(f"⚠️ stkinfo 네트워크 오류({code}): {e}")
                return None

            # 401이면 토큰 갱신 후 1회 재시도
            if resp.status_code == 401 and hasattr(self, "refresh_token_cb") and callable(self.refresh_token_cb):
                try:
                    new_token = await asyncio.to_thread(self.refresh_token_cb)
                    if new_token:
                        self.token = new_token
                        # 즉시 1회 재시도
                        try:
                            resp = await asyncio.to_thread(
                                requests.post,
                                url,
                                headers=_headers(cont_yn, next_key, self.token),
                                json=body,
                                timeout=8,
                            )
                        except requests.RequestException as e:
                            print(f"⚠️ stkinfo 재시도 네트워크 오류({code}): {e}")
                            return None
                except Exception as e:
                    print(f"⚠️ 토큰 갱신 실패: {e}")
                    return None

            # 2xx 외에는 제한적 재시도
            if resp.status_code // 100 != 2:
                if retries < max_retries and resp.status_code >= 500:
                    retries += 1
                    await asyncio.sleep(backoff * retries)
                    continue
                print(f"⚠️ stkinfo HTTP 오류({code}): {resp.status_code} {resp.text[:200]}")
                return None

            data = resp.json() if resp.content else {}
            rows = data.get("open_pric_pre_flu_rt") or []
            for r in rows:
                r_code = _normalize_code(str(r.get("stk_cd", "")))
                if r_code == code:
                    return {
                        "cur_prc": r.get("cur_prc"),
                        "flu_rt": r.get("flu_rt"),
                        "open_pric": r.get("open_pric"),
                        "high_pric": r.get("high_pric"),
                        "low_pric": r.get("low_pric"),
                        "open_pric_pre": r.get("open_pric_pre"),
                        "now_trde_qty": r.get("now_trde_qty"),
                        "cntr_str": r.get("cntr_str"),
                    }

            # 연속조회 헤더 (requests는 대소문자 무시)
            cont_yn = resp.headers.get("cont-yn", resp.headers.get("Cont-Yn", "N"))
            next_key = resp.headers.get("next-key", resp.headers.get("Next-Key"))

            if cont_yn != "Y" or not next_key:
                break

        return None
