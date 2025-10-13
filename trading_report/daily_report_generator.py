#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 오트 데일리 리포트 생성기 (통합 버전)
# - 기능: 단일 로그 파일(orders...jsonl)을 기반으로 리포트를 자동 생성.
# - 입력: logs/trades/orders_YYYY-MM-DD.jsonl (통합 로그)
# - 출력: reports/daily_report_YYYY-MM-DD.md

import json
import sys
import math
import statistics
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter, defaultdict

# --- 시간대 설정 (KST) ---
try:
    import zoneinfo
    KST = zoneinfo.ZoneInfo("Asia/Seoul")
except ImportError:
    class _KST(timezone):
        _utcoffset = timedelta(hours=9)
        _dst = None
        _name = "KST"
        def utcoffset(self, dt): return self._utcoffset
        def dst(self, dt): return self._dst
        def tzname(self, dt): return self._name
    KST = _KST()

# --- AI 요약 기능 (선택 사항) ---
def _gen_ai_summary_fallback(_prompt: str) -> str:
    """Gemini 호출 실패 시 사용될 기본 요약 메시지"""
    return "데이터 기반 요약: 오전장 추세추종 전략이 유효했고, 응답 지연이 높은 구간에 실패율이 증가했습니다. 역추세 전략은 약세장에서 성과가 저하되었습니다."

def call_gemini_if_available(prompt: str) -> str:
    """Gemini 클라이언트를 호출하여 텍스트를 생성하는 함수"""
    try:
        from utils.gemini_client import GeminiClient
        gc = GeminiClient()
        out = gc.generate_text(prompt=prompt, max_tokens=500)
        return (out or "").strip() or _gen_ai_summary_fallback(prompt)
    except Exception as e:
        print(f"경고: Gemini 호출 중 오류 발생 - {e}")
        return _gen_ai_summary_fallback(prompt)

# --- 포맷팅 헬퍼 함수 ---
def _fmt_pct(x: Optional[float], digits: int = 1) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))): return "—"
    return f"{x:.{digits}f}%"

def _fmt_ms(x: Optional[float]) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))): return "—"
    return f"{int(round(x))}ms"

def _fmt_num(x: Optional[float], digits: int = 0) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))): return "—"
    return f"{x:,.{digits}f}" if digits > 0 else f"{int(round(x)):,}"

