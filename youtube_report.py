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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

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
NOTION_DATE_ROOT = os.environ.get("NOTION_PARENT_PAGE_ID", "3324a00632f880fbb014d766d87a1079")  # Claude 페이지. 그 안에 날짜 페이지 자식

PROCESSED_FILE       = os.path.join(_BASE_DIR, "processed_videos.json")
FAILED_FILE          = os.path.join(_BASE_DIR, "failed_videos.json")
QUOTA_FILE           = os.path.join(_BASE_DIR, "yt_quota.json")
MAX_ATTEMPTS_PER_DAY = int(os.environ.get("YT_MAX_PER_DAY", "40"))  # 프록시 월 1GB 보호
MAX_FAIL_BEFORE_SKIP = int(os.environ.get("YT_MAX_FAIL", "3"))   # 이 횟수 실패하면 영구 스킵
NOTION_DAILY_PAGES   = os.path.join(_BASE_DIR, "notion_daily_pages.json")
ANALYSIS_CACHE       = os.path.join(_BASE_DIR, "latest_youtube_analysis.json")  # daily_recommend.py 가 사용
COOKIES_FILE         = os.path.join(_BASE_DIR, "youtube_cookies.txt")
LOCK_FILE            = os.path.join(_BASE_DIR, "youtube_report.lock")  # 중복 실행 방지
MAX_TRANSCRIPT_CHARS = 25000
MAX_VIDEOS_PER_RUN   = 15    # 유료 전환 후 제한 해제
LOCK_MAX_AGE_HOURS   = 0.5   # lock 파일 최대 유효 시간 (30분 — 정상 실행은 1~2분 내 완료)
ANALYSIS_CACHE_DAYS  = 7     # 분석본 캐시 보존 기간 (daily_recommend 가 최근 7일 사용)

# cron 환경의 PATH 가 minimal 이라 Homebrew 경로 안 잡힘 → 절대 경로 폴백
YT_DLP_BIN = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"

KST = timezone(timedelta(hours=9))

# ==========================
# 유틸
# ==========================
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def _quota(today: str) -> int:
    """오늘 자막 시도 횟수. 프록시 월 대역폭(1GB≈8,000요청)을 하루에 태우지 못하게 하는 상한."""
    if os.path.exists(QUOTA_FILE):
        try:
            with open(QUOTA_FILE) as f:
                d = json.load(f)
            if d.get("date") == today:
                return int(d.get("attempts", 0))
        except Exception:
            pass
    return 0


def _quota_add(today: str, n: int):
    # open(...,"w") 가 파일을 먼저 비우므로 누적값을 반드시 열기 전에 읽어야 한다
    # (안 그러면 _quota() 가 빈 파일을 읽어 0 을 돌려주고 카운트가 리셋된다)
    total = _quota(today) + n
    with open(QUOTA_FILE, "w") as f:
        json.dump({"date": today, "attempts": total}, f)


def load_failed() -> dict:
    """영상별 자막 실패 횟수. 자막 없는 영상을 매시간 무한 재시도하며 프록시 대역폭을 태우는 것 방지."""
    if os.path.exists(FAILED_FILE):
        try:
            with open(FAILED_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_failed(d: dict):
    with open(FAILED_FILE, "w") as f:
        json.dump(d, f, indent=2)


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
             "--playlist-items", "1-15", "--no-warnings",
             # YouTube 자동번역 제목(영어) 대신 한국어 원본 강제 (2026-07-09)
             "--extractor-args", "youtube:lang=ko", url],
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

    # 30초 강제 timeout — main thread 에서만 작동. worker thread (ThreadPoolExecutor) 는
    # socket.setdefaulttimeout(60) 에만 의존 (위험은 30초→60초 단일 영상 hang 정도).
    is_main = threading.current_thread() is threading.main_thread()
    if is_main:
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
                # 기본값 10 은 대역폭을 태운다. 실패 영상 8개 × 10회 × 시간당 = 하루 640회.
                # 2026-08-14~17 에 월 1GB 를 3일 만에 소진한 주범 (2026-08-18 조정).
                retries_when_blocked=int(os.environ.get("WEBSHARE_RETRIES", "2")),
            )
        try:
            api = YouTubeTranscriptApi(http_client=session, proxy_config=proxy_config)
            t   = api.fetch(video_id, languages=["ko", "ko-KR"])
        except Exception as e:
            # 프록시 자체가 죽은 경우(구독 만료 402·인증 407 등) 프록시 없이 한 번 더.
            # 2026-08-12~17 Webshare 402 로 자막이 5일간 0개였는데 그냥 넘어가고 있었다.
            if proxy_config is None or not any(k in str(e) for k in ("402", "407", "Tunnel", "proxy", "Proxy")):
                raise
            log(f"    ⚠️ 프록시 실패({str(e)[:60]}) → 프록시 없이 재시도")
            api = YouTubeTranscriptApi(http_client=session, proxy_config=None)
            t   = api.fetch(video_id, languages=["ko", "ko-KR"])
        text = " ".join(x.text for x in t)
        return text[:MAX_TRANSCRIPT_CHARS]
    except _TranscriptTimeout as e:
        log(f"    ⏱️ 자막 timeout: {e}")
        return None
    except Exception as e:
        log(f"    자막 오류: {e}")
        return None
    finally:
        if is_main:
            signal.alarm(0)

