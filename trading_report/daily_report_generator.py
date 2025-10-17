#trading_report/daily_report_generator.py
# -*- coding: utf-8 -*-
# 오트 데일리 리포트 생성기 (v2.3: 미청산 포지션 포함 분석)
# - 기능: 분석된 순수 데이터(List, Dict)와 미청산 포지션 데이터를 반환하여 UI 렌더링 모듈에 제공.

import json
import sys
import math
import statistics
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
from dataclasses import dataclass

# --- 시간대 설정 (KST) ---
try:
    import zoneinfo
    KST = zoneinfo.ZoneInfo("Asia/Seoul")
except ImportError:
    class _KST(timezone):
        _utcoffset = timedelta(hours=9)
        def utcoffset(self, dt): return self._utcoffset
        def tzname(self, dt): return "KST"
    KST = _KST()

# --- 데이터 모델 ---
@dataclass
class Trade:
    symbol: str; strategy: str; entry_ts: datetime; exit_ts: datetime
    avg_entry_price: float; avg_exit_price: float; quantity: int
    pnl: float = 0.0; pnl_pct: float = 0.0; holding_duration_min: float = 0.0
    action: str = "BUY"
    def __post_init__(self):
        # PnL 계산 (완료된 거래에 대해서만)
        self.pnl = (self.avg_exit_price - self.avg_entry_price) * self.quantity
        if self.avg_entry_price > 0: self.pnl_pct = (self.avg_exit_price / self.avg_entry_price - 1) * 100
        # 보유 기간 계산
        self.holding_duration_min = (self.exit_ts - self.entry_ts).total_seconds() / 60

# --- AI 요약 기능 ---
def _gen_ai_summary_fallback(_prompt: str) -> str: return "AI 요약 생성에 실패했습니다."
def call_gemini_if_available(prompt: str) -> str:
    # 이 함수는 실제 Gemini API 호출 로직을 대체합니다.
    try:
        from utils.gemini_client import GeminiClient
        return (GeminiClient().generate_text(prompt=prompt, max_tokens=1024) or "").strip() or _gen_ai_summary_fallback(prompt)
    except Exception as e:
        # 실제 환경에서는 로깅을 할 수 있지만, 여기서는 경고 메시지를 제거했습니다.
        return _gen_ai_summary_fallback(prompt)

# --- 포맷팅 헬퍼 ---
def _fmt(val: Optional[float], unit: str = "", digits: int = 1, is_int: bool = False) -> str:
    if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))): return "—"
    if is_int: return f"{int(round(val)):,}{unit}"
    return f"{val:,.{digits}f}{unit}"

# --- 데이터 로딩 및 전처리 ---
def _parse_ts(ts_str: Optional[str]) -> datetime:
    if not ts_str: return datetime.now(KST)
    try: return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(KST)
    except ValueError: return datetime.now(KST)

