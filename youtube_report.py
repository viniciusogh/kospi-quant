import os
import json
import re
import time
import requests
import urllib.request
from datetime import datetime, timezone, timedelta
from google import genai
from youtube_transcript_api import YouTubeTranscriptApi

# ==========================
# 환경설정
# ==========================
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CHANNEL_ID            = "UChlv4GSd7OQl3js-jkLOnFA"   # @3protv
GEMINI_API_KEY        = os.environ.get("GEMINI_API_KEY",        "AIzaSyBnoK6bxDxkRCkELWP612HXsNlcHGHHlRc")
NOTION_API_KEY        = os.environ.get("NOTION_API_KEY",        "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")
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_YT_PARENT_PAGE_ID", "3334a00632f880dc9c41fc9f09fab351")

PROCESSED_FILE       = os.path.join(_BASE_DIR, "processed_videos.json")
COOKIES_FILE         = os.path.join(_BASE_DIR, "youtube_cookies.txt")  # 쿠키 파일 경로
MAX_TRANSCRIPT_CHARS = 25000   # Gemini 요청 최대 자막 길이
MAX_VIDEOS_PER_RUN   = 10      # 1회 최대 처리 영상 수

KST = timezone(timedelta(hours=9))

# ==========================
# 유틸
# ==========================
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def load_processed() -> set:
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE) as f:
            return set(json.load(f))
    return set()

def save_processed(processed: set):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(sorted(processed), f, indent=2)

# ==========================
# YouTube RSS 영상 목록 수집
# ==========================
def get_channel_videos() -> list:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
    try:
        content = urllib.request.urlopen(url, timeout=10).read().decode()
    except Exception as e:
        log(f"❌ RSS 수집 실패: {e}")
        return []

    video_ids = re.findall(r"<yt:videoId>(.+?)</yt:videoId>", content)
    titles    = re.findall(r"<title>(.+?)</title>", content)
    published = re.findall(r"<published>(.+?)</published>", content)

    videos = []
    for i, vid in enumerate(video_ids):
        videos.append({
            "id":        vid,
            "title":     titles[i + 1] if i + 1 < len(titles) else "",
            "published": published[i + 1][:10] if i + 1 < len(published) else "",
            "url":       f"https://youtu.be/{vid}",
        })
    return videos

# ==========================
# 자막 추출
# ==========================
def get_transcript(video_id: str) -> str | None:
    # 쿠키 파일이 있으면 사용 (GitHub Actions IP 차단 우회)
    kwargs = {}
    if os.path.exists(COOKIES_FILE):
        kwargs["cookies"] = COOKIES_FILE
    try:
        api  = YouTubeTranscriptApi(**kwargs)
        t    = api.fetch(video_id, languages=["ko", "ko-KR"])
        text = " ".join(x.text for x in t)
        return text[:MAX_TRANSCRIPT_CHARS]
    except Exception as e:
        log(f"    자막 오류: {e}")
        return None

# ==========================
# Gemini 분석
# ==========================
def analyze_with_gemini(title: str, transcript: str, date: str) -> dict | None:
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""다음은 한국 주식 투자 채널 3proTV의 영상 자막입니다.
투자 정보를 분석해 아래 JSON 형식으로만 응답해주세요 (다른 텍스트 없이).

영상 제목: {title}
게시일: {date}

자막:
{transcript}

