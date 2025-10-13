# Quant Trading Daily Report: {{ date }}

> **리포트 생성 시각:** {{ generated_at }}

---

### Ⅰ. 총평 (Executive Summary)

{{ ai.summary }}

---

### Ⅱ. 전체 성과 대시보드 (Overall Performance)

| 지표 (Metric) | 값 (Value) | 지표 (Metric) | 값 (Value) |
| :--- | :--- | :--- | :--- |
| **순손익 (Net PnL)** | **{{ kpi.net_pnl_abs }}** | **승률 (Win Rate)** | `{{ kpi.win_rate }}` |
| **프로핏 팩터 (Profit Factor)** | `{{ kpi.profit_factor }}` | **손익비 (Payoff Ratio)** | `{{ kpi.payoff_ratio }}` |
| **최대 낙폭 (Max Drawdown)** | `{{ kpi.max_drawdown_pct }}` | **샤프 지수 (Annualized)** | `{{ kpi.sharpe_ratio_annualized }}` |
| 총 거래 횟수 | {{ kpi.total_trades }} 회 | 평균 보유 시간 | {{ kpi.avg_holding_min }} |

---

### Ⅲ. AI 전략 분석 및 제언 (AI Insights & Recommendations)

#### 심층 분석
{{ ai.insight }}

#### 실행 계획 (Action Items)
{{ ai.action_items }}

---

### Ⅳ. 전략별 성과 분석 (Performance by Strategy)

| 전략 (Strategy) | 거래 수 | 순손익 (PnL) | 승률 (Win Rate) | 프로핏 팩터 | 최대 낙폭 (MDD) |
| :--- | :--- | :--- | :--- | :--- | :--- |
{{ table.strategy_rows }}

---

### Ⅴ. 상세 거래 기록 (Trade Log)

| 종목 | 전략 | 수익률 | 손익(원) | 진입 | 청산 | 보유시간 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
{{ table.trade_log_rows }}