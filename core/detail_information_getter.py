# detail_information_getter.py
from __future__ import annotations

import os
import json
import time
import logging
import re
import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Dict, List, Optional, Callable, Union

import pandas as pd

import requests
from utils.token_manager import get_access_token
from utils.utils import load_api_keys
from datetime import datetime as dt
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
logging.getLogger("urllib3").setLevel(logging.INFO)
logging.getLogger("matplotlib").setLevel(logging.WARNING)
logging.getLogger("matplotlib.font_manager").setLevel(logging.WARNING)


def _redact(token: str) -> str:
	if not token:
		return ""
	t = str(token)
	return (t[:6] + "..." + t[-4:]) if len(t) > 12 else "***"

def _code6(s: str) -> str:
	d = "".join([c for c in str(s) if c.isdigit()])
	return d[-6:].zfill(6)

def _stkcd(code: str, ex="KRX") -> str:
	return f"{ex}:{_code6(code)}"

class DetailInformationGetter:
	"""
	- 5분봉 차트 데이터 수집 : ka10080
	- 일봉 차트 데이터 수집 : ka10081
	- 설계: 가이드와 동일한 raw 메서드 + 얇은 JSON 래퍼 제공
	"""
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

	# --- ka10080: 분봉 차트 ---
	def fetch_minute_chart_ka10080(self, code: str, *, tic_scope=5, upd_stkpc_tp="1", need=350, exchange_prefix="KRX") -> Dict[str,Any]:
		url = f"{self.base_url}/api/dostk/chart"
		body = {"stk_cd": _stkcd(code, exchange_prefix), "tic_scope": str(tic_scope), "upd_stkpc_tp": str(upd_stkpc_tp)}
		rows_all: List[Dict[str,Any]] = []
		cont_yn, next_key = None, None
		while True:
			resp = requests.post(url, headers=self._headers("ka10080", "Y" if next_key else None, next_key),
								 json=body, timeout=self.timeout)
			resp.raise_for_status()
			try: js = resp.json() or {}
			except: js = {}
			rows = (js.get("stk_min_pole_chart_qry")
					or js.get("stk_min_chart_qry")
					or js.get("body",{}).get("stk_min_pole_chart_qry")
					or js.get("data",{}).get("stk_min_pole_chart_qry")
					or [])
			if isinstance(rows, list): rows_all.extend(rows)
			cont_yn = resp.headers.get("cont-yn", "N"); next_key = resp.headers.get("next-key", "")
			if (need and len(rows_all)>=need) or cont_yn!="Y" or not next_key: break

		# 시간키 기준 중복 제거/정렬 + need tail
		key = lambda r: f"{r.get('dt','')}{r.get('cntr_tm','')}"
		uniq = { key(r): r for r in rows_all }
		rows = [uniq[k] for k in sorted(uniq.keys())]
		if need and len(rows)>need: rows = rows[-need:]
		return {"stock_code": _code6(code), "tic_scope": str(tic_scope), "rows": rows}

	# --- ka10081: 일봉 차트 ---
	def fetch_daily_chart_ka10081(self, code: str, *, base_dt: Optional[str]=None, upd_stkpc_tp: str="1", need: int=400) -> Dict[str,Any]:
		"""
		- URL: /api/dostk/chart
		- api-id: ka10081
		- body 예시: {'stk_cd':'005930', 'base_dt':'20241108', 'upd_stkpc_tp':'1'}
		- 페이지네이션: 헤더 cont-yn/next-key 사용
		- 반환 rows 키가 환경에 따라 다를 수 있어 여러 키를 시도
		"""
		url = f"{self.base_url}/api/dostk/chart"
		code6 = _code6(code)

		body = {"stk_cd": code6, "upd_stkpc_tp": str(upd_stkpc_tp)}
		if base_dt:  # 기준일 지정(YYYYMMDD). 미지정시 서버 기본값 사용
			body["base_dt"] = base_dt

		rows_all: List[Dict[str,Any]] = []
		cont_yn, next_key = None, None

		while True:
			resp = requests.post(url, headers=self._headers("ka10081", "Y" if next_key else None, next_key),
								 json=body, timeout=self.timeout)
			resp.raise_for_status()
			try:
				js = resp.json() or {}
			except:
				js = {}

			# 가능한 키들 순차 시도 (환경별 명칭 차이 흡수)
			rows = (js.get("stk_day_pole_chart_qry")
					or js.get("stk_day_chart_qry")
					or js.get("day_chart")  # 일부 샘플/문서에서
					or js.get("body",{}).get("stk_day_pole_chart_qry")
					or js.get("data",{}).get("stk_day_pole_chart_qry")
					or js.get("open_pric_pre_flu_rt")  # 구(ka10015) 호환 키, 혹시 통일된 응답인 경우
					or [])
			if isinstance(rows, list):
				rows_all.extend(rows)

			cont_yn = resp.headers.get("cont-yn", "N")
			next_key = resp.headers.get("next-key", "")
			# 충분히 모았거나 마지막 페이지면 종료
			if (need and len(rows_all) >= need) or cont_yn != "Y" or not next_key:
				break

		# 날짜 키 기준 정렬 + tail need
		key = lambda r: str(r.get("dt",""))
		uniq = { key(r): r for r in rows_all if r.get("dt") }
		rows = [uniq[k] for k in sorted(uniq.keys())]
		if need and len(rows) > need:
			rows = rows[-need:]

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
