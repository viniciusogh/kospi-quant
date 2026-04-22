import os
import json
import re
import time
import requests
import urllib.request
from http.cookiejar import MozillaCookieJar
from datetime import datetime, timezone, timedelta
from google import genai
from youtube_transcript_api import YouTubeTranscriptApi

# ==========================
# 환경설정
# ==========================
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CHANNEL_ID            = "UChlv4GSd7OQl3js-jkLOnFA"   # @3protv
GEMINI_API_KEY        = os.environ.get("GEMINI_API_KEY",        "AIzaSyBRAHYt5C38MIHObIoJ8tIzeAlRXArO_J0")
NOTION_API_KEY        = os.environ.get("NOTION_API_KEY",        "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")
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_YT_PARENT_PAGE_ID", "3484a00632f880988b41e8b13d7fbb0b")

PROCESSED_FILE       = os.path.join(_BASE_DIR, "processed_videos.json")
NOTION_DAILY_PAGES   = os.path.join(_BASE_DIR, "notion_daily_pages.json")
COOKIES_FILE         = os.path.join(_BASE_DIR, "youtube_cookies.txt")
LOCK_FILE            = os.path.join(_BASE_DIR, "youtube_report.lock")  # 중복 실행 방지
MAX_TRANSCRIPT_CHARS = 25000
MAX_VIDEOS_PER_RUN   = 3     # 1회당 3편 제한 (Gemini 무료 쿼터 절약)
LOCK_MAX_AGE_HOURS   = 2     # lock 파일 최대 유효 시간

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
    # 쿠키 파일이 있으면 requests 세션에 로드 (GitHub Actions IP 차단 우회)
    session = requests.Session()
    if os.path.exists(COOKIES_FILE):
        jar = MozillaCookieJar(COOKIES_FILE)
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies = jar
            log("    쿠키 로드 완료")
        except Exception as e:
            log(f"    쿠키 로드 실패: {e}")
    try:
        api  = YouTubeTranscriptApi(http_client=session)
        t    = api.fetch(video_id, languages=["ko", "ko-KR"])
        text = " ".join(x.text for x in t)
        return text[:MAX_TRANSCRIPT_CHARS]
    except Exception as e:
        log(f"    자막 오류: {e}")
        return None

# ==========================
# Gemini 분석
# ==========================
def analyze_with_gemini(title: str, transcript: str, date: str) -> str | None:
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""아래는 한국 주식 투자 유튜브 채널 3proTV 영상의 자막입니다.
이 영상의 내용을 **최대한 누락 없이** 상세하게 정리해주세요.

다음 항목을 보드락없이 포함해야 합니다:
- 핵심 주제 및 결론
- 언급된 종목명·섹터와 해당 분석 내용 (구체적 수치 포함)
- 시장 전망 및 매크로 관점
- 구체적인 투자 전략·매매 아이디어
- 언급된 주요 데이터 (지수, 가격, 비율, 날짜 등)
- 리스크·주의사항

자막의 흐름을 따라가며 중요한 내용이 빠지지 않게 작성하되, 반복 내용은 한 번만 정리해주세요.
독자가 영상을 보지 않아도 전체 내용을 완전히 파악할 수 있도록 상세하게 작성해주세요.

영상 제목: {title}
게시일: {date}

