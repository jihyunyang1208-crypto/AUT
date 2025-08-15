# C:\trade\trader\strategy\filter_2_technical.py

import os
import time
import json
import requests
import pandas as pd
from datetime import datetime, timedelta
# from dotenv import load_dotenv # main.py에서 처리하므로 주석 처리
import logging
import sys # sys.path 수정을 위해 필요

# -----------------------------------------------------------
# 로깅 설정 (이 모듈을 main.py가 import 할 경우, main.py의 로거 설정을 따릅니다.)
# (단독 실행 시에는 아래 __name__ == "__main__" 블록에서 별도로 설정됩니다.)
# -----------------------------------------------------------
logger = logging.getLogger(__name__) # 모듈별 로거 인스턴스 가져오기

# -----------------------------------------------------------
# 상수 정의
# -----------------------------------------------------------
BASE_URL = "https://openapi.koreainvestment.com:9443"

MIN_PRICE = 1000
MAX_PRICE = 1_000_000
MAX_DEBT_RATIO = 100
JUMP_THRESHOLD = 0.25 # 0.25 = 25%

# -----------------------------------------------------------
# API 도우미 함수
# -----------------------------------------------------------

# core.token_manager에서 get_access_token을 동적으로 임포트
# main.py에서 sys.path를 설정하고 이 모듈을 임포트할 때 순환 참조를 피하기 위함
# 단독 실행 시에는 dotenv 로딩 및 sys.path 설정이 여기서 처리됩니다.
try:
    # main.py가 project_root를 sys.path에 추가했음을 가정합니다.
    from core.token_manager import get_access_token
except ImportError:
    # 단독 실행 시 project_root가 sys.path에 없을 경우 임시로 추가
    current_dir_for_import = os.path.dirname(os.path.abspath(__file__))
    project_root_for_import = os.path.dirname(current_dir_for_import) # strategy에서 project_root로 이동
    if project_root_for_import not in sys.path:
        sys.path.append(project_root_for_import)
    from core.token_manager import get_access_token
    # 단독 실행 시 .env 파일이 아직 로드되지 않았을 수 있으므로 여기서 로드
    from dotenv import load_dotenv # 단독 실행을 위한 임포트
    load_dotenv()
    


def get_latest_trading_day():
    """최신 거래일(평일)을 'YYYYMMDD' 형식으로 반환합니다."""
    today = datetime.now()
    weekday = today.weekday() # 월=0, 일=6
    delta = 0
    if weekday == 5: # 토요일
        delta = 1
    elif weekday == 6: # 일요일
        delta = 2
    return (today - timedelta(days=delta)).strftime("%Y%m%d")

def get_current_price(stock_code):
    """주어진 종목코드의 현재가를 조회합니다."""
    access_token = get_access_token()
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price" # 실시간 현재가 조회 URL
    headers = {
        "authorization": f"Bearer {access_token}",
        "appKey": os.getenv("APP_KEY"),
        "appSecret": os.getenv("APP_SECRET"),
        "tr_id": "FHKST01010100", # 실시간 현재가 TR_ID
    }
    params = {
        "fid_cond_mrkt_div_code": "J", # 주식시장 조건 구분 코드 (J: 주식)
        "fid_input_iscd": stock_code, # 종목코드
    }
    try:
        logger.debug(f"[{stock_code}] 현재가 조회 요청: {url}, 파라미터: {params}")
        res = requests.get(url, headers=headers, params=params, timeout=5)
        res.raise_for_status() # HTTP 오류 (4xx, 5xx) 발생 시 예외 발생
        data = res.json()
        if data and data.get('output') and 'stck_prpr' in data['output']:
            price = int(data['output']['stck_prpr'])
            logger.debug(f"[{stock_code}] 현재가: {price:,}원")
            return price
        else:
            logger.warning(f"[{stock_code}] 현재가 조회 응답 오류 또는 데이터 없음. API 메시지: {data.get('msg1', '알 수 없는 오류')}")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"[{stock_code}] 현재가 조회 HTTP 요청 오류 발생: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"[{stock_code}] 현재가 조회 처리 중 예상치 못한 오류 발생: {e}", exc_info=True)
        return None

