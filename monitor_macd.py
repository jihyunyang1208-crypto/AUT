# monitor_macd.py
'''
모니터링 함수: monitor_macd 함수는 특정 종목의 5분봉 차트를 조회하고 MACD 신호를 계산하는 역할을 합니다.
이 함수는 무한 루프를 통해 주기적으로 데이터를 요청하고 MACD, 시그널 라인, 히스토그램을 계산합니다.
스레드 생성: start_monitoring 함수는 주어진 종목 리스트에 대해 각각의 스레드를 생성하여 monitor_macd 함수를 실행합니다.
API 요청: 각 스레드는 키움증권 REST API를 통해 5분봉 차트를 조회하고, 응답 데이터에서 MACD, 시그널 라인, 히스토그램을 계산하여 출력합니다.
대기 시간: time.sleep(300)을 통해 5분 간격으로 데이터를 요청합니다.
MACD, 시그널, 히스토그램 계산 함수: calculate_macd_and_signal 함수는 가격 리스트를 입력받아 MACD, 시그널 라인, 히스토그램 값을 반환합니다. (pandas 활용)
MACD 출력: 계산된 MACD, 시그널 라인, 히스토그램 값을 콜백을 통해 전달하고 터미널에도 출력합니다.
'''
from utils.utils import load_api_keys
from utils.token_manager import get_access_token
import requests
import json
import threading
import time
import numpy as np # numpy는 여전히 유용할 수 있습니다.
import pandas as pd # pandas 라이브러리
from collections import deque

import logging
logger = logging.getLogger(__name__)


# MACD, 시그널 라인, 히스토그램 계산 함수 (pandas 사용)
# 이 함수는 더 이상 macd_history 리스트를 인자로 받지 않습니다.
def calculate_macd_and_signal(prices):
    prices_series = pd.Series(prices)

    # Ensure enough data for 26-period EMA
    if len(prices_series) < 26: # 최소 26개 봉이 있어야 26일 EMA를 계산할 수 있음
        return None, None, None

    # Calculate 12-period EMA
    ema_12 = prices_series.ewm(span=12, adjust=False, min_periods=12).mean()
    # Calculate 26-period EMA
    ema_26 = prices_series.ewm(span=26, adjust=False, min_periods=26).mean()

    # MACD Line
    macd_line = ema_12 - ema_26

    # Ensure enough MACD points for 9-period Signal Line
    # dropna()를 사용하여 NaN이 아닌 유효한 MACD 값의 개수를 확인
    if len(macd_line.dropna()) < 9: # 최소 9개의 유효한 MACD 값이 있어야 시그널 라인을 계산할 수 있음
        # macd_line이 비어있지 않고 마지막 값이 NaN이 아니라면 MACD만 반환, Signal은 None
        return macd_line.iloc[-1] if not macd_line.empty and pd.notna(macd_line.iloc[-1]) else None, None, None

    # Signal Line (9-period EMA of MACD Line)
    signal_line = macd_line.ewm(span=9, adjust=False, min_periods=9).mean()

    # Ensure latest signal line exists
    if signal_line.empty or pd.isna(signal_line.iloc[-1]):
        # macd_line이 비어있지 않고 마지막 값이 NaN이 아니라면 MACD만 반환, Signal은 None
        return macd_line.iloc[-1] if not macd_line.empty and pd.notna(macd_line.iloc[-1]) else None, None, None

    # MACD Histogram
    macd_histogram = macd_line - signal_line

    # Return the latest calculated values
    # Check for NaN before returning
    latest_macd = macd_line.iloc[-1] if not macd_line.empty and pd.notna(macd_line.iloc[-1]) else None
    latest_signal = signal_line.iloc[-1] if not signal_line.empty and pd.notna(signal_line.iloc[-1]) else None
    latest_histogram = macd_histogram.iloc[-1] if not macd_histogram.empty and pd.notna(macd_histogram.iloc[-1]) else None

    return latest_macd, latest_signal, latest_histogram


