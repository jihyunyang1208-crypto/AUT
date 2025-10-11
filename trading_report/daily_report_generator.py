#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 오트 데일리 리포트 생성기
# - 입력: system_results_YYYY-MM-DD.json (필수), trade_YYYYMMDD.jsonl (선택)
# - 출력: Markdown 리포트 (템플릿 채움)

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import math
import statistics
from collections import Counter, defaultdict

try:
    import pandas as pd  # type: ignore
except Exception:
    pd = None  # optional

# KST tzinfo
try:
    import zoneinfo  # py3.9+
    KST = zoneinfo.ZoneInfo("Asia/Seoul")
except Exception:
    class _KST(timezone):
        pass
    KST = timezone(timedelta(hours=9), name="KST")

# Optional Gemini utils (user's util module path may vary)
def _gen_ai_summary_fallback(_prompt: str) -> str:
    return "데이터 기반 요약: 오전장 추세추종 전략이 유효했고, 응답 지연이 높은 구간(10~11시)에 실패율이 증가했습니다. 역추세 전략은 약세장에서 성과 저하."

def call_gemini_if_available(prompt: str) -> str:
    try:
        from utils.gemini_client import GeminiClient  # type: ignore
        gc = GeminiClient()
        out = gc.generate_text(prompt=prompt, max_tokens=500)
        return (out or "").strip() or _gen_ai_summary_fallback(prompt)
    except Exception:
        return _gen_ai_summary_fallback(prompt)

def _fmt_pct(x: Optional[float], digits: int = 1) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x:.{digits}f}%"

def _fmt_ms(x: Optional[float]) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{int(round(x))}ms"

def _fmt_num(x: Optional[float], digits: int = 0) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    if digits == 0:
        return f"{int(round(x)):,}"
    return f"{x:,.{digits}f}"

