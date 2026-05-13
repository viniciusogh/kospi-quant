"""
일일 종합 종목 추천 — 정량 데이터 (KIS 한투) + 유튜브 화자 의견 (Gemini 분석본) 의 교집합.

매일 22:30 KST 권장 (코스피 17:30 cron 완료 + 유튜브 22:00 cron 1차 완료 후).
출력: 노션 새 페이지 "💎 {date} 종합 추천".

비용 절감:
- 유튜브 원본 자막 (~25,000자/영상) 대신 youtube_report.py 가 만든 분석본 (~4,000자) 사용
- 정량 CSV 는 TOP 30 만 입력
- max_output_tokens = 1500 으로 응답 짧게
- gemini-2.5-flash (저렴한 모델)
"""
import os
import json
import socket
import time
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from google import genai

socket.setdefaulttimeout(60)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KST = timezone(timedelta(hours=9))

GEMINI_API_KEY        = os.environ.get("GEMINI_API_KEY",        "AIzaSyBRAHYt5C38MIHObIoJ8tIzeAlRXArO_J0")
NOTION_API_KEY        = os.environ.get("NOTION_API_KEY",        "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")
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_REC_PARENT_ID",  "3324a00632f880fbb014d766d87a1079")  # 코스피 추천종목 부모

ANALYSIS_CACHE = os.path.join(_BASE_DIR, "latest_youtube_analysis.json")
TOP_N          = 30


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_top_csv(path: str, n: int = TOP_N) -> str:
    if not os.path.exists(path):
        return ""
    try:
        df = pd.read_csv(path)
        return df.head(n).to_csv(index=False)
    except Exception as e:
        log(f"  ⚠️ {path} 읽기 실패: {e}")
        return ""


def load_youtube_analysis() -> str:
    """어제+오늘 분석본을 markdown 형식으로 합쳐 반환."""
    if not os.path.exists(ANALYSIS_CACHE):
        return ""
    try:
        with open(ANALYSIS_CACHE) as f:
            data = json.load(f)
    except Exception:
        return ""

    today     = datetime.now(KST).strftime("%Y-%m-%d")
    yesterday = (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")

    out = []
    for date in [yesterday, today]:
        if date not in data:
            continue
        for slug, videos in data[date].items():
            for v in videos:
                out.append(f"### [{date}] {v['channel_name']} — {v['title']}\n\n{v['analysis']}\n")
    return "\n---\n\n".join(out)


def analyze_and_recommend(quant_csv: str, youtube_text: str) -> str | None:
    client = genai.Client(api_key=GEMINI_API_KEY)
    today  = datetime.now(KST).strftime("%Y-%m-%d")

    prompt = f"""당신은 한국 주식 시장 종합 분석가입니다.

아래 두 종류 데이터를 바탕으로 **오늘({today}) 추천할 3~5 종목** 을 골라주세요.

## 데이터 1: 정량 모델 TOP 30 (한국투자증권 데이터 기반)
{quant_csv}

## 데이터 2: 유튜브 화자 의견 (최근 2일 영상 분석본)
{youtube_text}

## 추천 규칙 (엄격히 지킬 것)

1. **두 데이터 모두에서 시그널 있는 종목만** 추천. 한쪽만 강한 종목은 제외.
   - 정량: TOP 30 안에 들거나 명확한 모델 신호.
   - 유튜브: 화자가 구체적으로 추천/긍정 의견 낸 종목 (단순 언급 X).
2. **3~5 종목**. 이보다 많거나 적으면 안 됨.
3. 각 종목별 다음 형식:
   - 정량 시그널: 모델 점수, 랭킹, 핵심 재무 지표 1~2개
   - 유튜브 시그널: 화자명·채널명 + 직접 인용 (한 문장)
   - 결론: 한 줄 추천 이유
4. 출력 형식 (마크다운):

## TL;DR
(3~4줄. 오늘 추천 3~5종목 + 공통 테마.)

## 추천 종목

### 1. 종목명 (코드)
- **정량 시그널**: ...
- **유튜브 시그널**: ...
- **결론**: ...

### 2. 종목명 (코드)
...

## ⚠️ 주의
1~2줄. 이 추천의 한계 (예: 유튜브 편향, 모델 시차 등).
"""

    for attempt in range(2):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config={"max_output_tokens": 1500},
            )
            return response.text.strip()
        except Exception as e:
            err = str(e)
            if "429" in err:
                log(f"  ⚠️ Gemini 429 → 60초 대기 ({attempt+1}/2)")
                time.sleep(60)
            else:
                log(f"  ❌ Gemini 오류: {e}")
                return None
    return None


