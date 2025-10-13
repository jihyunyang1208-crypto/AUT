# 🧾 오트 데일리 매매 리포트 (Daily Trade Report)

> 생성일: {{generated_at}} / 거래일자: **{{date}}** / 모드: **{{mode}}**

---

## 1️⃣ Section A. 전체 하루 단위 복기 (Daily Overview)

### ① 날짜 및 환경 정보
| 항목 | 값 |
|------|----|
| 거래일자 | {{date}} |
| 실행 모드 | {{mode}} |
| 총 매매 횟수 | {{kpi.total_trades}} 회 |
| 총 매수 / 매도 | {{kpi.total_buys}} / {{kpi.total_sells}} |
| 평균 보유시간 | {{kpi.avg_holding_min}} 분 |
| 평균 수익률 | {{kpi.avg_pnl_pct}} |
| 체결 성공률 | {{kpi.fill_success_rate}} |
| 평균 주문 응답 지연 | {{kpi.avg_latency_ms}} |

> 요약: {{daily_summary}}

### ② 시간대별 매매 흐름 요약 (Timeline Summary)
| 시각 (KST) | 종목 | 전략 | 매수/매도 | 진입가 | 청산가 | 수익률 | 사유 |
|------------|------|------|-----------|--------|--------|--------|------|
{{table.timeline_rows}}

### ③ 주요 하이라이트
- ✅ 가장 성공적인 거래: {{highlights.best_trade}}
- ⚠️ 가장 아쉬운 거래: {{highlights.worst_trade}}
- 📈 가장 활발했던 구간: {{highlights.busiest_window}}
- ⚙️ 체결 실패: {{highlights.fail_count}}건
- ⏱️ 최대 API 지연: {{highlights.max_latency_ms}}ms

### ④ 실행 품질 (Execution Quality Report)
| 항목 | 평균(ms) | 중앙(ms) | 최대(ms) | 실패율 | 대표 에러 |
|------|-----------|-----------|-----------|----------|-----------|
| 주문 응답 지연 | {{quality.avg_latency_ms}} | {{quality.median_latency_ms}} | {{quality.max_latency_ms}} | {{quality.fail_rate}} | {{quality.top_errors}} |

### ⑤ 종합 코멘트 (Daily Reflection)
{{daily_reflection}}

---
### 데이터 출처
- system_results JSON: {{meta.system_results_path}}
- trade JSONL: {{meta.trade_jsonl_path}}