def had_25_percent_jump_within_20_days(stock_code):
    """
    주어진 종목이 최근 20 거래일 이내에 25% 이상 급등한 이력이 있는지 확인합니다.
    """
    access_token = get_access_token()
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-price" # 주식 일자별 시세 조회 URL
    headers = {
        "authorization": f"Bearer {access_token}",
        "appKey": os.getenv("APP_KEY"),
        "appSecret": os.getenv("APP_SECRET"),
        "tr_id": "FHKST01010400" # 주식 일자별 시세 TR_ID
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J", # 주식시장 조건 구분 코드 (J: 주식)
        "FID_INPUT_ISCD": stock_code, # 종목코드
        "FID_PERIOD_DIV_CODE": "D", # 일봉 데이터 요청
        "FID_ORG_ADJ_PRC": "1", # 수정주가 반영
        "fid_input_date": get_latest_trading_day(), # 최신 거래일 기준
        "fid_date_cnt": "30" # 충분한 과거 데이터 (최근 20 영업일 확인을 위해 30일 요청)
    }

    try:
        logger.debug(f"[{stock_code}] 주가 이력 조회 요청: {url}, 파라미터: {params}")
        res = requests.get(url, headers=headers, params=params, timeout=5)
        res.raise_for_status()
        data = res.json()
        prices_data = data.get("output1") or data.get("output") # API 응답 구조에 따라 output1 또는 output
        
        if not prices_data:
            logger.warning(f"[{stock_code}] 주가 이력 조회 데이터 없음. API 메시지: {data.get('msg1', '알 수 없는 오류')}")
            return False

        # 'stck_clpr' (종가) 값이 비어있지 않고 유효한지 확인 후 정수로 변환
        closes = [int(p["stck_clpr"]) for p in prices_data if p.get("stck_clpr") and p["stck_clpr"].strip()]
        
        if len(closes) < 21: # 최소 21개 데이터 (오늘 + 과거 20일) 필요
            logger.debug(f"[{stock_code}] 20일치 종가 데이터 부족: {len(closes)}개 데이터만 있음.")
            return False

        # API는 최신 데이터부터 제공하므로, 상위 21개 데이터만 사용
        closes_21_days = closes[:21]

        # 20일치 기간 동안 25% 급등 여부 확인
        # (i=1부터 시작하여 closes_21_days[i-1] (최신)과 closes_21_days[i] (과거) 비교)
        for i in range(1, len(closes_21_days)):
            prev_close = closes_21_days[i]   # 더 과거 시점의 종가
            curr_close = closes_21_days[i-1] # 더 최신 시점의 종가

            if prev_close == 0:
                logger.debug(f"[{stock_code}] {i}일 전 종가가 0이어서 급등률 계산 불가. (현재 종가: {curr_close})")
                continue
            
            rate = (curr_close / prev_close) - 1 # (최신 종가 / 과거 종가) - 1
            if rate >= JUMP_THRESHOLD:
                logger.debug(f"[{stock_code}] {i-1}일 전 ({curr_close:,}원) 대비 {i}일 전 ({prev_close:,}원) {rate:.2%} 급등 감지.")
                return True
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"[{stock_code}] 주가 이력 조회 HTTP 요청 오류 발생: {e}", exc_info=True)
        return False
    except Exception as e:
        logger.error(f"[{stock_code}] 주가 이력 조회 처리 중 예상치 못한 오류 발생: {e}", exc_info=True)
        return False


