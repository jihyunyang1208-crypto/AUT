# 전략 성과 분석 대시보드 📊

전문 트레이더를 위한 종합 전략 분석 시스템

## ✨ 주요 기능

### 1️⃣ 시간대별 분석
- **오전 (09:00-12:00)** vs **오후 (12:00-15:30)** 성과 비교
- 전략별 시간대 최적화 추천
- 손익/승률 시각화

### 2️⃣ 실시간 알림 시스템
- 🔴 **연속 손실 경고**: 3회 이상 연속 손실 시 알림
- 🚨 **일일 손실 한도**: -50만원 초과 시 자동 경고
- ⚠️ **Profit Factor 저하**: PF < 1.0 감지 시 알림

### 3️⃣ 전문가급 지표
- **Profit Factor**: 총이익/총손실 비율
- **Sharpe Ratio**: 위험 대비 수익률
- **Max Drawdown**: 최대 손실률
- **평균 이익/손실**: 거래당 평균 성과

### 4️⃣ 다차원 시각화
- ROI 랭킹 차트
- 승률 분포 차트
- Profit Factor 분석
- 시간대별 비교 차트

## 📦 설치

```bash
# 필수 패키지
pip install PySide6 matplotlib

# 프로젝트 구조
your_project/
├── risk_management/
│   ├── trading_results.py
│   └── orders_watcher.py
├── risk_dashboard.py
└── utils/
    └── result_paths.py
```

## 🚀 빠른 시작

### 기본 사용

```python
from PySide6.QtWidgets import QApplication
from risk_dashboard import RiskDashboard

app = QApplication([])

# 대시보드 생성
dashboard = RiskDashboard(
    json_path="results/trade/20241026/trading_result.json",
    poll_ms=2000  # 2초마다 자동 갱신
)

dashboard.show()
app.exec()
```

### 알림 설정

```python
from risk_management.trading_results import TradingResultStore, AlertConfig

# 커스텀 알림 핸들러
def my_alert(alert_type, message, data):
    print(f"⚠️ {message}")
    # 슬랙/텔레그램/이메일 전송
    # send_notification(message)

# 알림 설정
alert_config = AlertConfig(
    enable_pf_alert=True,
    enable_consecutive_loss_alert=True,
    consecutive_loss_threshold=3,      # 연속 손실 임계값
    enable_daily_loss_alert=True,
    daily_loss_limit=-500000.0,        # 일일 손실 한도 (원)
    on_alert=my_alert
)

# 스토어 생성
store = TradingResultStore(
    json_path="results/trade/today/trading_result.json",
    alert_config=alert_config
)
```

## 📊 JSON 출력 예시

```json
{
  "strategies": {
    "모멘텀_A": {
      "buy_notional": 10000000.0,
      "realized_pnl_net": 1235000.0,
      "roi_pct": 12.35,
      "win_rate": 70.0,
      "avg_win": 250000.0,
      "avg_loss": -150000.0,
      "consecutive_losses": 0,
      "daily_loss": 0.0,
      "morning": {
        "realized_pnl": 800000.0,
        "win_rate": 75.0,
        "trades": 4
      },
      "afternoon": {
        "realized_pnl": 435000.0,
        "win_rate": 66.7,
        "trades": 6
      }
    }
  },
  "summary": {
    "realized_pnl_net": 1235000.0,
    "win_rate": 70.0,
    "morning_pnl": 800000.0,
    "afternoon_pnl": 435000.0
  }
}
```

## 🎯 핵심 지표 해석

### Profit Factor
```
PF = 총이익 / 총손실

> 2.0  : 매우 우수 ⭐⭐⭐
1.5-2.0: 우수 ⭐⭐
1.0-1.5: 보통 ⭐
< 1.0  : 개선 필요 ⚠️
```

### Sharpe Ratio
```
SR = (평균 수익 - 무위험 수익) / 수익의 표준편차

> 1.0  : 우수
0.5-1.0: 보통
< 0.5  : 개선 필요
```

### Max Drawdown
```
MDD = 최고점 대비 최대 손실률

< 10%  : 안정적 ✅
10-20% : 관리 필요 ⚠️
> 20%  : 위험 🚨
```

## 🔔 알림 시나리오

### 시나리오 1: 연속 손실
```
[14:35:20] 🔴 전략 '역추세_B': 연속 3회 손실
→ 거래 중단 고려
```

