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
# 한도는 이제 **폭주 방지용**이다. 정상 운영을 막으면 안 된다(2026-08-28 재조정).
# 원래 근거 둘 다 소멸: ① 무료 티어 Gemini 429 → 유튜브는 OpenAI 로 이동 + Gemini 도 Tier 1
# ② 프록시 월 1GB → 1GB 를 태운 건 정상 볼륨이 아니라 재시도 루프 버그였고 8/18 에 고쳤다.
# 실측 하루 발행량(RSS): 8/24 6편 · 8/25 11 · 8/26 9 · 8/27 22(3proTV 만 14).
# 정상일엔 절대 안 닿고, 버그로 폭주할 때만 걸리는 값으로 둔다.
MAX_ATTEMPTS_PER_DAY = int(os.environ.get("YT_MAX_PER_DAY", "80"))
MAX_FAIL_BEFORE_SKIP = int(os.environ.get("YT_MAX_FAIL", "3"))   # 이 횟수 실패하면 영구 스킵
NOTION_DAILY_PAGES   = os.path.join(_BASE_DIR, "notion_daily_pages.json")
ANALYSIS_CACHE       = os.path.join(_BASE_DIR, "latest_youtube_analysis.json")  # daily_recommend.py 가 사용
COOKIES_FILE         = os.path.join(_BASE_DIR, "youtube_cookies.txt")
LOCK_FILE            = os.path.join(_BASE_DIR, "youtube_report.lock")  # 중복 실행 방지
MAX_TRANSCRIPT_CHARS = 25000
# 영상 분석 모델. 실제 자막 1건으로 3종 비교(2026-08-28) 후 gpt-5.4-mini 채택 —
# flash 와 속도 동급(6.1초 vs 5.8초)인데 화자 식별이 정확했다. gpt-5.5 는 26초로 4배 느리고
# 출력 토큰도 2.4배. 이 작업은 추론보다 '정해진 형식 준수 + 압축' 이라 큰 모델의 이점이 적다.
YT_MODEL = os.environ.get("YT_MODEL", "gpt-5.4-mini")
# 채널당 1회 상한. 6 은 3proTV 하루 14편의 절반도 못 받아 매일 밀렸다(사용자 지적).
# 실제 게시일 기반 YT_DAYS_BACK 필터가 살아났으므로 볼륨은 그쪽이 잡는다 → 20 으로 완화.
MAX_VIDEOS_PER_RUN   = int(os.environ.get("YT_MAX_VIDEOS", "20"))
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
def _videos_from_rss(channel_id: str) -> list:
    """RSS 로 실제 게시일까지 받는다. 2026-05 경 막혔다가 2026-08-28 재확인 시 200 으로 열려 있다.
    실제 날짜가 있어야 YT_DAYS_BACK(어제+오늘) 필터가 제 역할을 한다 — 하드코딩된 '오늘' 로는
    그 필터와 '3일 이상 묵은 영상 자동 스킵' 이 둘 다 무력했다."""
    import html as _html
    try:
        r = requests.get("https://www.youtube.com/feeds/videos.xml",
                         params={"channel_id": channel_id}, timeout=25)
        if r.status_code != 200:
            return []
    except Exception:
        return []
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", r.text, re.S):
        vid = re.search(r"<yt:videoId>([^<]+)</yt:videoId>", e)
        tit = re.search(r"<media:title>(.*?)</media:title>", e, re.S)
        pub = re.search(r"<published>(\d{4}-\d\d-\d\d)", e)
        if not (vid and tit and pub):
            continue
        out.append({"id": vid.group(1), "title": _html.unescape(tit.group(1)).strip(),
                    "published": pub.group(1), "url": f"https://youtu.be/{vid.group(1)}"})
    return out


