from __future__ import annotations
import os
from pathlib import Path
import google.generativeai as genai
import logging
from google.api_core import exceptions as gax_exceptions

logger = logging.getLogger(__name__)
from dotenv import load_dotenv
load_dotenv()

# 1) 안전한 기본값과 폴백 맵
PREFERRED_MODELS = [
    "gemini-2.5-flash-lite",  # 가장 저렴/고속 (가용 시)
    "gemini-2.0-flash",       # 범용 고속
    "gemini-2.0-pro",         # 정교함/긴맥락
]

def _get_first_available_model():
    # 모델 리스트 조회로 실제 가용 모델 확인
    try:
        available = {m.name for m in genai.list_models()}
        for m in PREFERRED_MODELS:
            if m in available:
                return m
    except Exception:
        pass
    # 조회 실패 시에도 무난한 기본값
    return "gemini-2.0-flash"


# ===== Gemini 클라이언트 =====
class GeminiClient:
    def __init__(self, prompt_file: str | Path | None = None):
        """
        GeminiClient를 초기화합니다.
        - prompt_file (선택 사항): 이전 버전과의 호환성을 위해 특정 프롬프트 파일을 지정할 수 있습니다.
                                   지정하지 않으면 범용 클라이언트로 동작합니다.
        """
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("환경변수 GEMINI_API_KEY 가 설정되어 있지 않습니다.")
        
        genai.configure(api_key=api_key)
        self.model_name = _get_first_available_model()
        self.model = genai.GenerativeModel(self.model_name)
        
        # 하위 호환성을 위해 prompt_file 속성 유지
        if prompt_file:
            self.prompt_file = Path(prompt_file)
        else:
            self.prompt_file = None

    def _load_prompt(self) -> str:
        """
        (하위 호환성) 초기화 시 지정된 프롬프트 파일을 로드합니다.
        """
        if not self.prompt_file or not self.prompt_file.exists():
            raise FileNotFoundError(f"프롬프트 파일이 지정되지 않았거나 찾을 수 없습니다: {self.prompt_file}")
        return self.prompt_file.read_text(encoding="utf-8").strip()

    def generate_text(self, prompt: str, max_tokens: int = 800) -> str:
        """
        [범용] 주어진 프롬프트를 사용하여 텍스트를 생성합니다. (daily_report_generator에서 사용)
        """
        try:
            generation_config = genai.types.GenerationConfig(
                max_output_tokens=max_tokens
            )
            response = self.model.generate_content(
                prompt,
                generation_config=generation_config
            )
            return response.text.strip() if hasattr(response, "text") and response.text else ""
        except gax_exceptions.PermissionDenied as e:
            logger.error(f"Gemini API 권한 오류: API 키 또는 모델('{self.model_name}') 설정을 확인하세요. - {e}")
            raise
        except Exception as e:
            logger.exception(f"Gemini 콘텐츠 생성 중 예상치 못한 오류 발생 ({self.model_name} 모델).")
            raise e

    def run_briefing(self, extra_context: str | None = None) -> str:
        """
        [하위 호환성] 프롬프트 파일을 기반으로 브리핑을 실행합니다. (기존 모듈에서 사용)
        """
        base_prompt = self._load_prompt()
        full_prompt = f"{base_prompt}\n\n추가 정보:\n{extra_context}" if extra_context else base_prompt
        
        # 내부적으로 범용 함수인 generate_text를 호출
        return self.generate_text(prompt=full_prompt)