# core/settings_manager.py
from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Optional

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox, QCheckBox,
    QComboBox, QDialogButtonBox, QWidget, QLineEdit
)


# =============== 데이터 모델 ===============
@dataclass
class AppSettings:
    # 트레이딩 스위치
    master_enable: bool = True
    auto_buy: bool = True
    auto_sell: bool = True

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

    # 라더(사다리) 매수 설정 ✅ 추가
    ladder_unit_amount: int = 100_000
    ladder_num_slices: int = 10

    # 유틸
    timezone: str = "Asia/Seoul"

    @classmethod
    def from_env(cls) -> "AppSettings":
        def _b(env_key: str, default: Optional[bool]=None):
            val = os.getenv(env_key)
            if val is None:
                return default
            return str(val).strip().lower() in ("1", "true", "yes", "y", "on")

        sim = _b("SIM_MODE", None)
        if sim is None:
            sim = _b("PAPER_MODE", False)

        api = os.getenv("HTTP_API_BASE", "").strip()
        # (라더 값은 .env 오버라이드가 필요하면 여기서 추가로 읽어도 OK)

        return cls(sim_mode=bool(sim), api_base_url=api)


# =============== 영속 스토어 (QSettings) ===============
class SettingsStore:
    ORG = "Trade"
    APP = "AutoTraderUI"
    KEY = "app_settings_v1"

    def __init__(self):
        self.qs = QSettings(self.ORG, self.APP)

    def load(self) -> AppSettings:
        base = AppSettings.from_env()
        raw = self.qs.value(self.KEY, None)
        if isinstance(raw, dict):
            merged = asdict(base)
            merged.update(raw)
            return AppSettings(**merged)
        # 구버전 호환
        auto_buy = self.qs.value("auto_buy", None, type=bool)
        auto_sell = self.qs.value("auto_sell", None, type=bool)
        if auto_buy is not None: base.auto_buy = bool(auto_buy)
        if auto_sell is not None: base.auto_sell = bool(auto_sell)
        return base

    def save(self, cfg: AppSettings):
        self.qs.setValue(self.KEY, asdict(cfg))
        self.qs.setValue("auto_buy", cfg.auto_buy)
        self.qs.setValue("auto_sell", cfg.auto_sell)