# 최근 값 저장 및 평균 추세 비교 함수
def track_moving_average_trend(value, history: deque, label: str, previous_avg_holder: dict):
    history.append(value)
    
    if len(history) == history.maxlen:
        current_avg = sum(history) / len(history)

        previous_avg = previous_avg_holder.get(label)
        if previous_avg is not None:
            if current_avg > previous_avg:
                logger.debug(f"🔴🔺 {label} 6개 평균 상승 중 ({previous_avg:.2f} → {current_avg:.2f})")
            elif current_avg < previous_avg:
                logger.debug(f"🔵🔻 {label} 6개 평균 하락 중 ({previous_avg:.2f} → {current_avg:.2f})")
            else:
                logger.debug(f"⚖️  {label} 6개 평균 변화 없음 ({current_avg:.2f})")

        previous_avg_holder[label] = current_avg  # 최신 평균값 저장


# MACD 신호 모니터링 함수
def monitor_macd(token, stk_cd, macd_callback=None):
    previous_macd_line = None
    previous_signal_line = None
    macd_history = deque(maxlen=6)
    signal_history = deque(maxlen=6)
    previous_avgs = {}  # 평균값 보관용 딕셔너리


    while True:
        # 1. 요청할 API URL
        host = 'https://api.kiwoom.com'  # 실전투자
        endpoint = '/api/dostk/chart'
        url = host + endpoint

        # 2. header 데이터
        headers = {
            'Content-Type': 'application/json;charset=UTF-8',  # 컨텐츠타입
            'authorization': f'Bearer {token}',  # 접근토큰
            'api-id': 'ka10080',  # TR명 (5분봉 차트 조회)
        }

        # 3. 요청 데이터
        params = {
            'stk_cd': stk_cd,  # 종목코드
            'tic_scope': '5',  # 5분봉
            'upd_stkpc_tp': '1',  # 수정주가구분
            'num_of_item_to_fetch': '200', # 충분한 과거 데이터를 요청 (예: 200봉)
        }

        # 4. API 요청
        try:
            response = requests.post(url, headers=headers, json=params)
            response.raise_for_status() # HTTP 에러 발생 시 예외 발생
            data = response.json()
        except requests.exceptions.RequestException as e:
            logger.debug(f"API 요청 오류 ({stk_cd}): {e}")
            time.sleep(60) # 오류 발생 시에도 잠시 대기 후 재시도
            continue
        except json.JSONDecodeError as e:
            logger.debug(f"JSON 디코딩 오류 ({stk_cd}): {e}")
            time.sleep(60)
            continue


        # 5. MACD 계산 및 출력
        if 'stk_min_pole_chart_qry' in data and data['stk_min_pole_chart_qry']:
            current_prices = [] # 매번 API에서 받아온 데이터를 저장할 리스트 (새로 시작)
            # Kiwoom API의 차트 데이터는 보통 최신 봉이 가장 위에 (인덱스 0) 있습니다.
            # pandas.ewm은 시간 순서대로 데이터를 처리하므로, 오래된 봉부터 순서대로 `current_prices`에 추가해야 합니다.
            for entry in reversed(data['stk_min_pole_chart_qry']):
                try:
                    cur_prc = float(entry['cur_prc'])  # 현재가
                    current_prices.append(cur_prc)  # 가격 리스트에 추가 (오래된 것부터)
                except (ValueError, KeyError) as e:
                    logger.debug(f"가격 데이터 파싱 오류 ({stk_cd}): {e}, entry: {entry}")
                    continue

            # 충분한 가격 데이터가 있을 때만 MACD 및 시그널 계산 시도
            if current_prices: # 가격 데이터가 실제로 있다면
                # calculate_macd_and_signal 함수는 이제 current_prices만 인자로 받습니다.
                macd_line, signal_line, macd_histogram = calculate_macd_and_signal(current_prices)
                
                if macd_line is not None and signal_line is not None:
                    logger.debug(f"종목: {stk_cd}, MACD: {macd_line:.2f}, Signal: {signal_line:.2f}, Histogram: {macd_histogram:.2f}")

                                        # 시그널 라인 골든 크로스 감지 로직 추가
                    if previous_signal_line is not None: # 이전 시그널 값이 있을 때만 비교
                        # 시그널 라인이 음수에서 양수로 전환되는 시점 감지
                        if previous_signal_line < 0 and signal_line >= 0:
                            logger.debug(f"🚨🚨🚨 종목: {stk_cd} - 시그널 라인 골든 크로스 발생! (이전: {previous_signal_line:.2f} -> 현재: {signal_line:.2f})")
                            # 골든 크로스 발생 시, 현재 가격 및 지표 값을 콜백 함수로 전달
                            if macd_callback:
                                # current_prices의 마지막 요소가 가장 최근 가격입니다.
                                current_price_at_crossover = current_prices[-1] 
                                macd_callback(stk_cd, current_price_at_crossover, macd_line, signal_line, macd_histogram)

                    # 현재 값을 다음 루프의 이전 값으로 업데이트
                    previous_macd_line = macd_line
                    previous_signal_line = signal_line


                else:
                    # 데이터 부족 메시지를 덜 혼란스럽게 변경
                    logger.debug(f"종목: {stk_cd}, MACD/Signal 계산을 위한 충분한 유효 데이터 부족. 현재 가격 봉 수: {len(current_prices)}")
            else:
                logger.debug(f"종목: {stk_cd}, API에서 유효한 차트 가격 데이터를 가져오지 못했습니다.")
        else:
            logger.debug(f"종목: {stk_cd}, 차트 데이터 없음 또는 응답 형식 오류: {data}")


        if current_prices:
            macd_line, signal_line, macd_histogram = calculate_macd_and_signal(current_prices)

            if macd_line is not None and signal_line is not None:
                # MACD 및 Signal 평균 추세 분석
                track_moving_average_trend(macd_line, macd_history, "MACD", previous_avgs)
                track_moving_average_trend(signal_line, signal_history, "Signal", previous_avgs)

                logger.debug(f"종목: {stk_cd}, MACD: {macd_line:.2f}, Signal: {signal_line:.2f}, Histogram: {macd_histogram:.2f}")

                # 골든크로스 감지
                if previous_signal_line is not None and previous_signal_line < 0 and signal_line >= 0:
                    logger.debug(f"🚨🚨🚨 종목: {stk_cd} - 시그널 라인 골든 크로스 발생!")
                    if macd_callback:
                        current_price_at_crossover = current_prices[-1]
                        macd_callback(stk_cd, current_price_at_crossover, macd_line, signal_line, macd_histogram)

                previous_macd_line = macd_line
                previous_signal_line = signal_line

        time.sleep(60)  # 5분봉이므로 300초(5분) 대기

# 스레드 생성 및 실행
def start_monitoring(token, stock_list, macd_callback=None):
    threads = []
    for stock in stock_list:
        thread = threading.Thread(target=monitor_macd, args=(token, stock, macd_callback))
        thread.start()
        threads.append(thread)


# 실행 구간 (테스트용 - 실제 Flask 앱에서는 main.py에서 호출)
if __name__ == '__main__':
    appkey, secretkey = load_api_keys()
    access_token = get_access_token(appkey, secretkey)
    stock_list = ['408900']  # 모니터링할 종목 리스트

    def test_macd_callback(stock_code, macd_line, signal_line, macd_histogram):
        logger.debug(f"콜백 수신 - 종목: {stock_code}, MACD: {macd_line:.2f}, Signal: {signal_line:.2f}, Histogram: {macd_histogram:.2f}")

    start_monitoring(access_token, stock_list, test_macd_callback)