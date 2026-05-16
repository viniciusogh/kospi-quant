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
import _env  # .env 자동 로드
import re
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

GEMINI_API_KEY        = os.environ["GEMINI_API_KEY"]
NOTION_API_KEY        = os.environ["NOTION_API_KEY"]
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_REC_PARENT_ID",  "3324a00632f880fbb014d766d87a1079")  # 코스피 추천종목 부모

ANALYSIS_CACHE     = os.path.join(_BASE_DIR, "latest_youtube_analysis.json")
NOTION_DAILY_PAGES = os.path.join(_BASE_DIR, "notion_daily_pages.json")
LOCK_FILE          = os.path.join(_BASE_DIR, "daily_recommend.lock")
LOCK_MAX_AGE_HOURS = 0.5  # 30분 (정상 실행은 1~2분)
TOP_N              = 100        # TOP 100 까지 입력 (밖은 자동 제외)
MIN_MCAP_KOSPI     = 500_000    # 백만원 단위 = 5,000억 이상 (소형주 노이즈 제외)
MIN_MCAP_KOSDAQ    = 100_000    # 1,000억 이상 (KOSDAQ 은 대형주도 5천억 미만 많음)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_top_json(path: str, n: int = TOP_N, min_mcap: int = 0) -> str:
    """CSV TOP N (시총 필터 적용) 을 종목별 JSON record 리스트로 변환.
    min_mcap: 시가총액 최소값 (백만원 단위). 0 이면 필터 없음. 소형주 노이즈 제거용.
    """
    if not os.path.exists(path):
        return ""
    try:
        df = pd.read_csv(path)
        if min_mcap > 0 and "시가총액" in df.columns:
            df = df[df["시가총액"] >= min_mcap]
        records = df.head(n).to_dict(orient="records")
        return json.dumps(records, ensure_ascii=False, indent=1)
    except Exception as e:
        log(f"  ⚠️ {path} 읽기 실패: {e}")
        return ""


def load_youtube_analysis_from_cache() -> str:
    """1차: json cache 에서 분석본 추출 (빠르고 무료)."""
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


def _fetch_block_text(block_id: str, depth: int = 0, max_depth: int = 4) -> str:
    """재귀로 노션 블록의 모든 자식 텍스트 추출 (토글도 펼쳐서)."""
    if depth >= max_depth:
        return ""

    out = []
    cursor = None
    while True:
        params = {"start_cursor": cursor} if cursor else {}
        try:
            r = requests.get(
                f"https://api.notion.com/v1/blocks/{block_id}/children",
                headers=_nh(), params=params, timeout=30,
            )
        except Exception:
            break
        if r.status_code != 200:
            break
        data = r.json()

        for block in data.get("results", []):
            btype = block.get("type")
            if btype == "toggle":
                title = "".join(t.get("plain_text", "") for t in block["toggle"].get("rich_text", []))
                if title:
                    out.append(f"\n{'#' * min(depth + 2, 6)} {title}")
                child = _fetch_block_text(block["id"], depth + 1, max_depth)
                if child:
                    out.append(child)
            elif btype in ("paragraph", "heading_1", "heading_2", "heading_3",
                           "bulleted_list_item", "numbered_list_item", "quote"):
                rt = block[btype].get("rich_text", [])
                text = "".join(t.get("plain_text", "") for t in rt)
                if text:
                    out.append(text)
            # divider, image, 기타: 무시
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return "\n".join(out)


