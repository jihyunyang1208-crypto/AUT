# risk_management/orders_watcher.py
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from glob import glob
from pathlib import Path
from typing import Dict, Optional, Iterable, Iterator, List

from PySide6.QtCore import QObject, QTimer, QElapsedTimer

from .trading_results import TradingResultStore, TradeRow

# -------------------- Logging --------------------
logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

# ---------- 설정 ----------
KST = timezone(timedelta(hours=9))

# 헤더 후보(캐논키 → 실제파일 헤더명 후보)
HEADER_CANDIDATES = {
    "time":   ["ts", "time", "order_time", "exec_time", "filled_at", "timestamp", "체결시각"],
    "side":   ["action", "side", "buy_sell", "bs", "direction", "매매구분"],
    "symbol": ["stk_cd", "symbol", "ticker", "code", "종목코드"],
    "qty":    ["qty", "quantity", "filled_qty", "exec_qty", "수량"],
    "price":  ["price", "exec_price", "avg_price", "체결가", "가격"],
    "fee":    ["fee", "commission", "comm", "수수료"],
    "status": ["status", "state", "order_status", "exec_status", "상태"],
    "strategy": ["strategy", "cond", "조건식"],
}

def _best_header_map(headers: Iterable[str]) -> Dict[str, str]:
    ah = [h.strip() for h in headers]
    ah_lower = [h.lower() for h in ah]
    m: Dict[str, str] = {}
    for canon, cands in HEADER_CANDIDATES.items():
        found = None
        for c in cands:
            if c.lower() in ah_lower:
                found = ah[ah_lower.index(c.lower())]
                break
        m[canon] = found or canon
    logger.debug(f"[header-map] mapped={m}")
    return m

def _to_int(s: str) -> int:
    try:
        return int(round(float(str(s).replace(",", "").strip())))
    except Exception:
        return 0

def _to_float(s: str) -> float:
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return 0.0

def _to_float_soft(s: str) -> float:
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return 0.0

def _normalize_side(v: str) -> Optional[str]:
    v = (v or "").strip().lower()
    if v in ("매수", "buy", "b", "long", "입고", "buytoopen", "bto"):
        return "buy"
    if v in ("매도", "sell", "s", "short", "출고", "sellshort", "stc", "buytoclose"):
        return "sell"
    if v.startswith("b"):
        return "buy"
    if v.startswith("s"):
        return "sell"
    return None

def _infer_side(side_text: str, qty: int) -> str:
    side = _normalize_side(side_text)
    if side is not None:
        return side
    inferred = "sell" if qty < 0 else "buy"
    logger.debug(f"[infer] side by qty → {inferred} (raw='{side_text}', qty={qty})")
    return inferred

def _pick_any(raw: dict, hdr_map: dict, keys: List[str], default: str = "") -> str:
    """캐논키 리스트 중 파일에 실제 존재하는 첫 키의 값을 반환"""
    for k in keys:
        real = hdr_map.get(k, k)
        val = (raw.get(real, "") or raw.get(k, "") or "").strip()
        if val != "":
            return val
    return default

