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


def read_dashboard():
    """대시보드 토글들을 긁어 원문을 보존한다. 종합 레포트의 근거이자 나중 재검증용."""
    def kids(b, n=60):
        return requests.get(f"{API}/blocks/{b}/children", headers=_H,
                            params={"page_size": n}, timeout=30).json().get("results", [])
    def tx(k):
        t = k["type"]
        return "".join(x["plain_text"] for x in k.get(t, {}).get("rich_text", []))

    _, _, _, mid, tail = D._layout(D.page_id())
    out = {"보유": [tx(b) for b in mid if tx(b)]}
    for b in tail:
        if b["type"] != "toggle":
            continue
        title = tx(b)
        key = ("섹터" if "섹터" in title else "모멘텀게이트" if "게이트" in title
               else "모멘텀" if "모멘텀" in title else "유튜브" if "유튜브" in title
               else "핵심요약" if "핵심 요약" in title else None)
        if not key:
            continue
        rows = []
        for k in kids(b["id"]):
            if k["type"] == "table":
                for r in kids(k["id"], 20):
                    rows.append(" | ".join("".join(x["plain_text"] for x in c).replace("\n", " ")
                                           for c in r["table_row"]["cells"]))
            elif k["type"] == "toggle":
                rows.append("▸ " + tx(k))
            elif tx(k):
                rows.append(tx(k))
        out[key] = {"title": title, "rows": rows}
    return out


def synthesize(dash, tok):
    """4개 리포트 → 종합 판단. 추천 1종목과 근거를 만든다."""
    trend = M.kospi_trend(tok)
    gate_ok = bool(trend and trend.get("uptrend"))

    # 모멘텀 후보 코드 추출
    cands = []
    for r in (dash.get("모멘텀") or {}).get("rows", []):
        m = re.search(r"([가-힣A-Za-z0-9·\.]+)\s+[+\-−][\d.]+%\s+\((\d{6})\)", r)
        if m:
            cands.append((m.group(1), m.group(2)))
    scored = []
    for name, code in cands[:10]:
        s = M.score_today(code, tok)
        fl = M.investor_flows(code, tok) or {}
        if not s:
            continue
        f5, o5 = fl.get("frgn5", 0), fl.get("orgn5", 0)
        # 수급이 양쪽 다 (+) 이고 과열이 아닌 것을 선호. 검증된 규칙이 아니라 후보 정렬용.
        both = 1 if (f5 > 0 and o5 > 0) else 0
        scored.append({"name": name, "code": code, "price": s["price"], "chg": s["chg"],
                       "ret5": s["ret5"], "ret20": s["ret20"], "hi60": s["hi60"],
                       "frgn5": f5, "orgn5": o5, "both": both,
                       "rank": (both, -abs(s["ret20"]), f5 + o5)})
    scored.sort(key=lambda x: (-x["both"], x["ret20"], -(x["frgn5"] + x["orgn5"])))
    pick = scored[0] if scored else None
    return trend, gate_ok, scored, pick


def build_blocks(dash, trend, gate_ok, scored, pick, today):
    b = []
    b.append({"object": "block", "type": "callout", "callout": {
        "icon": {"type": "emoji", "emoji": "🧭"},
        "color": "green_background" if gate_ok else "red_background",
        "rich_text": _rt(f"게이트 {'통과 — 신규 진입 가능' if gate_ok else '미충족 — 신규 진입 보류'}", True)
                     + _rt(f"\n{(trend or {}).get('text','')}", color="gray")}})

    b.append(_h2("📊 종합 판단"))
    sec = (dash.get("섹터") or {}).get("rows", [])
    sec_head = next((r for r in sec if "강세" in r or "덜 빠진" in r), "")
    if sec_head:
        b.append(_bul(_rt("순환매  ", True) + _rt(sec_head.split("\n")[0][:180])))
    yt = (dash.get("핵심요약") or {}).get("rows", [])
    kw = next((r for r in yt if "급증" in r or "처음 등장" in r), "")
    if kw:
        b.append(_bul(_rt("유튜브  ", True) + _rt(kw.replace("\n", " ")[:180])))
    if scored:
        good = [s for s in scored if s["both"]]
        b.append(_bul(_rt("수급  ", True) + _rt(
            f"후보 {len(scored)}개 중 외국인·기관 동시 순매수 {len(good)}개"
            + (f" — {', '.join(s['name'] for s in good[:4])}" if good else " (없음)"))))

    if pick:
        b.append(_h2("🎯 오늘의 추천"))
        b.append({"object": "block", "type": "callout", "callout": {
            "icon": {"type": "emoji", "emoji": "🎯"},
            "color": "gray_background" if gate_ok else "yellow_background",
            "rich_text": _rt(f"{pick['name']} ({pick['code']})  {pick['price']:,.0f}원", True)
              + _rt(f"\n오늘 {pick['chg']*100:+.1f}% · 5일 {pick['ret5']*100:+.1f}% · "
                    f"20일 {pick['ret20']*100:+.1f}% · 60일고점대비 {(pick['hi60']-1)*100:+.1f}%"
                    f"\n외국인 5일 {pick['frgn5']:+,.0f}억 · 기관 5일 {pick['orgn5']:+,.0f}억")
              + _rt("\n" + ("게이트 통과 구간의 추천입니다."
                            if gate_ok else
                            "⚠️ 게이트 미충족 상태의 '관찰 후보'입니다. 검증된 규칙은 지금 신규 진입을 권하지 않습니다."),
                    True, "red" if not gate_ok else "green")}})
        b.append(_para(_rt("선정 방식: 모멘텀 상위 10 중 ①외국인·기관 동시 순매수 ②20일 상승률이 낮은(미과열) "
                           "순. 이 조합은 백테스트되지 않았으므로 후보 정렬용입니다.", color="gray")))

    b.append(_h2("📎 원문 스냅샷"))
    for key in ("섹터", "모멘텀", "모멘텀게이트", "유튜브", "핵심요약", "보유"):
        v = dash.get(key)
        if not v:
            continue
        rows = v["rows"] if isinstance(v, dict) else v
        title = v["title"] if isinstance(v, dict) else "보유 현황"
        b.append({"object": "block", "type": "toggle", "toggle": {
            "rich_text": _rt(title[:100], True), "color": "default",
            "children": [_para(_rt(r[:1900])) for r in rows[:40] if r]}})
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
    dash = read_dashboard()
    M.log(f"  대시보드 수집: {[k for k in dash if dash.get(k)]}")
    trend, gate_ok, scored, pick = synthesize(dash, tok)
    summary = (f"{'게이트 통과' if gate_ok else '게이트 미충족'} · "
               + (f"추천 {pick['name']}" if pick else "추천 없음"))
    blocks = build_blocks(dash, trend, gate_ok, scored, pick, today)
    pid = upsert(today, blocks, pick, gate_ok, summary)
    M.log(f"✅ 아카이브 완료: {summary}" if pid else "❌ 아카이브 실패")


if __name__ == "__main__":
    main()
