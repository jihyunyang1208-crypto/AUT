"""
risk_dashboard.py

리스크 대시보드를 별도 모듈로 분리한 구현입니다.

RiskDashboard 클래스는 QGroupBox를 상속하여 자체적으로 KPI 표시, 익스포저 게이지, 손익 그래프,
전략별 손익 카드뷰 등을 포함합니다. 외부에서는 update_snapshot() 메서드를 호출하여 스냅샷 딕셔너리를
전달하면 리스크 대시보드가 자동으로 갱신됩니다.

on_daily_report_callback 매개변수로 데일리 리포트 버튼 클릭 시 실행할 콜백을 주입할 수 있습니다.

주의: 그래프 표시에는 matplotlib를 사용하며, 환경에 따라 _HAS_MPL 플래그를 통해 사용 여부를 판별합니다.
"""

from __future__ import annotations
import logging
from typing import Dict, Any, Callable, Optional

import pandas as pd

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QWidget, QScrollArea, QFrame, QVBoxLayout, QTableWidgetItem
)

try:
    from matplotlib.dates import DateFormatter
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    import matplotlib
    from matplotlib.ticker import FuncFormatter
    # 한글 폰트 및 마이너스 표시 설정
    matplotlib.rc('font', family='Malgun Gothic')
    matplotlib.rc('axes', unicode_minus=False)
    _HAS_MPL = True
except Exception:
    _HAS_MPL = False

logger = logging.getLogger(__name__)


