# utils/token_manager.py
from __future__ import annotations

import os
import json
import time
import errno
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple, Dict, Any

import requests

# -------------------------------
# 기존 설정값(그대로 유지)
# -------------------------------
DEFAULT_TOKEN_URL = "https://api.kiwoom.com/oauth2/token"
REQUEST_TIMEOUT_SEC = 8
REQUEST_RETRIES = 3
RETRY_BACKOFF_SEC = 1.2
EXPIRY_SAFETY_MARGIN_SEC = 60

# 기존 캐시 경로 유지
CACHE_DIR = Path(os.getcwd()) / ".cache"
CACHE_FILE = CACHE_DIR / "token_cache.json"
LOCK_FILE = CACHE_DIR / "token_cache.lock"

# 추가: 신형 경로(선택 지원, 읽기/쓰기 겸용)
APP_NAME = "AutoTrader"
def _config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.getenv("APPDATA", Path.home() / "AppData/Roaming"))
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d

ENV_FILE = _config_dir() / ".env"
NEW_TOKEN_FILE = _config_dir() / "access_token.json"  # 신형 파일도 함께 지원

# 모듈 전역 메모리 캐시(기존 유지)
_mem_token: Optional[str] = None
_mem_expires_at: Optional[float] = None
_mem_lock = threading.Lock()

# -------------------------------
# 유틸
# -------------------------------
def _now_ts() -> float:
    return time.time()

def _normalize_epoch_seconds(ts: float) -> float:
    try:
        ts = float(ts)
    except Exception:
        return _now_ts() + 3600
    if ts > 1e12:
        ts = ts / 1000.0
    if ts < 0 or ts > (_now_ts() + 10 * 365 * 24 * 3600):
        ts = _now_ts() + 3600
    return ts

def _ts_to_str(ts: float) -> str:
    ts = _normalize_epoch_seconds(ts)
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

def _ensure_cache_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

class _FileLock:
    def __init__(self, path: Path, timeout: float = 5.0, poll: float = 0.05):
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self.fd = None

    def __enter__(self):
        _ensure_cache_dir()
        deadline = _now_ts() + self.timeout
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode("utf-8"))
                return self
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
                if _now_ts() > deadline:
                    try:
                        os.remove(self.path)
                    except Exception:
                        pass
                    try:
                        self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        os.write(self.fd, str(os.getpid()).encode("utf-8"))
                        return self
                    except Exception:
                        raise TimeoutError(f"Lock acquire timeout: {self.path}")
                time.sleep(self.poll)

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.fd is not None:
                os.close(self.fd)
        finally:
            try:
                if self.path.exists():
                    os.remove(self.path)
            except Exception:
                pass

# -------------------------------
# 키 저장/로드 (로그인 탭용 추가 API)
# -------------------------------
def load_keys() -> Tuple[str, str]:
    """우선순위: ENV(.env 포함) > 프로세스 환경변수"""
    appkey = os.getenv("APP_KEY", "")
    appsecret = os.getenv("APP_SECRET", "")
    if ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                if line.startswith("APP_KEY="):    appkey = line.split("=",1)[1].strip()
                if line.startswith("APP_SECRET="): appsecret = line.split("=",1)[1].strip()
        except Exception:
            pass
    return appkey, appsecret

def set_keys(appkey: str, appsecret: str) -> None:
    """로그인 탭에서 저장 버튼 → .env 기록"""
    ENV_FILE.write_text(f"APP_KEY={appkey}\nAPP_SECRET={appsecret}\n", encoding="utf-8")

# -------------------------------
# 만료 파서(기존 유지 + 보완)
# -------------------------------
def _parse_expires_from_str(dt_str: str) -> Optional[float]:
    if not dt_str:
        return None
    s = str(dt_str).strip()
    if s.isdigit():
        try:
            return _normalize_epoch_seconds(float(s))
        except Exception:
            pass
    if len(s) == 14 and s.isdigit():
        try:
            dt = datetime.strptime(s, "%Y%m%d%H%M%S")
            return _normalize_epoch_seconds(dt.timestamp())
        except Exception:
            pass
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return _normalize_epoch_seconds(dt.timestamp())
    except Exception:
        pass
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        return _normalize_epoch_seconds(dt.timestamp())
    except Exception:
        pass
    return None

