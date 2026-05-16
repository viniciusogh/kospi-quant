import os
import _env  # .env 자동 로드
import json
import re
import shutil
import signal
import socket
import time
import subprocess
import requests
import urllib.request
from http.cookiejar import MozillaCookieJar
from datetime import datetime, timezone, timedelta
from google import genai
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig

# 모든 socket 작업에 60초 default timeout — 외부 API hang 으로 인한 좀비 누적 방지
socket.setdefaulttimeout(60)


class _TranscriptTimeout(Exception):
    pass


def _transcript_alarm_handler(signum, frame):
    raise _TranscriptTimeout("자막 호출 30초 초과")

# ==========================
# 환경설정
# ==========================
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CHANNELS = [
    {"slug": "3proTV",       "name": "3proTV",                       "channel_id": "UChlv4GSd7OQl3js-jkLOnFA", "emoji": "📊"},
    {"slug": "futuresnow",   "name": "오선의 미국 증시 라이브",          "channel_id": "UC_JJ_NhRqPKcIOj5Ko3W_3w", "emoji": "🇺🇸"},
    {"slug": "moneyinside",  "name": "머니인사이드",                   "channel_id": "UCxfko2YOD6DODYRGzeOPhIQ", "emoji": "💰"},
    {"slug": "donkkang",     "name": "강민우 돈깡TV",                 "channel_id": "UCI6C5V4J8FWRcLcOdh1yElw", "emoji": "🥊"},
    {"slug": "moneydo",      "name": "전인구경제연구소",               "channel_id": "UCznImSIaxZR7fdLCICLdgaQ", "emoji": "📈"},
    {"slug": "chaegookjang", "name": "채국장의 코스피 1만 코스닥 3천", "channel_id": "UCl7_Zg4RdQbHx0kXaiKL-5g", "emoji": "🎯"},
]
GEMINI_API_KEY        = os.environ["GEMINI_API_KEY"]
NOTION_API_KEY        = os.environ["NOTION_API_KEY"]
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_YT_PARENT_PAGE_ID", "3484a00632f880988b41e8b13d7fbb0b")

PROCESSED_FILE       = os.path.join(_BASE_DIR, "processed_videos.json")
NOTION_DAILY_PAGES   = os.path.join(_BASE_DIR, "notion_daily_pages.json")
ANALYSIS_CACHE       = os.path.join(_BASE_DIR, "latest_youtube_analysis.json")  # daily_recommend.py 가 사용
COOKIES_FILE         = os.path.join(_BASE_DIR, "youtube_cookies.txt")
LOCK_FILE            = os.path.join(_BASE_DIR, "youtube_report.lock")  # 중복 실행 방지
MAX_TRANSCRIPT_CHARS = 25000
MAX_VIDEOS_PER_RUN   = 15    # 유료 전환 후 제한 해제
LOCK_MAX_AGE_HOURS   = 0.5   # lock 파일 최대 유효 시간 (30분 — 정상 실행은 1~2분 내 완료)
ANALYSIS_CACHE_DAYS  = 3     # 분석본 캐시 보존 기간 (daily_recommend 가 어제+오늘만 쓰니 3일이면 충분)

# cron 환경의 PATH 가 minimal 이라 Homebrew 경로 안 잡힘 → 절대 경로 폴백
YT_DLP_BIN = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"

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