def get_channel_videos(channel_id: str) -> list:
    """채널 최근 영상 목록. RSS 우선(실제 게시일), 막히면 yt-dlp 로 폴백.
    yt-dlp 의 --flat-playlist 는 timestamp 를 주지 않아 published 를 오늘로 채운다."""
    rss = _videos_from_rss(channel_id)
    if rss:
        return rss
    log("    ℹ️ RSS 실패 → yt-dlp 폴백 (게시일은 오늘로 채움)")
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

def _clean_title(t, limit=40):
    """유튜브 원제목 → (코너, 짧은 헤드라인).

    원제목은 '헤드라인 | 게스트 소속·직함 [코너명]' 꼴이라 그대로 쓰면 두 줄로 넘치고
    목록이 어지러워진다(사용자 지적). 게스트 부분은 버리고 코너를 앞으로 뺀다.
    """
    t = (t or "").strip()
    seg = ""
    m = re.search(r"\[([^\[\]]{1,14})\]\s*$", t)          # 끝의 [코너명]
    if m:
        seg = m.group(1).strip()
        t = t[:m.start()].strip()
    m = re.match(r"^[\[【]([^\]】]{1,16})[\]】]\s*(.+)$", t)  # 앞의 【…】/[…] 는 항상 떼고
    if m:                                                    # 코너가 비어 있을 때만 태그로 승격
        lead, t = m.group(1).strip(), m.group(2).strip()
        seg = seg or lead
    # 구분자로 세로바(|) 말고 한글 자모 'ㅣ'(U+3163) 나 전각(｜) 을 쓰는 제목이 섞여 있다
    t = re.split(r"[|｜ㅣ]", t)[0].strip()                    # 게스트 이름·직함 꼬리 제거
    t = re.sub(r"\s*-\s*20\d\d/\d\d/\d\d\s*$", "", t)    # 끝의 날짜
    t = re.sub(r"\s*\([^()]{0,30}\d부\)\s*$", "", t)         # 끝의 (게스트 N부)
    t = re.sub(r"\s{2,}", " ", t).strip(" -·|")
    if len(t) > limit:
        t = t[:limit].rstrip() + "…"
    return seg, t


# ==========================
# Gemini 분석
# ==========================
def analyze_with_gemini(title: str, transcript: str, date: str, channel_name: str) -> str | None:
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
  · **미확인 정보는 미확인이라고 쓸 것.** 자막이 "~보도", "~한다는 소식", "확인되지 않았다" 로
    말한 것을 사실처럼 단정하지 마라. 근거 불릿에 `공식 확인 없음` 처럼 한 조각 덧붙여라.
    (비교 테스트에서 큰 모델만 이걸 지켰다 — 휴전 '보도' 를 확정 사실로 쓰면 판단이 왜곡된다.)
  · **숫자는 지어내지 말 것 (가장 흔한 사고).** 복잡한 통계(예: 0.8 엑사바이트, 웨이퍼 66만 장)는 과감히 생략하고 "훨씬 많이·폭등·급감" 같은 방향어로 옮겨라. 자막에 그대로 나온 숫자만 인용하고, "3배" 같은 비율을 직접 계산해 만들지 말 것 — 자막에 "3배"라고 안 했으면 "3배"라 쓰지 마라. 방향만 맞으면 충분하다.

⚠️ 제목·게시일은 별도 표시되므로 **첫 줄에 제목 반복 금지**. 바로 아래 구조로 시작.

# 정확히 이 3개 섹션만 사용:

## 한 줄 요약
**2문장 이내.** 오늘 화자의 핵심 결론 + 투자자가 취할 액션. 군더더기 X.
그 아래 줄에 태그 3~4개를 `·` 로 구분해 붙여라 — 🟢(기회) / 🔴(위험) / 🟡(주의) 중 하나 + 2~6자 라벨.
예: `🟢 반도체 쏠림 완화 · 🟢 실적주 기회 · 🔴 헤지펀드 청산 · 🟡 변동성 확대`

## 오늘의 스토리
가장 중요한 흐름 2~3개. **문단 나열 금지.** 각각 아래 형식을 정확히 지켜라.

