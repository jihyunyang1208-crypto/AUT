

# AUT: Automated Trading System (자동 트레이딩 시스템)

## 🎯 프로젝트 개요

`AUT` 프로젝트는 파이썬 기반의 **자동화된 주식 트레이딩 시스템**입니다. 키움증권 API(`kiwoom_api_token` 폴더 존재)와 연동하여 실시간 시장 데이터를 처리하고, 정의된 전략에 따라 자동 매매 주문을 실행하며, 리스크 관리 및 거래 시뮬레이션을 위한 통합 환경을 제공합니다.

### 주요 기능

  * **자동 주문 실행**: `AutoTrader`를 통한 매매 신호 기반의 자동 주문 로직 구현.
  * **포지션 관리**: 종목별 보유 현황, 평단가, 미체결 수량 관리.
  * **리스크 관리**: `risk_management` 모듈을 통한 포지션 및 손익 관리 기능.
  * **백테스팅 및 시뮬레이션**: `SimEngine`을 활용한 모의투자 환경 제공.
  * **기술 지표 계산**: `MacdCalculator`를 이용한 MACD 등 기술 지표 계산 및 캐싱.
  * **보고서 생성**: `trading_report` 모듈을 통한 일간 트레이딩 결과 보고서 생성.
  * **AI 통합**: `gemini_client.py`를 통한 AI 기반의 종목 분석 또는 보고서 생성 지원.

-----

## ⚙️ 설치 및 설정

### 1\. 저장소 클론

```bash
git clone https://github.com/jihyunyang1208-crypto/AUT.git
cd AUT
```

### 2\. 환경 설정 파일 구성

프로젝트의 최상위 디렉토리에 있는 `.env` 파일을 복사하거나 생성하여 환경 변수를 설정합니다.

```
# .env 파일 내용 (예시)
# KIWOOM_API_ID=YOUR_KIWOOM_ID
# TRADING_MODE=SIMULATION # 또는 REAL
```

### 3\. API 토큰 설정

  * **키움증권 API**: `kiwoom_api_token/` 폴더 내에 발급받은 **앱키** 및 **시크릿 키** 파일을 준비합니다. (예: `61363913_appkey.txt`, `61363913_secretkey.txt`)
  * **접근 토큰**: `access_token.json` 파일에 시스템 접근 및 인증 관련 토큰 정보를 저장합니다.

### 4\. 의존성 설치

`requirements.txt` 파일이 명확하지 않으므로, 프로젝트에 필요한 기본 의존성을 설치합니다. (PyQt/PySide6, pandas, numpy 등 금융 및 UI 관련 라이브러리가 필요할 수 있습니다.)

```bash
# 필요한 라이브러리 설치 (예시)
pip install pandas numpy PyQt6
# 추가로 필요한 경우:
# pip install google-genai
```

### 5\. 데이터 준비

  * **종목 코드**: `stock_codes.csv` 및 `상장법인목록.csv` 파일을 최신 종목 정보로 업데이트해야 합니다.
  * **후보 종목**: `candidate_stocks.csv` 파일에 트레이딩 대상으로 고려할 종목 리스트를 준비합니다.

-----

## ▶️ 시스템 실행

```bash
python main.py
```



-----

## 📚 핵심 모듈 및 클래스 역할 정리

이 시스템을 구성하는 주요 Python 클래스들의 역할과 핵심 기능을 요약합니다.

### 1\. 자동 트레이딩 및 주문 실행 (`trade_pro/`)

| 클래스 | 역할 | 주요 메서드 |
| :--- | :--- | :--- |
| **AutoTrader** | 매매 신호를 받아 실제 주문을 실행하거나 시뮬레이션합니다. 브로커 API 통신을 담당하는 핵심 자동 주문 로직입니다. | `set_simulation_mode(on)`: 시뮬레이션 모드 활성화/비활성화 |
| | | `handle_signal(payload)`: 외부 매매 신호 처리 및 주문 실행 |
| **TradeSettings** | 자동 매매의 기본 설정 (활성화, 주문 유형, 시뮬레이션 모드 등)을 관리하는 데이터 구조체 | N/A |
| **TradeLogger** | 모든 주문 및 체결 기록을 CSV/JSONL 파일 형식으로 저장 및 관리 | `write_order_record(record)`: 주문 정보 기록 |

### 2\. 포지션 관리 및 브로커 인터페이스 (`trade_pro/position_manager.py`, `core/broker_base.py`)

| 클래스 | 파일 | 역할 | 주요 메서드 |
| :--- | :--- | :--- | :--- |
| **PositionManager** | `position_manager.py` | 종목별 **보유 수량**, **평균 매수가**, **미체결 대기 수량**을 관리하고, 파일에 저장하여 데이터를 지속적으로 유지합니다. | `get_qty(symbol)`: 보유 수량 조회 |
| | | | `apply_fill_buy/sell(...)`: 매수/매도 체결 결과를 반영 |
| **ITradeBroker** | `broker_base.py` | 실거래 또는 시뮬레이션 브로커가 구현해야 하는 **추상 인터페이스**입니다. | `place_order(order)`: 주문 전송 및 체결 목록 반환 |

### 3\. MACD 계산 및 기술 지표 (`core/macd_calculator.py`)

| 클래스 | 역할 | 주요 메서드 |
| :--- | :--- | :--- |
| **MacdCalculator** | 캔들 데이터를 이용하여 **EMA, MACD, Signal Line, Histogram**을 계산합니다. 증분 계산을 위한 상태를 저장합니다. | `apply_rows_full(...)`: 전체 데이터로 MACD 계산 |
| | | `apply_append(...)`: 새로운 데이터로 MACD **증분 계산** |
| **MacdCache** | (종목, 시간대)별 **최근 MACD 계산 결과**를 캐시하고 관리합니다. | `save_series(code, tf, series)`: MACD 시계열 데이터 저장 |

### 4\. 설정 및 시뮬레이션 유틸리티

| 클래스 | 파일 | 역할 | 주요 메서드 |
| :--- | :--- | :--- | :--- |
| **SettingsStore** | `settings_manager.py` | Qt `QSettings` 등을 사용하여 애플리케이션 설정 데이터를 저장 및 로드하는 영속성 계층입니다. | `load()` / `save(cfg)`: 설정 객체 로드/저장 |
| **SimEngine** | `sim_engine.py` | 자동 트레이더에 주입되어 제한가 주문의 체결을 **시뮬레이션**합니다. 시장 업데이트에 따라 미체결 주문을 처리합니다. | `on_market_update(event)`: 현재가 기반 체결 여부 확인 및 처리 |
| **StockInfoManager** | `stock_info_manager.py` | CSV 파일에서 KRX **종목 코드, 종목명, 시장 구분** 정보를 로드하고 관리합니다. | `get_name(code)`: 종목 코드를 종목명으로 변환 |