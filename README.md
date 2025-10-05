
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


# 파일별 클래스
## UI MainWindow 클래스 구조 정리

`ui_main.py`의 주요 클래스와 메서드 역할

---

## DataFrameModel

| 메서드 | 역할 | 비고 |
|--------|------|------|
| `__init__(df, parent)` | 초기화, 내부 `DataFrame` 보관 | 복사 저장 |
| `setDataFrame(df)` | 모델에 `DataFrame` 바인딩/갱신 | `beginResetModel()`/`endResetModel()` 사용 |
| `rowCount(parent)` | 행 개수 반환 | Qt 테이블 모델 표준 |
| `columnCount(parent)` | 열 개수 반환 | Qt 테이블 모델 표준 |
| `data(index, role)` | 셀 표시 데이터/툴팁 | `NaN` → 빈 문자열 처리 |
| `headerData(section, orientation, role)` | 헤더 텍스트 반환 | 가로 방향에서 컬럼명 표시 |

---

## MainWindow

### 시그널
- `sig_new_stock_detail: Signal(dict)` → 워커/엔진 스레드에서 종목 상세를 UI 스레드로 전달  
- `sig_trade_signal: Signal(dict)` → 매매 체결/시그널 이벤트를 UI 스레드로 전달  

### 라이프사이클 / 초기화
| 메서드 | 역할 |
|--------|------|
| `__init__` | UI 초기화 및 의존성 주입, 상태/스타일/시계/시그널 구성, 후보 로드, 설정 복원 |
| `closeEvent(event)` | 창 종료 시 상태 저장(QSettings), 엔진/스트림 정리, 설정 저장 |
| `_start_clock()` | 상태바 시계 1초 갱신 |

### UI 빌드/스타일
| 메서드 | 역할 |
|--------|------|
| `_build_toolbar()` | 툴바/메뉴 액션 구성 |
| `_build_layout()` | 좌측 조건, 우측 탭(후보/검색결과), 하단 로그, 우측 리스크 홀더 |
| `_build_risk_panel()` | 리스크 대시보드(KPI, 익스포저, 차트, 전략 카드 뷰) 구성 |
| `_apply_stylesheet()` | 다크테마 스타일 적용 |

### 시그널 연결/브리지
| 메서드 | 역할 |
|--------|------|
| `_connect_signals()` | 버튼/입력/브리지 시그널 연결 |
| `_on_token_ready(token)` | 토큰 수신 후 API 객체 생성/갱신 |
| `on_initialization_complete()` | 엔진 초기화 완료 알림 |

### 리스크/포트폴리오 표시
| 메서드 | 역할 |
|--------|------|
| `on_pnl_snapshot(snap)` | 포트폴리오 스냅샷 수신 → KPI/배지/게이지/차트/전략카드 갱신 |
| `_apply_risk_badge(level)` | SAFE/WARN/DANGER 배지 스타일 변경 |
| `_update_exposure_gauge(pct)` | 총 익스포저(%) 게이지 값/툴팁 설정 |
| `_risk_level(...)` | 손익/낙폭/익스포저로 위험등급 산출 |

### 전략 카드 뷰
| 메서드 | 역할 |
|--------|------|
| `_create_strategy_card(cond_id)` | 전략 카드 생성 |
| `_paint_strategy_card(card, daily_pct)` | 손익 구간에 따른 카드 색상 적용 |
| `_update_strategy_cards(by_cond)` | 조건별 카드 추가/갱신/제거 |

### 조건/후보/결과 렌더
| 메서드 | 역할 |
|--------|------|
| `populate_conditions(conditions)` | 조건식 목록 표준화 후 리스트 채우기 |
| `load_candidates(path)` | CSV 후보 로드/정규화/모델 세팅 |
| `_render_results_html()` | 종목 검색 결과 HTML 렌더링 |
| `_on_result_anchor_clicked(url)` | 상세 링크 클릭 시 MACD 다이얼로그 열기 |

### MACD/실시간 관련
| 메서드 | 역할 |
|--------|------|
| `_ensure_macd_stream(code6)` | 코드별 MACD 스트림 시작 보장 |
| `_open_macd_dialog(code)` | MACD 상세 다이얼로그 열기 |
| `on_macd_data(...)` | MACD 실시간 값 상태바 표시 |
| `on_macd_series_ready(data)` | MACD 시리즈 수신 확장 포인트 |

### 신규 종목/매매 시그널
| 메서드 | 역할 |
|--------|------|
| `on_new_stock(code)` | 신규 종목 수신 시 상태바/라벨 갱신 |
| `on_new_stock_detail(payload)` | 종목 상세정보 갱신 및 MACD 스트림 보장 |
| `on_trade_signal(payload)` | 매수/매도 가격 갱신 |

### 버튼 핸들러
| 메서드 | 역할 |
|--------|------|
| `on_click_init()` | 엔진 초기화 |
| `on_click_start_condition()` | 조건 실시간 시작 요청 |
| `on_click_stop_condition()` | 조건 실시간 중지 요청 |
| `on_click_filter()` | 필터 실행 후 후보 갱신 |
| `on_open_settings_dialog()` | 환경설정 열기/저장/적용 |

### 검색/필터/정보 라벨
| 메서드 | 역할 |
|--------|------|
| `_filter_conditions(text)` | 조건식 리스트 필터 |
| `_filter_candidates(text)` | 후보 테이블 프록시 필터 |
| `_update_cond_info()` | “총 개수 / 선택” 라벨 갱신 |

### 로그/유틸
| 메서드 | 역할 |
|--------|------|
| `append_log(text)` | 로그창 출력(시각 포함) |
| `_fmt_num(v, digits)` | 숫자 포맷 유틸 |

### 스레드-세이프 프록시
| 메서드 | 역할 |
|--------|------|
| `threadsafe_new_stock_detail(payload)` | 스레드세이프 종목 상세 갱신 |
| `threadsafe_trade_signal(payload)` | 스레드세이프 매매 신호 갱신 |

### 리스크 패널 토글
| 메서드 | 역할 |
|--------|------|
| `_toggle_risk_panel(visible)` | 리스크 패널 표시/숨김 토글 |