# 함수 시그니처 수정: 완료된 거래 리스트와 미청산 포지션 리스트를 반환
def load_and_pair_trades(path: Path) -> Tuple[List[Trade], List[Dict[str, Any]]]:
    if not path.exists(): 
        print(f"오류: 로그 파일을 찾을 수 없습니다: {path}")
        return [], []
    try:
        with path.open("r", encoding="utf-8") as f:
            orders = [json.loads(line) for line in f if line.strip()]
    except (json.JSONDecodeError, IOError) as e:
        print(f"로그 파일 읽기 오류: {e}")
        return [], []

    orders.sort(key=lambda x: _parse_ts(x.get("ts")))
    # positions는 종목별로 미청산된 매수 거래(entry)들을 저장
    positions = defaultdict(lambda: {"entries": []})
    completed_trades: List[Trade] = []
    
    for order in orders:
        action, symbol, price, qty, strategy, ts = (order.get(k) for k in ["action", "stk_cd", "price", "qty", "strategy", "ts"])
        if not all([action, symbol, price, qty]): continue
        try: price, qty, action = float(price), int(qty), action.upper()
        except (ValueError, TypeError): continue

        if action == "BUY":
            # entries에 strategy 정보도 저장하여 미청산 포지션에서 활용
            positions[symbol]["entries"].append({
                "ts": _parse_ts(ts), 
                "qty": qty, 
                "price": price, 
                "strategy": strategy or "UNKNOWN"
            })
        
        elif action == "SELL" and positions[symbol]["entries"]:
            sold_qty, exit_value, entry_value, entry_ts_list = qty, qty * price, 0, []
            
            # FIFO 방식으로 매수 포지션을 청산
            while sold_qty > 0 and positions[symbol]["entries"]:
                entry = positions[symbol]["entries"][0]
                take_qty = min(sold_qty, entry["qty"])
                entry_value += take_qty * entry["price"]; entry_ts_list.append(entry["ts"])
                entry["qty"] -= take_qty; sold_qty -= take_qty
                
                if entry["qty"] == 0: positions[symbol]["entries"].pop(0)

            # 완료된 거래로 Trade 객체 생성
            if entry_value > 0:
                completed_trades.append(Trade(
                    symbol=symbol, 
                    strategy=strategy or "UNKNOWN", # SELL order의 strategy를 사용 (일관성 유지를 위해)
                    entry_ts=min(entry_ts_list), 
                    exit_ts=_parse_ts(ts), 
                    avg_entry_price=(entry_value / qty), 
                    avg_exit_price=(exit_value / qty), 
                    quantity=qty
                ))

    # 미청산 포지션 (Open Positions) 수집
    open_positions: List[Dict[str, Any]] = []
    for symbol, pos_data in positions.items():
        if pos_data["entries"]:
            all_entries = pos_data["entries"]
            total_qty = sum(e["qty"] for e in all_entries)
            
            if total_qty > 0:
                total_entry_cost = sum(e["qty"] * e["price"] for e in all_entries)
                earliest_entry = min(all_entries, key=lambda e: e["ts"])
                
                open_positions.append({
                    "symbol": symbol,
                    "quantity": total_qty,
                    "avg_entry_price": total_entry_cost / total_qty,
                    "entry_ts": earliest_entry["ts"].isoformat(), # ISO 포맷 문자열로 저장
                    "strategy": earliest_entry["strategy"],
                    "action": "BUY" # 매수 포지션임을 명시
                })

    return completed_trades, open_positions # 완료된 거래와 미청산 포지션을 모두 반환

