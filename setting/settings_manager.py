# setting/settings_manager.py
"""
- 토큰 생성 버튼: APP_KEY_i / APP_SECRET_i 저장만 수행.

- 저장 버튼: .cache 전체 정리 → 현재 테이블 프로필로 재발급 → KIWOOM_ACCOUNTS_JSON 최신화(덮어쓰기).
"""
from __future__ import annotations

import os
import json
from dataclasses import dataclass, asdict, field
from typing import Optional, Literal, Protocol, runtime_checkable, Iterable, List, Dict, Any

from PySide6.QtCore import QSettings, Qt, QRegularExpression, Slot
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox, QCheckBox,
    QComboBox, QDialogButtonBox, QWidget, QLineEdit, QGroupBox, QFormLayout,
    QTabWidget, QPushButton, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QRadioButton, QButtonGroup
)

# ----- AutoTrader 타입 힌트(런타임 의존 없음)
try:
    from trade_pro.auto_trader import TradeSettings as _TradeSettings, LadderSettings as _LadderSettings
except Exception:
    _TradeSettings = None  # type: ignore
    _LadderSettings = None  # type: ignore

# ----- 토큰 매니저 (행별 토큰 테스트/프로필 동기화/ENV 반영)
from utils.token_manager import (
    # Settings에서 필요한 공개 API (요구사항 적용)
    set_indexed_keys,                       # APP_KEY_n / APP_SECRET_n 저장
    set_keys as tm_set_main_keys,           # 메인(APP_KEY_1/APP_SECRET_1) 보정
    mint_tokens_from_settings_manager,      # 일괄 발급 + KIWOOM_ACCOUNTS_JSON 최신화
)

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
    # 트레이딩 스위치
    master_enable: bool = True
    auto_buy: bool = True
    auto_sell: bool = True
    buy_pro: bool = False
    sell_pro: bool = False
    # 주문 타입
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

    # 브로커 선택
    broker_vendor: Literal["sim", "mirae", "kiwoom", "kis"] = "kiwoom"

    # 라더(사다리)
    ladder_unit_amount: int = 100_000
    ladder_num_slices: int = 10

    # 기타
    timezone: str = "Asia/Seoul"
    # 알림 설정
    enable_pf_alert: bool = True
    enable_consecutive_loss_alert: bool = True
    consecutive_loss_threshold: int = 3
    enable_daily_loss_alert: bool = True
    daily_loss_limit: float = -500000.0

    @classmethod
    def from_env(cls) -> "AppSettings":
        """환경변수 → 초기값."""
        def _order_type_from_env() -> Literal["limit", "market"]:
            raw = _s("ORDER_TYPE", "").lower()
            return "market" if raw == "market" else "limit"

        # 시뮬 모드(하위호환 키 포함)
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
            sim = False

        api = _normalize_base_url(_s("HTTP_API_BASE", ""))

        broker = (_s("BROKER_VENDOR", "") or _s("BROKER_TYPE", "")).strip().lower()
        if broker not in ("sim", "mirae", "kiwoom", "kis"):
            broker = "kiwoom"

        return cls(
            sim_mode=bool(sim),
            api_base_url=api,
            order_type=_order_type_from_env(),
            broker_vendor=broker,
        )

# ---- (신규) Kiwoom 계좌 프로필 스키마 ----
@dataclass
class KiwoomProfile:
    id: str                 # 내부 식별자(임의 문자열)
    account_id: Optional[str] = ""   # 계좌번호(메인 선택시 필수였으나, 이제 선택사항)
    alias: str = ""         # 별칭
    app_key: str = ""
    app_secret: str = ""
    enabled: bool = True    # 체크박스 ON/OFF

@dataclass
class KiwoomSettings:
    profiles: List[KiwoomProfile] = field(default_factory=list)
    base_url: str = ""           # 비우면 브로커 기본값/환경변수 사용
    main_account_id: str = ""    # 메인(조건검색/시세수신) 계좌 — 비워도 저장 허용

