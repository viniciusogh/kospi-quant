"""일일 아카이브 — 대시보드 4개 리포트를 읽어 종합 레포트를 쓰고 Report DB 에 날짜별로 남긴다.

통합 대시보드는 매 실행마다 덮어써서 과거가 안 남는다(사용자 지적 2026-09-01).
나중에 '그때 추천이 실제로 올랐나' 를 평가하려면 추천 시점의 종목·진입가가 보존돼야 한다.
→ 추천종목·종목코드·진입가·게이트를 **DB 속성**으로 남겨 정렬·필터·집계가 되게 한다.

실행: python daily_archive.py        (하루 1회, 장 마감 후)
평가: python daily_archive.py --eval  (과거 행의 평가수익률 채우기)
"""
import os, sys, json, re, subprocess
import _env
import requests
from datetime import datetime, timedelta

import momentum_daily as M
import dashboard as D
from momentum_backtest import token, KST

_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.environ["NOTION_DAILY_DB_ID"]
API = "https://api.notion.com/v1"
_H = {"Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
      "Content-Type": "application/json", "Notion-Version": "2022-06-28"}


def _rt(t, bold=False, color=None):
    a = {"bold": bold}
    if color:
        a["color"] = color
    return [{"type": "text", "text": {"content": str(t)}, "annotations": a}]


def _para(rich):
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich}}


def _h2(t):
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rt(t, True)}}


def _h3(t):
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": _rt(t, True)}}


def _bul(rich):
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich}}


def collect(tok):
    """원본 파일에서 직접 재료를 모은다. 대시보드 스크랩이 아니라 소스에서 —
    사용자가 '원문 스냅샷이 아니라 원문 자체' 를 원하고, 통합 대시보드는 안 살려도 된다고 했다."""
    import pandas as pd
    d = {}
    d["trend"] = M.kospi_trend(tok)

    # 섹터: sector_history.csv 의 오늘 행
    try:
        h = pd.read_csv(os.path.join(_DIR, "sector_history.csv"), encoding="utf-8-sig")
        h = h[h["date"] == h["date"].max()]
        h = h.drop_duplicates(subset=["섹터"], keep="last").sort_values("오늘", ascending=False)
        d["sector"] = h
    except Exception as e:
        M.log(f"  ⚠️ 섹터 이력 없음: {str(e)[:60]}"); d["sector"] = None

    # 모멘텀: 추천 CSV + 오늘 지표 + 수급 + 캐시된 촉매/리스크
    cands = []
    try:
        r = pd.read_csv(os.path.join(_DIR, "latest_momentum_reco.csv"), dtype={"code": str})
        cache = json.load(open(os.path.join(_DIR, "momentum_analysis.json"), encoding="utf-8"))
        for _, x in r.head(10).iterrows():
            code = str(x["code"]).zfill(6)
            s2 = M.score_today(code, tok)
            if not s2:
                continue
            fl = M.investor_flows(code, tok) or {}
            a = cache.get(code, {})
            cands.append({"name": x["종목명"], "code": code, "sector": x.get("섹터", "-"),
                          "price": s2["price"], "chg": s2["chg"], "ret5": s2["ret5"],
                          "ret20": s2["ret20"], "hi60": s2["hi60"],
                          "per": s2.get("per"), "pbr": s2.get("pbr"),
                          "frgn5": fl.get("frgn5", 0), "orgn5": fl.get("orgn5", 0),
                          "prsn5": fl.get("prsn5", 0),
                          "한줄": a.get("한줄", ""), "촉매": a.get("촉매", ""),
                          "리스크": a.get("리스크", "")})
    except Exception as e:
        M.log(f"  ⚠️ 모멘텀 재료 실패: {str(e)[:70]}")
    d["cands"] = cands

    # 보유 현황 + 심층분석 (portfolio.json / momentum_analysis.json)
    try:
        pf = json.load(open(os.path.join(_DIR, "portfolio.json"), encoding="utf-8"))
        d["pf"] = pf
        cache = json.load(open(os.path.join(_DIR, "momentum_analysis.json"), encoding="utf-8"))
        for pos in pf.get("positions", []):
            a = cache.get(pos["code"], {})
            pos["한줄"] = a.get("한줄", "")
            pos["촉매"] = a.get("촉매", "")
            pos["리스크"] = a.get("리스크", "")
    except Exception as e:
        M.log(f"  ⚠️ 보유 재료 실패: {str(e)[:60]}"); d["pf"] = None

    # 유튜브: 최신 분석본 + 키워드 급증
    try:
        y = json.load(open(os.path.join(_DIR, "latest_youtube_analysis.json"), encoding="utf-8"))
        day = sorted(y)[-1]
        vids = [v for ch in y[day].values() for v in ch]
        d["yt_day"] = day
        d["yt"] = [{"title": v["title"], "ch": v.get("channel_name", ""),
                    "analysis": v.get("analysis", "")} for v in vids]
    except Exception as e:
        M.log(f"  ⚠️ 유튜브 재료 실패: {str(e)[:60]}"); d["yt"] = []
    try:
        import keyword_insight as KI
        sys.path.insert(0, os.path.join(_DIR, "viz"))
        import keywords as KW
        txt, n = KW._load_text(os.path.join(_DIR, "latest_youtube_analysis.json"), days=2)
        items = KI.extract(txt, n, top=25, log=lambda *a: None)
        _today = datetime.now(KST).strftime("%Y-%m-%d")
        KI.record_day(_today, txt, [w for w, _, _, _ in items])   # 오늘 표본을 먼저 남긴다
        d["kw"] = KI.spike(_today, items, log=M.log)
    except Exception as e:
        M.log(f"  ⚠️ 키워드 재료 실패: {str(e)[:60]}"); d["kw"] = []
    return d


