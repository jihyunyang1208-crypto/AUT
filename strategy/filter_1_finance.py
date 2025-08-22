
"""
trader\strategy\filter_1_finance.py
역할: 상장 법인 목록을 기반으로 기업의 재무 상태(영업이익, 부채비율, 시가총액)를 웹 크롤링하여 우량 기업을 1차 필터링합니다.
매수 조건: 해당 없음 (재무 데이터 기반 필터링)
매도 조건: 해당 없음 (재무 데이터 기반 필터링)
# 입력: 상장법인목록.csv
# 출력: stock_codes.csv
# 네이버 금융 크롤링 : https://finance.naver.com/item/main.nhn?code=005930
"""

import pandas as pd
import requests
from bs4 import BeautifulSoup
import time
import logging
import argparse
import re
import os # 파일 경로 처리를 위해 os 모듈 임포트


# ───────────────────────────────
# Logging Setup
# ───────────────────────────────
def setup_logger(log_level_str):
    numeric_level = getattr(logging, log_level_str.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"유효하지 않은 로그 레벨: {log_level_str}")
    
    # 기본 핸들러가 없는 경우에만 설정 (메인 스크립트에서 이미 설정했을 경우 중복 방지)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=numeric_level,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S"
        )
    # filter_1_finance 모듈의 로거를 가져옵니다.
    return logging.getLogger(__name__)

# 전역 로거 인스턴스 (setup_logger를 통해 초기화될 예정)
logger = None

# ───────────────────────────────
# Constants (상수 정의)
# ───────────────────────────────
MAX_DEBT_RATIO = 100 # 부채비율 최대 허용치 (100%)

# ───────────────────────────────
# Stock Processing
# ───────────────────────────────
def load_stock_list(file_path):
    """상장법인 목록 CSV 파일을 로드하고 종목코드를 6자리 문자열로 포맷합니다."""
    try:
        df = pd.read_csv(file_path, encoding='utf-8')
        df['종목코드'] = df['종목코드'].apply(lambda x: f"{x:06d}")
        logger.info(f"'{file_path}'에서 {len(df)}개 종목을 성공적으로 불러왔습니다.")
        return df[['회사명', '종목코드']]
    except FileNotFoundError:
        logger.critical(f"오류: 입력 파일 '{file_path}'을(를) 찾을 수 없습니다. 경로를 확인해주세요.")
        raise # 예외를 다시 발생시켜 상위 호출자에게 전달
    except Exception as e:
        logger.critical(f"CSV 파일 로드 중 오류 발생: {e}")
        raise

