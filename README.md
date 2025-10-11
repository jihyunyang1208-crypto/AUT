다음은 업로드된 주요 클래스 및 데이터 구조체의 역할과 핵심 메서드를 정리한 `README.md` 형식의 문서입니다.

# 핵심 모듈 및 클래스 역할 정리
jihyunyang1208-crypto/aut/AUT-c145bbac9a65f61080fc2293803cd441e0a6f3f0/
├── .cache/
│   └── token_cache.json
├── .env
├── .gitignore
├── README.md
├── __pycache/__  (컴파일된 파이썬 바이트코드 파일)
├── access_token.json
├── candidate_stocks.csv
├── core/
│   ├── __pycache/__
│   ├── broker_base.py          # 브로커 인터페이스 정의
│   ├── detail_information_getter.py
│   ├── detatil_worker.py
│   ├── macd_calculator.py      # MACD 지표 계산 및 캐시
│   ├── macd_dialog.py
│   ├── symbol_cache.py
│   ├── token_manager.py
│   └── websocket_client.py
├── data/
│   └── system_results_2025-09-14.json
├── kiwoom_api_token/           # 키움 API 인증 키
│   ├── 61363913_appkey.txt
│   └── 61363913_secretkey.txt
├── main.py                     # 메인 애플리케이션 시작점 (추정)
├── resources/
│   ├── bullish_analysis_prompt.md
│   ├── daily_briefing_prompt.md
│   └── krx_data.csv            # KRX 종목 정보 데이터
├── risk_management/
│   ├── init.py
│   ├── models.py
│   ├── position_wiring.py
│   └── shared_wallet_pnl.py
├── setting/
│   ├── __pycache/__
│   ├── settings_manager.py     # 애플리케이션 설정 관리 (UI 연동)
│   └── wiring.py
├── simulator/
│   ├── __pycache/__
│   ├── config.py
│   └── sim_engine.py           # 주문/체결 시뮬레이션 엔진
├── smoke_test_autotrader.py
├── static/
│   └── conditions.json
├── stock_codes.csv
├── strategy/
│   ├── __pycache/__
│   ├── filter_1_finance.py
│   └── filter_2_technical.py
├── trade_pro/
│   ├── README.md
│   ├── __pycache/__
│   ├── auto_trader.py          # 핵심 자동 주문 실행 로직
│   ├── auto_trader.py_backup
│   ├── entry_exit_monitor.py
│   ├── exitpro.md
│   └── position_manager.py     # 포지션 및 평단가 관리
├── trading_report/
│   ├── daily_report_generator.py
│   └── daily_report_template.md
├── ui_main.py                  # UI 정의 파일 (추정)
├── ui_main.py_backup
├── utils/
│   ├── __pycache/__
│   ├── gemini_client.py
│   ├── results_store.py
│   ├── stock_info_manager.py   # 종목 정보 조회 유틸리티
│   ├── token_cache.json
│   ├── token_manager.py
│   └── utils.py
├── youtube/
│   ├── .env
│   ├── output/
│   │   └── [2025-09-14] 005930,035420 데일리.md
│   ├── prompt.md
│   ├── report_daily.py
│   └── requirements.txt
└── 상장법인목록.csv







이 문서는 자동 트레이딩 시스템을 구성하는 주요 Python 클래스들의 역할과 핵심 기능을 요약합니다.

---

## 1. 자동 트레이딩 (`trade_pro/auto_trader.py`)

자동 주문 실행 로직을 담당하며, 실거래 및 시뮬레이션 환경을 통합 관리합니다.

| 클래스 | 역할 | 주요 메서드 |
| :--- | :--- | :--- |
| **TradeSettings** (dataclass) | 자동 매매의 기본 설정 (자동 매수/매도 활성화, 주문 유형, 시뮬레이션 모드 등)을 관리하는 데이터 구조체입니다. | N/A |
| **LadderSettings** (dataclass) | 사다리 주문 (매수/매도) 관련 설정 (단위 금액, 분할 수, 틱 간격 등)을 관리합니다. | N/A |
| **TradeLogger** | 모든 주문 및 체결 기록을 지정된 디렉토리에 **CSV** 및 **JSONL** 파일 형식으로 저장 및 관리합니다. | `write_order_record(record)`: 주문 정보를 CSV/JSONL에 기록합니다. |
| **AutoTrader** | 매매 신호를 받아 주문을 실행하거나 시뮬레이션합니다. 브로커 API 통신 및 시뮬레이션 엔진 연동을 담당합니다. | `set_simulation_mode(on)`: 런타임에 시뮬레이션 모드를 활성화/비활성화합니다. |
| | | `handle_signal(payload)`: 외부에서 발생한 매매 신호 (BUY/SELL)를 처리하여 주문을 실행합니다. |
| | | `make_on_signal(bridge)`: 매매 모니터에 주입할 신호 처리 핸들러 함수를 생성합니다. |
| | | `on_ws_message(raw)`: 웹소켓으로 수신된 체결/취소/거부 메시지를 처리하여 포지션 매니저에 반영하고 이벤트를 발생시킵니다. |