def _llm(prompt, model, max_tokens):
    """OpenAI 우선, 실패 시 Gemini 폴백. 한쪽이 죽어도 레포트는 나와야 한다."""
    if model.startswith(("gpt-", "o3", "o4")):
        try:
            from openai import OpenAI
            c = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            r = c.chat.completions.create(model=model, max_completion_tokens=max_tokens,
                                          messages=[{"role": "user", "content": prompt}])
            return (r.choices[0].message.content or "").strip()
        except Exception as e:
            M.log(f"  ⚠️ {model} 실패 → Gemini 폴백: {str(e)[:80]}")
    try:
        from google import genai
        c = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"timeout": GENAI_TIMEOUT_MS})
        r = c.models.generate_content(model="gemini-2.5-flash", contents=prompt,
                config={"max_output_tokens": max_tokens, "thinking_config": {"thinking_budget": 0}})
        return (r.text or "").strip()
    except Exception as e:
        M.log(f"  ❌ LLM 실패: {str(e)[:80]}"); return ""


def _fmt_material(d):
    """LLM 에 넘길 재료를 텍스트로. 숫자는 전부 우리가 계산한 값이다."""
    L = []
    t = d.get("trend") or {}
    L.append(f"[게이트] {t.get('text','')} · 통과여부={t.get('uptrend')}")
    if d.get("sector") is not None:
        L.append("\n[섹터 28개 · 당일/5일/20일/순매수(백만)/주도주]")
        for _, r in d["sector"].iterrows():
            L.append(f"  {r['섹터']}: 당일 {r['오늘']*100:+.1f}% · 5일 {r['d5']*100:+.1f}% · "
                     f"20일 {r['d20']*100:+.1f}% · 순매수 {r['순매수']/100:+,.0f}억 · 주도주 {r['주도주']}")
    L.append("\n[모멘텀 상위 10 · 수급은 5일 누적 억원]")
    for c in d["cands"]:
        L.append(f"  {c['name']}({c['code']}, {c['sector']}) {c['price']:,.0f}원 · "
                 f"당일 {c['chg']*100:+.1f}% · 5일 {c['ret5']*100:+.1f}% · 20일 {c['ret20']*100:+.1f}% · "
                 f"60일고점대비 {(c['hi60']-1)*100:+.1f}% · 외인 {c['frgn5']:+,.0f} · 기관 {c['orgn5']:+,.0f} · "
                 f"개인 {c['prsn5']:+,.0f}")
        if c["한줄"]:
            L.append(f"     투자포인트: {c['한줄'][:160]}")
        if c["촉매"]:
            L.append(f"     촉매: {c['촉매'].replace('||',' | ')[:220]}")
        if c["리스크"]:
            L.append(f"     리스크: {c['리스크'].replace('||',' | ')[:200]}")
    if d.get("kw"):
        L.append("\n[유튜브 키워드 급증]")
        for w, cnt, kind, why, v in d["kw"][:12]:
            lift = "NEW" if v is None else f"{v:.1f}배"
            L.append(f"  {w} {lift} ({cnt}회) — {why[:90]}")
    if d.get("yt"):
        L.append(f"\n[유튜브 분석본 {d.get('yt_day','')} · {len(d['yt'])}편]")
        for v in d["yt"][:8]:
            L.append(f"  [{v['ch']}] {v['title'][:60]}")
            L.append("   " + v["analysis"][:700].replace("\n", " ")[:700])
    return "\n".join(L)


