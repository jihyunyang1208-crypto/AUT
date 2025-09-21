
# AUT (AutoTrader 오트) 는 시그널 기반의 자동매수매도 솔루션과 전문적인 매매리포트(강의)를 통해 투자자들이 효율적이고 안정적인 수익을 달성할 수 있도록 돕습니다.

AutoTrader
├── core
│   ├── auto_trader.py
│   ├── detail_information_getter.py
│   ├── detail_worker.py
│   ├── macd_calculator.py
│   ├── macd_dialog.py
│   ├── symbol_cache.py
│   ├── token_manager.py
│   ├── broker_base.py          
│   └── websocket_client.py
│
├── brokers/                    # 브로커(실거래/시뮬) 모음
│   ├── broker_live.py          # 한국투자증권 API, 실제 매매
│   ├── broker_sim.py           # 시뮬레이션 전용 (체결가 = 종가 등 룰 기반)
├── execsim
│
├── trade_pro
│   ├── __pycache__
│   ├── adapters
│   ├── entry_exit_monitor.py
│   └── README.md
│
├── kiwoom_api_token
│
├── logs
│   └── app_20250913.log
│
├── static
│
├── strategy
│
├── utils
│
├── .env
├── .gitignore
├── access_token.json
├── candidate_stocks.csv
├── main.py
├── README.md
├── stock_codes.csv
├── ui_main.py
└── 상장법인목록.csv

├── youtube/
│   ├── client_secret.json         # 구글 OAuth 클라이언트(콘솔에서 다운로드)
│   ├── .env                       # 키/ID 환경변수
│   ├── requirements.txt
│   ├── report_daily.py               # 메인 실행 스크립트(매일 돌릴 것)
├── data/
│   └── (자동 생성) system_results_YYYY-MM-DD.json
