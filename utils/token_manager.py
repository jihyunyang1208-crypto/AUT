# utils/token_manager.py
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Tuple, TypedDict

import requests

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
# 상수 (하위호환 유지)
# ────────────────────────────────────────────────────────────────────
APP_NAME = "AutoTrader"
DEFAULT_TOKEN_URL = "https://api.kiwoom.com/oauth2/token"
REQUEST_TIMEOUT_SEC = 8
REQUEST_RETRIES = 3
RETRY_BACKOFF_SEC = 1.2
EXPIRY_SAFETY_MARGIN_SEC = 60  # 만료 60초 전부터 재발급
TOKEN_EXP_MARGIN = 60

# 구버전 단일 토큰 캐시 경로(하위호환)
CACHE_DIR = Path(os.getcwd()) / ".cache"
CACHE_FILE = CACHE_DIR / "token_cache.json"
LOCK_FILE = CACHE_DIR / "token_cache.lock"

def _config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.getenv("APPDATA", Path.home() / "AppData/Roaming"))
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d

CONFIG_DIR = _config_dir()
ENV_FILE = CONFIG_DIR / ".env"
NEW_TOKEN_FILE = CONFIG_DIR / "access_token.json"  # 신형 단일 토큰(하위호환)

# 프로젝트 루트 기준 멀티프로필 토큰 디렉토리
PROJECT_ROOT = Path.cwd()
TOKENS_DIR = PROJECT_ROOT / ".cache"
TOKENS_DIR.mkdir(parents=True, exist_ok=True)

# 프로필 저장 경로
_PROFILES_FILE = CONFIG_DIR / "kiwoom_profiles.json"

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

def _ensure_cache_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

class _FileLock:
    def __init__(self, path: Path, timeout: float = 5.0, poll: float = 0.05):
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

def _paths_for_cache_id(cache_id: str) -> Tuple[Path, Path]:
    fname = _safe_key(cache_id) + ".json"
    return (TOKENS_DIR / fname, TOKENS_DIR / (fname + ".lock"))

def _cache_id_for(app_key: str, cache_namespace: str, account_id: Optional[str]) -> str:
    fp = _fingerprint_key(cache_namespace, account_id or "", app_key)
    safe_acc = _safe_key(account_id) if account_id else "na"
    return f"{cache_namespace}-{safe_acc}-{fp}"

def _paths_for_namespace_id(cache_namespace: str, account_id: Optional[str], app_key: str) -> Tuple[Path, Path]:
    cache_id = _cache_id_for(app_key, cache_namespace, account_id)
    return _paths_for_cache_id(cache_id)

# ────────────────────────────────────────────────────────────────────
# .env 키 저장/로드 (하위호환)
# ────────────────────────────────────────────────────────────────────
def load_keys() -> Tuple[str, str]:
    """우선순위: ENV(.env 포함) > 프로세스 환경변수"""
    appkey = os.getenv("APP_KEY", "")
    appsecret = os.getenv("APP_SECRET", "")
    if ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                if line.startswith("APP_KEY="):
                    appkey = line.split("=", 1)[1].strip().strip('"')
                if line.startswith("APP_SECRET="):
                    appsecret = line.split("=", 1)[1].strip().strip('"')
        except Exception:
            pass
    return appkey, appsecret

def set_keys(appkey: str, appsecret: str) -> None:
    ENV_FILE.write_text(f'APP_KEY="{appkey}"\nAPP_SECRET="{appsecret}"\n', encoding="utf-8")

# ────────────────────────────────────────────────────────────────────
# 만료 파싱 유틸
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

# ────────────────────────────────────────────────────────────────────
# 레거시 단일 토큰 메모리 캐시 (하위호환)
# ────────────────────────────────────────────────────────────────────
_mem_token: Optional[str] = None
_mem_expires_at: Optional[float] = None
_mem_lock = threading.Lock()