PROMPT = """너는 한국 주식 애널리스트다. 아래 오늘의 실측 데이터만 근거로 **일일 종합 레포트**를 써라.

핵심 원칙:
- **숫자를 지어내지 마라.** 아래 데이터에 있는 값만 인용한다. 단위를 바꾸지 마라(억은 억으로).\n- 배수가 안 적힌 키워드는 배수를 추측해 쓰지 마라. 급증도 없이 언급만 한다.
- **논리를 써라.** 숫자 나열이 아니라 "왜 이 종목인가, 왜 다른 것은 아닌가" 를 논증한다.
- 게이트가 미충족이면 그 사실을 숨기지 말고, 그 전제에서 고른 '관찰 후보' 임을 명시한다.
- 쉬운 말로. 개조식 명사종결. 만연체 금지.

아래 형식을 정확히 지켜라 (마크다운):

## 오늘 시장
2~3문장. 지수·게이트 상태와 섹터 흐름을 연결해 "오늘 무슨 장이었나" 를 말한다.

## 순환매 흐름
- 어디서 어디로: 당일 강세 섹터와 5일/20일 흐름을 비교해 자금이 이동하는 방향을 짚는다. 3~4개 불릿.
- 각 불릿은 `섹터명 당일% (20일%) — 해석` 형태로 수치를 넣는다.

## 유튜브가 말하는 것
- 급증 키워드 3~4개를 근거로 시장의 관심이 어디로 쏠렸는지. 각 불릿에 배수와 이유를 넣는다.
- 모멘텀 후보와 겹치는 테마가 있으면 반드시 연결한다.

## 수급이 좋은 종목
- 외국인·기관이 동시에 사는 종목만 나열. 각각 `종목명 외인 +N억 / 기관 +N억 · 20일 +N%` 형태.
- 반대로 대량 이탈이 있는 종목도 짚는다.

## 오늘의 추천 — <종목명>(코드)
**한 줄 결론** (45자 이내)
그리고 근거를 3~5개 불릿으로. 각 불릿은 수치를 포함하고, 다음을 반드시 다뤄라:
- 수급이 왜 이 종목에서 가장 깨끗한지
- 과열이 아닌 근거 (20일 상승률·고점대비)
- 섹터가 뒷받침하는지
- 촉매(있으면)
- 이 종목의 최대 리스크 1개

## 왜 다른 종목은 아닌가
후보 중 상위 4~5개를 각각 한 줄로 탈락시켜라. `종목명 — 탈락 사유(수치 포함)`.

## 판단의 한계
2~3문장. 이 조합 규칙이 백테스트되지 않았다는 점과, 게이트 상태가 뜻하는 바를 적는다.

--- 오늘의 데이터 ---
{material}
"""


def synthesize(d):
    """LLM 으로 종합 레포트를 쓴다. 어제 손으로 쓴 한화 논증 수준을 목표로 한다
    (사용자 지적: 숫자 나열만 있고 매수 논리가 없다)."""
    material = _fmt_material(d)
    prompt = PROMPT.replace("{material}", material)
    model = os.environ.get("ARCHIVE_MODEL", "gpt-5.5")
    out = _llm(prompt, model, 6000)
    M.log(f"  🤖 종합 레포트 {model} · {len(out):,}자 · 재료 {len(material):,}자")
    return out


def pick_from(report, cands):
    """레포트의 '## 오늘의 추천 — 종목명(코드)' 에서 종목을 뽑는다."""
    m = re.search(r"오늘의 추천[^\n]*?([가-힣A-Za-z0-9·\.]+)\s*\((\d{6})\)", report)
    if m:
        code = m.group(2)
        for c in cands:
            if c["code"] == code:
                return c
    return cands[0] if cands else None


def _md_rt(t):
    """**볼드** 만 처리."""
    parts, cur, bold, i = [], "", False, 0
    while i < len(t):
        if t[i:i+2] == "**":
            if cur:
                parts.append({"type": "text", "text": {"content": cur}, "annotations": {"bold": bold}})
                cur = ""
            bold = not bold; i += 2; continue
        cur += t[i]; i += 1
    if cur:
        parts.append({"type": "text", "text": {"content": cur}, "annotations": {"bold": bold}})
    return parts or [{"type": "text", "text": {"content": t[:2000]}}]


