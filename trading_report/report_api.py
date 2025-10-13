from typing import Any, Dict, List
from trading_report.daily_report_generator import generate_report_context, Trade, _fmt

def _build_strategy_table_html(strategy_kpis: List[Dict[str, Any]]) -> str:
    """전략별 분석 데이터(dict list)로 HTML 테이블 행들을 생성합니다."""
    rows = []
    for kpi in strategy_kpis:
        rows.append(f"""
        <tr>
            <td><strong>{kpi.get('strategy_name','—')}</strong></td>
            <td>{kpi.get('total_trades','—')}</td>
            <td>{_fmt(kpi.get('net_pnl_abs'), '원', is_int=True)}</td>
            <td>{_fmt(kpi.get('win_rate'), '%')}</td>
            <td>{_fmt(kpi.get('profit_factor'))}</td>
            <td>{_fmt(kpi.get('max_drawdown_pct'), '%')}</td>
        </tr>
        """)
    return "\n".join(rows) if rows else "<tr><td colspan='6'>데이터 없음</td></tr>"

def _build_trade_log_html(trades: List[Trade]) -> str:
    """거래 객체 리스트로 HTML 테이블 행들을 생성합니다."""
    rows = []
    for t in trades:
        pnl_class = "trade-win" if t.pnl > 0 else "trade-loss"
        rows.append(f"""
        <tr class='{pnl_class}'>
            <td>{t.symbol}</td>
            <td>{t.strategy}</td>
            <td>{_fmt(t.pnl_pct, '%')}</td>
            <td>{_fmt(t.pnl, '원', is_int=True)}</td>
            <td>{t.entry_ts.strftime('%H:%M')}</td>
            <td>{t.exit_ts.strftime('%H:%M')}</td>
            <td>{_fmt(t.holding_duration_min, '분', is_int=True)}</td>
        </tr>
        """)
    return "\n".join(rows) if rows else "<tr><td colspan='7'>데이터 없음</td></tr>"

def get_report_html(date_str: str) -> str:
    """
    지정된 날짜의 분석 데이터를 기반으로 스타일이 적용된 최종 HTML 문자열을 생성합니다.
    이 함수가 PyQt UI를 위한 유일한 인터페이스입니다.
    """
    ctx = generate_report_context(date_str)

    if "error" in ctx:
        return f"<h1>{ctx['date']} 리포트 생성 오류</h1><p>{ctx['error']}</p>"

    # 데이터로부터 HTML 테이블 부분을 동적으로 생성
    strategy_rows_html = _build_strategy_table_html(ctx['strategy_kpis'])
    trade_log_rows_html = _build_trade_log_html(ctx['trade_log'])

    css = """
    <style>
        body { font-family: 'Malgun Gothic', 'Segoe UI', sans-serif; line-height: 1.7; color: #212529; background-color: #f8f9fa; margin: 0; padding: 20px; }
        .container { max-width: 1000px; margin: auto; background: white; padding: 20px 40px; border-radius: 8px; box-shadow: 0 4px 15px rgba(0,0,0,0.08); }
        h1, h2, h3 { color: #1a3a6c; border-bottom: 2px solid #e9ecef; padding-bottom: 10px; margin-top: 40px; }
        h1 { font-size: 28px; text-align: center; border: none; }
        h2 { font-size: 22px; }
        h3 { font-size: 18px; border-bottom: none; color: #004a9e; }
        .header-meta { text-align: center; color: #6c757d; margin-bottom: 40px; font-size: 14px; }
        .kpi-table { width: 100%; border-collapse: collapse; }
        .kpi-table td { padding: 12px; border: 1px solid #dee2e6; width: 25%; }
        .kpi-table td:nth-child(odd) { background-color: #f8f9fa; font-weight: bold; color: #495057; }
        .ai-section { background-color: #e9f5ff; padding: 20px; border-radius: 5px; margin-top: 15px; border-left: 5px solid #007bff; }
        .ai-section ul { padding-left: 20px; margin: 0; }
        .ai-section li { margin-bottom: 8px; }
        table { width: 100%; border-collapse: collapse; margin-top: 1em; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }
        th, td { border: 1px solid #ddd; padding: 12px; text-align: left; }
        th { background-color: #004a9e; color: white; font-weight: bold; }
        tr:nth-child(even) { background-color: #f8f9fa; }
        .trade-win { color: #155724; background-color: #d4edda; }
        .trade-loss { color: #721c24; background-color: #f8d7da; }
    </style>
    """

    kpi = ctx['kpi']
    html = f"""
    <!DOCTYPE html>
    <html lang="ko">
    <head><meta charset="UTF-8"><title>Daily Report - {ctx['date']}</title>{css}</head>
    <body>
        <div class="container">
            <h1>퀀트 트레이딩 데일리 리포트</h1>
            <p class="header-meta"><b>대상 날짜:</b> {ctx['date']} | <b>생성 시각:</b> {ctx['generated_at']}</p>
            
            <h2>Ⅰ. AI 애널리스트 총평</h2>
            <div class="ai-section"><p>{ctx['ai']['summary']}</p></div>

            <h2>Ⅱ. 전체 성과 대시보드</h2>
            <table class="kpi-table">
                <tr><td>순손익 (Net PnL)</td><td>{_fmt(kpi['net_pnl_abs'], '원', is_int=True)}</td><td>승률 (Win Rate)</td><td>{_fmt(kpi['win_rate'], '%')}</td></tr>
                <tr><td>프로핏 팩터</td><td>{_fmt(kpi['profit_factor'])}</td><td>손익비</td><td>{_fmt(kpi['payoff_ratio'])}</td></tr>
                <tr><td>최대 낙폭 (MDD)</td><td>{_fmt(kpi['max_drawdown_pct'], '%')}</td><td>샤프 지수</td><td>{_fmt(kpi['sharpe_ratio_annualized'])}</td></tr>
                <tr><td>총 거래 횟수</td><td>{kpi['total_trades']} 회</td><td>평균 보유 시간</td><td>{_fmt(kpi['avg_holding_min'], '분', is_int=True)}</td></tr>
            </table>

            <h2>Ⅲ. AI 전략 분석 및 제언</h2>
            <h3>심층 분석</h3>
            <div class="ai-section"><p>{ctx['ai']['insight']}</p></div>
            <h3>실행 계획 (Action Items)</h3>
            <div class="ai-section"><ul><li>{ctx['ai']['action_items'].replace('-', '').strip().replace('\n', '</li><li>')}</li></ul></div>

            <h2>Ⅳ. 전략별 성과 분석</h2>
            <table>
                <thead><tr><th>전략</th><th>거래 수</th><th>순손익 (PnL)</th><th>승률</th><th>프로핏 팩터</th><th>최대 낙폭 (MDD)</th></tr></thead>
                <tbody>{strategy_rows_html}</tbody>
            </table>

            <h2>Ⅴ. 상세 거래 기록</h2>
            <table>
                <thead><tr><th>종목</th><th>전략</th><th>수익률</th><th>손익(원)</th><th>진입</th><th>청산</th><th>보유시간</th></tr></thead>
                <tbody>{trade_log_rows_html}</tbody>
            </table>
        </div>
    </body>
    </html>
    """
    return html

