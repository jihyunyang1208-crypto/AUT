# utils/token_manager.py
"""
# 토큰 관리 규칙

Main 토큰: get_main_token()이 .env의 APP_KEY_1/APP_SECRET_1 우선 사용 → OK.
Settings 저장: set_indexed_keys()가 APP_KEY_n/APP_SECRET_n로 저장 → OK.
Settings 토큰 발급/적용:
- request_new_token_for_profile() / mint_tokens_from_settings_manager() → 발급 후 파일 저장 및 rebuild_kiwoom_accounts_env()로 최신 리스트 덮어쓰기 → OK.
- settings_apply_token_list() → 전달받은 리스트로 누적 없이 완전 교체 → OK.
- 주문(브로커) 단일 진입점: list_order_accounts_strict()가 **ENV(KIWOOM_ACCOUNTS_JSON)**만 신뢰 → 브로커와 일치 → OK.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Tuple, TypedDict, Callable

import requests

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
# 상수/경로
# ────────────────────────────────────────────────────────────────────
DEFAULT_TOKEN_URL = "https://api.kiwoom.com/oauth2/token"
REQUEST_TIMEOUT_SEC = 8
REQUEST_RETRIES = 3
RETRY_BACKOFF_SEC = 1.2
EXPIRY_SAFETY_MARGIN_SEC = 60  # 만료 60초 전부터 재발급
TOKEN_EXP_MARGIN = 60          # 파일캐시 재사용 여유

PROJECT_ROOT = Path.cwd()
CACHE_DIR = PROJECT_ROOT / ".cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 레거시 단일 토큰 파일 캐시(하위호환)
LEGACY_TOKEN_FILE = CACHE_DIR / "token_cache.json"
LEGACY_LOCK_FILE = CACHE_DIR / "token_cache.lock"

# 표준 멀티프로필 토큰 디렉토리/프로필 파일
TOKENS_DIR = CACHE_DIR
TOKENS_DIR.mkdir(parents=True, exist_ok=True)
_PROFILES_FILE = TOKENS_DIR / "kiwoom_profiles.json"

# ────────────────────────────────────────────────────────────────────
# 공용 유틸
# ────────────────────────────────────────────────────────────────────

def _now_ts() -> float:
    return time.time()

def _normalize_epoch_seconds(ts: float) -> float:
    try:
        ts = float(ts)
    except Exception:
        return _now_ts() + 3600
    if ts > 1e12:  # ms → s
        ts = ts / 1000.0
    if ts < 0 or ts > (_now_ts() + 10 * 365 * 24 * 3600):
        ts = _now_ts() + 3600
    return ts

def _ts_to_str(ts: float) -> str:
    ts = _normalize_epoch_seconds(ts)
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", delete=False, encoding=encoding, dir=str(path.parent)) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)

def _safe_key(s: str) -> str:
    return "".join(ch if str(ch).isalnum() or ch in ("-", "_", ".") else "_" for ch in str(s))

def _fingerprint_key(*parts: str) -> str:
    raw = "|".join(p or "" for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

def _cache_id_for(app_key: str, cache_namespace: str, account_id: Optional[str]) -> str:
    fp = _fingerprint_key(cache_namespace, account_id or "", app_key)
    safe_acc = _safe_key(account_id) if account_id else "na"
    return f"{cache_namespace}-{safe_acc}-{fp}"

def _paths_for_cache_id(cache_id: str) -> Tuple[Path, Path]:
    fname = _safe_key(cache_id) + ".json"
    return (TOKENS_DIR / fname, TOKENS_DIR / (fname + ".lock"))

def _paths_for_namespace_id(cache_namespace: str, account_id: Optional[str], app_key: str) -> Tuple[Path, Path]:
    cache_id = _cache_id_for(app_key, cache_namespace, account_id)
    return _paths_for_cache_id(cache_id)

class _FileLock:
    """
    파일 기반 락. 타임아웃 경과 시 고아 락 정리 후 재시도.
    - timeout: 10s
    - poll: 20ms
    """
    def __init__(self, path: Path, timeout: float = 10.0, poll: float = 0.02):
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self.fd: Optional[int] = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
                    # stale lock cleanup
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

# ────────────────────────────────────────────────────────────────────
# .env 읽기/쓰기 (프로젝트 루트만 사용)
# ────────────────────────────────────────────────────────────────────
def update_env_variable(key: str, value: str, env_path: Optional[str] = None) -> Path:
    """
    dotenv 호환 .env 업데이트 (없으면 생성). 프로젝트 루트 .env만 사용.
    os.environ에도 반영.
    """
    if env_path:
        resolved = Path(env_path).expanduser().resolve()
    else:
        resolved = PROJECT_ROOT / ".env"

    if not resolved.exists():
        resolved.touch()

    # python-dotenv가 있으면 사용, 없으면 수동 편집
    try:
        from dotenv import set_key  # type: ignore
        set_key(str(resolved), key, value)
    except Exception:
        try:
            text = resolved.read_text(encoding="utf-8")
        except Exception:
            text = resolved.read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        key_prefix = f"{key}="
        new_lines = [line for line in lines if not line.strip().startswith(key_prefix)]
        safe_value = value.replace("\n", "\\n")
        new_lines.append(f'{key}="{safe_value}"')
        _atomic_write_text(resolved, "\n".join(new_lines) + "\n", encoding="utf-8")

    os.environ[key] = value
    return resolved

def load_keys() -> Tuple[str, str]:
    """
    메인 계좌 키 로딩 우선순위
      1) APP_KEY_1 / APP_SECRET_1
      2) APP_KEY   / APP_SECRET   (하위호환)
    + .env(프로젝트 루트)도 함께 스캔
    """
    appkey = (os.getenv("APP_KEY_1") or os.getenv("APP_KEY") or "").strip()
    appsecret = (os.getenv("APP_SECRET_1") or os.getenv("APP_SECRET") or "").strip()

    def _read_env_file(path: Path, ak: str, sk: str) -> Tuple[str, str]:
        if not path.exists():
            return ak, sk
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("APP_KEY_1="):
                    ak = line.split("=", 1)[1].strip().strip('"').strip()
                elif line.startswith("APP_SECRET_1="):
                    sk = line.split("=", 1)[1].strip().strip('"').strip()
                elif line.startswith("APP_KEY=") and not ak:
                    ak = line.split("=", 1)[1].strip().strip('"').strip()
                elif line.startswith("APP_SECRET=") and not sk:
                    sk = line.split("=", 1)[1].strip().strip('"').strip()
        except Exception:
            pass
        return ak, sk

    appkey, appsecret = _read_env_file(PROJECT_ROOT / ".env", appkey, appsecret)
    return appkey, appsecret

def set_keys(appkey: str, appsecret: str) -> None:
    """
    메인 키를 .env에 기록.
    - 규약: 메인은 APP_KEY_1 / APP_SECRET_1
    - 하위호환: APP_KEY / APP_SECRET도 같이 세팅
    """
    update_env_variable("APP_KEY_1", appkey)
    update_env_variable("APP_SECRET_1", appsecret)
    update_env_variable("APP_KEY", appkey)        # fallback
    update_env_variable("APP_SECRET", appsecret)  # fallback
    os.environ["APP_KEY_1"] = appkey
    os.environ["APP_SECRET_1"] = appsecret
    os.environ["APP_KEY"] = appkey
    os.environ["APP_SECRET"] = appsecret

def set_indexed_keys(index: int, appkey: str, appsecret: str) -> None:
    """
    세팅매니저용: 인덱스에 맞춰 APP_KEY_{n} / APP_SECRET_{n} 저장
    예) index=2 → APP_KEY_2, APP_SECRET_2
    """
    if index < 1:
        raise ValueError("index는 1 이상의 정수여야 합니다.")
    update_env_variable(f"APP_KEY_{index}", appkey)
    update_env_variable(f"APP_SECRET_{index}", appsecret)
    os.environ[f"APP_KEY_{index}"] = appkey
    os.environ[f"APP_SECRET_{index}"] = appsecret

def get_indexed_keys(index: int) -> Tuple[str, str]:
    ak = (os.getenv(f"APP_KEY_{index}") or "").strip()
    sk = (os.getenv(f"APP_SECRET_{index}") or "").strip()
    if not ak or not sk:
        # .env 파일에서 재시도
        path = PROJECT_ROOT / ".env"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"APP_KEY_{index}="):
                    ak = line.split("=", 1)[1].strip().strip('"').strip()
                elif line.startswith(f"APP_SECRET_{index}="):
                    sk = line.split("=", 1)[1].strip().strip('"').strip()
    return ak, sk

# ────────────────────────────────────────────────────────────────────
# 만료/캐시 유틸 (레거시 호환 포함)
# ────────────────────────────────────────────────────────────────────
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
    for fmt in ("%Y-%m-%d %H:%M:%S",):
        try:
            dt = datetime.strptime(s, fmt)
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
    for k in ("expires_at", "expires_dt"):
        if k in data and str(data[k]).strip():
            ts = _parse_expires_from_str(str(data[k]))
            if ts:
                return _normalize_epoch_seconds(ts)
    return _normalize_epoch_seconds(now + 24 * 3600 - 60)

def _is_valid(expires_at: Optional[float]) -> bool:
    return bool(expires_at and (_now_ts() + EXPIRY_SAFETY_MARGIN_SEC) < _normalize_epoch_seconds(expires_at))

# 레거시 단일 토큰 메모리 캐시 (하위호환)
_mem_token: Optional[str] = None
_mem_expires_at: Optional[float] = None
_mem_lock = threading.Lock()

def _load_legacy_file_cache() -> Tuple[Optional[str], Optional[float]]:
    if LEGACY_TOKEN_FILE.exists():
        try:
            data = json.loads(LEGACY_TOKEN_FILE.read_text(encoding="utf-8"))
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
    return None, None

def _save_legacy_file_cache(token: str, expires_at: float) -> None:
    payload = {
        "token": token,
        "expires_at": _normalize_epoch_seconds(expires_at),
        "expires_dt": _ts_to_str(expires_at),
    }
    LEGACY_TOKEN_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def set_access_token(token: str, ttl_seconds: Optional[int] = None) -> None:
    """레거시 호환: 메모리/단일 파일 캐시에 토큰 저장."""
    global _mem_token, _mem_expires_at
    if not token:
        return
    with _mem_lock:
        _mem_token = token
        _mem_expires_at = _now_ts() + (ttl_seconds if ttl_seconds else 55 * 60)
        with _FileLock(LEGACY_LOCK_FILE):
            _save_legacy_file_cache(_mem_token, _mem_expires_at)

def clear_access_token_cache() -> None:
    """레거시 호환: 단일 파일 캐시 삭제."""
    global _mem_token, _mem_expires_at
    with _mem_lock:
        _mem_token = None
        _mem_expires_at = None
        try:
            if LEGACY_TOKEN_FILE.exists():
                LEGACY_TOKEN_FILE.unlink()
        except Exception:
            pass

def get_cached_token() -> Optional[str]:
    """레거시 호환: 메모리/파일 캐시에서 유효 토큰 조회."""
    global _mem_token, _mem_expires_at
    with _mem_lock:
        if _mem_token and _is_valid(_mem_expires_at):
            return _mem_token
    with _FileLock(LEGACY_LOCK_FILE):
        token, exp = _load_legacy_file_cache()
    if token and _is_valid(exp):
        with _mem_lock:
            _mem_token, _mem_expires_at = token, exp
        return token
    return None

def get_token_expiry() -> Optional[datetime]:
    with _mem_lock:
        exp = _mem_expires_at
    if exp:
        return datetime.fromtimestamp(_normalize_epoch_seconds(exp))
    with _FileLock(LEGACY_LOCK_FILE):
        _, exp = _load_legacy_file_cache()
    return datetime.fromtimestamp(_normalize_epoch_seconds(exp)) if exp else None

# ────────────────────────────────────────────────────────────────────
# HTTP 토큰 발급 (JSON/FORM 모두 시도)
# ────────────────────────────────────────────────────────────────────
def _request_new_token(appkey: str, secretkey: str, token_url: str = DEFAULT_TOKEN_URL) -> Tuple[str, float]:
    def _h(s):
        s = (s or "").strip()
        return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]
    logger.info("TokenReq url=%s appkey#%s secret#%s", token_url, _h(appkey), _h(secretkey))

    # token_url 유효성
    from urllib.parse import urlparse
    pu = urlparse(token_url or "")
    if not (pu.scheme and pu.netloc):
        raise RuntimeError(f"token_url이 올바르지 않습니다: {token_url!r}")

    headers_json = {"Content-Type": "application/json;charset=UTF-8", "Accept": "application/json"}
    headers_form = {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", "Accept": "application/json"}

    def _find_token_and_exp_in_headers(h) -> Tuple[Optional[str], Optional[float]]:
        if not h:
            return None, None
        kv = {str(k).lower(): v for k, v in h.items()}
        for key in ("access_token", "access-token", "token", "approval_key", "approval-key"):
            if key in kv and str(kv[key]).strip():
                return str(kv[key]).strip(), None
        return None, None

    def _find_token_and_exp(obj: Any) -> Tuple[Optional[str], Optional[float]]:
        token_keys = {"access_token", "token", "approval_key", "ACCESS_TOKEN", "TOKEN", "APPROVAL_KEY"}
        exp = None
        tok = None
        def _walk(x: Any):
            nonlocal tok, exp
            if tok and exp is not None:
                return
            if isinstance(x, dict):
                for k, v in x.items():
                    ks = str(k)
                    if ks in token_keys or ks.lower() in {"access_token", "token", "approval_key"}:
                        if isinstance(v, str) and v.strip():
                            tok = v.strip()
                    if ks.lower() in {"expires_in", "expire_in"}:
                        try:
                            exp = _normalize_epoch_seconds(_now_ts() + float(v))
                        except Exception:
                            pass
                    elif ks.lower() in {"expires_at", "expires_dt", "expire_at"}:
                        ts = _parse_expires_from_str(str(v))
                        if ts:
                            exp = _normalize_epoch_seconds(ts)
                for v2 in x.values():
                    _walk(v2)
            elif isinstance(x, list):
                for it in x:
                    _walk(it)
        _walk(obj)
        return tok, exp

    def _parse_response(resp_json: Dict[str, Any]) -> Tuple[Optional[str], Optional[float], Optional[str]]:
        tok, exp = _find_token_and_exp(resp_json)
        rc = str(resp_json.get("return_code") or "").strip()
        rm = str(resp_json.get("return_msg") or "").strip()
        err = None
        if rc and rc != "0":
            err = f"Kiwoom error {rc}: {rm or 'Unknown error'}"
        return tok, (exp or _parse_expires_from_response(resp_json)), err

    scenarios = [
        # 1) JSON (appsecret)
        ("json(appsecret)", "json", {"grant_type": "client_credentials", "appkey": appkey, "appsecret": secretkey}),
        # 2) JSON (secretkey)
        ("json(secretkey)", "json", {"grant_type": "client_credentials", "appkey": appkey, "secretkey": secretkey}),
        # 3) FORM (appsecret)
        ("form(appsecret)", "form", {"grant_type": "client_credentials", "appkey": appkey, "appsecret": secretkey}),
        # 4) FORM (secretkey)
        ("form(secretkey)", "form", {"grant_type": "client_credentials", "appkey": appkey, "secretkey": secretkey}),
    ]

    last_exc: Optional[Exception] = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        for label, mode, payload in scenarios:
            try:
                if mode == "json":
                    resp = requests.post(token_url, headers=headers_json, json=payload, timeout=REQUEST_TIMEOUT_SEC)
                else:
                    resp = requests.post(token_url, headers=headers_form, data=payload, timeout=REQUEST_TIMEOUT_SEC)

                if resp.status_code // 100 != 2:
                    raise RuntimeError(f"HTTP {resp.status_code} on {label}: {resp.text[:200]}")

                tok_h, exp_h = _find_token_and_exp_in_headers(resp.headers)
                if tok_h:
                    return tok_h, _normalize_epoch_seconds(float(exp_h or (_now_ts() + 24*3600 - 60)))

                data: Dict[str, Any] = {}
                try:
                    data = resp.json() if resp.content else {}
                except Exception:
                    data = {"raw": resp.text}

                token, exp, kiwoom_err = _parse_response(data)

                if (not token) and isinstance(data, dict):
                    for key in ("output", "result", "body", "response", "data"):
                        sub = data.get(key)
                        if isinstance(sub, dict):
                            token, exp2 = _find_token_and_exp(sub)
                            if token:
                                exp = exp or exp2
                                break

                if kiwoom_err:
                    raise RuntimeError(f"{kiwoom_err} [{label}]")

                if token:
                    return token, _normalize_epoch_seconds(float(exp or (_now_ts() + 24*3600 - 60)))

                raise KeyError(f"Token not found in response [{label}] keys={list(data.keys())}")
            except Exception as e:
                last_exc = e
                logger.debug("Token attempt failed (%s, try=%d/%d): %s", label, attempt, REQUEST_RETRIES, e)
        if attempt < REQUEST_RETRIES:
            time.sleep(RETRY_BACKOFF_SEC * attempt)

    raise RuntimeError(f"Token request failed after {REQUEST_RETRIES} retries: {last_exc}")

# ────────────────────────────────────────────────────────────────────
# 표준 멀티프로필 캐시 (메인)
# ────────────────────────────────────────────────────────────────────
def _read_token_file(path: Path) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    if not path.exists():
        return None, None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tok = (data.get("access_token") or "") or None
        exp = data.get("expires_at")
        acc = (data.get("account_id") or "") or None
        if tok and exp is not None:
            return str(tok), float(exp), acc
    except Exception:
        pass
    return None, None, None

def _write_token_file(path: Path, *, token: str, expires_at: float,
                      cache_namespace: str, account_id: Optional[str], app_key: str) -> None:
    cache_id = _cache_id_for(app_key, cache_namespace, account_id)
    payload = {
        "access_token": token,
        "expires_at": float(_normalize_epoch_seconds(expires_at)),
        "expires_dt": _ts_to_str(expires_at),
        "cache_id": cache_id,
        "namespace": cache_namespace,
        "account_id": (account_id or ""),
    }
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def rebuild_kiwoom_accounts_env(*, write_dotenv: bool = True) -> List[Dict[str, Any]]:
    """
    .cache/*.json을 스캔해 KIWOOM_ACCOUNTS_JSON을 다시 구성하고
    os.environ 및 (옵션) .env에 **덮어쓰기**로 반영. (누적/병합 없음)
    """
    # 프로필에서 alias 우선
    profs = _load_profiles_file()
    alias_by_acc: Dict[str, str] = {}
    for p in profs.values():
        acc = p.get("account_id") or ""
        if acc:
            alias_by_acc[acc] = p.get("nickname") or acc

    accs: List[Dict[str, Any]] = []
    for p in TOKENS_DIR.glob("*.json"):
        # 불필요한 메타/프로필 파일 제외
        if p.name == "kiwoom_profiles.json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            tok = (data.get("access_token") or "").strip()
            if not tok:
                continue
            acc_no = (data.get("account_id") or "").strip()
            accs.append({
                "token": tok,
                "acc_no": acc_no or "unknown",
                "enabled": True,
                "alias": alias_by_acc.get(acc_no, acc_no or "unknown"),
            })
        except Exception:
            continue

    payload = json.dumps(accs, ensure_ascii=False)
    os.environ["KIWOOM_ACCOUNTS_JSON"] = payload
    if write_dotenv:
        update_env_variable("KIWOOM_ACCOUNTS_JSON", payload)
    logger.info("✅ KIWOOM_ACCOUNTS_JSON updated (count=%d)", len(accs))
    return accs

def get_access_token_cached(
    app_key: str,
    app_secret: str,
    account_id: str = "",
    *,
    cache_namespace: str = "kiwoom-prod",
    token_url: str = DEFAULT_TOKEN_URL,
    update_env: bool = True,
) -> str:
    """
    표준 멀티프로필 캐시 진입점.
    - 파일명: .cache/{namespace}-{acc_or_na}-{hash16}.json
    - 구조:  {access_token, expires_at, expires_dt, cache_id, namespace, account_id}
    - 만료 60초 전 재발급.
    """
    ak = (app_key or "").strip()
    sk = (app_secret or "").strip()
    if not ak or not sk:
        raise ValueError("app_key/app_secret is required (non-empty)")

    json_path, lock_path = _paths_for_namespace_id(cache_namespace, account_id or None, ak)
    now = _now_ts()

    tok, exp, _ = _read_token_file(json_path)
    if tok and exp and (exp - now > TOKEN_EXP_MARGIN):
        return tok

    with _FileLock(lock_path):
        tok, exp, _ = _read_token_file(json_path)
        if tok and exp and (exp - now > TOKEN_EXP_MARGIN):
            return tok
        new_tok, new_exp = _request_new_token(ak, sk, token_url=token_url)
        new_exp = _normalize_epoch_seconds(new_exp)
        _write_token_file(json_path, token=new_tok, expires_at=new_exp,
                          cache_namespace=cache_namespace, account_id=(account_id or ""), app_key=ak)

    if update_env:
        rebuild_kiwoom_accounts_env(write_dotenv=True)

    tok, _, _ = _read_token_file(json_path)
    if not tok:
        raise RuntimeError("Token cache write failed unexpectedly.")
    return tok

def request_new_token(appkey: Optional[str] = None, appsecret: Optional[str] = None,
                      token_url: str = DEFAULT_TOKEN_URL) -> str:
    ak = (appkey or "").strip()
    sk = (appsecret or "").strip()
    if not ak or not sk:
        _ak, _sk = load_keys()
        ak = ak or _ak
        sk = sk or _sk
    tok, exp = _request_new_token(ak, sk, token_url=token_url)
    json_path, lock_path = _paths_for_namespace_id("kiwoom-prod", None, ak)
    with _FileLock(lock_path):
        _write_token_file(json_path, token=tok, expires_at=_normalize_epoch_seconds(exp),
                          cache_namespace="kiwoom-prod", account_id="", app_key=ak)
    try:
        rebuild_kiwoom_accounts_env(write_dotenv=True)
    except Exception:
        pass
    return tok

def request_new_token_for_profile(*, account_id: str, app_key: str, app_secret: str,
                                  cache_namespace: str = "kiwoom-prod",
                                  token_url: str = DEFAULT_TOKEN_URL) -> str:
    """
    세팅매니저에서 특정 프로필(account_id, app_key, app_secret)로 토큰 발급 후
    해당 파일을 저장(계정 식별 포함)하고, KIWOOM_ACCOUNTS_JSON을 최신 상태로 덮어쓴다.
    """
    tok, exp = _request_new_token(app_key, app_secret, token_url=token_url)
    json_path, lock_path = _paths_for_namespace_id(cache_namespace, account_id or None, app_key)
    with _FileLock(lock_path):
        _write_token_file(json_path, token=tok, expires_at=_normalize_epoch_seconds(exp),
                          cache_namespace=cache_namespace, account_id=(account_id or ""), app_key=app_key)
    rebuild_kiwoom_accounts_env(write_dotenv=True)
    return tok

# ────────────────────────────────────────────────────────────────────
# 멀티계좌(프로필) 관리
# ────────────────────────────────────────────────────────────────────
class KiwoomProfile(TypedDict, total=False):
    profile_id: str
    nickname: str          # UI 표시명
    account_id: str        # 표시/요청 전달용
    app_key: str
    app_secret: str
    is_main: bool
    enabled: bool

_prof_mem: Dict[str, KiwoomProfile] = {}
_prof_lock = threading.Lock()

def _load_profiles_file() -> Dict[str, KiwoomProfile]:
    if not _PROFILES_FILE.exists():
        return {}
    try:
        data = json.loads(_PROFILES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_profiles_file(data: Dict[str, KiwoomProfile]) -> None:
    _atomic_write_text(_PROFILES_FILE, json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _ensure_profiles_loaded() -> None:
    with _prof_lock:
        if _prof_mem:
            return
        _prof_mem.update(_load_profiles_file())

def upsert_profile(p: KiwoomProfile) -> str:
    if not p.get("account_id") or not p.get("app_key") or not p.get("app_secret"):
        raise ValueError("account_id, app_key, app_secret는 필수입니다.")
    _ensure_profiles_loaded()
    with _prof_lock:
        pid = p.get("profile_id") or p.get("account_id") or f"profile-{int(_now_ts()*1000)}"
        cur = _prof_mem.get(pid, {})
        cur.update(p)
        cur.setdefault("nickname", cur.get("account_id", pid))
        cur.setdefault("enabled", True)
        cur.setdefault("is_main", False)
        cur["profile_id"] = pid
        _prof_mem[pid] = cur

        if cur.get("is_main"):
            for opid, op in _prof_mem.items():
                if opid != pid and op.get("is_main"):
                    op["is_main"] = False

        _save_profiles_file(_prof_mem)
        return pid

def delete_profile(profile_id: str) -> None:
    _ensure_profiles_loaded()
    with _prof_lock:
        if profile_id in _prof_mem:
            _prof_mem.pop(profile_id)
            _save_profiles_file(_prof_mem)

def list_profiles() -> List[KiwoomProfile]:
    _ensure_profiles_loaded()
    with _prof_lock:
        return list(_prof_mem.values())

def set_main_profile(profile_id: str) -> None:
    _ensure_profiles_loaded()
    with _prof_lock:
        if profile_id not in _prof_mem:
            raise KeyError(f"Unknown profile_id: {profile_id}")
        for pid in list(_prof_mem.keys()):
            _prof_mem[pid]["is_main"] = (pid == profile_id)
        _save_profiles_file(_prof_mem)

def set_profile_enabled(profile_id: str, enabled: bool) -> None:
    _ensure_profiles_loaded()
    with _prof_lock:
        if profile_id in _prof_mem:
            _prof_mem[profile_id]["enabled"] = bool(enabled)
            _save_profiles_file(_prof_mem)

def _find_main_profile() -> Optional[KiwoomProfile]:
    _ensure_profiles_loaded()
    with _prof_lock:
        for p in _prof_mem.values():
            if p.get("is_main"):
                return p
    return None

def main_account_id() -> Optional[str]:
    mp = _find_main_profile()
    return mp.get("account_id") if mp else None

def active_account_ids() -> List[str]:
    _ensure_profiles_loaded()
    with _prof_lock:
        return [p["account_id"] for p in _prof_mem.values() if p.get("enabled")]

# ────────────────────────────────────────────────────────────────────
# 주문(브로드캐스트) 컨텍스트
# ────────────────────────────────────────────────────────────────────
def list_order_accounts_strict() -> List[Dict[str, Any]]:
    """
    주문 시 **반드시** ENV(KIWOOM_ACCOUNTS_JSON)만 사용하여 계정 리스트를 반환.
    - 누적 금지/병합 금지(요구사항 #2, #4)
    구조: [{"token":str, "acc_no":str|None, "enabled":bool, "alias":str|None}, ...]
    """
    raw = os.getenv("KIWOOM_ACCOUNTS_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        out: List[Dict[str, Any]] = []
        for x in data:
            if not isinstance(x, dict):
                continue
            tok = (x.get("token") or "").strip()
            if not tok:
                continue
            acc_no = (x.get("acc_no") or "").strip()
            alias = (x.get("alias") or "").strip() or (acc_no or "unknown")
            out.append({
                "token": tok,
                "acc_no": acc_no or None,
                "enabled": bool(x.get("enabled", True)),
                "alias": alias,
            })
        return out
    except Exception:
        logger.warning("Failed to parse KIWOOM_ACCOUNTS_JSON")
        return []

def build_account_provider_from_env_or_cache() -> Callable[[], List[Dict[str, Any]]]:
    """
    (참고) 기존 브로커 호환 팩토리.
    1) ENV(KIWOOM_ACCOUNTS_JSON)이 있으면 우선 사용
    2) 없으면 .cache/*.json 스캔 → ENV도 채워줌
    """
    def _from_env() -> List[Dict[str, Any]]:
        return list_order_accounts_strict()

    def _from_cache() -> List[Dict[str, Any]]:
        res: List[Dict[str, Any]] = []
        profs = _load_profiles_file()
        alias_by_acc: Dict[str, str] = {}
        for p in profs.values():
            acc = p.get("account_id") or ""
            if acc:
                alias_by_acc[acc] = p.get("nickname") or acc
        for p in TOKENS_DIR.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                tok = (data.get("access_token") or "").strip()
                if not tok:
                    continue
                acc_no = data.get("account_id") or None
                res.append({
                    "token": tok,
                    "acc_no": acc_no,
                    "enabled": True,
                    "alias": alias_by_acc.get(acc_no or "", acc_no),
                })
            except Exception:
                continue
        return res

    def _provider() -> List[Dict[str, Any]]:
        lst = _from_env()
        if lst:
            return lst
        lst = _from_cache()
        if lst:
            try:
                update_env_variable("KIWOOM_ACCOUNTS_JSON", json.dumps(lst, ensure_ascii=False))
            except Exception:
                pass
        return lst

    return _provider

# ────────────────────────────────────────────────────────────────────
# 전역(싱글톤) 토큰 공급자 & 메인 토큰
# ────────────────────────────────────────────────────────────────────
_GLOBAL_TOKEN_SUPPLIER: Optional[Callable[[], str]] = None
_SUPPLIER_LOCK = threading.Lock()

def build_token_supplier(
    *,
    app_key: Optional[str] = None,
    app_secret: Optional[str] = None,
    account_id: str = "",
    cache_namespace: str = "kiwoom-prod",
    token_url: str = DEFAULT_TOKEN_URL,
) -> Callable[[], str]:
    ak = (app_key or "").strip()
    sk = (app_secret or "").strip()

    if not ak or not sk:
        try:
            mp = _find_main_profile()
        except Exception:
            mp = None
        if mp and not ak and not sk:
            ak, sk = mp.get("app_key", ""), mp.get("app_secret", "")
            if not account_id:
                account_id = mp.get("account_id", "") or ""
        if (not ak or not sk):
            _ak, _sk = load_keys()
            ak = ak or _ak
            sk = sk or _sk

    if not ak or not sk:
        raise RuntimeError("build_token_supplier: APP_KEY/APP_SECRET을 찾을 수 없습니다 (.env 또는 프로필 확인).")

    def _supplier() -> str:
        return get_access_token_cached(
            app_key=ak,
            app_secret=sk,
            account_id=account_id,
            cache_namespace=cache_namespace,
            token_url=token_url,
            update_env=True,
        )

    return _supplier

def set_global_token_supplier(supplier: Callable[[], str]) -> None:
    global _GLOBAL_TOKEN_SUPPLIER
    with _SUPPLIER_LOCK:
        _GLOBAL_TOKEN_SUPPLIER = supplier

def reset_global_token_supplier() -> None:
    global _GLOBAL_TOKEN_SUPPLIER
    with _SUPPLIER_LOCK:
        _GLOBAL_TOKEN_SUPPLIER = None

def get_main_token() -> str:
    """
    요구사항 #1: 메인 계좌 토큰은 .env의 APP_KEY_1 / APP_SECRET_1을 사용
    (프로필이 있더라도 '메인'은 .env(1번) 우선)
    """
    ak1 = (os.getenv("APP_KEY_1") or "").strip()
    sk1 = (os.getenv("APP_SECRET_1") or "").strip()
    if not ak1 or not sk1:
        ak1, sk1 = load_keys()
    if not ak1 or not sk1:
        raise RuntimeError("get_main_token: .env의 APP_KEY_1 / APP_SECRET_1 자격이 없습니다.")
    return get_access_token_cached(app_key=ak1, app_secret=sk1, account_id="", cache_namespace="kiwoom-prod")

def get_token() -> str:
    """
    기존 전역 제공과 호환: 전역 supplier → 프로필 메인 → .env(1번)
    """
    with _SUPPLIER_LOCK:
        supplier = _GLOBAL_TOKEN_SUPPLIER
    if callable(supplier):
        return supplier()

    try:
        mp = _find_main_profile()
    except Exception:
        mp = None
    if mp:
        return get_access_token_cached(
            app_key=mp["app_key"],
            app_secret=mp["app_secret"],
            account_id=mp.get("account_id", "") or "",
            cache_namespace="kiwoom-prod",
        )
    # 마지막으로 .env (요구사항 1 충족)
    return get_main_token()

def force_refresh_token(
    *,
    app_key: Optional[str] = None,
    app_secret: Optional[str] = None,
    account_id: str = "",
    cache_namespace: str = "kiwoom-prod",
    token_url: str = DEFAULT_TOKEN_URL,
) -> str:
    ak = (app_key or "").strip()
    sk = (app_secret or "").strip()
    if not ak or not sk:
        with _SUPPLIER_LOCK:
            supplier = _GLOBAL_TOKEN_SUPPLIER
        if callable(supplier):
            raise RuntimeError("force_refresh_token: app_key/app_secret을 명시해 주세요 (전역공급자 경로는 미지원).")
        try:
            mp = _find_main_profile()
        except Exception:
            mp = None
        if mp and not ak and not sk:
            ak, sk = mp.get("app_key", ""), mp.get("app_secret", "")
            if not account_id:
                account_id = mp.get("account_id", "") or ""
        if (not ak or not sk):
            _ak, _sk = load_keys()
            ak = ak or _ak
            sk = sk or _sk
    if not ak or not sk:
        raise RuntimeError("force_refresh_token: APP_KEY/APP_SECRET이 필요합니다.")

    tok, exp = _request_new_token(ak, sk, token_url=token_url)
    json_path, lock_path = _paths_for_namespace_id(cache_namespace, account_id or None, ak)
    with _FileLock(lock_path):
        _write_token_file(
            json_path,
            token=tok,
            expires_at=_normalize_epoch_seconds(exp),
            cache_namespace=cache_namespace,
            account_id=(account_id or ""),
            app_key=ak,
        )
    rebuild_kiwoom_accounts_env(write_dotenv=True)
    return tok

# ────────────────────────────────────────────────────────────────────
# 세팅매니저 연동: 최신 리스트로만 ENV 적용
# ────────────────────────────────────────────────────────────────────
def settings_apply_token_list(accounts: List[Dict[str, Any]], *, write_dotenv: bool = True) -> None:
    """
    요구사항 #4: 세팅매니저에서 토큰 생성 직후,
    전달받은 accounts 리스트만으로 KIWOOM_ACCOUNTS_JSON을 **덮어쓰기** 적용.
    accounts 원소 예:
      {"token": "...", "acc_no": "12345678", "enabled": true, "alias": "주계좌"}
    """
    sanitized: List[Dict[str, Any]] = []
    for x in accounts:
        if not isinstance(x, dict):
            continue
        tok = (x.get("token") or "").strip()
        if not tok:
            continue
        sanitized.append({
            "token": tok,
            "acc_no": (x.get("acc_no") or "") or "unknown",
            "enabled": bool(x.get("enabled", True)),
            "alias": x.get("alias") or (x.get("acc_no") or "unknown"),
        })
    payload = json.dumps(sanitized, ensure_ascii=False)
    os.environ["KIWOOM_ACCOUNTS_JSON"] = payload
    if write_dotenv:
        update_env_variable("KIWOOM_ACCOUNTS_JSON", payload)
    logger.info("🔄 KIWOOM_ACCOUNTS_JSON replaced by settings (count=%d)", len(sanitized))

def mint_tokens_from_settings_manager(
    profiles: List[Dict[str, Any]],
    *,
    cache_namespace: str = "kiwoom-prod",
    token_url: str = DEFAULT_TOKEN_URL,
    write_dotenv: bool = True,
) -> List[Dict[str, Any]]:
    """
    세팅매니저에서 전달한 프로필 리스트로 일괄 토큰 발급 후,
    **누적 없이 최신 리스트로만** KIWOOM_ACCOUNTS_JSON을 덮어씀.
    profiles 원소 예:
      {"account_id":"12345678","app_key":"...","app_secret":"...","enabled":true,"alias":"별칭"}
    """
    out_accounts: List[Dict[str, Any]] = []
    for p in profiles:
        acc = (p.get("account_id") or "").strip()
        ak = (p.get("app_key") or "").strip()
        sk = (p.get("app_secret") or "").strip()
        if not acc or not ak or not sk:
            logger.warning("skip profile (missing fields): %s", p)
            continue
        tok, exp = _request_new_token(ak, sk, token_url=token_url)
        json_path, lock_path = _paths_for_namespace_id(cache_namespace, acc or None, ak)
        with _FileLock(lock_path):
            _write_token_file(json_path, token=tok, expires_at=_normalize_epoch_seconds(exp),
                              cache_namespace=cache_namespace, account_id=acc, app_key=ak)
        out_accounts.append({
            "token": tok,
            "acc_no": acc,
            "enabled": bool(p.get("enabled", True)),
            "alias": p.get("alias") or acc,
        })

    # 최신 리스트만 반영
    settings_apply_token_list(out_accounts, write_dotenv=write_dotenv)
    return out_accounts
