"""유튜브 분석 원문 → 핵심 키워드 + '왜 언급되는가' 이유 (Gemini 1콜).

사전 방식의 한계(신조어를 못 잡음)를 여기서 해결한다. Gemini 가 원문을 읽고 반복 등장하는
키워드를 뽑되, **횟수는 우리가 직접 센다**(LLM 이 세면 틀린다). 즉 추출은 AI, 계량은 코드.

이유는 원문 앞부분을 잘라오는 게 아니라 "왜 계속 언급되는가" 를 전체 맥락에서 요약하게 한다.
"""
import os, re, json, collections

# Gemini HTTP 타임아웃(ms). 없으면 응답이 안 올 때 무한 대기한다 — 2026-08-28 holdings_report 가
# Gemini 연결에 3일 6시간 물려 좀비로 남았고, launchd 가 이후 실행을 전부 건너뛰었다.
GENAI_TIMEOUT_MS = int(os.environ.get("GENAI_TIMEOUT_MS", "180000"))


CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest_keyword_insight.json")


def _count_exact(txt, words):
    """긴 단어 우선 + 구간 마스킹으로 중복 없이 센다 (LLM 이 아니라 코드가 센다)."""
    mask = bytearray(len(txt))
    cnt = collections.Counter()
    for w in sorted({w for w in words if w and len(w) >= 2}, key=len, reverse=True):
        for m in re.finditer(re.escape(w), txt):
            a, b = m.start(), m.end()
            if any(mask[a:b]):
                continue
            for i in range(a, b):
                mask[i] = 1
            cnt[w] += 1
    return cnt


def extract(txt, n_videos, top=28, log=print):
    """[(단어, 횟수, 종류, 이유)] — 캐시 있으면 재사용(본문 길이 같으면 동일 입력으로 간주)."""
    key = f"{len(txt)}:{n_videos}:{top}"
    if os.path.exists(CACHE):
        try:
            c = json.load(open(CACHE))
            if c.get("key") == key and c.get("items"):
                log(f"  키워드 인사이트 캐시 재사용 ({len(c['items'])}개)")
                return [tuple(x) for x in c["items"]]
        except Exception:
            pass
    if not os.environ.get("GEMINI_API_KEY"):
        return []

    from google import genai
    prompt = f"""아래는 한국 주식 유튜브 {n_videos}개의 분석본이다.

## 할 일
반복적으로 등장하는 **핵심 키워드 {top}개**를 뽑고, 각 키워드가 **왜 계속 언급되는지** 설명하라.

## 키워드 규칙
- 종목명·기업명·테마·매크로 지표·신조어를 모두 포함한다. 사전에 없는 새 용어도 반드시 잡아라.
- 조사·접속사·일반명사(시장, 오늘, 생각, 부분 등)는 제외한다.
- 본문에 **그대로 등장하는 표기**로 적어라(횟수를 세야 하므로 변형·번역 금지).
- 2~12자. 띄어쓰기 없는 형태를 우선한다.

## 이유 규칙
- 앞부분을 요약하지 말고 **전체를 읽고 "왜 이 단어가 계속 나오는지"** 를 쓴다.
- **50자 이내**. 모바일에서 한두 줄로 읽혀야 한다.
- ⚠️ **단어의 정의를 쓰지 마라.** "~를 가늠하는 핵심 지표", "~에 필수적인 기술" 같은 설명은 금지.
  본문에 나온 **수치·날짜·기업명·사건**을 넣어 "오늘 무슨 일이 있었는지" 를 써라.
  좋은 예: "30년물 국채 5.309%, 2007년 이후 최고"
           "빌 게이츠 테라파워가 SK와 SMR 동맹 발표"
           "AI 토큰 사용량 전년比 7~10배 급증"
  나쁜 예: "연준의 금리 정책 방향을 가늠하는 핵심 물가 지표"  ← 정의일 뿐
           "AI 인프라 구축에 필수적이며 안정적 수익 창출"      ← 일반론
- 본문에 수치가 있으면 **반드시 수치를 넣어라**. 없으면 구체적 사건·기업명이라도 넣어라.
- "때문", "이기 때문", "하고 있다" 같은 꼬리말을 붙이지 마라.
- 본문에 없는 내용을 만들지 마라.

## 출력 (정확히 이 형식, 헤더·설명 없이 {top}줄)
키워드|종류|이유
(종류는 종목 / 테마 / 매크로 중 하나)

## 분석본
{txt[:120000]}
"""
    try:
        c = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"timeout": GENAI_TIMEOUT_MS})
        r = c.models.generate_content(
            model="gemini-2.5-flash", contents=prompt,
            config={"max_output_tokens": 4000, "thinking_config": {"thinking_budget": 0}})
        raw = (r.text or "").strip()
    except Exception as e:
        log(f"  ⚠️ 키워드 인사이트 실패: {str(e)[:90]}")
        return []

    # 모델이 코드펜스·서문·표 헤더를 붙이는 경우가 있어 방어적으로 벗긴다(실측)
    raw = re.sub(r"^```[a-zA-Z]*\s*$", "", raw, flags=re.M)
    rows = []
    for line in raw.splitlines():
        line = line.strip().strip("|")          # 마크다운 표로 올 때 양끝 파이프
        if set(line) <= set("-| :"):            # 표 구분선
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3 or not parts[0] or parts[0] == "키워드":
            continue
        w, kind, why = parts[0], parts[1], parts[2]
        # 구분선(---, ===)·기호만 있는 줄이 단어로 파싱되던 문제 (실측: '---' 169회로 1위)
        if not re.search(r"[가-힣A-Za-z0-9]", w) or len(w) < 2 or len(w) > 14:
            continue
        if not why or len(why) < 6:
            continue
        if kind not in ("종목", "테마", "매크로"):
            kind = "테마"
        rows.append((w, kind, why))
    if not rows:
        log("  ⚠️ 키워드 파싱 결과 없음")
        return []

    cnt = _count_exact(txt, [w for w, _, _ in rows])
    items = [(w, int(cnt.get(w, 0)), k, why) for w, k, why in rows if cnt.get(w, 0) >= 2]
    items.sort(key=lambda x: -x[1])
    json.dump({"key": key, "items": items}, open(CACHE, "w"), ensure_ascii=False)
    log(f"  키워드 인사이트 {len(items)}개 (AI 추출 → 코드 계량)")
    return items