def _save_analysis_cache(today: str, channel: dict, video: dict, analysis: str):
    """일일 분석 결과 누적 캐시. 3일 이상 오래된 데이터 자동 삭제 (입력 토큰 유지).
    daily_recommend.py 가 이 파일을 읽음 — 자막 원본 대신 요약본 재사용해 Gemini 비용 절감.
    """
    data = {}
    if os.path.exists(ANALYSIS_CACHE):
        try:
            with open(ANALYSIS_CACHE) as f:
                data = json.load(f)
        except Exception:
            data = {}

    cutoff = (datetime.now(KST) - timedelta(days=ANALYSIS_CACHE_DAYS)).strftime("%Y-%m-%d")
    data = {d: v for d, v in data.items() if d >= cutoff}

    data.setdefault(today, {}).setdefault(channel["slug"], []).append({
        "video_id":     video["id"],
        "title":        video["title"],
        "url":          video["url"],
        "channel_name": channel["name"],
        "analysis":     analysis,
    })

    with open(ANALYSIS_CACHE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ==========================
# YouTube RSS 영상 목록 수집
# ==========================
def get_channel_videos(channel_id: str) -> list:
    """yt-dlp 로 채널 최근 영상 15개의 ID/title 수집.
    YouTube RSS endpoint 가 막힌 이후 (2026-05 경) 대체 경로. flat-playlist 모드는
    timestamp 를 안 주므로 published 는 오늘 날짜로 하드코딩 (cutoff 로직과 호환).
    """
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    try:
        result = subprocess.run(
            [YT_DLP_BIN, "--flat-playlist", "--dump-json",
             "--playlist-items", "1-15", "--no-warnings", url],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        log(f"❌ yt-dlp 호출 실패: {e}")
        return []
    if result.returncode != 0:
        log(f"❌ yt-dlp 실패: {result.stderr[:200]}")
        return []

    today = datetime.now(KST).strftime("%Y-%m-%d")
    videos = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        vid = d.get("id")
        if not vid:
            continue
        videos.append({
            "id":        vid,
            "title":     d.get("title", ""),
            "published": today,
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

    # 30초 강제 timeout — youtube-transcript-api 가 timeout 옵션 없어서 hang 시 좀비화됨
    signal.signal(signal.SIGALRM, _transcript_alarm_handler)
    signal.alarm(30)
    try:
        # Webshare proxy 설정 (GitHub Actions 등 cloud IP 차단 우회).
        # 환경변수 없으면 proxy 없이 직접 호출 (로컬 실행 시).
        proxy_config = None
        if os.environ.get("WEBSHARE_USER") and os.environ.get("WEBSHARE_PASS"):
            proxy_config = WebshareProxyConfig(
                proxy_username=os.environ["WEBSHARE_USER"],
                proxy_password=os.environ["WEBSHARE_PASS"],
            )
        api  = YouTubeTranscriptApi(http_client=session, proxy_config=proxy_config)
        t    = api.fetch(video_id, languages=["ko", "ko-KR"])
        text = " ".join(x.text for x in t)
        return text[:MAX_TRANSCRIPT_CHARS]
    except _TranscriptTimeout as e:
        log(f"    ⏱️ 자막 timeout: {e}")
        return None
    except Exception as e:
        log(f"    자막 오류: {e}")
        return None
    finally:
        signal.alarm(0)

# ==========================
# Gemini 분석
# ==========================
def analyze_with_gemini(title: str, transcript: str, date: str, channel_name: str) -> str | None:
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""아래는 한국 주식 투자 유튜브 채널 {channel_name} 영상의 자막입니다.
이 영상의 내용을 **최대한 누락 없이** 상세하게 정리해주세요.

⚠️ 주의: 제목과 게시일은 별도로 표시되므로 **첫 줄에 제목을 반복하지 마세요**. 바로 내용 분석부터 시작해주세요.

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
    """영상 1편 = 토글 1개 (클릭하면 펼쳐지는 구조)"""

    # 토글 내부에 들어갈 children 블록
    children = []
    children.extend(_para(f"📅 {video['published']}  |  📝 자막 {transcript_len:,}자"))

    if analysis:
        children.extend(markdown_to_notion(analysis))
    else:
        children.extend(_para("⚠️ Gemini 분석 실패 (자막은 정상 수집됨)"))

    # Notion 토글 블록 (children 최대 95개 제한)
    toggle = {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [{
                "type": "text",
                "text": {"content": video["title"], "link": {"url": video["url"]}},
                "annotations": {"bold": True},
            }],
            "children": children[:95],
        },
    }
    return [toggle]

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


def _load_pages() -> dict:
    if os.path.exists(NOTION_DAILY_PAGES):
        with open(NOTION_DAILY_PAGES) as f:
            return json.load(f)
    return {}


def _get_entry(pages: dict, today: str) -> dict | None:
    """notion_daily_pages.json 엔트리 가져오기. 레거시 string(page_id) → dict 자동 승격."""
    entry = pages.get(today)
    if entry is None:
        return None
    if isinstance(entry, str):
        # 옛 형식 (단일 채널 시절: 값이 page_id 문자열). dict 로 승격.
        return {"page_id": entry, "channels": {}}
    return entry


def get_or_create_daily_page(today: str) -> str | None:
    """
    오늘 날짜의 통합 Notion 페이지 ID 반환 (채널 무관, 1개/일).
    없으면 새 페이지 생성 후 notion_daily_pages.json 에 저장.
    제목: 📺 {today} 유튜브 분석
    """
    pages = _load_pages()
    entry = _get_entry(pages, today)

    if entry is not None:
        log(f"✅ 오늘 통합 페이지 재사용: {entry['page_id']}")
        # 옛 형식이었으면 dict 로 정상화 후 저장
        if not isinstance(pages.get(today), dict):
            pages[today] = entry
            _save_daily_pages(pages)
        return entry["page_id"]

    body = {
        "parent":     {"page_id": NOTION_PARENT_PAGE_ID},
        "properties": {"title": {"title": [{"text": {"content": f"📺 {today} 유튜브 분석"}}]}},
    }
    r = requests.post("https://api.notion.com/v1/pages", headers=_nh(), json=body, timeout=30)
    if r.status_code != 200:
        log(f"❌ 페이지 생성 실패 ({r.status_code}): {r.text[:200]}")
        return None

    page_id = r.json()["id"]
    log(f"✅ 새 통합 페이지 생성: {r.json().get('url', '')}")

    pages[today] = {"page_id": page_id, "channels": {}}
    _save_daily_pages(pages)
    return page_id


def get_or_create_channel_toggle(today: str, channel: dict) -> str | None:
    """
    오늘 통합 페이지 안의 채널 토글 block_id 반환.
    없으면 빈 토글 새로 만들고 ID 캐시한 뒤 반환.
    """
    pages = _load_pages()
    entry = _get_entry(pages, today)
    if entry is None:
        log(f"❌ 통합 페이지 entry 없음 — get_or_create_daily_page 먼저 호출되어야 함")
        return None

    slug = channel["slug"]
    if slug in entry.get("channels", {}):
        return entry["channels"][slug]

    # 빈 채널 토글을 페이지에 추가
    page_id = entry["page_id"]
    toggle_block = {
        "object": "block",
        "type":   "toggle",
        "toggle": {
            "rich_text": [{
                "type": "text",
                "text": {"content": f"{channel['emoji']} {channel['name']}"},
                "annotations": {"bold": True},
            }],
            "children": [],
        },
    }
    r = requests.patch(
        f"https://api.notion.com/v1/blocks/{page_id}/children",
        headers=_nh(),
        json={"children": [toggle_block]},
        timeout=30,
    )
    if r.status_code != 200:
        log(f"❌ 채널 토글 생성 실패 ({r.status_code}): {r.text[:200]}")
        return None

    toggle_id = r.json()["results"][0]["id"]
    entry.setdefault("channels", {})[slug] = toggle_id
    pages[today] = entry
    _save_daily_pages(pages)
    log(f"  ➕ 새 채널 토글 생성: {channel['name']}")
    return toggle_id


def append_to_block(parent_block_id: str, blocks: list, today: str) -> bool:
    """
    Notion 블록(페이지 또는 토글)에 자식 블록 이어붙이기.
    아카이브 감지 시 notion_daily_pages.json 의 today 엔트리 제거 후 False 반환.
    """
    # 외부 블록 1개씩 전송 — 토글 1개 + nested children 95개 = 96 < 100 (Notion API 한도)
    for i in range(0, len(blocks), 1):
        r = requests.patch(
            f"https://api.notion.com/v1/blocks/{parent_block_id}/children",
            headers=_nh(),
            json={"children": blocks[i:i+1]},
            timeout=60,
        )
        if r.status_code == 400 and "archived" in r.text:
            log(f"⚠️ 부모 블록이 아카이브 상태 → 저장된 today 엔트리 제거 후 재실행 필요")
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
        try:
            os.remove(LOCK_FILE)
        except FileNotFoundError:
            pass  # 이미 삭제됨 → 무시


def _main():
    today = datetime.now(KST).strftime("%Y-%m-%d")
    log(f"▶ 다채널 분석 시작 (KST 기준: {today}, {len(CHANNELS)}개 채널)")

    # 통합 페이지 한 번만 만들거나 가져옴 (채널 무관)
    page_id = get_or_create_daily_page(today)
    if not page_id:
        log("❌ 통합 페이지 가져오기 실패. 종료.")
        return

    for channel in CHANNELS:
        log(f"")
        log(f"=== [{channel['name']}] ===")
        try:
            _process_channel(channel, today, page_id)
        except Exception as e:
            log(f"❌ {channel['name']} 처리 실패: {e}")
            continue


def _process_channel(channel: dict, today: str, page_id: str):
    # RSS 영상 수집
    videos = get_channel_videos(channel["channel_id"])
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
        analysis = analyze_with_gemini(video["title"], transcript, video["published"], channel["name"])
        log(f"    Gemini {'✅' if analysis else '❌'}")

        all_blocks.extend(build_video_blocks(video, analysis, len(transcript)))
        processed_now.append(video["id"])

        # 분석 성공한 영상만 캐시 (daily_recommend.py 가 이걸로 종목 추천)
        if analysis:
            _save_analysis_cache(today, channel, video, analysis)

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

    # 채널 토글 가져오거나 생성 후 그 안에 이어붙이기
    log(f"▶ Notion 업로드 시작 (블록 {len(all_blocks)}개)")
    toggle_id = get_or_create_channel_toggle(today, channel)
    if toggle_id and append_to_block(toggle_id, all_blocks, today):
        processed.update(processed_now)
        save_processed(processed)
        log(f"🎉 완료! {len(processed_now)}개 영상 분석 이어붙이기 완료")

if __name__ == "__main__":
    main()