def extract_market_cap(soup, code):
    try:
        market_cap_val = None
        
        # 시가총액 정보가 포함된 <tr> 태그를 찾습니다.
        # 이미지에서 <tr class="strong"> 태그 안에 시가총액 정보가 있었으므로,
        # 해당 tr을 먼저 찾고, 그 안의 td를 찾는 것이 더 안정적입니다.
        market_cap_row = None
        # 일단 id="_market_sum"을 포함하는 <em> 태그가 들어있는 <td>를 직접 찾아서 부모 <tr>로 올라가는 방법
        em_tag = soup.find('em', id='_market_sum')
        if em_tag and em_tag.parent and em_tag.parent.name == 'td':
            # 부모 td의 부모 tr을 찾음
            market_cap_row = em_tag.parent.parent
            logging.debug(f"[{code}] '_market_sum' ID를 통해 시가총액 행을 찾았습니다.")
        else:
            # 혹은 "시가총액" th 태그를 기준으로 찾기 (기존의 table tbody tr 순회 방식)
            # 이 방식은 'summary="시가총액 정보"' 테이블이 유일하거나 명확할 때 좋습니다.
            finance_table = soup.find('table', summary="시가총액 정보")
            if finance_table:
                for row in finance_table.select("tbody tr"):
                    if "시가총액" in row.get_text(): # th 태그에 시가총액 텍스트가 있으므로 get_text() 사용
                        market_cap_row = row
                        logging.debug(f"[{code}] '시가총액 정보' 테이블에서 시가총액 행을 찾았습니다.")
                        break
            
        if market_cap_row:
            # 시가총액 값이 들어있는 <td> 태그를 찾습니다.
            market_cap_td = market_cap_row.find('td')
            if market_cap_td:
                # <td> 태그 내부의 모든 텍스트 노드를 가져옵니다.
                # .get_text(strip=True)를 사용하여 하위 태그와 텍스트 노드 모두를 한 줄로 합쳐 가져옴
                raw_full_text = market_cap_td.get_text(strip=True)
                logging.debug(f"[{code}] <td>에서 추출한 원본 전체 텍스트: '{raw_full_text}'")

                # 숫자, 점, '조', '억', '천'만 남기고 모두 제거
                clean_text = re.sub(r'[^\d.조억천]', '', raw_full_text) 
                logging.debug(f"[{code}] 정규식으로 정제된 텍스트: '{clean_text}'")

                # 단위 처리 로직 (이전 버전보다 더 견고하게)
                if '조' in clean_text:
                    parts = clean_text.split('조')
                    trillion_part = parts[0] if parts[0] else '0'
                    # '조' 뒤에 '억'이 붙는 경우를 처리 (예: 2조9899억)
                    billion_part = parts[1].replace('억', '').replace('천', '') if len(parts) > 1 else '0'
                    
                    trillion_val = float(trillion_part) * 1_0000_0000_0000
                    billion_val = 0
                    if billion_part:
                        billion_val = float(billion_part) * 1_0000_0000
                    market_cap_val = int(trillion_val + billion_val)

                elif '억' in clean_text:
                    billion_part = clean_text.replace('억', '').replace('천', '')
                    market_cap_val = int(float(billion_part) * 1_0000_0000)
                elif '천' in clean_text: # 억 미만 단위는 사실상 거의 없음
                    thousand_part = clean_text.replace('천', '')
                    market_cap_val = int(float(thousand_part) * 1000)
                else:
                    # '조'나 '억' 단위가 명시되지 않은 경우, 기본적으로 '원' 단위라고 가정
                    # 이미지 상으로는 '억 원'이 단위이므로, 이 else 블록에 들어오면 안 됨
                    # 만약 들어온다면 문제가 있는 것.
                    logging.warning(f"[{code}] 시가총액에 단위('조', '억', '천')가 명시되지 않았습니다. {clean_text}를 그대로 원으로 변환 시도.")
                    market_cap_val = int(float(clean_text))
                
                logging.debug(f"[{code}] 최종 변환된 시가총액: {market_cap_val:,}원")
                return market_cap_val

            logging.warning(f"[{code}] 시가총액 <td> 태그를 찾을 수 없습니다.")
            return None
        
        logging.warning(f"[{code}] 시가총액 정보를 포함하는 <tr> 태그를 찾을 수 없습니다.")
        return None
    except ValueError as ve:
        logging.warning(f"[{code}] 시가총액 값 변환 실패 (ValueError): {ve}. 원본 텍스트: '{raw_full_text if 'raw_full_text' in locals() else 'N/A'}'")
        return None
    except Exception as e:
        logging.error(f"[{code}] 시가총액 추출 중 예외 발생: {e}", exc_info=True)
        return None


def get_financial_info(code):
    """
    네이버 금융에서 종목의 영업이익, 부채비율, 시가총액을 웹 크롤링하여 추출합니다.
    """
    try:
        url = f"https://finance.naver.com/item/main.nhn?code={code}"
        # 크롤링 차단을 피하기 위해 더 구체적인 User-Agent 사용
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
        
        logger.debug(f"[{code}] 네이버 금융 재무 정보 크롤링 요청: {url}")
        res = requests.get(url, headers=headers, timeout=10) # 타임아웃 10초로 증가
        res.raise_for_status() # HTTP 오류 (4xx, 5xx) 발생 시 예외 발생

        soup = BeautifulSoup(res.text, 'html.parser')

        # --- 영업이익 및 부채비율 추출 ---
        # 재무제표 테이블 (연간/분기 실적)
        finance_table = soup.select_one("table.tb_type1.tb_num.tb_type1_ifrs")
        operating_profit = None
        debt_ratio = None 

        if not finance_table:
            logger.warning(f"[{code}] 재무제표 테이블 (class=tb_type1_ifrs)을 찾을 수 없습니다.")
        else:
            # 테이블의 모든 행을 순회하여 영업이익과 부채비율을 찾음
            for row in finance_table.select("tr"):
                # 영업이익 추출
                if "영업이익" in row.text:
                    tds = row.find_all("td")
                    # 가장 최근 연간 또는 분기 영업이익 (첫 번째 td 값)
                    profits_raw = [td.text.strip().replace(',', '') for td in tds if td.text.strip()]
                    if profits_raw:
                        try:
                            operating_profit = float(profits_raw[0])
                            logger.debug(f"[{code}] '영업이익' 추출: {operating_profit}")
                        except ValueError:
                            logger.warning(f"[{code}] 영업이익 값 변환 실패: '{profits_raw[0]}'")
                
                # 부채비율 추출
                if "부채비율" in row.text:
                    tds = row.find_all("td")
                    # 가장 최근 부채비율 (첫 번째 td 값)
                    ratios_raw = [td.text.strip().replace(',', '').replace('%', '') for td in tds if td.text.strip()]
                    if ratios_raw:
                        try:
                            debt_ratio = float(ratios_raw[0])
                            logger.debug(f"[{code}] '부채비율' 추출: {debt_ratio}%")
                        except ValueError:
                            logger.warning(f"[{code}] 부채비율 값 변환 실패: '{ratios_raw[0]}'")

            if operating_profit is None:
                logger.warning(f"[{code}] 재무제표 테이블에서 '영업이익' 계정을 찾지 못했습니다.")
            if debt_ratio is None:
                logger.warning(f"[{code}] 재무제표 테이블에서 '부채비율' 계정을 찾지 못했습니다.")

        # --- 시가총액 추출 ---
        market_cap = extract_market_cap(soup, code)
        if market_cap is None:
            logger.warning(f"[{code}] 시가총액 정보를 찾지 못했습니다.")

        return operating_profit, debt_ratio, market_cap

    except requests.exceptions.RequestException as e:
        logger.error(f"[{code}] HTTP 요청 오류 발생 (네이버 금융): {e}", exc_info=True)
        return None, None, None
    except Exception as e:
        logger.error(f"[{code}] 네이버 금융 정보 크롤링 중 예상치 못한 오류 발생: {e}", exc_info=True)
        return None, None, None