# ===================== 영속 스토어(QSettings) =====================
class SettingsStore:
    ORG = "Trade"
    APP = "AutoTraderUI"
    KEY = "app_settings_v1"

    def __init__(self):
        self.qs = QSettings(self.ORG, self.APP)

    def load(self) -> AppSettings:
        base = AppSettings.from_env()
        raw = self.qs.value(self.KEY, None)

        # bytes → str
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except Exception:
                raw = None

        # str(JSON) → dict
        if isinstance(raw, str) and raw.strip():
            try:
                raw = json.loads(raw)
            except Exception:
                raw = None

        if isinstance(raw, dict):
            merged = asdict(base)
            merged.update(raw)
            return AppSettings(
                master_enable=bool(merged.get("master_enable", True)),
                auto_buy=bool(merged.get("auto_buy", True)),
                auto_sell=bool(merged.get("auto_sell", True)),
                buy_pro=bool(merged.get("buy_pro", False)),
                sell_pro=bool(merged.get("sell_pro", False)),
                order_type=("market" if str(merged.get("order_type", "limit")) == "market" else "limit"),
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
                broker_vendor=(
                    str(merged.get("broker_vendor", base.broker_vendor)).strip().lower()
                    if str(merged.get("broker_vendor", "")).strip().lower() in ("sim","mirae","kiwoom","kis")
                    else base.broker_vendor
                ),
            )

        # --- 구버전 키 반영 ---
        auto_buy = self.qs.value("auto_buy", None, type=bool)
        auto_sell = self.qs.value("auto_sell", None, type=bool)
        broker_vendor = self.qs.value("broker_vendor", None, type=str)

        if auto_buy is not None:
            base.auto_buy = bool(auto_buy)
        if auto_sell is not None:
            base.auto_sell = bool(auto_sell)
        if isinstance(broker_vendor, str) and broker_vendor.strip().lower() in ("sim","mirae","kiwoom","kis"):
            base.broker_vendor = broker_vendor.strip().lower()

        return base

    def save(self, cfg: AppSettings):
        cfg.api_base_url = _normalize_base_url(cfg.api_base_url)

        # 1) QSettings
        self.qs.setValue(self.KEY, json.dumps(asdict(cfg), ensure_ascii=False))
        self.qs.setValue("auto_buy", cfg.auto_buy)
        self.qs.setValue("auto_sell", cfg.auto_sell)
        self.qs.setValue("broker_vendor", cfg.broker_vendor)

        # 2) 런타임 환경변수
        if cfg.api_base_url:
            os.environ["HTTP_API_BASE"] = cfg.api_base_url
        if getattr(cfg, "broker_vendor", ""):
            os.environ["BROKER_VENDOR"] = cfg.broker_vendor
        if getattr(cfg, "ws_uri", ""):
            os.environ["WS_URI"] = cfg.ws_uri  # 선택적

        # 3) .env 반영(존재 시): token_manager가 .env만 쓰므로 유지
        try:
            from pathlib import Path
            env_path = Path(".env")
            if env_path.exists():
                lines = env_path.read_text(encoding="utf-8").splitlines()
            else:
                lines = []

            # BROKER_VENDOR 업데이트/추가
            found = False
            for i, line in enumerate(lines):
                if line.startswith("BROKER_VENDOR="):
                    lines[i] = f"BROKER_VENDOR={cfg.broker_vendor}"
                    found = True
                    break
            if not found:
                lines.append(f"BROKER_VENDOR={cfg.broker_vendor}")

            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to update .env file: {e}")

        self.qs.sync()