### 시나리오 2: 일일 한도 초과
```
[15:10:45] 🚨 전략 '돌파_D': 일일 손실 -523,000원 (한도 초과)
→ 자동 거래 중단
```

### 시나리오 3: Profit Factor 저하
```
[11:20:15] ⚠️ 전략 '단타_C': Profit Factor 0.85 (손실 누적 중)
→ 전략 재검토 필요
```

## 📈 시간대 분석 활용법

### 1. 오전 집중 전략
```
오전 손익: +800K (승률 75%)
오후 손익: +200K (승률 55%)

💡 추천: 오전 집중, 오후는 관망
```

### 2. 오후 집중 전략
```
오전 손익: -100K (승률 40%)
오후 손익: +600K (승률 70%)

💡 추천: 오전은 관망, 오후 집중
```

### 3. 균형 전략
```
오전 손익: +400K (승률 65%)
오후 손익: +450K (승률 63%)

💡 추천: 하루 종일 균형 배분
```

## 🔧 고급 설정

### CSV 파싱 커스터마이징
```python
from risk_management.orders_watcher import WatcherConfig

config = WatcherConfig(
    base_dir=Path("results/trade"),
    subdir=".",
    file_pattern="orders_{date}.csv",
    watch_interval=1.0
)

dashboard = RiskDashboard(
    json_path="results/trading_result.json"
)
dashboard._watcher_cfg = config
```

### 알림 임계값 조정
```python
# 더 민감한 설정 (단타 전략용)
alert_config = AlertConfig(
    consecutive_loss_threshold=2,      # 2회 연속 손실
    daily_loss_limit=-200000.0,        # -20만원 한도
)

# 더 느슨한 설정 (장기 전략용)
alert_config = AlertConfig(
    consecutive_loss_threshold=5,      # 5회 연속 손실
    daily_loss_limit=-1000000.0,       # -100만원 한도
)
```

## 📝 실전 팁

### 1. 일일 시작 루틴
```python
# 1) 전날 데이터 리빌드
store.rebuild_from_trades(yesterday_trades)

# 2) 대시보드 확인
dashboard.refresh(force=True)

# 3) 알림 테스트
test_alert("TEST", "시스템 정상 작동", {})
```

### 2. 장중 모니터링
- 2초마다 자동 갱신 (poll_ms=2000)
- 알림 패널에서 실시간 경고 확인
- 연속 손실 3회 → 즉시 거래 중단

### 3. 장 마감 분석
```python
# 시간대별 분석
example_time_analysis()

# 전략 재검토
if profit_factor < 1.0:
    print("전략 파라미터 조정 필요")
```

### 4. 주간 백테스트
```python
# 5일간 데이터 분석
example_backtest_analysis()

# 최적 시간대 도출
# → 오전 집중? 오후 집중?
```

## 🐛 트러블슈팅

### Q1: 데이터가 업데이트 안 됨
```python
# A1: 강제 새로고침
dashboard.refresh(force=True)

# A2: 전체 리빌드
trades = load_all_trades_from_csv()
store.rebuild_from_trades(trades)
```

### Q2: 알림이 안 옴
```python
# A: 콜백 연결 확인
store.set_alert_callback(my_alert_handler)

# 테스트
store._check_alerts("test_strategy", test_strategy_state)
```

### Q3: 시간대 데이터 없음
```python
# A: 체결 시간 포맷 확인
# 필요 형식: "2024-10-26T10:30:00" 또는 "10:30:00"
```

## 📚 추가 자료

- [전략 최적화 가이드](docs/strategy_optimization.md)
- [알림 통합 가이드](docs/alert_integration.md)
- [백테스트 분석](docs/backtest_analysis.md)

## 🤝 기여

버그 리포트 및 기능 제안 환영합니다!

## 📄 라이선스

MIT License

-------------

"""
전략 성과 분석 대시보드 사용 예시
- 시간대별 분석
- 실시간 알림
"""
import sys
import logging
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget

from risk_dashboard import RiskDashboard
from risk_management.trading_results import TradingResultStore, TradeRow, AlertConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("전략 성과 분석 시스템")
        self.setGeometry(100, 100, 1400, 900)
        
        # 중앙 위젯
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        
        # JSON 경로 설정
        json_path = Path("results/trade/20241026/trading_result.json")
        json_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 대시보드 생성 (알림 자동 연결됨)
        self.dashboard = RiskDashboard(
            json_path=str(json_path),
            poll_ms=2000  # 2초마다 갱신
        )
        layout.addWidget(self.dashboard)
        
        # 스토어 직접 접근 (테스트용)
        self.store = self.dashboard._store
        
        # 샘플 데이터 추가
        self._add_sample_data()
    
    def _add_sample_data(self):
        """시간대별 + 알림 테스트용 샘플 데이터"""
        logger.info("=== 샘플 데이터 생성 중 ===")
        
        trades = [
            # 전략A - 오전 거래 (성공)
            TradeRow(time="2024-10-26T09:15:00", side="buy", symbol="005930", 
                    qty=100, price=70000, fee=100, strategy="모멘텀_A"),
            TradeRow(time="2024-10-26T09:45:00", side="sell", symbol="005930",
                    qty=100, price=72000, fee=100),
            
            # 전략A - 오전 거래 (성공)
            TradeRow(time="2024-10-26T10:30:00", side="buy", symbol="000660",
                    qty=50, price=120000, fee=150, strategy="모멘텀_A"),
            TradeRow(time="2024-10-26T11:00:00", side="sell", symbol="000660",
                    qty=50, price=125000, fee=150),
            
            # 전략A - 오후 거래 (손실)
            TradeRow(time="2024-10-26T13:00:00", side="buy", symbol="035720",
                    qty=200, price=50000, fee=200, strategy="모멘텀_A"),
            TradeRow(time="2024-10-26T14:00:00", side="sell", symbol="035720",
                    qty=200, price=48000, fee=200),
            
            # 전략B - 오전 거래 (손실 연속)
            TradeRow(time="2024-10-26T09:30:00", side="buy", symbol="051910",
                    qty=80, price=85000, fee=120, strategy="역추세_B"),
            TradeRow(time="2024-10-26T10:00:00", side="sell", symbol="051910",
                    qty=80, price=83000, fee=120),
            
            TradeRow(time="2024-10-26T10:30:00", side="buy", symbol="035420",
                    qty=60, price=95000, fee=100, strategy="역추세_B"),
            TradeRow(time="2024-10-26T11:00:00", side="sell", symbol="035420",
                    qty=60, price=93000, fee=100),
            
            # 전략B - 연속 손실 (알림 발생 예상)
            TradeRow(time="2024-10-26T13:30:00", side="buy", symbol="005380",
                    qty=100, price=45000, fee=150, strategy="역추세_B"),
            TradeRow(time="2024-10-26T14:00:00", side="sell", symbol="005380",
                    qty=100, price=43000, fee=150),
            
            # 전략C - 오후 집중 거래 (성공률 높음)
            TradeRow(time="2024-10-26T13:00:00", side="buy", symbol="000270",
                    qty=150, price=65000, fee=180, strategy="단타_C"),
            TradeRow(time="2024-10-26T13:20:00", side="sell", symbol="000270",
                    qty=150, price=67000, fee=180),
            
            TradeRow(time="2024-10-26T14:00:00", side="buy", symbol="005490",
                    qty=120, price=105000, fee=200, strategy="단타_C"),
            TradeRow(time="2024-10-26T14:30:00", side="sell", symbol="005490",
                    qty=120, price=108000, fee=200),
            
            TradeRow(time="2024-10-26T15:00:00", side="buy", symbol="012330",
                    qty=90, price=78000, fee=120, strategy="단타_C"),
            TradeRow(time="2024-10-26T15:20:00", side="sell", symbol="012330",
                    qty=90, price=80000, fee=120),
            
            # 전략D - 큰 손실 (일일 한도 알림 발생 예상)
            TradeRow(time="2024-10-26T09:00:00", side="buy", symbol="028260",
                    qty=500, price=40000, fee=500, strategy="돌파_D"),
            TradeRow(time="2024-10-26T10:00:00", side="sell", symbol="028260",
                    qty=500, price=38000, fee=500),
            
            TradeRow(time="2024-10-26T14:00:00", side="buy", symbol="036570",
                    qty=400, price=50000, fee=400, strategy="돌파_D"),
            TradeRow(time="2024-10-26T14:30:00", side="sell", symbol="036570",
                    qty=400, price=48000, fee=400),
        ]
        
        # 전체 재계산
        self.store.rebuild_from_trades(trades)
        logger.info("=== 샘플 데이터 생성 완료 ===")
        
        # 강제 새로고침
        self.dashboard.refresh(force=True)