def get_debt_ratio_only(stock_code):
    """주어진 종목코드의 부채비율을 조회합니다."""
    access_token = get_access_token()
    # TODO: KIS 개발자 포털에서 FHKST03010100 (재무정보 조회)의 정확한 URL을 재확인하세요.
    # 현재 '404 Not Found' 오류가 발생하고 있습니다.
    # API 문서의 "기본정보" 탭에서 "URL" 항목을 확인해야 합니다.
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-financial-info" # << 이 URL을 확인하세요!
    headers = {
        "authorization": f"Bearer {access_token}",
        "appkey": os.getenv("APP_KEY"),
        "appsecret": os.getenv("APP_SECRET"),
        "tr_id": "FHKST03010100", # 재무상태표, 손익계산서, 현금흐름표 조회 TR_ID
    }
    params = {
        "fid_cond_mrkt_div_code": "J", # 주식시장 조건 구분 코드 (J: 주식)
        "fid_input_iscd": stock_code, # 종목코드
    }

    try:
        logger.debug(f"[{stock_code}] 부채비율 조회 요청: {url}, 파라미터: {params}")
        res = requests.get(url, headers=headers, params=params, timeout=5)
        res.raise_for_status() # HTTP 오류 (4xx, 5xx) 발생 시 예외 발생
        data = res.json()
        items = data.get("output", [])
        
        if not items:
            logger.warning(f"[{stock_code}] 재무정보 조회 데이터 없음. API 메시지: {data.get('msg1', '알 수 없는 오류')}")
            return None

        for item in items:
            if "부채비율" in item.get("account_nm", ""):
                try:
                    debt_ratio_str = item.get("thstrm_amount", "").strip().replace(",", "").replace("%", "")
                    if debt_ratio_str:
                        ratio = float(debt_ratio_str)
                        logger.debug(f"[{stock_code}] 부채비율: {ratio:.1f}%")
                        return ratio
                except ValueError:
                    logger.warning(f"[{stock_code}] 부채비율 값 변환 실패: '{item.get('thstrm_amount', 'N/A')}'")
                    pass # 변환 실패 시 다음 항목 확인 또는 None 반환
        logger.warning(f"[{stock_code}] 재무정보에서 '부채비율' 계정을 찾을 수 없습니다.")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"[{stock_code}] 부채비율 조회 HTTP 요청 오류 발생 (404 등): {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"[{stock_code}] 부채비율 조회 처리 중 예상치 못한 오류 발생: {e}", exc_info=True)
        return None

# -----------------------------------------------------------
# 메인 필터링 함수 (main.py에서 호출)
# -----------------------------------------------------------
def run_technical_filter(input_csv="stock_codes.csv", output_csv="candidate_stocks.csv"):
    """
    기술적 및 일부 재무적 필터를 주식 목록에 적용합니다.
    'stock_codes.csv'를 읽고 'candidate_stocks.csv'를 생성합니다.
    """
    logger.info("--- 📈 기술적/재무적 필터링 시작 (filter_2_technical.py) ---")

    # 프로젝트 루트를 기준으로 파일 경로 결정
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_script_dir) # strategy에서 project_root로 상위 이동

    input_file_path = os.path.join(project_root, input_csv)
    output_file_path = os.path.join(project_root, output_csv)

    if not os.path.exists(input_file_path):
        logger.critical(f"입력 파일 '{input_file_path}'을(를) 찾을 수 없습니다. "
                        "재무 필터 (filter_1_finance.py)를 먼저 실행해야 합니다.")
        # 실패를 나타내기 위해 빈 DataFrame 반환
        return pd.DataFrame()

    try:
        df = pd.read_csv(input_file_path)
        if df.empty:
            logger.warning(f"입력 파일 '{input_file_path}'이(가) 비어 있습니다. 필터링할 종목이 없습니다.")
            return pd.DataFrame()
        logger.info(f"'{input_file_path}'에서 {len(df)}개 종목을 불러왔습니다.")
    except Exception as e:
        logger.critical(f"입력 CSV 파일 '{input_file_path}' 읽기 실패: {e}", exc_info=True)
        return pd.DataFrame()

    result = []
    total_stocks = len(df)
    processed_count = 0

    for index, row in df.iterrows():
        name = row.get('회사명', '알 수 없음')
        code = str(row.get('종목코드', '')).zfill(6)
        
        if not code or code == '000000':
            logger.warning(f"유효하지 않은 종목코드/회사명 건너뛰기 (행 {index}): 회사명={name}, 종목코드={code}")
            continue

        processed_count += 1
        logger.info(f"[{processed_count}/{total_stocks}] 종목 처리 중: {name} ({code})")

        # 1. 현재가 범위 필터
        now_price = get_current_price(code)
        if now_price is None:
            logger.info(f"[{name}({code})] 조건 미충족: 현재가 조회 실패. 건너뜁니다.")
            continue
        if not (MIN_PRICE <= now_price <= MAX_PRICE):
            logger.info(f"[{name}({code})] 조건 미충족: 현재가 {now_price:,}원 (범위 {MIN_PRICE:,}~{MAX_PRICE:,}원 외부). 건너뜁니다.")
            continue
        logger.debug(f"[{name}({code})] 현재가 범위 통과: {now_price:,}원.")
        time.sleep(0.1) # API 호출 간격 유지

        # 2. 20일 내 25% 급등 여부 필터
        if not had_25_percent_jump_within_20_days(code):
            logger.info(f"[{name}({code})] 조건 미충족: 최근 20일 내 {JUMP_THRESHOLD:.0%} 이상 급등 없음. 건너뜁니다.")
            continue
        logger.debug(f"[{name}({code})] 20일 내 {JUMP_THRESHOLD:.0%} 급등 조건 통과.")
        time.sleep(0.1) # API 호출 간격 유지


        # 모든 조건을 통과한 경우
        result.append({
            "회사명": name
            ,"종목코드": code
            ,"현재가": now_price
        })
        logger.info(f"✅ [{name} ({code})] 모든 필터 조건 통과!")
        time.sleep(0.5) # API 요청 과부하 방지를 위한 긴 대기 시간

    filtered_df = pd.DataFrame(result)

    try:
        if not filtered_df.empty:
            filtered_df.to_csv(output_file_path, index=False, encoding="utf-8-sig")
            logger.info(f"\n📈 최종 후보 종목 {len(filtered_df)}개 → '{output_file_path}'에 저장 완료.")
        else:
            logger.info("\n🚫 모든 기술적/재무적 필터를 통과한 종목이 없습니다. 'candidate_stocks.csv' 파일이 비어있거나 생성되지 않습니다.")
            # 종목이 없어도 헤더가 포함된 빈 CSV 파일을 생성하여 이후 단계 오류 방지
            with open(output_file_path, 'w', encoding='utf-8-sig') as f:
                f.write("회사명,종목코드,현재가,부채비율\n") # 헤더만 작성
    except Exception as e:
        logger.critical(f"최종 후보 종목을 '{output_file_path}'에 저장 실패: {e}", exc_info=True)

    logger.info("--- 📈 기술적/재무적 필터링 완료 ---")
    return filtered_df