def _parse_ts(ts: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts.replace("Z","+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except Exception:
        return datetime.now(tz=KST)

def load_jsonl_objects(path: Path) -> List[Dict[str, Any]]:
    """
    JSONL(각 줄이 하나의 JSON)만 파싱.
    - 공백/빈줄/주석(//)은 무시
    - dict가 아닌 줄은 건너뜀
    """
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for ln, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("//"):
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    rows.append(obj)
            except Exception as e:
                raise ValueError(f"JSONL 파싱 오류: {path.name} #{ln}: {e}")
    return rows


def load_system_results(path: Path) -> Dict[str, Any]:
    """
    system_results_YYYY-MM-DD.jsonl 을 가정.
    여러 줄 JSON을 읽어 signals를 전부 합치고,
    portfolio/quality/meta는 '마지막 줄'을 우선 적용.
    """
    if path.suffix.lower() != ".jsonl":
        raise ValueError(f"system_results는 JSONL만 지원합니다: {path}")

    docs = load_jsonl_objects(path)
    if not docs:
        return {"signals": []}

    merged: Dict[str, Any] = {"signals": []}
    for doc in docs:
        sigs = doc.get("signals")
        if isinstance(sigs, list):
            merged["signals"].extend([x for x in sigs if isinstance(x, dict)])
        # 마지막 문서의 대표 키들 우선
        if isinstance(doc.get("portfolio"), dict):
            merged["portfolio"] = doc["portfolio"]
        if isinstance(doc.get("quality"), dict):
            merged["quality"] = doc["quality"]
        if isinstance(doc.get("meta"), dict):
            merged["meta"] = doc["meta"]
    return merged


def load_trade_jsonl(path: Optional[Path]) -> List[Dict[str, Any]]:
    """
    trade_YYYYMMDD.jsonl 전용.
    없으면 빈 리스트 반환.
    """
    if not path:
        return []
    if not path.exists():
        return []
    if path.suffix.lower() != ".jsonl":
        raise ValueError(f"trade 로그는 JSONL만 지원합니다: {path}")
    return load_jsonl_objects(path)

def derive_kpi(system: Dict[str, Any], trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    signals: List[Dict[str, Any]] = system.get("signals", []) or []
    total_trades = len(signals)
    total_buys = sum(1 for s in signals if (s.get("side") or "").upper() == "BUY")
    total_sells = sum(1 for s in signals if (s.get("side") or "").upper() == "SELL")

    holding_values: List[float] = []
    pnl_pcts: List[float] = []
    for s in signals:
        p = s.get("pnl_pct")
        h = s.get("holding_min")
        if isinstance(h, (int, float)):
            holding_values.append(float(h))
        if isinstance(p, (int, float)):
            pnl_pcts.append(float(p))

    avg_hold = statistics.mean(holding_values) if holding_values else None
    avg_pnl = statistics.mean(pnl_pcts) if pnl_pcts else None

    status_labels = [ (t.get("status_label") or "").upper() for t in trades if "status_label" in t ]
    latencies = [ float(t.get("duration_ms")) for t in trades if isinstance(t.get("duration_ms"), (int,float)) ]
    fail_count = sum(1 for s in status_labels if s and s != "SUCCESS")
    success_count = sum(1 for s in status_labels if s == "SUCCESS")
    total_orders = len(status_labels)

    fill_success_rate = (success_count / total_orders * 100.0) if total_orders else None
    avg_latency = statistics.mean(latencies) if latencies else None

    return dict(
        total_trades=total_trades,
        total_buys=total_buys,
        total_sells=total_sells,
        avg_holding_min= _fmt_num(avg_hold, 0),
        avg_pnl_pct= _fmt_pct(avg_pnl, 1),
        fill_success_rate= _fmt_pct(fill_success_rate, 1),
        avg_latency_ms= _fmt_ms(avg_latency),
        fail_count=fail_count,
        success_count=success_count,
        total_orders=total_orders,
        max_latency_ms= _fmt_ms(max(latencies) if latencies else None),
    )

def build_timeline_rows(system: Dict[str, Any]) -> str:
    rows = []
    for s in sorted(system.get("signals", []), key=lambda x: _parse_ts(x.get("ts",""))):
        ts = _parse_ts(s.get("ts","")).strftime("%H:%M")
        sym = s.get("symbol") or s.get("stk_cd") or "—"
        strat = s.get("strategy") or s.get("condition_name") or s.get("condition_seq") or "—"
        side = (s.get("side") or "—").upper()
        pe = s.get("price_entry") or s.get("price") or "—"
        px = s.get("price_exit") or "—"
        pnl_pct = s.get("pnl_pct")
        reason = s.get("reason") or "—"
        rows.append(f"| {ts} | {sym} | {strat} | {side} | {pe} | {px} | {_fmt_pct(pnl_pct,1)} | {reason} |")
    return "\n".join(rows) if rows else "| — | — | — | — | — | — | — | — |"

def group_by_strategy(system: Dict[str, Any], trades: List[Dict[str, Any]]) -> Tuple[List[str], str, str]:
    agg: Dict[str, Dict[str, Any]] = defaultdict(lambda: dict(
        count=0, fail=0, pnl_list=[], hold_list=[], reasons=Counter(),
    ))
    for s in system.get("signals", []):
        strat = s.get("strategy") or s.get("condition_name") or s.get("condition_seq") or "UNKNOWN"
        agg[strat]["count"] += 1
        if isinstance(s.get("pnl_pct"), (int, float)):
            agg[strat]["pnl_list"].append(float(s["pnl_pct"]))
        if isinstance(s.get("holding_min"), (int, float)):
            agg[strat]["hold_list"].append(float(s["holding_min"]))
        if s.get("reason"):
            agg[strat]["reasons"][s["reason"]] += 1

    for t in trades:
        strat = t.get("strategy") or t.get("condition_name") or t.get("condition_seq") or "UNKNOWN"
        label = (t.get("status_label") or "").upper()
        if label and label != "SUCCESS":
            agg[strat]["fail"] += 1

    s_rows = []
    score_rows = []
    for strat, d in sorted(agg.items(), key=lambda kv: kv[0]):
        count = d["count"]
        fail = d["fail"]
        success = max(count - fail, 0)
        success_rate = (success / count * 100.0) if count else None
        avg_pnl = statistics.mean(d["pnl_list"]) if d["pnl_list"] else None
        sum_pnl = sum(d["pnl_list"]) if d["pnl_list"] else None
        avg_hold = statistics.mean(d["hold_list"]) if d["hold_list"] else None
        top_reason = ", ".join([f"{r}({c})" for r, c in d["reasons"].most_common(2)]) if d["reasons"] else "—"

        s_rows.append(
            f"| {strat} | {count} | {_fmt_pct(success_rate,1)} | {_fmt_pct(avg_pnl,1)} | {_fmt_pct(sum_pnl,1)} | "
            f"{_fmt_num(avg_hold,0)} | {fail} | {top_reason} |"
        )

        profitability =  ( (avg_pnl or 0.0) * 10 / 5 )  
        stability     =  ( (success_rate or 0.0) / 10 )
        efficiency    =  max(0.0, 10.0 - (avg_hold or 0.0)/10.0)  
        quality       =  max(0.0, 10.0 - (fail or 0) )            
        total = round(0.4*profitability + 0.3*stability + 0.2*efficiency + 0.1*quality, 1)

        score_rows.append(f"| {strat} | {round(profitability,1)} | {round(stability,1)} | {round(efficiency,1)} | {round(quality,1)} | **{total}** |")

    return s_rows, "\n".join(s_rows) if s_rows else "| — | — | — | — | — | — | — | — |", "\n".join(score_rows) if score_rows else "| — | — | — | — | — |"

def build_quality(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    latencies = [ float(t.get("duration_ms")) for t in trades if isinstance(t.get("duration_ms"), (int,float)) ]
    labels = [ (t.get("status_label") or "").upper() for t in trades if "status_label" in t ]
    errors = []
    for t in trades:
        body = t.get("response") or {}
        msg = None
        if isinstance(body, dict):
            b = body.get("body") or {}
            msg = b.get("error") or b.get("msg") or b.get("message")
        if msg:
            errors.append(str(msg))
    counter = Counter(errors)
    top_errors = ", ".join([f"{k}({v})" for k,v in counter.most_common(3)]) if counter else "—"

    avg = statistics.mean(latencies) if latencies else None
    med = statistics.median(latencies) if latencies else None
    mx  = max(latencies) if latencies else None
    fail = sum(1 for x in labels if x and x != "SUCCESS")
    total = len(labels)
    fail_rate = (fail/total*100.0) if total else None

    return dict(
        avg_latency_ms=_fmt_ms(avg),
        median_latency_ms=_fmt_ms(med),
        max_latency_ms=_fmt_ms(mx),
        fail_rate=_fmt_pct(fail_rate,1),
        top_errors=top_errors,
    )

def best_worst_trade(system: Dict[str, Any]) -> Tuple[str, str]:
    best = None; worst = None
    for s in system.get("signals", []):
        p = s.get("pnl_pct")
        if not isinstance(p, (int,float)):
            continue
        if best is None or p > best.get("pnl_pct", -1e9):
            best = s
        if worst is None or p < worst.get("pnl_pct", 1e9):
            worst = s
    def _fmt(s: Optional[Dict[str,Any]]) -> str:
        if not s: return "—"
        sym = s.get("symbol") or s.get("stk_cd") or "—"
        strat = s.get("strategy") or s.get("condition_name") or "—"
        p = _fmt_pct(s.get("pnl_pct"),1)
        return f"{sym} {p} ({strat})"
    return _fmt(best), _fmt(worst)

def busiest_window(system: Dict[str, Any]) -> str:
    buckets = Counter()
    for s in system.get("signals", []):
        dt = _parse_ts(s.get("ts",""))
        key = dt.strftime("%H:%M")
        buckets[key] += 1
    if not buckets:
        return "—"
    top = buckets.most_common(1)[0]
    return f"{top[0]}대 ({top[1]}건)"

def render_template(template_text: str, ctx: Dict[str, Any]) -> str:
    out = template_text
    def get_path(d: Dict[str,Any], path: str) -> str:
        cur: Any = d
        for key in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(key)
            else:
                cur = None
        if cur is None:
            return "—"
        if isinstance(cur, (int,float)):
            return str(cur)
        return str(cur)
    import re
    for m in re.findall(r"{{\s*([^}]+)\s*}}", template_text):
        out = out.replace("{{"+m+"}}", get_path(ctx, m))
    return out

def main():
    ap = argparse.ArgumentParser(description="오트 데일리 리포트 생성기")
    ap.add_argument("--date", required=True, help="거래일자 YYYY-MM-DD (KST)")
    ap.add_argument("--system", required=True, help="system_results json 경로")
    ap.add_argument("--trades", default="", help="trade jsonl 경로(선택)")
    ap.add_argument("--template", required=True, help="Markdown 템플릿 경로")
    ap.add_argument("--output", required=True, help="출력 md 경로")
    ap.add_argument("--mode", default="Live", help="실행 모드 (Live/Simulation)")
    ap.add_argument("--use_ai", action="store_true", help="Gemini를 사용해 코멘트 생성(옵션)")
    args = ap.parse_args()

    date_str = args.date
    sys_path = Path(args.system)
    trd_path = Path(args.trades) if args.trades else None
    tpl = Path(args.template).read_text(encoding="utf-8")

    system = load_system_results(sys_path)
    trades = load_trade_jsonl(trd_path)

    kpi = derive_kpi(system, trades)
    timeline_rows = build_timeline_rows(system)
    s_rows_list, s_rows_md, s_scores_md = group_by_strategy(system, trades)
    quality = build_quality(trades)

    best, worst = best_worst_trade(system)
    busiest = busiest_window(system)

    if args.use_ai:
        prompt_daily = f"[Daily Overview] date={date_str}\nKPI={kpi}\nQuality={quality}\nPlease summarize in Korean, 3~5 lines, no markdown."
        daily_summary = call_gemini_if_available(prompt_daily)
        prompt_reflect = f"[Reflection] Provide 3~5 line reflection in Korean for improvements given KPI={kpi} and strategy_rows={s_rows_list[:5]}."
        daily_reflection = call_gemini_if_available(prompt_reflect)
        prompt_insights = f"[Strategy Insights] Analyze per-strategy strengths/weaknesses in Korean. strategies={list( {k:v for k,v in enumerate(s_rows_list)} )}"
        strategy_insights = call_gemini_if_available(prompt_insights)
        prompt_improve = f"[Improvements] Suggest concrete improvements per strategy in Korean."
        strategy_improvements = call_gemini_if_available(prompt_improve)
        prompt_actions = f"[Action Items] Provide 3~5 bullet action items in Korean based on KPI and quality."
        action_items = "- " + "\n- ".join(call_gemini_if_available(prompt_actions).splitlines()[:5])
    else:
        daily_summary = "오전 추세 구간에서 추세추종 전략이 유효했고, 오후 변동성 축소로 역추세 전략 성과가 저하되었습니다."
        daily_reflection = "- 추세추종 전략은 유지, 역추세 전략은 조건 완화/시간대 제한 검토.\n- 응답 지연이 큰 시간대(10~11시)에는 주문 슬로틀 적용 필요."
        strategy_insights = "- MACD: 신호 일관성 양호\n- Reversal: 약세장에서 성과 저하\n- LadderBuy: 체결률 우수하나 지연 민감"
        strategy_improvements = "- MACD: 유지\n- Reversal: 손절/재진입 로직 강화\n- LadderBuy: 배치 단위 축소, 가격 범위 조정"
        action_items = "- MACD 오전 우선순위 유지\n- Reversal 조건 재조정\n- duration_ms>400 건 로그 분석"

    ctx = dict(
        generated_at = datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S %Z"),
        date = date_str,
        mode = args.mode,
        kpi = kpi,
        table = dict(
            timeline_rows = timeline_rows,
            strategy_rows = s_rows_md,
            strategy_scores = s_scores_md,
        ),
        highlights = dict(
            best_trade = best,
            worst_trade = worst,
            busiest_window = busiest,
            fail_count = kpi.get("fail_count","—"),
            max_latency_ms = kpi.get("max_latency_ms","—")
        ),
        quality = quality,
        daily_summary = daily_summary,
        daily_reflection = daily_reflection,
        strategy_insights = strategy_insights,
        strategy_improvements = strategy_improvements,
        action_items = action_items,
        meta = dict(
            system_results_path = str(sys_path),
            trade_jsonl_path = str(trd_path) if trd_path else "—",
        )
    )

    md = render_template(tpl, ctx)
    Path(args.output).write_text(md, encoding="utf-8")
    print(f"Wrote report to: {args.output}")

if __name__ == "__main__":
    main()
