from __future__ import annotations
import os
from pathlib import Path
import google.generativeai as genai
import logging

logger = logging.getLogger(__name__)

# ===== Gemini 클라이언트 =====
class GeminiClient:
    def __init__(self, prompt_file: str = "resources/daily_briefing_prompt.md"):
        self.prompt_file = Path(prompt_file)

        # API 키: 환경설정에서 읽음
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            # 🔹 API 키가 없으면 RuntimeError를 발생시켜 호출자에게 알림
            raise RuntimeError("환경변수 GEMINI_API_KEY 가 설정되어 있지 않습니다.")
        
        genai.configure(api_key=api_key)

        # 모델 초기화 (필요시 모델명 교체)
        self.model = genai.GenerativeModel("gemini-1.5-flash")

    def _load_prompt(self) -> str:
        if not self.prompt_file.exists():
            # 🔹 프롬프트 파일이 없으면 FileNotFoundError를 발생
            raise FileNotFoundError(f"프롬프트 파일 없음: {self.prompt_file}")
        return self.prompt_file.read_text(encoding="utf-8").strip()

    def run_briefing(self, extra_context: str | None = None) -> str:
        """
        - 프롬프트 파일에서 기본 프롬프트 로드
        - 필요시 추가 context 붙여 Gemini 호출
        """
        try:
            base_prompt = self._load_prompt()
            if extra_context:
                full_prompt = f"{base_prompt}\n\n추가 정보:\n{extra_context}"
            else:
                full_prompt = base_prompt

            response = self.model.generate_content(full_prompt)
            return response.text.strip() if hasattr(response, "text") else str(response)
        except Exception as e:
            # 🔹 API 호출 중 발생한 예외를 로그하고 다시 발생시킴
            logger.exception("Gemini content generation failed.")
            raise e