### N. 짧은 제목 (14자 이내)
**결론 한 줄** — 이 흐름의 요점 (45자 이내)
- 근거 한 줄 (35자 이내, 자막에 나온 수치·종목·사건을 넣어라)
- 근거 한 줄
- 근거 한 줄 (2~3개. 없으면 2개만)

⚠️ 근거는 **완결 문장이 아니라 사실 한 조각**이다. "~입니다" 로 끝나는 설명문을 쓰지 말고
핵심만 남겨라. 좋은 예: `영국계 헤지펀드 레버리지 청산 → 급락 촉발`
나쁜 예: `최근 삼성전자와 SK하이닉스의 주가 급락은 단순히 주주환원 기대 미달 때문만은 아닙니다.`

## 종목
화자가 **방향성을 갖고 언급한 종목만** 표로. 잡다한 단순 언급은 제외.
| 종목 | 방향 | 한 줄 이유 |
| :--- | :---: | :--- |
| (종목명) | 🟢/🟡/🔴 | (수치 포함 핵심 근거 한 줄) |

**문체**: 결론 줄은 쉬운 말로 확 와닿게, 근거 불릿은 짧은 명사구·단편으로. 만연체 금지. 어려운 개념은 결론 줄에서 비유로 한 번만 풀어라(미사여구·과장 X, 쉬움 ≠ 유치). 단 비유·해석은 자막에 있는 사실을 쉽게 옮기는 용도일 뿐, 자막에 없는 내용을 지어내지 말 것. 추측·일반론 금지.

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

    return _llm(prompt, YT_MODEL)


def _llm(prompt: str, model: str) -> str | None:
    """모델 이름으로 제공자를 고른다. gpt-*/o* → OpenAI, 그 외 → Gemini.
    OpenAI 가 죽으면(크레딧 소진 등) Gemini 로 떨어져 리포트 자체는 살린다 —
    2026-08-27 에 실제로 OpenAI 크레딧 0 으로 429 를 맞았다."""
    if model.startswith(("gpt-", "o3", "o4")):
        out = _openai(prompt, model)
        if out:
            _used(model)
            return out
        log(f"    ⚠️ {model} 실패 → gemini-2.5-flash 로 폴백")
        fb = _gemini(prompt, "gemini-2.5-flash")
        if fb:
            _used("gemini-2.5-flash (폴백)")
        return fb
    out = _gemini(prompt, model)
    if out:
        _used(model)
    return out


_USED = set()


def _used(model):
    """실행당 한 번만 찍는다 — 어느 제공자로 돌았는지 로그로 확인 가능해야 한다."""
    if model not in _USED:
        _USED.add(model)
        log(f"    🤖 분석 모델: {model}")


def _openai(prompt: str, model: str) -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log("    ⚠️ OPENAI_API_KEY 없음")
        return None
    try:
        from openai import OpenAI
    except ImportError:
        log("    ⚠️ openai 패키지 없음 (requirements.txt 확인)")
        return None
    c = OpenAI(api_key=key)
    for attempt in range(2):
        try:
            r = c.chat.completions.create(model=model, max_completion_tokens=12000,
                                          messages=[{"role": "user", "content": prompt}])
            return (r.choices[0].message.content or "").strip() or None
        except Exception as e:
            err = str(e)
            if "429" in err and "no credits" not in err.lower():
                log(f"    OpenAI 한도 → 30초 대기 후 재시도 ({attempt+1}/2)")
                time.sleep(30)
            else:
                log(f"    OpenAI 오류: {err[:150]}")
                break
    return None