# ---- (신규) Kiwoom 전용 스토어 ----
class KiwoomStore:
    KEY = "kiwoom_settings_v1"

    def __init__(self):
        self.qs = QSettings(SettingsStore.ORG, SettingsStore.APP)

    def load(self) -> KiwoomSettings:
        raw = self.qs.value(self.KEY, None)
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except Exception:
                raw = None
        if isinstance(raw, str) and raw.strip():
            try:
                data = json.loads(raw)
            except Exception:
                data = {}
        elif isinstance(raw, dict):
            data = raw
        else:
            data = {}

        profiles: List[KiwoomProfile] = []
        for p in data.get("profiles", []) or []:
            try:
                profiles.append(KiwoomProfile(**p))
            except Exception:
                pass
        base_url = _normalize_base_url(data.get("base_url", ""))
        main_id = str(data.get("main_account_id", ""))
        return KiwoomSettings(profiles=profiles, base_url=base_url, main_account_id=main_id)

    def save(self, cfg: KiwoomSettings) -> None:
        # 중복 제거: (account_id, app_key)
        seen: Dict[str, KiwoomProfile] = {}
        for p in cfg.profiles:
            k = f"{(p.account_id or '').strip()}::{p.app_key.strip()}"
            if k not in seen:
                seen[k] = p
            else:
                # 병합 규칙: enabled OR, alias/secret 최신값
                a = seen[k]
                a.enabled = a.enabled or p.enabled
                if p.alias:
                    a.alias = p.alias
                if p.app_secret:
                    a.app_secret = p.app_secret
        serial = {
            "profiles": [asdict(x) for x in seen.values()],
            "base_url": _normalize_base_url(cfg.base_url or ""),
            "main_account_id": cfg.main_account_id or "",
        }
        self.qs.setValue(self.KEY, json.dumps(serial, ensure_ascii=False))
        self.qs.sync()