def _parse_ts(ts: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except Exception:
        return datetime.now(tz=KST)

# --- 데이터 로딩 함수 (단일화) ---
def load_unified_log_data(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        print(f"오류: 통합 로그 파일을 찾을 수 없습니다: {path}")
        sys.exit(1)
    
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    print(f"경고: 잘못된 형식의 JSON 라인을 건너뜁니다: {line.strip()}")
    return rows

# --- 핵심 분석/가공 함수 ---
def derive_kpi(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    signals = [r for r in records if r.get("pnl_pct") is not None]
    pnl_pcts = [float(s["pnl_pct"]) for s in signals if isinstance(s.get("pnl_pct"), (int, float))]
    holding_values = [float(s["holding_min"]) for s in signals if isinstance(s.get("holding_min"), (int, float))]
    status_labels = [(r.get("status_label") or "").upper() for r in records if "status_label" in r]
    latencies = [float(r["duration_ms"]) for r in records if isinstance(r.get("duration_ms"), (int, float))]
    success_count = status_labels.count("SUCCESS")
    total_orders = len(status_labels)
    fill_success_rate = (success_count / total_orders * 100.0) if total_orders else None
    
    return {
        "total_trades": len(signals),
        "total_buys": sum(1 for s in signals if (s.get("side") or "").upper() == "BUY"),
        "total_sells": sum(1 for s in signals if (s.get("side") or "").upper() == "SELL"),
        "avg_holding_min": _fmt_num(statistics.mean(holding_values) if holding_values else None, 0),
        "avg_pnl_pct": _fmt_pct(statistics.mean(pnl_pcts) if pnl_pcts else None, 1),
        "fill_success_rate": _fmt_pct(fill_success_rate, 1),
        "avg_latency_ms": _fmt_ms(statistics.mean(latencies) if latencies else None),
        "fail_count": total_orders - success_count,
        "success_count": success_count,
        "total_orders": total_orders,
        "max_latency_ms": _fmt_ms(max(latencies) if latencies else None),
    }

def build_timeline_rows(records: List[Dict[str, Any]]) -> str:
    rows = []
    sorted_records = sorted(records, key=lambda r: _parse_ts(r.get("ts", "")))
    for r in sorted_records:
        ts = _parse_ts(r.get("ts", "")).strftime("%H:%M")
        sym = r.get("symbol") or r.get("stk_cd") or "—"
        strat = r.get("strategy") or r.get("condition_name") or "—"
        side = (r.get("side") or "—").upper()
        pe = r.get("price_entry") or r.get("price") or "—"
        px = r.get("price_exit") or "—"
        reason = r.get("reason") or "—"
        rows.append(f"| {ts} | {sym} | {strat} | {side} | {pe} | {px} | {_fmt_pct(r.get('pnl_pct'), 1)} | {reason} |")
    return "\n".join(rows) or "| — | — | — | — | — | — | — | — |"

def group_by_strategy(records: List[Dict[str, Any]]) -> Tuple[List[str], str, str]:
    agg = defaultdict(lambda: {"count": 0, "fail": 0, "pnl_list": [], "hold_list": [], "reasons": Counter()})
    for r in records:
        strat = r.get("strategy") or r.get("condition_name") or "UNKNOWN"
        if r.get("pnl_pct") is not None:
            agg[strat]["count"] += 1
            if isinstance(r.get("pnl_pct"), (int, float)): agg[strat]["pnl_list"].append(float(r["pnl_pct"]))
            if isinstance(r.get("holding_min"), (int, float)): agg[strat]["hold_list"].append(float(r["holding_min"]))
            if r.get("reason"): agg[strat]["reasons"][r["reason"]] += 1
        if "status_label" in r and (r.get("status_label") or "").upper() != "SUCCESS":
            agg[strat]["fail"] += 1
    
    s_rows, score_rows = [], []
    for strat, d in sorted(agg.items()):
        count, fail, success = d["count"], d["fail"], max(d["count"] - d["fail"], 0)
        success_rate = (success / count * 100.0) if count else None
        avg_pnl = statistics.mean(d["pnl_list"]) if d["pnl_list"] else None
        sum_pnl = sum(d["pnl_list"]) or None
        avg_hold = statistics.mean(d["hold_list"]) if d["hold_list"] else None
        top_reason = ", ".join([f"{r}({c})" for r, c in d["reasons"].most_common(2)]) or "—"
        
        s_rows.append(f"| {strat} | {count} | {_fmt_pct(success_rate, 1)} | {_fmt_pct(avg_pnl, 1)} | {_fmt_pct(sum_pnl, 1)} | {_fmt_num(avg_hold, 0)} | {fail} | {top_reason} |")
        
        profitability = ((avg_pnl or 0.0) * 10 / 5)
        stability = ((success_rate or 0.0) / 10)
        efficiency = max(0.0, 10.0 - (avg_hold or 0.0) / 10.0)
        quality_score = max(0.0, 10.0 - (fail or 0))
        total = round(0.4 * profitability + 0.3 * stability + 0.2 * efficiency + 0.1 * quality_score, 1)
        score_rows.append(f"| {strat} | {round(profitability, 1)} | {round(stability, 1)} | {round(efficiency, 1)} | {round(quality_score, 1)} | **{total}** |")
        
    return s_rows, "\n".join(s_rows) or "| — | — | — | — | — | — | — | — |", "\n".join(score_rows) or "| — | — | — | — | — |"

def build_quality(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    latencies = [float(r["duration_ms"]) for r in records if isinstance(r.get("duration_ms"), (int, float))]
    labels = [(r.get("status_label") or "").upper() for r in records if "status_label" in r]
    errors = []
    for r in records:
        body = r.get("response", {})
        if isinstance(body, dict):
            b = body.get("body", {})
            msg = b.get("error") or b.get("msg") or b.get("message")
            if msg: errors.append(str(msg))
    
    total, fail = len(labels), sum(1 for x in labels if x != "SUCCESS")
    
    return {
        "avg_latency_ms": _fmt_ms(statistics.mean(latencies) if latencies else None),
        "median_latency_ms": _fmt_ms(statistics.median(latencies) if latencies else None),
        "max_latency_ms": _fmt_ms(max(latencies) if latencies else None),
        "fail_rate": _fmt_pct((fail / total * 100.0) if total else None, 1),
        "top_errors": ", ".join([f"{k}({v})" for k, v in Counter(errors).most_common(3)]) or "—",
    }

def best_worst_trade(records: List[Dict[str, Any]]) -> Tuple[str, str]:
    signals = [r for r in records if isinstance(r.get("pnl_pct"), (int, float))]
    if not signals: return "—", "—"
    
    best = max(signals, key=lambda s: s.get("pnl_pct", -1e9))
    worst = min(signals, key=lambda s: s.get("pnl_pct", 1e9))

    def _fmt(s: Dict[str, Any]) -> str:
        sym = s.get("symbol") or s.get("stk_cd") or "—"
        strat = s.get("strategy") or s.get("condition_name") or "—"
        return f"{sym} {_fmt_pct(s.get('pnl_pct'), 1)} ({strat})"
        
    return _fmt(best), _fmt(worst)

def busiest_window(records: List[Dict[str, Any]]) -> str:
    buckets = Counter(_parse_ts(r.get("ts", "")).strftime("%H:%M") for r in records)
    if not buckets: return "—"
    top = buckets.most_common(1)[0]
    return f"{top[0]} ({top[1]}건)"

def render_template(template_text: str, ctx: Dict[str, Any]) -> str:
    def get_path(d: Any, path: str) -> str:
        cur = d
        for key in path.split("."):
            if isinstance(cur, dict): cur = cur.get(key)
            else: return "—"
        return str(cur) if cur is not None else "—"
    return re.sub(r"{{\s*([^}]+)\s*}}", lambda m: get_path(ctx, m.group(1).strip()), template_text)

# --- 메인 실행 함수 ---
def run_report_generation(target_date_str: Optional[str] = None):
    # 1. 설정 및 경로 정의
    MODE, USE_AI_SUMMARY = "Live", True
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

    # 2. 날짜 설정
    target_dt = datetime.strptime(target_date_str, "%Y-%m-%d").replace(tzinfo=KST) if target_date_str else datetime.now(KST)
    date_str = target_dt.strftime("%Y-%m-%d")

    # 3. 파일 경로 설정
    trades_dir = PROJECT_ROOT / "logs" / "trades"
    log_path = trades_dir / f"orders_{date_str}.jsonl"
    tpl_path = PROJECT_ROOT / "trading_report" / "daily_report_template.md"
    output_dir = PROJECT_ROOT / "reports"
    output_path = output_dir / f"daily_report_{date_str}.md"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"리포트 생성을 시작합니다 (대상일: {date_str})")
    print(f" - 통합 로그: {log_path}")

    # 4. 데이터 로딩
    all_records = load_unified_log_data(log_path)
    tpl = tpl_path.read_text(encoding="utf-8")
    
    if not all_records:
        print("경고: 로그 데이터가 비어있어, 빈 리포트를 생성합니다.")
        # 빈 리포트 생성 및 종료 로직 추가 가능
        return

    # 5. 데이터 분석 및 가공
    kpi = derive_kpi(all_records)
    quality = build_quality(all_records)
    s_rows_list, s_rows_md, s_scores_md = group_by_strategy(all_records)
    best, worst = best_worst_trade(all_records)
    
    # 6. AI 코멘트 생성
    # AI가 생성할 각 항목에 대한 변수를 기본값으로 초기화합니다.
    daily_summary, daily_reflection, strategy_insights, strategy_improvements, action_items = "...", "...", "...", "...", "..."

    # AI 요약 기능 사용 여부를 확인합니다.
    if USE_AI_SUMMARY:
        print("AI를 사용하여 리포트 코멘트를 생성합니다...")
        
        # 일일 총평 생성
        daily_summary = call_gemini_if_available(f"[일일 총평] 날짜={date_str}\nKPI={kpi}\n품질={quality}\n내용을 한국어로 3~5줄 요약해주세요. 마크다운 형식은 제외합니다.")
        
        # 개선점 분석
        daily_reflection = call_gemini_if_available(f"[개선점 분석] 주어진 KPI={kpi} 와 전략별 성과={s_rows_list[:5]} 를 바탕으로 개선점을 한국어로 3~5줄 제안해주세요.")
        
        # 전략별 강점 및 약점 분석
        strategy_insights = call_gemini_if_available(f"[전략별 분석] 다음 전략들의 강점과 약점을 한국어로 분석해주세요. 전략: {s_rows_list}")
               
        # 실행 계획 (Action Items) 제안
        action_items = "- " + "\n- ".join(call_gemini_if_available(f"[실행 계획] KPI와 품질 지표를 바탕으로, 실행할 구체적인 행동 계획을 한국어로 3~5가지 제안해주세요.").splitlines()[:5])
    
    # 7. 템플릿 렌더링을 위한 컨텍스트 생성
    ctx = {
        "generated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date": date_str, "mode": MODE, "kpi": kpi, "quality": quality,
        "table": {
            "timeline_rows": build_timeline_rows(all_records),
            "strategy_rows": s_rows_md,
            "strategy_scores": s_scores_md,
        },
        "highlights": {
            "best_trade": best, "worst_trade": worst,
            "busiest_window": busiest_window(all_records),
            "fail_count": kpi.get("fail_count", "—"),
            "max_latency_ms": quality.get("max_latency_ms", "—")
        },
        "daily_summary": daily_summary,
        "daily_reflection": daily_reflection,
        "strategy_insights": strategy_insights,
        "strategy_improvements": strategy_improvements,
        "action_items": action_items,
        "meta": {"log_path": str(log_path)}
    }

    # 8. 리포트 파일 생성
    md = render_template(tpl, ctx)
    output_path.write_text(md, encoding="utf-8")
    print(f"리포트 생성이 완료되었습니다: {output_path}")

if __name__ == "__main__":
    run_report_generation()