def _gemini(prompt: str, model: str) -> str | None:
    client = genai.Client(api_key=GEMINI_API_KEY)
    for attempt in range(2):
        try:
            r = client.models.generate_content(model=model, contents=prompt)
            return (r.text or "").strip() or None
        except Exception as e:
            err = str(e)
            if "429" in err:
                m = re.search(r"retry in (\d+(?:\.\d+)?)s", err)
                wait = min(float(m.group(1)) + 3 if m else 60, 65)
                log(f"    Gemini 할당량 초과 → {wait:.0f}초 대기 후 재시도 ({attempt+1}/2)")
                time.sleep(wait)
            else:
                log(f"    Gemini 오류: {err[:150]}")
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

    seg, head = _clean_title(video["title"])

    # Notion 토글 블록 (children 최대 95개 제한)
    toggle = {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": ([{"type": "text",
                            "text": {"content": f"{seg}  "},
                            "annotations": {"bold": True, "color": "gray"}}] if seg else [])
                         + [{"type": "text",
                             "text": {"content": head, "link": {"url": video["url"]}},
                             "annotations": {"bold": True}}],
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



def _refresh_keyword_image(root_id: str, today: str):
    """'많이 나온 단어' 트리맵을 유튜브 토글에 갱신한다(매 실행 교체).

    본문이 길어 한눈에 안 들어온다는 피드백 → 요약 시각화를 함께 보여준다.
    노션 파일업로드 버전(2026-03-11)은 append 의 `after` 를 거부하므로 위치 지정이 안 된다
    (실측 400: "body.after should be not present"). 채널 토글은 접혀 있어 2줄뿐이니
    맨 끝에 붙여도 바로 보인다.
    """
    try:
        import sys as _s
        _s.path.insert(0, os.path.join(_BASE_DIR, "viz"))
        import keywords as KW
        import dashboard as D

        import keyword_insight as KI
        txt, n = KW._load_text(os.path.join(_BASE_DIR, "latest_youtube_analysis.json"), days=2)
        # 사전 대신 AI 추출 → 신조어("호르무즈 해협"·"전력기기"·"주주환원")도 잡힌다.
        # 횟수는 코드가 센다(LLM 이 세면 틀린다).
        items = KI.extract(txt, n, top=28, log=log)
        if len(items) < 5:
            log("  단어 집계 표본 부족 — 생략")
            return
        for b in D.children(root_id):        # 이전 이미지·머리말·표 제거 (매 실행 교체)
            t = b["type"]
            if t in ("image", "table"):
                D._delete([b["id"]])
            elif t == "callout" and any(x in "".join(y.get("plain_text", "")
                                                    for y in b["callout"]["rich_text"])
                                       for x in ("오늘 급증", "처음 등장")):
                D._delete([b["id"]])
            elif t == "bulleted_list_item":
                D._delete([b["id"]])
            elif t == "paragraph" and "트리맵 참고" in "".join(
                    x.get("plain_text", "") for x in b["paragraph"]["rich_text"]):
                D._delete([b["id"]])
            elif t == "paragraph" and "많이 나온 단어" in "".join(
                    x.get("plain_text", "") for x in b["paragraph"]["rich_text"]):
                D._delete([b["id"]])
        D._append(root_id, [{"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": f"📊 많이 나온 단어 (영상 {n}개 기준)"},
                           "annotations": {"bold": True, "color": "gray"}}]}}])
        png = os.path.join(_BASE_DIR, "latest_keywords.png")
        KW.render(items, today, png, n_videos=n)
        with open(png, "rb") as f:
            D.append_image(root_id, f.read(), "keywords.png")

        # 오늘만의 특별한 단어 — 절대 빈도만 보면 AI·금리가 매일 1등이라 변화가 안 보인다.
        # 만자당 빈도로 정규화해 과거 평균과 비교(급증도). 표는 급증 순으로 정렬한다.
        KI.record_day(today, txt, [w for w, _, _, _ in items])
        sp = KI.spike(today, items, log=log)

        def _c(t, bold=False, color=None):
            a = {"bold": bold}
            if color:
                a["color"] = color
            return [{"type": "text", "text": {"content": str(t)}, "annotations": a}]

        def _lift(v):
            if v is None:
                return _c("NEW", True, "purple")
            if v >= 2.0:
                return _c(f"🔥 {v:.1f}배", True, "red")
            if v >= 1.3:
                return _c(f"↗ {v:.1f}배", color="orange")
            if v <= 0.7:
                return _c(f"↘ {v:.1f}배", color="blue")
            return _c(f"{v:.1f}배", color="gray")

        # 정렬 점수: 급증도. NEW 는 배수가 없으므로 언급량으로 가중해 섞는다 —
        # 2회짜리 NEW 가 11.8배 급증한 단어보다 위로 오면 안 된다.
        def _score(x):
            return x[4] if x[4] is not None else (5.0 + x[1] / 10.0)
        sp.sort(key=lambda x: (-_score(x), -x[1]))
        surge = [f"{w} {v:.1f}배" for w, c, k, wh, v in sp if v and v >= 2.0][:5]
        newly = [w for w, c, k, wh, v in sp if v is None][:5]
        note = []
        if surge:
            note.append("🔥 오늘 급증: " + " · ".join(surge))
        if newly:
            note.append("🆕 처음 등장: " + " · ".join(newly))
        if note:
            D._append(root_id, [{"object": "block", "type": "callout", "callout": {
                "icon": {"type": "emoji", "emoji": "✨"}, "color": "gray_background",
                "rich_text": [{"type": "text", "text": {"content": "\n".join(note)},
                               "annotations": {"bold": True}}]}}])

        # 모바일에서 5열 표는 행마다 6줄로 늘어나 25행이면 150줄이 된다(사용자 지적).
        # 한 줄짜리 목록으로 바꾸고 상위 N개만 — 여백 낭비 없이 같은 정보를 담는다.
        TOP_SHOW = int(os.environ.get("KW_SHOW", "15"))
        blocks = []
        for w, c, kind, why, v in sp[:TOP_SHOW]:
            if v is None:
                mark, col = "🆕", "purple"
            elif v >= 2.0:
                mark, col = "🔥", "red"
            elif v >= 1.3:
                mark, col = "↗", "orange"
            elif v <= 0.7:
                mark, col = "↘", "blue"
            else:
                mark, col = "·", "gray"
            lift = "NEW" if v is None else f"{v:.1f}배"
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": [
                               {"type": "text", "text": {"content": f"{mark} {w} "},
                                "annotations": {"bold": True}},
                               {"type": "text", "text": {"content": f"{lift}"},
                                "annotations": {"bold": True, "color": col}},
                               {"type": "text", "text": {"content": f" · {c}회 — "},
                                "annotations": {"color": "gray"}},
                               {"type": "text", "text": {"content": why}}]}})
        if len(sp) > TOP_SHOW:
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {
                "rich_text": [{"type": "text",
                               "text": {"content": f"… 외 {len(sp)-TOP_SHOW}개는 트리맵 참고"},
                               "annotations": {"color": "gray", "italic": True}}]}})
        D.append_blocks(root_id, blocks, chunk=20)
        log(f"  📊 단어 트리맵+이유표 갱신 ({len(items)}단어 / 영상 {n}개)")
    except Exception as e:
        log(f"  ⚠️ 단어 트리맵 생략: {str(e)[:90]}")


