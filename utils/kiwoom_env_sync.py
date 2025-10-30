# utils/kiwoom_env_sync.py
from __future__ import annotations
import os, json
from pathlib import Path
from typing import List, Dict, Any

from setting.settings_manager import KiwoomStore
from utils.token_manager import get_access_token_cached

def rebuild_kiwoom_accounts_env(*, cache_ns: str = "kiwoom-prod", write_dotenv: bool = True) -> List[Dict[str, Any]]:
    """
    Settings ▶ '키움 계좌 관리' 프로필을 모두 읽어,
    각 프로필의 App Key/Secret로 토큰(캐시)을 확보한 뒤
    KIWOOM_ACCOUNTS_JSON을 재구성한다.
    """
    ks = KiwoomStore()
    cfg = ks.load()

    accounts: List[Dict[str, Any]] = []
    for p in cfg.profiles:
        if not p.enabled:
            continue
        app_key = (p.app_key or "").strip()
        app_sec = (p.app_secret or "").strip()
        acc_no  = (p.account_id or "").strip()
        alias   = (p.alias or acc_no or "").strip()

        # AppKey/Secret 없으면 스킵
        if not (app_key and app_sec):
            continue

        # 계좌번호가 비어있어도 일단 토큰은 발급 가능하나,
        # 브로드캐스트/표시에 유용하니 가급적 채워두는 걸 권장
        token = get_access_token_cached(
            app_key=app_key,
            app_secret=app_sec,
            account_id=(acc_no or None),          # 계정별 캐시 분리
            cache_namespace=cache_ns,
            update_env=False,                      # 여기서는 일괄 구성 후 한 번에 ENV 반영
        )

        accounts.append({
            "token": token,
            "acc_no": acc_no or None,
            "enabled": True,
            "alias": alias or None,
        })

    # 1) 프로세스 ENV 업데이트
    js = json.dumps(accounts, ensure_ascii=False)
    os.environ["KIWOOM_ACCOUNTS_JSON"] = js

    # 2) .env에 영구 반영
    if write_dotenv:
        _write_env_line("KIWOOM_ACCOUNTS_JSON", js)

    return accounts

def _write_env_line(key: str, value: str, path: str | Path = ".env") -> None:
    path = Path(path)
    lines: List[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    found = False
    for i, ln in enumerate(lines):
        if ln.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
