from __future__ import annotations

import os
import sys
from pathlib import Path

# --- 보안: 키/비밀번호 마스킹 처리 ---
def mask_key(key_value: str | None) -> str:
    """키 값을 안전하게 마스킹하여 출력합니다 (예: "myse..." ...cret")"""
    if not key_value or not isinstance(key_value, str):
        return "(값이 없음)"
    if len(key_value) < 8:
        return f"{key_value[:1]}***{key_value[-1:]}"
    return f"{key_value[:4]}...{key_value[-4:]}"

# --- 프로젝트 모듈 임포트 ---
# 이 스크립트가 main.py와 같은 위치에 있다고 가정합니다.
sys.path.append(os.getcwd())

try:
    from setting.settings_manager import SettingsStore
    from utils.token_manager import load_keys, ENV_FILE as CENTRAL_ENV_FILE
except ImportError as e:
    print(f"--- 🚨 중요 🚨 ---")
    print(f"오류: 필요한 모듈을 찾을 수 없습니다. ({e})")
    print("이 스크립트('check_keys.py')를 'main.py'가 있는")
    print("프로젝트 루트 디렉토리에서 실행해야 합니다.")
    print("-" * 50)
    sys.exit(1)


def read_keys_from_file(path: Path) -> tuple[str, str]:
    """ .env 파일에서 키 값을 직접 읽어옵니다 (테스트용) """
    ak, sk = "", ""
    if not path.exists():
        return ak, sk
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("APP_KEY="):
                ak = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("APP_SECRET="):
                sk = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ak, sk

# --- 1. AppSettings (.json 설정) 값 확인 ---
print("--- 1. 'AppSettings' 객체 (SettingsStore) 테스트 ---")
print(" (UI 설정 화면 등에서 저장된 .json 파일 값)")
try:
    store = SettingsStore()
    app_cfg = store.load()
    key_from_settings = (getattr(app_cfg, "app_key", None) or "").strip()
    secret_from_settings = (getattr(app_cfg, "app_secret", None) or "").strip()
    
    print(f"  [Settings] APP_KEY   : {mask_key(key_from_settings)}")
    print(f"  [Settings] APP_SECRET: {mask_key(secret_from_settings)}")
    if not key_from_settings or not secret_from_settings:
        print("  [결과] 설정 파일(.json)에 키가 없거나 비어있습니다.")
    else:
        print("  [결과] ✅ 설정 파일(.json)에서 키를 로드했습니다.")
except Exception as e:
    print(f"  [오류] SettingsStore 로드 중 오류 발생: {e}")
print("-" * 50)


# --- 2. 프로세스 환경 변수 값 확인 ---
print("--- 2. 프로세스 환경 변수 (os.getenv) 테스트 ---")
print(" (현재 실행 환경에 시스템 변수로 설정된 값)")
try:
    key_from_env = (os.getenv("APP_KEY") or "").strip()
    secret_from_env = (os.getenv("APP_SECRET") or "").strip()
    
    print(f"  [os.getenv] APP_KEY   : {mask_key(key_from_env)}")
    print(f"  [os.getenv] APP_SECRET: {mask_key(secret_from_env)}")
    
    if not key_from_env or not secret_from_env:
        print("  [결과] 시스템 환경 변수에 키가 설정되어 있지 않습니다.")
    else:
        print("  [결과] ✅ 시스템 환경 변수에서 키를 로드했습니다.")
except Exception as e:
    print(f"  [오류] 환경 변수 읽기 중 오류 발생: {e}")
print("-" * 50)


# --- 3. .env 파일 (load_keys 함수) 값 확인 ---
print("--- 3. .env 파일 (파일 직접 읽기) 테스트 ---")
print(" (load_keys() 함수가 참조하는 파일들의 내용)")
try:
    local_env_file = Path.cwd() / ".env"

    print(f"  [경로 1] 중앙 .env: {CENTRAL_ENV_FILE}")
    central_key, central_secret = read_keys_from_file(CENTRAL_ENV_FILE)
    print(f"    -> APP_KEY   : {mask_key(central_key)}")
    print(f"    -> APP_SECRET: {mask_key(central_secret)}")

    print(f"\n  [경로 2] 로컬 .env: {local_env_file}")
    local_key, local_secret = read_keys_from_file(local_env_file)
    print(f"    -> APP_KEY   : {mask_key(local_key)}")
    print(f"    -> APP_SECRET: {mask_key(local_secret)}")
    
    print("\n  [결과] 위 경로의 파일들에서 값을 확인했습니다.")
except Exception as e:
    print(f"  [오류] .env 파일 읽기 중 오류 발생: {e}")
print("-" * 50)


# --- 🏆 최종 결론: main.py가 실제 사용할 값 ---
print("--- 🏆 최종 사용될 값 (main.py 로직 기준) ---")

try:
    # main.py의 _build_trader_from_cfg 로직을 그대로 시뮬레이션
    
    # 1순위: AppSettings (JSON)
    final_key = (getattr(app_cfg, "app_key", None) or "").strip()
    final_secret = (getattr(app_cfg, "app_secret", None) or "").strip()
    source = "1. AppSettings (.json)"

    # 2순위: 환경 변수 (os.getenv)
    if not final_key:
        final_key = (os.getenv("APP_KEY") or "").strip()
        source = "2. 환경 변수 (os.getenv)"
    if not final_secret:
        final_secret = (os.getenv("APP_SECRET") or "").strip()
        source = "2. 환경 변수 (os.getenv)"

    # 3순위: .env 파일 (load_keys) - 1, 2순위가 하나라도 비었을 때만 실행
    if not final_key or not final_secret:
        source = "3. .env 파일 (load_keys)"
        # load_keys()는 내부적으로 env > central > local 순서로 다시 확인
        lk, ls = load_keys() 
        
        final_key = final_key or (lk or "").strip()
        final_secret = final_secret or (ls or "").strip()

    print(f"▶️ 실제 토큰 발급에 사용될 값의 출처: '{source}'")
    print(f"  FINAL APP_KEY   : {mask_key(final_key)}")
    print(f"  FINAL APP_SECRET: {mask_key(final_secret)}")

    if not final_key or not final_secret:
        print("\n[경고] 🚨 최종 키 또는 비밀번호 값이 비어있습니다!")
        print("[조치] 위 1, 2, 3번 경로 중 하나에 올바른 값을 입력하세요.")
    else:
        print("\n[정보] ✅ 이 값으로 토큰 발급을 시도합니다.")
        print("   만약 이 값으로도 실패한다면, 키/비밀번호 '값' 자체가 잘못된 것입니다.")

except Exception as e:
    print(f"[오류] 최종 값 확인 중 오류 발생: {e}")

print("-" * 50)