def _dashboard_youtube_root(today: str) -> str | None:
    """통합 대시보드의 유튜브 토글(슬롯4) id. 채널 토글들이 이 안에 들어간다.
    사용자는 통합 포트폴리오 페이지만 보므로 날짜별 유튜브 페이지는 만들지 않는다(중복 방지)."""
    try:
        import dashboard as D
    except Exception as e:
        log(f"❌ dashboard 모듈 로드 실패: {e}")
        return None
    title = f"📺 {today} 유튜브 분석"
    tid = D.get_or_create_report(title)
    if not tid:
        return None
    # 키워드 집계는 '오늘의 핵심 요약'(daily_recommend)으로 통합 — 같은 소스이므로 한 곳에
    pages = _load_pages()
    entry = _get_entry(pages, today)
    if not isinstance(entry, dict) or entry.get("page_id") != tid:
        # 토글이 새로 만들어졌거나 대상이 바뀜 → 채널 캐시 초기화(옛 토글 id 재사용 방지)
        pages[today] = {"page_id": tid, "channels": {}}
        _save_daily_pages(pages)
        log(f"  📺 대시보드 유튜브 토글 준비: {tid}")
    return tid


def get_or_create_daily_page(today: str) -> str | None:
    """
    오늘 날짜의 통합 유튜브 분석 페이지 ID 반환 (채널 무관, 1개/일).
    날짜 페이지 ({today}) 안에 자식으로 만듦. 다른 추천 페이지들과 같은 위치.
    제목: 📺 {today} 유튜브 분석
    """
    if os.environ.get("YT_TARGET", "dashboard") == "dashboard":
        return _dashboard_youtube_root(today)      # 통합 대시보드 토글로 (기본)

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

