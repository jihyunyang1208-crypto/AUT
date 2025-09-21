import pandas as pd
import threading
import logging
from typing import Optional, Dict, Any, List
import os

logger = logging.getLogger(__name__)

# CSV 파일 경로
_FILE_PATH = "resources/krx_data.csv"

class StockInfoManager:
    """
    종목 코드, 종목명, 시장 구분 정보를 CSV 파일에서 로드하여 관리하는 싱글톤 클래스.
    """
    _instance: Optional['StockInfoManager'] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init_data()
        return cls._instance

    def _init_data(self):
        """
        CSV 파일을 읽어 DataFrame을 초기화하고, 필요한 컬럼명을 변경합니다.
        """
        self._df: Optional[pd.DataFrame] = None
        self._is_loaded = False
        
        try:
            # 🔹 파일 존재 여부 확인
            if not os.path.exists(_FILE_PATH):
                raise FileNotFoundError(f"Stock list file not found at {_FILE_PATH}.")
            
            # 🔹 한글 컬럼명으로 데이터 로드
            # pandas.errors.EmptyDataError 등을 대비해 try-except 블록을 사용합니다.
            try:
                self._df = pd.read_csv(
                    _FILE_PATH, 
                    dtype={'종목코드': str},
                    encoding='utf-8-sig',
                    usecols=['종목코드', '종목명', '시장구분']
                )
            except Exception as e:
                # read_csv에서 발생하는 모든 오류를 잡아냅니다.
                logger.error(f"Failed to read CSV file: {e}")
                self._is_loaded = False
                return # 메서드 종료

            # 🔹 데이터가 정상적으로 로드되었는지 확인
            if self._df is None or self._df.empty:
                logger.error("The CSV file was read, but no valid data was found.")
                self._is_loaded = False
                return
            
            # 🔹 컬럼명을 영문으로 변경
            self._df.rename(columns={
                '종목코드': 'code',
                '종목명': 'name',
                '시장구분': 'market'
            }, inplace=True)
            
            self._df.set_index("code", inplace=True)
            self._is_loaded = True
            logger.info("Stock info loaded successfully from CSV.")
            
        except FileNotFoundError as e:
            logger.error(str(e) + " Please check if the file exists.")
        except Exception as e:
            # 예상치 못한 다른 모든 오류 처리
            logger.error(f"An unexpected error occurred during initialization: {e}")
            self._is_loaded = False

    def get_name(self, code: str) -> str:
        if not self._is_loaded or self._df is None:
            return code
        
        norm_code = str(code).zfill(6)
        try:
            name = self._df.loc[norm_code, "name"]
            return name if pd.notna(name) else code
        except KeyError:
            return code
    
    def is_loaded(self) -> bool:
        return self._is_loaded

stock_info_manager = StockInfoManager()