# -----------------------------------------------------------
# 스크립트 진입점 (단독 실행/테스트용)
# -----------------------------------------------------------
if __name__ == "__main__":
    # 단독 실행 시, 로거가 아직 설정되지 않은 경우 기본 로거를 설정합니다.
    # main.py에서 이미 로거를 설정했다면 중복되지 않습니다.
    if not logging.getLogger().handlers:
        from dotenv import load_dotenv # 단독 실행을 위한 load_dotenv 임포트
        load_dotenv() # .env 파일 로드 (단독 실행 시)
        
        # .env 파일에서 로깅 레벨 및 파일 로깅 여부 설정 불러오기
        LOG_LEVEL_STR_STANDALONE = os.getenv("LOG_LEVEL", "INFO").upper()
        LOG_LEVEL_STANDALONE = getattr(logging, LOG_LEVEL_STR_STANDALONE, logging.INFO)
        LOG_TO_FILE_STANDALONE = os.getenv("LOG_TO_FILE", "false").lower() == "true"

        standalone_handlers = [logging.StreamHandler()]
        if LOG_TO_FILE_STANDALONE:
            os.makedirs("logs", exist_ok=True) # 로그 디렉토리 생성
            standalone_file_handler = logging.FileHandler("logs/filter_2_technical_standalone.log", encoding="utf-8")
            standalone_handlers.append(standalone_file_handler)

        logging.basicConfig(
            level=LOG_LEVEL_STANDALONE,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=standalone_handlers
        )
        # basicConfig 호출 후 로거를 다시 가져와 새 핸들러가 적용되도록 합니다.
        logger = logging.getLogger(__name__)
        logger.info("--- filter_2_technical.py (단독 실행) 로깅 초기화 완료 ---")


    logger.info("--- filter_2_technical.py 단독 실행 시작 ---")
    
    # 단독 실행 시 입력 CSV 파일 경로 조정
    # stock_codes.csv는 project_root 디렉토리(strategy 상위 디렉토리)에 있다고 가정
    input_csv_path_for_standalone = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stock_codes.csv"
    )
    
    run_technical_filter(input_csv=input_csv_path_for_standalone)
    logger.info("--- filter_2_technical.py 단독 실행 완료 ---")