# ==========================
# Gemini 분석
# ==========================
def analyze_with_gemini(title: str, transcript: str, date: str, channel_name: str) -> str | None:
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""아래는 한국 주식 투자 유튜브 채널 {channel_name} 영상의 자막입니다.
이 영상을 **읽는 사람이 흐름을 한눈에 파악할 수 있게** 역피라미드(결론 먼저) 구조로 정리해주세요.

핵심 원칙:
- **중요도 순**으로 작성. 화자가 길게 강조한 것은 위로, 30초 스쳐간 언급은 종목 표 한 줄로 줄이거나 생략.
- **모든 걸 나열하지 말 것.** 평면 bullet dump 금지. 부수적 디테일보다 "오늘 화자가 말하려는 핵심 메시지와 그 근거"를 우선.
- 숫자/종목은 **스토리 안에서 근거로** 녹여 쓸 것. 데이터를 따로 모아 나열하지 말 것.
- **쉬운 말로, 확 와닿게 쓸 것 (가장 중요).** 주식·코인 초보가 술술 읽고 "아 그렇구나" 해야 성공. 다음을 지켜라:
  · **일상 비유로 풀 것.** 어려운 구조는 익숙한 그림으로 바꿔라. 예: "핀테크(로빈후드·코인베이스)는 고객 많은 대형 플랫폼 = 돈 끌어모으는 재주, 1세대 디파이(유니스왑·아베)는 검증된 전통 금융 = 안전하게 굴리는 재주. 둘이 손잡는 것."
  · **전문용어·영어약어(디파이·레이어2·cBTC·MiCA 등)는 처음 나올 때 괄호로 한 번 풀 것.** 예: "MiCA(EU 가상자산 규제법)". 못 풀면 그 개념을 아예 빼라.
  · **"과거엔 이랬는데 지금은 이렇다" 대비**로 변화를 또렷하게. 예: "예전엔 신기술 자랑만 해도 10배 뛰었지만, 이제는 실제로 돈 버는 프로젝트만 오른다."
  · **한 문장에 개념 하나, 짧게.** 만연체·미사여구·수식어 남발 금지. 한 문단 5줄 넘으면 쪼개라.
  · **숫자는 지어내지 말 것 (가장 흔한 사고).** 복잡한 통계(예: 0.8 엑사바이트, 웨이퍼 66만 장)는 과감히 생략하고 "훨씬 많이·폭등·급감" 같은 방향어로 옮겨라. 자막에 그대로 나온 숫자만 인용하고, "3배" 같은 비율을 직접 계산해 만들지 말 것 — 자막에 "3배"라고 안 했으면 "3배"라 쓰지 마라. 방향만 맞으면 충분하다.

⚠️ 제목·게시일은 별도 표시되므로 **첫 줄에 제목 반복 금지**. 바로 아래 구조로 시작.

# 정확히 이 3개 섹션만 사용:

## 한 줄 요약
3줄 이내. 오늘 화자의 핵심 결론 + 투자자가 취할 액션. 군더더기 X.

