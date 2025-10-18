# websocket_client.py
import asyncio
import websockets
from websockets import exceptions 
import json
import time
from typing import Optional, Any, Dict, List, Callable
from core.detail_information_getter import SimpleMarketAPI
import logging
import threading
from core.symbol_cache import symbol_name_cache
logger = logging.getLogger(__name__)


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
        *,
        uri: str,
        token: str,
        market_api: SimpleMarketAPI,                              # ← 의존성 주입
        socketio: Optional[SocketIO] = None,
        on_condition_list: Optional[Callable[[List[Any]], None]] = None,
        #on_new_stock: Optional[Callable[[str], None]] = None,     # 문자열 code 콜백
        #on_new_stock_detail: Optional[Callable[[Dict[str, Any]], None]] = None,  # 상세 dict 콜백
        dedup_ttl_sec: int = 3,
        detail_timeout_sec: float = 6.0,
        refresh_token_cb: Optional[Callable[[], str]] = None,    
        bridge=None,               
        **kwargs
    ):
        self.uri = uri
        self.token = token
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.connected = False
        self.keep_running = True

        self.socketio = socketio
        self.on_condition_list = on_condition_list
        #self.on_new_stock = on_new_stock
        #self.on_new_stock_detail = on_new_stock_detail

        # 세션/태스크 상태 (이전 답변의 _gen/_reader_task/_hb_task도 그대로 유지)
        self._gen = 0
        self._reader_task: Optional[asyncio.Task]= None
        self._hb_task: Optional[asyncio.Task]= None
        self._writer_task: Optional[asyncio.Task] = None
        self._connect_lock = None            
        self._connecting: bool = False        # ← NEW: 중복 접속 가드
        self._suspend_reconnect_until: float = 0.0  # ← NEW: R10001 이후 재연결 유예
        self._loop: Optional[asyncio.AbstractEventLoop] = None


        # 🔧 start()/stop() 실행 제어용 상태 추가
        self._start_lock = threading.Lock()   # <-- 누락돼서 에러났던 부분
        self._runner_thread = None            # 전용 스레드 모드
        self._runner_task = None              # 외부 루프 모드
        self._stopped = False
        self._outbox = None
        self._loop_id = None   

              
        self.market_api = market_api
        self.refresh_token_cb = refresh_token_cb

        # 조건식/종목 매핑 캐시
        self.condition_idx_to_name_dict: Dict[str, str] = {}
        self.condition_name_to_idx_dict: Dict[str, str] = {}
        self.stock_code_to_name: Dict[str, str] = {}

        # 중복 제어/타임아웃
        self._recent_codes_ttl: Dict[str, float] = {}
        self._dedup_ttl_sec = dedup_ttl_sec
        self._detail_timeout_sec = detail_timeout_sec

        self.bridge = bridge
        try:
            btype = type(self.bridge).__name__ if self.bridge else None
            logger.info("bridge wired: %s", btype)
            if self.bridge:
                logger.debug("bridge has signals: "
                             "detail=%s, new_stock=%s",
                             hasattr(self.bridge, "new_stock_detail_received"),
                             hasattr(self.bridge, "new_stock_received"))
        except Exception as e:
            logger.debug("bridge introspect failed: %s", e)

        self.start()


    def self_bridge(self, bridge):
        self.bridge = bridge
        logger.info("bridge set: %s", type(self.bridge).__name__ if bridge else None)
    def attach_bridge(self, bridge):
        self.bridge = bridge
        logger.info("bridge attached via attach_bridge()")

    def start(self, loop: asyncio.AbstractEventLoop | None = None):
        with self._start_lock:
            self._stopped = False

            if loop and loop.is_running():
                self._loop = loop
                if getattr(self, "_runner_task", None) and not self._runner_task.done():
                    logger.debug("start(): runner already active on external loop")
                    return
                # 외부 루프 사용 시에도 primitives는 그 루프에서 '한 번만' 생성
                if self._connect_lock is None:
                    self._connect_lock = asyncio.Lock()
                if self._outbox is None:
                    self._outbox = asyncio.Queue()
                self._runner_task = loop.create_task(self._run_client(), name="ws-runner")
                logger.info("started on existing loop")
                return

            if self._runner_thread and self._runner_thread.is_alive():
                logger.debug("start(): runner thread already alive")
                return

            def _runner():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop  # ★ 루프 보관!

                # ★★ 러너 루프에서 primitives '한 번만' 생성
                self._connect_lock = asyncio.Lock()
                self._outbox = asyncio.Queue()

                self._runner_task = loop.create_task(self._run_client(), name="ws-runner")
                try:
                    loop.run_forever()
                finally:
                    pending = asyncio.all_tasks(loop)
                    for t in pending:
                        t.cancel()
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    loop.close()

            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            self._runner_thread = t
            logger.info("started on dedicated thread")

    def _ensure_async_primitives(self):
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        if self._outbox is None:
            self._outbox = asyncio.Queue()

    async def _run_client(self):
        self._ensure_async_primitives()      
        backoff = 1.0
        while not getattr(self, "_stopped", False):

            now = time.time()
            remain = getattr(self, "_suspend_reconnect_until", 0.0) - now
            if remain > 0:
                await asyncio.sleep(min(remain, 5.0))
                continue

            ok = await self.connect()
            if ok:
                backoff = 1.0
                try:
                    # ✅ reader task가 종료될 때까지 대기
                    await self._reader_task
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.error("Reader task failed, reconnecting...")
            else:
                await asyncio.sleep(min(30.0, backoff))
                backoff = min(30.0, backoff * 2.0 + 1.0)

        # 종료 정리
        try:
            await self._cleanup()
        except Exception as e:                     
            logger.error("Cleanup failed: %s", e)  
        logger.info("ws runner stopped")

    async def _cleanup(self):
        """태스크/소켓 정리"""
        tasks = [self._hb_task, self._reader_task, self._writer_task]
        for task in tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self.websocket:
            try:
                await self.websocket.close()
            except Exception as e:
                logger.debug("websocket.close() failed: %s", e)
        
        self.websocket = None
        self.connected = False


    def stop(self):
        with self._start_lock:
            if self._stopped:
                return
            self._stopped = True
            self.keep_running = False  # ← 권장: 수신 루프 빠르게 탈출
            owns_loop = bool(self._runner_thread and self._runner_thread.is_alive())
            if self._loop and self._loop.is_running():
                def _shutdown():
                    async def _do():
                        await self._cleanup()
                    asyncio.create_task(_do())
                    if owns_loop:
                        self._loop.stop()
                self._loop.call_soon_threadsafe(_shutdown)
            if not owns_loop and self._runner_task and not self._runner_task.done():
                try:
                    self._runner_task.cancel()
                except Exception:
                    pass
            self._runner_task = None

    # --------------------------
    # WebSocket 연결/송수신
    # --------------------------
    async def connect(self) -> bool:
        self._ensure_async_primitives()      
        async with self._connect_lock:
            if self._connecting or self.connected:
                return False
            if time.time() < self._suspend_reconnect_until:
                return False
            self._connecting = True
        try:
            self.websocket = await websockets.connect(
                self.uri, ping_interval=30, ping_timeout=10, close_timeout=5, max_queue=None
            )
            self.connected = True
            if self.bridge and hasattr(self.bridge, "log"):
                self.bridge.log.emit("🟢 WebSocket 연결 성공")


            # 이전 태스크 정리 후 새 태스크 시작
            await self._cleanup_tasks()
            self._reader_task = asyncio.create_task(self.receive_messages(), name="ws-reader")
            self._hb_task = asyncio.create_task(self._heartbeat(), name="ws-hb")
            self._writer_task = asyncio.create_task(self._drain_outbox(), name="ws-writer")
            # 로그인 전송
            await self.send_message({"trnm": "LOGIN", "token": self.token})

            return True
        except Exception as e:
            logger.warning("❌ WebSocket 연결 실패: %s", e)  
            try:
                if self.bridge and hasattr(self.bridge, "log"):
                    self.bridge.log.emit(f"❌ WebSocket 연결 실패: {type(e).__name__}", str(e))
            except Exception as emit_e:
                logger.error("bridge.log.emit failed: %s", emit_e)
            self.connected = False
            return False
        finally:
            self._connecting = False

    async def _cleanup_tasks(self):
        tasks = [self._reader_task, self._hb_task, self._writer_task]
        for task in tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass



    async def _send_raw(self, message: Any):
        await self.websocket.send(json.dumps(message, ensure_ascii=False))

    """        
        if isinstance(message, str):
            await self.websocket.send(message)
        else:
            await self.websocket.send(json.dumps(message, ensure_ascii=False))
        logger.debug("Message sent: %s", message)
    """

    async def _enqueue(self, payload: dict | str):
        self._ensure_async_primitives() 
        msg = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        try:
            self._outbox.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning("outbox full; dropping message: %s", msg[:120])

    def send(self, payload: dict | str) -> None:
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("WebSocketClient loop not ready yet")
        asyncio.run_coroutine_threadsafe(self._enqueue(payload), self._loop)

    async def send_message(self, payload: dict | str):
        # 내부 코루틴에서 쓰는 용도라면 그대로 _enqueue 호출
        await self._enqueue(payload)

    async def _drain_outbox(self):
        self._ensure_async_primitives()
        try:
            while True:
                msg = await self._outbox.get()
                if not self.connected or not self.websocket:
                    await asyncio.sleep(0.2)
                    await self._outbox.put(msg)
                    continue
                try:
                    await self.websocket.send(msg)
                    logger.debug("Message sent: %s", msg)
                except Exception as e:
                    logger.debug("writer send failed: %s", e)
                    await asyncio.sleep(0.2)
                    await self._outbox.put(msg)
                    # break  ❌
                    continue  # ✅ 계속 돌아라
        except asyncio.CancelledError:
            logger.info("Writer task cancelled")
        except Exception as e:
            logger.error("Writer task failed: %s", e)

    async def _heartbeat(self):
        """Periodically sends a PING to keep the connection alive."""
        try:
            while self.connected:
                # 서버 요구사항에 따라 PING 메시지를 보냅니다.
                await self.send_message({"trnm": "PING"})
                # 서버의 타임아웃 시간에 맞춰 적절한 대기 시간을 설정합니다.
                await asyncio.sleep(55) # 예: 서버 타임아웃이 60초일 경우
        except asyncio.CancelledError:
            logger.info("Heartbeat task cancelled.")
        except Exception as e:
            logger.error(f"Heartbeat task failed: {e}")
        finally:
            self.connected = False

    async def wait_for_condition_list(self, timeout=10):
        if self._reader_task and not self._reader_task.done():
            raise RuntimeError("wait_for_condition_list cannot be used while reader is running.")

        if not self.websocket:
            return {}
        start = time.time()
        while time.time() - start < timeout:
            try:
                response = json.loads(await self.websocket.recv())
                if response.get("trnm") == "CNSRLST":
                    return response
            except Exception as e:
                logger.debug(f"수신 오류: {e}")
        return {}

    async def receive_messages(self):

        try:
            while self.keep_running and self.connected:

                raw = await self.websocket.recv()
                response = json.loads(raw)
                trnm = response.get("trnm")

                # 1) 로그인
                if trnm == "LOGIN":
                    if response.get("return_code") != 0:
                        self.bridge.log.emit(f"❌ 로그인 실패: {response.get('return_msg')}")
                        # (옵션) 토큰 갱신 후 재시도
                        if self.refresh_token_cb:
                            try:
                                new_token = self.refresh_token_cb()
                                if new_token:
                                    self.token = new_token
                                    self.bridge.log.emit("🔁 토큰 갱신 후 재로그인 시도")
                                    await self._send_raw({"trnm": "LOGIN", "token": self.token})
                                    continue
                            except Exception as e:
                                logger.debug(f"⚠️ 토큰 갱신 실패: {e}")
                        break
                    else:
                        self.bridge.log.emit("🔐 로그인 성공")
                        await self.request_condition_list()

                # 2) 서버 시스템 메시지
                elif trnm == "SYSTEM":
                    code = response.get("code")
                    if code == "R10001":
                        # 동일 계정 중복 접속 → 유예 후 재연결
                        self._suspend_reconnect_until = time.time() + 60
                        logger.warning("SYSTEM R10001: another session logged in. suspend reconnect for 60s.")
                        break  # 현재 루프 종료 → 소켓은 서버가 곧 닫음

                elif trnm == "PING":
                    await self.send_message(response)
                    

                # 3) 조건식 목록
                elif trnm == "CNSRLST":
                    data = response.get("data")
                    if not isinstance(data, list):
                        logger.debug("⚠️ 'data' 키 없음 또는 형식 오류")
                        continue

                    # 매핑 초기화
                    self.condition_idx_to_name_dict.clear()
                    self.condition_name_to_idx_dict.clear()

                    for cond in data:
                        # 보통 [seq, name]
                        if isinstance(cond, (list, tuple)) and len(cond) >= 2:
                            seq, name = cond[0], cond[1]
                            self.condition_idx_to_name_dict[str(seq)] = str(name)
                            self.condition_name_to_idx_dict[str(name)] = str(seq)

                    # 저장 & 콜백
                    try:
                        import os
                        os.makedirs("static", exist_ok=True)
                        with open("static/conditions.json", "w", encoding="utf-8") as f:
                            json.dump(data, f, ensure_ascii=False, indent=2)
                        logger.debug("conditions.json 저장 완료")
                        if self.on_condition_list:
                            self.on_condition_list(data)
                    except Exception as e:
                        logger.debug(f"❌ 조건식 저장 또는 전송 실패: {e}")

                # 4) 초기 조건검색 결과
                elif trnm == "CNSRREQ":
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
                            logger.debug("⚠️ 종목코드 없음. 응답 구조 확인 필요: %s", item)
                            continue

                        self.stock_code_to_name[code] = name
                        logger.debug(f"   - 종목코드: {code}, 종목명: {name}, 현재가: {price}")

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
                            price_str = info_map.get("10", "0")

                            logger.debug(f"- 종목명: {name},\n- 종목코드: {code},\n- 편입편출: {inout}")

                            if inout == "I" and code:
                                base_payload = {
                                    "stock_code": code,
                                    "condition_index": cond_idx,
                                    "condition_name": cond_name,
                                    "stock_name": name,
                                    "price": price_str,
                                }
                                asyncio.create_task(self._emit_code_and_detail(base_payload))

        except websockets.exceptions.ConnectionClosed:
            logger.warning("⚠️ WebSocket 연결 종료됨. 재연결 대기...")
            self.connected = False

        except json.JSONDecodeError:
            logger.error(f"❌ JSON 디코딩 오류: {raw}")

        except Exception as e:
            logger.exception(f"메시지 수신 중 예상치 못한 오류 발생: {e}")
        finally:
            await self.disconnect()

    # --------------------------
    # 조건식 관련 송신
    # --------------------------
    async def request_condition_list(self):
        await self.send_message({"trnm": "CNSRLST"})

    async def send_condition_clear_request(self, seq: str):
        clear_payload = {"trnm": "CNSRCLR", "seq": seq}
        await self.send_message(clear_payload)
        logger.debug(f"🧹 CNSRCLR 조건 해제 요청 보냄: {clear_payload}")

    async def send_condition_search_request(self, seq: str = "034"):
        # 기존 조건 해제 → 재등록
        # await self.send_condition_clear_request(seq)
        search_payload = {
            "trnm": "CNSRREQ",
            "seq": seq,
            "search_type": "1",  # 1: 실시간
            "stex_tp": "K",      # 거래소구분 (필요 시 조정)
        }
        await self.send_message(search_payload)
        logger.debug(f"📨 CNSRREQ 조건검색 요청 보냄: {search_payload}")

    async def register_condition_realtime_result(self, condition_name: str):
        cond_idx = self.condition_name_to_idx_dict.get(condition_name)
        if not cond_idx:
            logger.debug(f"⚠️ 조건식명 매핑 실패: {condition_name}")
            return
        logger.debug(f"{condition_name} 실시간 등록")
        await self.send_message({
            "trnm": "CNSRREQ",
            "seq": f"{cond_idx}",
            "search_type": "1",
            "stex_tp": "K",
        })

    async def remove_condition_realtime(self, seq: str):
        logger.debug(f"{seq} 실시간 등록 해제")
        await self.send_message({"trnm": "CNSRCLR", "seq": seq})

    # --------------------------
    # 내부 콜백/유틸
    # --------------------------
    async def disconnect(self):
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
        self.connected = False

    # -------- 신규 감지 시: 코드/상세를 분리 콜백 (KA10001만 사용) --------
    async def _emit_code_and_detail(self, base_payload: dict):
        """문자열 코드 콜백(on_new_stock) → KA10001로 종목명 보강 → 상세 콜백(on_new_stock_detail)"""
        code = str(base_payload.get("stock_code", "")).strip()
        if not code:
            # 🔒 UI 스레드 전송은 항상 bridge 시그널로
            try:
                if hasattr(self, "bridge") and hasattr(self.bridge, "new_stock_detail_received"):
                    self.bridge.new_stock_detail_received.emit(base_payload)
                else:
                    logger.warning("bridge or signal missing; drop detail payload(no code)")
            except Exception as e:
                logger.warning("bridge emit failed (no code): %s", e)
            return

        # 0) 과호출/중복 방지
        now = time.time()
        exp = self._recent_codes_ttl.get(code)
        if exp and now < exp:
            return
        self._recent_codes_ttl[code] = now + self._dedup_ttl_sec

        try:
            
            # main.py에서 monitor 객체를 bridge에 연결해두었으므로, bridge를 통해 접근
            if self.bridge and hasattr(self.bridge, "monitor") and self.bridge.monitor:
                
                cond_name = base_payload.get("condition_name", "N/A")
                logger.info("Monitor.on_condition_detected 호출 준비: %s (즉시 매수 평가 시작, 조건식: %s)", code, cond_name)
                async def run_detection():
                    await self.bridge.monitor.on_condition_detected(
                        symbol=code,
                        # ✅ price=price_val 인수를 제거합니다.
                        condition_name=base_payload.get("condition_name", "")
                    )
                
                if self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(run_detection(), self._loop)
                else:
                    logger.warning("WebSocketClient loop not available, creating new task for monitor.")
                    asyncio.create_task(run_detection())
            else:
                logger.warning("Bridge 또는 Monitor가 연결되지 않아 즉시 매수 신호를 보낼 수 없습니다.")

        except Exception as e:
            logger.error(f"monitor.on_condition_detected 호출 실패: {e}")

        # 1) 선공지: 코드만 즉시 전달 (bridge 시그널 사용)
        try:
            self.bridge.new_stock_received.emit(code)
            logger.debug("emit new_stock_received: %s", code)
        except Exception as e:
            logger.warning("emit new_stock_received failed for %s: %s", code, e)


        # 2) KA10001(주식기본정보)로 종목명 보강만 수행
        try:
            code6 = code[:6].zfill(6)
            logger.info("fetching KA10001 for %s", code6)

            res01 = await asyncio.wait_for(
                asyncio.to_thread(self.market_api.fetch_basic_info_ka10001, code6),
                timeout=self._detail_timeout_sec,
            )
            if not isinstance(res01, dict):
                res01 = {}

            # 종목명 추출
            try:
                name01 = self._extract_name_from_ka10001(res01)
            except Exception:
                name01 = (
                    res01.get("stk_nm")
                    or res01.get("stock_name")
                    or (res01.get("body") or {}).get("stk_nm")
                    or "종목명 없음"
                )

            # 1) 캐시에 넣고
            symbol_name_cache.set(code, name01)
            # 2) 브리지로 이벤트 발행 (UI가 바로 라벨 갱신할 수 있도록)
            if self.bridge:
                try:
                    self.bridge.symbol_name_updated.emit(code, name01)
                except Exception:
                    logger.warning("bridge emit failed for %s: %s", code, e)

            # UI로 넘길 최소 정보 구성
            detail = {
                "stock_code": code6,
                "stock_name": name01 or base_payload.get("stock_name") or "종목명 없음",
            }

            # 조건식명 보강
            if base_payload.get("condition_name"):
                detail["condition_name"] = base_payload["condition_name"]

            # ── KA10001 → UI키 매핑
            merge_map = {
                "cur_prc": "cur_prc",
                "flu_rt": "flu_rt",
                "open_pric": "open_pric",
                "high_pric": "high_pric",
                "low_pric": "low_pric",
                "trde_qty": "now_trde_qty",  # 거래량
                "cntr_str": "cntr_str",
                "open_pric_pre": "open_pric_pre",
            }

            def _is_missing(v) -> bool:
                s = "" if v is None else str(v).strip()
                return s in ("", "-")

            filled_from_ka10001 = []
            for src, dst in merge_map.items():
                v = res01.get(src)  # ← js → res01 로 수정
                if not _is_missing(v):
                    detail[dst] = v
                    filled_from_ka10001.append(dst)

            # ── base_payload에서 보조키 패스스루(비어있을 때만 보강)
            passthrough_keys = (
                "cur_prc", "flu_rt", "open_pric", "high_pric", "low_pric",
                "now_trde_qty", "cntr_str", "open_pric_pre",
                "stck_prpr", "prdy_ctrt", "stck_oprc", "stck_hgpr",
                "stck_lwpr", "acml_vol", "cttr", "prdy_vrss",
                "antc_tr_pbmn", "price",
            )
            applied_from_base = []
            for k in passthrough_keys:
                v = base_payload.get(k)
                if v not in (None, "", "-") and k not in detail:
                    detail[k] = v
                    applied_from_base.append(k)

            logger.info(
                "[WS][KA10001 merge] code=%s name=%s filled_from_ka10001=%s applied_from_base=%s",
                code6, detail.get("stock_name"), filled_from_ka10001, applied_from_base
            )


            # 2) 상세 emit
            try:
                self.bridge.new_stock_detail_received.emit(detail)
                logger.debug("emit new_stock_detail: %s keys=%s",
                            detail.get("stock_code"), list(detail.keys()))
            except Exception as e:
                logger.warning("emit new_stock_detail failed for %s: %s",
                            detail.get("stock_code"), e)

        except asyncio.TimeoutError:
            logger.warning("KA10001 timeout for %s", code)
            # ❌ UI 직접 호출 금지 → 시그널로만
            try:
                self.bridge.new_stock_detail_received.emit(base_payload)
                logger.debug("emit timeout/fallback detail: %s", code)
            except Exception as e:
                logger.warning("emit timeout/fallback failed for %s: %s", code, e)

        except Exception as e:
            logger.warning("⚠️ 상세 조회 실패(%s): %s", code, e)
            # ❌ UI 직접 호출 금지 → 시그널로만
            try:
                if hasattr(self, "bridge") and hasattr(self.bridge, "new_stock_detail_received"):
                    self.bridge.new_stock_detail_received.emit(base_payload)
                else:
                    logger.warning("bridge missing; drop fallback payload for %s", code)
            except Exception as ee:
                logger.warning("bridge emit failed (fallback) for %s: %s", code, ee)


    @staticmethod
    def _pick_rows_any(payload: dict) -> list:
        """여러 형태로 올 수 있는 rows를 통일해서 리스트로 반환."""
        if not isinstance(payload, dict):
            return []
        return (
            payload.get("rows")
            or payload.get("open_pric_pre_flu_rt")
            or (payload.get("body") or {}).get("open_pric_pre_flu_rt")
            or (payload.get("data") or {}).get("open_pric_pre_flu_rt")
            or []
        )

    @staticmethod
    def _pick_first_by_code(rows: list, code6: str) -> dict:
        """rows 리스트에서 stk_cd가 code6(6자리)인 첫 행 반환."""
        if not isinstance(rows, list):
            return {}
        for r in rows:
            try:
                rc = str((r or {}).get("stk_cd", "")).strip()[:6].zfill(6)
                if rc == code6:
                    return r or {}
            except Exception:
                continue
        return {}

    @staticmethod
    def _extract_name_from_ka10001(js: dict) -> str:
        """
        KA10001 응답에서 종목명(stk_nm/isu_nm/stock_name) 추출.
        환경별로 위치가 다를 수 있으니 방어적으로 탐색.
        """
        if not isinstance(js, dict):
            return ""

        # 후보 경로들 우선 탐색
        candidates = [
            ("rows",),
            ("data", "rows"),
            ("body", "rows"),
            ("data", "stk_bas"),
            ("body", "stk_bas"),
        ]
        for path in candidates:
            node = js
            ok = True
            for p in path:
                if isinstance(node, dict) and p in node:
                    node = node[p]
                else:
                    ok = False
                    break
            if ok and isinstance(node, list) and node:
                nm = node[0].get("stk_nm") or node[0].get("isu_nm") or node[0].get("stock_name")
                if nm:
                    return str(nm)

        # 평평한 위치
        nm = js.get("stk_nm") or (js.get("data") or {}).get("stk_nm") or (js.get("body") or {}).get("stk_nm")
        if nm:
            return str(nm)

        # 깊이 탐색(최후 수단)
        def deep(obj):
            if isinstance(obj, dict):
                if "stk_nm" in obj: return obj["stk_nm"]
                if "isu_nm" in obj: return obj["isu_nm"]
                if "stock_name" in obj: return obj["stock_name"]
                for v in obj.values():
                    got = deep(v)
                    if got: return got
            elif isinstance(obj, list):
                for it in obj:
                    got = deep(it)
                    if got: return got
            return None

        nm = deep(js)
        return str(nm) if nm else ""