# ── 오늘만의 특별한 단어 (평소 대비 급증도) ─────────────────────────────
HIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keyword_history.json")


def _load_hist():
    if os.path.exists(HIST):
        try:
            return json.load(open(HIST))
        except Exception:
            pass
    return {}


def record_day(date, txt, words, chars=None):
    """그날의 단어별 빈도와 본문 길이를 누적 저장. 정규화(만자당 빈도)를 위해 길이도 남긴다.
    분석 캐시는 7일만 보관되지만 이 파일은 계속 쌓여 baseline 이 두꺼워진다."""
    h = _load_hist()
    cnt = _count_exact(txt, words)
    h[date] = {"chars": chars if chars is not None else len(txt),
               "counts": {w: int(c) for w, c in cnt.items() if c > 0}}
    json.dump(h, open(HIST, "w"), ensure_ascii=False)
    return h


def spike(date, items, min_days=2, log=print):
    """[(단어, 오늘횟수, 종류, 이유, 배수 or None)] — 평소 대비 급증도.

    절대 빈도만 보면 AI·금리·실적이 매일 1등이라 '오늘만의 단어'가 안 보인다(사용자 지적).
    만자당 빈도로 정규화해 과거 평균과 비교한다. 과거에 없던 단어는 NEW(배수 None).
    baseline 이 얇으면(min_days 미만) 판단을 보류하고 None 을 돌려준다.
    """
    h = _load_hist()
    past = {d: v for d, v in h.items() if d < date and v.get("chars")}
    if len(past) < min_days:
        log(f"  급증도 보류 — 과거 표본 {len(past)}일 (최소 {min_days}일 필요)")
        return [(w, c, k, why, None) for w, c, k, why in items]
    today = h.get(date) or {}
    tchars = max(today.get("chars", 1), 1)
    out = []
    for w, c, k, why in items:
        t_rate = c / tchars * 10000
        rates = [v["counts"].get(w, 0) / v["chars"] * 10000 for v in past.values()]
        base = sum(rates) / len(rates)
        out.append((w, c, k, why, (t_rate / base) if base > 0.05 else None))
    return out