def md_blocks(md):
    """마크다운 → 노션 블록. 레포트 본문을 페이지에 그대로 쓴다(스냅샷 아님)."""
    out = []
    for raw in md.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("## "):
            out.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": _md_rt(line[3:])}})
        elif line.startswith("### "):
            out.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": _md_rt(line[4:])}})
        elif line.lstrip().startswith(("- ", "* ")):
            out.append({"object": "block", "type": "bulleted_list_item",
                        "bulleted_list_item": {"rich_text": _md_rt(line.lstrip()[2:])}})
        else:
            out.append(_para(_md_rt(line)))
    return out


def _toggle(title, children, color="default"):
    """노션 토글. children 은 100개/요청 한도가 있어 95개로 자른다."""
    return {"object": "block", "type": "toggle", "toggle": {
        "rich_text": _rt(title, True), "color": color, "children": children[:95]}}


RO = {"id", "created_time", "last_edited_time", "created_by", "last_edited_by",
      "has_children", "archived", "in_trash", "parent", "object", "request_id"}

# 블록 타입별 허용 필드. 원본 body 를 그대로 넘기면 paragraph 에 icon 이 섞이는 식으로
# 400 이 난다. 지우기(블랙리스트)보다 허용(화이트리스트)이 안전하다 — 새 필드가 생겨도 안 샌다.
ALLOW = {
    "paragraph": {"rich_text", "color", "children"},
    "heading_1": {"rich_text", "color", "is_toggleable"},
    "heading_2": {"rich_text", "color", "is_toggleable"},
    "heading_3": {"rich_text", "color", "is_toggleable"},
    "bulleted_list_item": {"rich_text", "color", "children"},
    "numbered_list_item": {"rich_text", "color", "children"},
    "to_do": {"rich_text", "color", "checked", "children"},
    "toggle": {"rich_text", "color", "children"},
    "callout": {"rich_text", "color", "icon", "children"},
    "quote": {"rich_text", "color", "children"},
    "code": {"rich_text", "language", "caption"},
    "table": {"table_width", "has_column_header", "has_row_header", "children"},
    "table_row": {"cells"},
    "divider": set(),
    "image": {"type", "external", "file_upload", "caption"},
    "bookmark": {"url", "caption"},
}


# 노션이 받는 이미지 MIME. 응답 헤더를 무조건 믿지 않고 allowlist 로 건다 (코덱스 권고).
IMG_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
            "image/webp": ".webp", "image/svg+xml": ".svg"}


def _caption(im):
    """caption 을 재생성 가능한 형태로 정규화. _copy_image 가 ALLOW 를 우회해
    caption 을 통째로 버리고 있었다 (코덱스 지적 2026-09-02)."""
    cap = im.get("caption") or []
    return [{k: v for k, v in r.items() if k not in ("plain_text", "href")} for r in cap]


def _copy_image(blk):
    """노션 호스팅 이미지(type=file)는 API 로 재생성할 수 없다 — url 이 만료되는 서명 링크라
    external 로 걸면 곧 깨진다. 내려받아 file_upload 로 다시 올린다."""
    im = blk.get("image") or {}
    cap = _caption(im)
    if im.get("type") == "external":
        out = {"type": "external", "external": {"url": im["external"]["url"]}}
        if cap:
            out["caption"] = cap
        return {"object": "block", "type": "image", "image": out}
    url = (im.get("file") or {}).get("url")
    if not url:
        _lost("이미지 url 없음")
        return None
    try:
        r = requests.get(url, timeout=60)
        if r.status_code != 200:
            _lost(f"이미지 다운로드 실패 HTTP {r.status_code}")
            return None
        # Content-Type 에서 MIME 을 받아 확장자·업로드 타입을 맞춘다.
        # PNG 로 고정하면 JPG·GIF 가 훼손되거나 업로드가 실패한다.
        ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ct not in IMG_MIME:
            if ct:
                _lost(f"이미지 MIME 미지원 {ct} — PNG 로 시도")
            ct = "image/png"
        fid = D.upload_image(r.content, f"archive{IMG_MIME[ct]}", ct)
        if not fid:
            _lost("이미지 업로드 실패")
            return None
        out = {"type": "file_upload", "file_upload": {"id": fid}}
        if cap:
            out["caption"] = cap
        return {"object": "block", "type": "image", "image": out}
    except Exception as e:
        _lost(f"이미지 복사 예외 {str(e)[:40]}")
        return None


# 복사 과정에서 잃은 것을 모은다. 빈 복사가 '성공' 으로 보이면 안 된다
# (코덱스 지적 2026-09-02: HTTP 실패·절단·미지원 타입이 전부 무음이었다).
COPY_ISSUES = []


def _lost(what):
    COPY_ISSUES.append(what)
    M.log(f"  \u26a0\ufe0f 복사 손실: {what}")


