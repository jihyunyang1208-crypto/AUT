# detail_information_getter.py (핵심부만; 그대로 교체 권장)
from __future__ import annotations

import os, json, logging
from typing import Any, Dict, List, Optional
import pandas as pd
import requests
from datetime import datetime as dt
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from core.macd_calculator import calculator  # apply_rows → macd_bus.emit

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("urllib3").setLevel(logging.INFO)

def _redact(token: str) -> str:
    if not token: return ""
    t = str(token);  return (t[:6] + "..." + t[-4:]) if len(t) > 12 else "***"

def _code6(s: str) -> str:
    d = "".join([c for c in str(s) if c.isdigit()])
    return d[-6:].zfill(6)

class DetailInformationGetter:
    def __init__(self, base_url: Optional[str]=None, token: Optional[str]=None, timeout: float=7.0):
        self.base_url = (base_url or os.getenv("HTTP_API_BASE") or "https://api.kiwoom.com").rstrip("/")
        self.token = token or os.getenv("ACCESS_TOKEN") or ""
        self.timeout = timeout
        logger.info("[DetailInfo] base_url=%s", self.base_url)

    def _headers(self, api_id: str, cont_yn: Optional[str]=None, next_key: Optional[str]=None) -> Dict[str,str]:
        h = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.token}",
            "api-id": api_id,
            "accept": "application/json",
        }
        if cont_yn:  h["cont-yn"]  = cont_yn
        if next_key: h["next-key"] = next_key
        return h

    # -------- KA10080: 분봉 수집 + 클린업 --------
    def fetch_minute_chart_ka10080(self, code: str, *, tic_scope=5, upd_stkpc_tp="1", need=350) -> Dict[str,Any]:
        logger.debug("fetch_minute_chart_ka10080")
        url = f"{self.base_url}/api/dostk/chart"
        code6 = _code6(code)
        body = {"stk_cd": code6, "tic_scope": str(tic_scope), "upd_stkpc_tp": str(upd_stkpc_tp)}

        rows_all: List[Dict[str,Any]] = []
        cont_yn, next_key = None, None

        while True:
            resp = requests.post(url, headers=self._headers("ka10080", "Y" if next_key else "N", next_key),
                                 json=body, timeout=self.timeout)
            logger.debug("Code: %s", resp.status_code)
            logger.debug("Header: %s", json.dumps(
                {k: resp.headers.get(k) for k in ["next-key", "cont-yn", "api-id"]}, ensure_ascii=False, indent=2))
            try:
                logger.debug("Body: %s", json.dumps(resp.json(), ensure_ascii=False, indent=2))
            except Exception:
                logger.debug("Body(raw): %s", resp.text)

            resp.raise_for_status()
            try: js = resp.json() or {}
            except Exception: js = {}

            rows = (js.get("stk_min_pole_chart_qry")
                    or js.get("stk_min_chart_qry")
                    or js.get("body",{}).get("stk_min_pole_chart_qry")
                    or js.get("data",{}).get("stk_min_pole_chart_qry")
                    or [])
            if isinstance(rows, list):
                # 모든 값이 빈 문자열/None인 더미행 제거
                cleaned = [r for r in rows if any(v not in ("", None) for v in r.values())]
                rows_all.extend(cleaned)

            cont_yn = resp.headers.get("cont-yn", "N")
            next_key = resp.headers.get("next-key", "")
            if (need and len(rows_all) >= need) or cont_yn != "Y" or not next_key:
                break

        # 키 표준화(파서/계산기 호환): dt→base_dt, cntr_tm→trd_tm
        for r in rows_all:
            if "dt" in r and "base_dt" not in r: r["base_dt"] = r["dt"]
            if "cntr_tm" in r and "trd_tm" not in r: r["trd_tm"] = r["cntr_tm"]

        # 시간키 기준 중복 제거 + 정렬 + tail
        key = lambda r: f"{r.get('base_dt', r.get('dt',''))}{r.get('trd_tm', r.get('cntr_tm',''))}"
        uniq = { key(r): r for r in rows_all if (r.get('trd_tm') or r.get('cntr_tm')) }
        rows = [uniq[k] for k in sorted(uniq.keys())]
        if need and len(rows) > need: rows = rows[-need:]

        logger.debug("[KA10080] rows(after clean)=%d", len(rows))
        return {"stock_code": code6, "tic_scope": str(tic_scope), "rows": rows}

    # --------- 한 방에 MACD 버스까지 ---------
    def emit_macd_for_ka10080(
        self,
        bridge,
        code: str,
        *,
        tic_scope: int = 5,
        upd_stkpc_tp: str = "1",
        need: int = 350,
        max_points: int = 200,
    ) -> dict:
        packet = self.fetch_minute_chart_ka10080(
            code, tic_scope=tic_scope, upd_stkpc_tp=upd_stkpc_tp, need=need
        )
        rows = packet.get("rows", [])
        code6 = packet.get("stock_code")
        tic = int(packet.get("tic_scope", tic_scope))

        # (선택) 원본 rows도 UI로
        if hasattr(bridge, "minute_bars_received"):
            bridge.minute_bars_received.emit(code6, rows)

        # ✅ 계산기 호출 → macd_bus.emit → 다이얼로그 반영
        tf = "5m" if tic == 5 else "30m" if tic == 30 else f"{tic}m"
        calculator.apply_rows(code=code6, tf=("5m" if tf not in ("5m","30m","1d") else tf),
                              rows=rows, need=max_points)

        return {"code": code6, "tf": tf, "count": min(max_points, len(rows))}

    # -------- KA10081: 일봉 --------
    def fetch_daily_chart_ka10081(self, code: str, *, base_dt: Optional[str]=None,
                                  upd_stkpc_tp: str="1", need: int=400) -> Dict[str,Any]:
        url = f"{self.base_url}/api/dostk/chart"
        code6 = _code6(code)
        body = {"stk_cd": code6, "upd_stkpc_tp": str(upd_stkpc_tp)}
        if base_dt: body["base_dt"] = base_dt

        rows_all: List[Dict[str,Any]] = []
        cont_yn, next_key = None, None
        while True:
            resp = requests.post(url, headers=self._headers("ka10081", "Y" if next_key else "N", next_key),
                                 json=body, timeout=self.timeout)
            resp.raise_for_status()
            try: js = resp.json() or {}
            except Exception: js = {}
            rows = (js.get("stk_day_pole_chart_qry")
                    or js.get("stk_day_chart_qry")
                    or js.get("day_chart")
                    or js.get("body",{}).get("stk_day_pole_chart_qry")
                    or js.get("data",{}).get("stk_day_pole_chart_qry")
                    or [])
            if isinstance(rows, list): rows_all.extend(rows)
            cont_yn = resp.headers.get("cont-yn", "N"); next_key = resp.headers.get("next-key", "")
            if (need and len(rows_all) >= need) or cont_yn != "Y" or not next_key: break

        key = lambda r: str(r.get("dt",""))
        uniq = { key(r): r for r in rows_all if r.get("dt") }
        rows = [uniq[k] for k in sorted(uniq.keys())]
        if need and len(rows)>need: rows = rows[-need:]
        return {"stock_code": code6, "rows": rows, "base_dt": base_dt or ""}