def _parse_expires_from_response(data: Dict[str, Any]) -> float:
    now = _now_ts()
    if "expires_in" in data and str(data["expires_in"]).strip():
        try:
            return _normalize_epoch_seconds(now + float(data["expires_in"]))
        except Exception:
            pass
    if "expires_at" in data and str(data["expires_at"]).strip():
        ts = _parse_expires_from_str(str(data["expires_at"]))
        if ts:
            return _normalize_epoch_seconds(ts)
    if "expires_dt" in data and str(data["expires_dt"]).strip():
        ts = _parse_expires_from_str(str(data["expires_dt"]))
        if ts:
            return _normalize_epoch_seconds(ts)
    # 한국투자처럼 만료값을 안 주는 API 대비 기본 24h-60s
    return _normalize_epoch_seconds(now + 24*3600 - 60)

# -------------------------------
# 캐시 로드/저장 (구·신형 모두 지원)
# -------------------------------
def _load_file_cache() -> Tuple[Optional[str], Optional[float]]:
    # 1) 구형 경로 우선(완전 호환)
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            token = data.get("token") or data.get("access_token")
            exp: Optional[float] = None
            if "expires_at" in data and str(data["expires_at"]).strip():
                try:
                    exp = float(data["expires_at"])
                except Exception:
                    exp = _parse_expires_from_str(str(data["expires_at"]))
            if not exp and "expires_dt" in data and str(data["expires_dt"]).strip():
                exp = _parse_expires_from_str(str(data["expires_dt"]))
            if not exp and "expires_in" in data and str(data["expires_in"]).strip():
                try:
                    exp = _now_ts() + float(data["expires_in"])
                except Exception:
                    exp = None
            if exp:
                exp = _normalize_epoch_seconds(exp)
            if token:
                return token, exp
        except Exception:
            pass
    # 2) 신형 경로도 읽기 시도(앞으로의 확장용)
    if NEW_TOKEN_FILE.exists():
        try:
            data = json.loads(NEW_TOKEN_FILE.read_text(encoding="utf-8"))
            token = data.get("token") or data.get("access_token")
            exp = data.get("expires_at")
            if not exp:
                exp = _parse_expires_from_response(data)
            exp = _normalize_epoch_seconds(exp)
            if token:
                return token, exp
        except Exception:
            pass
    return None, None

def _save_file_cache(token: str, expires_at: float):
    _ensure_cache_dir()
    expires_at = _normalize_epoch_seconds(expires_at)
    payload = {
        "token": token,
        "expires_at": expires_at,
        "expires_dt": _ts_to_str(expires_at),
    }
    # 1) 구형 경로(완전 호환)
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # 2) 신형 경로도 같이 갱신(선택)
    NEW_TOKEN_FILE.write_text(json.dumps({"access_token": token, "expires_at": expires_at}, ensure_ascii=False, indent=2), encoding="utf-8")

def _is_valid(expires_at: Optional[float]) -> bool:
    if not expires_at:
        return False
    return (_now_ts() + EXPIRY_SAFETY_MARGIN_SEC) < expires_at

# -------------------------------
# 외부에서 토큰 주입/초기화 (기존 유지)
# -------------------------------
def set_access_token(token: str, ttl_seconds: Optional[int] = None):
    global _mem_token, _mem_expires_at
    if not token:
        return
    with _mem_lock:
        _mem_token = token
        _mem_expires_at = _now_ts() + (ttl_seconds if ttl_seconds else 55 * 60)
        with _FileLock(LOCK_FILE):
            _save_file_cache(_mem_token, _mem_expires_at)

def clear_access_token_cache():
    global _mem_token, _mem_expires_at
    with _mem_lock:
        _mem_token = None
        _mem_expires_at = None
        try:
            if CACHE_FILE.exists():
                CACHE_FILE.unlink()
        except Exception:
            pass
        try:
            if NEW_TOKEN_FILE.exists():
                NEW_TOKEN_FILE.unlink()
        except Exception:
            pass

