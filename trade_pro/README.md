# 동작 흐름 설명
1) 의존성 주입(재사용)

DetailInformationGetter: KA10081 5분봉 캔들을 가져옵니다. (이미 보유한 클래스를 그대로 주입)

IMacdDialogFeedAdapter: MacdDialog가 계산/보유한 30분 MACD (macd, signal, hist, ts) 최신값을 그대로 제공합니다. (재계산 없음)

2) 평가 타이밍

매 루프마다 현재 시각을 확인하여 **5분봉 마감 구간(분%5==0 & 5~30초)**에만 신호 평가를 수행합니다.
→ 체결/수신 지연을 고려해 버퍼를 둔 방식.

3) 매도/매수 판정

매도 기본 룰: Close[t] <= Open[t-1] → SellRules.sell_if_close_below_prev_open

매수 예시 룰(교체 가능): 전 봉 음봉 + 현재 봉 양봉 + 현재 High>전 High → BuyRules.buy_if_5m_break_prev_bear_high

4) MACD 30분 필터(옵션)

설정 use_macd30_filter=True일 때만 적용.

IMacdDialogFeedAdapter.get_latest(symbol, "30m")로 MACD 최신값을 받아 hist ≥ 0인지 확인.

값이 없거나 **신선도(기본 120초)**를 초과하면 보수적으로 통과 실패로 처리 → 신호 차단.

5) 중복 트리거 방지

같은 (종목, 매수/매도) 조합에 대해 동일 5분봉 ts에서는 1회만 발행되도록 _last_trig으로 관리.

6) 신호 후처리 훅

on_signal(TradeSignal) 콜백에서 실제 주문(키움 REST), 로그, 텔레그램 알림 등을 수행합니다.
→ 모니터러는 “판정”까지만 담당, 주문은 외부로 위임(느슨한 결합).

# 교체/확장 포인트

매수/매도 룰 교체: BuyRules, SellRules에 원하는 전략으로 간단히 치환.

MACD 필터 끄기: use_macd30_filter=False면 30분 MACD 조건 미적용.

리샘플 대안: 굳이 필요하면 5분봉→30분봉 리샘플로 MACD를 계산하도록 확장 가능(현재는 MacdDialog 값 그대로 사용 기조).

마감 구간 튜닝: 거래소/API 지연에 맞춰 TimeRules.is_5m_bar_close_window의 초 버퍼 조정.