def _load_file_cache() -> Tuple[Optional[str], Optional[float]]:
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
    if NEW_TOKEN_FILE.exists():
        try:
            data = json.loads(NEW_TOKEN_FILE.read_text(encoding="utf-8"))
            token = data.get("token") or data.get("access_token")
            exp = data.get("expires_at") or _parse_expires_from_response(data)
            exp = _normalize_epoch_seconds(float(exp))
            if token:
                return token, exp
        except Exception:
            pass
    return None, None

def _save_file_cache(token: str, expires_at: float) -> None:
    _ensure_cache_dir()
    expires_at = _normalize_epoch_seconds(expires_at)
    payload = {"token": token, "expires_at": expires_at, "expires_dt": _ts_to_str(expires_at)}
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    NEW_TOKEN_FILE.write_text(json.dumps({"access_token": token, "expires_at": expires_at}, ensure_ascii=False, indent=2), encoding="utf-8")

# ────────────────────────────────────────────────────────────────────
# HTTP 발급
# ────────────────────────────────────────────────────────────────────
def _request_new_token(appkey: str, secretkey: str, token_url: str = DEFAULT_TOKEN_URL) -> Tuple[str, float]:
    """
    키움/게이트웨이 호환 토큰 발급 (JSON only)
    - Content-Type/Accept: application/json
    - payload 키 변형 지원: (appkey, appsecret) 또는 (appkey, secretkey)
    - 응답 스키마 지원:
        1) {"access_token": "...", "expires_in": 86400}
        2) {"output": {"access_token": "...", "expires_in": 86400}}
        3) {"result": {"access_token": "...", "expires_in": 86400}}
        4) 오류: {"return_code": "...", "return_msg": "..."} (또는 동등)
    """
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json",
    }

    def _parse(resp_json: Dict[str, Any]) -> Tuple[Optional[str], Optional[float], Optional[str]]:
        # 토큰 추출 (평문 / output / result)
        token = (
            resp_json.get("access_token")
            or (resp_json.get("output") or {}).get("access_token")
            or (resp_json.get("result") or {}).get("access_token")
        )

        # 만료 추출
        exp = _parse_expires_from_response(resp_json) if token else None

        # 키움 표준 오류 추출
        rc = str(resp_json.get("return_code") or "").strip()
        rm = str(resp_json.get("return_msg") or "").strip()
        err = None
        if rc and rc != "0":
            err = f"Kiwoom error {rc}: {rm or 'Unknown error'}"
        elif (not token) and (rc or rm):
            err = rm or "Token not present in response"

        return token, exp, err

    # JSON only 시나리오(게이트웨이에 따라 appsecret/secretkey 호환)
    scenarios = [
        {"data": {"grant_type": "client_credentials", "appkey": appkey, "appsecret": secretkey}, "label": "json(appsecret)"},
        {"data": {"grant_type": "client_credentials", "appkey": appkey, "secretkey": secretkey}, "label": "json(secretkey)"},
    ]

    last_exc: Optional[Exception] = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        for sc in scenarios:
            try:
                resp = requests.post(token_url, headers=headers, json=sc["data"], timeout=REQUEST_TIMEOUT_SEC)

                if resp.status_code == 415:
                    # 미디어 타입 미지원 → 이 엔드포인트는 JSON만 허용하므로 다른 포맷은 시도하지 않음
                    raise RuntimeError(f"HTTP 415 UNSUPPORTED_MEDIA_TYPE on {sc['label']}: {resp.text[:200]}")

                if resp.status_code // 100 != 2:
                    raise RuntimeError(f"HTTP {resp.status_code} on {sc['label']}: {resp.text[:200]}")

                data = resp.json() if resp.content else {}
                token, exp, kiwoom_err = _parse(data)

                if kiwoom_err:
                    raise RuntimeError(f"{kiwoom_err} [{sc['label']}]")

                if token:
                    expires_at = float(exp or (_now_ts() + 24 * 3600 - 60))
                    return token, _normalize_epoch_seconds(expires_at)

                # 200인데 토큰 키가 없음 → 스키마 미일치
                raise KeyError(f"Token not found in response keys {list(data.keys())} [{sc['label']}]")

            except Exception as e:
                last_exc = e
                logger.debug("Token attempt failed (%s, try=%d/%d): %s", sc["label"], attempt, REQUEST_RETRIES, e)

        if attempt < REQUEST_RETRIES:
            time.sleep(RETRY_BACKOFF_SEC * attempt)

    raise RuntimeError(f"Token request failed after {REQUEST_RETRIES} retries: {last_exc}")

