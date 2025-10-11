# -*- coding: utf-8 -*-
"""
trading_report/report_api.py
- daily_report_generator.py의 내부 함수를 직접 가져와 리포트를 생성하는 프로그램용 API.
- subprocess 없이 Python 함수 호출만으로 리포트 생성 가능.
"""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from datetime import datetime

# ====== generator 모듈에서 필요한 함수들 직접 import ======
# 파일 위치: trading_report/daily_report_generator.py
from . import daily_report_generator as gen  # 같은 패키지 안에 있어야 함

try:
    import pandas as pd  # for KST now (optional)
except Exception:
    pd = None


# --------------------------------------------------------------------------------------
# 경로 추정/유효성 모델
# --------------------------------------------------------------------------------------
@dataclass
class DailyReportPaths:
    system: Path
    trades: Optional[Path]
    template: Path
    output: Path
    reports_dir: Path


def _is_kst_available() -> bool:
    return pd is not None


def _today_kst_str() -> str:
    if _is_kst_available():
        return pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


def _resolve_project_root(root_like: str) -> Path:
    """
    실행 위치가 어긋나도 실제 프로젝트 루트를 찾아서 사용.
    기준: candidate_stocks.csv 또는 trading_report/ 폴더 존재 여부
    """
    cand = Path(root_like or ".").resolve()

    def _ok(p: Path) -> bool:
        return (p / "candidate_stocks.csv").exists() or (p / "trading_report").exists()

    if _ok(cand):
        return cand
    here = Path(__file__).resolve().parent.parent  # trading_report/ 상위
    if _ok(here):
        return here
    if _ok(here.parent):
        return here.parent
    return cand


def guess_paths(project_root: str, date_str: Optional[str] = None) -> DailyReportPaths:
    """
    질문에서 제공한 디렉토리 구조에 맞춰 기본 경로를 유추한다.
    - system_results: ./data/system_results_YYYY-MM-DD.json
    - trades(jsonl):  ./logs/trades/trade_YYYYMMDD.jsonl (없어도 OK)
    - template:       ./trading_report/daily_report_template.md
    - output:         ./reports/daily_YYYY-MM-DD.md
    """
    root = _resolve_project_root(project_root)
    reports_dir = (root / "reports"); reports_dir.mkdir(parents=True, exist_ok=True)

    day = (date_str or _today_kst_str())
    ymd_dash = day
    ymd_compact = day.replace("-", "")

    system_path   = root / "logs" / "trades" / f"orders_{ymd_dash}.jsonl"
    trades_path   = root / "logs" / "trades" / f"orders_{ymd_dash}.jsonl"
    template_path = root / "trading_report" / "daily_report_template.md"
    output_path = reports_dir / f"daily_{ymd_dash}.md"

    return DailyReportPaths(
        system=system_path,
        trades=trades_path if trades_path.exists() else None,
        template=template_path,
        output=output_path,
        reports_dir=reports_dir,
    )