def _touch_root_title(page_id: str | None, today: str):
    """부모 유튜브 토글의 '갱신 HH:MM' 만 현재 시각으로 올린다(영상이 실제로 추가될 때 호출)."""
    if not page_id:
        return
    try:
        import dashboard as D
        D._retitle(page_id, D._stamped(f"📺 {today} 유튜브 분석"))
    except Exception as e:
        log(f"  ⚠️ 유튜브 토글 제목 갱신 실패: {str(e)[:60]}")


def get_or_create_channel_toggle(today: str, channel: dict) -> str | None:
    """
    오늘 통합 페이지 안의 채널 토글 block_id 반환.
    없으면 빈 토글 새로 만들고 ID 캐시한 뒤 반환.
    """
    pages = _load_pages()
    entry = _get_entry(pages, today)
    if entry is None:
        # 지연 생성: 올릴 내용이 확정된 뒤에만 부모를 만든다. 미리 만들면 새 영상이 없는 날
        # (주말·휴일)에 빈 토글이 전날 분석을 덮어쓴다.
        if not get_or_create_daily_page(today):
            log("❌ 유튜브 부모(대시보드 토글) 확보 실패")
            return None
        pages = _load_pages()
        entry = _get_entry(pages, today)
        if entry is None:
            return None
    else:
        # 캐시된 부모를 재사용할 때도 제목의 갱신 시각은 올려야 한다. 안 그러면
        # 하루 종일 영상이 추가되는데 제목은 첫 실행 시각(08:01)에 멈춘다(2026-08-31 지적).
        _touch_root_title(entry.get("page_id"), today)

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

    # 부모(대시보드 유튜브 토글)는 올릴 내용이 확정된 뒤 get_or_create_channel_toggle 에서
    # 지연 생성한다. 여기서 미리 만들면 새 영상이 없는 날 빈 토글이 전날 분석을 덮어쓴다.

    failed = 0
    for channel in CHANNELS:
        log(f"")
        log(f"=== [{channel['name']}] ===")
        try:
            _process_channel(channel, today)
        except Exception as e:
            log(f"❌ {channel['name']} 처리 실패: {e}")
            failed += 1
            continue
    # 전 채널 실패를 success 로 끝내면 며칠씩 조용히 멈춘다(2026-08-18~25 실제 발생).
    # 워크플로가 빨간불이 되어야 알 수 있다.
    if failed == len(CHANNELS):
        raise SystemExit(f"전 채널 실패 ({failed}/{len(CHANNELS)}) — 조용한 중단 방지를 위해 실패 처리")