def main():
    """메인 실행"""
    app = QApplication(sys.argv)
    
    # 다크 모드 스타일
    app.setStyle("Fusion")
    from PySide6.QtGui import QPalette, QColor
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(13, 17, 23))
    palette.setColor(QPalette.WindowText, QColor(201, 209, 217))
    palette.setColor(QPalette.Base, QColor(22, 27, 34))
    palette.setColor(QPalette.AlternateBase, QColor(28, 33, 40))
    palette.setColor(QPalette.Text, QColor(201, 209, 217))
    app.setPalette(palette)
    
    window = MainWindow()
    window.show()
    
    logger.info("=== 대시보드 실행 ===")
    logger.info("예상 알림:")
    logger.info("  - 역추세_B: 연속 3회 손실 경고")
    logger.info("  - 돌파_D: 일일 손실 한도 초과 경고")
    logger.info("  - 역추세_B: Profit Factor < 1.0 경고")
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()


# ==================== 추가 사용 예시 ====================

def example_custom_alert_handler():
    """커스텀 알림 핸들러 예시"""
    from risk_management.trading_results import AlertConfig
    
    def my_alert_handler(alert_type: str, message: str, data: dict):
        """커스텀 알림 처리"""
        print(f"[커스텀 알림] {alert_type}: {message}")
        
        # 슬랙/텔레그램 전송
        # send_to_slack(message)
        
        # 이메일 발송
        # send_email(f"거래 경고: {alert_type}", message)
        
        # 로그 파일 기록
        with open("alerts.log", "a", encoding="utf-8") as f:
            from datetime import datetime
            f.write(f"[{datetime.now()}] {alert_type}: {message}\n")
    
    # 스토어 생성 시 알림 설정
    alert_config = AlertConfig(
        enable_pf_alert=True,
        enable_consecutive_loss_alert=True,
        consecutive_loss_threshold=3,
        enable_daily_loss_alert=True,
        daily_loss_limit=-500000.0,
        on_alert=my_alert_handler
    )
    
    store = TradingResultStore(
        json_path="results/trade/20241026/trading_result.json",
        alert_config=alert_config
    )
    
    return store


