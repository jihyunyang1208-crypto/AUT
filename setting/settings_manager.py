# core/settings_manager.py
from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Optional, Literal

from PySide6.QtCore import QSettings, Qt, QRegularExpression
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox, QCheckBox,
    QComboBox, QDialogButtonBox, QWidget, QLineEdit, QGroupBox, QFormLayout
)

# ===================== 데이터 모델 =====================
@dataclass
class AppSettings:
    # 트레이딩 스위치
    master_enable: bool = True
    auto_buy: bool = True
    auto_sell: bool = True

    # 주문 타입(지정가/시장가) - AutoTrader.TradeSettings.order_type 와 매핑
    order_type: Literal["limit", "market"] = "limit"

    # 전략/필터
    use_macd30_filter: bool = False
    macd30_timeframe: str = "30m"
    macd30_max_age_sec: int = 1800

    # 모니터 루프
    poll_interval_sec: int = 20
    bar_close_window_start_sec: int = 5
    bar_close_window_end_sec: int = 30

    # 시뮬/실거래
    sim_mode: bool = False
    api_base_url: str = ""

    # 라더(사다리) 매수 기본 설정
    ladder_unit_amount: int = 100_000
    ladder_num_slices: int = 10

    # 유틸
    timezone: str = "Asia/Seoul"

    @classmethod
    def from_env(cls) -> "AppSettings":
        """환경변수 → 초기값. QSettings와 병합 시 '기본'으로 사용됩니다."""
        def _b(env_key: str, default: Optional[bool] = None):
            val = os.getenv(env_key)
            if val is None:
                return default
            return str(val).strip().lower() in ("1", "true", "yes", "y", "on")

        def _s(env_key: str, default: str = "") -> str:
            v = os.getenv(env_key)
            return (v.strip() if isinstance(v, str) else default)

        def _order_type_from_env() -> Literal["limit", "market"]:
            # ORDER_TYPE=limit/market (없으면 limit)
            raw = _s("ORDER_TYPE", "").lower()
            return "market" if raw == "market" else "limit"

        # 시뮬 모드: SIM_MODE 우선, 없으면 PAPER_MODE 사용
        sim = _b("SIM_MODE", None)
        if sim is None:
            sim = _b("PAPER_MODE", False)

        api = _s("HTTP_API_BASE", "")
        if api.endswith("/"):
            api = api[:-1]

        return cls(
            sim_mode=bool(sim),
            api_base_url=api,
            order_type=_order_type_from_env(),
        )


# ===================== 영속 스토어 (QSettings) =====================
class SettingsStore:
    ORG = "Trade"
    APP = "AutoTraderUI"
    KEY = "app_settings_v1"

    def __init__(self):
        self.qs = QSettings(self.ORG, self.APP)

    def load(self) -> AppSettings:
        """
        1) .env에서 기본값(AppSettings.from_env) 생성
        2) QSettings의 dict와 병합 (있으면 덮어쓰기)
        3) 구버전 호환 키(auto_buy/auto_sell 등)도 반영
        """
        base = AppSettings.from_env()
        raw = self.qs.value(self.KEY, None)

        if isinstance(raw, dict):
            merged = asdict(base)
            merged.update(raw)
            # 타입 안정화(일부 값이 str로 저장되는 경우 방지)
            return AppSettings(
                master_enable=bool(merged.get("master_enable", True)),
                auto_buy=bool(merged.get("auto_buy", True)),
                auto_sell=bool(merged.get("auto_sell", True)),
                order_type=("market" if merged.get("order_type", "limit") == "market" else "limit"),
                use_macd30_filter=bool(merged.get("use_macd30_filter", False)),
                macd30_timeframe=str(merged.get("macd30_timeframe", "30m")),
                macd30_max_age_sec=int(merged.get("macd30_max_age_sec", 1800)),
                poll_interval_sec=int(merged.get("poll_interval_sec", 20)),
                bar_close_window_start_sec=int(merged.get("bar_close_window_start_sec", 5)),
                bar_close_window_end_sec=int(merged.get("bar_close_window_end_sec", 30)),
                sim_mode=bool(merged.get("sim_mode", False)),
                api_base_url=str(merged.get("api_base_url", "")),
                ladder_unit_amount=int(merged.get("ladder_unit_amount", 100_000)),
                ladder_num_slices=int(merged.get("ladder_num_slices", 10)),
                timezone=str(merged.get("timezone", "Asia/Seoul")),
            )

        # --- 구버전 호환 (개별 키) ---
        auto_buy = self.qs.value("auto_buy", None, type=bool)
        auto_sell = self.qs.value("auto_sell", None, type=bool)
        if auto_buy is not None:
            base.auto_buy = bool(auto_buy)
        if auto_sell is not None:
            base.auto_sell = bool(auto_sell)

        return base

    def save(self, cfg: AppSettings):
        # URL 뒤 슬래시는 저장 전에 정규화
        api = cfg.api_base_url.strip()
        if api.endswith("/"):
            api = api[:-1]
        cfg.api_base_url = api

        self.qs.setValue(self.KEY, asdict(cfg))
        # 구버전 호환키도 같이 저장(점진적 이전용)
        self.qs.setValue("auto_buy", cfg.auto_buy)
        self.qs.setValue("auto_sell", cfg.auto_sell)