# ===================== (신규) 키움 계좌 관리 탭 =====================
class _KiwoomAccountsTab(QWidget):
    """
    - '키움 계좌 관리' 탭에서 멀티계정 설정/저장
    - 메인 계좌(라디오 버튼) 1개 선택 (계좌번호 비워도 저장 허용)
    - 각 행에 App Key/Secret 입력 후 '선택 토큰 발급' 버튼은 이제 '키 저장' 동작만 수행
    """
    COLS = ["메인", "활성", "계좌번호", "별칭", "App Key", "App Secret", "상태"]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = KiwoomStore()
        self._radio_group = QButtonGroup(self)      # 메인계좌 단일 선택
        self._radio_group.setExclusive(True)
        self._build_ui()
        self._load()

    # ───────────── 내부 전용 헬퍼들 (공개 API 변경 없음) ─────────────
    def __purge_kiwoom_cache(self, namespaces: tuple[str, ...] = ("kiwoom-prod",)) -> int:
        """
        .cache 내 토큰 JSON을 **네임스페이스 기준으로 전부 삭제**.
        - token_manager 공개 API를 건드리지 않기 위해 여기서 자체 구현.
        - 파일 내용(JSON)의 "namespace" == 대상이면 삭제.
        - 안전망: 파일명이 '{ns}-' 접두면도 삭제.
        반환: 삭제된 파일 수
        """
        from pathlib import Path
        import json as _json
        removed = 0
        cache_dir = (Path.cwd() / ".cache")
        if not cache_dir.exists():
            return 0
        for p in cache_dir.glob("*.json"):
            try:
                data = _json.loads(p.read_text(encoding="utf-8"))
                ns = str(data.get("namespace") or "")
                if ns and ns in namespaces:
                    p.unlink(missing_ok=True)
                    removed += 1
                    continue
            except Exception:
                # 내용 파싱 실패 시 파일명 접두로 추정
                if any(p.name.startswith(f"{ns}-") for ns in namespaces):
                    try:
                        p.unlink(missing_ok=True)
                        removed += 1
                    except Exception:
                        pass
        return removed

    def __reissue_all_tokens_and_refresh_env(self, profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        현재 테이블의 프로필만 대상으로 **일괄 토큰 재발급**하고,
        token_manager의 표준 공개 API로 **KIWOOM_ACCOUNTS_JSON을 최신 리스트로 덮어쓰기**.
        - 공개 API( mint_tokens_from_settings_manager )만 호출
        - 외부 시그니처 변경 없음
        """
        latest_accounts = mint_tokens_from_settings_manager(
            profiles,
            cache_namespace="kiwoom-prod",
            write_dotenv=True,       # .env에도 반영
        )
        return latest_accounts

    # ---- UI ----
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0,0,0,0)
        outer.setSpacing(8)

        # 테이블
        self.tbl = QTableWidget(0, len(self.COLS))
        self.tbl.setHorizontalHeaderLabels(self.COLS)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        outer.addWidget(self.tbl)

        # 버튼들
        btns = QHBoxLayout()
        self.btn_add = QPushButton("추가")
        self.btn_del = QPushButton("삭제")
        self.btn_token = QPushButton("선택 키 저장")
        self.btn_save = QPushButton("저장")
        btns.addWidget(self.btn_add)
        btns.addWidget(self.btn_del)
        btns.addWidget(self.btn_token)
        btns.addStretch(1)
        btns.addWidget(self.btn_save)
        outer.addLayout(btns)

        # Base URL
        base_row = QHBoxLayout()
        base_row.addWidget(QLabel("Kiwoom Base URL"))
        self.le_base = QLineEdit(); self.le_base.setPlaceholderText("비우면 기본값/환경변수")
        url_regex = QRegularExpression(r"^$|^https?://[^\s/$.?#].[^\s]*$")
        self.le_base.setValidator(QRegularExpressionValidator(url_regex))
        base_row.addWidget(self.le_base)
        outer.addLayout(base_row)

        self.btn_add.clicked.connect(self._on_add)
        self.btn_del.clicked.connect(self._on_del)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_token.clicked.connect(self._on_token)

    # ---- 데이터 로드/세이브 ----
    def _load(self):
        cfg = self.store.load()
        self.tbl.setRowCount(0)
        for p in cfg.profiles:
            self._append_row(p, is_main=(p.account_id == cfg.main_account_id))
        self.le_base.setText(cfg.base_url or "")

        # 메인 없으면 첫 행을 메인으로
        if self.tbl.rowCount() > 0 and not any(
            isinstance(self.tbl.cellWidget(r, 0), QRadioButton) and self.tbl.cellWidget(r,0).isChecked()
            for r in range(self.tbl.rowCount())
        ):
            rb = self.tbl.cellWidget(0, 0)
            if isinstance(rb, QRadioButton):
                rb.setChecked(True)

    def _append_row(self, p: Optional[KiwoomProfile] = None, *, is_main: bool = False):
        r = self.tbl.rowCount()
        self.tbl.insertRow(r)

        # 메인(라디오)
        rb = QRadioButton()
        self.tbl.setCellWidget(r, 0, rb)
        self._radio_group.addButton(rb)
        rb.setChecked(is_main)

        # 활성(체크)
        chk = QTableWidgetItem()
        chk.setFlags(chk.flags() | Qt.ItemIsUserCheckable)
        chk.setCheckState(Qt.Checked if ((p.enabled if p else True)) else Qt.Unchecked)
        self.tbl.setItem(r, 1, chk)

        # 계좌/별칭/키/시크릿/상태
        for c, text, editable in [
            (2, (p.account_id if p else ""), True),
            (3, (p.alias if p else ""), True),
            (4, (p.app_key if p else ""), True),
            (5, (p.app_secret if p else ""), True),
            (6, "-", False),
        ]:
            it = QTableWidgetItem(text)
            if not editable:
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            if c == 5:  # 비밀번호처럼 표시
                it.setText("•" * len(text))
                it.setData(Qt.UserRole, text)
            self.tbl.setItem(r, c, it)

    def _collect(self) -> KiwoomSettings:
        profiles: List[KiwoomProfile] = []
        main_id = ""
        # 라디오 체크된 행 파악
        radio_row = -1
        for r in range(self.tbl.rowCount()):
            rb = self.tbl.cellWidget(r, 0)
            if isinstance(rb, QRadioButton) and rb.isChecked():
                radio_row = r
                break

        for r in range(self.tbl.rowCount()):
            rb = self.tbl.cellWidget(r, 0)
            is_main = (r == radio_row) and isinstance(rb, QRadioButton) and rb.isChecked()

            enabled = self.tbl.item(r, 1).checkState() == Qt.Checked
            account = (self.tbl.item(r, 2).text() if self.tbl.item(r,2) else "").strip()  # ← 비워도 됨
            alias   = (self.tbl.item(r, 3).text() if self.tbl.item(r,3) else "").strip()
            app_key = (self.tbl.item(r, 4).text() if self.tbl.item(r,4) else "").strip()

            sec_it  = self.tbl.item(r, 5)
            app_sec = (sec_it.data(Qt.UserRole) if sec_it and sec_it.data(Qt.UserRole) else (sec_it.text() if sec_it else "")).strip()

            # App Key/Secret 필수
            if not app_key or not app_sec:
                continue

            if is_main:
                # 메인 계좌는 이제 계좌번호가 비어도 저장 허용
                main_id = account or ""

            # QSettings용 프로필 id (토큰매니저 upsert에 그대로 전달)
            pid = f"{(account or 'noacc')}:{app_key[:4]}"

            profiles.append(KiwoomProfile(
                id=pid,
                account_id=account or "",
                alias=alias,
                app_key=app_key,
                app_secret=app_sec,
                enabled=enabled,
            ))

        return KiwoomSettings(
            profiles=profiles,
            base_url=self.le_base.text().strip(),
            main_account_id=main_id,  # 빈 문자열이어도 저장
        )



    # ---- 버튼 핸들러 ----
    @Slot()
    def _on_add(self):
        self._append_row()

    @Slot()
    def _on_del(self):
        r = self.tbl.currentRow()
        if r >= 0:
            # 1) .env에서 해당 인덱스(i = r+1)의 APP_KEY_i / APP_SECRET_i 제거
            self.__remove_indexed_keys_from_env(r + 1)

            # 2) UI 테이블에서 행 삭제
            self.tbl.removeRow(r)

            # (선택 사항) 상태 표시 갱신 또는 안내 메시지 필요 시 아래와 같이 추가 가능
            QMessageBox.information(self, "삭제", f"APP_KEY_{r+1} / APP_SECRET_{r+1} 제거 및 행 삭제 완료")

    def __remove_indexed_keys_from_env(self, index: int) -> None:
        """
        .env에서 APP_KEY_{index}, APP_SECRET_{index} 라인을 제거하고
        os.environ에서도 제거한다.
        """
        try:
            from pathlib import Path
            env_path = Path(".env")
            if env_path.exists():
                lines = env_path.read_text(encoding="utf-8").splitlines()
            else:
                lines = []

            key_prefix  = f"APP_KEY_{index}="
            sec_prefix  = f"APP_SECRET_{index}="

            new_lines = [
                ln for ln in lines
                if not (ln.startswith(key_prefix) or ln.startswith(sec_prefix))
            ]

            # 파일 업데이트 (.env가 비게 되더라도 정상 동작)
            env_text = "\n".join(new_lines)
            if env_text and not env_text.endswith("\n"):
                env_text += "\n"
            env_path.write_text(env_text, encoding="utf-8")

            # 프로세스 환경변수도 함께 정리
            os.environ.pop(f"APP_KEY_{index}", None)
            os.environ.pop(f"APP_SECRET_{index}", None)
        except Exception:
            # 조용히 무시 (UI 상에서 삭제는 계속 진행)
            pass

    @Slot()
    def _on_save(self):
        from utils.token_manager import update_env_variable, set_keys as _set_main_keys

        cfg = self._collect()
        if not cfg.profiles:
            QMessageBox.warning(self, "입력 필요", "최소 1개 프로필(App Key/Secret)을 입력하세요.")
            return

        # 1) UI 영속화(QSettings)
        self.store.save(cfg)

        # 2) .env에 APP_KEY_n / APP_SECRET_n (행 인덱스 기준) 저장
        #    - 요구사항③: 세팅매니저 인덱스로 저장
        for i, p in enumerate(cfg.profiles, start=1):
            try:
                set_indexed_keys(i, p.app_key, p.app_secret)
            except Exception:
                pass

        # 3) 메인 키 보정 (요구사항①: 메인 토큰은 APP_KEY_1/APP_SECRET_1 사용)
        #    - 라디오 선택 행의 키를 APP_KEY_1/APP_SECRET_1로 설정
        try:
            chosen_main_keys: Optional[tuple[str, str]] = None
            for r in range(self.tbl.rowCount()):
                rb = self.tbl.cellWidget(r, 0)
                if isinstance(rb, QRadioButton) and rb.isChecked():
                    ak = (self.tbl.item(r, 4).text() if self.tbl.item(r,4) else "").strip()
                    it = self.tbl.item(r, 5)
                    sk = (it.data(Qt.UserRole) if it and it.data(Qt.UserRole) else (it.text() if it else "")).strip()
                    if ak and sk:
                        chosen_main_keys = (ak, sk)
                    break
            if chosen_main_keys:
                tm_set_main_keys(*chosen_main_keys)
            else:
                # 라디오가 없으면 첫 활성 프로필로 보정
                pick = next((p for p in cfg.profiles if p.enabled and p.app_key and p.app_secret), None)
                if pick:
                    tm_set_main_keys(pick.app_key, pick.app_secret)
        except Exception:
            pass

        # 4) .cache(kiwoom) **전부 삭제** → 현재 프로필로 **재발급** → KIWOOM_ACCOUNTS_JSON **덮어쓰기**
        try:
            profiles = [
                {
                    "account_id": (p.account_id or ""),
                    "app_key": p.app_key,
                    "app_secret": p.app_secret,
                    "alias": (p.alias or (p.account_id or p.id)),
                    "enabled": bool(p.enabled),
                }
                for p in cfg.profiles
            ]
            try:
                removed = self.__purge_kiwoom_cache(("kiwoom-prod",))
                __import__("logging").getLogger(__name__).info("purged kiwoom cache: %s files", removed)
            except Exception:
                pass

            latest_accounts = self.__reissue_all_tokens_and_refresh_env(profiles)
            QMessageBox.information(
                self, "저장 완료",
                f"키움 계좌 설정 저장 및 토큰 최신화 완료\n"
                f"(활성 계정 {len([a for a in latest_accounts if a.get('enabled', True)])}개)"
            )
        except Exception as e:
            QMessageBox.warning(
                self, "저장(일부)",
                f"설정 저장은 완료했으나 토큰 최신화 중 문제가 발생했습니다.\n사유: {e}"
            )

    @Slot()
    def _on_token(self):
        r = self.tbl.currentRow()
        if r < 0:
            QMessageBox.warning(self, "선택 필요", "토큰을 발급할 행을 선택하세요.")
            return

        app_key = (self.tbl.item(r, 4).text() if self.tbl.item(r,4) else "").strip()
        sec_it  = self.tbl.item(r, 5)
        app_sec = (sec_it.data(Qt.UserRole) if sec_it and sec_it.data(Qt.UserRole) else (sec_it.text() if sec_it else "")).strip()

        if not (app_key and app_sec):
            QMessageBox.warning(self, "입력 필요", "App Key / App Secret 을 입력하세요.")
            return
        try:
            # 요구사항 변경: "토큰 생성" 버튼은 **키 저장만** (캐시/ENV 변경 없음)
            set_indexed_keys(r + 1, app_key, app_sec)
            self.tbl.setItem(r, 6, QTableWidgetItem("키 저장됨"))
            QMessageBox.information(self, "저장", f"APP_KEY_{r+1} / APP_SECRET_{r+1} 저장 완료")
        except Exception as e:
            self.tbl.setItem(r, 6, QTableWidgetItem("저장 실패"))
            QMessageBox.critical(self, "실패", f"키 저장 실패: {e}")

# ===================== 설정 다이얼로그 =====================
class SettingsDialog(QDialog):
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
        self.setMinimumWidth(820)

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

        # --- 주문/연동 그룹 ---
        grp_order = QGroupBox("주문/연동")
        fo = QFormLayout(grp_order); fo.setLabelAlignment(Qt.AlignRight)
        self.cmb_order_type = QComboBox()
        self.cmb_order_type.addItems(["지정가", "시장가"])
        self.cmb_broker = QComboBox()
        self._broker_items = [
            ("시뮬레이터", "sim"),
            ("키움", "kiwoom"),
            ("미래에셋(미지원)", "mirae"),
            ("한국투자(미지원)", "kis"),
        ]
        for label, _code in self._broker_items:
            self.cmb_broker.addItem(label)
        self.cb_sim = QCheckBox("시뮬레이션 모드 (SIM_MODE/PAPER_MODE 대체)")
        self.le_api = QLineEdit(); self.le_api.setPlaceholderText("API Base URL (비우면 .env/기본값)")
        url_regex = QRegularExpression(r"^$|^https?://[^\s/$.?#].[^\s]*$")
        self.le_api.setValidator(QRegularExpressionValidator(url_regex))

        fo.addRow("브로커(증권사)", self.cmb_broker)
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

        # --- 모니터/마감창 그룹 ---
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

        self.tab_general.setLayout(lay)

        # ---------- (탭2) 키움 계좌 관리 ----------
        self.tab_kiwoom = _KiwoomAccountsTab(self)

        # 탭 추가
        self.tabs.addTab(self.tab_general, "매매 설정")
        self.tabs.addTab(self.tab_kiwoom, "키움 계좌 관리")
        outer.addWidget(self.tabs)

        # 하단 버튼
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

        # 간단 스타일
        self.setStyleSheet("""
            QGroupBox {
                font-weight: 600;
                border: 1px solid rgba(128,128,128,0.35);
                border-radius: 8px;
                margin-top: 12px;
            }
            QGroupBox::title { left: 8px; padding: 0 4px; }
            QLineEdit, QComboBox, QSpinBox { min-height: 26px; }
        """)

    # ---------------- UI ← 설정 ----------------
    def _load_to_widgets(self):
        c = self.cfg
        self.cb_auto_buy.setChecked(c.auto_buy)
        self.cb_auto_sell.setChecked(c.auto_sell)
        self.cb_buy_pro.setChecked(c.buy_pro)
        self.cb_sell_pro.setChecked(c.sell_pro)

        self.cmb_order_type.setCurrentText("시장가" if c.order_type == "market" else "지정가")

        cur_code = (c.broker_vendor or "kiwoom").lower()
        idx = 0
        for i, (_label, code) in enumerate(self._broker_items):
            if code == cur_code:
                idx = i; break
        self.cmb_broker.setCurrentIndex(idx)

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

        order_txt = self.cmb_order_type.currentText()
        c.order_type = "market" if order_txt == "시장가" else "limit"

        bi = max(0, self.cmb_broker.currentIndex())
        _, code = self._broker_items[bi]
        c.broker_vendor = code

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
        if not self.le_api.hasAcceptableInput():
            self.le_api.setFocus()
            return
        self._save_geometry()
        super().accept()

    def reject(self):
        self._save_geometry()
        super().reject()

# ===================== AutoTrader 연동 헬퍼 =====================
@runtime_checkable
class _Configurable(Protocol):
    def apply_settings(self, cfg: AppSettings) -> None: ...

def to_trade_settings(cfg: AppSettings):
    """AppSettings → AutoTrader.TradeSettings 변환"""
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
    """AppSettings → AutoTrader.LadderSettings 변환"""
    if _LadderSettings is None:
        raise RuntimeError("trade_pro.auto_trader.LadderSettings 를 불러올 수 없습니다.")
    lad = _LadderSettings()
    lad.unit_amount = int(cfg.ladder_unit_amount)
    lad.num_slices = int(cfg.ladder_num_slices)
    return lad

def apply_to_autotrader(trader, cfg: AppSettings):
    """
    이미 생성된 AutoTrader 인스턴스에 UI 설정 반영.
    (하위호환 유지. 신규 코드는 apply_all_settings 사용 권장)
    """
    if hasattr(trader, "set_simulation_mode"):
        trader.set_simulation_mode(bool(cfg.sim_mode))

    if hasattr(trader, "settings"):
        s = trader.settings
        s.master_enable = bool(cfg.master_enable)
        s.auto_buy      = bool(cfg.auto_buy)
        s.auto_sell     = bool(cfg.auto_sell)
        s.order_type    = ("market" if cfg.order_type == "market" else "limit")
        if hasattr(s, "buy_pro"):
            s.buy_pro = bool(cfg.buy_pro)
        if hasattr(s, "sell_pro"):
            s.sell_pro = bool(cfg.sell_pro)

    if hasattr(trader, "ladder"):
        trader.ladder.unit_amount = int(cfg.ladder_unit_amount)
        trader.ladder.num_slices  = int(cfg.ladder_num_slices)

    if cfg.api_base_url:
        os.environ["HTTP_API_BASE"] = _normalize_base_url(cfg.api_base_url)

def _adapt_autotrader(trader) -> _Configurable:
    """AutoTrader에 apply_settings가 없을 때를 위한 어댑터."""
    class _ATAdapter:
        def __init__(self, t): self.t = t
        def apply_settings(self, cfg: AppSettings) -> None:
            apply_to_autotrader(self.t, cfg)
    return _ATAdapter(trader)

def _adapt_monitor(monitor) -> _Configurable:
    """
    Monitor에 apply_settings가 없으면 set_custom 등으로 폴백.
    (ExitEntryMonitor가 apply_settings를 직접 구현했다면 그걸 우선 사용)
    """
    class _MonAdapter:
        def __init__(self, m): self.m = m
        def apply_settings(self, cfg: AppSettings) -> None:
            # 정식 API가 있으면 우선 사용
            if hasattr(self.m, "apply_settings") and callable(self.m.apply_settings):
                self.m.apply_settings(cfg)
                return
            # 폴백: 핵심 스위치 전달
            if hasattr(self.m, "set_custom") and callable(self.m.set_custom):
                try:
                    self.m.set_custom(
                        enabled=True,
                        auto_buy=cfg.auto_buy,
                        auto_sell=cfg.auto_sell,
                        allow_intrabar_condition_triggers=True,
                        buy_pro=cfg.buy_pro,
                        sell_pro=cfg.sell_pro,
                    )
                except Exception:
                    pass
            # 루프/창 파라미터 속성 반영(있을 때만)
            for name, val in [
                ("poll_interval_sec", int(cfg.poll_interval_sec)),
                ("_win_start", int(cfg.bar_close_window_start_sec)),
                ("_win_end",   int(cfg.bar_close_window_end_sec)),
                ("tz",         cfg.timezone or "Asia/Seoul"),
            ]:
                if hasattr(self.m, name):
                    try: setattr(self.m, name, val)
                    except Exception: pass
    return _MonAdapter(monitor)

def apply_all_settings(
    cfg: AppSettings,
    *,
    trader=None,
    monitor=None,
    extra: Iterable[object] | None = None,
) -> None:
    """
    단일 진입점: 전달된 모든 대상에게 AppSettings 일괄 반영.
    대상이 already apply_settings(cfg)를 구현했으면 그걸 호출,
    아니면 적절한 어댑터로 동일하게 반영.
    """
    targets: list[_Configurable] = []

    if trader is not None:
        if isinstance(trader, _Configurable):
            targets.append(trader)
        else:
            targets.append(_adapt_autotrader(trader))

    if monitor is not None:
        if isinstance(monitor, _Configurable):
            targets.append(monitor)
        else:
            targets.append(_adapt_monitor(monitor))

    if extra:
        for obj in extra:
            if obj is None:
                continue
            if isinstance(obj, _Configurable):
                targets.append(obj)
            # 필요 시 추가 어댑터 분기 가능

    for t in targets:
        try:
            t.apply_settings(cfg)
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception("apply_all_settings target failed: %s", e)
