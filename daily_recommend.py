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
TOP_N              = 100        # TOP 100 까지 입력 (수급 점수 순)
MIN_MCAP_KOSPI     = 0          # 시총 필터 제거 (사용자 요청 — 소형주도 후보)
MIN_MCAP_KOSDAQ    = 0          # 동일


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

    today = datetime.now(KST)
    # 최근 N일 분석본 (오늘 포함) 사용 - 사용자 요청. 기본 7일.
    # 이 입력이 Gemini 호출 크기를 결정한다 — 실측 8/10 에 216,218자였고 그날 429(쿼터초과) 발생.
    # 비용을 줄이려면 YT_ANALYSIS_DAYS=3 처럼 낮추면 된다(리포트 근거 기간이 줄어드는 트레이드오프).
    days = int(os.environ.get("YT_ANALYSIS_DAYS", "7"))
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

    out = []
    for date in dates:
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
    for date in (today, yesterday):        # 2026-08-14~16 크래시 원인: 정의 없는 `dates` 를 순회했음
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

    prompt = f"""당신은 한국 주식 시장 분석가입니다.

아래 **최근 7일 유튜브 화자 분석본** 을 모두 읽고, **오늘 ({today}) 의 한 장 핵심 요약 레포트** 를 작성하세요.
종목 추천이 아니라 **요약 레포트** 입니다.

## 데이터: 유튜브 화자 의견 (최근 7일 영상 분석본)

{youtube_text}

## 출력 형식 (정확히 이대로)

SUMMARY: <오늘 보고서를 관통하는 한 줄 (40자 이내, 예: "AI/반도체 견조, 방산·바이오 신규 모멘텀">

# 📊 {today} 오늘의 핵심 요약

## 🏷️ 자주 언급된 종목 TOP 5

화자들이 자주 언급한 종목 5개 (언급 빈도 순). 추천이 아니라 주목도 표시.

| 순위 | 종목명 (코드) | 언급 횟수 | 주요 화자 | 핵심 시각 |
|---|---|---|---|---|
| 1 | ... | N회 | 채널 1, 채널 2 | 긍정/부정/중립 + 1줄 |
| 2 | ... | ... | ... | ... |
| ... | ... | ... | ... | ... |
| 5 | ... | ... | ... | ... |

## 🎤 화자별 핵심 의견

주요 화자 5~8명. 각 화자별 한 마디:

- **{{채널/화자명}}**: "한 줄 핵심 (50자 이내, 화자가 강조한 본 의견)"
- **{{채널/화자명}}**: "..."
- (5~8개)

## 🌐 매크로 오늘의 이슈

화자들이 점검한 거시 이슈 3~5개. 각 이슈별:

- **{{이슈명}}** (예: 미중 무역, AI 사이클, 금리, 지정학, 환율): 1~2줄 정리 + 화자 의견 종합

⚠️ 추측 금지. 데이터에 없는 종목/화자/이슈 추가 X. 모든 종목명·인용은 정확히 데이터 1 에서.
⚠️ 한 장 분량으로 (전체 2,500자 내외). 너무 길게 X.
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
    """노션 database 의 today row 찾거나 생성. NOTION_DAILY_DB_ID 사용."""
    db_id = os.environ.get("NOTION_DAILY_DB_ID", "")
    if not db_id:
        log("❌ NOTION_DAILY_DB_ID 미설정.")
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



def push_to_notion(text: str) -> str | None:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    title = f"📊 {today} 오늘의 핵심 요약"

    # 다른 추천 페이지들과 같이 "YYYY-MM-DD" 날짜 페이지 안에 넣기
    date_page_id = _get_or_create_date_page(today)
    if not date_page_id:
        log("❌ 날짜 페이지 가져오기 실패. 최종 추천 페이지 생성 중단.")
        return None

    # 사용자는 통합 대시보드만 보므로 날짜별 페이지를 만들지 않는다(중복 방지).
    # date_page_id 는 아래 'DB 행 제목(헤드라인) 갱신' 용도로 계속 필요하다.
    dash = os.environ.get("REC_TARGET", "dashboard") == "dashboard"
    if dash:
        import dashboard as D
        page_id = D.add_report(title, [])      # 슬롯2 토글 확보(같은 날 재실행이면 교체)
        if not page_id:
            log("❌ 대시보드 토글 생성 실패")
            return None
        page_url = D.url()
    else:
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

    # markdown → 노션 블록 변환 (heading/bullet/bold/표 처리)
    blocks = []
    src_lines = text.split("\n")
    idx = 0
    while idx < len(src_lines):
        raw = src_lines[idx]
        line = raw.strip()
        if not line:
            idx += 1
            continue
        # 마크다운 표: | a | b | + 다음 줄 |---|---|
        if line.startswith("|") and line.endswith("|") and idx + 1 < len(src_lines):
            next_line = src_lines[idx + 1].strip()
            if next_line.startswith("|") and "---" in next_line:
                header_cells = [c.strip() for c in line.strip("|").split("|")]
                col_count = len(header_cells)
                data_rows = []
                j = idx + 2
                while j < len(src_lines):
                    row_line = src_lines[j].strip()
                    if not row_line.startswith("|"):
                        break
                    cells = [c.strip() for c in row_line.strip("|").split("|")]
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
        if line.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                           "heading_3": {"rich_text": _parse_bold(line[4:])}})
        elif line.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                           "heading_2": {"rich_text": _parse_bold(line[3:])}})
        elif line.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1",
                           "heading_1": {"rich_text": _parse_bold(line[2:])}})
        elif line.startswith("- "):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _parse_bold(line[2:])}})
        elif line.startswith("---") or line.startswith("***"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        else:
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _parse_bold(line)}})
        idx += 1

    # 외부 블록 1개씩 (youtube_report 패턴 — 100블록/요청 한도 회피, 여기선 어차피 적지만 안전)
    for i in range(0, len(blocks), 1):
        r = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=_nh(),
            json={"children": blocks[i:i+1]},
            timeout=60,
        )
        if r.status_code != 200:
            # 예전엔 여기서 return 해서 아래 row 제목 update 를 건너뛰었다 → 블록 하나 실패에
            # 날짜 행이 "준비 중" 으로 남는 문제. 본문이 일부라도 올라갔으면 제목은 달아준다.
            log(f"⚠️ 블록 추가 실패 ({i}/{len(blocks)}): {r.status_code} {r.text[:120]} — 나머지 중단, 제목은 계속")
            break
        time.sleep(0.2)

    # 응답에서 SUMMARY 추출 후 database row 의 '이름' (title) UPDATE
    m = re.search(r"^SUMMARY:\s*(.+)$", text, re.MULTILINE)
    summary = m.group(1).strip()[:80] if m else f"{today} 분석 완료"
    try:
        requests.patch(
            f"https://api.notion.com/v1/pages/{date_page_id}",
            headers=_nh(),
            json={"properties": {"이름": {"title": [{"type": "text", "text": {"content": summary}}]}}},
            timeout=15,
        )
        log(f"✅ row 제목 update: {summary[:60]}")
    except Exception as e:
        log(f"⚠️ row 제목 update 실패: {e}")

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
    # 정량 데이터 사용 안 함 - 핵심 요약 레포트로 변경 (사용자 요청)
    quant_csv = ""

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
        # 무음 실패 방지: 2026-08-10~13 Gemini 429 로 나흘간 제목이 "준비 중" 이었는데
        # 워크플로가 success 로 보고돼 아무도 몰랐다. 이제는 실패로 드러난다.
        log("❌ Gemini 응답 실패 (429 쿼터 초과 가능) — 종료코드 1")
        raise SystemExit(1)
    log(f"✅ Gemini 응답: {len(rec):,}자")

    # 4. 노션 push
    url = push_to_notion(rec)
    if url:
        log(f"🎉 완료: {url}")
    else:
        log("⚠️ 노션 push 실패 — Gemini 응답은 정상 (로그 위에 출력됨) — 종료코드 1")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