# --- 분석 함수들 (완료된 거래 기반으로만 분석, 미청산 포지션은 현황 보고에 활용) ---
def analyze_performance(trades: List[Trade]) -> Dict[str, Any]:
    if not trades: 
        # 거래가 하나도 없을 경우 0 또는 None으로 초기화된 딕셔너리를 반환하여 오류 방지
        return {
            "total_trades": 0, "net_pnl_abs": 0.0, "net_pnl_pct": 0.0,
            "profit_factor": 0.0, "win_rate": 0.0, "payoff_ratio": 0.0,
            "avg_win_pnl": 0.0, "avg_loss_pnl": 0.0, "max_drawdown_pct": 0.0,
            "sharpe_ratio_annualized": 0.0, "avg_holding_min": 0.0
        }
    
    returns = [t.pnl_pct for t in trades]; pnl_values = [t.pnl for t in trades]
    wins, losses = [t for t in trades if t.pnl > 0], [t for t in trades if t.pnl <= 0]
    total_trades = len(trades)
    win_rate = len(wins) / total_trades * 100 if total_trades > 0 else 0
    gross_profit, gross_loss = sum(t.pnl for t in wins), abs(sum(t.pnl for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    avg_win, avg_loss = statistics.mean(t.pnl for t in wins) if wins else 0, abs(statistics.mean(t.pnl for t in losses)) if losses else 0
    payoff_ratio = avg_win / avg_loss if avg_loss > 0 else float('inf')
    equity_curve = [sum(pnl_values[:i+1]) for i in range(total_trades)]
    peak, max_drawdown = 0.0, 0.0
    for equity in equity_curve:
        if equity > peak: peak = equity
        drawdown = (peak - equity)
        if drawdown > max_drawdown: max_drawdown = drawdown
    
    # 추정 초기 자본 계산 로직 (수익률 계산을 위한 기준)
    initial_capital_guess = (abs(next((t.avg_entry_price * t.quantity for t in trades), 1)) * 5) or 1
    max_drawdown_pct = (max_drawdown / (initial_capital_guess + peak)) * 100 if (initial_capital_guess + peak) > 0 else 0
    stdev_returns = statistics.stdev(returns) if len(returns) > 1 else 0
    sharpe_ratio = (statistics.mean(returns) / stdev_returns) * math.sqrt(252) if stdev_returns > 0 else 0.0
    
    return {
        "total_trades": total_trades, "net_pnl_abs": sum(pnl_values), "net_pnl_pct": sum(returns),
        "profit_factor": profit_factor, "win_rate": win_rate, "payoff_ratio": payoff_ratio,
        "avg_win_pnl": avg_win, "avg_loss_pnl": avg_loss, "max_drawdown_pct": max_drawdown_pct,
        "sharpe_ratio_annualized": sharpe_ratio,
        "avg_holding_min": statistics.mean(t.holding_duration_min for t in trades) if trades else 0
    }

def analyze_by_strategy(trades: List[Trade]) -> List[Dict[str, Any]]:
    grouped = defaultdict(list); result = []
    for t in trades: grouped[t.strategy].append(t)
    for strategy, str_trades in sorted(grouped.items()):
        # 완료된 거래가 없더라도 analyze_performance가 안전하게 0 값을 반환하도록 수정되었음
        kpi = analyze_performance(str_trades)
        kpi['strategy_name'] = strategy
        result.append(kpi)
    return result

def generate_report_context(target_date_str: Optional[str] = None) -> Dict[str, Any]:
    USE_AI_SUMMARY = True
    try:
        PROJECT_ROOT = Path(__file__).resolve().parent.parent
    except NameError:
        PROJECT_ROOT = Path.cwd()
    
    target_dt = datetime.strptime(target_date_str, "%Y-%m-%d") if target_date_str else datetime.now(KST)
    date_str = target_dt.strftime("%Y-%m-%d")
    log_path = PROJECT_ROOT / "logs" / "trades" / f"orders_{date_str}.jsonl"
    
    # 완료된 거래와 미청산 포지션 데이터를 함께 받음
    completed_trades, open_positions = load_and_pair_trades(log_path)
    
    # 완료된 거래와 미청산 포지션이 모두 없는 경우에만 에러 반환
    if not completed_trades and not open_positions:
        return {"date": date_str, "error": "분석할 거래 내역이나 미청산 포지션이 없습니다."}

    # 완료된 거래가 없더라도 빈 리스트를 기반으로 분석 
    overall_kpi = analyze_performance(completed_trades)
    strategy_kpis = analyze_by_strategy(completed_trades)
    
    ai_summary, ai_insight, ai_action_items = "...", "...", "..."
    if USE_AI_SUMMARY:
        # AI 프롬프트용 데이터는 포맷팅된 문자열로 생성
        kpi_str = ", ".join([f"{k}: {_fmt(v)}" for k, v in overall_kpi.items()])
        # AI 프롬프트에 미청산 포지션 정보 포함
        open_pos_str = json.dumps([{
            "symbol": op['symbol'], 
            "qty": op['quantity'], 
            "avg_price": _fmt(op['avg_entry_price'], digits=0, is_int=True),
            "strategy": op['strategy']
        } for op in open_positions])
        
        prompt_data = f"날짜:{date_str}\n전체성과:{kpi_str}\n전략별성과:{strategy_kpis}\n미청산포지션:\n{open_pos_str}"
        
        # AI 프롬프트 내용에 미청산 포지션 분석 요청 추가
        ai_summary = call_gemini_if_available(f"전문 퀀트 애널리스트로서 다음 자동매매 성과와 미청산 포지션 목록을 시장 상황과 연계하여 3~5줄로 냉철하게 총평해주세요.\n\n{prompt_data}")
        ai_insight = call_gemini_if_available(f"트레이딩 전략가로서 다음 데이터를 보고 각 전략의 강점, 약점, 유효했던 시장 환경, 그리고 미청산 포지션의 잠재적 리스크/기회를 3~5줄로 심층 분석해주세요.\n\n{prompt_data}")
        ai_action_items = call_gemini_if_available(f"시스템 운영 관리자로서 다음 성과와 미청산 포지션을 기반으로 수익성 개선과 리스크 관리를 위한 구체적인 액션 아이템 3가지를 제안해주세요.\n\n{prompt_data}")

    return {
        "date": date_str,
        "generated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "kpi": overall_kpi,
        "ai": {"summary": ai_summary, "insight": ai_insight, "action_items": ai_action_items},
        "strategy_kpis": strategy_kpis,
        "trade_log": sorted(completed_trades, key=lambda t: t.pnl_pct, reverse=True),
        "open_positions": open_positions # 미청산 포지션 데이터
    }

if __name__ == "__main__":
    target_date = sys.argv[1] if len(sys.argv) > 1 else None
    report_data = generate_report_context(target_date)
    import pprint
    pprint.pprint(report_data)