# ────────────────────────────────────────────────────────────────────
# 표준 멀티프로필 캐시 (메인)
# ────────────────────────────────────────────────────────────────────
def _read_token_file(path: Path) -> Tuple[Optional[str], Optional[float]]:
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tok = data.get("access_token")
        exp = data.get("expires_at")
        if tok and exp is not None:
            return str(tok), float(exp)
    except Exception:
        pass
    return None, None

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

def update_env_variable(key: str, value: str, env_path: Optional[str] = None) -> Path:
    """dotenv 호환 .env 업데이트 (없으면 생성)"""
    if env_path:
        resolved = Path(env_path).expanduser().resolve()
    else:
        try:
            from dotenv import find_dotenv  # type: ignore
            found = find_dotenv(usecwd=True)
            resolved = Path(found).resolve() if found else (Path.cwd() / ".env")
        except Exception:
            resolved = Path.cwd() / ".env"

    if not resolved.exists():
        resolved.touch()

    try:
        from dotenv import set_key  # type: ignore
        set_key(str(resolved), key, value)
    except Exception:
        try:
            lines = resolved.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = resolved.read_text(encoding="utf-8-sig").splitlines()
        key_prefix = f"{key}="
        new_lines = [line for line in lines if not line.strip().startswith(key_prefix)]
        safe_value = value.replace("\n", "\\n")
        new_lines.append(f'{key}="{safe_value}"')
        _atomic_write_text(resolved, "\n".join(new_lines) + "\n", encoding="utf-8")

    os.environ[key] = value
    return resolved

def _update_KIWOOM_ACCOUNTS_JSON_from_cache_dir() -> int:
    """ .cache/*.json → KIWOOM_ACCOUNTS_JSON 재구성 """
    accs: List[Dict[str, Any]] = []
    for p in TOKENS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            tok = data.get("access_token")
            if not tok:
                continue
            accs.append({
                "token": tok,
                "acc_no": data.get("account_id") or "unknown",
                "enabled": True,
                "alias": data.get("account_id") or "unknown",
            })
        except Exception:
            continue
    update_env_variable("KIWOOM_ACCOUNTS_JSON", json.dumps(accs, ensure_ascii=False))
    logger.info("✅ KIWOOM_ACCOUNTS_JSON updated (count=%d)", len(accs))
    return len(accs)

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

    # 1) 빠른 경로
    tok, exp = _read_token_file(json_path)
    if tok and exp and (exp - now > TOKEN_EXP_MARGIN):
        return tok

    # 2) 락 잡고 더블 체크 → 재발급
    with _FileLock(lock_path):
        tok, exp = _read_token_file(json_path)
        if tok and exp and (exp - now > TOKEN_EXP_MARGIN):
            return tok
        new_tok, new_exp = _request_new_token(ak, sk, token_url=token_url)
        new_exp = _normalize_epoch_seconds(new_exp)
        _write_token_file(json_path, token=new_tok, expires_at=new_exp,
                          cache_namespace=cache_namespace, account_id=(account_id or ""), app_key=ak)

    if update_env:
        _update_KIWOOM_ACCOUNTS_JSON_from_cache_dir()

    tok, _ = _read_token_file(json_path)
    if not tok:
        raise RuntimeError("Token cache write failed unexpectedly.")
    return tok