def _nh():
    return {
        "Authorization":  f"Bearer {NOTION_API_KEY}",
        "Content-Type":   "application/json",
        "Notion-Version": "2022-06-28",
    }


def push_to_notion(text: str) -> str | None:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    title = f"💎 {today} 종합 추천"

    body = {
        "parent":     {"page_id": NOTION_PARENT_PAGE_ID},
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
    }
    r = requests.post("https://api.notion.com/v1/pages", headers=_nh(), json=body, timeout=30)
    if r.status_code != 200:
        log(f"❌ 페이지 생성 실패 ({r.status_code}): {r.text[:200]}")
        return None
    page_id  = r.json()["id"]
    page_url = r.json().get("url", "")

    # markdown → 단순 paragraph 변환 (heading/bullet 만 분기)
    blocks = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                           "heading_3": {"rich_text": [{"type": "text", "text": {"content": line[4:]}}]}})
        elif line.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                           "heading_2": {"rich_text": [{"type": "text", "text": {"content": line[3:]}}]}})
        elif line.startswith("- "):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": line[2:]}}]}})
        else:
            for chunk in [line[i:i+1900] for i in range(0, len(line), 1900)]:
                blocks.append({"object": "block", "type": "paragraph",
                               "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}})

    # 외부 블록 1개씩 (youtube_report 패턴 — 100블록/요청 한도 회피, 여기선 어차피 적지만 안전)
    for i in range(0, len(blocks), 1):
        r = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=_nh(),
            json={"children": blocks[i:i+1]},
            timeout=60,
        )
        if r.status_code != 200:
            log(f"⚠️ 블록 추가 실패 ({i}): {r.status_code} {r.text[:120]}")
            return page_url
        time.sleep(0.2)

    return page_url


def main():
    log("▶ 일일 종합 추천 시작")

    # 1. 정량 데이터 (TOP 30 만)
    parts = []
    for fname, label in [
        ("latest_results.csv",         "KOSPI 수급 모델"),
        ("latest_kosdaq_results.csv",  "KOSDAQ 수급 모델"),
        ("latest_quality_results.csv", "KOSPI Quality 모델"),
    ]:
        csv = load_top_csv(os.path.join(_BASE_DIR, fname), TOP_N)
        if csv:
            parts.append(f"### {label} TOP {TOP_N}\n{csv}")
    quant_csv = "\n\n".join(parts)
    log(f"✅ 정량 데이터 수집: {len(quant_csv):,}자")

    if not quant_csv:
        log("❌ 정량 CSV 없음. 종료.")
        return

    # 2. 유튜브 분석본 (어제+오늘)
    youtube_text = load_youtube_analysis()
    log(f"✅ 유튜브 분석본: {len(youtube_text):,}자")

    if not youtube_text:
        log("⚠️ 유튜브 분석본 비었음 — 어제·오늘 처리된 영상이 없거나 캐시 미생성. 종료.")
        return

    # 3. Gemini 호출
    log("▶ Gemini 호출 중...")
    rec = analyze_and_recommend(quant_csv, youtube_text)
    if not rec:
        log("❌ Gemini 응답 실패. 종료.")
        return
    log(f"✅ Gemini 응답: {len(rec):,}자")

    # 4. 노션 push
    url = push_to_notion(rec)
    if url:
        log(f"🎉 완료: {url}")
    else:
        log("⚠️ 노션 push 실패 — Gemini 응답은 정상 (로그 위에 출력됨)")


if __name__ == "__main__":
    main()
