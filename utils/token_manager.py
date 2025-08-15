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
# 설정값 (필요에 맞게 조정 가능)
# -------------------------------
DEFAULT_TOKEN_URL = "https://api.kiwoom.com/oauth2/token"
REQUEST_TIMEOUT_SEC = 8
REQUEST_RETRIES = 3
RETRY_BACKOFF_SEC = 1.2
# 만료 임박 시(초) 재발급 여유
EXPIRY_SAFETY_MARGIN_SEC = 60

# 캐시 경로 (기본: 프로젝트 루트/.cache/token_cache.json)
CACHE_DIR = Path(os.getcwd()) / ".cache"
CACHE_FILE = CACHE_DIR / "token_cache.json"
LOCK_FILE = CACHE_DIR / "token_cache.lock"

# 모듈 전역(프로세스 내) 메모리 캐시
_mem_token: Optional[str] = None
_mem_expires_at: Optional[float] = None
_mem_lock = threading.Lock()


# -------------------------------
# 유틸
# -------------------------------
def _now_ts() -> float:
    return time.time()


def _normalize_epoch_seconds(ts: float) -> float:
    """
    epoch 값을 초 단위로 정규화.
    - 밀리초(>1e12)면 1000으로 나눔
    - 음수/비정상적으로 먼 미래(>10년)는 now+1h 로 대체
    """
    try:
        ts = float(ts)
    except Exception:
        return _now_ts() + 3600

    if ts > 1e12:  # ms로 추정
        ts = ts / 1000.0
    if ts < 0 or ts > (_now_ts() + 10 * 365 * 24 * 3600):
        ts = _now_ts() + 3600
    return ts


def _ts_to_str(ts: float) -> str:
    # 입력 ts는 반드시 초 단위여야 함
    ts = _normalize_epoch_seconds(ts)
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _ensure_cache_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


class _FileLock:
    """
    간단한 파일락. 같은 프로세스 내 중복 호출은 _mem_lock으로도 보호되지만,
    여러 프로세스에서 동시에 접근하는 경우를 대비.
    """
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
                # O_CREAT | O_EXCL 로 원자적 생성 시도
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode("utf-8"))
                return self
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
                if _now_ts() > deadline:
                    # 락 강제 해제(스테일) 시도
                    try:
                        os.remove(self.path)
                    except Exception:
                        pass
                    # 마지막 한 번 더 시도
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
# 만료 파서
# -------------------------------
def _parse_expires_from_str(dt_str: str) -> Optional[float]:
    """
    문자열 만료시각 파서.
    지원:
      - "YYYY-MM-DD HH:MM:SS"
      - "YYYYMMDDHHMMSS" (14자리)
      - ISO8601 유사(가능하면 fromisoformat 활용, 'Z'는 +00:00으로 교체)
      - epoch 문자열(숫자만)
    """
    if not dt_str:
        return None
    s = str(dt_str).strip()

    # epoch number
    if s.isdigit():
        try:
            return _normalize_epoch_seconds(float(s))
        except Exception:
            pass

    # 14-digit compact
    if len(s) == 14 and s.isdigit():
        try:
            dt = datetime.strptime(s, "%Y%m%d%H%M%S")
            return _normalize_epoch_seconds(dt.timestamp())
        except Exception:
            pass

    # "YYYY-MM-DD HH:MM:SS"
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return _normalize_epoch_seconds(dt.timestamp())
    except Exception:
        pass

    # ISO8601-ish
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        return _normalize_epoch_seconds(dt.timestamp())
    except Exception:
        pass

    return None


def _parse_expires_from_response(data: Dict[str, Any]) -> float:
    """
    응답 딕셔너리에서 만료 시각(UNIX timestamp)을 추출.
    지원 포맷:
      - expires_in: 초(int/str)
      - expires_at: epoch(초) 또는 ISO8601/표준 문자열
      - expires_dt: "YYYY-MM-DD HH:MM:SS" 또는 "YYYYMMDDHHMMSS"
    찾지 못하면 기본 3600초.
    """
    now = _now_ts()

    # 1) expires_in
    if "expires_in" in data and str(data["expires_in"]).strip():
        try:
            return _normalize_epoch_seconds(now + float(data["expires_in"]))
        except Exception:
            pass

    # 2) expires_at
    if "expires_at" in data and str(data["expires_at"]).strip():
        ts = _parse_expires_from_str(str(data["expires_at"]))
        if ts:
            return _normalize_epoch_seconds(ts)

    # 3) expires_dt
    if "expires_dt" in data and str(data["expires_dt"]).strip():
        ts = _parse_expires_from_str(str(data["expires_dt"]))
        if ts:
            return _normalize_epoch_seconds(ts)

    # fallback: 1시간
    return _normalize_epoch_seconds(now + 3600)


# -------------------------------
# 캐시 로드/저장
# -------------------------------
def _load_file_cache() -> Tuple[Optional[str], Optional[float]]:
    if not CACHE_FILE.exists():
        return None, None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        token = data.get("token") or data.get("access_token")

        # expires_at(절대시각) 우선, 없으면 expires_dt(문자열), 또 없으면 expires_in(상대)
        expires_at: Optional[float] = None
        if "expires_at" in data and str(data["expires_at"]).strip():
            try:
                expires_at = float(data["expires_at"])
            except Exception:
                expires_at = _parse_expires_from_str(str(data["expires_at"]))
        if not expires_at and "expires_dt" in data and str(data["expires_dt"]).strip():
            expires_at = _parse_expires_from_str(str(data["expires_dt"]))
        if not expires_at and "expires_in" in data and str(data["expires_in"]).strip():
            try:
                expires_at = _now_ts() + float(data["expires_in"])
            except Exception:
                expires_at = None

        if expires_at:
            expires_at = _normalize_epoch_seconds(expires_at)

        return token, expires_at
    except Exception:
        return None, None