# ===================== 설정 다이얼로그 =====================
class SettingsDialog(QDialog):
    """
    - .env 값은 기본값(AppSettings.from_env)로 로딩되고,
      사용자가 다이얼로그에서 조정하면 QSettings에 저장됩니다.
    - order_type(지정가/시장가) 필드(ComboBox)
    - 라더 매수 기본값(1회 금액, 분할 수) 편집 가능
    """
    GEOM_KEY = "ui/settings_dialog_geometry_v1"

    def __init__(self, parent: QWidget | None, cfg: AppSettings):
        super().__init__(parent)
        self.setWindowTitle("환경설정")
        self.cfg = cfg
        self._qs = QSettings(SettingsStore.ORG, SettingsStore.APP)

        self._build_ui()
        self._load_to_widgets()
        self._restore_geometry()

    # ----------- UI 구성(그룹/폼으로 세련되게 정리) -----------
    def _build_ui(self):
        self.setModal(True)
        self.setMinimumWidth(520)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 10)
        lay.setSpacing(10)

        # --- 트레이딩 스위치 그룹 ---
        grp_switch = QGroupBox("트레이딩 스위치")
        sw = QHBoxLayout(grp_switch)
        self.cb_master = QCheckBox("마스터 ON"); self.cb_master.setToolTip("전체 자동 매매 스위치")
        self.cb_auto_buy = QCheckBox("자동 매수")
        self.cb_auto_sell = QCheckBox("자동 매도")
        sw.addWidget(self.cb_master); sw.addSpacing(8)
        sw.addWidget(self.cb_auto_buy); sw.addSpacing(8)
        sw.addWidget(self.cb_auto_sell); sw.addStretch(1)
        lay.addWidget(grp_switch)

        # --- 주문 타입/시뮬/API 그룹 ---
        grp_order = QGroupBox("주문/연동")
        fo = QFormLayout(grp_order); fo.setLabelAlignment(Qt.AlignRight)
        self.cmb_order_type = QComboBox()
        self.cmb_order_type.addItems(["지정가", "시장가"])
        self.cmb_order_type.setToolTip("기본 주문 타입")
        self.cb_sim = QCheckBox("시뮬레이션 모드 (SIM_MODE/PAPER_MODE 대체)")
        self.le_api = QLineEdit(); self.le_api.setPlaceholderText("API Base URL (비우면 .env/기본값)")
        url_regex = QRegularExpression(r"^$|^https?://[^\s/$.?#].[^\s]*$")
        self.le_api.setValidator(QRegularExpressionValidator(url_regex))
        fo.addRow("주문 타입", self.cmb_order_type)
        fo.addRow("시뮬레이션 모드", self.cb_sim)
        fo.addRow("API Base", self.le_api)
        lay.addWidget(grp_order)

        # --- MACD 그룹 ---
        grp_macd = QGroupBox("MACD 30m 필터")
        fm = QFormLayout(grp_macd)
        self.cb_macd = QCheckBox("사용 (hist ≥ 0)")
        self.cmb_macd_tf = QComboBox(); self.cmb_macd_tf.addItems(["30m"])
        self.sp_macd_age = QSpinBox(); self.sp_macd_age.setRange(60, 24*3600); self.sp_macd_age.setSuffix(" sec")
        fm.addRow(self.cb_macd)
        fm.addRow("타임프레임", self.cmb_macd_tf)
        fm.addRow("최대 지연", self.sp_macd_age)
        lay.addWidget(grp_macd)

        # --- 모니터/마감 창 그룹 ---
        grp_loop = QGroupBox("모니터링 루프 & 5m 마감창")
        fl = QFormLayout(grp_loop)
        self.sp_poll = QSpinBox(); self.sp_poll.setRange(5, 120); self.sp_poll.setSuffix(" sec")
        self.sp_close_s = QSpinBox(); self.sp_close_s.setRange(0, 59); self.sp_close_s.setSuffix(" s")
        self.sp_close_e = QSpinBox(); self.sp_close_e.setRange(0, 59); self.sp_close_e.setSuffix(" s")
        fl.addRow("폴링 주기", self.sp_poll)
        row_be = QWidget(); hb = QHBoxLayout(row_be); hb.setContentsMargins(0,0,0,0); hb.setSpacing(8)
        hb.addWidget(QLabel("시작")); hb.addWidget(self.sp_close_s)
        hb.addSpacing(12)
        hb.addWidget(QLabel("종료")); hb.addWidget(self.sp_close_e); hb.addStretch(1)
        fl.addRow("마감창", row_be)
        lay.addWidget(grp_loop)

        # --- 라더 그룹 ---
        grp_ladder = QGroupBox("라더(사다리) 매수 기본값")
        fld = QFormLayout(grp_ladder)
        self.sp_ladder_unit_amount = QSpinBox()
        self.sp_ladder_unit_amount.setRange(10_000, 50_000_000)
        self.sp_ladder_unit_amount.setSingleStep(10_000)
        self.sp_ladder_unit_amount.setSuffix(" 원")
        self.sp_ladder_num_slices = QSpinBox()
        self.sp_ladder_num_slices.setRange(1, 100)
        self.sp_ladder_num_slices.setSuffix(" 회")
        fld.addRow("1회 매수금액", self.sp_ladder_unit_amount)
        fld.addRow("분할 수", self.sp_ladder_num_slices)
        lay.addWidget(grp_ladder)

        # --- 타임존 그룹 ---
        grp_tz = QGroupBox("기타")
        ft = QFormLayout(grp_tz)
        self.le_tz = QLineEdit(); self.le_tz.setPlaceholderText("Asia/Seoul")
        ft.addRow("Time Zone", self.le_tz)
        lay.addWidget(grp_tz)

        # --- 버튼 ---
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        # 기본 스타일(라이트/다크 공용, 기존 코드와 충돌 없음)
        self.setStyleSheet("""
            QGroupBox {
                font-weight: 600;
                border: 1px solid rgba(128,128,128,0.35);
                border-radius: 8px;
                margin-top: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }
            QLineEdit, QComboBox, QSpinBox { min-height: 26px; }
        """)

    # ---------------- 내부: UI ← 설정 ----------------
    def _load_to_widgets(self):
        c = self.cfg
        self.cb_master.setChecked(c.master_enable)
        self.cb_auto_buy.setChecked(c.auto_buy)
        self.cb_auto_sell.setChecked(c.auto_sell)

        # 주문 타입 매핑
        self.cmb_order_type.setCurrentText("시장가" if c.order_type == "market" else "지정가")

        self.cb_macd.setChecked(c.use_macd30_filter)
        self.cmb_macd_tf.setCurrentText(c.macd30_timeframe or "30m")
        self.sp_macd_age.setValue(int(c.macd30_max_age_sec))

        self.sp_poll.setValue(int(c.poll_interval_sec))
        self.sp_close_s.setValue(int(c.bar_close_window_start_sec))
        self.sp_close_e.setValue(int(c.bar_close_window_end_sec))

        self.cb_sim.setChecked(bool(c.sim_mode))
        self.le_api.setText(c.api_base_url or "")

        self.sp_ladder_unit_amount.setValue(int(c.ladder_unit_amount))
        self.sp_ladder_num_slices.setValue(int(c.ladder_num_slices))

        self.le_tz.setText(c.timezone or "Asia/Seoul")

    # ---------------- 내부: UI → 설정 ----------------
    def get_settings(self) -> AppSettings:
        c = AppSettings(**asdict(self.cfg))

        c.master_enable = self.cb_master.isChecked()
        c.auto_buy = self.cb_auto_buy.isChecked()
        c.auto_sell = self.cb_auto_sell.isChecked()

        # 주문 타입 매핑
        order_txt = self.cmb_order_type.currentText()
        c.order_type = "market" if order_txt == "시장가" else "limit"

        c.use_macd30_filter = self.cb_macd.isChecked()
        c.macd30_timeframe = self.cmb_macd_tf.currentText()
        c.macd30_max_age_sec = int(self.sp_macd_age.value())

        c.poll_interval_sec = int(self.sp_poll.value())
        c.bar_close_window_start_sec = int(self.sp_close_s.value())
        c.bar_close_window_end_sec = int(self.sp_close_e.value())

        c.sim_mode = self.cb_sim.isChecked()
        api = (self.le_api.text().strip() or "")
        c.api_base_url = api[:-1] if api.endswith("/") else api

        c.ladder_unit_amount = int(self.sp_ladder_unit_amount.value())
        c.ladder_num_slices = int(self.sp_ladder_num_slices.value())

        c.timezone = self.le_tz.text().strip() or "Asia/Seoul"
        return c

    # --------------- 다이얼로그 위치/크기 기억(선택) ---------------
    def _restore_geometry(self):
        data = self._qs.value(self.GEOM_KEY, None)
        if isinstance(data, bytes):
            self.restoreGeometry(data)

    def _save_geometry(self):
        self._qs.setValue(self.GEOM_KEY, self.saveGeometry())

    def accept(self):
        # URL 간단 검증 실패 시 포커스 반환(기존 코드와 호환)
        if not self.le_api.hasAcceptableInput():
            self.le_api.setFocus()
            return
        self._save_geometry()
        super().accept()

    def reject(self):
        self._save_geometry()
        super().reject()