---

## 2. 포지션 관리 및 브로커 인터페이스 (`trade_pro/position_manager.py`, `core/broker_base.py`)

종목별 보유 현황 및 브로커 연동을 위한 추상화 계층입니다.

| 클래스 | 파일 | 역할 | 주요 메서드 |
| :--- | :--- | :--- | :--- |
| **PositionManager** | `position_manager.py` | 종목별 **보유 수량**, **평균 매수가**, **미체결 대기 수량** (매수/매도)을 관리하고 파일에 저장하여 지속성을 확보합니다. | `get_qty(symbol)`: 보유 수량 조회. |
| | | | `get_avg_buy(symbol)`: 평균 매수가 조회. |
| | | | `apply_fill_buy/sell(...)`: 매수/매도 체결 결과를 반영합니다. |
| **ITradeBroker** | `broker_base.py` | 실거래 또는 시뮬레이션 브로커가 구현해야 하는 **추상 인터페이스**입니다. | `place_order(order)`: 주문을 전송하고 체결 목록을 반환합니다 (필수 구현). |
| | | | `get_positions()`: 현재 보유 포지션 스냅샷을 반환합니다 (필수 구현). |

---

## 3. MACD 계산 및 캐시 (`core/macd_calculator.py`)

MACD 기술 지표를 계산하고 결과를 캐시하는 기능을 제공합니다.

| 클래스 | 역할 | 주요 메서드 |
| :--- | :--- | :--- |
| **MacdBus** (QObject) | MACD 계산 완료 결과를 다른 컴포넌트에 **시그널**로 전달하는 역할을 합니다. | `macd_series_ready`: MACD 결과가 준비되었음을 알리는 시그널. |
| **MacdCache** | (종목, 시간대)별 **최근 MACD 계산 결과**를 저장하고 중복 및 시간 역행을 관리합니다. | `save_series(code, tf, series)`: MACD 시계열 데이터를 캐시에 저장합니다. |
| | | `get_points(code, tf, n)`: 최근 N개의 MACD 포인트 (지표 값)를 반환합니다. |
| **MacdCalculator** | 캔들 데이터를 이용하여 **EMA**, **MACD**, **Signal Line**, **Histogram**을 계산하고, 증분 계산을 위한 상태를 저장합니다. | `apply_rows_full(...)`: 전체 데이터로 MACD를 계산하고 상태를 초기화합니다. |
| | | `apply_append(...)`: 새로운 캔들 데이터를 이용하여 MACD를 **증분 계산**합니다. |

---

## 4. 설정 및 유틸리티

애플리케이션 설정 관리, 시뮬레이션 엔진, 종목 정보 관리를 위한 클래스입니다.

| 클래스 | 파일 | 역할 | 주요 메서드 |
| :--- | :--- | :--- | :--- |
| **AppSettings** (dataclass) | 앱의 전체 설정을 담는 데이터 모델입니다. | `from_env()`: 환경 변수에서 초기값을 로드합니다. |
| **SettingsStore** | `settings_manager.py` | Qt `QSettings`를 사용하여 설정 데이터를 저장 및 로드하는 영속성 계층입니다. | `load()` / `save(cfg)`: 설정 객체를 파일에 로드/저장합니다. |
| **SettingsDialog** (QDialog) | 사용자에게 설정을 보여주고 편집할 수 있도록 하는 UI 다이얼로그입니다. | `get_settings()`: UI에서 편집된 값을 `AppSettings` 객체로 반환합니다. |
| **SimEngine** | `sim_engine.py` | 자동 트레이더에 주입되어 제한가 주문의 체결을 **시뮬레이션**합니다. | `on_market_update(event)`: 현재가를 기반으로 미체결 주문의 체결 여부를 확인하고 처리합니다. |
| **StockInfoManager** | `stock_info_manager.py` | CSV 파일에서 KRX **종목 코드**, **종목명**, **시장 구분** 정보를 로드하고 관리하는 싱글톤 객체입니다. | `get_name(code)`: 종목 코드를 종목명으로 변환하여 반환합니다. |