from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


def _today_yyyymmdd() -> str:
    try:
        if ZoneInfo:
            ts = datetime.now(ZoneInfo("Asia/Seoul"))
        else:
            ts = datetime.now()
        d = ts.strftime("%Y%m%d")
        logger.debug("[helper] _today_yyyymmdd -> %s", d)
        return d
    except Exception as e:
        logger.exception("[helper] _today_yyyymmdd error: %s", e)
        # 실패시 UTC기반 fallback
        return datetime.utcnow().strftime("%Y%m%d")


def _extract_first_row(js: dict, code6: str) -> dict:
    try:
        path = None
        if isinstance(js.get("open_pric_pre_flu_rt"), list):
            rows = js.get("open_pric_pre_flu_rt")
            path = "open_pric_pre_flu_rt"
        elif isinstance(js.get("body", {}).get("open_pric_pre_flu_rt"), list):
            rows = js.get("body", {}).get("open_pric_pre_flu_rt")
            path = "body.open_pric_pre_flu_rt"
        elif isinstance(js.get("data", {}).get("open_pric_pre_flu_rt"), list):
            rows = js.get("data", {}).get("open_pric_pre_flu_rt")
            path = "data.open_pric_pre_flu_rt"
        elif isinstance(js.get("rows"), list):
            rows = js.get("rows")
            path = "rows"
        else:
            rows = []
            path = "(not-found)"

        logger.debug("[extract] rows_path=%s len=%d", path, len(rows) if isinstance(rows, list) else -1)

        first = rows[0] if isinstance(rows, list) and rows else {}
        if not rows:
            logger.warning("[extract] rows empty at path=%s", path)
        else:
            logger.debug("[extract] row0_keys=%s", list(first.keys()) if isinstance(first, dict) else "(not-dict)")

        base = {"stock_code": code6}
        if isinstance(first, dict):
            base.update(first)

        logger.info("[extract] done for code=%s keys=%d", code6, len(base.keys()))
        return base

    except Exception as e:
        logger.exception("[extract] error for code=%s: %s", code6, e)
        return {"stock_code": code6}