def get_cached_token() -> Optional[str]:
    global _mem_token, _mem_expires_at
    with _mem_lock:
        if _mem_token and _is_valid(_mem_expires_at):
            return _mem_token
    with _FileLock(LOCK_FILE):
        token, exp = _load_file_cache()
    if token and _is_valid(exp):
        with _mem_lock:
            _mem_token, _mem_expires_at = token, exp
        return token
    return None

# -------------------------------
# 실제 발급 HTTP (기존 이름/동작 유지)
# -------------------------------
def _request_new_token(appkey: str, secretkey: str, token_url: str = DEFAULT_TOKEN_URL) -> Tuple[str, float]:
    payload = {
        "grant_type": "client_credentials",
        "appkey": appkey,
        "secretkey": secretkey,   # ※ 기존 필드명 유지
    }
    headers = {"Content-Type": "application/json;charset=UTF-8"}

    last_exc = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            resp = requests.post(token_url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SEC)
            if resp.status_code // 100 != 2:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
            data: Dict[str, Any] = resp.json() if resp.content else {}
            token = data.get("access_token") or data.get("token")
            if not token:
                raise KeyError(f"Token not found in response keys: {list(data.keys())}")
            expires_at = _parse_expires_from_response(data)
            return token, float(expires_at)
        except Exception as e:
            last_exc = e
            if attempt < REQUEST_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
            else:
                raise
    if last_exc:
        raise last_exc
    raise RuntimeError("Token request failed for unknown reasons.")

# -------------------------------
# 퍼블릭 API (기존 유지)
# -------------------------------
def get_access_token(appkey: str, secretkey: str, token_url: str = DEFAULT_TOKEN_URL) -> str:
    token = get_cached_token()
    if token:
        return token
    token, expires_at = _request_new_token(appkey, secretkey, token_url=token_url)
    expires_at = _normalize_epoch_seconds(expires_at)
    with _mem_lock:
        global _mem_token, _mem_expires_at
        _mem_token, _mem_expires_at = token, expires_at
    with _FileLock(LOCK_FILE):
        _save_file_cache(token, expires_at)
    return token

def get_access_token_cached(appkey: Optional[str] = None, secretkey: Optional[str] = None, token_url: str = DEFAULT_TOKEN_URL) -> str:
    token = get_cached_token()
    if token:
        return token
    if appkey is None or secretkey is None:
        # 기존 코드 호환: utils.utils.load_api_keys()를 시도
        try:
            from utils.utils import load_api_keys  # 지연 임포트
            appkey, secretkey = load_api_keys()
        except Exception:
            # 없으면 .env/ENV에서 로드
            ak, sk = load_keys()
            appkey = appkey or ak
            secretkey = secretkey or sk
    return get_access_token(appkey, secretkey, token_url=token_url)

def get_token_expiry() -> Optional[datetime]:
    with _mem_lock:
        exp = _mem_expires_at
    if exp:
        return datetime.fromtimestamp(_normalize_epoch_seconds(exp))
    with _FileLock(LOCK_FILE):
        _, exp = _load_file_cache()
    return datetime.fromtimestamp(_normalize_epoch_seconds(exp)) if exp else None

# -------------------------------
# 로그인 탭 호환용 별칭(편의)
# -------------------------------
def request_new_token(appkey: Optional[str] = None, appsecret: Optional[str] = None, token_url: str = DEFAULT_TOKEN_URL) -> str:
    """로그인 탭에서 '토큰 발급 테스트' 용. (.env/ENV 로드 + 강제 신규발급)"""
    if not appkey or not appsecret:
        ak, sk = load_keys()
        appkey = appkey or ak
        appsecret = appsecret or sk
    token, exp = _request_new_token(appkey, appsecret, token_url=token_url)
    with _mem_lock:
        global _mem_token, _mem_expires_at
        _mem_token, _mem_expires_at = token, _normalize_epoch_seconds(exp)
    with _FileLock(LOCK_FILE):
        _save_file_cache(_mem_token, _mem_expires_at)
    return token
