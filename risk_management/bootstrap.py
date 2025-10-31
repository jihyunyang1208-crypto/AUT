from datetime import timedelta, timezone
from pathlib import Path
import logging, sys

# TradingResultStore 호출
from risk_management.trading_results import TradingResultStore

# --- 로깅 강제 리셋 ---
for h in logging.root.handlers[:]:
    logging.root.removeHandler(h)
logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

logger = logging.getLogger("bootstrap")

KST = timezone(timedelta(hours=9))
BASE_DIR = Path("C:/trade/AutoTrader/logs/results")
BASE_DIR.mkdir(parents=True, exist_ok=True)


def bootstrap_results():
    logger.info("=== bootstrap 시작 ===")
    try:
        store = TradingResultStore(json_path=BASE_DIR / "trading_results.jsonl")
        logger.info(f"store initialized | daily={store.daily_jsonl} | cum={store.cumulative_jsonl}")

        if not store.cumulative_jsonl.exists() or store.cumulative_jsonl.stat().st_size == 0:
            store._append_cumulative_total_event()
            logger.info("[bootstrap] cumulative file created (total)")

        if not store.daily_jsonl.exists() or store.daily_jsonl.stat().st_size == 0:
            store._append_daily_total_event()
            logger.info("[bootstrap] daily file created (total)")

        store.store_updated.emit()
        logger.info("=== bootstrap 완료 ===")
    except Exception as e:
        logger.exception(f"bootstrap 실패: {e}")

if __name__ == "__main__":
    bootstrap_results()