def _pick_encoding(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp949"):
        try:
            path.read_text(encoding=enc)
            logger.debug(f"[encoding] {path.name}: {enc}")
            return enc
        except Exception:
            continue
    logger.debug(f"[encoding] {path.name}: fallback utf-8")
    return "utf-8"

def _sniff_delim(path: Path) -> str:
    try:
        enc = _pick_encoding(path)
        head = "".join(path.read_text(encoding=enc).splitlines(True)[:2])
        dialect = csv.Sniffer().sniff(head)
        logger.debug(f"[delimiter] {path.name}: {dialect.delimiter!r} (sniffed)")
        return dialect.delimiter
    except Exception:
        enc = _pick_encoding(path)
        txt = path.read_text(encoding=enc)
        delim = "\t" if txt.count("\t") > max(txt.count(","), txt.count(";")) else ("," if txt.count(",") >= txt.count(";") else ";")
        logger.debug(f"[delimiter] {path.name}: {delim!r} (heuristic)")
        return delim

# ---------- 전수 재계산 유틸 ----------
def iter_trades_from_csv(path: Path) -> Iterator[TradeRow]:
    """
    파일 헤더(실제 순서)를 DictReader.fieldnames로 사용.
    status는 통과, side는 모호하면 추론. symbol/price만 비면 무효.
    """
    if not path.exists() or not path.is_file():
        logger.info(f"[csv-scan] skip (missing): {path}")
        return iter(())

    enc = _pick_encoding(path)
    delim = _sniff_delim(path)

    total = 0
    inferred_side_cnt = 0
    invalid_cnt = 0

    logger.info(f"[csv-scan] start file={path.name} enc={enc} delim={delim!r}")

    def _gen() -> Iterator[TradeRow]:
        nonlocal total, inferred_side_cnt, invalid_cnt
        with open(path, "r", encoding=enc) as fh:
            head = fh.readline()
            if not head:
                logger.info(f"[csv-scan] empty header: {path.name}")
                return
            reader = csv.reader(io.StringIO(head), delimiter=delim)
            header = next(reader, None)
            if not header:
                logger.info(f"[csv-scan] header not found: {path.name}")
                return
            hdr_map = _best_header_map(header)

            for line in fh:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                try:
                    # ★ 헤더는 파일의 실제 헤더를 그대로 사용
                    dr = csv.DictReader(io.StringIO(line), fieldnames=header, delimiter=delim)
                    raw = next(dr)

                    qty_raw = _to_int(_pick_any(raw, hdr_map, ["qty", "quantity", "filled_qty", "exec_qty", "수량"], "0"))
                    price_txt = _pick_any(raw, hdr_map, ["price", "exec_price", "avg_price", "체결가", "가격"], "0")
                    price = _to_float_soft(price_txt)
                    symbol = _pick_any(raw, hdr_map, ["symbol", "stk_cd", "ticker", "code", "종목코드"])
                    fee = _to_float(_pick_any(raw, hdr_map, ["fee", "commission", "comm", "수수료"], "0"))
                    side_text = _pick_any(raw, hdr_map, ["side", "action", "buy_sell", "bs", "direction", "매매구분"])

                    if not symbol or price <= 0.0:
                        invalid_cnt += 1
                        logger.debug(f"[csv-scan] invalid (symbol/price) symbol='{symbol}' price_txt='{price_txt}'")
                        continue

                    side = _normalize_side(side_text)
                    if side is None:
                        side = _infer_side(side_text, qty_raw)
                        inferred_side_cnt += 1

                    qty = abs(qty_raw) if qty_raw != 0 else 0
                    status = _pick_any(raw, hdr_map, ["status", "state", "order_status", "exec_status", "상태"], "filled") or "filled"

                    total += 1
                    if total % 1000 == 0:
                        logger.debug(f"[csv-scan] {path.name} parsed={total} inferred_side={inferred_side_cnt} invalid={invalid_cnt}")

                    yield TradeRow(
                        time=_pick_any(raw, hdr_map, ["time", "ts", "order_time", "exec_time", "filled_at", "timestamp", "체결시각"]),
                        side=side,
                        symbol=symbol,
                        qty=qty,
                        price=price,
                        fee=fee,
                        status=status,
                        strategy=_pick_any(raw, hdr_map, ["strategy", "cond", "조건식"]) or None,
                        meta=None,
                    )
                except Exception as e:
                    invalid_cnt += 1
                    logger.debug(f"[csv-scan] parse error: {e}")
                    continue

    try:
        for row in _gen():
            yield row
    finally:
        logger.info(
            f"[csv-scan] done file={path.name} parsed={total} "
            f"inferred_side={inferred_side_cnt} invalid={invalid_cnt}"
        )

@dataclass
class WatcherConfig:
    base_dir: Path = Path(__file__).resolve().parent.parent / "logs"
    file_pattern: str = "orders_{date}.csv"
    subdir: str = "trades"
    poll_ms: int = 700
    json_path: Path = Path(__file__).resolve().parent / "data" / "trading_result.json"
    bootstrap_if_missing: bool = True

def rebuild_store_from_all_csv(store: TradingResultStore, cfg: WatcherConfig) -> int:
    base = (cfg.base_dir / cfg.subdir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    pattern_glob = str(base / cfg.file_pattern.replace("{date}", "*"))
    candidates = sorted(glob(pattern_glob))

    logger.info(f"[rebuild-all] files={len(candidates)} dir={base}")

    all_trades: List[TradeRow] = []
    total = 0
    for i, p in enumerate(candidates, 1):
        path = Path(p)
        before = len(all_trades)
        for t in iter_trades_from_csv(path):
            all_trades.append(t)
        added = len(all_trades) - before
        total += added
        logger.info(f"[rebuild-all] ({i}/{len(candidates)}) {path.name} +{added} (cum={total})")

    store.rebuild_from_trades(all_trades)
    logger.info(f"[rebuild-all] completed total_trades={total}")
    return total

# ---------- 실시간 tail Watcher (메인스레드용) ----------
class OrdersCSVWatcher(QObject):
    """
    메인스레드에서 QTimer로 tail 처리.
    - 틱당 처리 라인/시간 제한으로 UI 멈춤 방지
    - status 무시/side 자동추론으로 스킵 최소화
    - ★ DictReader.fieldnames는 항상 '실제 파일 헤더' 사용
    """
    MAX_LINES_PER_TICK = 500
    MAX_MSEC_PER_TICK = 15  # ms

    def __init__(self, store: Optional[TradingResultStore] = None, config: Optional[WatcherConfig] = None, parent=None):
        super().__init__(parent)
        self.cfg = config or WatcherConfig()
        self.store = store or TradingResultStore(str(self.cfg.json_path))

        self._timer = QTimer(self)
        self._timer.setInterval(int(self.cfg.poll_ms))
        self._timer.timeout.connect(self._on_tick)

        self._cur_path: Optional[Path] = None
        self._fh = None
        self._buffer = ""  # partial line buffer
        self._hdr_map: Optional[Dict[str, str]] = None
        self._header: Optional[List[str]] = None   # ★ 실제 파일 헤더 보관
        self._delimiter = ","
        self._skip_initial_backfill = False

        # 진행 카운터
        self.total_applied: int = 0
        self.total_invalid: int = 0
        self.total_lines_read: int = 0
        self.total_inferred_side: int = 0

        # 초기 파일 오픈
        self._open_today_file()

        # 부트스트랩
        try:
            if self.cfg.bootstrap_if_missing and not Path(self.cfg.json_path).exists():
                logger.info("[watcher] bootstrap missing trading_result.json → rebuild all")
                total = rebuild_store_from_all_csv(self.store, self.cfg)
                logger.info(f"[watcher] bootstrap rebuild_all done: trades={total}")
                self._skip_initial_backfill = True
        except Exception:
            logger.exception("[watcher] bootstrap rebuild_all failed; continue with tail")

    # ---------- lifecycle ----------
    def start(self) -> None:
        if not self._timer.isActive():
            logger.info(f"[watcher] start poll_ms={self.cfg.poll_ms}")
            self._timer.start()

    def stop(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
            logger.info("[watcher] stopped")
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    # ---------- internals ----------
    def _today_path(self) -> Path:
        today = datetime.now(KST).date().isoformat()
        return (self.cfg.base_dir / self.cfg.subdir / self.cfg.file_pattern.format(date=today)).resolve()

    def _open_today_file(self) -> None:
        path = self._today_path()
        self._cur_path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            self._close_fh()
            self._fh = None
            self._hdr_map = None
            self._header = None
            logger.info(f"[watcher] today file not found (wait): {path}")
            return

        enc = _pick_encoding(path)
        self._fh = open(path, "r", encoding=enc)
        self._buffer = ""
        self._delimiter = _sniff_delim(path)

        # 헤더
        first = self._fh.readline()
        reader = csv.reader(io.StringIO(first), delimiter=self._delimiter)
        header = next(reader, None)
        if header is None:
            self._hdr_map = None
            self._header = None
            logger.warning(f"[watcher] header not found in {path.name}")
            return
        self._hdr_map = _best_header_map(header)
        self._header = header
        logger.info(f"[watcher] open {path.name} enc={enc} delim={self._delimiter!r} header={self._hdr_map}")

        # 전수 재계산 직후가 아니면, 현재 내용 백필(한 번)
        if not self._skip_initial_backfill:
            rest = self._fh.read()
            backfill_lines = 0
            applied = 0
            invalid = 0
            inferred_side = 0
            if rest:
                for line in rest.splitlines():
                    if not line.strip():
                        continue
                    backfill_lines += 1
                    try:
                        dr = csv.DictReader(io.StringIO(line), fieldnames=self._header, delimiter=self._delimiter)
                        raw = next(dr)

                        qty_raw = _to_int(_pick_any(raw, self._hdr_map, ["qty", "quantity", "filled_qty", "exec_qty", "수량"], "0"))
                        price_txt = _pick_any(raw, self._hdr_map, ["price", "exec_price", "avg_price", "체결가", "가격"], "0")
                        price = _to_float_soft(price_txt)
                        symbol = _pick_any(raw, self._hdr_map, ["symbol", "stk_cd", "ticker", "code", "종목코드"])
                        fee = _to_float(_pick_any(raw, self._hdr_map, ["fee", "commission", "comm", "수수료"], "0"))
                        side_text = _pick_any(raw, self._hdr_map, ["side", "action", "buy_sell", "bs", "direction", "매매구분"])

                        if not symbol or price <= 0.0:
                            invalid += 1
                            continue

                        side = _normalize_side(side_text)
                        if side is None:
                            side = _infer_side(side_text, qty_raw); inferred_side += 1

                        qty = abs(qty_raw) if qty_raw != 0 else 0
                        status = _pick_any(raw, self._hdr_map, ["status", "state", "order_status", "exec_status", "상태"], "filled") or "filled"

                        self.store.apply_trade(TradeRow(
                            time=_pick_any(raw, self._hdr_map, ["time", "ts", "order_time", "exec_time", "filled_at", "timestamp", "체결시각"]),
                            side=side, symbol=symbol, qty=qty, price=price, fee=fee, status=status,
                            strategy=_pick_any(raw, self._hdr_map, ["strategy", "cond", "조건식"]) or None, meta=None,
                        ))
                        applied += 1
                    except Exception:
                        invalid += 1
                        continue
            self.total_applied += applied
            self.total_invalid += invalid
            self.total_lines_read += backfill_lines
            self.total_inferred_side += inferred_side
            logger.info(f"[watcher] backfill {path.name} lines={backfill_lines} applied={applied} invalid={invalid} inferred_side={inferred_side}")

        # tail 시작: 파일 끝으로 이동
        self._fh.seek(0, 2)
        self._skip_initial_backfill = False

    def _close_fh(self) -> None:
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
        self._fh = None

    def _rollover_if_needed(self) -> None:
        want = self._today_path()
        if self._cur_path != want:
            logger.info(f"[watcher] rollover → {want.name}")
            self._close_fh()
            self._cur_path = None
            self._open_today_file()

    def _on_tick(self) -> None:
        # 날짜 전환 감시
        self._rollover_if_needed()

        # 파일 미존재 → 생성 대기
        if not self._fh:
            if self._today_path().exists():
                logger.info("[watcher] today file appeared; open")
                self._open_today_file()
            return

        # 새 데이터 읽기
        data = self._fh.read()
        if not data:
            return

        text = self._buffer + data
        lines = text.splitlines(keepends=True)

        complete_lines: List[str] = []
        rest = ""
        for ln in lines:
            if ln.endswith("\n") or ln.endswith("\r"):
                complete_lines.append(ln.rstrip("\r\n"))
            else:
                rest += ln
        self._buffer = rest

        # 첫 틱에 헤더가 포함될 수도 있음
        if (not self._hdr_map or not self._header) and complete_lines:
            reader = csv.reader(io.StringIO(complete_lines[0]), delimiter=self._delimiter or ",")
            header = next(reader, None)
            if header:
                self._hdr_map = _best_header_map(header)
                self._header = header
                logger.info(f"[watcher] header inferred during tail: {self._hdr_map}")
                complete_lines = complete_lines[1:]
            else:
                return

        # 협력형 처리: 틱당 라인/시간 제한 (스킵 최소화)
        timer = QElapsedTimer(); timer.start()
        applied = 0; invalid = 0; processed = 0; inferred_side = 0

        for idx, row_text in enumerate(complete_lines):
            processed += 1
            try:
                dr = csv.DictReader(io.StringIO(row_text), fieldnames=self._header, delimiter=self._delimiter or ",")
                raw = next(dr)

                qty_raw = _to_int(_pick_any(raw, self._hdr_map, ["qty", "quantity", "filled_qty", "exec_qty", "수량"], "0"))
                price_txt = _pick_any(raw, self._hdr_map, ["price", "exec_price", "avg_price", "체결가", "가격"], "0")
                price = _to_float_soft(price_txt)
                symbol = _pick_any(raw, self._hdr_map, ["symbol", "stk_cd", "ticker", "code", "종목코드"])
                fee = _to_float(_pick_any(raw, self._hdr_map, ["fee", "commission", "comm", "수수료"], "0"))
                side_text = _pick_any(raw, self._hdr_map, ["side", "action", "buy_sell", "bs", "direction", "매매구분"])

                if not symbol or price <= 0.0:
                    invalid += 1
                    continue

                side = _normalize_side(side_text)
                if side is None:
                    side = _infer_side(side_text, qty_raw); inferred_side += 1

                qty = abs(qty_raw) if qty_raw != 0 else 0
                status = _pick_any(raw, self._hdr_map, ["status", "state", "order_status", "exec_status", "상태"], "filled") or "filled"

                self.store.apply_trade(TradeRow(
                    time=_pick_any(raw, self._hdr_map, ["time", "ts", "order_time", "exec_time", "filled_at", "timestamp", "체결시각"]),
                    side=side, symbol=symbol, qty=qty, price=price, fee=fee, status=status,
                    strategy=_pick_any(raw, self._hdr_map, ["strategy", "cond", "조건식"]) or None, meta=None,
                ))
                applied += 1
            except Exception:
                invalid += 1
                continue

            if processed >= self.MAX_LINES_PER_TICK or timer.elapsed() >= self.MAX_MSEC_PER_TICK:
                remaining = complete_lines[idx + 1:]
                if remaining:
                    self._buffer = "".join([ln + "\n" for ln in remaining]) + self._buffer
                break

        self.total_lines_read += processed
        self.total_applied += applied
        self.total_invalid += invalid
        self.total_inferred_side += inferred_side

        if applied or invalid:
            logger.info(
                f"[watcher] tail {self._cur_path.name if self._cur_path else '?'} "
                f"+lines={processed} applied={applied} invalid={invalid} inferred_side={inferred_side} "
                f"(cum applied={self.total_applied}, invalid={self.total_invalid}, inferred_side={self.total_inferred_side})"
            )