def _process_channel(channel: dict, today: str):
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
    # YT_DAYS_BACK=N 이면 오늘부터 N일 전 게시분까지만 (0=오늘만, 1=어제+오늘).
    # 밀린 backlog 를 한꺼번에 돌려 Gemini 토큰·프록시 대역폭을 태우는 것 방지.
    # YT_MAX_VIDEOS 로 1회 처리량 상한을 따로 줄 수 있다.
    _db = os.environ.get("YT_DAYS_BACK")
    if _db is not None and _db != "":
        cutoff = (datetime.now(KST) - timedelta(days=int(_db))).strftime("%Y-%m-%d")
        pool = [v for v in new_videos if v["published"] >= cutoff]
    else:
        pool = today_new + other_new
    target = list(reversed(pool[:MAX_VIDEOS_PER_RUN]))

    used = _quota(today)
    left = MAX_ATTEMPTS_PER_DAY - used
    if left <= 0:
        log(f"⏸️ 오늘 자막 시도 한도 도달 ({used}/{MAX_ATTEMPTS_PER_DAY}) — 종료. "
            f"올리려면 YT_MAX_PER_DAY (월 지출 한도 확인 후)")
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
    # 병렬 5 는 Gemini 분당 한도를 즉시 넘겨 429 를 유발한다(2026-08-18 전건 실패).
    with ThreadPoolExecutor(max_workers=int(os.environ.get("YT_WORKERS", "2"))) as ex:
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
    # 표본이 1~2개면 '전부 실패' 가 인프라 장애의 근거가 못 된다. 자막이 아예 없는
    # 생방송 한 편이 걸려도 전부 실패가 되어 프로세스를 죽이고 남은 채널까지 못 돌았다
    # (2026-08-28 실제 발생: 오선 채널 1편 실패로 뒤 4개 채널이 아예 실행 안 됨).
    # 채널 단위로 예외를 올리고, 런 실패 여부는 _main 의 '전 채널 실패' 판정에 맡긴다.
    if target and not results:
        if len(target) >= 3:
            raise RuntimeError(f"처리 대상 {len(target)}개 전부 자막 실패 — 프록시/쿠키/차단 확인")
        log(f"⚠️ 대상 {len(target)}개 전부 자막 실패 — 표본이 작아 장애로 보지 않고 넘어감")
        return

    order = {v["id"]: i for i, v in enumerate(target)}
    results.sort(key=lambda r: order[r["video"]["id"]])

    all_blocks = []
    processed_now = []
    for r in results:
        # Gemini 분석 실패(429 등) 영상은 마킹·업로드 안 함 → 다음 run 에서 재시도.
        # (예전엔 무조건 마킹해서 결제 소진 시간대 영상이 영구 skip 됐음)
        if not r["analysis"]:
            # 자막은 받았는데 Gemini 가 실패한 경우. 마킹을 안 하면 다음 실행에서 자막을
            # 다시 받아 프록시 대역폭을 재소모한다(2026-08-18: 35건 자막·분석 0건 사례).
            # 실패 카운터에 반영해 MAX_FAIL_BEFORE_SKIP 회 넘으면 포기한다.
            vid = r["video"]["id"]
            failed[vid] = failed.get(vid, 0) + 1
            if failed[vid] >= MAX_FAIL_BEFORE_SKIP:
                log(f"    ⏭️ Gemini {failed[vid]}회 실패 → 포기(자막 재취득 방지): {r['video']['title'][:34]}")
                processed_now.append(vid)
            else:
                log(f"    ⏭️ Gemini 실패({failed[vid]}/{MAX_FAIL_BEFORE_SKIP}회) → 다음 run 재시도: {r['video']['title'][:34]}")
            continue
        all_blocks.extend(build_video_blocks(r["video"], r["analysis"], r["transcript_len"]))
        processed_now.append(r["video"]["id"])
        _save_analysis_cache(today, channel, r["video"], r["analysis"])

    if not all_blocks:
        if processed_now:            # Gemini 포기분 — 마킹해서 자막 재취득을 끊는다
            processed.update(processed_now)
            save_processed(processed)
        save_failed(failed)
        log(f"⚠️ 업로드할 내용 없음 (Gemini 포기 {len(processed_now)}건 마킹). 종료.")
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
        save_failed(failed)          # 포기 처리분·재시도 카운트 반영
        log(f"🎉 완료! {len(processed_now)}개 영상 분석 이어붙이기 완료")

if __name__ == "__main__":
    main()