def load_youtube_analysis_from_notion() -> str:
    """2차 fallback: notion_daily_pages.json 의 어제·오늘 페이지에서 직접 분석본 추출.
    cache 비었거나 손상된 경우만 호출. 노션 API 호출 N번 — 캐시보단 느림.
    """
    if not os.path.exists(NOTION_DAILY_PAGES):
        return ""
    try:
        with open(NOTION_DAILY_PAGES) as f:
            pages = json.load(f)
    except Exception:
        return ""

    today     = datetime.now(KST).strftime("%Y-%m-%d")
    yesterday = (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")

    out = []
    for date in [yesterday, today]:
        entry = pages.get(date)
        if entry is None:
            continue
        page_id = entry if isinstance(entry, str) else entry.get("page_id")
        if not page_id:
            continue
        log(f"  ↪ 노션 페이지 fetch: {date}")
        text = _fetch_block_text(page_id, depth=0)
        if text:
            out.append(f"### [{date}]\n\n{text}\n")

    return "\n---\n\n".join(out)


def load_youtube_analysis() -> tuple[str, str]:
    """cache + 노션 fallback 둘 다 사용 (cache 풍부해질 때까지 임시).
    cache 가 50,000자 이상이면 cache 만, 미만이면 노션 fallback 도 합쳐서 보완.
    """
    text_cache = load_youtube_analysis_from_cache()
    if len(text_cache) >= 50000:
        return text_cache, "cache"

    text_notion = load_youtube_analysis_from_notion()
    if text_notion and text_cache:
        return text_cache + "\n\n---\n\n" + text_notion, "cache+notion"
    if text_notion:
        return text_notion, "notion"
    if text_cache:
        return text_cache, "cache"
    return "", "none"


def analyze_and_recommend(quant_csv: str, youtube_text: str) -> str | None:
    client = genai.Client(api_key=GEMINI_API_KEY)
    today  = datetime.now(KST).strftime("%Y-%m-%d")

    prompt = f"""당신은 한국 주식 시장 종합 분석가입니다.

오늘({today}) **KOSPI 7~9 종목 + KOSDAQ 5~7 종목, 총 12~16 종목** 을 추천하세요.

## 데이터 1: 수급 모델 TOP 100 (한국투자증권, 시총 필터 적용)
- KOSPI 수급 모델 + KOSDAQ 수급 모델 (외국인·기관 순매수, 수급 강도 점수 기반)
- KOSPI: 시총 5,000억 이상만 (소형주 제외)
- KOSDAQ: 시총 1,000억 이상만
- 각 종목은 별도 JSON 객체. **종목명/코드/지표를 해당 record 에서 정확히 인용**하세요.
- (Quality 모델은 별도 페이지 (💎 KOSPI Quality 추천종목) 에 있으므로 여기선 다루지 않음)

{quant_csv}

## 데이터 2: 유튜브 화자 의견 (최근 2일 영상 분석본)
{youtube_text}

## 추천 규칙 (엄격히 지킬 것)

1. **시장 분리**: KOSPI 추천과 KOSDAQ 추천을 **별도 섹션**으로.
   - KOSPI: 7~9 종목
   - KOSDAQ: 5~7 종목
2. **후보 = 정량 데이터에 있는 종목만** (TOP 100 + 시총 필터 통과). 유튜브에서 강추되어도 데이터에 없으면 제외.

3. **유튜브 시그널 필수 요건 (가장 중요)**:
   - 각 추천 종목에 **반드시 2명 이상의 화자 인용** 필수. 1명만 인용 가능한 종목은 추천 X.
   - 인용 못 찾으면 **그 종목 빼고 다른 종목 선택**. "직접 언급 없지만…" 같은 빈 인용 절대 금지.
   - **유튜브 분석본 전체를 끝까지 훑고**, 해당 종목명·종목 코드·섹터(반도체/조선/금융/방산/이차전지/제약 등)·관련 키워드(예: HBM, 로봇, 보스턴 다이내믹스, AI 메모리)를 모두 검색해서 화자가 언급한 부분을 다 모아 오세요.
   - 한 부분만 보고 인용 끝내지 말고, **여러 영상·여러 화자에 분산된 의견을 종합**하여 가장 적합한 2~3개를 인용.
   - 직접 인용 + 화자명·채널명 명시. (예: 김장열·3proTV — "...")

4. 각 종목별 형식:
   - **수급 시그널**: JSON record 에서 그대로 인용 — 수급 모델 랭킹(KOSPI/KOSDAQ 몇 위), 외국인+기관 순매수대금, 수급강화점수, 멀티팩터점수, 매수우위비율(10일) 등 **수급 관련 지표** 4~6개. (재무 지표는 보조 정보로만, 핵심 X)
   - **유튜브 시그널**: 화자명·채널명 — "직접 인용1"  /  다른화자명·채널명 — "직접 인용2"
   - **결론**: 1~2 줄.

5. **KOSDAQ 처리**: 유튜브에서 KOSDAQ 종목 언급 적으면 솔직히 "유튜브에서 KOSDAQ 언급 빈약" 명시하고 추천 수 줄이세요 (0개 가능).

## 데이터 정확성 (절대 지킬 것)

- 종목명·종목코드는 JSON 의 그 필드에서 그대로 인용. 환각 금지.
- 정량 지표 값은 해당 종목 record 에서 그대로 인용. 다른 종목 값으로 채우지 마세요.
- TOP 100 + 시총 필터 안에 없는 종목은 추천 X.

## 출력 형식

## 오늘의 핵심
(4~6줄. 오늘 KOSPI/KOSDAQ 추천 종목의 공통 테마 + 시장 분위기.)

## KOSPI 추천 (대형주)

### 1. 종목명 (코드)
- **수급 시그널**: KOSPI 수급 N위, 외인+기관 +XXX억, 수급강화점수 X, 멀티팩터 X, 매수우위비율(10일) X ...
- **유튜브 시그널**: 화자명·채널명 — "인용1"  /  다른화자명·채널명 — "인용2"
- **결론**: ...

### 2. ... (7~9번까지)

## KOSDAQ 추천

### 1. 종목명 (코드)
- ...

### 2. ... (5~7번까지)

## ⚠️ 주의
2~3줄. 추천의 한계 (유튜브 편향, 모델 시차 등).

규칙:
- 마크다운 ** 강조 그대로. 노션이 자동 변환.
- flat bullet 만 (sub-bullet X).
- 유튜브 인용 없는 종목 추천 X.
"""

    for attempt in range(2):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                # gemini-2.5-flash 는 thinking 모델 — thinking 토큰이 max_output 을 잡아먹어
                # 출력 짧아지는 문제 (90자 truncated 케이스). thinking 끄고 출력 토큰 충분히 확보.
                config={
                    "max_output_tokens": 12000,  # 12~16종목 × ~300자 + 핵심 + 주의 = 여유 있게
                    "thinking_config": {"thinking_budget": 0},
                },
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


def _parse_bold(text: str) -> list:
    """**bold** 마크다운을 Notion rich_text bold annotation 으로 변환."""
    parts = re.split(r'(\*\*[^*]+\*\*)', text)
    out = []
    for p in parts:
        if p.startswith("**") and p.endswith("**"):
            out.append({"type": "text", "text": {"content": p[2:-2]},
                        "annotations": {"bold": True}})
        elif p:
            out.append({"type": "text", "text": {"content": p}})
    return out or [{"type": "text", "text": {"content": text}}]


def _get_or_create_date_page(today: str) -> str | None:
    """🤖 Claude / {today} 날짜 페이지 찾거나 생성. 다른 워크플로우들이 만드는 것과 같은 패턴."""
    cursor = None
    while True:
        url = f"https://api.notion.com/v1/blocks/{NOTION_PARENT_PAGE_ID}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        r = requests.get(url, headers=_nh(), timeout=15)
        if r.status_code != 200:
            log(f"⚠️ 부모 children 조회 실패 ({r.status_code}): {r.text[:200]}")
            break
        j = r.json()
        for b in j.get("results", []):
            if b.get("type") == "child_page" and b.get("child_page", {}).get("title") == today:
                log(f"✅ 날짜 페이지 재사용: {today}")
                return b["id"]
        if not j.get("has_more"):
            break
        cursor = j.get("next_cursor")

    body = {
        "parent":     {"page_id": NOTION_PARENT_PAGE_ID},
        "properties": {"title": {"title": [{"text": {"content": today}}]}},
    }
    r = requests.post("https://api.notion.com/v1/pages", headers=_nh(), json=body, timeout=30)
    if r.status_code != 200:
        log(f"❌ 날짜 페이지 생성 실패 ({r.status_code}): {r.text[:200]}")
        return None
    log(f"✅ 날짜 페이지 생성: {today}")
    return r.json()["id"]


def push_to_notion(text: str) -> str | None:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    title = f"🎯 {today} 최종 추천종목"

    # 다른 추천 페이지들과 같이 "YYYY-MM-DD" 날짜 페이지 안에 넣기
    date_page_id = _get_or_create_date_page(today)
    if not date_page_id:
        log("❌ 날짜 페이지 가져오기 실패. 최종 추천 페이지 생성 중단.")
        return None

    body = {
        "parent":     {"page_id": date_page_id},
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
    }
    r = requests.post("https://api.notion.com/v1/pages", headers=_nh(), json=body, timeout=30)
    if r.status_code != 200:
        log(f"❌ 페이지 생성 실패 ({r.status_code}): {r.text[:200]}")
        return None
    page_id  = r.json()["id"]
    page_url = r.json().get("url", "")

    # markdown → 노션 블록 변환 (heading/bullet/bold 처리)
    blocks = []
    for raw in text.split("\n"):
        line = raw.strip()  # 양쪽 공백 제거 (들여쓰기 sub-bullet 도 정상 처리)
        if not line:
            continue
        if line.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                           "heading_3": {"rich_text": _parse_bold(line[4:])}})
        elif line.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                           "heading_2": {"rich_text": _parse_bold(line[3:])}})
        elif line.startswith("- "):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _parse_bold(line[2:])}})
        elif line.startswith("---") or line.startswith("***"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        else:
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _parse_bold(line)}})

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
    # 중복 실행 방지 (cron + manual 동시 실행 → 2번 push 되던 문제)
    if os.path.exists(LOCK_FILE):
        lock_age_h = (time.time() - os.path.getmtime(LOCK_FILE)) / 3600
        if lock_age_h < LOCK_MAX_AGE_HOURS:
            log(f"⚠️ 이전 실행 진행 중 (lock 나이 {lock_age_h:.1f}h). 스킵.")
            return
        log(f"⚠️ stale lock 제거 ({lock_age_h:.1f}h)")
        os.remove(LOCK_FILE)
    open(LOCK_FILE, "w").close()

    try:
        _run()
    finally:
        try:
            os.remove(LOCK_FILE)
        except FileNotFoundError:
            pass


