from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Optional, Literal

from PySide6.QtCore import QSettings, Qt, QRegularExpression, Slot
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox, QCheckBox,
    QComboBox, QDialogButtonBox, QWidget, QLineEdit, QGroupBox, QFormLayout,
    QTabWidget, QPushButton, QMessageBox
)

# ----- AutoTrader 타입 힌트를 위해 (런타임 의존 없음)
try:
    from trade_pro.auto_trader import TradeSettings as _TradeSettings, LadderSettings as _LadderSettings
except Exception:
    _TradeSettings = None  # type: ignore
    _LadderSettings = None  # type: ignore

# 로그인 탭 연동(앞서 제공한 호환판 token_manager 전제)
try:
    from utils.token_manager import load_keys as tm_load_keys, set_keys as tm_set_keys, request_new_token as tm_request_new_token
except Exception:
    # 토큰 모듈이 아직 없을 수 있으므로, 안전한 no-op 대체
    def tm_load_keys():
        return os.getenv("APP_KEY", ""), os.getenv("APP_SECRET", "")
    def tm_set_keys(appkey: str, appsecret: str):
        pass
    def tm_request_new_token(appkey: Optional[str] = None, appsecret: Optional[str] = None, token_url: str = "") -> str:
        raise RuntimeError("token_manager가 준비되지 않았습니다.")


# ===================== 유틸 =====================
def _b(env_key: str, default: Optional[bool] = None) -> Optional[bool]:
    val = os.getenv(env_key)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")

def _s(env_key: str, default: str = "") -> str:
    v = os.getenv(env_key)
    return (v.strip() if isinstance(v, str) else default)

def _normalize_base_url(api: str) -> str:
    api = (api or "").strip()
    if api.endswith("/"):
        api = api[:-1]
    return api


# ===================== 데이터 모델 =====================
@dataclass
class AppSettings:
    # 트레이딩 스위치 (master_enable은 하위호환용으로만 보관)
    master_enable: bool = True
    auto_buy: bool = True
    auto_sell: bool = True
    buy_pro: bool = False
    sell_pro: bool = False
    # 주문 타입(지정가/시장가)
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
    sim_mode: bool = False              # ✅ UI 시뮬레이션 스위치 (paper ≡ simulation)
    api_base_url: str = ""

    # 라더(사다리) 매수 기본 설정
    ladder_unit_amount: int = 100_000
    ladder_num_slices: int = 10

    # 유틸
    timezone: str = "Asia/Seoul"

    @classmethod
    def from_env(cls) -> "AppSettings":
        """환경변수 → 초기값. QSettings와 병합 시 '기본'으로 사용됩니다."""
        def _order_type_from_env() -> Literal["limit", "market"]:
            raw = _s("ORDER_TYPE", "").lower()
            return "market" if raw == "market" else "limit"

        # --- 시뮬 모드(하위호환 포함) ---
        sim = _b("SIM_MODE", None)
        if sim is None:
            sim = _b("SIMULATION_MODE", None)
        if sim is None:
            sim = _b("PAPER_MODE", None)

        if sim is None:
            trade_mode = (_s("TRADE_MODE", "") or "").lower()
            if trade_mode in ("paper", "sim", "simulation"):
                sim = True
            elif trade_mode in ("live", "real", "prod"):
                sim = False

        if sim is None:
            sim = False  # default

        api = _normalize_base_url(_s("HTTP_API_BASE", ""))

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
            # 타입 안정화
            return AppSettings(
                master_enable=bool(merged.get("master_enable", True)),
                auto_buy=bool(merged.get("auto_buy", True)),
                auto_sell=bool(merged.get("auto_sell", True)),
                buy_pro=bool(merged.get("buy_pro", False)),
                sell_pro=bool(merged.get("sell_pro", False)),
                order_type=("market" if merged.get("order_type", "limit") == "market" else "limit"),
                use_macd30_filter=bool(merged.get("use_macd30_filter", False)),
                macd30_timeframe=str(merged.get("macd30_timeframe", "30m")),
                macd30_max_age_sec=int(merged.get("macd30_max_age_sec", 1800)),
                poll_interval_sec=int(merged.get("poll_interval_sec", 20)),
                bar_close_window_start_sec=int(merged.get("bar_close_window_start_sec", 5)),
                bar_close_window_end_sec=int(merged.get("bar_close_window_end_sec", 30)),
                sim_mode=bool(merged.get("sim_mode", False)),
                api_base_url=_normalize_base_url(str(merged.get("api_base_url", ""))),
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
        cfg.api_base_url = _normalize_base_url(cfg.api_base_url)
        self.qs.setValue(self.KEY, asdict(cfg))
        self.qs.setValue("auto_buy", cfg.auto_buy)
        self.qs.setValue("auto_sell", cfg.auto_sell)

        # 즉시 환경변수 반영
        if cfg.api_base_url:
            os.environ["HTTP_API_BASE"] = cfg.api_base_url
        if hasattr(cfg, "ws_uri") and cfg.ws_uri:
            os.environ["WS_URI"] = cfg.ws_uri

# ===================== (신규) 로그인 탭 =====================
class _LoginTab(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self.le_appkey = QLineEdit()
        self.le_secret = QLineEdit()
        self.le_secret.setEchoMode(QLineEdit.Password)

        ak, sk = tm_load_keys()
        self.le_appkey.setText(ak or "")
        self.le_secret.setText(sk or "")

        form.addRow("APP_KEY", self.le_appkey)
        form.addRow("APP_SECRET", self.le_secret)

        btn_row = QHBoxLayout()
        self.btn_save = QPushButton("저장")
        self.btn_test = QPushButton("토큰 발급 테스트")
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_test)
        btn_row.addStretch(1)

        lay.addLayout(form)
        lay.addLayout(btn_row)
        lay.addStretch(1)

        self.btn_save.clicked.connect(self._on_save)
        self.btn_test.clicked.connect(self._on_test)

    @Slot()
    def _on_save(self):
        ak = (self.le_appkey.text() or "").strip()
        sk = (self.le_secret.text() or "").strip()
        if not ak or not sk:
            QMessageBox.warning(self, "저장 실패", "APP_KEY와 APP_SECRET을 모두 입력하세요.")
            return
        try:
            tm_set_keys(ak, sk)
            QMessageBox.information(self, "완료", "키가 저장되었습니다 (.env).")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"키 저장 실패:\n{e}")

    @Slot()
    def _on_test(self):
        try:
            tok = tm_request_new_token()  # .env/ENV를 사용하여 강제 발급
            if tok:
                QMessageBox.information(self, "성공", "토큰 발급 성공! (캐시에 저장됨)")
        except Exception as e:
            QMessageBox.critical(self, "실패", f"토큰 발급 실패:\n{e}")