def _strip(blk, depth=0):
    """노션 블록을 '다시 만들 수 있는' 형태로 정제한다.
    읽기전용 필드를 지우지 않으면 400 이 난다(2026-06-24 블록 이관 사고의 원인).
    표는 3단계 중첩이 안 되므로 depth 로 자른다."""
    t = blk.get("type")
    if not t:
        return None
    if t in ("child_page", "child_database", "unsupported", "synced_block"):
        _lost(f"{t} (API 로 재생성 불가)")
        return None
    if t == "image":
        return _copy_image(blk)
    # 노션은 요청 깊이 3 에서 table 을 못 만든다(모멘텀→종목토글→분기실적표).
    # 표를 통째로 버리기보다 한 줄 요약 문단으로 낮춰 정보는 남긴다.
    if t == "table" and depth >= 2:
        _lost("표 → 문단 대체 (요청 깊이 3 제한)")
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "📊 (표는 원본 리포트 참조 — 중첩 깊이 제한)"},
             "annotations": {"color": "gray", "italic": True}}]}}
    allow = ALLOW.get(t)
    if allow is None:
        _lost(f"{t} (ALLOW 미등록 — 화이트리스트에 추가 필요)")
        return None       # 그대로 넘기면 400 이 난다
    body = {k: v for k, v in (blk.get(t) or {}).items() if k in allow and k not in RO}
    for k in ("rich_text", "caption"):
        if k in body:
            body[k] = [{kk: vv for kk, vv in r.items() if kk not in ("plain_text", "href")}
                       for r in body[k]]
    if t != "divider" and not body:
        return None                       # 알맹이 없는 블록은 400 을 유발한다
    out = {"object": "block", "type": t, t: body}
    if blk.get("has_children") and depth < 2:
        kids = _fetch_children(blk["id"], depth + 1)
        if kids:
            if len(kids) > 95:
                _lost(f"{t} 자식 {len(kids)-95}개 절단 (요청당 100 한도)")
            out[t]["children"] = kids[:95]
    elif blk.get("has_children"):
        out[t].pop("children", None)
    return out


def _fetch_children(bid, depth=0):
    # has_more 를 무시하면 101번째부터 조용히 사라진다 (코덱스 지적).
    raw, cur = [], None
    while True:
        p = {"page_size": 100}
        if cur:
            p["start_cursor"] = cur
        r = requests.get(f"{API}/blocks/{bid}/children", headers=_H, params=p, timeout=30)
        if r.status_code != 200:
            _lost(f"자식 조회 실패 HTTP {r.status_code} — 이 블록 이하 전부 누락")
            break
        j = r.json()
        raw += j.get("results", [])
        if not j.get("has_more"):
            break
        cur = j.get("next_cursor")
    out = []
    for k in raw:
        c = _strip(k, depth)
        if c and c.get("type") and c.get(c["type"]) is not None:
            out.append(c)
    return out


def dashboard_copy():
    """통합 대시보드의 블록을 **원문 그대로** 복사한다.
    재생성하면 중복이고 원본과 갈라진다(사용자 지적 2026-09-02).
    가공은 '오늘의 추천' 하나만 한다."""
    out = []
    try:
        _, _, _, mid, tail = D._layout(D.page_id())
    except Exception as e:
        M.log(f"  ⚠️ 대시보드 읽기 실패: {str(e)[:70]}"); return out
    # 보유 현황(앵커~divider 사이)
    held = [c for c in (_strip(b) for b in mid) if c]
    if held:
        out.append(_toggle("💰 보유 현황", held, "gray_background"))
    # 리포트 토글들 — 제목·색까지 원본 유지
    for b in tail:
        if b.get("type") != "toggle":
            continue
        c = _strip(b)
        if c and c.get("type") and c.get(c["type"]) is not None:
            out.append(c)
    M.log(f"  📋 대시보드 원문 복사: {len(out)}개 섹션")
    return out


def _sections(md):
    """## 헤딩으로 레포트를 쪼갠다 → [(제목, [본문줄])]."""
    out, cur, body = [], None, []
    for ln in md.split("\n"):
        m = re.match(r"^##\s+(.*?)\s*$", ln)
        if m:
            if cur is not None:
                out.append((cur, body))
            cur, body = m.group(1), []
        elif cur is not None:
            body.append(ln)
    if cur is not None:
        out.append((cur, body))
    return out


def _bullets(lines):
    return [re.sub(r"^\s*[-*\u2022\u00b7]\s*", "", l).strip().replace("**", "")
            for l in lines if re.match(r"^\s*[-*\u2022\u00b7]\s+", l)]


