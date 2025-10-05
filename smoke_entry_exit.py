# smoke_entry_exit.py
import asyncio
from dataclasses import dataclass
from pathlib import Path
import pandas as pd

# 테스트 대상 모듈 import (경로에 맞게 수정)
from trade_pro.entry_exit_monitor import (
    ExitEntryMonitor,
    DailyResultsRecorder,
    TradeSignal,
    DetailGetter,
)

# --- 1) 더미 DetailGetter: 5분봉 2개짜리 DF를 반환 --------------------------
class FakeDetailGetter:
    async def get_bars(self, code: str, interval: str, count: int) -> pd.DataFrame:
        # 보통은 캐시로 먹이지만, 혹시 호출되면 최소 동작 보장
        return make_test_5m_df()

def make_test_5m_df():
    tz = "Asia/Seoul"
    # 5분 스냅으로 깔끔히 맞춰줌
    now = pd.Timestamp.now(tz=tz).ceil("5min")
    idx = pd.DatetimeIndex([now - pd.Timedelta(minutes=5), now], tz=tz)

    # 조건:
    # - 직전봉 prev: 음봉(Open=100, Close=90), High=105
    # - 현재봉 last: 약한 양/음 상관없이 Close=95 (prev_open=100보다 작아서 SELL 조건 충족)
    df = pd.DataFrame(
        {
            "Open":   [100.0,  92.0],
            "High":   [105.0,  99.0],
            "Low":    [  89.0,  90.0],
            "Close":  [ 90.0,  95.0],   # last_close(95) < prev_open(100) → SELL 발생
            "Volume": [ 10000, 12000],
        },
        index=idx,
    )
    return df

# --- 2) on_signal 콜백: 신호가 오면 콘솔에 찍기 -----------------------------
def print_signal(sig: TradeSignal):
    print(
        f"SIG | side={sig.side} symbol={sig.symbol} ts={sig.ts} "
        f"price={sig.price} reason={sig.reason} source={getattr(sig,'source','bar')}"
    )

# --- 3) 메인 시나리오 -------------------------------------------------------
async def main():
    # 결과 폴더 준비
    out_dir = Path("tmp_results")
    out_dir.mkdir(exist_ok=True)

    # 레코더 & 더미 게터
    rec = DailyResultsRecorder(str(out_dir))
    dg = FakeDetailGetter()

    mon = ExitEntryMonitor(
        detail_getter=dg,
        on_signal=print_signal,
        # 스모크 테스트에서는 5분봉 마감창 제약을 풀어 바로 평가되게 함
        bar_close_window_start_sec=0,
        bar_close_window_end_sec=59,
        # 캐시를 써서 확실히 동작시키기 위해 서버 pull은 끄고, 아래에서 수동 주입
        disable_server_pull=True,
        results_recorder=rec,
    )

    # 005930(삼성전자)로 테스트
    symbol = "073010"
    df5 = make_test_5m_df()
    mon.ingest_bars(symbol, "5m", df5)  # 캐시에 주입

    # 3-1) 기존 경로: 봉 평가 → BUY/SELL 신호 발생
    await mon._check_symbol(symbol)

    # 3-2) (선택) 조건검색 즉시 트리거 경로
    await mon.on_condition_detected(symbol, condition_name="급등감지")

    # 3-3) JSONL 직접 기록 경로 확인
    rec.record_signal(
        TradeSignal("BUY", symbol, pd.Timestamp.now(tz="Asia/Seoul"), 70000, "manual smoke")
    )

    # 결과 파일 확인
    today = pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d")
    fpath = out_dir / f"system_results_{today}.jsonl"
    print(f"\n== JSONL saved: {fpath} ==")
    if fpath.exists():
        print(fpath.read_text(encoding="utf-8"))
    else:
        print("결과 파일이 생성되지 않았습니다.")

if __name__ == "__main__":
    asyncio.run(main())