class SimpleMarketAPI:
	"""
	- base_url: env HTTP_API_BASE 없으면 https://api.kiwoom.com
	- token   : 'Bearer ' 제외 원본 토큰 문자열
	- 설계: 가이드와 동일한 raw 메서드 + 얇은 JSON 래퍼 제공
	"""

	def __init__(
		self,
		*,
		base_url: Optional[str] = None,
		token: Optional[str] = None,
		default_timeout: float = 7.0,
	):
		self.base_url = (base_url or os.getenv("HTTP_API_BASE") or "https://api.kiwoom.com").rstrip("/")
		self.token = token or os.getenv("ACCESS_TOKEN") or ""
		self.default_timeout = default_timeout
		logger.info("[SimpleMarketAPI] base_url=%s token=%s", self.base_url, _redact(self.token))

	def set_token(self, token: str):
		self.token = token or ""
		logger.info("[SimpleMarketAPI] token updated: %s", _redact(self.token))

	# ---------------------------------------------------------------------
	# KA10015: 일별거래상세 (가이드 1:1 ― raw)
	# ---------------------------------------------------------------------
	def fetch_daily_detail_ka10015_raw(
		self,
		code: str,
		*,
		strt_dt: str,				  # YYYYMMDD (필수)
		end_dt: Optional[str] = None,  # 선택
		cont_yn: str = "N",
		next_key: str = "",
		timeout: Optional[float] = None,
	) -> requests.Response:
		"""
		가이드 순정 호출:
		  - URL: /api/dostk/stkinfo
		  - headers: Content-Type, authorization, api-id=ka10015, (옵션) cont-yn/next-key
		  - body: { "stk_cd": "005930", "strt_dt": "YYYYMMDD", (opt) "end_dt": "YYYYMMDD" }
		콘솔에 Code/Header/Body를 그대로 출력하고, requests.Response를 반환합니다.
		"""
		url = f"{self.base_url}/api/dostk/stkinfo"
		timeout = timeout or self.default_timeout

		# 가이드에 맞춰 6자리 숫자만 전송
		code6 = str(code).strip()[:6].zfill(6)

		headers = {
			"Content-Type": "application/json;charset=UTF-8",
			"authorization": f"Bearer {self.token}",
			"api-id": "ka10015",
		}
		if cont_yn == "Y":
			headers["cont-yn"] = "Y"
		if next_key:
			headers["next-key"] = next_key

		body = {"stk_cd": code6, "strt_dt": str(strt_dt)}
		if end_dt:
			body["end_dt"] = str(end_dt)

		resp = requests.post(url, headers=headers, json=body, timeout=timeout)
		
		logger.debug("Code: %s", resp.status_code)
		logger.debug(
			"Header: %s",
			json.dumps(
				{k: resp.headers.get(k) for k in ["next-key", "cont-yn", "api-id"]},
				indent=4,
				ensure_ascii=False,
			),
		)
		
		try:
			logger.debug("Body: %s", json.dumps(resp.json(), indent=4, ensure_ascii=False))
		except Exception:
			logger.debug("Body: %s", resp.text)
		
		return resp


	# ---------------------------------------------------------------------
	# KA10080: 분봉 차트 (가이드 1:1 ― raw)
	# ---------------------------------------------------------------------
	def fetch_intraday_chart_ka10080_raw(
		self,
		data: Dict[str, Any],
		*,
		cont_yn: str = "N",
		next_key: str = "",
		timeout: Optional[float] = None,
	) -> requests.Response:
		"""
		가이드 순정 호출:
		  - URL: /api/dostk/chart
		  - headers: Content-Type, authorization, api-id=ka10080, (옵션) cont-yn/next-key
		  - body: 호출자가 준비한 data 그대로 사용
		콘솔에 Code/Header/Body를 그대로 출력하고, requests.Response를 반환합니다.
		"""
		url = f"{self.base_url}/api/dostk/chart"
		timeout = timeout or self.default_timeout

		headers = {
			"Content-Type": "application/json;charset=UTF-8",
			"authorization": f"Bearer {self.token}",
			"api-id": "ka10080",
		}
		if cont_yn == "Y":
			headers["cont-yn"] = "Y"
		if next_key:
			headers["next-key"] = next_key

		resp = requests.post(url, headers=headers, json=data, timeout=timeout)
		
		logger.debug("Code: %s", resp.status_code)
		logger.debug(
			"Header:: %s",
			json.dumps(
				{k: resp.headers.get(k) for k in ["next-key", "cont-yn", "api-id"]},
				indent=4,
				ensure_ascii=False,
			),
		)
		try:
			logger.debug("Body: %s", json.dumps(resp.json(), indent=4, ensure_ascii=False))
		except Exception:
			logger.debug("Body: %s", resp.text)
		
		return resp



	def fetch_intraday_chart(
		self,
		code: str,
		*,
		tic_scope: str = "5",	 # 1/3/5/10/15/30/45/60
		upd_stkpc_tp: str = "1",  # 0/1
		cont_yn: str = "N",
		next_key: str = "",
		timeout: Optional[float] = None,
		# ▼ 저장 옵션
		save_json: bool = True,
		out_dir: Optional[str] = None,	 # 기본: (cwd)/chart
		date_str: Optional[str] = None,	# 기본: 오늘(Asia/Seoul) YYYYMMDD
	) -> Dict[str, Any]:
		"""
		얇은 JSON 래퍼:
		- code는 6자리로 맞춰 전송 (접두사 미부착)
		- 연속조회 필요시 cont_yn='Y', next_key='...'로 다시 호출
		- save_json=True 이면 (root)/chart/{code}_{date}.json 으로 저장
		"""
		code6 = str(code).strip()[:6].zfill(6)
		data = {
			"stk_cd": code6,
			"tic_scope": str(tic_scope),
			"upd_stkpc_tp": str(upd_stkpc_tp),
		}
		resp = self.fetch_intraday_chart_ka10080_raw(
			data, cont_yn=cont_yn, next_key=next_key, timeout=timeout
		)

		# JSON 파싱
		try:
			payload = resp.json()
		except Exception:
			payload = {}

		# === 파일 저장 처리 ===
		if save_json:
			# 날짜 문자열 결정
			if date_str:
				dstr = str(date_str)
			else:
				if ZoneInfo:
					now = dt.now(ZoneInfo("Asia/Seoul"))
				else:
					now = dt.now()
				dstr = now.strftime("%Y%m%d")

			# 디렉토리 결정
			base_root = os.getenv("PROJECT_ROOT", os.getcwd())
			target_dir = out_dir or os.path.join(base_root, "chart")
			os.makedirs(target_dir, exist_ok=True)

			# 파일 경로
			filename = f"{code6}_{dstr}.json"
			filepath = os.path.join(target_dir, filename)

			# 저장
			try:
				with open(filepath, "w", encoding="utf-8") as f:
					json.dump(payload, f, ensure_ascii=False, indent=2)
				logger.debug(f"[chart] saved: {filepath}")
			except Exception as e:
				logger.debug(f"[chart] save failed: {e}")

		return payload


	# ---------------------------------------------------------------------
	# KA10001: 주식기본정보요청 (가이드 1:1 ― raw)
	# ---------------------------------------------------------------------
	def fetch_basic_info_ka10001_raw(
		self,
		data: Dict[str, Any],
		*,
		cont_yn: str = "N",
		next_key: str = "",
		timeout: Optional[float] = None,
	) -> requests.Response:
		"""
		가이드 순정 호출:
		  - URL: /api/dostk/stkinfo
		  - headers: Content-Type, authorization, api-id=ka10001, (옵션) cont-yn/next-key
		  - body: 호출자가 준비한 data 그대로 사용 (예: {"stk_cd":"005930"} 등)
		콘솔에 Code/Header/Body를 그대로 출력하고, requests.Response를 반환합니다.
		"""
		url = f"{self.base_url}/api/dostk/stkinfo"
		timeout = timeout or self.default_timeout

		headers = {
			"Content-Type": "application/json;charset=UTF-8",
			"authorization": f"Bearer {self.token}",
			"api-id": "ka10001",
		}
		if cont_yn == "Y":
			headers["cont-yn"] = "Y"
		if next_key:
			headers["next-key"] = next_key

		resp = requests.post(url, headers=headers, json=data, timeout=timeout)

		logger.debug("Code: %s", resp.status_code)
		logger.debug(
			"Header:: %s",
			json.dumps(
				{k: resp.headers.get(k) for k in ["next-key", "cont-yn", "api-id"]},
				indent=4,
				ensure_ascii=False,
			),
		)
		try:
			logger.debug("Body: %s", json.dumps(resp.json(), indent=4, ensure_ascii=False))
		except Exception:
			logger.debug("Body: %s", resp.text)

		return resp

	def fetch_basic_info_ka10001(
		self,
		code: Optional[str] = None,
		*,
		cont_yn: str = "N",
		next_key: str = "",
		timeout: Optional[float] = None,
		**kwargs: Any,
	) -> Dict[str, Any]:
		body: Dict[str, Any] = dict(kwargs)
		if code:
			code6 = str(code).strip()[:6].zfill(6)
			body.setdefault("stk_cd", code6)

		resp = self.fetch_basic_info_ka10001_raw(
			body, cont_yn=cont_yn, next_key=next_key, timeout=timeout
		)
		try:
			return resp.json()
		except Exception:
			return {}

	# ---------------------------------------------------------------------
	# KA10001: 주식기본정보요청 (얇은 JSON 래퍼)
	# ---------------------------------------------------------------------
	def fetch_daily_detail_ka10015(
		self,
		code: str,
		*,
		strt_dt: str,
		end_dt: Optional[str] = None,
		cont_yn: str = "N",
		next_key: str = "",
		timeout: Optional[float] = None,
	) -> Dict[str, Any]:
		code6 = str(code).strip()[:6].zfill(6)
		rows_all: List[Dict[str, Any]] = []
		max_pages, page_count = 50, 0

		while True:
			page_count += 1
			if page_count > max_pages:
				logger.warning("[KA10015] page limit exceeded (%d) for %s", max_pages, code6)
				break

			resp = self.fetch_daily_detail_ka10015_raw(
				code, strt_dt=strt_dt, end_dt=end_dt, cont_yn=cont_yn, next_key=next_key, timeout=timeout
			)
			try:
				js = resp.json() or {}
			except Exception:
				js = {}

			rows = (
				js.get("open_pric_pre_flu_rt")
				or js.get("body", {}).get("open_pric_pre_flu_rt")
				or js.get("data", {}).get("open_pric_pre_flu_rt")
				or js.get("rows")
				or []
			)
			if isinstance(rows, list) and rows:
				rows_all.extend(rows)

			cont_yn = resp.headers.get("cont-yn", "N")
			next_key = resp.headers.get("next-key", "")
			if cont_yn != "Y" or not next_key:
				break

		logger.debug("[KA10015] rows: %d", len(rows_all))
		return {"stock_code": code6, "strt_dt": strt_dt, **({"end_dt": end_dt} if end_dt else {}), "rows": rows_all}
		





	# (옵션) 단독 실행 예시
	if __name__ == "__main__":
		appkey, secretkey = load_api_keys()
		access_token = get_access_token(appkey, secretkey)

		api = SimpleMarketAPI(token=access_token)


		# KA10080 — JSON 래퍼
		logger.debug("\n=== KA10080 (json) ===")
		api.fetch_minute_chart_ka10080("005930", tic_scope="5", upd_stkpc_tp="1")

