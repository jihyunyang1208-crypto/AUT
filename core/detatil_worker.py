# detail_worker.py
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot
from typing import Dict, Any

class DetailWorker(QObject):
    detailReady = pyqtSignal(dict)     # UI가 받을 이벤트 (payload dict)
    error = pyqtSignal(str, str)       # (code, message)

    def __init__(self, market_api):
        super().__init__()
        self.api = market_api

    @pyqtSlot(str, str)  # (code, condition_name)
    def fetch_ka10001(self, code: str, condition_name: str = ""):
        """
        KA10001(주식기본정보)만 호출해서 UI가 바로 쓰는 키로 평탄화 후 이벤트로 전달.
        """
        try:
            code6 = code[:6].zfill(6)
            js = self.api.fetch_basic_info_ka10001(code6)  # dict 반환 가정

            # 평탄화: UI on_new_stock_detail 이 기대하는 키로 맞춤
            dst = {
                "stock_code": code6,
                "stock_name": js.get("stk_nm") or "종목명 없음",
            }
            if condition_name:
                dst["condition_name"] = condition_name

            # KA10001의 주요 키 → UI 키로 병합
            # trde_qty → now_trde_qty 로 매핑
            merge_map = {
                "cur_prc": "cur_prc",
                "flu_rt": "flu_rt",
                "open_pric": "open_pric",
                "high_pric": "high_pric",
                "low_pric": "low_pric",
                "trde_qty": "now_trde_qty",
                "cntr_str": "cntr_str",
                "open_pric_pre": "open_pric_pre",
            }
            for src, dst_key in merge_map.items():
                v = js.get(src)
                if v not in (None, ""):
                    dst[dst_key] = v

            self.detailReady.emit(dst)
        except Exception as e:
            self.error.emit(code, str(e))