def example_time_analysis():
    """시간대 분석 예시"""
    import json
    
    # JSON에서 시간대별 데이터 읽기
    with open("results/trade/20241026/trading_result.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    
    strategies = data.get("strategies", {})
    
    print("\n=== 시간대별 성과 분석 ===\n")
    
    for name, s in strategies.items():
        morning = s.get("morning", {})
        afternoon = s.get("afternoon", {})
        
        morning_pnl = morning.get("realized_pnl", 0)
        morning_wr = morning.get("win_rate", 0)
        afternoon_pnl = afternoon.get("realized_pnl", 0)
        afternoon_wr = afternoon.get("win_rate", 0)
        
        print(f"전략: {name}")
        print(f"  오전 (09:00-12:00):")
        print(f"    손익: {morning_pnl:+,.0f}원")
        print(f"    승률: {morning_wr:.1f}%")
        print(f"    거래: {morning.get('trades', 0)}건")
        print(f"  오후 (12:00-15:30):")
        print(f"    손익: {afternoon_pnl:+,.0f}원")
        print(f"    승률: {afternoon_wr:.1f}%")
        print(f"    거래: {afternoon.get('trades', 0)}건")
        
        # 시간대별 선호도 분석
        if morning.get('trades', 0) > 0 and afternoon.get('trades', 0) > 0:
            if morning_wr > afternoon_wr + 10:
                print(f"  ✅ 오전 집중 전략 추천 (승률 차이: +{morning_wr - afternoon_wr:.1f}%)")
            elif afternoon_wr > morning_wr + 10:
                print(f"  ✅ 오후 집중 전략 추천 (승률 차이: +{afternoon_wr - morning_wr:.1f}%)")
        
        print()


def example_alert_monitoring():
    """실시간 알림 모니터링 예시"""
    from datetime import datetime
    
    class AlertMonitor:
        def __init__(self):
            self.alerts = []
        
        def on_alert(self, alert_type: str, message: str, data: dict):
            """알림 수신"""
            self.alerts.append({
                "timestamp": datetime.now(),
                "type": alert_type,
                "message": message,
                "data": data
            })
            
            # 실시간 출력
            print(f"\n🔔 [{datetime.now().strftime('%H:%M:%S')}] {alert_type}")
            print(f"   {message}")
            
            # 심각도별 처리
            if alert_type == "CONSECUTIVE_LOSSES":
                print("   ⚠️  거래 중단을 고려하세요!")
                # auto_stop_trading()
            
            elif alert_type == "DAILY_LOSS_LIMIT":
                print("   🚨 일일 손실 한도 도달! 거래를 중단합니다.")
                # force_stop_all_trading()
            
            elif alert_type == "PROFIT_FACTOR_LOW":
                print("   ℹ️  전략 검토가 필요합니다.")
        
        def get_alert_summary(self):
            """알림 요약"""
            if not self.alerts:
                return "알림 없음"
            
            by_type = {}
            for alert in self.alerts:
                t = alert["type"]
                by_type[t] = by_type.get(t, 0) + 1
            
            summary = []
            for alert_type, count in by_type.items():
                summary.append(f"{alert_type}: {count}건")
            
            return ", ".join(summary)
    
    # 사용
    monitor = AlertMonitor()
    
    alert_config = AlertConfig(
        enable_pf_alert=True,
        enable_consecutive_loss_alert=True,
        consecutive_loss_threshold=2,  # 더 민감하게
        enable_daily_loss_alert=True,
        daily_loss_limit=-300000.0,    # 더 엄격하게
        on_alert=monitor.on_alert
    )
    
    store = TradingResultStore(
        json_path="results/trade/20241026/trading_result.json",
        alert_config=alert_config
    )
    
    # 거래 진행...
    # (자동으로 알림 발생)
    
    # 나중에 요약 확인
    print("\n=== 알림 요약 ===")
    print(monitor.get_alert_summary())
    
    return monitor


def example_backtest_analysis():
    """백테스트용 시간대 최적화"""
    import json
    from collections import defaultdict
    
    # 여러 날의 데이터 수집
    dates = ["20241021", "20241022", "20241023", "20241024", "20241025"]
    
    strategy_time_stats = defaultdict(lambda: {
        "morning": {"pnl": 0, "trades": 0, "wins": 0},
        "afternoon": {"pnl": 0, "trades": 0, "wins": 0}
    })
    
    for date in dates:
        path = f"results/trade/{date}/trading_result.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            strategies = data.get("strategies", {})
            for name, s in strategies.items():
                morning = s.get("morning", {})
                afternoon = s.get("afternoon", {})
                
                stats = strategy_time_stats[name]
                stats["morning"]["pnl"] += morning.get("realized_pnl", 0)
                stats["morning"]["trades"] += morning.get("trades", 0)
                stats["morning"]["wins"] += morning.get("wins", 0)
                stats["afternoon"]["pnl"] += afternoon.get("realized_pnl", 0)
                stats["afternoon"]["trades"] += afternoon.get("trades", 0)
                stats["afternoon"]["wins"] += afternoon.get("wins", 0)
        except FileNotFoundError:
            continue
    
    # 분석 결과
    print("\n=== 시간대 최적화 분석 (5일) ===\n")
    
    for strategy, stats in strategy_time_stats.items():
        morning = stats["morning"]
        afternoon = stats["afternoon"]
        
        morning_wr = (morning["wins"] / morning["trades"] * 100) if morning["trades"] > 0 else 0
        afternoon_wr = (afternoon["wins"] / afternoon["trades"] * 100) if afternoon["trades"] > 0 else 0
        
        print(f"전략: {strategy}")
        print(f"  오전 - 손익: {morning['pnl']:+,.0f}원 | 승률: {morning_wr:.1f}%")
        print(f"  오후 - 손익: {afternoon['pnl']:+,.0f}원 | 승률: {afternoon_wr:.1f}%")
        
        # 추천
        if morning["pnl"] > afternoon["pnl"] * 1.5:
            print(f"  💡 추천: 오전 집중 (수익 {morning['pnl'] / afternoon['pnl']:.1f}배)")
        elif afternoon["pnl"] > morning["pnl"] * 1.5:
            print(f"  💡 추천: 오후 집중 (수익 {afternoon['pnl'] / morning['pnl']:.1f}배)")
        else:
            print(f"  💡 추천: 균형 배분")
        
        print()


# ==================== 실행 예시 ====================

if __name__ == "__main__":
    # 1. 기본 실행
    # main()
    
    # 2. 시간대 분석
    # example_time_analysis()
    
    # 3. 알림 모니터링
    # monitor = example_alert_monitoring()
    
    # 4. 백테스트 분석
    # example_backtest_analysis()
    
    pass