# ────────────────────────────────────────────────────────────────────
# 하위호환 API: 내부적으로 표준 캐시로 위임
# ────────────────────────────────────────────────────────────────────
def set_access_token(token: str, ttl_seconds: Optional[int] = None) -> None:
    """레거시 단일 토큰 수동 주입(하위호환)"""
    global _mem_token, _mem_expires_at
    if not token:
        return
    with _mem_lock:
        _mem_token = token
        _mem_expires_at = _now_ts() + (ttl_seconds if ttl_seconds else 55 * 60)
        with _FileLock(LOCK_FILE):
            _save_file_cache(_mem_token, _mem_expires_at)

def clear_access_token_cache() -> None:
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
    """레거시 단일 토큰 조회(하위호환)"""
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

def get_access_token(appkey: str, secretkey: str, token_url: str = DEFAULT_TOKEN_URL) -> str:
    """레거시 단일 토큰 API → 표준 캐시로 위임(na, kiwoom-prod)"""
    return get_access_token_cached(app_key=appkey, app_secret=secretkey,
                                   account_id="", cache_namespace="kiwoom-prod",
                                   token_url=token_url, update_env=False)

def get_token_expiry() -> Optional[datetime]:
    with _mem_lock:
        exp = _mem_expires_at
    if exp:
        return datetime.fromtimestamp(_normalize_epoch_seconds(exp))
    with _FileLock(LOCK_FILE):
        _, exp = _load_file_cache()
    return datetime.fromtimestamp(_normalize_epoch_seconds(exp)) if exp else None

def request_new_token(appkey: Optional[str] = None, appsecret: Optional[str] = None,
                      token_url: str = DEFAULT_TOKEN_URL) -> str:
    """레거시 강제 발급 → 표준 캐시에 기록"""
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
    return tok

# ────────────────────────────────────────────────────────────────────
# 멀티계좌(프로필) 관리
# ────────────────────────────────────────────────────────────────────
class KiwoomProfile(TypedDict, total=False):
    profile_id: str        # 내부 식별자
    nickname: str          # UI 표시명
    account_id: str        # 증권사 계좌번호
    app_key: str
    app_secret: str
    is_main: bool          # 메인 계정
    enabled: bool          # 브로드캐스트 주문 대상 여부

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
    """
    계정 저장/수정. 반환: profile_id
    필수: account_id, app_key, app_secret
    옵션: nickname, enabled, is_main, profile_id
    """
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
# 멀티계좌 토큰 공급자
# ────────────────────────────────────────────────────────────────────
def get_token_for_account(
    appkey: str,
    appsecret: str,
    *,
    token_url: str = DEFAULT_TOKEN_URL,
    cache_namespace: str = "kiwoom-prod",
    account_id: Optional[str] = None,
    update_env: bool = True,
) -> str:
    """공개 API 유지: 내부적으로 표준 캐시 사용"""
    return get_access_token_cached(
        app_key=appkey, app_secret=appsecret, account_id=(account_id or ""),
        cache_namespace=cache_namespace, token_url=token_url, update_env=update_env
    )

def token_provider_for_account_id(account_id: str) -> str:
    """특정 account_id의 프로필을 찾아 토큰 반환"""
    _ensure_profiles_loaded()
    with _prof_lock:
        tgt = None
        for p in _prof_mem.values():
            if p.get("account_id") == account_id:
                tgt = p
                break
    if not tgt:
        raise KeyError(f"Unknown account_id: {account_id}")
    return get_token_for_account(tgt["app_key"], tgt["app_secret"], account_id=account_id)

def token_provider_for_main() -> str:
    """메인 프로필(AppKey/Secret)로 토큰 반환. 메인이 없으면 .env 사용."""
    mp = _find_main_profile()
    if not mp:
        ak, sk = load_keys()
        if not ak or not sk:
            raise RuntimeError("No main profile and no .env APP_KEY/APP_SECRET found.")
        return get_access_token_cached(app_key=ak, app_secret=sk, account_id="", cache_namespace="kiwoom-prod")
    return get_access_token_cached(
        app_key=mp["app_key"], app_secret=mp["app_secret"],
        account_id=mp["account_id"], cache_namespace="kiwoom-prod"
    )
