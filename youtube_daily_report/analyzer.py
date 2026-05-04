"""
LLM을 사용해 자막을 분석하고 경제 보고서 섹션을 생성하는 모듈
지원 provider:
  - ollama : 로컈 뮸료 (Ollama 설치 필요, 인터넷 불필요)
  - gemini : Google Gemini API 무료 티어 (API 키 필요)
  - openai : OpenAI GPT (유료, 기본 슬컴 최고 품질)
"""
import os
import json
from transcript_fetcher import format_transcript_for_llm


# ─────────────────────────────────────────────
# LLM 클라이언트 팩토리
# ─────────────────────────────────────────────

def _call_ollama(prompt_system: str, prompt_user: str, model: str, json_mode: bool = False) -> str:
    """Ollama 로컬 LLM 호출 (http://localhost:11434)"""
    import urllib.request

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt_system},
            {"role": "user", "content": prompt_user},
        ],
        "stream": False,
        "options": {"temperature": 0.3},
    }
    if json_mode:
        payload["format"] = "json"

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read())
    return result["message"]["content"]


def _call_gemini(prompt_system: str, prompt_user: str, model: str) -> str:
    """Google Gemini API 호출"""
    import urllib.request

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "system_instruction": {"parts": [{"text": prompt_system}]},
        "contents": [{"parts": [{"text": prompt_user}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4000},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    return result["candidates"][0]["content"]["parts"][0]["text"]


def _call_openai(prompt_system: str, prompt_user: str, model: str, json_mode: bool = False) -> tuple[str, int]:
    """OpenAI API 호출"""
    from openai import OpenAI
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY 환경변수가 설정되지 않았습니다.")
    client = OpenAI(api_key=api_key)
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": prompt_system},
            {"role": "user", "content": prompt_user},
        ],
        temperature=0.3,
        max_tokens=4000,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content, resp.usage.total_tokens


def _llm_call(prompt_system: str, prompt_user: str, provider: str, model: str, json_mode: bool = False) -> str:
    """provider에 따라 적절한 LLM 호출"""
    if provider == "ollama":
        return _call_ollama(prompt_system, prompt_user, model, json_mode)
    elif provider == "gemini":
        return _call_gemini(prompt_system, prompt_user, model)
    elif provider == "openai":
        text, tokens = _call_openai(prompt_system, prompt_user, model, json_mode)
        print(f"    (OpenAI 토큰: {tokens})")
        return text
    else:
        raise ValueError(f"알 수 없는 provider: {provider}")


# ─────────────────────────────────────────────
# 개별 영상 분석
# ─────────────────────────────────────────────

SINGLE_VIDEO_SYSTEM_PROMPT = """당신은 경제·금융 전문 애널리스트입니다.
한국 경제 유튜브 채널 영상의 자막을 분석하여 핵심 내용을 구조화된 JSON으로 추출합니다.
반드시 다음 JSON 스키마를 따르세요:
{
  "summary": "영상 핵심 내용 3~5문장 요약",
  "key_topics": ["주요 토픽1", "주요 토픽2", ...],
  "market_mentions": {
    "indices": [{"name": "지수명", "direction": "상승/하락/횡보/언급", "detail": "내용"}],
    "sectors": [{"name": "섹터명", "sentiment": "긍정/부정/중립", "detail": "내용"}],
    "assets": [{"name": "종목/자산명", "direction": "상승/하락/횡보/언급", "detail": "내용"}]
  },
  "economic_indicators": [{"name": "지표명", "value": "수치(있으면)", "interpretation": "해석"}],
  "risk_factors": ["리스크 요인1", "리스크 요인2"],
  "opportunities": ["투자 기회/긍정 요인1", "투자 기회/긍정 요인2"],
  "sentiment": "강세/약세/중립/혼조",
  "key_quote": "영상에서 가장 중요한 발언 1개 (직접 인용 또는 요약)"
}"""

def analyze_single_video(video_data: dict, model: str = "gpt-4o") -> dict:
    """단일 영상 자막 분석"""
    client = get_openai_client()

    transcript_text = format_transcript_for_llm(video_data)  # 전체 자막 (제한 없음)
    title = video_data.get("title", "")
    description = video_data.get("description", "")[:500]

    user_prompt = f"""제목: {title}
설명: {description}

자막 내용:
{transcript_text}

위 영상을 분석하여 JSON 형식으로 반환하세요."""

    print(f"  [LLM] '{title[:40]}...' 분석 중 (모델: {model})")

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SINGLE_VIDEO_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=2000,
        )
        content = response.choices[0].message.content
        analysis = json.loads(content)
        analysis["_model"] = model
        analysis["_tokens"] = response.usage.total_tokens
        print(f"    → 분석 완료 (토큰: {response.usage.total_tokens})")
        return analysis

    except Exception as e:
        print(f"    → 분석 실패: {e}")
        return {"error": str(e), "summary": "분석 실패"}