## 오늘의 스토리
가장 중요한 흐름 2~3개. 각각 `**N. 짧은 제목**` 으로 시작하고, **왜 그게 중요한지 인과를 설명하는 서사 문단**으로 작성. 관련 종목·수치는 이 문단 안에서 근거로 인용. (예: "브로드컴이 신고가에서 호실적을 내고도 시간외 -15% → 눈높이 충족만으로 차익실현 트리거")

## 종목
화자가 **방향성을 갖고 언급한 종목만** 표로. 잡다한 단순 언급은 제외.
| 종목 | 방향 | 한 줄 이유 |
| :--- | :---: | :--- |
| (종목명) | 🟢/🟡/🔴 | (수치 포함 핵심 근거 한 줄) |

**문체**: 스토리 섹션은 친근하고 쉬운 말로, 비유를 곁들여 확 와닿게 풀어 쓸 것(미사여구·과장은 X, 쉬움 ≠ 유치). 표/요약은 단편·명사구 OK. 단 비유·해석은 자막에 있는 사실을 쉽게 옮기는 용도일 뿐, 자막에 없는 내용을 지어내지 말 것. 추측·일반론 금지.

# 스타일 예시 (이 '변환 감각'을 그대로 따라라 — 주제 무관, 어려운 글을 쉽게 옮기는 방식만 참고):
[나쁜 예 — 전문가용, 딱딱하고 어려움]
"메모리 팹 증설에도 HBM의 웨이퍼 소모량이 난HBM 대비 높아 구조적 쇼티지 발생. 삼성이 HBM을 0.8→2.8 엑사바이트로 늘리려면 웨이퍼 투입이 8만→28만 장으로 급증."
[좋은 예 — 초보용, 쉽고 확 와닿음]
"HBM(고대역폭 메모리)은 D램을 아파트처럼 위로 높게 쌓아 만드는 반도체예요. 그래서 같은 양을 만들어도 원판(웨이퍼)이 훨씬 더 많이 듭니다. 공장을 아무리 늘려도(삼성 P5 등) 그 재료를 죄다 HBM에 쏟다 보니, 정작 일반 D램 만들 재료가 부족해지는(쇼티지) 거죠."
→ 핵심: 어려운 수치(엑사바이트·만 장)는 버렸고, 비유(아파트)로 개념을 옮겼고, 방향(훨씬 많이·부족)만 남겼다. 이 감각으로 써라.

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
    """Gemini 마크다운 출력을 Notion 블록으로 변환. 표 / 헤딩 / 불릿 / 볼드 / 다이비더 처리."""
    blocks = []
    src_lines = text.split('\n')
    idx = 0
    while idx < len(src_lines):
        raw = src_lines[idx]
        line = raw.strip()
        if not line:
            idx += 1
            continue
        # 마크다운 표: | a | b | + 다음 줄 |---|---|
        if line.startswith('|') and line.endswith('|') and idx + 1 < len(src_lines):
            next_line = src_lines[idx + 1].strip()
            if next_line.startswith('|') and '---' in next_line:
                header_cells = [c.strip() for c in line.strip('|').split('|')]
                col_count = len(header_cells)
                data_rows = []
                j = idx + 2
                while j < len(src_lines):
                    row_line = src_lines[j].strip()
                    if not row_line.startswith('|'):
                        break
                    cells = [c.strip() for c in row_line.strip('|').split('|')]
                    while len(cells) < col_count:
                        cells.append("")
                    cells = cells[:col_count]
                    data_rows.append(cells)
                    j += 1
                children = [{
                    "object": "block", "type": "table_row",
                    "table_row": {"cells": [_parse_bold(c) for c in header_cells]}
                }]
                for row in data_rows:
                    children.append({
                        "object": "block", "type": "table_row",
                        "table_row": {"cells": [_parse_bold(c) for c in row]}
                    })
                blocks.append({
                    "object": "block", "type": "table",
                    "table": {
                        "table_width": col_count,
                        "has_column_header": True,
                        "has_row_header": False,
                        "children": children,
                    },
                })
                idx = j
                continue
        if line.startswith('### '):
            blocks.append({"object": "block", "type": "heading_3",
                           "heading_3": {"rich_text": _parse_bold(line[4:].strip())}})
        elif line.startswith('## '):
            blocks.append({"object": "block", "type": "heading_2",
                           "heading_2": {"rich_text": _parse_bold(line[3:].strip())}})
        elif line.startswith('# '):
            blocks.append({"object": "block", "type": "heading_1",
                           "heading_1": {"rich_text": _parse_bold(line[2:].strip())}})
        elif line in ('---', '***', '___'):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif re.match(r'^[*\-] ', line) or re.match(r'^\*{1,3}\s+', line):
            content_inner = re.sub(r'^[*\-]+\s*', '', line).strip()
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _parse_bold(content_inner)}})
        else:
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _parse_bold(line)}})
        idx += 1
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