def _prose(lines):
    return [l.strip().replace("**", "") for l in lines
            if l.strip() and not re.match(r"^\s*[-*\u2022\u00b7]\s+", l)]


def _table(head, rows):
    """페이지 직속 표만 만든다. 토글 안에 넣으면 table_row 가 요청 깊이 3 이라 400 이 난다."""
    def row(vals, bold=False):
        return {"object": "block", "type": "table_row",
                "table_row": {"cells": [_rt(v, bold) for v in vals]}}
    return {"object": "block", "type": "table", "table": {
        "table_width": len(head), "has_column_header": True, "has_row_header": False,
        "children": [row(head, True)] + [row(r) for r in rows]}}


# 서술형 섹션은 토글로 내리고, 판단에 쓰는 표·불릿은 펼치지 않아도 보이게 한다
# (2026-09-02 사용자 지적: 추천이 블록 9번에 묻혀 있고 레포트가 접혀 아무것도 안 보인다).
PROSE_SECS = ["오늘 시장", "순환매 흐름", "유튜브가 말하는 것", "판단의 한계"]


def _leftovers(bullets, parsed_n, label):
    """표 파싱이 놓친 줄은 불릿으로 살린다. 형식이 바뀌어도 내용이 조용히 사라지지 않게."""
    if parsed_n >= len(bullets):
        return []
    miss = bullets[parsed_n:] if parsed_n else bullets
    M.log(f"  \u26a0\ufe0f {label} {len(miss)}줄 표 파싱 실패 \u2014 불릿으로 대체")
    return [{"object": "block", "type": "bulleted_list_item",
             "bulleted_list_item": {"rich_text": _rt(t)}} for t in miss]