def _save_file_cache(token: str, expires_at: float):
    _ensure_cache_dir()
    expires_at = _normalize_epoch_seconds(expires_at)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "token": token,
                "expires_at": expires_at,
                "expires_dt": _ts_to_str(expires_at),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def _is_valid(expires_at: Optional[float]) -> bool:
    if not expires_at:
        return False
    return (_now_ts() + EXPIRY_SAFETY_MARGIN_SEC) < expires_at


# -------------------------------
# 외부에서 토큰 주입/초기화 API
# -------------------------------
def set_access_token(token: str, ttl_seconds: Optional[int] = None):
    """
    외부(예: Engine.initialize)에서 발급된 토큰을 주입.
    ttl_seconds 없으면 55분 유효로 가정(임시).
    """
    global _mem_token, _mem_expires_at
    if not token:
        return
    with _mem_lock:
        _mem_token = token
        expires_at = _now_ts() + (ttl_seconds if ttl_seconds else 55 * 60)
        _mem_expires_at = expires_at
        # 파일 캐시도 반영
        with _FileLock(LOCK_FILE):
            _save_file_cache(_mem_token, _mem_expires_at)


def clear_access_token_cache():
    """메모리/파일 캐시 초기화."""
    global _mem_token, _mem_expires_at
    with _mem_lock:
        _mem_token = None
        _mem_expires_at = None
        try:
            if CACHE_FILE.exists():
                CACHE_FILE.unlink()
        except Exception:
            pass


def get_cached_token() -> Optional[str]:
    """유효한 캐시 토큰(메모리 우선)을 반환. 없으면 None."""
    global _mem_token, _mem_expires_at
    with _mem_lock:
        if _mem_token and _is_valid(_mem_expires_at):
            return _mem_token
    # 파일 캐시 확인
    with _FileLock(LOCK_FILE):
        token, exp = _load_file_cache()
    if token and _is_valid(exp):
        # 메모리 캐시에 반영
        with _mem_lock:
            _mem_token, _mem_expires_at = token, exp
        return token
    return None


# -------------------------------
# 발급 로직
# -------------------------------
def _request_new_token(appkey: str, secretkey: str,
                       token_url: str = DEFAULT_TOKEN_URL) -> Tuple[str, float]:
    """
    실제 토큰 발급 HTTP 호출. (재시도 포함)
    Returns: (token, expires_at_ts)
    """
    payload = {
        "grant_type": "client_credentials",
        "appkey": appkey,
        "secretkey": secretkey,
    }
    headers = {"Content-Type": "application/json;charset=UTF-8"}

    last_exc = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            resp = requests.post(
                token_url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SEC
            )
            if resp.status_code // 100 != 2:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")

            data: Dict[str, Any] = resp.json() if resp.content else {}
            # 다양한 키명에 대응
            token = data.get("access_token") or data.get("token")
            if not token:
                raise KeyError(f"Token not found in response keys: {list(data.keys())}")

            # 다양한 포맷을 지원하는 만료 파서
            expires_at = _parse_expires_from_response(data)

            return token, float(expires_at)
        except Exception as e:
            last_exc = e
            if attempt < REQUEST_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
            else:
                raise

    # 여기 도달하지 않지만 mypy 안심용
    if last_exc:
        raise last_exc
    raise RuntimeError("Token request failed for unknown reasons.")


# -------------------------------
# 퍼블릭 API
# -------------------------------
def get_access_token(appkey: str, secretkey: str,
                     token_url: str = DEFAULT_TOKEN_URL) -> str:
    """
    기존 시그니처 유지. 캐시 확인 후, 필요 시 새로 발급 및 저장.
    """
    global _mem_token, _mem_expires_at
    # 1) 메모리/파일 캐시 체크
    token = get_cached_token()
    if token:
        return token

    # 2) 신규 발급
    token, expires_at = _request_new_token(appkey, secretkey, token_url=token_url)
    expires_at = _normalize_epoch_seconds(expires_at)

    # 3) 캐시에 반영
    with _mem_lock:
        _mem_token, _mem_expires_at = token, expires_at
    with _FileLock(LOCK_FILE):
        _save_file_cache(token, expires_at)

    return token


def get_access_token_cached(appkey: Optional[str] = None,
                            secretkey: Optional[str] = None,
                            token_url: str = DEFAULT_TOKEN_URL) -> str:
    """
    캐시 우선 반환. 없으면 appkey/secretkey로 발급.
    appkey/secretkey가 None이면 utils.utils.load_api_keys() 사용.
    """
    token = get_cached_token()
    if token:
        return token

    if appkey is None or secretkey is None:
        # 지연 임포트 (순환참조 방지)
        from utils.utils import load_api_keys
        appkey, secretkey = load_api_keys()

    return get_access_token(appkey, secretkey, token_url=token_url)


def get_token_expiry() -> Optional[datetime]:
    """현재 캐시된 토큰의 만료 시각을 datetime으로 반환 (없으면 None)."""
    with _mem_lock:
        exp = _mem_expires_at
    if exp:
        return datetime.fromtimestamp(_normalize_epoch_seconds(exp))
    # 파일 캐시에서 확인
    with _FileLock(LOCK_FILE):
        _, exp = _load_file_cache()
    return datetime.fromtimestamp(_normalize_epoch_seconds(exp)) if exp else None
