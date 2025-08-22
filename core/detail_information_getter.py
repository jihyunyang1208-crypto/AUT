# detail_information_getter.py
from __future__ import annotations

import os
import json
import time
import logging
from typing import Any, Dict, Optional, List

import requests
from utils.token_manager import get_access_token
from utils.utils import load_api_keys
from datetime import datetime
try:
	from zoneinfo import ZoneInfo
except Exception:
	ZoneInfo = None
	
logger = logging.getLogger(__name__)
if not logger.handlers:
	logging.basicConfig(
		level=os.getenv("LOG_LEVEL", "INFO"),
		format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
	)


def _redact(token: str) -> str:
	if not token:
		return ""
	t = str(token)
	return (t[:6] + "..." + t[-4:]) if len(t) > 12 else "***"


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
		
		logger.debug("Code:", resp.status_code)
		logger.debug(
			"Header:",
			json.dumps(
				{k: resp.headers.get(k) for k in ["next-key", "cont-yn", "api-id"]},
				indent=4,
				ensure_ascii=False,
			),
		)
		
		try:
			logger.debug("Body:", json.dumps(resp.json(), indent=4, ensure_ascii=False))
		except Exception:
			logger.debug("Body:", resp.text)
		
		return resp

	# ---------------------------------------------------------------------
	# KA10015: 일별거래상세 json
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
		"""
		JSON 래퍼: raw 호출을 반복해 모든 페이지 rows를 합쳐 반환.
		반환:
		{
			"stock_code": "005930",
			"strt_dt": "YYYYMMDD",
			"end_dt": "YYYYMMDD"(옵션),
			"rows": [ ...모든 페이지 합본... ],
		}
		"""
		code6 = str(code).strip()[:6].zfill(6)
		rows_all: List[Dict[str, Any]] = []

		# 최대 페이지 가드(무한루프 방지)
		max_pages = 50
		page_count = 0

		while True:
			page_count += 1
			if page_count > max_pages:
				break

			resp = self.fetch_daily_detail_ka10015_raw(
				code,
				strt_dt=strt_dt,
				end_dt=end_dt,
				cont_yn=cont_yn,
				next_key=next_key,
				timeout=timeout,
			)
			try:
				js = resp.json()
			except Exception:
				js = {}
			
			cont_yn = resp.headers.get("cont-yn", "N")
			next_key = resp.headers.get("next-key", "")
			if cont_yn != "Y" or not next_key:
				break

		logger.debug("code information received")
		return {
			"stock_code": code6,
			"strt_dt": strt_dt,
			**({"end_dt": end_dt} if end_dt else {}),
			"rows": rows_all,
		}


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
		
		logger.debug("Code:", resp.status_code)
		logger.debug(
			"Header:",
			json.dumps(
				{k: resp.headers.get(k) for k in ["next-key", "cont-yn", "api-id"]},
				indent=4,
				ensure_ascii=False,
			),
		)
		try:
			logger.debug("Body:", json.dumps(resp.json(), indent=4, ensure_ascii=False))
		except Exception:
			logger.debug("Body:", resp.text)
		
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
					now = datetime.now(ZoneInfo("Asia/Seoul"))
				else:
					now = datetime.now()
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

		logger.debug("Code:", resp.status_code)
		logger.debug(
			"Header:",
			json.dumps(
				{k: resp.headers.get(k) for k in ["next-key", "cont-yn", "api-id"]},
				indent=4,
				ensure_ascii=False,
			),
		)
		try:
			logger.debug("Body:", json.dumps(resp.json(), indent=4, ensure_ascii=False))
		except Exception:
			logger.debug("Body:", resp.text)

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
		"""
		JSON 래퍼: raw 호출을 반복해 모든 페이지 rows를 합쳐 반환.
		반환:
		{
			"stock_code": "005930",
			"strt_dt": "YYYYMMDD",
			"end_dt": "YYYYMMDD"(옵션),
			"rows": [ ...모든 페이지 합본... ],
		}
		"""
		code6 = str(code).strip()[:6].zfill(6)
		rows_all: List[Dict[str, Any]] = []

		# 무한루프 방지
		max_pages = 50
		page_count = 0

		while True:
			page_count += 1
			if page_count > max_pages:
				logger.warning("[KA10015] page limit exceeded (%d) for %s", max_pages, code6)
				break

			resp = self.fetch_daily_detail_ka10015_raw(
				code,
				strt_dt=strt_dt,
				end_dt=end_dt,
				cont_yn=cont_yn,
				next_key=next_key,
				timeout=timeout,
			)

			try:
				js = resp.json() or {}
			except Exception:
				js = {}

			# rows 안전 추출 (환경별 키 차이 흡수)
			rows = (
				js.get("open_pric_pre_flu_rt")
				or js.get("body", {}).get("open_pric_pre_flu_rt")
				or js.get("data", {}).get("open_pric_pre_flu_rt")
				or js.get("rows")
				or []
			)
			if isinstance(rows, list) and rows:
				rows_all.extend(rows)

			# 다음 페이지 여부
			cont_yn = resp.headers.get("cont-yn", "N")
			next_key = resp.headers.get("next-key", "")
			if cont_yn != "Y" or not next_key:
				break

		logger.debug("code information received")
		return {
			"stock_code": code6,
			"strt_dt": strt_dt,
			**({"end_dt": end_dt} if end_dt else {}),
			"rows": rows_all,
		}



# (옵션) 단독 실행 예시
if __name__ == "__main__":
	appkey, secretkey = load_api_keys()
	access_token = get_access_token(appkey, secretkey)

	api = SimpleMarketAPI(token=access_token)

	# KA10015 — 가이드 순정 호출
	logger.debug("\n=== KA10015 (raw) ===")
	_ = api.fetch_daily_detail_ka10015_raw("005930", strt_dt="20250819")

	# KA10015 — JSON 래퍼
	logger.debug("\n=== KA10015 (json) ===")
	daily_json = api.fetch_daily_detail_ka10015("005930", strt_dt="20250819")
	logger.debug(json.dumps(daily_json, indent=2, ensure_ascii=False))

	# KA10080 — 가이드 순정 호출
	logger.debug("\n=== KA10080 (raw) ===")
	params = {"stk_cd": "005930", "tic_scope": "5", "upd_stkpc_tp": "1"}
	_ = api.fetch_intraday_chart_ka10080_raw(params)

	# KA10080 — JSON 래퍼
	logger.debug("\n=== KA10080 (json) ===")
	chart_json = api.fetch_intraday_chart("005930", tic_scope="5", upd_stkpc_tp="1")