# ─────────────────────────────────────────────
# 전체 종합 보고서 생성
# ─────────────────────────────────────────────

SYNTHESIS_SYSTEM_PROMPT = """당신은 한국 금융시장 전문 수석 애널리스트입니다.
오늘 업로드된 경제 유튜브 채널(@3protv)의 영상들을 종합 분석하여
투자자를 위한 일일 경제 보고서를 작성합니다.

보고서 작성 원칙:
- 객관적이고 균형 잡힌 시각 유지
- 구체적인 수치와 데이터 우선 인용
- 상충하는 의견이 있으면 양측 모두 제시
- 투자 권유가 아닌 정보 제공 관점으로 작성
- 한국어로 작성, 전문 용어는 괄호 안에 영문 병기"""

def synthesize_daily_report(
    date_str: str,
    analyzed_videos: list[dict],
    model: str = "gpt-4o",
) -> str:
    """
    분석된 영상들을 종합하여 일일 경제 보고서(마크다운) 생성
    analyzed_videos: analyze_single_video 결과 + 영상 메타데이터 포함 리스트
    """
    client = get_openai_client()

    # 각 영상 분석 요약을 LLM에 전달
    videos_summary = []
    for i, v in enumerate(analyzed_videos, 1):
        analysis = v.get("analysis", {})
        videos_summary.append(f"""
## 영상 {i}: {v.get('title', '제목 없음')}
- URL: {v.get('url', '')}
- 업로드: {v.get('published_at', '')}
- 요약: {analysis.get('summary', '없음')}
- 주요 토픽: {', '.join(analysis.get('key_topics', []))}
- 시장 심리: {analysis.get('sentiment', '불명')}
- 리스크: {'; '.join(analysis.get('risk_factors', []))}
- 기회 요인: {'; '.join(analysis.get('opportunities', []))}
- 핵심 발언: "{analysis.get('key_quote', '')}"
""")

    combined = "\n".join(videos_summary)
    total_videos = len(analyzed_videos)

    user_prompt = f"""날짜: {date_str}
분석한 영상 수: {total_videos}개

각 영상 분석 결과:
{combined}

위 내용을 바탕으로 {date_str} 일일 경제 보고서를 마크다운 형식으로 작성하세요.

보고서 구조 (반드시 이 순서와 섹션 이름을 사용):
1. ## 📊 오늘의 핵심 요약 (3~5줄 bullet point)
2. ## 🌏 글로벌 시장 동향 (주요 지수, 환율, 원자재 등 언급된 내용)
3. ## 🇰🇷 국내 시장 동향 (코스피/코스닥, 주목 섹터/종목)
4. ## 💡 주요 경제 이슈 & 분석 (오늘 핵심 경제 이슈들)
5. ## ⚠️ 리스크 요인 (주의해야 할 리스크)
6. ## 🔍 투자 관심 포인트 (기회 요인 및 관심 섹터/종목)
7. ## 📺 분석 영상 목록 (제목 + URL 링크)

각 섹션은 구체적이고 실질적인 내용으로 채우세요. 언급되지 않은 내용은 임의로 추가하지 마세요."""

    print(f"\n[LLM] 일일 종합 보고서 생성 중 ({total_videos}개 영상 종합, 모델: {model})...")

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=4000,
        )
        report_md = response.choices[0].message.content
        print(f"  → 보고서 생성 완료 (토큰: {response.usage.total_tokens})")
        return report_md

    except Exception as e:
        print(f"  → 보고서 생성 실패: {e}")
        raise


def analyze_all_videos(videos_with_transcripts: list[dict], model: str = "gpt-4o") -> list[dict]:
    """
    자막이 포함된 영상 목록을 받아 각각 LLM 분석 수행
    """
    print(f"\n[LLM 분석] 총 {len(videos_with_transcripts)}개 영상 분석 시작...")
    results = []

    for video in videos_with_transcripts:
        if not video.get("transcript"):
            print(f"  [스킵] '{video.get('title', '')[:40]}' - 자막 없음")
            video["analysis"] = {"error": "자막 없음", "summary": "자막을 가져올 수 없어 분석 불가"}
        else:
            analysis = analyze_single_video(video, model=model)
            video["analysis"] = analysis

        results.append(video)

    return results