# --------------------------------------------------------------------------------------
# 리포트 생성 로직 (daily_report_generator의 main과 동일한 파이프라인을 함수화)
# --------------------------------------------------------------------------------------
def _build_context(
    date_str: str,
    mode: str,
    system_data: Dict[str, Any],
    trade_rows: List[Dict[str, Any]],
    template_text: str,
    use_ai: bool,
) -> Tuple[Dict[str, Any], str]:
    """generator의 파이프라인을 그대로 따라 컨텍스트와 최종 md 텍스트를 만든다."""
    kpi = gen.derive_kpi(system_data, trade_rows)
    timeline_rows = gen.build_timeline_rows(system_data)
    s_rows_list, s_rows_md, s_scores_md = gen.group_by_strategy(system_data, trade_rows)
    quality = gen.build_quality(trade_rows)

    best, worst = gen.best_worst_trade(system_data)
    busiest = gen.busiest_window(system_data)

    if use_ai:
        prompt_daily = f"[Daily Overview] date={date_str}\nKPI={kpi}\nQuality={quality}\nPlease summarize in Korean, 3~5 lines, no markdown."
        daily_summary = gen.call_gemini_if_available(prompt_daily)
        prompt_reflect = f"[Reflection] Provide 3~5 line reflection in Korean for improvements given KPI={kpi} and strategy_rows={s_rows_list[:5]}."
        daily_reflection = gen.call_gemini_if_available(prompt_reflect)
        prompt_insights = f"[Strategy Insights] Analyze per-strategy strengths/weaknesses in Korean. strategies={list( {k:v for k,v in enumerate(s_rows_list)} )}"
        strategy_insights = gen.call_gemini_if_available(prompt_insights)
        prompt_improve = f"[Improvements] Suggest concrete improvements per strategy in Korean."
        strategy_improvements = gen.call_gemini_if_available(prompt_improve)
        prompt_actions = f"[Action Items] Provide 3~5 bullet action items in Korean based on KPI and quality."
        action_items = "- " + "\n- ".join(gen.call_gemini_if_available(prompt_actions).splitlines()[:5])
    else:
        # generator의 fallback 텍스트와 동등
        daily_summary = "오전 추세 구간에서 추세추종 전략이 유효했고, 오후 변동성 축소로 역추세 전략 성과가 저하되었습니다."
        daily_reflection = "- 추세추종 전략은 유지, 역추세 전략은 조건 완화/시간대 제한 검토.\n- 응답 지연이 큰 시간대(10~11시)에는 주문 슬로틀 적용 필요."
        strategy_insights = "- MACD: 신호 일관성 양호\n- Reversal: 약세장에서 성과 저하\n- LadderBuy: 체결률 우수하나 지연 민감"
        strategy_improvements = "- MACD: 유지\n- Reversal: 손절/재진입 로직 강화\n- LadderBuy: 배치 단위 축소, 가격 범위 조정"
        action_items = "- MACD 오전 우선순위 유지\n- Reversal 조건 재조정\n- duration_ms>400 건 로그 분석"

    ctx = dict(
        generated_at = datetime.now(tz=gen.KST).strftime("%Y-%m-%d %H:%M:%S %Z") if hasattr(gen, "KST") else datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        date = date_str,
        mode = mode,
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
            system_results_path = "N/A",
            trade_jsonl_path = "—",
        )
    )

    md = gen.render_template(template_text, ctx)
    return ctx, md


def run_daily_report(
    project_root: str,
    date_str: Optional[str] = None,
    *,
    system_path: Optional[str | Path] = None,
    trades_path: Optional[str | Path] = None,
    template_path: Optional[str | Path] = None,
    output_path: Optional[str | Path] = None,
    mode: str = "Live",
    use_ai: bool = False,
) -> Path:
    """
    데일리 리포트를 생성하고 출력 파일 경로를 반환.
    - 인자를 비우면 프로젝트 구조를 기준으로 자동 경로 추정.
    - daily_report_generator.py 내부 함수를 직접 사용 (subprocess 불필요).
    """
    day = date_str or _today_kst_str()
    root = _resolve_project_root(project_root)

    # 기본 경로 유추
    guessed = guess_paths(str(root), day)

    sys_p = Path(system_path) if system_path else guessed.system
    trd_p = Path(trades_path) if trades_path else (guessed.trades if guessed.trades else None)
    tpl_p = Path(template_path) if template_path else guessed.template
    out_p = Path(output_path) if output_path else guessed.output
    out_p.parent.mkdir(parents=True, exist_ok=True)

    # 필수 파일 확인: system + template
    if not sys_p.exists():
        raise FileNotFoundError(f"[trading_report] system_results 파일을 찾을 수 없습니다: {sys_p}")
    if not tpl_p.exists():
        raise FileNotFoundError(f"[trading_report] 템플릿 파일을 찾을 수 없습니다: {tpl_p}")

    # 데이터 로드
    system_data = gen.load_system_results(sys_p)
    trade_rows: List[Dict[str, Any]] = gen.load_trade_jsonl(trd_p) if trd_p else []

    # 템플릿 로드
    template_text = Path(tpl_p).read_text(encoding="utf-8")

    # 컨텍스트 + md 생성
    ctx, md = _build_context(
        date_str=day,
        mode=mode,
        system_data=system_data,
        trade_rows=trade_rows,
        template_text=template_text,
        use_ai=use_ai,
    )

    # meta 실제 경로 기록(참고용)
    ctx["meta"]["system_results_path"] = str(sys_p)
    ctx["meta"]["trade_jsonl_path"] = str(trd_p) if trd_p else "—"

    # 저장
    out_p.write_text(md, encoding="utf-8")
    return out_p
