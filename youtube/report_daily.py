# report_daily_md.py
import os, io, csv, json, time, requests
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv, find_dotenv
from string import Template   

# ========== 설정 ==========
load_dotenv(find_dotenv())

GEMINI_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_KEY or GEMINI_KEY.startswith("YOUR_"):
    raise RuntimeError("GEMINI_API_KEY가 비어있거나 플레이스홀더입니다. .env에 실제 키(AI Studio 발급)를 넣어주세요.")

# 모델/엔드포인트 (테스트·초안은 flash 권장)
GEMINI_MODEL = "gemini-1.5-flash"
GEMINI_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# ========== 유틸 ==========
def today_str():
    return datetime.now().strftime("%Y-%m-%d")

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def read_system_results(date_str: str) -> tuple[str, list[list[str]]]:
    """
    data/system_results_{date}.json 또는 .csv를 읽어
    - 프롬프트에 넣을 CSV 문자열
    - 테이블 렌더용 2차원 리스트
    를 반환. 파일이 없으면 샘플 사용.
    """
    data_dir = Path("data")
    json_path = data_dir / f"system_results_{date_str}.json"
    csv_path  = data_dir / f"system_results_{date_str}.csv"

    if json_path.exists():
        with open(json_path, "r", encoding="utf-8") as f:
            arr = json.load(f)
        # JSON → CSV 문자열
        header = ["ticker","entry_time","exit_time","pl_pct","notes"]
        rows = [header] + [
            [str(x.get("ticker","")), str(x.get("entry_time","")), str(x.get("exit_time","")),
             str(x.get("pl_pct","")), str(x.get("notes",""))] for x in arr
        ]
        csv_text = "\n".join([",".join(r) for r in rows])
        return csv_text, rows

    if csv_path.exists():
        text = csv_path.read_text(encoding="utf-8").strip()
        f = io.StringIO(text)
        reader = csv.reader(f)
        rows = [row for row in reader]
        return text, rows

    # 둘 다 없으면 샘플
    sample = """ticker,entry_time,exit_time,pl_pct,notes
005930,09:17,10:05,1.8,돌파/거래량 2.1x
035420,10:12,10:48,-0.7,전고점 저항 후 손절"""
    rows = [r.split(",") for r in sample.splitlines()]
    return sample, rows

def read_tickers_from_rows(rows: list[list[str]]) -> list[str]:
    if not rows or len(rows) < 2: return []
    # 첫 컬럼이 ticker라고 가정
    return sorted({r[0] for r in rows[1:] if r and r[0]})

def read_image_urls(date_str: str) -> list[str]:
    """
    data/chart_images_{date}.txt (줄바꿈으로 URL 나열)가 있으면 읽어
    마크다운 이미지로 삽입한다. 없으면 빈 리스트.
    """
    p = Path("data") / f"chart_images_{date_str}.txt"
    if not p.exists(): return []
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln and (ln.startswith("http://") or ln.startswith("https://"))]

def csv_to_md_table(rows: list[list[str]]) -> str:
    if not rows: return ""
    header = rows[0]
    data = rows[1:]
    out = []
    out.append("| " + " | ".join(header) + " |")
    out.append("| " + " | ".join(["---"]*len(header)) + " |")
    for r in data:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)

def _post_with_backoff(url, params, json_body, max_retries=5, base=1.6):
    """
    429/503 대비 지수 백오프(+지터). 성공 시 응답 반환.
    """
    import random
    for attempt in range(max_retries):
        r = requests.post(url, params=params, json=json_body, timeout=120)
        if r.status_code < 400:
            return r
        if r.status_code in (429, 503):
            retry_after = r.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                sleep_s = int(retry_after)
            else:
                sleep_s = (base ** attempt) + random.random() * 0.3
            time.sleep(sleep_s)
            continue
        r.raise_for_status()
    r.raise_for_status()