def data_asof():
    """모멘텀 입력이 어느 날짜 것인지. CSV 에 날짜 컬럼이 없어 커밋 시각으로 판정한다.
    이 저장소를 pull 하는 스케줄이 없어 이틀 묵은 후보로 추천이 나간 적이 있다(2026-09-04)."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%cI", "--",
                              "latest_momentum_reco.csv"], cwd=_DIR,
                             capture_output=True, text=True, timeout=15)
        return (out.stdout or "").strip()[:10]
    except Exception as e:
        M.log(f"  ⚠️ 기준일 확인 실패 — 낡음 검사 불가: {str(e)[:60]}")
        return ""


def stale_days(asof):
    """입력 기준일이 최근 영업일보다 며칠 뒤쳐졌나. 주말은 세지 않는다."""
    if not asof:
        return 0
    try:
        a = datetime.strptime(asof, "%Y-%m-%d").date()
    except ValueError:
        return 0
    cur = datetime.now(KST).date()
    n = 0
    while cur > a:
        if cur.weekday() < 5:
            n += 1
        cur -= timedelta(days=1)
    return n


def build_blocks(d, report, pick, gate_ok):
    secs = dict(_sections(report))
    b = []

    # ① 추천 — 맨 위. 종목·가격·한 줄 결론을 클릭 없이 본다.
    rec_key = next((k for k in secs if k.startswith("오늘의 추천")), None)
    rec_body = secs.get(rec_key, [])
    one = next(iter(_prose(rec_body)), "")
    if pick:
        head = f"오늘의 추천 — {pick['name']} ({pick['code']})  ·  {pick['price']:,.0f}원"
        sub = one or ("게이트 통과 후보" if gate_ok else "게이트 미충족 — 관찰 후보")
        if not gate_ok:
            sub += "  ·  게이트 미충족이라 확정 매수가 아닌 관찰 후보"
        b.append({"object": "block", "type": "callout", "callout": {
            "icon": {"type": "emoji", "emoji": "🎯"},
            "color": "blue_background" if gate_ok else "orange_background",
            "rich_text": _rt(head, True) + _rt(f"\n{sub}")}})
        b.append(_table(["외국인", "기관", "20일", "60일 고점대비"],
                        [[f"{pick['frgn5']:+,.0f}억", f"{pick['orgn5']:+,.0f}억",
                          f"{pick['ret20']*100:+.1f}%", f"{(pick['hi60']-1)*100:+.1f}%"]]))
        for t in _bullets(rec_body)[:5]:
            b.append({"object": "block", "type": "bulleted_list_item",
                      "bulleted_list_item": {"rich_text": _rt(t)}})

    # 입력이 낡았으면 추천 바로 아래에 드러낸다. pull 이 실패해도 조용히 나가면 안 된다.
    asof = data_asof()
    sd = stale_days(asof)
    if sd >= 1:
        b.append({"object": "block", "type": "callout", "callout": {
            "icon": {"type": "emoji", "emoji": "🚨"}, "color": "red_background",
            "rich_text": _rt(f"입력 데이터가 {sd}영업일 낡음 — 모멘텀 후보 기준일 {asof}", True)
                         + _rt("\nGitHub Actions 산출물을 못 받은 상태다. "
                               "이 추천은 오늘 후보가 아니라 그날 후보에서 고른 것이다.",
                               color="gray")}})

    # ② 게이트 — 추천 바로 아래 (전제이므로 추천보다 뒤)
    b.append({"object": "block", "type": "callout", "callout": {
        "icon": {"type": "emoji", "emoji": "🧭"},
        "color": "green_background" if gate_ok else "red_background",
        "rich_text": _rt(f"게이트 {'통과 — 신규 진입 가능' if gate_ok else '미충족 — 신규 진입 보류'}", True)
                     + _rt(f"\n{(d.get('trend') or {}).get('text','')}", color="gray")}})

    # ③ 수급 표 — 같은 형태가 반복되는 목록은 불릿보다 표 (사용자 반복 요청)
    sup = []
    for t in _bullets(secs.get("수급이 좋은 종목", [])):
        m = re.match(r"^(.+?)\s+외인\s*([+\-][\d,.]+\s*억)\s*/\s*기관\s*([+\-][\d,.]+\s*억)"
                     r"[^\d+\-]*20일\s*([+\-][\d.]+%)", t)
        if m:
            sup.append([m.group(1).strip(), m.group(2), m.group(3), m.group(4)])
    sup_all = _bullets(secs.get("수급이 좋은 종목", []))
    if sup:
        b.append(_h3("외국인·기관이 같이 사는 종목"))
        b.append(_table(["종목", "외국인", "기관", "20일"], sup))
    b += _leftovers(sup_all, len(sup), "수급")

    # ④ 탈락 표
    rej = []
    for t in _bullets(secs.get("왜 다른 종목은 아닌가", [])):
        # 종목명 안의 하이픈(S-Oil)과 구분자를 구별한다 — 긴 대시 먼저, 그다음 '띄어쓰기 하이픈'
        m = (re.match(r"^(.+?)\s*[\u2014\u2013]\s*(.+)$", t)
             or re.match(r"^(.+?)\s+-\s+(.+)$", t))
        if m:
            rej.append([m.group(1).strip(), m.group(2).strip()])
    rej_all = _bullets(secs.get("왜 다른 종목은 아닌가", []))
    if rej:
        b.append(_h3("왜 다른 종목은 아닌가"))
        b.append(_table(["종목", "탈락 사유"], rej))
    b += _leftovers(rej_all, len(rej), "탈락")

    # ⑤ 서술형은 토글로
    detail = []
    for name in PROSE_SECS:
        if name not in secs:
            continue
        detail.append(_h3(name))
        for t in _prose(secs[name]):
            detail.append(_para(_rt(t)))
        for t in _bullets(secs[name]):
            detail.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _rt(t)}})
    if detail:
        b.append(_toggle("📖 시장 해설 — 순환매·유튜브·한계", detail, "gray_background"))

    b += dashboard_copy()
    if COPY_ISSUES:
        from collections import Counter
        cnt = Counter(COPY_ISSUES)
        detail = " · ".join(f"{k} ×{v}" if v > 1 else k for k, v in cnt.most_common(6))
        b.append({"object": "block", "type": "callout", "callout": {
            "icon": {"type": "emoji", "emoji": "⚠️"}, "color": "yellow_background",
            "rich_text": _rt(f"원문 복사 중 {len(COPY_ISSUES)}건 누락", True)
                         + _rt(f"\n{detail}", color="gray")}})
    return b


def upsert(today, blocks, pick, gate_ok, summary):
    q = requests.post(f"{API}/databases/{DB}/query", headers=_H, timeout=30,
                      json={"filter": {"property": "날짜", "date": {"equals": today}}, "page_size": 1})
    props = {
        "이름": {"title": [{"type": "text", "text": {"content": summary[:120]}}]},
        "날짜": {"date": {"start": today}},
        "게이트": {"select": {"name": "통과" if gate_ok else "미충족"}},
    }
    if pick:
        props["추천종목"] = {"rich_text": [{"type": "text", "text": {"content": pick["name"]}}]}
        props["종목코드"] = {"rich_text": [{"type": "text", "text": {"content": pick["code"]}}]}
        props["진입가"] = {"number": float(pick["price"])}

    existing = (q.json().get("results") or [None])[0] if q.status_code == 200 else None
    if existing:
        pid = existing["id"]
        requests.patch(f"{API}/pages/{pid}", headers=_H, json={"properties": props}, timeout=30)
        # 첫 100개만 지우면 잔여 블록이 아래에 남아 누적된다 (코덱스 지적)
        dead, cur = [], None
        while True:
            pp = {"page_size": 100}
            if cur:
                pp["start_cursor"] = cur
            jj = requests.get(f"{API}/blocks/{pid}/children", headers=_H,
                              params=pp, timeout=30).json()
            dead += jj.get("results", [])
            if not jj.get("has_more"):
                break
            cur = jj.get("next_cursor")
        for k in dead:
            requests.delete(f"{API}/blocks/{k['id']}", headers=_H, timeout=30)
        M.log(f"  기존 {today} 행 갱신")
    else:
        r = requests.post(f"{API}/pages", headers=_H, timeout=30,
                          json={"parent": {"database_id": DB}, "properties": props})
        if r.status_code != 200:
            M.log(f"❌ 행 생성 실패: {r.text[:200]}"); return None
        pid = r.json()["id"]
        M.log(f"  {today} 행 생성")
    for i in range(0, len(blocks), 40):
        rr = requests.patch(f"{API}/blocks/{pid}/children", headers=_H, timeout=60,
                            json={"children": blocks[i:i+40]})
        if rr.status_code != 200:
            M.log(f"  ⚠️ 블록 추가 {rr.status_code}: {rr.text[:600]}")
    return pid


def evaluate():
    """과거 행의 진입가와 현재가를 비교해 평가수익률을 채운다."""
    tok = token()
    q = requests.post(f"{API}/databases/{DB}/query", headers=_H, timeout=30,
                      json={"filter": {"property": "종목코드", "rich_text": {"is_not_empty": True}},
                            "sorts": [{"property": "날짜", "direction": "descending"}], "page_size": 100})
    n = 0
    for p in q.json().get("results", []):
        pr = p["properties"]
        code = "".join(x["plain_text"] for x in pr.get("종목코드", {}).get("rich_text", []))
        entry = (pr.get("진입가") or {}).get("number")
        if not code or not entry:
            continue
        s = M.score_today(code, tok)
        if not s:
            continue
        ret = s["price"] / entry - 1
        requests.patch(f"{API}/pages/{p['id']}", headers=_H, timeout=30, json={"properties": {
            "평가수익률": {"number": round(ret, 4)},
            "평가일": {"date": {"start": datetime.now(KST).strftime("%Y-%m-%d")}}}})
        nm = "".join(x["plain_text"] for x in pr.get("추천종목", {}).get("rich_text", []))
        dt = (pr.get("날짜", {}).get("date") or {}).get("start", "")
        M.log(f"  {dt} {nm}({code}) {entry:,.0f} → {s['price']:,.0f} = {ret*100:+.1f}%")
        n += 1
    M.log(f"✅ {n}건 평가 갱신")


def main():
    if "--eval" in sys.argv:
        evaluate(); return
    today = datetime.now(KST).strftime("%Y-%m-%d")
    tok = token()
    M.log(f"▶ 일일 아카이브 {today}")
    d = collect(tok)
    M.log(f"  재료: 섹터 {0 if d.get('sector') is None else len(d['sector'])}행 · "
          f"후보 {len(d['cands'])}개 · 유튜브 {len(d.get('yt',[]))}편 · 키워드 {len(d.get('kw',[]))}개")
    if not d["cands"]:
        M.log("❌ 모멘텀 후보 없음 — 중단 (latest_momentum_reco.csv 확인)"); return
    report = synthesize(d)
    if not report:
        M.log("❌ 종합 레포트 실패 — 중단"); return
    gate_ok = bool((d.get("trend") or {}).get("uptrend"))
    pick = pick_from(report, d["cands"])
    m = re.search(r"^##\s*오늘 시장\s*$", report, re.M)
    summary = ""
    if m:
        rest = [l.strip() for l in report[m.end():].split("\n") if l.strip()]
        summary = rest[0][:110] if rest else ""
    if not summary:
        summary = f"{'게이트 통과' if gate_ok else '게이트 미충족'} · 추천 {pick['name'] if pick else '없음'}"
    blocks = build_blocks(d, report, pick, gate_ok)
    pid = upsert(today, blocks, pick, gate_ok, summary)
    M.log(f"✅ 완료 · 추천 {pick['name'] if pick else '없음'} · 블록 {len(blocks)}개" if pid else "❌ 실패")


if __name__ == "__main__":
    main()
