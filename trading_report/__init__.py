# -*- coding: utf-8 -*-

# report_api 모듈에서 PyQt UI가 사용할 핵심 함수인 get_report_html을 가져옵니다.
from .report_api import get_report_html

# 이 패키지에서 외부로 공개할 함수 목록을 정의합니다.
# "from trading_report import *" 구문 사용 시 아래 목록의 함수만 임포트됩니다.
__all__ = ["get_report_html"]