자막:
{transcript}"""

    for attempt in range(2):   # 최대 2회만 재시도 (무한 대기 방지)
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            return response.text.strip()
        except Exception as e:
            err = str(e)
            if "429" in err:
                m = re.search(r"retry in (\d+(?:\.\d+)?)s", err)
                wait = min(float(m.group(1)) + 3 if m else 60, 65)  # 최대 65초
                log(f"    Gemini 할당량 초과 → {wait:.0f}초 대기 후 재시도 ({attempt+1}/2)")
                time.sleep(wait)
            else:
                log(f"    Gemini 오류: {e}")
                break
    return None

# ==========================
# Notion 블록 생성
# ==========================
def _parse_bold(text: str) -> list:
    """**bold** 텍스트를 Notion rich_text 볼드로 변환"""
    parts = re.split(r'(\*\*[^*]+\*\*)', text)
    result = []
    for p in parts:
        if p.startswith('**') and p.endswith('**'):
            result.append({"type": "text", "text": {"content": p[2:-2]},
                           "annotations": {"bold": True}})
        elif p:
            result.append({"type": "text", "text": {"content": p}})
    return result or [{"type": "text", "text": {"content": text}}]


def _para(content: str, block_type: str = "paragraph") -> list:
    blocks = []
    for chunk in [content[i:i+1900] for i in range(0, len(content), 1900)]:
        blocks.append({
            "object": "block", "type": block_type,
            block_type: {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
        })
    return blocks


def markdown_to_notion(text: str) -> list:
    """
    Gemini 마크다운 출력을 Notion 블록으로 변환
    ### 헤딩 / ** 볼드 / * 불릿 / --- 다이비더
    """
    blocks = []
    for raw in text.split('\n'):
        line = raw.strip()
        if not line:
            continue
        if line.startswith('### '):
            blocks.append({
                "object": "block", "type": "heading_3",
                "heading_3": {"rich_text": _parse_bold(line[4:].strip())}
            })
        elif line.startswith('## '):
            blocks.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": _parse_bold(line[3:].strip())}
            })
        elif line in ('---', '***', '___'):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif re.match(r'^[*\-] ', line) or re.match(r'^\*{1,3}\s+', line):
            content = re.sub(r'^[*\-]+\s*', '', line).strip()
            blocks.append({
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _parse_bold(content)}
            })
        else:
            # 일반 단락 (1900자 청크)
            rt = _parse_bold(line)
            # rt는 짧으니 chunk 분할 불필요
            blocks.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": rt}
            })
    return blocks


def build_video_blocks(video: dict, analysis: str | None, transcript_len: int) -> list:
    blocks = []

    # 영상 제목 (H3, 링크 포함)
    blocks.append({
        "object": "block", "type": "heading_3",
        "heading_3": {
            "rich_text": [{
                "type": "text",
                "text": {"content": video["title"], "link": {"url": video["url"]}},
            }]
        },
    })

    # 메타
    blocks.extend(_para(f"📅 {video['published']}  |  📝 자막 {transcript_len:,}자"))

    # Gemini 분석 본문: 마크다운 → Notion 블록 변환
    if analysis:
        blocks.extend(markdown_to_notion(analysis))
    else:
        blocks.extend(_para("⚠️ Gemini 분석 실패 (자막은 정상 수집됨)"))

    blocks.append({"object": "block", "type": "divider", "divider": {}})
    return blocks

# ==========================
# Notion 판리제: 일일 페이지 이어붙이기
# ==========================
def _nh():
    return {
        "Authorization":  f"Bearer {NOTION_API_KEY}",
        "Content-Type":   "application/json",
        "Notion-Version": "2022-06-28",
    }

def _save_daily_pages(pages: dict):
    with open(NOTION_DAILY_PAGES, "w") as f:
        json.dump(pages, f, indent=2)


def get_or_create_daily_page(today: str) -> str | None:
    """
    오늘 날짜의 Notion 페이지 ID 반환.
    없으면 새 페이지 생성 후 notion_daily_pages.json에 저장.
    """
    pages = {}
    if os.path.exists(NOTION_DAILY_PAGES):
        with open(NOTION_DAILY_PAGES) as f:
            pages = json.load(f)

    if today in pages:
        log(f"✅ 오늘 페이지 재사용: {pages[today]}")
        return pages[today]

    # 새 페이지 생성 (빈 페이지, 이후 이어붙이기)
    body = {
        "parent":     {"page_id": NOTION_PARENT_PAGE_ID},
        "properties": {"title": {"title": [{"text": {"content": f"📺 {today} 3proTV 분석"}}]}},
    }
    r = requests.post("https://api.notion.com/v1/pages", headers=_nh(), json=body, timeout=15)
    if r.status_code != 200:
        log(f"❌ 페이지 생성 실패 ({r.status_code}): {r.text[:200]}")
        return None

    page_id = r.json()["id"]
    log(f"✅ 새 페이지 생성: {r.json().get('url', '')}")

    pages[today] = page_id
    _save_daily_pages(pages)
    return page_id


def append_to_page(page_id: str, blocks: list, today: str) -> bool:
    """
    Notion 페이지에 블록 이어붙이기 (100개씨).
    아카이브/삭제된 페이지라면 notion_daily_pages.json에서 제거 후 False 반환.
    """
    for i in range(0, len(blocks), 100):
        r = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=_nh(),
            json={"children": blocks[i:i+100]},
            timeout=30,
        )
        if r.status_code == 400 and "archived" in r.text:
            log(f"⚠️ 페이지가 아카이브 상태 → 저장된 ID 제거 후 재실행 필요")
            if os.path.exists(NOTION_DAILY_PAGES):
                with open(NOTION_DAILY_PAGES) as f:
                    pages = json.load(f)
                pages.pop(today, None)
                _save_daily_pages(pages)
            return False
        if r.status_code != 200:
            log(f"⚠️ 블록 추가 실패 ({i}~): {r.status_code} {r.text[:100]}")
            return False
        time.sleep(0.3)
    return True

# ==========================
# 메인
# ==========================
def main():
    # 중복 실행 방지 (lock 파일)
    if os.path.exists(LOCK_FILE):
        lock_age_h = (time.time() - os.path.getmtime(LOCK_FILE)) / 3600
        if lock_age_h < LOCK_MAX_AGE_HOURS:
            log(f"⚠️ 이전 실행이 진행 중 (lock 나이 {lock_age_h:.1f}시간). 스킵.")
            return
        log(f"⚠️ stale lock 발견 ({lock_age_h:.1f}시간) → 자동 제거 후 실행")
        os.remove(LOCK_FILE)
    open(LOCK_FILE, "w").close()

    try:
        _main()
    finally:
        os.remove(LOCK_FILE)   # 정상/비정상 종료 시 모두 lock 해제


def _main():
    today = datetime.now(KST).strftime("%Y-%m-%d")
    log(f"▶ 3proTV 분석 시작 (KST 기준: {today})")

    # RSS 영상 수집
    videos = get_channel_videos()
    log(f"✅ RSS 영상 {len(videos)}개 수집")

    # 미처리 영상 선별
    processed = load_processed()

    # 3일 이상 오래된 미처리 영상은 Gemini 호출 없이 바로 완료 체크 (쿠터 절약)
    cutoff_date = (datetime.now(KST) - timedelta(days=3)).strftime("%Y-%m-%d")
    old_skipped = []
    for v in videos:
        if v["id"] not in processed and v["published"] < cutoff_date:
            old_skipped.append(v["id"])
    if old_skipped:
        processed.update(old_skipped)
        save_processed(processed)
        log(f"⏩ {len(old_skipped)}개 오래된 영상 자동 스킵 (쿠터 절약)")

    new_videos = [v for v in videos if v["id"] not in processed]
    today_new   = [v for v in new_videos if v["published"] == today]
    other_new   = [v for v in new_videos if v["published"] != today]
    # RSS는 최신순 정렬 → reverse하여 오래된 것부터 처리 (Notion에 최신이 아래 쌓이도록)
    target      = list(reversed((today_new + other_new)[:MAX_VIDEOS_PER_RUN]))
    target      = list(reversed((today_new + other_new)[:MAX_VIDEOS_PER_RUN]))

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

        # Gemini 분석 (요청 간격 유지 - 분당 한도 여유)
        time.sleep(5)
        analysis = analyze_with_gemini(video["title"], transcript, video["published"])
        log(f"    Gemini {'✅' if analysis else '❌'}")

        all_blocks.extend(build_video_blocks(video, analysis, len(transcript)))
        processed_now.append(video["id"])

    if not all_blocks:
        log("⚠️ 업로드할 내용 없음. 종료.")
        return

    # 업데이트 시간 헤딩 추가 (어느 시간 모드에서 추가된 영상인지 표시)
    now_kst = datetime.now(KST).strftime("%H:%M")
    update_header = [
        {
            "object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {
                "content": f"🕔 {now_kst} 업데이트 — {len(processed_now)}편 추가"
            }}]},
        },
    ]
    all_blocks = update_header + all_blocks

    # 일일 페이지 가져오거나 생성 후 이어붙이기
    log(f"▶ Notion 업로드 시작 (블록 {len(all_blocks)}개)")
    page_id = get_or_create_daily_page(today)
    if page_id and append_to_page(page_id, all_blocks, today):
        processed.update(processed_now)
        save_processed(processed)
        log(f"🎉 완료! {len(processed_now)}개 영상 분석 이어붙이기 완료")

if __name__ == "__main__":
    main()