# =============== 설정 다이얼로그 ===============
class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None, cfg: AppSettings):
        super().__init__(parent)
        self.setWindowTitle("환경설정")
        self.cfg = cfg

        lay = QVBoxLayout(self)

        # 트레이딩 스위치
        row1 = QHBoxLayout()
        self.cb_master = QCheckBox("마스터 ON")
        self.cb_auto_buy = QCheckBox("자동 매수")
        self.cb_auto_sell = QCheckBox("자동 매도")
        row1.addWidget(self.cb_master); row1.addWidget(self.cb_auto_buy); row1.addWidget(self.cb_auto_sell)
        row1.addStretch(1)
        lay.addLayout(row1)

        # MACD 30 필터
        row2 = QHBoxLayout()
        self.cb_macd = QCheckBox("MACD 30m 필터 사용(hist ≥ 0)")
        self.cmb_macd_tf = QComboBox(); self.cmb_macd_tf.addItems(["30m"])
        self.sp_macd_age = QSpinBox(); self.sp_macd_age.setRange(60, 24*3600); self.sp_macd_age.setSuffix(" sec")
        row2.addWidget(self.cb_macd)
        row2.addWidget(QLabel("TF")); row2.addWidget(self.cmb_macd_tf)
        row2.addWidget(QLabel("최대 지연")); row2.addWidget(self.sp_macd_age)
        row2.addStretch(1)
        lay.addLayout(row2)

        # 모니터 루프 & 바 마감창
        row3 = QHBoxLayout()
        self.sp_poll = QSpinBox(); self.sp_poll.setRange(5, 120); self.sp_poll.setSuffix(" sec")
        self.sp_close_s = QSpinBox(); self.sp_close_s.setRange(0, 59); self.sp_close_s.setSuffix(" s")
        self.sp_close_e = QSpinBox(); self.sp_close_e.setRange(0, 59); self.sp_close_e.setSuffix(" s")
        row3.addWidget(QLabel("폴링 주기")); row3.addWidget(self.sp_poll)
        row3.addSpacing(12)
        row3.addWidget(QLabel("5m 마감창 시작")); row3.addWidget(self.sp_close_s)
        row3.addWidget(QLabel("종료")); row3.addWidget(self.sp_close_e)
        row3.addStretch(1)
        lay.addLayout(row3)

        # 시뮬/실거래 & 엔드포인트
        row4 = QHBoxLayout()
        self.cb_sim = QCheckBox("시뮬레이션 모드 (SIM_MODE/PAPER_MODE 대체)")
        self.le_api = QLineEdit(); self.le_api.setPlaceholderText("API Base URL (비우면 .env/기본값)")
        row4.addWidget(self.cb_sim)
        row4.addSpacing(12)
        row4.addWidget(QLabel("API Base")); row4.addWidget(self.le_api, 1)
        lay.addLayout(row4)

        # 라더(사다리) 매수 설정 ✅ 추가
        row_ladder = QHBoxLayout()
        self.sp_ladder_unit_amount = QSpinBox(); self.sp_ladder_unit_amount.setRange(10_000, 50_000_000); self.sp_ladder_unit_amount.setSingleStep(10_000); self.sp_ladder_unit_amount.setSuffix(" 원")
        self.sp_ladder_num_slices = QSpinBox(); self.sp_ladder_num_slices.setRange(1, 100); self.sp_ladder_num_slices.setSuffix(" 회")
        row_ladder.addWidget(QLabel("라더 1회 매수금액")); row_ladder.addWidget(self.sp_ladder_unit_amount)
        row_ladder.addSpacing(12)
        row_ladder.addWidget(QLabel("라더 분할 수")); row_ladder.addWidget(self.sp_ladder_num_slices)
        row_ladder.addStretch(1)
        lay.addLayout(row_ladder)

        # 타임존(옵션)
        row5 = QHBoxLayout()
        self.le_tz = QLineEdit(); self.le_tz.setPlaceholderText("Asia/Seoul")
        row5.addWidget(QLabel("Time Zone")); row5.addWidget(self.le_tz)
        lay.addLayout(row5)

        # 버튼
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._load_to_widgets()

    def _load_to_widgets(self):
        c = self.cfg
        self.cb_master.setChecked(c.master_enable)
        self.cb_auto_buy.setChecked(c.auto_buy)
        self.cb_auto_sell.setChecked(c.auto_sell)

        self.cb_macd.setChecked(c.use_macd30_filter)
        self.cmb_macd_tf.setCurrentText(c.macd30_timeframe or "30m")
        self.sp_macd_age.setValue(int(c.macd30_max_age_sec))

        self.sp_poll.setValue(int(c.poll_interval_sec))
        self.sp_close_s.setValue(int(c.bar_close_window_start_sec))
        self.sp_close_e.setValue(int(c.bar_close_window_end_sec))

        self.cb_sim.setChecked(bool(c.sim_mode))
        self.le_api.setText(c.api_base_url or "")

        # ✅ 라더 초기값
        self.sp_ladder_unit_amount.setValue(int(c.ladder_unit_amount))
        self.sp_ladder_num_slices.setValue(int(c.ladder_num_slices))

        self.le_tz.setText(c.timezone or "Asia/Seoul")

    def get_settings(self) -> AppSettings:
        c = AppSettings(**asdict(self.cfg))
        c.master_enable = self.cb_master.isChecked()
        c.auto_buy = self.cb_auto_buy.isChecked()
        c.auto_sell = self.cb_auto_sell.isChecked()

        c.use_macd30_filter = self.cb_macd.isChecked()
        c.macd30_timeframe = self.cmb_macd_tf.currentText()
        c.macd30_max_age_sec = int(self.sp_macd_age.value())

        c.poll_interval_sec = int(self.sp_poll.value())
        c.bar_close_window_start_sec = int(self.sp_close_s.value())
        c.bar_close_window_end_sec = int(self.sp_close_e.value())

        c.sim_mode = self.cb_sim.isChecked()
        c.api_base_url = self.le_api.text().strip()

        # ✅ 라더 반영
        c.ladder_unit_amount = int(self.sp_ladder_unit_amount.value())
        c.ladder_num_slices = int(self.sp_ladder_num_slices.value())

        c.timezone = self.le_tz.text().strip() or "Asia/Seoul"
        return c
