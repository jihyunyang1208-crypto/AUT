┌──────────────────┐        append 라인        ┌───────────────────────┐
│  주문작성자/브릿지 │ ───────────────────────▶ │  orders_YYYY-MM-DD.csv │
└──────────────────┘                           └───────────────────────┘
                                                       │ tail (poll)
                                                       ▼
                                             ┌──────────────────────┐
                                             │  OrdersCSVWatcher    │
                                             │  - 부분라인 보류     │
                                             │  - 롤오버 감지       │
                                             └─────────┬────────────┘
                            BUY 체결 콜백(on_buy_fill) │  │ SELL 체결 콜백(on_sell_fill)
                                                       │  │
                     ┌─────────────────────────────────┘  └─────────────────────────────────┐
                     ▼                                                                        ▼
        ┌───────────────────────────┐                                         ┌───────────────────────────┐
        │ PositionManager           │                                         │ PositionManager           │
        │ apply_fill_buy_with_result│                                         │ apply_fill_sell_with_result
        │ → (new_qty, new_avg)      │                                         │ → (realized, new_qty, avg)│
        └───────────┬───────────────┘                                         └───────────┬───────────────┘
                    │                                                                       │
                    │                                                                       │
                    ▼                                                                       ▼
        ┌───────────────────────────┐                                         ┌───────────────────────────┐
        │ TradingResultStore        │                                         │ TradingResultStore        │
        │ record_buy(...)           │                                         │ record_sell(...)          │
        │  - fees/qty/avg 반영      │                                         │  - 실현손익/fees 반영      │
        │  - summary 갱신           │                                         │  - summary(승률 등) 갱신   │
        │  - trading_result.json 저장 (원자적 rename)                         │  - trading_result.json 저장
        └───────────────────────────┘                                         └───────────────────────────┘
                               ▲                                                          ▲
                               │                                                          │
                     ┌─────────┴─────────┐                                    ┌───────────┴───────────┐
                     │ Risk Dashboard     │  polling/구독 → trading_result.json │ Daily Report Generator │
                     │ (UI 표시)          │ ───────────────────────────────────▶ │ (파일/문서 출력)        │
                     └────────────────────┘                                    └─────────────────────────┘