def _run():
    log("▶ 일일 종합 추천 시작")

    # 1. 정량 데이터 (수급 모델만 — Quality 는 별도 페이지 (💎 KOSPI Quality 추천종목) 있음)
    parts = []
    for fname, label, mcap in [
        ("latest_kospi_supply.csv",         "KOSPI 수급 모델",  MIN_MCAP_KOSPI),
        ("latest_kosdaq.csv",  "KOSDAQ 수급 모델", MIN_MCAP_KOSDAQ),
    ]:
        rec = load_top_json(os.path.join(_BASE_DIR, fname), TOP_N, mcap)
        if rec:
            parts.append(f"### {label} TOP {TOP_N} (시총 필터 적용, 각 객체가 1 종목)\n{rec}")
    quant_csv = "\n\n".join(parts)
    log(f"✅ 정량 데이터 수집: {len(quant_csv):,}자 (KOSPI ≥{MIN_MCAP_KOSPI//10000}억, KOSDAQ ≥{MIN_MCAP_KOSDAQ//10000}억)")

    if not quant_csv:
        log("❌ 정량 데이터 없음. 종료.")
        return

    # 2. 유튜브 분석본 (어제+오늘) — cache 우선, 비었으면 노션 fallback
    youtube_text, source = load_youtube_analysis()
    log(f"✅ 유튜브 분석본: {len(youtube_text):,}자 (소스: {source})")

    if not youtube_text:
        log("⚠️ 유튜브 분석본 비었음 — 어제·오늘 처리된 영상 없음. 종료.")
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