# ===================== 설정 다이얼로그 =====================
class SettingsDialog(QDialog):
    """
    (호환 보장)
    - 생성자 서명: SettingsDialog(parent: QWidget|None, cfg: AppSettings)
    - .env 값은 기본값(AppSettings.from_env)로 로딩되고,
      사용자가 다이얼로그에서 조정하면 QSettings에 저장됩니다.
    - order_type(지정가/시장가) 필드(ComboBox)
    - 라더 매수 기본값(1회 금액, 분할 수) 편집 가능
    - (신규) 로그인 탭 추가: APP_KEY/APP_SECRET 저장 및 토큰 발급 테스트
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

    # ----------- UI 구성 -----------
    def _build_ui(self):
        self.setModal(True)
        self.setMinimumWidth(680)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 10)
        outer.setSpacing(10)

        self.tabs = QTabWidget(self)

        # ---------- (탭1) 일반 ----------
        self.tab_general = QWidget(self)
        lay = QVBoxLayout(self.tab_general)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # --- 트레이딩 스위치 그룹 ---
        grp_switch = QGroupBox("트레이딩 스위치")
        sw = QHBoxLayout(grp_switch)
        self.cb_auto_buy = QCheckBox("자동 매수")
        self.cb_auto_sell = QCheckBox("자동 매도")
        self.cb_buy_pro = QCheckBox("Buy-Pro(룰 기반)")
        self.cb_sell_pro = QCheckBox("Sell-Pro(룰 기반)")
        sw.addWidget(self.cb_auto_buy); sw.addSpacing(8)
        sw.addWidget(self.cb_auto_sell); sw.addSpacing(8)
        sw.addWidget(self.cb_buy_pro); sw.addSpacing(8)
        sw.addWidget(self.cb_sell_pro); sw.addStretch(1)
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

        # 일반 탭 완성
        self.tab_general.setLayout(lay)

        # ---------- (탭2) 로그인 ----------
        self.tab_login = _LoginTab(self)

        # 탭 추가
        self.tabs.addTab(self.tab_general, "매매 설정")
        self.tabs.addTab(self.tab_login, "로그인")
        outer.addWidget(self.tabs)

        # --- 버튼 (기존 그대로 Ok/Cancel) ---
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

        # 스타일(기존과 충돌 없음)
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

    # ---------------- UI ← 설정 ----------------
    def _load_to_widgets(self):
        c = self.cfg
        self.cb_auto_buy.setChecked(c.auto_buy)
        self.cb_auto_sell.setChecked(c.auto_sell)
        self.cb_buy_pro.setChecked(c.buy_pro)
        self.cb_sell_pro.setChecked(c.sell_pro)

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

    # ---------------- UI → 설정 ----------------
    def get_settings(self) -> AppSettings:
        c = AppSettings(**asdict(self.cfg))

        c.auto_buy = self.cb_auto_buy.isChecked()
        c.auto_sell = self.cb_auto_sell.isChecked()
        c.buy_pro = self.cb_buy_pro.isChecked()
        c.sell_pro = self.cb_sell_pro.isChecked()

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
        c.api_base_url = _normalize_base_url(api)

        c.ladder_unit_amount = int(self.sp_ladder_unit_amount.value())
        c.ladder_num_slices = int(self.sp_ladder_num_slices.value())

        c.timezone = self.le_tz.text().strip() or "Asia/Seoul"
        return c

    # --------------- 다이얼로그 위치/크기 기억 ---------------
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


# ===================== AutoTrader 연동 헬퍼 =====================
def to_trade_settings(cfg: AppSettings):
    """
    AppSettings → AutoTrader.TradeSettings 변환
    - simulation_mode로 단일 스위치 연결 (paper ≡ simulation)
    - master_enable은 하위호환 필드로만 전달(현재 AutoTrader 로직에서는 미사용)
    """
    if _TradeSettings is None:
        raise RuntimeError("trade_pro.auto_trader.TradeSettings 를 불러올 수 없습니다.")
    return _TradeSettings(
        master_enable=bool(cfg.master_enable),
        auto_buy=bool(cfg.auto_buy),
        auto_sell=bool(cfg.auto_sell),
        order_type=("market" if cfg.order_type == "market" else "limit"),
        simulation_mode=bool(cfg.sim_mode),
    )

def to_ladder_settings(cfg: AppSettings):
    """
    AppSettings → AutoTrader.LadderSettings 변환
    - unit_amount/num_slices만 기본 매핑 (나머지는 AutoTrader 기본값 사용)
    """
    if _LadderSettings is None:
        raise RuntimeError("trade_pro.auto_trader.LadderSettings 를 불러올 수 없습니다.")
    lad = _LadderSettings()
    lad.unit_amount = int(cfg.ladder_unit_amount)
    lad.num_slices = int(cfg.ladder_num_slices)
    return lad

def apply_to_autotrader(trader, cfg: AppSettings):
    """
    이미 생성된 AutoTrader 인스턴스에 UI 설정을 반영.
    - simulation_mode 런타임 토글
    - order_type, auto_* 스위치 반영 (master_enable은 유지 전달되지만 AutoTrader에서는 미사용)
    - ladder 기본값 반영
    - base url provider를 쓰는 구조라면, 환경변수(HTTP_API_BASE)를 업데이트하는 방식으로 전달 가능
    """
    # simulation toggle (런타임 반영)
    if hasattr(trader, "set_simulation_mode"):
        trader.set_simulation_mode(bool(cfg.sim_mode))

    # settings 객체 값 반영
    if hasattr(trader, "settings"):
        trader.settings.master_enable = bool(cfg.master_enable)  # 하위호환 보관
        trader.settings.auto_buy = bool(cfg.auto_buy)
        trader.settings.auto_sell = bool(cfg.auto_sell)
        trader.settings.order_type = ("market" if cfg.order_type == "market" else "limit")
        # 프로 스위치가 TradeSettings에 존재한다면 반영
        if hasattr(trader.settings, "buy_pro"):
            trader.settings.buy_pro = bool(cfg.buy_pro)
        if hasattr(trader.settings, "sell_pro"):
            trader.settings.sell_pro = bool(cfg.sell_pro)


    if hasattr(trader, "ladder"):
        trader.ladder.unit_amount = int(cfg.ladder_unit_amount)
        trader.ladder.num_slices = int(cfg.ladder_num_slices)

    # API Base URL은 AutoTrader가 base_url_provider를 통해 읽습니다.
    if cfg.api_base_url:
        os.environ["HTTP_API_BASE"] = _normalize_base_url(cfg.api_base_url)