# ========== 프롬프트 빌더 ==========
DEFAULT_PROMPT_FALLBACK = """\
[역할]
너는 시스템 데일리 트레이더이자 한국 애널리스트야.
입력된 시스템 트레이딩 결과(CSV)와 사용자가 제공한 맥락(테마 메모, 힌트)을 바탕으로
"10분 리뷰형" 유튜브 스크립트를 **Markdown 문서**로 작성한다.

[중요 지침]
- **실시간 웹 검색 사용 금지**. 실제 기사 제목/URL/정확 수치 **임의 생성 금지**.
- 금일(${DATE}) 해당 종목/테마에 **관련 이슈가 있었을 가능성**을 '관측/가능성' 수준으로 서술하라.
  - 예: "기관 수급 둔화 가능성", "AI 반도체 투자 기대감 유지" 등
  - **확정적 표현 금지**: 모르면 "불확실/가능성" 명시.
- 입력 CSV/메모에 있는 수치/시간 외 수치·가격 **생성 금지**.
- '근거 → 결론' 순서, 문장 간결, 교육 톤.

[출력 포맷 (반드시 준수, Markdown)]
# 1. 인트로 (약 1분)
- 오늘 핵심 한 줄 요약
- 시청자에게 줄 가치 (학습 포인트)

# 2. 시스템 트레이딩 결과 리뷰 (약 2분)
- 아래 CSV 표를 요약 설명
- 성공/실패 1건씩 하이라이트 (수치/시간은 CSV 범위만)

# 3. 차트 분석 (약 3분)
- 종목별: 진입 근거(가격/거래량/패턴/지표) / 손절·익절 규칙 / 재진입 조건
- (이미지는 별도 제공 예정. 이미지를 가정한 '가능한 해석' 수준으로 설명)

# 4. 테마/뉴스 가능성 분석 (약 3분)
- 금일 ${TICKERS} 관련 **테마/이슈가 있었을 가능성**을 3~5개 포인트로 정리
- 불확실 시 '가능성/관측' 표기. 실제 기사 제목·URL·정확 수치 **기재 금지**
- 리스크/주의점 포함

# 5. 요약 & CTA (약 1분)
- 핵심 포인트 3줄
- 다음 화 예고 + 구독/알림 유도 문장

[입력 데이터]
- DATE: ${DATE}
- TIMEFRAME: ${TIMEFRAME}
- TICKERS: ${TICKERS}

[SYSTEM_RESULTS_TABLE (CSV)]
${SYSTEM_CSV}

[THEME_MEMO]
${THEME_MEMO}

[NEWS_HINT]
${NEWS_HINT}
"""

def load_prompt_template(path_str: str) -> str:
    p = Path(path_str)
    if p.exists():
        return p.read_text(encoding="utf-8")
    # 폴백 사용 + 경고 출력
    print(f"[report_daily_md] ⚠ 템플릿 파일을 찾을 수 없어 기본 템플릿을 사용합니다: {p}")
    return DEFAULT_PROMPT_FALLBACK

def build_prompt(date_str: str, timeframe: str, tickers: list[str],
                 system_csv: str, theme_memo: str, news_hint: str) -> str:
    """
    파일 템플릿을 ${VAR} 형식으로 로드하여 값 치환.
    - 사용 가능한 변수: DATE, TIMEFRAME, TICKERS, SYSTEM_CSV, THEME_MEMO, NEWS_HINT
    """
    tmpl_text = load_prompt_template(Path(__file__).parent / "prompt.md")
    tmpl = Template(tmpl_text)
    return tmpl.safe_substitute(
        DATE=date_str,
        TIMEFRAME=timeframe,
        TICKERS=",".join(tickers),
        SYSTEM_CSV=system_csv,
        THEME_MEMO=(theme_memo or "(비어 있음)"),
        NEWS_HINT=(news_hint or "(예: AI 반도체, 2차전지, 방산…)"),
    )

# ========== Gemini 호출 ==========
def call_gemini_md(prompt: str) -> str:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        # 뉴스 툴 비활성 (사용자 요청)
        # "tools": [{"google_search": {}}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 1400
        }
    }
    r = _post_with_backoff(
        GEMINI_ENDPOINT,
        params={"key": GEMINI_KEY},
        json_body=payload,
        max_retries=5
    )
    data = r.json()
    if not data.get("candidates"):
        return ""
    parts = data["candidates"][0]["content"].get("parts", [])
    return "\n".join([p.get("text","") for p in parts if p.get("text")]).strip()

# ========== 메인 ==========
def main():
    date_str = today_str()
    timeframe = "5m"  # 필요시 변경
    # 시스템 결과 로드 (JSON 또는 CSV)
    system_csv, rows = read_system_results(date_str)
    tickers = read_tickers_from_rows(rows) or ["TICKER"]
    theme_memo = ""   # 필요시 data/theme_memo_{date}.txt 등으로 분리 가능
    news_hint  = ""   # 필요시 data/news_hint_{date}.txt 등으로 분리 가능

    # 차트 이미지 URL이 있으면 아래처럼 MD 본문 하단에 미리보기로 붙일 수 있음 (선택)
    image_urls = read_image_urls(date_str)  # data/chart_images_{date}.txt (줄마다 URL)

    prompt = build_prompt(date_str, timeframe, tickers, system_csv, theme_memo, news_hint)
    md_body = call_gemini_md(prompt)

    # 시스템 결과 표를 MD 표로도 덧붙여주면 가독성↑
    md_table = csv_to_md_table(rows)

    # 최종 MD 합성 (모델 출력 + 표 + (선택) 이미지 미리보기)
    out_lines = []
    out_lines.append(md_body)
    out_lines.append("\n---\n")
    out_lines.append("## 📊 시스템 결과 (원본 표)")
    out_lines.append(md_table)
    if image_urls:
        out_lines.append("\n---\n")
        out_lines.append("## 🖼 차트 캡처 (미리보기)")
        for i, url in enumerate(image_urls, 1):
            out_lines.append(f"![chart_{i}]({url})")

    output_dir = Path(__file__).parent /"output"
    ensure_dir(output_dir)
    safe_tickers = ",".join(tickers)
    out_path = output_dir / f"[{date_str}] {safe_tickers} 데일리.md"
    out_path.write_text("\n".join(out_lines), encoding="utf-8")

    print("완료:", str(out_path.resolve()))

if __name__ == "__main__":
    main()