def _get_or_create_date_page(today: str) -> str | None:
    """노션 database 의 today row 찾거나 생성. NOTION_DAILY_DB_ID 사용."""
    db_id = os.environ.get("NOTION_DAILY_DB_ID", "")
    if not db_id:
        log("❌ NOTION_DAILY_DB_ID 미설정. database 패턴 사용 불가.")
        return None

    r = requests.post(
        f"https://api.notion.com/v1/databases/{db_id}/query",
        headers=_nh(),
        json={"filter": {"property": "날짜", "date": {"equals": today}}, "page_size": 1},
        timeout=15,
    )
    if r.status_code == 200:
        results = r.json().get("results", [])
        if results:
            return results[0]["id"]

    body = {
        "parent": {"database_id": db_id},
        "properties": {
            "이름": {"title": [{"type": "text", "text": {"content": "준비 중"}}]},
            "날짜": {"date": {"start": today}},
        },
    }
    r = requests.post("https://api.notion.com/v1/pages", headers=_nh(), json=body, timeout=30)
    if r.status_code != 200:
        log(f"❌ database row 생성 실패 ({r.status_code}): {r.text[:200]}")
        return None
    log(f"✅ database row 생성: {today}")
    return r.json()["id"]



def get_or_create_daily_page(today: str) -> str | None:
    """
    오늘 날짜의 통합 유튜브 분석 페이지 ID 반환 (채널 무관, 1개/일).
    날짜 페이지 ({today}) 안에 자식으로 만듦. 다른 추천 페이지들과 같은 위치.
    제목: 📺 {today} 유튜브 분석
    """
    pages = _load_pages()
    entry = _get_entry(pages, today)

    if entry is not None:
        log(f"✅ 오늘 통합 페이지 재사용: {entry['page_id']}")
        if not isinstance(pages.get(today), dict):
            pages[today] = entry
            _save_daily_pages(pages)
        return entry["page_id"]

    # 날짜 페이지 (다른 추천들과 같은 위치) 안에 만들기
    date_page_id = _get_or_create_date_page(today)
    if not date_page_id:
        log("❌ 날짜 페이지 가져오기 실패. 유튜브 분석 페이지 생성 중단.")
        return None

    body = {
        "parent":     {"page_id": date_page_id},
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

    # 자막이 계속 안 잡히는 영상(자막 자체가 없거나 지역제한)을 매시간 재시도하며 프록시
    # 대역폭을 태우던 문제 → MAX_FAIL_BEFORE_SKIP 회 실패하면 processed 에 넣어 영구 제외.
    failed = load_failed()
    give_up = [vid for vid, n in failed.items() if n >= MAX_FAIL_BEFORE_SKIP]
    if give_up:
        log(f"⏭️ 반복 실패 {len(give_up)}개 영구 스킵 ({MAX_FAIL_BEFORE_SKIP}회 이상 자막 실패)")
        processed.update(give_up)
        save_processed(processed)
        for vid in give_up:
            failed.pop(vid, None)
        save_failed(failed)

    new_videos = [v for v in videos if v["id"] not in processed]
    today_new   = [v for v in new_videos if v["published"] == today]
    other_new   = [v for v in new_videos if v["published"] != today]
    # RSS는 최신순 정렬 → reverse하여 오래된 것부터 처리 (Notion에 최신이 아래 쌓이도록)
    # YT_TODAY_ONLY=1 이면 오늘 게시분만 (밀린 backlog 를 한꺼번에 돌려 토큰 태우는 것 방지).
    # YT_MAX_VIDEOS 로 1회 처리량 상한을 따로 줄 수 있다.
    pool = today_new if os.environ.get("YT_TODAY_ONLY") == "1" else (today_new + other_new)
    cap = int(os.environ.get("YT_MAX_VIDEOS") or MAX_VIDEOS_PER_RUN)
    target      = list(reversed(pool[:cap]))

    used = _quota(today)
    left = MAX_ATTEMPTS_PER_DAY - used
    if left <= 0:
        log(f"⏸️ 오늘 자막 시도 한도 도달 ({used}/{MAX_ATTEMPTS_PER_DAY}) — 프록시 대역폭 보호. 종료.")
        return
    if len(target) > left:
        log(f"⏸️ 일일 한도로 {len(target)}개 → {left}개만 처리 ({used}/{MAX_ATTEMPTS_PER_DAY} 사용)")
        target = target[:left]
    _quota_add(today, len(target))

    if not target:
        log("✅ 처리할 새 영상 없음. 종료.")
        return

    log(f"▶ 처리 대상 {len(target)}개 (오늘: {len(today_new)}개, 이전: {len(other_new[:MAX_VIDEOS_PER_RUN - len(today_new)])}개)")

    # 영상 5개 병렬 처리 (자막 fetch + Gemini 분석). Webshare paid plan 500 동시 한도 안에서 안전.
    def _process_video(v):
        t = get_transcript(v["id"])
        if t is None:
            return {"video": v, "transcript_len": 0, "analysis": None, "ok": False}
        a = analyze_with_gemini(v["title"], t, v["published"], channel["name"])
        return {"video": v, "transcript_len": len(t), "analysis": a, "ok": True}

    results = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_process_video, v): v for v in target}
        for fut in as_completed(futures):
            v = futures[fut]
            try:
                r = fut.result()
                if not r["ok"]:
                    failed[v["id"]] = failed.get(v["id"], 0) + 1
                    log(f"    ⚠️ 자막 없음({failed[v['id']]}/{MAX_FAIL_BEFORE_SKIP}회): {v['title'][:44]}")
                else:
                    failed.pop(v["id"], None)      # 성공하면 카운터 리셋
                    results.append(r)
                    log(f"    ✅ {v['title'][:50]} (자막 {r['transcript_len']:,}자, Gemini {'✅' if r['analysis'] else '❌'})")
            except Exception as e:
                log(f"    ❌ 처리 실패 {v['title'][:50]}: {e}")

    # target 순서로 재정렬 (Notion 에 시간순으로 쌓이도록)
    # 무음 실패 방지: 처리 대상이 있었는데 하나도 못 받았으면 인프라 문제(프록시 만료·쿠키·차단)다.
    # 2026-08-12~17 Webshare 402 로 자막이 5일간 0개였는데 워크플로가 success 로 떠서 아무도 몰랐다.
    save_failed(failed)          # 종료 경로와 무관하게 카운터는 남겨야 누적된다
    if target and not results:
        log(f"❌ 처리 대상 {len(target)}개 전부 자막 실패 — 프록시/쿠키/차단 확인 필요 (종료코드 1)")
        raise SystemExit(1)

    order = {v["id"]: i for i, v in enumerate(target)}
    results.sort(key=lambda r: order[r["video"]["id"]])

    all_blocks = []
    processed_now = []
    for r in results:
        # Gemini 분석 실패(429 등) 영상은 마킹·업로드 안 함 → 다음 run 에서 재시도.
        # (예전엔 무조건 마킹해서 결제 소진 시간대 영상이 영구 skip 됐음)
        if not r["analysis"]:
            log(f"    ⏭️ Gemini 분석 실패 → 마킹 안 함, 다음 run 재시도: {r['video']['title'][:40]}")
            continue
        all_blocks.extend(build_video_blocks(r["video"], r["analysis"], r["transcript_len"]))
        processed_now.append(r["video"]["id"])
        _save_analysis_cache(today, channel, r["video"], r["analysis"])

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