{{
  "summary": "핵심 내용 요약 3~4문장",
  "key_claims": ["주요 주장 1", "주요 주장 2", "주요 주장 3"],
  "stocks_sectors": ["언급 종목/섹터 (코드 포함 시 함께)"],
  "market_outlook": "시장 전망 또는 매크로 관점 1~2문장",
  "investment_ideas": ["실행 가능한 투자 아이디어 1", "아이디어 2"],
  "risks": ["리스크 요인 1", "리스크 요인 2"]
}}"""

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        raw = response.text.strip()
        m   = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        log(f"    Gemini 오류: {e}")
    return None

# ==========================
# Notion 블록 생성
# ==========================
def _para(content: str, block_type: str = "paragraph") -> list:
    """2000자 제한을 지키며 단락 블록 리스트 생성"""
    blocks = []
    for chunk in [content[i:i+1900] for i in range(0, len(content), 1900)]:
        blocks.append({
            "object": "block",
            "type":   block_type,
            block_type: {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
        })
    return blocks

def _bullet(items: list) -> list:
    return [{
        "object": "block",
        "type":   "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": str(item)}}]},
    } for item in items]

def build_video_blocks(video: dict, analysis: dict | None, transcript_len: int) -> list:
    blocks = []

    # 영상 제목 (H3, 링크 포함)
    blocks.append({
        "object": "block",
        "type":   "heading_3",
        "heading_3": {
            "rich_text": [{
                "type": "text",
                "text": {"content": video["title"], "link": {"url": video["url"]}},
            }]
        },
    })

    # 메타
    blocks.extend(_para(f"📅 {video['published']}  |  📝 자막 {transcript_len:,}자"))

    if analysis:
        blocks.extend(_para("📌 핵심 요약", "heading_3"))
        blocks.extend(_para(analysis.get("summary", "-")))

        if analysis.get("key_claims"):
            blocks.extend(_para("💡 주요 포인트", "heading_3"))
            blocks.extend(_bullet(analysis["key_claims"]))

        if analysis.get("stocks_sectors"):
            blocks.extend(_para("📈 언급 종목/섹터", "heading_3"))
            blocks.extend(_bullet(analysis["stocks_sectors"]))

        if analysis.get("market_outlook"):
            blocks.extend(_para("🌐 시장 전망", "heading_3"))
            blocks.extend(_para(analysis["market_outlook"]))

        if analysis.get("investment_ideas"):
            blocks.extend(_para("🎯 투자 아이디어", "heading_3"))
            blocks.extend(_bullet(analysis["investment_ideas"]))

        if analysis.get("risks"):
            blocks.extend(_para("⚠️ 리스크", "heading_3"))
            blocks.extend(_bullet(analysis["risks"]))
    else:
        blocks.extend(_para("⚠️ Gemini 분석 실패 (자막은 정상 수집됨)"))

    blocks.append({"object": "block", "type": "divider", "divider": {}})
    return blocks

# ==========================
# Notion 업로드
# ==========================
def upload_to_notion(date_str: str, blocks: list) -> bool:
    headers = {
        "Authorization":  f"Bearer {NOTION_API_KEY}",
        "Content-Type":   "application/json",
        "Notion-Version": "2022-06-28",
    }

    # 페이지 생성 (첫 100블록)
    body = {
        "parent":     {"page_id": NOTION_PARENT_PAGE_ID},
        "properties": {"title": {"title": [{"text": {"content": f"📺 {date_str} 3proTV 분석 리포트"}}]}},
        "children":   blocks[:100],
    }
    r = requests.post("https://api.notion.com/v1/pages", headers=headers, json=body, timeout=30)
    if r.status_code != 200:
        log(f"❌ Notion 페이지 생성 실패 ({r.status_code}): {r.text[:300]}")
        return False

    page_id = r.json()["id"]
    log(f"✅ Notion 페이지 생성 완료: {r.json().get('url', '')}")

    # 나머지 블록 추가 (100개씩)
    for i in range(100, len(blocks), 100):
        r2 = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=headers,
            json={"children": blocks[i:i+100]},
            timeout=30,
        )
        if r2.status_code != 200:
            log(f"⚠️ 블록 추가 실패 ({i}~{i+100}): {r2.status_code}")
        time.sleep(0.3)

    return True

# ==========================
# 메인
# ==========================
def main():
    today = datetime.now(KST).strftime("%Y-%m-%d")
    log(f"▶ 3proTV 분석 시작 (KST 기준: {today})")

    # RSS 영상 수집
    videos = get_channel_videos()
    log(f"✅ RSS 영상 {len(videos)}개 수집")

    # 미처리 영상 선별 (오늘 영상 우선, 최대 MAX_VIDEOS_PER_RUN)
    processed = load_processed()
    new_videos = [v for v in videos if v["id"] not in processed]

    today_new   = [v for v in new_videos if v["published"] == today]
    other_new   = [v for v in new_videos if v["published"] != today]
    target      = (today_new + other_new)[:MAX_VIDEOS_PER_RUN]

    if not target:
        log("✅ 처리할 새 영상 없음. 종료.")
        return

    log(f"▶ 처리 대상 {len(target)}개 (오늘: {len(today_new)}개, 이전: {len(other_new[:MAX_VIDEOS_PER_RUN - len(today_new)])}개)")

    all_blocks      = []
    processed_now   = []

    for i, video in enumerate(target, 1):
        log(f"  [{i}/{len(target)}] {video['title'][:50]}")

        # 자막 추출
        transcript = get_transcript(video["id"])
        if transcript is None:
            log("    ⚠️ 자막 없음 → 스킵 (다음 실행 재시도)")
            continue
        log(f"    자막 {len(transcript):,}자")

        # Gemini 분석 (요청 간격 유지)
        time.sleep(2)
        analysis = analyze_with_gemini(video["title"], transcript, video["published"])
        log(f"    Gemini {'✅' if analysis else '❌'}")

        all_blocks.extend(build_video_blocks(video, analysis, len(transcript)))
        processed_now.append(video["id"])

    if not all_blocks:
        log("⚠️ 업로드할 내용 없음. 종료.")
        return

    # 상단 헤딩 추가
    header = [
        {
            "object": "block",
            "type":   "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {
                    "content": f"총 {len(processed_now)}편 분석 | {today}"
                }}]
            },
        },
        {"object": "block", "type": "divider", "divider": {}},
    ]
    all_blocks = header + all_blocks

    # Notion 업로드
    log(f"▶ Notion 업로드 시작 (블록 {len(all_blocks)}개)")
    if upload_to_notion(today, all_blocks):
        processed.update(processed_now)
        save_processed(processed)
        log(f"🎉 완료! {len(processed_now)}개 영상 분석 리포트 업로드")

if __name__ == "__main__":
    main()
