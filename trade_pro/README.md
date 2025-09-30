
# 핵심 요약
### 역할: 5분봉 마감 근사 시각에 매수/매도 룰을 평가하고, 신호를 내보냅니다.

### 입력:

- DetailInformationGetter.get_bars(code, "5m", count) → tz-aware OHLCV DataFrame

- IMacdPointsFeed.get_points(symbol, "30m", 1) → 최근 30분 MACD 포인트(전역 MACD 캐시)

### 출력:

- on_signal(TradeSignal) 콜백 호출

- DailyResultsRecorder가 JSON(data/system_results_YYYY-MM-DD.json) 파일로 즉시 기록

### 보조 규칙:

- 동일 봉 중복 트리거 방지

- MACD 신선도 체크(기본 1800초)

- 초기 MACD 캐시 비어 있음 → 안전하게 skip 후 다음 사이클


# 구조 개요

## (1) 초기화/워밍업
```mermaid

sequenceDiagram
  autonumber
  participant Runner as "App / Runner"
  participant Monitor as "ExitEntryMonitor"
  participant Bars as "DetailInformationGetter (5m DF 공급)"
  participant MACDCalc as "macd_calculator (apply_rows_full/append)"
  participant MACDFeed as "IMacdPointsFeed (get_points)"
  participant Recorder as "DailyResultsRecorder"
  participant Bridge as "UI Bridge (optional)"

  Note over Runner: 초기화 & 배선
  Runner->>Bars: 인스턴스 생성 (토큰 등 설정)
  Runner->>MACDFeed: (주입 대상 준비: get_points 제공자)
  Runner->>Recorder: 인스턴스 생성(파일 경로/타임존)
  Runner->>Monitor: detail_getter, macd_feed, symbols, settings 주입

  Note over Runner,MACDCalc: (선택) 시작 시 MACD 캐시 워밍업
  Runner->>MACDCalc: apply_rows_full(code, "5m", rows)
  Runner->>MACDCalc: apply_rows_full(code, "30m", rows)
  MACDCalc-->>MACDFeed: 전역 macd_cache 채움

  Runner->>Monitor: start() 호출 (async 루프 진입)
  Monitor-->>Runner: 모니터링 시작 로그

```


## (2) 루프 내 흐름
```mermaid
sequenceDiagram
  autonumber
  participant Monitor as "ExitEntryMonitor"
  participant Time as "TimeRules"
  participant Bars as "DetailInformationGetter (5m DF 공급)"
  participant MACDFeed as "IMacdPointsFeed (get_points)"
  participant Sell as "SellRules"
  participant Buy as "BuyRules"
  participant Recorder as "DailyResultsRecorder"
  participant Bridge as "UI Bridge (optional)"

  loop poll_interval_sec 주기 루프
    Monitor->>Time: is_5m_bar_close_window(now_kst)?
    alt 5분봉 마감 근사 구간 아님
      Time-->>Monitor: False
      Note over Monitor: 대기 (sleep poll_interval_sec)
    else 5분봉 마감 근사 구간
      Time-->>Monitor: True
      par 각 심볼 s ∈ symbols
        Note over Monitor: 심볼 s 평가 시작
        Monitor->>Bars: get_bars(code=s, interval="5m", count=N)
        Bars-->>Monitor: 5m OHLCV DataFrame (tz-aware)

        alt 데이터 없음/부족
          Note over Monitor: df5 empty 또는 len<2 → skip
        else 데이터 충분
          Note over Monitor: ref_ts = 마지막 캔들 시각<br/>last_close, prev_open 계산
          alt use_macd30_filter == True
            Monitor->>MACDFeed: get_points(s, "30m", 1)
            MACDFeed-->>Monitor: 최근 30m MACD 포인트 or []
            alt 포인트 없음(초기 캐시 비어있음)
              Note over Monitor: "MACD30 not ready yet → skip this bar"
            else 포인트 존재
              Note over Monitor: hist>=0 확인 + 신선도(age_sec≤max_age)
              alt 신선도 초과 또는 hist<0
                Note over Monitor: 필터 실패 → skip
              else 필터 통과
                Monitor->>Sell: sell_if_close_below_prev_open(df5)
                Sell-->>Monitor: True/False
                alt SELL 조건 충족
                  Note over Monitor: 동일 봉 중복 트리거 체크
                  Monitor->>Bridge: (optional) 로그 emit
                  Monitor->>Recorder: record_signal(SELL, s, ref_ts, last_close, reason)
                  Recorder-->>Monitor: 저장 완료(JSON 누적)
                end

                Monitor->>Buy: buy_if_5m_break_prev_bear_high(df5)
                Buy-->>Monitor: True/False
                alt BUY 조건 충족
                  Note over Monitor: (필요 시) MACD 필터 재확인
                  Monitor->>Bridge: (optional) 로그 emit
                  Monitor->>Recorder: record_signal(BUY, s, ref_ts, last_close, reason)
                  Recorder-->>Monitor: 저장 완료(JSON 누적)
                end
              end
            end
          else MACD 필터 비활성
            Monitor->>Sell: sell_if_close_below_prev_open(df5)
            Sell-->>Monitor: True/False
            alt SELL 조건 충족
              Monitor->>Bridge: (optional) 로그 emit
              Monitor->>Recorder: record_signal(SELL, s, ref_ts, last_close, reason)
            end

            Monitor->>Buy: buy_if_5m_break_prev_bear_high(df5)
            Buy-->>Monitor: True/False
            alt BUY 조건 충족
              Monitor->>Bridge: (optional) 로그 emit
              Monitor->>Recorder: record_signal(BUY, s, ref_ts, last_close, reason)
            end
          end
        end
      and (다음 심볼 병렬 처리)
      end
    end
  end
```