def filter_stocks(df, min_profit_billion, min_market_cap_billion, max_debt_ratio):
    """
    주어진 재무 조건(영업이익, 시가총액, 부채비율)을 기반으로 종목을 필터링합니다.
    """
    results = []
    total_stocks = len(df)
    for i, row in df.iterrows():
        name = row['회사명']
        code = row['종목코드']
        logger.info(f"⏳ [{i+1}/{total_stocks}] 종목 확인 중: {name} ({code})...")

        # 재무 정보 크롤링
        profit_crawled, debt_ratio_crawled, market_cap_crawled = get_financial_info(code)
        
        # 필수 정보 누락 시 건너뛰기
        if profit_crawled is None or debt_ratio_crawled is None or market_cap_crawled is None:
            logger.warning(f"[{name}({code})] 필수 재무 정보(영업이익, 부채비율, 시가총액) 중 일부 누락되어 필터링 대상에서 제외합니다.")
            time.sleep(0.5) # 다음 크롤링 전 대기
            continue

        # 네이버 금융의 영업이익 단위는 '억'원이므로, 입력 받은 최소 영업이익과 단위를 맞춥니다.
        # min_profit_billion은 억 원 단위로 입력받았으므로, profit_crawled가 억 원 단위임을 가정
        
        # 1차 필터링 조건 : 영업이익 ≥ X억, 시가총액 ≥ Y원, 부채비율 ≤ Z%
        # 시가총액은 get_financial_info에서 이미 원화 단위로 변환되어 반환된다고 가정합니다.
        # 따라서 min_market_cap_billion은 억원 단위이므로 변환이 필요합니다.
        min_market_cap_won = min_market_cap_billion * 1_0000_0000 # 억원 -> 원

        if (profit_crawled >= min_profit_billion and
            market_cap_crawled >= min_market_cap_won and
            debt_ratio_crawled <= max_debt_ratio):
            
            logger.info(f"✅ [{name}({code})] 모든 필터 조건 통과! (영업이익: {profit_crawled:,}억, 시총: {market_cap_crawled:,}원, 부채비율: {debt_ratio_crawled:.1f}%)")
            results.append({
                '회사명': name,
                '종목코드': code,
                '영업이익(억)': profit_crawled,
                '부채비율(%)': debt_ratio_crawled,
                '시가총액(원)': market_cap_crawled
            })
        else:
            logger.info(f"❌ [{name}({code})] 필터 조건 미충족. (영업이익: {profit_crawled:,}억, 시총: {market_cap_crawled:,}원, 부채비율: {debt_ratio_crawled:.1f}%)")
        time.sleep(0.5) # 크롤링 간격 유지를 위해 충분히 대기

    return pd.DataFrame(results)