class RiskDashboard(QGroupBox):
    """
    리스크 대시보드를 담당하는 위젯.

    KPI, 익스포저 게이지, 손익 차트 및 전략별 손익 카드뷰를 포함하며, update_snapshot()
    메서드를 통해 외부에서 전달되는 손익 스냅샷을 기반으로 UI를 갱신한다.
    """

    def __init__(self, on_daily_report_callback: Optional[Callable[[], None]] = None, parent: Optional[QWidget] = None):
        super().__init__("리스크 대시보드", parent)
        # 데일리 리포트 버튼 클릭 시 호출되는 콜백
        self._on_daily_report_callback = on_daily_report_callback or (lambda: None)
        # 전략 카드 위젯 저장소
        self._strategy_card_widgets: Dict[str, QFrame] = {}
        # UI 구성
        self._init_ui()

    def _init_ui(self):
        # 메인 레이아웃 설정
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        # KPI 라벨 행
        row = QHBoxLayout()
        self.kpi_equity = QLabel("Equity +0.00%")
        self.kpi_daily  = QLabel("Today +0.00%")
        self.kpi_mdd    = QLabel("MDD 0.00%")
        for w in (self.kpi_equity, self.kpi_daily, self.kpi_mdd):
            w.setStyleSheet(
                "QLabel { background:#23272e; border:1px solid #3a414b; border-radius:999px; "
                "padding:6px 10px; color:#dbe3ec; font-weight:600; }"
            )
        row.addWidget(self.kpi_equity)
        row.addWidget(self.kpi_daily)
        row.addWidget(self.kpi_mdd)
        row.addStretch(1)

        # 리스크 상태 배지
        self.lbl_risk_status = QLabel("SAFE")
        self._apply_risk_badge("safe")
        row.addWidget(self.lbl_risk_status)
        lay.addLayout(row)

        # 데일리 리포트 버튼 행
        btn_row = QHBoxLayout()
        self.btn_daily_report = QPushButton("📄 데일리 매매리포트")
        self.btn_daily_report.clicked.connect(self._on_daily_report_callback)
        btn_row.addWidget(self.btn_daily_report)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        # 익스포저 게이지
        lay.addWidget(QLabel("총 포지션 비중(순자산 대비, %)"))
        self.pb_exposure = QProgressBar()
        self.pb_exposure.setRange(0, 200)
        self.pb_exposure.setValue(0)
        lay.addWidget(self.pb_exposure)
        self._update_exposure_gauge(0)

        # 차트 영역
        if _HAS_MPL:
            money_fmt = FuncFormatter(lambda x, pos: f"{int(x):,}")

            # 손익곡선 차트
            fig1 = Figure(figsize=(4, 2.2), tight_layout=True, facecolor="#000000")
            self.canvas_equity = FigureCanvas(fig1)
            self.ax_equity = fig1.add_subplot(111)
            self.ax_equity.set_facecolor("#000000")
            self.ax_equity.tick_params(colors="#e9edf1")
            self.ax_equity.title.set_color("#e9edf1")
            self.ax_equity.set_title(
                "20일 손익",
                color="#e9edf1",
                fontsize=8,
                fontweight="bold",
            )
            self.ax_equity.set_xlabel("일자"); self.ax_equity.xaxis.label.set_color("#cfd6df")
            self.ax_equity.set_ylabel("순자산"); self.ax_equity.yaxis.label.set_color("#cfd6df")
            self.ax_equity.yaxis.set_major_formatter(money_fmt)
            for s in self.ax_equity.spines.values():
                s.set_color("#555")
            self.ax_equity.grid(True, which="major", alpha=0.25, color="#555")
            lay.addWidget(self.canvas_equity)

            # 일일 손익 히스토그램
            fig2 = Figure(figsize=(4, 1.8), tight_layout=True, facecolor="#000000")
            self.canvas_hist = FigureCanvas(fig2)
            self.ax_hist = fig2.add_subplot(111)
            self.ax_hist.set_facecolor("#000000")
            self.ax_hist.tick_params(colors="#e9edf1")
            self.ax_hist.title.set_color("#e9edf1")
            self.ax_hist.set_title(
                "일일 손익",
                color="#e9edf1",
                fontsize=8,
                fontweight="bold",
            )
            self.ax_hist.set_xlabel("일자"); self.ax_hist.xaxis.label.set_color("#cfd6df")
            self.ax_hist.set_ylabel("손익"); self.ax_hist.yaxis.label.set_color("#cfd6df")
            self.ax_hist.yaxis.set_major_formatter(money_fmt)
            for s in self.ax_hist.spines.values():
                s.set_color("#555")
            self.ax_hist.grid(True, which="major", alpha=0.25, color="#555")
            lay.addWidget(self.canvas_hist)
        else:
            lay.addWidget(QLabel("(차트 라이브러리 없음 – 손익곡선/히스토그램 생략)"))

        # 전략 카드뷰 (스크롤 영역)
        card_box = QGroupBox("전략별 손익")
        card_box.setObjectName("cardBox")
        card_lay = QVBoxLayout(card_box)
        card_lay.setContentsMargins(8, 8, 8, 8)
        card_lay.setSpacing(6)
        self._strategy_cards_container = QWidget()
        self._strategy_cards_layout = QVBoxLayout(self._strategy_cards_container)
        self._strategy_cards_layout.setContentsMargins(0, 0, 0, 0)
        self._strategy_cards_layout.setSpacing(8)
        self._strategy_cards_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._strategy_cards_container)
        card_lay.addWidget(scroll)
        lay.addWidget(card_box)

    # ----------- 리스크 배지/게이지 -----------
    def _apply_risk_badge(self, level: str):
        """위험 수준에 따라 배지 색상을 변경"""
        mapping = {
            "safe":   ("SAFE",   "rgba(34,197,94,0.12)",  "#22c55e", "rgba(34,197,94,0.4)"),
            "warn":   ("WARN",   "rgba(245,158,11,0.12)", "#f59e0b", "rgba(245,158,11,0.4)"),
            "danger": ("DANGER", "rgba(239,68,68,0.12)",  "#ef4444", "rgba(239,68,68,0.4)")
        }
        text, bg, fg, bd = mapping.get(level, ("N/A", "rgba(255,255,255,0.06)", "#e9edf1", "rgba(255,255,255,0.2)"))
        self.lbl_risk_status.setText(text)
        # 스타일시트 적용
        self.lbl_risk_status.setStyleSheet(
            f"QLabel {{ background:{bg}; color:{fg}; border:1px solid {bd}; "
            f"border-radius:999px; padding:4px 10px; font-weight:700; }}"
        )

    def _update_exposure_gauge(self, pct: float):
        """익스포저 게이지 값을 반영하고 색상을 변경"""
        v = max(0, min(200, int(round(pct))))
        self.pb_exposure.setValue(v)
        if v <= 60:
            chunk = "background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #60a5fa, stop:1 #60a5fa);"
        elif v <= 120:
            chunk = "background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #60a5fa, stop:1 #22c55e);"
        else:
            chunk = "background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #f59e0b, stop:1 #ef4444);"
        css = (
            "QProgressBar{background:#23272e; border:1px solid #3a414b; "
            "border-radius:8px; text-align:center; color:#dbe3ec; height:14px;} "
            "QProgressBar::chunk{border-radius:8px; margin:0px; " + chunk + "}"
        )
        self.pb_exposure.setStyleSheet(css)
        self.pb_exposure.setToolTip("총 익스포저(%) – 120% 이상은 위험 구간")

    def _risk_level(self, daily_pct: float, mdd_pct: float, gross_pct: float) -> str:
        """위험 수준 결정"""
        if daily_pct <= -3 or mdd_pct <= -10 or gross_pct >= 120:
            return "danger"
        if daily_pct <= -1 or mdd_pct <= -5 or gross_pct >= 90:
            return "warn"
        return "safe"

    # ----------- 전략 카드뷰 -----------
    def _create_strategy_card(self, cond_id: str) -> QFrame:
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        card.setCursor(Qt.PointingHandCursor)
        card.setStyleSheet(
            "QFrame { background:#2a2f36; border:1px solid #3a414b; "
            "border-radius:12px; } QFrame:hover { border-color:#4b5563; }"
        )
        lay = QHBoxLayout(card); lay.setContentsMargins(12,8,12,8); lay.setSpacing(8)
        name = QLabel(cond_id); name.setStyleSheet("font-weight:700;")
        pct  = QLabel("오늘 +0.00% | 누적 +0.00%"); pct.setStyleSheet("color:#c7d0db;")
        meta = QLabel("종목수 0"); meta.setStyleSheet("color:#8b93a0;")
        lay.addWidget(name, 1); lay.addWidget(pct); lay.addWidget(meta)
        card._lbl_name = name; card._lbl_pcts = pct; card._lbl_meta = meta
        return card

    def _paint_strategy_card(self, card: QFrame, daily_pct: float):
        """일별 수익률에 따라 카드 색상 변경"""
        if daily_pct <= -3:
            css = "QFrame { background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #2a1f21, stop:1 #2d1f22); border:1px solid #ef4444; border-radius:12px; }"
        elif daily_pct <= -1:
            css = "QFrame { background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #2a281f, stop:1 #2d271f); border:1px solid #f59e0b; border-radius:12px; }"
        else:
            css = "QFrame { background:#2a2f36; border:1px solid #3a414b; border-radius:12px; }"
        card.setStyleSheet(css + " QFrame:hover { border-color:#4b5563; }")

    def _update_strategy_cards(self, by_cond: Dict[str, Any]):
        """전략 카드뷰를 갱신"""
        # 생성/업데이트
        for cond_id, data in by_cond.items():
            card = self._strategy_card_widgets.get(cond_id)
            daily = float(data.get("daily_pnl_pct", 0.0))
            cum   = float(data.get("cum_return_pct", 0.0))
            symbols = int(data.get("symbol_count", len(data.get("positions", []))))
            if card is None:
                card = self._create_strategy_card(cond_id)
                # 새 카드 삽입: stretch 바로 앞에 추가
                count = self._strategy_cards_layout.count()
                # 레이아웃 마지막 stretch 앞에 삽입
                self._strategy_cards_layout.insertWidget(count-1, card)
                self._strategy_card_widgets[cond_id] = card
            card._lbl_pcts.setText(f"오늘 {daily:+.2f}% | 누적 {cum:+.2f}%")
            card._lbl_meta.setText(f"종목수 {symbols}")
            self._paint_strategy_card(card, daily)
        # 제거된 카드 정리
        for cond_id in list(self._strategy_card_widgets.keys()):
            if cond_id not in by_cond:
                w = self._strategy_card_widgets.pop(cond_id)
                w.setParent(None)
                w.deleteLater()

    # ----------- 스냅샷 갱신 -----------
    def update_snapshot(self, snap: Dict[str, Any]) -> None:
        """
        손익 스냅샷을 받아 리스크 대시보드 UI를 갱신합니다.
        snap 구조 예시는 MainWindow.on_pnl_snapshot의 설명을 참고하세요.
        이 메서드는 KPI, 배지, 익스포저, 그래프, 전략 카드뷰를 업데이트합니다.
        """
        try:
            port = snap.get("portfolio") or {}
            daily_pct = float(port.get("daily_pnl_pct", 0.0))
            cum_pct   = float(port.get("cum_return_pct", 0.0))
            mdd_pct   = float(port.get("mdd_pct", 0.0))
            gross_pct = float(port.get("gross_exposure_pct", 0.0))
            # KPI 라벨
            self.kpi_equity.setText(f"누적 수익률 {cum_pct:+.2f}%")
            self.kpi_daily.setText(f"Today {daily_pct:+.2f}%")
            self.kpi_mdd.setText(f"MDD {mdd_pct:.2f}%")
            # 배지
            level = self._risk_level(daily_pct, mdd_pct, gross_pct)
            self._apply_risk_badge(level)
            # 익스포저
            self._update_exposure_gauge(gross_pct)
            # 차트
            if _HAS_MPL:
                # Equity curve
                self.ax_equity.clear()
                eq = port.get("equity_curve") or []
                if eq:
                    xs_raw = [p.get("t") for p in eq][-20:]
                    try:
                        xs = [pd.to_datetime(x).to_pydatetime() for x in xs_raw]
                    except Exception:
                        xs = xs_raw
                    ys = [float(p.get("equity", 0)) for p in eq][-20:]
                    self.ax_equity.plot(xs, ys, linewidth=1.8)
                    self.ax_equity.xaxis.set_major_formatter(DateFormatter("%Y-%m-%d"))
                    for t in self.ax_equity.get_xticklabels():
                        t.set_rotation(30)
                self.ax_equity.set_title(
                    "20일 손익",
                    color="#e9edf1",
                    fontsize=8,
                    fontweight="bold",
                    fontname="Malgun Gothic",
                )
                self.ax_equity.grid(True, which="major", alpha=0.25)
                self.ax_equity.tick_params(axis="x", labelsize=8)
                self.ax_equity.tick_params(axis="y", labelsize=8)
                self.canvas_equity.draw_idle()
                # Daily histogram
                self.ax_hist.clear()
                hist = port.get("daily_hist") or []
                if hist:
                    xs = [p.get("d") for p in hist]
                    ys = [float(p.get("pnl", 0)) for p in hist]
                    self.ax_hist.bar(xs, ys)
                    if len(xs) > 8:
                        self.ax_hist.set_xticks(xs[:: max(1, len(xs)//8)])
                        for t in self.ax_hist.get_xticklabels(): t.set_rotation(28)
                self.ax_hist.set_title(
                    "일일 손익",
                    color="#e9edf1",
                    fontsize=8,
                    fontweight="bold",
                    fontname="Malgun Gothic",
                )
                self.ax_hist.grid(True, which="major", alpha=0.25)
                self.ax_hist.tick_params(axis="x", labelsize=8)
                self.ax_hist.tick_params(axis="y", labelsize=8)
                self.canvas_hist.draw_idle()
            # 전략 카드뷰
            self._update_strategy_cards(snap.get("by_condition") or {})
        except Exception as e:
            logger.exception("RiskDashboard.update_snapshot error", exc_info=e)
