# smoke_test_report_generator.py

import json
import os
import sys
from pathlib import Path

# 'daily_report_generator.py'의 'run_report_generation' 함수를 import
# 이 스크립트가 프로젝트 루트에 있다고 가정
try:
    from trading_report.daily_report_generator import run_report_generation
except ImportError as e:
    print(f"오류: 'trading_report' 모듈을 찾을 수 없습니다. ({e})")
    print("이 스크립트를 프로젝트의 최상위 디렉토리에서 실행해주세요.")
    sys.exit(1)


def create_mock_unified_log(test_date: str, root_path: Path) -> Path:
    """
    테스트에 필요한 임시 통합 로그 파일(orderss...jsonl)을 생성합니다.
    """
    logs_dir = root_path / "logs" / "trades"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # 1. 임시 orderss_...jsonl 파일 생성
    log_path = logs_dir / f"orderss_{test_date}.jsonl"
    
    # 신호(signal)와 주문(order) 정보가 통합된 데이터 예시
    mock_unified_data = [
        # 1번 거래: 성공 (신호 + 주문 정보)
        {"ts": f"{test_date}T09:30:00+09:00", "symbol": "005930", "strategy": "TrendFollow", "side": "BUY", 
         "price_entry": 75000, "price_exit": 76500, "pnl_pct": 2.1, "holding_min": 45, "reason": "TakeProfit", 
         "status_label": "SUCCESS", "duration_ms": 150},
         
        # 2번 거래: 실패 (신호 정보는 있으나, 주문 실패)
        {"ts": f"{test_date}T14:15:00+09:00", "symbol": "035720", "strategy": "MeanReversion", "side": "SELL", 
         "price_entry": 88000, "pnl_pct": -1.2, "holding_min": 25, "reason": "StopLoss",
         "status_label": "FAIL", "duration_ms": 400, "response": {"body": {"error": "Order Rejected"}}},
         
        # 3번 거래: 단순 주문 성공 기록 (성과 정보 없음)
        {"ts": f"{test_date}T10:05:00+09:00", "strategy": "TrendFollow", "status_label": "SUCCESS", "duration_ms": 120},
    ]
    
    with log_path.open("w", encoding="utf-8") as f:
        for record in mock_unified_data:
            f.write(json.dumps(record) + "\n")
            
    print(f"임시 통합 로그 생성: {log_path}")
    return log_path


def cleanup_files(files: list):
    """테스트에 사용된 임시 파일들을 삭제합니다."""
    for f_path in files:
        if not isinstance(f_path, Path): f_path = Path(f_path)
        try:
            if f_path.exists():
                os.remove(f_path)
                print(f"임시 파일 삭제: {f_path}")
        except OSError as e:
            print(f"오류: 파일 삭제 실패 {f_path}: {e}")


def main():
    """스모크 테스트 메인 함수"""
    TEST_DATE = "2025-10-11"
    PROJECT_ROOT = Path(__file__).resolve().parent
    
    print("="*50)
    print(f"데일리 리포트(통합 로그) 스모크 테스트 (대상일: {TEST_DATE})")
    print("="*50)
    
    # 1. 테스트 데이터 생성
    mock_log_file = create_mock_unified_log(TEST_DATE, PROJECT_ROOT)
    
    # 2. 리포트 생성 함수 실행
    output_path = PROJECT_ROOT / "reports" / f"daily_report_{TEST_DATE}.md"
    
    test_passed = False
    try:
        print("\n리포트 생성 함수를 실행합니다...")
        run_report_generation(target_date_str=TEST_DATE)
        
        # 3. 결과 확인
        if output_path.exists() and output_path.stat().st_size > 0:
            print(f"\n✅ 테스트 성공: 리포트 파일이 성공적으로 생성되었습니다.")
            print(f"   -> {output_path}")
            test_passed = True
        elif not output_path.exists():
            print(f"\n❌ 테스트 실패: 리포트 파일이 생성되지 않았습니다.")
        else:
            print(f"\n❌ 테스트 실패: 리포트 파일이 생성되었으나 내용이 비어있습니다.")
            
    except Exception:
        import traceback
        print(f"\n❌ 테스트 실패: 리포트 생성 중 예외가 발생했습니다.")
        print(traceback.format_exc())
        
             
    print("\n" + "="*50)
    print("스모크 테스트가 종료되었습니다.")
    print("="*50)
    
    if not test_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()