# ───────────────────────────────
# Main Execution Function (외부에서 호출될 함수)
# ───────────────────────────────
def run_finance_filter(input_csv="상장법인목록.csv", output_csv="stock_codes.csv"):
    """
    재무 데이터를 기반으로 주식 목록을 필터링하는 메인 함수.
    Args:
        input_csv (str): 상장법인 목록이 포함된 CSV 파일 경로.
        output_csv (str): 필터링된 주식 코드를 저장할 CSV 파일 경로.
    """
    global logger # 전역 로거 변수 사용을 선언
    logger = setup_logger(os.getenv("LOG_LEVEL", "INFO")) # .env에서 LOG_LEVEL을 가져와 로거 설정

    logger.info("--- 📊 금융 필터링 시작 (filter_1_finance.py) ---")

    # 프로젝트 루트를 기준으로 파일 경로 처리
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    # 이 스크립트가 'strategy' 폴더 안에 있다고 가정하고,
    # '상장법인목록.csv'와 'stock_codes.csv'는 프로젝트 루트에 있다고 가정합니다.
    project_root = os.path.dirname(current_script_dir) 

    input_file_full_path = os.path.join(project_root, input_csv)
    output_file_full_path = os.path.join(project_root, output_csv)

    logger.info(f"📄 상장 기업 목록 불러오는 중: '{input_file_full_path}'...")
    try:
        stock_df = load_stock_list(input_file_full_path)
    except Exception: # load_stock_list에서 이미 로그를 남겼으므로 여기서는 pass
        return # 파일 로드 실패 시 함수 종료

    if stock_df.empty:
        logger.warning("로드된 종목이 없어 필터링을 진행하지 않습니다.")
        # 필터링할 종목이 없어도 빈 CSV 파일을 생성하여 다음 단계 오류 방지
        pd.DataFrame(columns=['회사명', '종목코드', '영업이익(억)', '부채비율(%)', '시가총액(원)']).to_csv(output_file_full_path, index=False, encoding='utf-8-sig')
        logger.info(f"빈 후보 종목 파일 '{output_file_full_path}' 생성 완료.")
        logger.info("--- 📊 금융 필터링 완료 (필터링된 종목 없음) ---")
        return pd.DataFrame() # 빈 DataFrame 반환

    # 필터링 조건 정의
    min_profit_billion = 5         # 영업이익 5억 이상
    min_market_cap_billion = 1000   # 시가총액 1000억 이상
    max_debt_ratio = MAX_DEBT_RATIO # 부채비율 100% 이하 (상수 사용)

    logger.info(f"🔍 필터링 조건: 영업이익 ≥ {min_profit_billion}억, 시가총액 ≥ {min_market_cap_billion}억, 부채비율 ≤ {max_debt_ratio}%")

    result_df = filter_stocks(stock_df, min_profit_billion, min_market_cap_billion, max_debt_ratio)

    logger.info(f"💾 결과 저장 중: '{output_file_full_path}'...")
    try:
        if not result_df.empty:
            result_df.to_csv(output_file_full_path, index=False, encoding='utf-8-sig')
            logger.info(f"🎉 금융 필터링 완료! 총 {len(result_df)}개의 종목이 필터링 조건을 통과했습니다.")
        else:
            logger.info("🚫 모든 금융 필터를 통과한 종목이 없습니다.")
            # 결과가 없는 경우에도 헤더를 포함한 빈 CSV 파일 생성
            pd.DataFrame(columns=['회사명', '종목코드', '영업이익(억)', '부채비율(%)', '시가총액(원)']).to_csv(output_file_full_path, index=False, encoding='utf-8-sig')
            logger.info(f"빈 후보 종목 파일 '{output_file_full_path}' 생성 완료.")
    except Exception as e:
        logger.critical(f"결과 CSV 파일 저장 중 오류 발생: {e}", exc_info=True)

    logger.info("--- 📊 금융 필터링 완료 ---")
    return result_df

# ───────────────────────────────
# Script Entry Point (개발/테스트를 위해 이 파일 단독 실행 시 사용)
# ───────────────────────────────
if __name__ == "__main__":
    # 단독 실행 시 .env 파일이 로드되지 않았을 수 있으므로 여기서 로드 시도
    try:
        from dotenv import load_dotenv
        load_dotenv()
        logger.debug("💡 .env 파일 로드 완료 (단독 실행 모드).")
    except ImportError:
        logger.debug("⚠️ python-dotenv 라이브러리가 설치되지 않았습니다. 'pip install python-dotenv'로 설치하세요.")
    except Exception as e:
        logger.debug(f"⚠️ .env 파일 로드 중 오류 발생: {e}")

    # Argument 파싱
    parser = argparse.ArgumentParser(description="재무 데이터를 기반으로 주식 종목을 필터링합니다.")
    parser.add_argument("--log", default="INFO", help="로그 레벨 (DEBUG, INFO, WARNING, ERROR). 기본값: INFO")
    args = parser.parse_args()

    # 단독 실행 시 로거 설정
    logger = setup_logger(args.log) # 전역 로거 변수 초기화
    
    logger.info("--- filter_1_finance.py 단독 실행 시작 ---")
    
    # 입력 CSV 파일 경로를 현재 스크립트 위치 기준으로 설정 (프로젝트 루트에 '상장법인목록.csv' 있다고 가정)
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_script_dir)
    input_csv_path_for_standalone = os.path.join(project_root, "상장법인목록.csv")
    output_csv_path_for_standalone = os.path.join(project_root, "stock_codes.csv")

    # 필터링 함수 실행
    run_finance_filter(input_csv=input_csv_path_for_standalone, output_csv=output_csv_path_for_standalone)
    
    logger.info("--- filter_1_finance.py 단독 실행 종료 ---")