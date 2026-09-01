"""일일 아카이브 — 대시보드 4개 리포트를 읽어 종합 레포트를 쓰고 Report DB 에 날짜별로 남긴다.

통합 대시보드는 매 실행마다 덮어써서 과거가 안 남는다(사용자 지적 2026-09-01).
나중에 '그때 추천이 실제로 올랐나' 를 평가하려면 추천 시점의 종목·진입가가 보존돼야 한다.
→ 추천종목·종목코드·진입가·게이트를 **DB 속성**으로 남겨 정렬·필터·집계가 되게 한다.

실행: python daily_archive.py        (하루 1회, 장 마감 후)
평가: python daily_archive.py --eval  (과거 행의 평가수익률 채우기)
"""
import os, sys, json, re
import _env
import requests
from datetime import datetime

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
        h = h[h["date"] == h["date"].max()].sort_values("오늘", ascending=False)
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
    try:
        from openai import OpenAI
        c = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        r = c.chat.completions.create(model=model, max_completion_tokens=6000,
                                      messages=[{"role": "user", "content": prompt}])
        out = (r.choices[0].message.content or "").strip()
        M.log(f"  🤖 종합 레포트 {model} · {len(out):,}자 · 재료 {len(material):,}자")
        return out
    except Exception as e:
        M.log(f"  ⚠️ {model} 실패 → Gemini 폴백: {str(e)[:90]}")
    try:
        from google import genai
        cc = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"timeout": GENAI_TIMEOUT_MS})
        r = cc.models.generate_content(model="gemini-2.5-flash", contents=prompt,
                config={"max_output_tokens": 8000, "thinking_config": {"thinking_budget": 0}})
        return (r.text or "").strip()
    except Exception as e:
        M.log(f"  ❌ 종합 실패: {str(e)[:90]}")
        return ""


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


def build_blocks(d, report, pick, gate_ok):
    b = [{"object": "block", "type": "callout", "callout": {
        "icon": {"type": "emoji", "emoji": "🧭"},
        "color": "green_background" if gate_ok else "red_background",
        "rich_text": _rt(f"게이트 {'통과 — 신규 진입 가능' if gate_ok else '미충족 — 신규 진입 보류'}", True)
                     + _rt(f"\n{(d.get('trend') or {}).get('text','')}", color="gray")}}]
    b += md_blocks(report)
    if pick:
        b.append({"object": "block", "type": "callout", "callout": {
            "icon": {"type": "emoji", "emoji": "📌"}, "color": "gray_background",
            "rich_text": _rt(f"기록: {pick['name']}({pick['code']}) 진입가 {pick['price']:,.0f}원", True)
              + _rt(f"\n외인 {pick['frgn5']:+,.0f}억 · 기관 {pick['orgn5']:+,.0f}억 · "
                    f"20일 {pick['ret20']*100:+.1f}% · 60일고점대비 {(pick['hi60']-1)*100:+.1f}%", color="gray")}})
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
        for k in requests.get(f"{API}/blocks/{pid}/children", headers=_H,
                              params={"page_size": 100}, timeout=30).json().get("results", []):
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
            M.log(f"  ⚠️ 블록 추가 {rr.status_code}: {rr.text[:150]}")
    return pid


def evaluate():
    """과거 행의 진입가와 현재가를 비교해 평가수익률을 채운다."""
    tok = token()
    q = requests.post(f"{API}/databases/{DB}/query", headers=_H, timeout=30,
                      json={"filter": {"property": "종목코드", "rich_text": {"is_not_empty": True}},
                            "sorts": [{"property": "날짜", "direction": "descending"}], "page_size": 60})
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
